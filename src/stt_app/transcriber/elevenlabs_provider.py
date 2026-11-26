"""ElevenLabs remote transcription provider.

Batch transcription via the ElevenLabs Speech to Text API; streaming through the
realtime WebSocket (Scribe v3 Live, `elevenlabs_realtime.py`).
Requires: an ElevenLabs API key.
API key stored via keyring (settings_dialog / secret_store).

Supported batch model: scribe_v3.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import (
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_LANGUAGE_MODE,
    ELEVENLABS_MODELS,
    language_modes_for_selection,
)
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._http_utils import (
    audio_content_type,
    format_ssl_error_message,
    http_error_suffix,
    key_redactor,
    multipart_form_data,
    normalize_transcript_text,
    open_request,
    read_http_error_detail,
    transcript_from_json,
)
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    StreamingErrorCallback,
    TranscriptionError,
)
from .elevenlabs_realtime import ElevenLabsRealtimeSession

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"

_ELEVENLABS_LANGUAGE_CODES = {
    "af": "afr",
    "am": "amh",
    "ar": "ara",
    "as": "asm",
    "ast": "ast",
    "hy": "hye",
    "az": "aze",
    "ba": "bak",
    "be": "bel",
    "bn": "ben",
    "bo": "bod",
    "br": "bre",
    "bs": "bos",
    "bg": "bul",
    "ca": "cat",
    "yue": "yue",
    "ceb": "ceb",
    "ny": "nya",
    "zh": "zho",
    "hr": "hrv",
    "cs": "ces",
    "da": "dan",
    "nl": "nld",
    "en": "eng",
    "et": "est",
    "eu": "eus",
    "fi": "fin",
    "fo": "fao",
    "fr": "fra",
    "ff": "ful",
    "lg": "lug",
    "gl": "glg",
    "gu": "guj",
    "de": "deu",
    "el": "ell",
    "he": "heb",
    "ha": "hau",
    "haw": "haw",
    "hi": "hin",
    "ht": "hat",
    "hu": "hun",
    "is": "isl",
    "id": "ind",
    "ig": "ibo",
    "ga": "gle",
    "it": "ita",
    "ja": "jpn",
    "jw": "jav",
    "ka": "kat",
    "kn": "kan",
    "kk": "kaz",
    "kea": "kea",
    "km": "khm",
    "ko": "kor",
    "ku": "kur",
    "ky": "kir",
    "la": "lat",
    "lb": "ltz",
    "ln": "lin",
    "lo": "lao",
    "luo": "luo",
    "lv": "lav",
    "lt": "lit",
    "mk": "mkd",
    "ms": "msa",
    "mg": "mlg",
    "ml": "mal",
    "mn": "mon",
    "mr": "mar",
    "mi": "mri",
    "mt": "mlt",
    "my": "mya",
    "ne": "nep",
    "nso": "nso",
    "nn": "nno",
    "no": "nor",
    "oc": "oci",
    "or": "ori",
    "pa": "pan",
    "fa": "fas",
    "pl": "pol",
    "ps": "pus",
    "pt": "por",
    "ro": "ron",
    "ru": "rus",
    "sa": "san",
    "sd": "snd",
    "sr": "srp",
    "si": "sin",
    "sk": "slk",
    "sl": "slv",
    "sn": "sna",
    "so": "som",
    "sq": "sqi",
    "es": "spa",
    "su": "sun",
    "sw": "swa",
    "sv": "swe",
    "tl": "fil",
    "ta": "tam",
    "te": "tel",
    "tg": "tgk",
    "th": "tha",
    "tk": "tuk",
    "tr": "tur",
    "tt": "tat",
    "uk": "ukr",
    "umb": "umb",
    "ur": "urd",
    "uz": "uzb",
    "vi": "vie",
    "cy": "cym",
    "wo": "wol",
    "xh": "xho",
    "yi": "yid",
    "yo": "yor",
    "zu": "zul",
}


class ElevenLabsTranscriber(ProgressReporter, ITranscriber):
    def __init__(
        self,
        api_key: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        model: str = DEFAULT_ELEVENLABS_MODEL,
        request_timeout_s: int = 120,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "ElevenLabs API key is missing. Enter your key in Settings -> Providers."
            )
        self._api_key = api_key
        self._scrub = key_redactor(api_key)
        self._model = model if model in ELEVENLABS_MODELS else DEFAULT_ELEVENLABS_MODEL
        # Needs self._model, so this must run after it is assigned above.
        self.set_language_mode(language_mode)
        self._request_timeout_s = max(5, int(request_timeout_s))
        self._stream_lock = threading.Lock()
        self._stream_session: ElevenLabsRealtimeSession | None = None

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if normalized not in language_modes_for_selection("elevenlabs", self._model):
            normalized = DEFAULT_LANGUAGE_MODE
        return normalized

    def _auth_header(self) -> str:
        return self._api_key

    def _format_error(self, exc: Exception) -> str:
        if _is_ssl_error(exc):
            return format_ssl_error_message("ElevenLabs")
        return str(exc)

    def _normalize_text(self, value: str) -> str:
        return normalize_transcript_text(value)

    def _build_request(
        self,
        audio_bytes: bytes,
        filename: str,
    ) -> urllib.request.Request:
        fields: list[tuple[str, str]] = [("model_id", self._model)]
        if self._language_mode != DEFAULT_LANGUAGE_MODE:
            fields.append(
                (
                    "language_code",
                    _ELEVENLABS_LANGUAGE_CODES.get(
                        self._language_mode,
                        self._language_mode,
                    ),
                )
            )

        body, content_type = multipart_form_data(
            fields=fields,
            file_field=(
                "file",
                filename,
                audio_bytes,
                audio_content_type(filename),
            ),
        )

        req = urllib.request.Request(
            f"{ELEVENLABS_API_BASE}/speech-to-text",
            data=body,
            method="POST",
        )
        req.add_header("xi-api-key", self._auth_header())
        req.add_header("Content-Type", content_type)
        return req

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        try:
            if isinstance(audio_source, bytes):
                audio_bytes = bytes(audio_source)
                filename = "audio.wav"
            else:
                path = Path(audio_source)
                audio_bytes = path.read_bytes()
                filename = path.name or "audio.wav"

            req = self._build_request(audio_bytes, filename)
            self._emit_progress(
                "Uploading audio to ElevenLabs and waiting for transcription..."
            )
            with open_request(
                req, timeout=self._request_timeout_s, provider_name="ElevenLabs"
            ) as resp:
                payload = resp.read()

            return transcript_from_json(
                payload, prefix="ElevenLabs transcription failed", redact=self._scrub
            )
        except FileNotFoundError as exc:
            raise TranscriptionError(
                "ElevenLabs transcription failed: missing file path. "
                "This can happen when the input file does not exist or when "
                "TEMP/TMP points to a non-existent folder."
            ) from exc
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise TranscriptionError(
                    "ElevenLabs: Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                ) from exc
            if exc.code == 429:
                raise TranscriptionError(
                    "ElevenLabs: Rate limit exceeded (HTTP 429). "
                    "Wait a moment and try again."
                ) from exc
            # The body, not `exc.reason`: that is only the status
            # phrase, and ElevenLabs puts the quota and format
            # reasons in the body.
            detail = (
                read_http_error_detail(exc, self._scrub)
                or exc.reason
                or ("unknown error")
            )
            raise TranscriptionError(
                f"ElevenLabs transcription failed (HTTP {exc.code}): {detail}"
            ) from exc
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"ElevenLabs transcription failed: {self._format_error(exc)}"
            ) from exc

    def test_connection(self) -> tuple[bool, str]:
        req = urllib.request.Request(
            f"{ELEVENLABS_API_BASE}/user",
            method="GET",
        )
        req.add_header("xi-api-key", self._auth_header())
        try:
            with open_request(req, timeout=10, provider_name="ElevenLabs") as resp:
                if resp.status == 200:
                    return True, "Connection OK — API key is valid."
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, (
                    "Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                )
            return False, (
                f"API returned HTTP {exc.code}{http_error_suffix(exc, self._scrub)}"
            )
        except Exception as exc:
            return False, f"Connection failed: {self._format_error(exc)}"
        return False, "Unexpected response from ElevenLabs API."

    # -- Streaming (Scribe v3 Live; the session is `elevenlabs_realtime.py`) -------

    def _get_websocket_module(self):
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise TranscriptionError(
                "ElevenLabs streaming requires 'websocket-client'. "
                "Install it with: pip install websocket-client"
            ) from exc
        return websocket

    def _streaming_language_code(self) -> str | None:
        """The chosen language as the realtime `language_code`, or None for Auto.

        The realtime API takes ISO 639-1 or 639-3 codes and detects the
        language itself when none is sent; the same code table as batch is used.
        """
        if self._language_mode == DEFAULT_LANGUAGE_MODE:
            return None
        return _ELEVENLABS_LANGUAGE_CODES.get(self._language_mode, self._language_mode)

    def start_stream(
        self,
        on_partial: StreamingCallback | None = None,
        on_error: StreamingErrorCallback | None = None,
    ) -> None:
        websocket = self._get_websocket_module()
        with self._stream_lock:
            if self._stream_session is not None:
                raise TranscriptionError("Streaming session already active.")
            session = ElevenLabsRealtimeSession(
                api_key=self._api_key,
                language_code=self._streaming_language_code(),
                websocket_module=websocket,
                on_partial=on_partial,
                on_error=on_error,
            )
            self._stream_session = session
        try:
            session.connect()
        except BaseException:
            self._release_session(session)
            raise

    def _release_session(self, session: ElevenLabsRealtimeSession) -> None:
        with self._stream_lock:
            if self._stream_session is session:
                self._stream_session = None

    def push_audio_chunk(
        self, chunk: bytes, *, block_timeout_s: float | None = None
    ) -> None:
        session = self._stream_session
        if session is None:
            raise TranscriptionError("Streaming session is not active.")
        session.push(chunk, block_timeout_s=block_timeout_s)

    def stop_stream(self) -> str:
        session = self._stream_session
        if session is None:
            raise TranscriptionError("Streaming session is not active.")
        try:
            return session.finish()
        finally:
            # Also after a refused stop of a session that is still connecting:
            # `connect` sees the retired state and tears the socket down.
            self._release_session(session)

    def abort_stream(self) -> None:
        with self._stream_lock:
            session, self._stream_session = self._stream_session, None
        if session is not None:
            session.abort()
