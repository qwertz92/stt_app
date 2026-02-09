from __future__ import annotations

from ..config import DEFAULT_ENGINE
from ..settings_store import AppSettings
from .base import ITranscriber
from .local_faster_whisper import LocalFasterWhisperTranscriber
from .remote_placeholders import AzureTranscriber, DeepgramTranscriber, OpenAITranscriber


def create_transcriber(settings: AppSettings) -> ITranscriber:
    if settings.engine == DEFAULT_ENGINE:
        return LocalFasterWhisperTranscriber(
            model_size=settings.model_size,
            language_mode=settings.language_mode,
            vad_filter=settings.vad_enabled,
        )
    if settings.engine == "openai":
        return OpenAITranscriber()
    if settings.engine == "azure":
        return AzureTranscriber()
    if settings.engine == "deepgram":
        return DeepgramTranscriber()

    return LocalFasterWhisperTranscriber(
        model_size=settings.model_size,
        language_mode=settings.language_mode,
        vad_filter=settings.vad_enabled,
    )
