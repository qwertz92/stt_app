from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

AudioInput = bytes | str | Path
StreamingCallback = Callable[[str], None]


class TranscriptionError(RuntimeError):
    pass


class ITranscriber(ABC):
    @abstractmethod
    def transcribe_batch(self, audio_source: AudioInput) -> str:
        raise NotImplementedError

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError("Phase 2: streaming is not implemented.")

    def push_audio_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("Phase 2: streaming is not implemented.")

    def stop_stream(self) -> str:
        raise NotImplementedError("Phase 2: streaming is not implemented.")


Transcriber = ITranscriber
