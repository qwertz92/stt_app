from __future__ import annotations

from ..config import DEFAULT_ENGINE
from ..settings_store import AppSettings
from .base import ITranscriber
from .assemblyai_provider import AssemblyAITranscriber
from .deepgram_provider import DeepgramTranscriber
from .groq_provider import GroqTranscriber
from .local_faster_whisper import LocalFasterWhisperTranscriber
from .openai_provider import OpenAITranscriber


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
        api_key = ""
        if secret_store is not None:
            api_key = secret_store.get_api_key("openai") or ""
        return OpenAITranscriber(
            api_key=api_key,
            language_mode=settings.language_mode,
            model=settings.openai_model,
        )
    if settings.engine == "deepgram":
        api_key = ""
        if secret_store is not None:
            api_key = secret_store.get_api_key("deepgram") or ""
        return DeepgramTranscriber(
            api_key=api_key,
            language_mode=settings.language_mode,
        )

    # Unknown engine — fall back to local provider.
    return LocalFasterWhisperTranscriber(
        model_size=settings.model_size,
        language_mode=settings.language_mode,
        vad_filter=settings.vad_enabled,
        offline_mode=settings.offline_mode,
        model_dir=settings.model_dir,
    )
