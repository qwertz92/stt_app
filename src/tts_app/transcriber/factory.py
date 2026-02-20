from __future__ import annotations

from ..config import DEFAULT_ENGINE
from ..settings_store import AppSettings
from .base import ITranscriber
from .assemblyai_provider import AssemblyAITranscriber
from .groq_provider import GroqTranscriber
from .local_faster_whisper import LocalFasterWhisperTranscriber
from .remote_placeholders import (
    AzureTranscriber,
    DeepgramTranscriber,
    OpenAITranscriber,
)


def create_transcriber(
    settings: AppSettings,
    secret_store=None,
) -> ITranscriber:
    if settings.engine == DEFAULT_ENGINE:
        return LocalFasterWhisperTranscriber(
            model_size=settings.model_size,
            language_mode=settings.language_mode,
            vad_filter=settings.vad_enabled,
            offline_mode=settings.offline_mode,
            model_dir=settings.model_dir,
        )
    if settings.engine == "assemblyai":
        api_key = ""
        if secret_store is not None:
            api_key = secret_store.get_api_key("assemblyai") or ""
        return AssemblyAITranscriber(
            api_key=api_key,
            language_mode=settings.language_mode,
        )
    if settings.engine == "groq":
        api_key = ""
        if secret_store is not None:
            api_key = secret_store.get_api_key("groq") or ""
        return GroqTranscriber(
            api_key=api_key,
            language_mode=settings.language_mode,
            model=settings.groq_model,
        )
    if settings.engine == "openai":
        return OpenAITranscriber()
    if settings.engine == "azure":
        return AzureTranscriber()
    if settings.engine == "deepgram":
        return DeepgramTranscriber()

    # Unknown engine — fall back to local provider.
    return LocalFasterWhisperTranscriber(
        model_size=settings.model_size,
        language_mode=settings.language_mode,
        vad_filter=settings.vad_enabled,
        offline_mode=settings.offline_mode,
        model_dir=settings.model_dir,
    )
