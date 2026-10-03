"""A bring-your-own OpenAI-compatible endpoint (batch only).

The user supplies a base URL, a model and a key -- or a command that prints a
short-lived token -- for any server that speaks the OpenAI REST shapes: a
LiteLLM or vLLM gateway, a local speech server (speaches, LocalAI), or a
hosted OpenAI-compatible API. Two request styles are offered, because a
gateway may route audio only to a multimodal LLM:

- ``transcriptions``: ``POST {base}/audio/transcriptions``, multipart, the
  OpenAI speech API.
- ``chat``: ``POST {base}/chat/completions`` with an ``input_audio`` content
  part, answered by a model that listens and writes the transcript.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    CUSTOM_API_MODE_CHAT,
    CUSTOM_API_MODES,
    CUSTOM_KEY_COMMAND_TIMEOUT_S,
    CUSTOM_KEY_COMMAND_TTL_S,
    DEFAULT_CUSTOM_API_MODE,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    LANGUAGE_MODE_LABELS,
    language_modes_for_selection,
    parse_custom_vocabulary,
    remote_batch_part_limit,
)
from ..process_tree import run_bounded
from ..ssl_utils import create_ssl_context
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._audio_parts import transcribe_in_parts
from ._http_utils import (
    audio_content_type,
    body_excerpt,
    format_ssl_error_message,
    http_error_suffix,
    is_markup_page,
    multipart_form_data,
    normalize_transcript_text,
    read_http_error_detail,
    transcript_from_json,
)
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    TranscriptionError,
)

logger = logging.getLogger(__name__)

_PROVIDER_NAME = "Custom endpoint"
_UPLOAD_PROGRESS = (
    "Uploading audio to the custom endpoint and waiting for transcription..."
)
_ENDPOINT_EXAMPLE = (
    "for example https://llm-gateway.example.com/v1 or http://localhost:8000/v1"
)
_MODELS_TIMEOUT_S = 15
# What one response may put into memory. A model list of a large gateway is
# tens of kilobytes; a transcript of a five-minute part is a few.
_MAX_MODELS_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TRANSCRIPT_RESPONSE_BYTES = 8 * 1024 * 1024
# LiteLLM's `/models` carries a `mode` per entry. These can never transcribe.
_UNUSABLE_MODEL_MODES = frozenset({"embedding", "image_generation", "rerank"})
_MODEL_MODE_RANK = {"audio_transcription": 0, "chat": 1}
# Most servers send no `mode` (OpenAI's own list does not), so a model
# whose id names a speech recognizer is listed before the rest of its
# group. It orders, never filters: an id says nothing reliable.
_SPEECH_MODEL_NAME = re.compile(
    r"whisper|transcri|speech|voxtral|parakeet|canary|sensevoice|paraformer"
    r"|funasr|wav2vec|moonshine|scribe|(?:^|[-_/.:])(?:asr|stt)(?:$|[-_/.:])",
    re.IGNORECASE,
)
_SPEECH_SYNTHESIS_NAME = re.compile(
    r"text-to-speech|(?:^|[-_/.:])tts(?:$|[-_/.:\d])", re.IGNORECASE
)
# A base URL pasted together with one of the routes the app appends.
_PASTED_ROUTES = ("/audio/transcriptions", "/chat/completions", "/models")
_ERROR_TAIL_MAX_CHARS = 200
_CODE_FENCE = re.compile(r"^```[\w+-]*[ \t]*\n?(.*?)\n?```$", re.DOTALL)
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("„", "“"))


@dataclass(frozen=True)
class CustomEndpointModel:
    """One entry of the endpoint's model list; ``mode`` is "" when unknown."""

    id: str
    mode: str = ""


def normalize_custom_endpoint(endpoint: str) -> str:
    """The base URL requests are built on: stripped, no trailing slash.

    Nothing is appended: a gateway may serve the OpenAI routes under `/v1`,
    under another prefix or at its root, so the URL is used as given --
    except that a route the app appends itself (`/audio/transcriptions`,
    `/chat/completions`, `/models`) is taken off the end, because users
    paste the request URL their gateway documents, and the app then posted
    to `.../audio/transcriptions/audio/transcriptions` and got a 404.
    """
    value = str(endpoint or "").strip().rstrip("/")
    if not value:
        raise TranscriptionError(
            "Custom endpoint URL is missing. Enter the base URL of an "
            f"OpenAI-compatible API ({_ENDPOINT_EXAMPLE}) in Settings -> "
            "Providers."
        )
    try:
        parsed = urllib.parse.urlsplit(value)
        _port = parsed.port
    except ValueError as exc:
        raise TranscriptionError(f"Custom endpoint URL is invalid: {exc}") from exc
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise TranscriptionError(
            "Custom endpoint URL must start with https:// or http://, "
            f"{_ENDPOINT_EXAMPLE}."
        )
    if parsed.username is not None or parsed.password is not None:
        raise TranscriptionError(
            "Custom endpoint URL must not contain user credentials; enter the "
            "key in its own field."
        )
    if parsed.query or parsed.fragment:
        raise TranscriptionError(
            "Custom endpoint URL must be a base URL without a query or "
            f"fragment, {_ENDPOINT_EXAMPLE}."
        )
    lowered = value.lower()
    for route in _PASTED_ROUTES:
        if lowered.endswith(route):
            return value[: -len(route)].rstrip("/")
    return value


_DEFAULT_PORTS = {"http": 80, "https": 443}
_DISPLAY_URL_MAX_CHARS = 200


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    return (
        scheme,
        (parts.hostname or "").lower(),
        parts.port or _DEFAULT_PORTS.get(scheme),
    )


def _redirect_keeps_origin(old_url: str, new_url: str) -> bool:
    """Whether a redirect stays where the key may go.

    The same scheme, host and port, or the upgrade from http to https on the
    default ports of one host. Anything else -- another host, another port, or
    https to http, which would send the key in clear -- is another origin.
    """
    try:
        old_scheme, old_host, old_port = _origin(old_url)
        new_scheme, new_host, new_port = _origin(new_url)
    except ValueError:
        return False
    if not old_host or old_host != new_host:
        return False
    if (old_scheme, old_port) == (new_scheme, new_port):
        return True
    return (old_scheme, old_port, new_scheme, new_port) == ("http", 80, "https", 443)


def _display_url(url: str) -> str:
    """Scheme, host, port and path of a URL the server named; no query."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "an invalid URL"
    return f"{parts.scheme}://{host}{port}{parts.path}"[:_DISPLAY_URL_MAX_CHARS]


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only where it cannot leak the key or drop the audio.

    urllib's own handler copies every header, `Authorization` included, to
    whatever host `Location` names (reproduced: 127.0.0.1:A -> localhost:B
    received the Bearer key), and turns a redirected POST into a GET without
    its body, which the endpoint answers with an HTTP 405 that names nothing
    the user can act on. An upload is therefore never redirected, and a model
    list only within its origin.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = _display_url(newurl)
        if req.data is not None:
            refusal = (
                f"{_PROVIDER_NAME}: the endpoint redirected the upload (HTTP "
                f"{code}) to {target}. Enter the base URL the server redirects "
                "to in Settings -> Providers."
            )
        elif not _redirect_keeps_origin(req.full_url, newurl):
            refusal = (
                f"{_PROVIDER_NAME}: the endpoint redirected (HTTP {code}) to "
                f"another origin, {target}; the key is not sent there. Enter "
                "that base URL in Settings -> Providers if it is the right "
                "server."
            )
        else:
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        try:
            fp.close()
        except Exception:
            pass
        raise TranscriptionError(refusal)


def _open(request: urllib.request.Request, *, timeout: float):
    """`urlopen` with the system trust store and `_RedirectGuard`."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=create_ssl_context()),
        _RedirectGuard(),
    )
    return opener.open(request, timeout=timeout)


def _names_a_speech_recognizer(model_id: str) -> bool:
    return bool(_SPEECH_MODEL_NAME.search(model_id)) and not bool(
        _SPEECH_SYNTHESIS_NAME.search(model_id)
    )


def _header_unsafe(value: str) -> bool:
    """Whether a Bearer value holds a character an HTTP header cannot carry.

    Visible ASCII only: `http.client` refuses a line break with the whole
    header value in its message, and encodes the rest as Latin-1, so a
    pasted key with a stray newline, a non-breaking space or a typographic
    quote fails in a way that would name the key.
    """
    return any(not ("\x21" <= character <= "\x7e") for character in value)


def _command_arguments(command: str) -> list[str]:
    """Split a key command into arguments without a shell.

    Windows paths keep their backslashes (`posix=False`), which also keeps the
    quotes around an argument; those are removed here.
    """
    try:
        arguments = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise TranscriptionError(f"The key command cannot be parsed: {exc}") from exc
    return [
        argument[1:-1]
        if len(argument) >= 2 and argument[0] == argument[-1] == '"'
        else argument
        for argument in arguments
    ]


def _last_line(text: str) -> str:
    """The last non-empty line, whole. A token is often a JWT of 800+ chars."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _error_tail(text: str) -> str:
    """The last non-empty line, capped for an error message."""
    return _last_line(text)[:_ERROR_TAIL_MAX_CHARS]


def _strip_reply_wrapping(text: str) -> str:
    """Remove a code fence or one pair of quotes an LLM put around its reply."""
    value = str(text or "").strip()
    fenced = _CODE_FENCE.match(value)
    if fenced:
        value = fenced.group(1).strip()
    for opening, closing in _QUOTE_PAIRS:
        if len(value) >= 2 and value.startswith(opening) and value.endswith(closing):
            value = value[len(opening) : -len(closing)].strip()
            break
    return value


class CustomEndpointTranscriber(ProgressReporter, ITranscriber):
    def __init__(
        self,
        api_key: str = "",
        endpoint: str = "",
        model: str = "",
        api_mode: str = DEFAULT_CUSTOM_API_MODE,
        key_command: str = "",
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
        request_timeout_s: int = 120,
        silence_gate_threshold: float = DEFAULT_SILENCE_GATE_THRESHOLD,
    ) -> None:
        ProgressReporter.__init__(self)
        # Decides whether a part of a split recording that came back empty
        # held sound (`transcribe_in_parts`).
        self._silence_gate_threshold = float(silence_gate_threshold)
        self._api_key = str(api_key or "").strip()
        self._key_command = str(key_command or "").strip()
        if not self._api_key and not self._key_command:
            raise TranscriptionError(
                "Custom endpoint API key is missing. A server without "
                "authentication accepts any placeholder such as 'none', or "
                "set a key command. Enter your key in Settings -> Providers."
            )
        if self._api_key and _header_unsafe(self._api_key):
            # Never echoed: the key is the one thing this message must
            # not contain.
            raise TranscriptionError(
                "Custom endpoint API key contains a line break, a space or "
                "another character an HTTP header cannot carry. Enter it "
                "again in Settings -> Providers."
            )
        self._base_url = normalize_custom_endpoint(endpoint)
        self._model = str(model or "").strip()
        mode = str(api_mode or "").strip().lower()
        self._api_mode = mode if mode in CUSTOM_API_MODES else DEFAULT_CUSTOM_API_MODE
        self.set_language_mode(language_mode)
        self._request_timeout_s = max(5, int(request_timeout_s))
        self._terms = parse_custom_vocabulary(custom_vocabulary)
        # Chat backends that refuse `reasoning_effort` are remembered for the
        # life of this runtime, so the refusal costs one request, not one per
        # dictation.
        self._send_reasoning_effort = True
        self._token_lock = threading.Lock()
        self._cached_token = ""
        self._cached_token_expires_at = 0.0

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if normalized not in language_modes_for_selection("custom", self._model):
            normalized = DEFAULT_LANGUAGE_MODE
        return normalized

    # -- Credentials -------------------------------------------------------

    def _run_key_command(self) -> str:
        """Run the key command and return the token it printed.

        No shell, no console window, no stdin. The token is the last non-empty
        line of its output, so a helper that prints a notice first still works.
        Nothing here puts the token into a log line or an exception.

        `run_bounded`, not `subprocess.run`: a helper that starts a
        program of its own (`wsl.exe -e ...`, a `.cmd` wrapper) hands it
        the pipes, and `subprocess.run` then reads them until that program
        exits -- 15.1 s on a 2 s timeout, reproduced.
        """
        arguments = _command_arguments(self._key_command)
        if not arguments:
            raise TranscriptionError("The key command is empty.")
        extra: dict[str, object] = {}
        if os.name == "nt":
            extra["creationflags"] = subprocess.CREATE_NO_WINDOW
        started = time.monotonic()
        try:
            completed = run_bounded(
                arguments,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CUSTOM_KEY_COMMAND_TIMEOUT_S,
                **extra,
            )
        except FileNotFoundError as exc:
            raise TranscriptionError(
                f"The key command was not found: {arguments[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TranscriptionError(
                "The key command did not finish within "
                f"{CUSTOM_KEY_COMMAND_TIMEOUT_S:.0f} s."
            ) from exc
        except OSError as exc:
            raise TranscriptionError(
                f"The key command could not be started: {exc}"
            ) from exc
        token = _last_line(completed.stdout or "")
        if token and _header_unsafe(token):
            raise TranscriptionError(
                "The key command printed a token with a space or a "
                "character an HTTP header cannot carry."
            )
        if completed.returncode != 0 or not token:
            reason = _error_tail(completed.stderr or "")
            detail = f": {reason}" if reason else ""
            if completed.returncode == 0:
                raise TranscriptionError(
                    f"The key command printed no token (exit code 0){detail}"
                )
            raise TranscriptionError(
                f"The key command failed (exit code {completed.returncode}){detail}"
            )
        logger.info(
            "custom_endpoint_key_command_ok elapsed_ms=%d",
            int((time.monotonic() - started) * 1000),
        )
        return token

    def _token(self, *, refresh: bool = False) -> str:
        """The Bearer token: the key command's (cached) or the stored key."""
        if not self._key_command:
            return self._api_key
        with self._token_lock:
            now = time.monotonic()
            if (
                refresh
                or not self._cached_token
                or now >= self._cached_token_expires_at
            ):
                self._cached_token = self._run_key_command()
                self._cached_token_expires_at = now + CUSTOM_KEY_COMMAND_TTL_S
            return self._cached_token

    # -- HTTP --------------------------------------------------------------

    def _read(
        self, request: urllib.request.Request, timeout: float, max_bytes: int
    ) -> bytes:
        with _open(request, timeout=timeout) as resp:
            payload = resp.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise TranscriptionError(
                f"{_PROVIDER_NAME}: the response is larger than "
                f"{max_bytes // (1024 * 1024)} MB."
            )
        if is_markup_page(payload):
            # A proxy's sign-in or block page answers HTTP 200. Read as a
            # plain-text transcript it was pasted into the document; its
            # text is left out of the message, it is not the server's.
            raise TranscriptionError(
                f"{_PROVIDER_NAME}: the endpoint answered with an HTML page "
                "instead of JSON -- typically a proxy's sign-in or block "
                "page, or a base URL that points at a website. Check the "
                f"base URL ({_ENDPOINT_EXAMPLE}) and whether the proxy "
                "needs a sign-in."
            )
        return payload

    def _send(
        self,
        build: Callable[[str], urllib.request.Request],
        *,
        timeout: float,
        max_bytes: int,
    ) -> bytes:
        """Send one request, retrying once with a fresh token after a 401.

        Only a key command can produce a fresh token; a stored key that is
        refused is refused again, so it fails at once.
        """
        try:
            return self._read(build(self._token()), timeout, max_bytes)
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or not self._key_command:
                raise
            exc.close()
            logger.info("custom_endpoint_token_refresh reason=http_401")
        return self._read(build(self._token(refresh=True)), timeout, max_bytes)

    def _request(
        self, path: str, token: str, *, data: bytes | None, content_type: str = ""
    ) -> urllib.request.Request:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            method="POST" if data is not None else "GET",
        )
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", "application/json")
        if content_type:
            request.add_header("Content-Type", content_type)
        return request

    def _http_error(
        self, exc: urllib.error.HTTPError, what: str, detail: str | None = None
    ) -> TranscriptionError:
        if exc.code == 401:
            source = (
                "the token the key command printed"
                if self._key_command
                else "the API key"
            )
            return TranscriptionError(
                f"{_PROVIDER_NAME}: authentication failed (HTTP 401); the "
                f"endpoint refused {source}."
            )
        if exc.code == 429:
            return TranscriptionError(
                f"{_PROVIDER_NAME}: rate limit exceeded (HTTP 429). Wait a "
                "moment and try again."
            )
        suffix = f": {detail}" if detail else http_error_suffix(exc)
        hint = (
            f" Check the base URL ({_ENDPOINT_EXAMPLE}) and the API style."
            if exc.code in (404, 405)
            else ""
        )
        return TranscriptionError(
            f"{_PROVIDER_NAME} {what} failed (HTTP {exc.code}){suffix}{hint}"
        )

    def _other_error(self, exc: Exception, what: str) -> TranscriptionError:
        if _is_ssl_error(exc):
            return TranscriptionError(format_ssl_error_message(_PROVIDER_NAME))
        if "invalid header" in str(exc).lower():
            # `http.client` puts the refused header value -- the Bearer
            # token -- into its message. The checks above keep such a
            # value from being sent; this keeps any other road to that
            # message from showing it.
            return TranscriptionError(
                f"{_PROVIDER_NAME} {what} failed: a request header was "
                f"refused ({type(exc).__name__})."
            )
        return TranscriptionError(f"{_PROVIDER_NAME} {what} failed: {exc}")

    # -- Model list --------------------------------------------------------

    def list_models(self) -> list[CustomEndpointModel]:
        """The models the endpoint offers, the likeliest transcribers first.

        `GET {base}/models`. Entries whose `mode` says they cannot transcribe
        (embeddings, image generation, rerankers) are left out; an entry
        without a `mode` is kept, since most servers do not send one.
        """
        try:
            payload = self._send(
                lambda token: self._request("/models", token, data=None),
                timeout=_MODELS_TIMEOUT_S,
                max_bytes=_MAX_MODELS_RESPONSE_BYTES,
            )
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, "model list") from exc
        except TranscriptionError:
            raise
        except Exception as exc:
            raise self._other_error(exc, "model list") from exc
        try:
            parsed = json.loads(payload.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise TranscriptionError(
                f"{_PROVIDER_NAME}: the model list is not JSON. Check the base "
                f"URL ({_ENDPOINT_EXAMPLE})."
            ) from exc
        entries = parsed.get("data") if isinstance(parsed, dict) else parsed
        if not isinstance(entries, list):
            raise TranscriptionError(
                f"{_PROVIDER_NAME}: the model list has no 'data' array."
            )
        models: dict[str, CustomEndpointModel] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            mode = entry.get("mode")
            mode = mode.strip().lower() if isinstance(mode, str) else ""
            if mode in _UNUSABLE_MODEL_MODES:
                continue
            models.setdefault(
                model_id.strip(), CustomEndpointModel(model_id.strip(), mode)
            )
        return sorted(
            models.values(),
            key=lambda model: (
                _MODEL_MODE_RANK.get(model.mode, 2),
                0 if _names_a_speech_recognizer(model.id) else 1,
                model.id.lower(),
            ),
        )

    def test_connection(self) -> tuple[bool, str]:
        try:
            models = self.list_models()
        except TranscriptionError as exc:
            return False, str(exc)
        if self._model and self._model not in {model.id for model in models}:
            return False, (
                f"Connected, but the endpoint does not offer the model "
                f"'{self._model}'. Fetch the model list and pick one it offers."
            )
        return True, f"Connection OK: the endpoint offers {len(models)} models."

    # -- Transcription -----------------------------------------------------

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        if not self._model:
            raise TranscriptionError(
                "No model is selected for the custom endpoint. Enter or fetch "
                "one on the Transcription tab."
            )
        return transcribe_in_parts(
            audio_source,
            self._transcribe_request,
            limit=remote_batch_part_limit("custom", self._model, self._api_mode),
            progress_text=_UPLOAD_PROGRESS,
            raise_if_canceled=self._raise_if_canceled,
            silence_threshold=self._silence_gate_threshold,
        )

    def _transcribe_request(self, audio_source: AudioInput, progress_text: str) -> str:
        try:
            if isinstance(audio_source, (bytes, bytearray)):
                audio_bytes = bytes(audio_source)
                filename = "audio.wav"
            else:
                path = Path(audio_source)
                audio_bytes = path.read_bytes()
                filename = path.name or "audio.wav"
            self._emit_progress(progress_text)
            if self._api_mode == CUSTOM_API_MODE_CHAT:
                return self._chat_transcribe(audio_bytes, filename)
            return self._transcriptions_transcribe(audio_bytes, filename)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, "transcription") from exc
        except TranscriptionError:
            raise
        except Exception as exc:
            raise self._other_error(exc, "transcription") from exc

    def _transcription_fields(self) -> list[tuple[str, str]]:
        fields: list[tuple[str, str]] = [("model", self._model)]
        if self._language_mode != DEFAULT_LANGUAGE_MODE:
            fields.append(("language", self._language_mode))
        if self._terms:
            fields.append(("prompt", ", ".join(self._terms)))
        fields.append(("response_format", "json"))
        return fields

    def _transcriptions_transcribe(self, audio_bytes: bytes, filename: str) -> str:
        body, content_type = multipart_form_data(
            fields=self._transcription_fields(),
            file_field=("file", filename, audio_bytes, audio_content_type(filename)),
        )
        payload = self._send(
            lambda token: self._request(
                "/audio/transcriptions", token, data=body, content_type=content_type
            ),
            timeout=self._request_timeout_s,
            max_bytes=_MAX_TRANSCRIPT_RESPONSE_BYTES,
        )
        # The request asks for `response_format=json`, so a body that is not
        # JSON -- "Internal Server Error" from a proxy -- is an error, not a
        # transcript to paste (review of 2026-10-01).
        return transcript_from_json(
            payload, prefix=_PROVIDER_NAME, accept_bare_string=True
        )

    def _chat_instruction(self) -> str:
        parts = [
            (
                "Transcribe the speech in the attached audio verbatim, in the "
                "language that is spoken. Add punctuation and capitalization. "
                "Do not translate, summarize or comment. Reply with the "
                "transcript only, without quotation marks or formatting."
            )
        ]
        if self._language_mode != DEFAULT_LANGUAGE_MODE:
            language = LANGUAGE_MODE_LABELS.get(
                self._language_mode, self._language_mode
            )
            parts.append(f"The speech is in {language}.")
        if self._terms:
            parts.append(
                f"Spell these terms exactly as written: {', '.join(self._terms)}."
            )
        parts.append("If the audio contains no speech, reply with nothing.")
        return " ".join(parts)

    def _chat_body(self, audio_bytes: bytes, filename: str) -> bytes:
        audio_format = Path(filename).suffix.lower().lstrip(".") or "wav"
        body: dict[str, object] = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": self._chat_instruction()},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(audio_bytes).decode("ascii"),
                                "format": audio_format,
                            },
                        }
                    ],
                },
            ],
        }
        if self._send_reasoning_effort:
            # A reasoning model otherwise spends hundreds of tokens thinking
            # before it writes a transcript, which tripled the latency on the
            # gateway this was measured against.
            body["reasoning_effort"] = "low"
        return json.dumps(body).encode("utf-8")

    def _post_chat(self, body: bytes) -> bytes:
        return self._send(
            lambda token: self._request(
                "/chat/completions",
                token,
                data=body,
                content_type="application/json",
            ),
            timeout=self._request_timeout_s,
            max_bytes=_MAX_TRANSCRIPT_RESPONSE_BYTES,
        )

    def _chat_transcribe(self, audio_bytes: bytes, filename: str) -> str:
        try:
            payload = self._post_chat(self._chat_body(audio_bytes, filename))
        except urllib.error.HTTPError as exc:
            if exc.code != 400 or not self._send_reasoning_effort:
                raise
            # Read once: the body is a stream, and the error below needs it.
            detail = read_http_error_detail(exc)
            lowered = detail.lower()
            if "reasoning" not in lowered and "thinking" not in lowered:
                raise self._http_error(exc, "transcription", detail) from exc
            self._send_reasoning_effort = False
            logger.info(
                "custom_endpoint_reasoning_effort_disabled model=%s reason=http_400",
                self._model,
            )
            payload = self._post_chat(self._chat_body(audio_bytes, filename))
        return self._chat_text(payload)

    @staticmethod
    def _chat_text(payload: bytes) -> str:
        try:
            parsed = json.loads(payload.decode("utf-8", errors="replace"))
            choice = parsed["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise TranscriptionError(
                f"{_PROVIDER_NAME}: the chat reply has no message content."
            ) from exc
        if content is None:
            # A refusal or an answer cut off at the token limit, not
            # "nothing said": read as silence, a part of a split recording
            # vanished from the transcript (review of 2026-10-01).
            refusal = message.get("refusal")
            if isinstance(refusal, str) and refusal.strip():
                raise TranscriptionError(
                    f"{_PROVIDER_NAME}: the model refused: {body_excerpt(refusal)}"
                )
            reason = choice.get("finish_reason") if isinstance(choice, dict) else None
            raise TranscriptionError(
                f"{_PROVIDER_NAME}: the chat reply has no text "
                + (
                    f"(finish reason '{reason}')."
                    if isinstance(reason, str) and reason
                    else "(no finish reason given)."
                )
            )
        if isinstance(content, list):
            parts = [part for part in content if isinstance(part, dict)]
            texts = [
                part["text"] for part in parts if isinstance(part.get("text"), str)
            ]
            refusals = [
                part["refusal"]
                for part in parts
                if isinstance(part.get("refusal"), str) and part["refusal"].strip()
            ]
            if not texts and refusals:
                raise TranscriptionError(
                    f"{_PROVIDER_NAME}: the model refused: {body_excerpt(refusals[0])}"
                )
            content = " ".join(texts)
        if not isinstance(content, str):
            # A number or an object is no reply text; read as silence it
            # would drop a part of a split recording without a word.
            raise TranscriptionError(
                f"{_PROVIDER_NAME}: the chat reply's content is not text "
                f"({type(content).__name__})."
            )
        return normalize_transcript_text(_strip_reply_wrapping(content))

    # -- Streaming ---------------------------------------------------------

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "The custom endpoint is batch-only. Use batch mode, or use "
            "local/AssemblyAI/Deepgram for streaming."
        )

    def push_audio_chunk(
        self, chunk: bytes, *, block_timeout_s: float | None = None
    ) -> None:
        raise NotImplementedError("The custom endpoint is batch-only.")

    def stop_stream(self) -> str:
        raise NotImplementedError("The custom endpoint is batch-only.")

    def abort_stream(self) -> None:
        raise NotImplementedError("The custom endpoint is batch-only.")
