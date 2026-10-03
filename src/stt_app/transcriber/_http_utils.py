"""Shared HTTP helpers for REST-based remote transcription providers.

These helpers exist so the OpenAI and ElevenLabs providers (and any future
HTTP-only provider) do not duplicate identical multipart encoding and SSL
error formatting.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import urllib.error
from collections.abc import Callable
from pathlib import Path

from ..config import DOC_SSL_PROXY_PATH
from .base import TranscriptionError

_AUDIO_CONTENT_TYPE_BY_SUFFIX = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


def audio_content_type(filename: str) -> str:
    """Return a deterministic audio MIME type for supported import suffixes."""
    return _AUDIO_CONTENT_TYPE_BY_SUFFIX.get(
        Path(str(filename or "")).suffix.lower(),
        "application/octet-stream",
    )


def _quoted_header_parameter(value: str, *, label: str) -> str:
    normalized = str(value)
    if "\r" in normalized or "\n" in normalized:
        raise ValueError(f"Multipart {label} must not contain CR or LF characters.")
    return normalized.replace("\\", "\\\\").replace('"', '\\"')


def multipart_form_data(
    *,
    fields: list[tuple[str, str]],
    file_field: tuple[str, str, bytes, str],
) -> tuple[bytes, str]:
    """Encode a multipart/form-data request body.

    ``file_field`` is ``(form_field_name, filename, file_bytes, content_type)``.
    Returns ``(body_bytes, content_type_header_value)``.
    """
    boundary = f"stt-app-{secrets.token_hex(24)}"
    lines: list[bytes] = []

    for name, value in fields:
        quoted_name = _quoted_header_parameter(name, label="field name")
        lines.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{quoted_name}"\r\n\r\n'
                ).encode(),
                f"{value}\r\n".encode(),
            ]
        )

    field_name, filename, data, content_type = file_field
    quoted_field_name = _quoted_header_parameter(field_name, label="file field name")
    quoted_filename = _quoted_header_parameter(filename, label="filename")
    safe_content_type = str(content_type).strip()
    if not safe_content_type or "\r" in safe_content_type or "\n" in safe_content_type:
        raise ValueError("Multipart content type must be a non-empty single line.")
    lines.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{quoted_field_name}"; '
                f'filename="{quoted_filename}"\r\n'
            ).encode(),
            f"Content-Type: {safe_content_type}\r\n\r\n".encode(),
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )

    body = b"".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def normalize_transcript_text(value: object) -> str:
    """Collapse whitespace runs and trim, defensively handling ``None``."""
    return " ".join(str(value or "").strip().split()).strip()


_BYTE_ORDER_MARK = b"\xef\xbb\xbf"
_BODY_EXCERPT_MAX_CHARS = 80

# Takes text the server sent and returns it with credentials removed. Applied
# before any cut to a maximum length: scrubbed afterwards, a key that straddles
# the cut leaves its first characters in the message (review of 2026-10-03).
Redactor = Callable[[str], str]


def _unredacted(text: str) -> str:
    return text


def is_markup_page(payload: bytes) -> bool:
    """Whether a response body is an HTML or XML page rather than JSON.

    JSON never starts with ``<``, and a proxy's sign-in or block page does,
    whatever comes first: a byte-order mark, ``<!-- ... -->``, ``<?xml ...?>``
    or a bare ``<head>``. Matching ``<!doctype html``/``<html`` only let each
    of those through as a transcript (review of 2026-10-01).
    """
    return payload.lstrip().removeprefix(_BYTE_ORDER_MARK).lstrip().startswith(b"<")


def body_excerpt(payload: bytes | str, redact: Redactor = _unredacted) -> str:
    """The start of a body on one line, short enough for an error message."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig", errors="replace")
    text = " ".join(redact(payload).split())
    if len(text) > _BODY_EXCERPT_MAX_CHARS:
        text = text[: _BODY_EXCERPT_MAX_CHARS - 3] + "..."
    return text


# What follows a page's first characters is never looked at beyond this: a
# `<title>` is in the head, and an unbounded lazy search was quadratic on a
# body of unclosed `<title>` tags (3.95 s for 64 KB, review of 2026-10-03).
_PAGE_HEAD_BYTES = 4096
_HTML_START = re.compile(rb"<(?:!doctype\s+html|html|head|body|title)\b", re.IGNORECASE)
_PAGE_TITLE = re.compile(rb"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_PAGE_TAG = re.compile(r"<[^>]*>")
_MARKUP_ERROR_HINT = "(a proxy or firewall block page?)"


def is_html_page(payload: bytes) -> bool:
    """Whether a body starts like an HTML page, as opposed to any markup.

    Narrower than `is_markup_page`: an XML error body
    (`<Error><Code>AccessDenied</Code><Message>...`) is markup but carries
    the provider's message, which calling it an HTML page threw away. The
    start is `<!doctype html`, `<html`, `<head`, `<body` or `<title`, after
    whitespace and a byte-order mark.
    """
    head = payload[:_PAGE_HEAD_BYTES].lstrip().removeprefix(_BYTE_ORDER_MARK)
    return _HTML_START.match(head.lstrip()) is not None


def markup_page_description(payload: bytes, redact: Redactor = _unredacted) -> str:
    """What to say about an HTML page that came where JSON was expected.

    The page's own `<title>` ("Zscaler - Access Denied", "413 Request Entity
    Too Large") as text only -- tags dropped, entities decoded, one line, 80
    characters -- or a fixed sentence when there is none. The page itself is
    never shown: it is a proxy's, not the provider's, and as markup it
    filled the message (review of 2026-10-03).
    """
    match = _PAGE_TITLE.search(payload[:_PAGE_HEAD_BYTES])
    if match:
        title = _PAGE_TAG.sub(
            " ", html.unescape(match.group(1).decode("utf-8", errors="replace"))
        )
        title = body_excerpt(title, redact)
        if title:
            return f'an HTML page titled "{title}" {_MARKUP_ERROR_HINT}'
    return f"an HTML page {_MARKUP_ERROR_HINT}"


def transcript_from_json(
    payload: bytes,
    *,
    prefix: str,
    accept_bare_string: bool = False,
    redact: Redactor = _unredacted,
) -> str:
    """The ``text`` of a transcription answer, or an error naming what came.

    For providers asked for JSON. ``"text": ""`` is a valid answer (nothing
    said). An HTML page, a body that is not JSON (a plain "Internal Server
    Error"), or JSON without a string ``text`` is not: read as silence or
    as a transcript, it was pasted, or a part of a split recording dropped.
    ``prefix`` starts every message ("Custom endpoint", "Mistral
    transcription failed"); ``accept_bare_string`` takes a top-level JSON
    string as the transcript.
    """
    if is_markup_page(payload):
        raise TranscriptionError(
            f"{prefix}: the answer is an HTML page instead of JSON -- "
            "typically a proxy's sign-in or block page."
        )
    try:
        parsed = json.loads(payload.decode("utf-8-sig", errors="replace"))
    except ValueError as exc:
        excerpt = body_excerpt(payload, redact)
        raise TranscriptionError(
            f"{prefix}: the answer is not JSON"
            + (f" (it begins: {excerpt})." if excerpt else " (it is empty).")
        ) from exc
    if accept_bare_string and isinstance(parsed, str):
        return normalize_transcript_text(parsed)
    value = parsed.get("text") if isinstance(parsed, dict) else None
    if not isinstance(value, str):
        error_text = reply_error_text(parsed, redact)
        if error_text:
            raise TranscriptionError(
                f"{prefix}: the server answered HTTP 200 with an error: {error_text}"
            )
        keys = (
            ", ".join(sorted(str(key) for key in parsed)[:8])
            if isinstance(parsed, dict)
            else type(parsed).__name__
        )
        raise TranscriptionError(
            f"{prefix}: the answer has no 'text' field (it holds: {keys or 'nothing'})."
        )
    return normalize_transcript_text(value)


# The recovered text goes into a user-facing error, and the overlay's error
# detail is selectable, so it can be copied out of the failure. Bounded so a
# very long dictation cannot fill the overlay end to end; the audio is kept
# for Retry either way.
RECOVERED_TEXT_MAX_CHARS = 2000


def recovered_text_suffix(finalized: list[str], current: str) -> str:
    """Name what the service had already delivered, if anything.

    Every exit of a Fun-ASR request other than `task-finished` used to discard
    the sentences already received -- measured: two finished sentences lost to
    a server close, to a read timeout, and to a `task-failed`. The WAV is kept
    for Retry, so nothing is unrecoverable, but a re-run costs the whole
    upload and transcription again and this text is often the entire
    dictation minus its last words. It is reported rather than returned:
    returning it would hand back a silently truncated transcript that reads
    exactly like a complete one. The loop over a long recording's parts
    (`_audio_parts.transcribe_in_parts`) reports the parts before a failed one
    the same way.
    """
    parts = [part for part in [*finalized, current] if part]
    # One guard, not two: an empty `parts` joins to "" and so does a list
    # of nothing but whitespace, and both reach the same answer here.
    text = normalize_transcript_text(" ".join(parts))
    if not text:
        return ""
    if len(text) > RECOVERED_TEXT_MAX_CHARS:
        text = text[:RECOVERED_TEXT_MAX_CHARS].rstrip() + "..."
    return f' Received before the failure: "{text}"'


# What is read off the socket, as opposed to what survives into the message.
# The 300-character cap below is applied to the extracted text and says
# nothing about how much reached memory first: `exc.read()` is unbounded, and
# `urlopen(timeout=...)` bounds each socket read rather than the total, so a
# 50,000,000-byte error body was pulled off in full to produce a 300-character
# detail (measured with a counting body behind a real HTTPError). 64 KiB is
# far more than any provider's JSON error object and small enough to be
# uninteresting; a body that overruns it stops parsing as JSON and falls
# through to the truncated-text arm, which is the right answer for a response
# that large.
_MAX_ERROR_BODY_BYTES = 64 * 1024


def nested_error_text(value: object) -> str:
    """The human-readable text inside a provider's nested error object, or "".

    `str()` of such an object hands the user Python dict syntax with the
    request id in it, capped mid-dict, so every provider that can receive one
    unwraps it instead -- the HTTP readers below through
    `read_http_error_detail`, Fun-ASR through its own `task-failed` header,
    which is a parsed WebSocket frame and never passes through an
    `HTTPError`. The order is the one the HTTP shapes need (a gateway's
    `{"detail": {"error": "..."}}` included) and is shared rather than
    copied so the two cannot drift.

    Uncapped on purpose: each caller applies its own cap (300 characters for
    the HTTP providers, `_FAILURE_DETAIL_MAX_CHARS` for Fun-ASR), and a
    helper that capped as well would silently apply the tighter of the two.
    """
    if not isinstance(value, dict):
        return ""
    for key in ("message", "detail", "error", "status", "code"):
        inner = value.get(key)
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return ""


def reply_error_text(parsed: object, redact: Redactor = _unredacted) -> str:
    """The text of an `error` member of a reply that answered HTTP 200, or "".

    Some gateways report a failed backend call inside a 200 reply
    (`{"error": {"message": ...}}`); read as a transcript or a chat reply it
    showed only "no 'text' field" or "no message content", which hid the one
    thing the user could act on (review of 2026-10-03). Capped like an HTTP
    error detail.
    """
    if not isinstance(parsed, dict):
        return ""
    error = parsed.get("error")
    text = error.strip() if isinstance(error, str) else nested_error_text(error)
    return redact(text)[:300]


def read_http_error_detail(
    exc: urllib.error.HTTPError, redact: Redactor = _unredacted
) -> str:
    """Return what the provider actually said, or "" when it said nothing.

    `HTTPError.reason` is only the status phrase -- "Bad Request" -- so a
    message built from it throws away the one part that tells the user what to
    change: OpenAI's "Invalid file format", ElevenLabs' quota text, Deepgram's
    rejected parameter. The body is read once (an HTTPError is a response
    object), JSON is unwrapped where the common shapes allow, and the result is
    capped so a provider cannot push an HTML error page into a dialog. The
    read itself is capped too -- see `_MAX_ERROR_BODY_BYTES`.
    """
    try:
        payload = exc.read(_MAX_ERROR_BODY_BYTES)
    except Exception:
        return ""
    if is_html_page(payload):
        # A proxy's block page (403 Zscaler, 407, nginx 413), not the
        # provider's answer: reported by its title instead of pasted.
        return f"the reply was {markup_page_description(payload, redact)}"
    # Credentials come out of the body before it is parsed and out of every
    # extracted text before it is cut to 300 characters (a JSON-escaped form of
    # a credential survives the first and is caught by the second).
    raw = redact(payload.decode("utf-8", errors="replace"))
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw.strip()[:300]
    if isinstance(parsed, dict):
        # Two nesting levels cover every documented shape: OpenAI's
        # `{"error": {"message"}}`, ElevenLabs' `{"detail": {"message"}}`,
        # Deepgram's flat `message` / `err_msg`, Azure's wrapped `error`. A
        # nested object without a `message` used to be `str()`-ed whole, so
        # the user read Python dict syntax with the request id in it, capped
        # mid-dict; the nested object's own text fields are tried first and
        # the JSON text is the last resort.
        error = parsed.get("error")
        detail = parsed.get("detail")
        if (
            isinstance(error, str)
            and error.strip()
            and isinstance(detail, str)
            and detail.strip()
        ):
            # Speechmatics' shape, `{"code": 403, "error": "Forbidden",
            # "detail": "Entitlement check failed"}`: `error` is only the
            # status phrase and `detail` is the part that says what to change,
            # so the two are kept together (unless the detail already starts
            # with the phrase).
            error, detail = error.strip(), detail.strip()
            if detail.lower().startswith(error.lower()):
                return redact(detail)[:300]
            return redact(f"{error}: {detail}")[:300]
        for key in ("error", "message", "detail", "err_msg"):
            value = parsed.get(key)
            if isinstance(value, dict):
                unwrapped = nested_error_text(value)
                if unwrapped:
                    return redact(unwrapped)[:300]
                continue
            if isinstance(value, str) and value.strip():
                return redact(value.strip())[:300]
        # Anything else -- a number, a list, an empty object -- reads better as
        # the JSON text itself than as `str()` of one field.
    return raw.strip()[:300]


def http_error_suffix(
    exc: urllib.error.HTTPError, redact: Redactor = _unredacted
) -> str:
    """`": <what the provider said>"`, falling back to the status phrase."""
    detail = read_http_error_detail(exc, redact)
    return f": {detail}" if detail else f": {exc.reason}"


def format_ssl_error_message(provider_name: str) -> str:
    """Return the standard SSL/proxy error message for a remote provider."""
    return (
        f"{provider_name}: SSL certificate verification failed "
        "(likely a corporate proxy such as Zscaler). "
        "Set SSL_CERT_FILE or REQUESTS_CA_BUNDLE to your corporate CA .pem. "
        f"See {DOC_SSL_PROXY_PATH} for details."
    )
