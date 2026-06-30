from __future__ import annotations

from ..config import (
    DEFAULT_AZURE_ENDPOINT,
    DEFAULT_AZURE_SPEECH_MODEL,
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_ENGINE,
    DEFAULT_FUNASR_MODEL,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_WEBGPU_MODEL_SIZES,
)
from ..settings_store import AppSettings
from .assemblyai_provider import AssemblyAITranscriber
from .azure_provider import AzureLlmSpeechTranscriber
from .base import ITranscriber
from .deepgram_provider import DeepgramTranscriber
from .elevenlabs_provider import ElevenLabsTranscriber
from .funasr_provider import FunAsrTranscriber
from .groq_provider import GroqTranscriber
from .local_faster_whisper import LocalFasterWhisperTranscriber
from .local_nemotron import LocalNemotronTranscriber
from .local_webgpu_asr import LocalOnnxWebGpuTranscriber
from .openai_provider import OpenAITranscriber


def _create_local_transcriber(settings: AppSettings) -> ITranscriber:
    """Select the local transcriber for the configured ``model_size``.

    Single source of truth for the Nemotron → WebGpu → faster-whisper
    selection, shared by the explicit ``local`` engine path and the
    unknown-engine fallback so the two cannot drift.
    """
    if settings.model_size in LOCAL_NEMOTRON_MODEL_SIZES:
        return LocalNemotronTranscriber(
            model_size=settings.model_size,
            language_mode=settings.language_mode,
            offline_mode=settings.offline_mode,
            model_dir=settings.model_dir,
            use_runtime_vad=settings.vad_enabled,
        )
    if settings.model_size in LOCAL_WEBGPU_MODEL_SIZES:
        return LocalOnnxWebGpuTranscriber(
            model_size=settings.model_size,
            language_mode=settings.language_mode,
            offline_mode=settings.offline_mode,
            model_dir=settings.model_dir,
        )
    return LocalFasterWhisperTranscriber(
        model_size=settings.model_size,
        language_mode=settings.language_mode,
        vad_filter=settings.vad_enabled,
        stream_final_full_pass=settings.streaming_full_final_transcript,
        offline_mode=settings.offline_mode,
        model_dir=settings.model_dir,
    )


def _api_key(secret_store, provider: str) -> str:
    if secret_store is None:
        return ""
    return secret_store.get_api_key(provider) or ""


def create_transcriber(
    settings: AppSettings,
    secret_store=None,
) -> ITranscriber:
    if settings.engine == DEFAULT_ENGINE:
        return _create_local_transcriber(settings)
    if settings.engine == "assemblyai":
        return AssemblyAITranscriber(
            api_key=_api_key(secret_store, "assemblyai"),
            language_mode=settings.language_mode,
            model=settings.assemblyai_model,
        )
    if settings.engine == "groq":
        return GroqTranscriber(
            api_key=_api_key(secret_store, "groq"),
            language_mode=settings.language_mode,
            model=settings.groq_model,
        )
    if settings.engine == "openai":
        return OpenAITranscriber(
            api_key=_api_key(secret_store, "openai"),
            language_mode=settings.language_mode,
            model=settings.openai_model,
        )
    if settings.engine == "deepgram":
        return DeepgramTranscriber(
            api_key=_api_key(secret_store, "deepgram"),
            language_mode=settings.language_mode,
            model=settings.deepgram_model,
        )
    if settings.engine == "elevenlabs":
        return ElevenLabsTranscriber(
            api_key=_api_key(secret_store, "elevenlabs"),
            language_mode=settings.language_mode,
            model=getattr(settings, "elevenlabs_model", DEFAULT_ELEVENLABS_MODEL),
        )
    if settings.engine == "azure":
        return AzureLlmSpeechTranscriber(
            api_key=_api_key(secret_store, "azure"),
            endpoint=getattr(settings, "azure_endpoint", DEFAULT_AZURE_ENDPOINT),
            language_mode=settings.language_mode,
            model=getattr(settings, "azure_speech_model", DEFAULT_AZURE_SPEECH_MODEL),
        )
    if settings.engine == "funasr":
        return FunAsrTranscriber(
            api_key=_api_key(secret_store, "funasr"),
            language_mode=settings.language_mode,
            model=getattr(settings, "funasr_model", DEFAULT_FUNASR_MODEL),
        )

    # Unknown engine — fall back to local provider.
    return _create_local_transcriber(settings)
