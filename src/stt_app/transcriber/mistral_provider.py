"""Mistral Voxtral remote transcription provider (batch only).

`POST https://api.mistral.ai/v1/audio/transcriptions`, multipart, with
`model`, `file`, an optional `language` and repeated `context_bias` fields;
the answer is JSON with a `text` field. Read on the vendor's pages on
2026-09-27; `docs/agents/remote-providers.md` quotes them.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from ..config import (
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MISTRAL_MODEL,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    MISTRAL_MODELS,
    language_modes_for_selection,
    parse_custom_vocabulary,
    remote_batch_part_limit,
)
from ..ssl_utils import create_ssl_context
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._audio_parts import transcribe_in_parts
from ._http_utils import (
    audio_content_type,
    format_ssl_error_message,
    http_error_suffix,
    multipart_form_data,
    transcript_from_json,
)
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    TranscriptionError,
)

# The global endpoint. Mistral's regional `api.eu.mistral.ai` lists no audio
# route (https://docs.mistral.ai/inference/regional-inference, read
# 2026-09-27), and of the global one "Mistral does not commit to a specific
# inference location", so no region is offered.
MISTRAL_API_BASE = "https://api.mistral.ai/v1"
_UPLOAD_PROGRESS = "Uploading audio to Mistral and waiting for transcription..."


class MistralTranscriber(ProgressReporter, ITranscriber):
    def __init__(
        self,
        api_key: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        model: str = DEFAULT_MISTRAL_MODEL,
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
        silence_gate_threshold: float = DEFAULT_SILENCE_GATE_THRESHOLD,
        request_timeout_s: int = 300,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "Mistral API key is missing. Enter your key in Settings -> API Keys."
            )
        self._api_key = api_key
        self._model = model if model in MISTRAL_MODELS else DEFAULT_MISTRAL_MODEL
        self.set_language_mode(language_mode)
        # "up to 100 words or phrases"; `parse_custom_vocabulary` caps at 100.
        # "Context biasing is optimized for English; support for other
        # languages is experimental."
        self._context_bias = parse_custom_vocabulary(custom_vocabulary)
        self._silence_gate_threshold = float(silence_gate_threshold)
        # The request is synchronous: the answer comes once the whole part is
        # transcribed, so the socket timeout has to cover that.
        self._request_timeout_s = max(5, int(request_timeout_s))

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if normalized not in language_modes_for_selection("mistral", self._model):
            normalized = DEFAULT_LANGUAGE_MODE
        return normalized

    def _format_error(self, exc: Exception) -> str:
        if _is_ssl_error(exc):
            return format_ssl_error_message("Mistral")
        return str(exc)

    def _request_fields(self) -> list[tuple[str, str]]:
        fields: list[tuple[str, str]] = [("model", self._model)]
        if self._language_mode != DEFAULT_LANGUAGE_MODE:
            fields.append(("language", self._language_mode))
        # An array field is repeated parts in multipart form data.
        fields.extend(("context_bias", term) for term in self._context_bias)
        return fields

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        return transcribe_in_parts(
            audio_source,
            self._transcribe_request,
            limit=remote_batch_part_limit("mistral", self._model),
            progress_text=_UPLOAD_PROGRESS,
            raise_if_canceled=self._raise_if_canceled,
            silence_threshold=self._silence_gate_threshold,
        )

    def _transcribe_request(self, audio_source: AudioInput, progress_text: str) -> str:
        """One request, for a whole recording or one part of it."""
        try:
            if isinstance(audio_source, bytes):
                audio_bytes = bytes(audio_source)
                filename = "audio.wav"
            else:
                path = Path(audio_source)
                audio_bytes = path.read_bytes()
                filename = path.name or "audio.wav"
            body, content_type = multipart_form_data(
                fields=self._request_fields(),
                file_field=(
                    "file",
                    filename,
                    audio_bytes,
                    audio_content_type(filename),
                ),
            )
            request = urllib.request.Request(
                f"{MISTRAL_API_BASE}/audio/transcriptions", data=body, method="POST"
            )
            request.add_header("Authorization", f"Bearer {self._api_key}")
            request.add_header("Content-Type", content_type)
            self._emit_progress(progress_text)
            with urllib.request.urlopen(
                request, timeout=self._request_timeout_s, context=create_ssl_context()
            ) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise TranscriptionError(
                    "Mistral: Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                ) from exc
            if exc.code == 429:
                raise TranscriptionError(
                    "Mistral: Rate limit exceeded (HTTP 429). "
                    "Wait a moment and try again."
                ) from exc
            raise TranscriptionError(
                f"Mistral transcription failed (HTTP {exc.code}){http_error_suffix(exc)}"
            ) from exc
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"Mistral transcription failed: {self._format_error(exc)}"
            ) from exc
        return self._text_of(payload)

    @staticmethod
    def _text_of(payload: bytes) -> str:
        """The transcript in an answer, or an error naming what came instead.

        `"text": ""` is a valid answer (nothing said). An HTML page, JSON
        without a string `text`, or an answer that is not JSON at all is not:
        read as silence, a part of a split recording would be dropped from
        the transcript without a word.
        """
        return transcript_from_json(payload, prefix="Mistral transcription failed")

    def test_connection(self) -> tuple[bool, str]:
        request = urllib.request.Request(f"{MISTRAL_API_BASE}/models", method="GET")
        request.add_header("Authorization", f"Bearer {self._api_key}")
        try:
            with urllib.request.urlopen(
                request, timeout=10, context=create_ssl_context()
            ) as resp:
                if resp.status == 200:
                    return True, "Connection OK — API key is valid."
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, (
                    "Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                )
            return False, f"API returned HTTP {exc.code}{http_error_suffix(exc)}"
        except Exception as exc:
            return False, f"Connection failed: {self._format_error(exc)}"
        return False, "Unexpected response from the Mistral API."

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "Mistral streaming is not implemented in this project. "
            "Use batch mode, or use local/AssemblyAI/Deepgram for streaming."
        )

    def push_audio_chunk(
        self, chunk: bytes, *, block_timeout_s: float | None = None
    ) -> None:
        raise NotImplementedError(
            "Mistral streaming is not implemented in this project."
        )

    def stop_stream(self) -> str:
        raise NotImplementedError(
            "Mistral streaming is not implemented in this project."
        )

    def abort_stream(self) -> None:
        raise NotImplementedError(
            "Mistral streaming is not implemented in this project."
        )
