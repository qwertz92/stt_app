"""ElevenLabs remote transcription provider.

Batch transcription via the ElevenLabs Speech to Text API.
Requires: an ElevenLabs API key.
API key stored via keyring (settings_dialog / secret_store).

Supported batch models: scribe_v1, scribe_v2.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import (
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_ELEVENLABS_MODEL,
    ELEVENLABS_MODELS,
    VALID_LANGUAGE_MODES,
)
from ..ssl_utils import create_ssl_context, is_ssl_error as _is_ssl_error
from ._http_utils import (
    format_ssl_error_message,
    multipart_form_data,
    normalize_transcript_text,
)
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    TranscriptionError,
)

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"


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
                "ElevenLabs API key is missing. "
                "Enter your key in Settings -> Remote Provider API Keys."
            )
        self._api_key = api_key
        self._language_mode = (
            (language_mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        )
        if self._language_mode not in VALID_LANGUAGE_MODES:
            self._language_mode = DEFAULT_LANGUAGE_MODE
        self._model = (
            model if model in ELEVENLABS_MODELS else DEFAULT_ELEVENLABS_MODEL
        )
        self._request_timeout_s = max(5, int(request_timeout_s))

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
            fields.append(("language_code", self._language_mode))

        body, content_type = multipart_form_data(
            fields=fields,
            file_field=("file", filename, audio_bytes, "audio/wav"),
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
            ssl_ctx = create_ssl_context()
            self._emit_progress(
                "Uploading audio to ElevenLabs and waiting for transcription..."
            )
            with urllib.request.urlopen(
                req, timeout=self._request_timeout_s, context=ssl_ctx
            ) as resp:
                payload = resp.read()

            try:
                parsed = json.loads(payload.decode("utf-8", errors="replace"))
            except Exception:
                return self._normalize_text(payload.decode("utf-8", errors="replace"))

            if isinstance(parsed, dict):
                text = parsed.get("text", "")
                return self._normalize_text(str(text))
            return self._normalize_text(str(parsed))
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
            detail = exc.reason or "unknown error"
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
            ssl_ctx = create_ssl_context()
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
                if resp.status == 200:
                    return True, "Connection OK — API key is valid."
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, (
                    "Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                )
            return False, f"API returned HTTP {exc.code}: {exc.reason}"
        except Exception as exc:
            return False, f"Connection failed: {self._format_error(exc)}"
        return False, "Unexpected response from ElevenLabs API."

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "ElevenLabs streaming is not implemented in this project yet. "
            "Use batch mode, or use local/AssemblyAI/Deepgram for streaming."
        )

    def push_audio_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("ElevenLabs streaming is not implemented yet.")

    def stop_stream(self) -> str:
        raise NotImplementedError("ElevenLabs streaming is not implemented yet.")

    def abort_stream(self) -> None:
        raise NotImplementedError("ElevenLabs streaming is not implemented yet.")
