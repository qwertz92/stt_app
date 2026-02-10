"""AssemblyAI remote transcription provider (Phase 2).

Batch transcription via the AssemblyAI Python SDK.
Requires: pip install assemblyai
API key stored via keyring (settings_dialog / secret_store).

The provider uses Universal-3-Pro + Universal-2 speech models with
automatic language detection enabled.
"""

from __future__ import annotations

import tempfile
import wave
from pathlib import Path

from ..config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from .base import AudioInput, ITranscriber, StreamingCallback, TranscriptionError


def _default_assemblyai():
    """Lazy import to avoid hard dependency at module level."""
    try:
        import assemblyai as aai  # type: ignore

        return aai
    except ImportError:
        raise TranscriptionError(
            "The 'assemblyai' package is not installed. "
            "Install it with: pip install assemblyai  "
            "(or: uv add assemblyai)"
        )


class AssemblyAITranscriber(ITranscriber):
    """Batch transcription using AssemblyAI's REST API via the official SDK.

    Parameters
    ----------
    api_key : str
        AssemblyAI API key (required).
    language_mode : str
        ``"auto"`` for automatic language detection,
        or a language code like ``"de"`` / ``"en"``.
    aai_module :
        Injected ``assemblyai`` module (for testing).
    """

    def __init__(
        self,
        api_key: str,
        language_mode: str = "auto",
        *,
        aai_module=None,
    ) -> None:
        if not api_key:
            raise TranscriptionError(
                "AssemblyAI API key is missing. "
                "Enter your key in Settings → Remote Provider API Keys."
            )
        self._api_key = api_key
        self._language_mode = (language_mode or "auto").strip().lower()
        self._aai = aai_module  # None → lazy import on first use

    def _get_aai(self):
        if self._aai is None:
            self._aai = _default_assemblyai()
        return self._aai

    def _configure(self):
        """Set API key on the assemblyai global settings."""
        aai = self._get_aai()
        aai.settings.api_key = self._api_key

    def _build_config(self):
        """Build a TranscriptionConfig for the current language mode."""
        aai = self._get_aai()

        kwargs: dict = {
            "speech_models": [
                aai.SpeechModel.universal_3_pro,
                aai.SpeechModel.universal_2,
            ],
        }

        if self._language_mode == "auto":
            kwargs["language_detection"] = True
        else:
            # Map short codes to AssemblyAI language codes.
            _LANG_MAP = {
                "de": "de",
                "en": "en",
                "es": "es",
                "fr": "fr",
                "pt": "pt",
                "it": "it",
            }
            lang_code = _LANG_MAP.get(self._language_mode)
            if lang_code:
                kwargs["language_code"] = lang_code
                kwargs["language_detection"] = False
            else:
                # Unknown language code → fall back to auto detection.
                kwargs["language_detection"] = True

        return aai.TranscriptionConfig(**kwargs)

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        """Transcribe audio via AssemblyAI batch API.

        Accepts WAV bytes, a file path, or a Path object.
        """
        self._configure()
        aai = self._get_aai()

        temp_path: Path | None = None
        try:
            if isinstance(audio_source, bytes):
                # Write WAV bytes to a temp file for the SDK.
                with tempfile.NamedTemporaryFile(
                    suffix=".wav", delete=False
                ) as handle:
                    handle.write(audio_source)
                    temp_path = Path(handle.name)
                file_path = str(temp_path)
            else:
                file_path = str(audio_source)

            config = self._build_config()
            transcriber = aai.Transcriber()
            transcript = transcriber.transcribe(file_path, config=config)

            if transcript.status == aai.TranscriptStatus.error:
                raise TranscriptionError(
                    f"AssemblyAI transcription failed: {transcript.error}"
                )

            text = transcript.text or ""
            return text.strip()

        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"AssemblyAI transcription failed: {exc}"
            ) from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    # -- Streaming stubs (Phase 2b) ------------------------------------------

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "AssemblyAI streaming is planned for Phase 2b and not yet implemented. "
            "Use batch mode with AssemblyAI, or use local provider for streaming."
        )

    def push_audio_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("AssemblyAI streaming is not yet implemented.")

    def stop_stream(self) -> str:
        raise NotImplementedError("AssemblyAI streaming is not yet implemented.")

    def abort_stream(self) -> None:
        raise NotImplementedError("AssemblyAI streaming is not yet implemented.")
