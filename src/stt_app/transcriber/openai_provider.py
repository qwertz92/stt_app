"""OpenAI remote transcription provider (batch only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import (
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_OPENAI_MODEL,
    OPENAI_MODELS,
    language_modes_for_selection,
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

OPENAI_API_BASE = "https://api.openai.com/v1"


class OpenAITranscriber(ProgressReporter, ITranscriber):
    def __init__(
        self,
        api_key: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        model: str = DEFAULT_OPENAI_MODEL,
        request_timeout_s: int = 120,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "OpenAI API key is missing. "
                "Enter your key in Settings -> Remote Provider API Keys."
            )
        self._api_key = api_key
        self._model = model if model in OPENAI_MODELS else DEFAULT_OPENAI_MODEL
        self._language_mode = (language_mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if self._language_mode not in language_modes_for_selection(
            "openai",
            self._model,
        ):
            self._language_mode = DEFAULT_LANGUAGE_MODE
        self._request_timeout_s = max(5, int(request_timeout_s))

    def _auth_header(self) -> str:
        return f"Bearer {self._api_key}"

    def _format_error(self, exc: Exception) -> str:
        if _is_ssl_error(exc):
            return format_ssl_error_message("OpenAI")
        return str(exc)

    def _normalize_text(self, value: str) -> str:
        return normalize_transcript_text(value)

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        try:
            if isinstance(audio_source, bytes):
                audio_bytes = bytes(audio_source)
                filename = "audio.wav"
            else:
                path = Path(audio_source)
                audio_bytes = path.read_bytes()
                filename = path.name or "audio.wav"

            fields: list[tuple[str, str]] = [("model", self._model)]
            if self._language_mode != DEFAULT_LANGUAGE_MODE:
                fields.append(("language", self._language_mode))
            fields.append(("response_format", "json"))

            body, content_type = multipart_form_data(
                fields=fields,
                file_field=("file", filename, audio_bytes, "audio/wav"),
            )

            req = urllib.request.Request(
                f"{OPENAI_API_BASE}/audio/transcriptions",
                data=body,
                method="POST",
            )
            req.add_header("Authorization", self._auth_header())
            req.add_header("Content-Type", content_type)

            ssl_ctx = create_ssl_context()
            self._emit_progress(
                "Uploading audio to OpenAI and waiting for transcription..."
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
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise TranscriptionError(
                    "OpenAI: Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                ) from exc
            if exc.code == 429:
                raise TranscriptionError(
                    "OpenAI: Rate limit exceeded (HTTP 429). "
                    "Wait a moment and try again."
                ) from exc
            detail = exc.reason or "unknown error"
            raise TranscriptionError(
                f"OpenAI transcription failed (HTTP {exc.code}): {detail}"
            ) from exc
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"OpenAI transcription failed: {self._format_error(exc)}"
            ) from exc

    def test_connection(self) -> tuple[bool, str]:
        model_name = urllib.parse.quote(self._model, safe="")
        req = urllib.request.Request(
            f"{OPENAI_API_BASE}/models/{model_name}",
            method="GET",
        )
        req.add_header("Authorization", self._auth_header())
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
        return False, "Unexpected response from OpenAI API."

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "OpenAI streaming is disabled in this project. "
            "Use batch mode, or use local/AssemblyAI/Deepgram for streaming."
        )

    def push_audio_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("OpenAI streaming is disabled in this project.")

    def stop_stream(self) -> str:
        raise NotImplementedError("OpenAI streaming is disabled in this project.")

    def abort_stream(self) -> None:
        raise NotImplementedError("OpenAI streaming is disabled in this project.")
