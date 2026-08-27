"""Transcriber implementations.

Every name below is resolved lazily (PEP 562). Importing one submodule --
`stt_app.transcriber.local_faster_whisper`, say -- runs this package first, and
while these were plain imports that pulled in every remote provider SDK with
it. The two worker subprocesses (`local_model_download_worker`,
`local_model_scan_worker`) do exactly that and paid 0.23 s and ~150 modules per
launch for provider code they never call.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from .assemblyai_provider import AssemblyAITranscriber as AssemblyAITranscriber
    from .azure_provider import AzureLlmSpeechTranscriber as AzureLlmSpeechTranscriber
    from .base import ITranscriber as ITranscriber
    from .base import TranscriptionError as TranscriptionError
    from .deepgram_provider import DeepgramTranscriber as DeepgramTranscriber
    from .elevenlabs_provider import ElevenLabsTranscriber as ElevenLabsTranscriber
    from .factory import create_transcriber as create_transcriber
    from .funasr_provider import FunAsrTranscriber as FunAsrTranscriber
    from .groq_provider import GroqTranscriber as GroqTranscriber
    from .local_faster_whisper import (
        LocalFasterWhisperTranscriber as LocalFasterWhisperTranscriber,
    )
    from .local_faster_whisper import find_cached_models as find_cached_models
    from .openai_provider import OpenAITranscriber as OpenAITranscriber

_LAZY_ATTRIBUTES = {
    "AssemblyAITranscriber": ".assemblyai_provider",
    "AzureLlmSpeechTranscriber": ".azure_provider",
    "DeepgramTranscriber": ".deepgram_provider",
    "ElevenLabsTranscriber": ".elevenlabs_provider",
    "FunAsrTranscriber": ".funasr_provider",
    "GroqTranscriber": ".groq_provider",
    "ITranscriber": ".base",
    "LocalFasterWhisperTranscriber": ".local_faster_whisper",
    "OpenAITranscriber": ".openai_provider",
    "TranscriptionError": ".base",
    "create_transcriber": ".factory",
    "find_cached_models": ".local_faster_whisper",
}

__all__ = sorted(_LAZY_ATTRIBUTES)


def __getattr__(name: str) -> object:
    module_name = _LAZY_ATTRIBUTES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    # Cache it so the next lookup skips this function entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_ATTRIBUTES})
