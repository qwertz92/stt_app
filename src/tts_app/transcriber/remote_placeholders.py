from __future__ import annotations

from .base import AudioInput, ITranscriber


class _Phase2Transcriber(ITranscriber):
    provider_name = "remote"

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        raise NotImplementedError(
            f"{self.provider_name} provider is planned for Phase 2 and not implemented."
        )


class OpenAITranscriber(_Phase2Transcriber):
    provider_name = "OpenAI"


class AzureTranscriber(_Phase2Transcriber):
    provider_name = "Azure"


class DeepgramTranscriber(_Phase2Transcriber):
    provider_name = "Deepgram"
