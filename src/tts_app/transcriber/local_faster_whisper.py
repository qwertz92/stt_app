from __future__ import annotations

import tempfile
from pathlib import Path

from ..config import DEFAULT_LANGUAGE_MODE, DEFAULT_MODEL_SIZE
from .base import AudioInput, ITranscriber, TranscriptionError


def _default_model_factory(*args, **kwargs):
    from faster_whisper import WhisperModel  # type: ignore

    return WhisperModel(*args, **kwargs)


class LocalFasterWhisperTranscriber(ITranscriber):
    def __init__(
        self,
        model_size: str = DEFAULT_MODEL_SIZE,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        device: str = "auto",
        compute_type: str = "int8",
        vad_filter: bool = True,
        model_factory=None,
    ) -> None:
        self.model_size = model_size
        self.language_mode = language_mode
        self.device = device
        self.compute_type = compute_type
        self.vad_filter = vad_filter
        self._model_factory = model_factory or _default_model_factory
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            self._model = self._model_factory(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    def _language_arg(self) -> str | None:
        mode = (self.language_mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if mode == DEFAULT_LANGUAGE_MODE:
            return None
        return mode

    def _format_transcription_error(self, exc: Exception) -> str:
        if isinstance(exc, ModuleNotFoundError):
            missing = exc.name or "unknown"
            return (
                f"Missing dependency '{missing}'. "
                "Run `uv sync --group dev` and restart the app."
            )
        return str(exc)

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        temp_path: Path | None = None

        try:
            if isinstance(audio_source, bytes):
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                    handle.write(audio_source)
                    temp_path = Path(handle.name)
                input_for_model = str(temp_path)
            else:
                input_for_model = str(audio_source)

            model = self._ensure_model()
            segments, _info = model.transcribe(
                input_for_model,
                language=self._language_arg(),
                vad_filter=self.vad_filter,
            )

            parts = []
            for segment in segments:
                text = getattr(segment, "text", "")
                if text:
                    stripped = str(text).strip()
                    if stripped:
                        parts.append(stripped)

            return " ".join(parts).strip()

        except Exception as exc:
            detail = self._format_transcription_error(exc)
            raise TranscriptionError(f"Local transcription failed: {detail}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
