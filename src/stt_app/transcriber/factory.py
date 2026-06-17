from __future__ import annotations

from ..config import (
    DEFAULT_AZURE_ENDPOINT,
    DEFAULT_AZURE_SPEECH_MODEL,
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_ENGINE,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_WEBGPU_MODEL_SIZES,
)
from ..settings_store import AppSettings
from .assemblyai_provider import AssemblyAITranscriber
from .azure_provider import AzureLlmSpeechTranscriber
from .deepgram_provider import DeepgramTranscriber
from .base import ITranscriber
from .elevenlabs_provider import ElevenLabsTranscriber
from .groq_provider import GroqTranscriber
from .local_faster_whisper import LocalFasterWhisperTranscriber
from .local_nemotron import LocalNemotronTranscriber
from .local_webgpu_asr import LocalOnnxWebGpuTranscriber
from .openai_provider import OpenAITranscriber


def create_transcriber(
    settings: AppSettings,
    secret_store=None,
) -> ITranscriber:
    if settings.engine == DEFAULT_ENGINE:
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
    if settings.engine == "assemblyai":
        api_key = ""
        if secret_store is not None:
            api_key = secret_store.get_api_key("assemblyai") or ""
        return AssemblyAITranscriber(
            api_key=api_key,
            language_mode=settings.language_mode,
            model=settings.assemblyai_model,
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
            model=settings.deepgram_model,
        )
    if settings.engine == "elevenlabs":
        api_key = ""
        if secret_store is not None:
            api_key = secret_store.get_api_key("elevenlabs") or ""
        return ElevenLabsTranscriber(
            api_key=api_key,
            language_mode=settings.language_mode,
            model=getattr(settings, "elevenlabs_model", DEFAULT_ELEVENLABS_MODEL),
        )
    if settings.engine == "azure":
        api_key = ""
        if secret_store is not None:
            api_key = secret_store.get_api_key("azure") or ""
        return AzureLlmSpeechTranscriber(
            api_key=api_key,
            endpoint=getattr(settings, "azure_endpoint", DEFAULT_AZURE_ENDPOINT),
            language_mode=settings.language_mode,
            model=getattr(settings, "azure_speech_model", DEFAULT_AZURE_SPEECH_MODEL),
        )

    # Unknown engine — fall back to local provider.
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
