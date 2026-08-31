"""Shared HTTP helpers for REST-based remote transcription providers.

These helpers exist so the OpenAI and ElevenLabs providers (and any future
HTTP-only provider) do not duplicate identical multipart encoding and SSL
error formatting.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
from pathlib import Path

from ..config import DOC_SSL_PROXY_PATH

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


def read_http_error_detail(exc: urllib.error.HTTPError) -> str:
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
        raw = exc.read(_MAX_ERROR_BODY_BYTES).decode("utf-8", errors="replace")
    except Exception:
        return ""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw.strip()[:300]
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
        if isinstance(error, str) and error.strip():
            return error.strip()[:300]
        for key in ("message", "detail", "err_msg"):
            if parsed.get(key):
                return str(parsed[key])[:300]
    return raw.strip()[:300]


def http_error_suffix(exc: urllib.error.HTTPError) -> str:
    """`": <what the provider said>"`, falling back to the status phrase."""
    detail = read_http_error_detail(exc)
    return f": {detail}" if detail else f": {exc.reason}"


def format_ssl_error_message(provider_name: str) -> str:
    """Return the standard SSL/proxy error message for a remote provider."""
    return (
        f"{provider_name}: SSL certificate verification failed "
        "(likely a corporate proxy such as Zscaler). "
        "Set SSL_CERT_FILE or REQUESTS_CA_BUNDLE to your corporate CA .pem. "
        f"See {DOC_SSL_PROXY_PATH} for details."
    )
