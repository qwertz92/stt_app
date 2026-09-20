"""OpenAI remote transcription provider (batch only)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import (
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_OPENAI_MODEL,
    OPENAI_ARRAY_FIELD_MODELS,
    OPENAI_KEYWORD_FORBIDDEN_CHARACTERS,
    OPENAI_MODELS,
    language_modes_for_selection,
    parse_custom_vocabulary,
)
from ..ssl_utils import create_ssl_context
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._http_utils import (
    audio_content_type,
    format_ssl_error_message,
    http_error_suffix,
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

logger = logging.getLogger(__name__)

OPENAI_API_BASE = "https://api.openai.com/v1"


class OpenAITranscriber(ProgressReporter, ITranscriber):
    def __init__(
        self,
        api_key: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        model: str = DEFAULT_OPENAI_MODEL,
        request_timeout_s: int = 120,
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "OpenAI API key is missing. "
                "Enter your key in Settings -> API Keys."
            )
        self._api_key = api_key
        self._model = model if model in OPENAI_MODELS else DEFAULT_OPENAI_MODEL
        # Needs self._model, so this must run after it is assigned above.
        self.set_language_mode(language_mode)
        self._request_timeout_s = max(5, int(request_timeout_s))
        self._uses_array_fields = self._model in OPENAI_ARRAY_FIELD_MODELS
        terms = parse_custom_vocabulary(custom_vocabulary)
        # Two request shapes, one setting. `gpt-transcribe` takes the terms as
        # repeated `keywords[]` fields, which is what they are -- "Use
        # keywords for literal terms you expect to hear" -- while the three
        # older models have no such field and keep the comma-joined `prompt`.
        self._prompt = "" if self._uses_array_fields else ", ".join(terms)
        self._keywords, self._dropped_keyword_count = (
            self._split_keywords(terms) if self._uses_array_fields else ([], 0)
        )

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if normalized not in language_modes_for_selection("openai", self._model):
            normalized = DEFAULT_LANGUAGE_MODE
        return normalized

    @staticmethod
    def _split_keywords(terms: list[str]) -> tuple[list[str], int]:
        """Split the vocabulary into terms OpenAI accepts and a dropped count.

        "The API rejects the entire request when it encounters one of these
        characters", so one `<` in one term would cost the whole dictation.
        Dropping the term keeps the rest of the vocabulary and the recording.
        """
        kept = [
            term
            for term in terms
            if not any(
                character in term
                for character in OPENAI_KEYWORD_FORBIDDEN_CHARACTERS
            )
        ]
        return kept, len(terms) - len(kept)

    def _auth_header(self) -> str:
        return f"Bearer {self._api_key}"

    def _format_error(self, exc: Exception) -> str:
        if _is_ssl_error(exc):
            return format_ssl_error_message("OpenAI")
        return str(exc)

    def _normalize_text(self, value: str) -> str:
        return normalize_transcript_text(value)

    def _request_fields(self) -> list[tuple[str, str]]:
        """The multipart fields beside the audio, in the order they are sent.

        `multipart_form_data` takes a list of pairs rather than a mapping, so
        a repeated field is just the same name twice -- which is exactly what
        the guide's own example sends: `-F 'keywords[]=premium plan' -F
        'keywords[]=AC-42' -F 'languages[]=en' -F 'languages[]=fr'`.

        `response_format=json` is sent for every model. The guide's
        `gpt-transcribe` examples omit it and rely on the default, but `json`
        is the shape this provider parses (`{"text": ...}`, with the
        `languages` array beside it that the app has no use for), so it is
        stated rather than assumed.
        """
        fields: list[tuple[str, str]] = [("model", self._model)]
        if self._uses_array_fields:
            if self._language_mode != DEFAULT_LANGUAGE_MODE:
                # Never `language` as well: "languages replaces the singular
                # language field. Don't send both fields."
                fields.append(("languages[]", self._language_mode))
            fields.extend(("keywords[]", term) for term in self._keywords)
            if self._dropped_keyword_count:
                # The count, never the terms: the vocabulary is the user's own
                # text and does not belong in a log file. INFO, because
                # nothing failed -- the request goes out with the rest.
                logger.info(
                    "openai_keywords_dropped count=%d model=%s "
                    "reason=forbidden_character",
                    self._dropped_keyword_count,
                    self._model,
                )
        else:
            if self._language_mode != DEFAULT_LANGUAGE_MODE:
                fields.append(("language", self._language_mode))
            if self._prompt:
                fields.append(("prompt", self._prompt))
        fields.append(("response_format", "json"))
        return fields

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        try:
            if isinstance(audio_source, bytes):
                audio_bytes = bytes(audio_source)
                filename = "audio.wav"
            else:
                path = Path(audio_source)
                audio_bytes = path.read_bytes()
                filename = path.name or "audio.wav"

            fields = self._request_fields()

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
            # The body, not `exc.reason`: that is only the status
            # phrase ("Bad Request"), which drops the one part that
            # says what to change.
            raise TranscriptionError(
                f"OpenAI transcription failed (HTTP {exc.code})"
                f"{http_error_suffix(exc)}"
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
            return False, (
                f"API returned HTTP {exc.code}{http_error_suffix(exc)}"
            )
        except Exception as exc:
            return False, f"Connection failed: {self._format_error(exc)}"
        return False, "Unexpected response from OpenAI API."

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "OpenAI streaming is disabled in this project. "
            "Use batch mode, or use local/AssemblyAI/Deepgram for streaming."
        )

    def push_audio_chunk(
        self, chunk: bytes, *, block_timeout_s: float | None = None
    ) -> None:
        raise NotImplementedError("OpenAI streaming is disabled in this project.")

    def stop_stream(self) -> str:
        raise NotImplementedError("OpenAI streaming is disabled in this project.")

    def abort_stream(self) -> None:
        raise NotImplementedError("OpenAI streaming is disabled in this project.")
