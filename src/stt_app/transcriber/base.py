from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

AudioInput = bytes | str | Path
StreamingCallback = Callable[[str], None]
StreamingErrorCallback = Callable[[str], None]
ProgressCallback = Callable[[str], None]


class TranscriptionError(RuntimeError):
    pass


class TranscriptionCanceled(Exception):
    """Raised inside a transcriber when a cooperative cancel was requested.

    Transcribers that support stopping mid-run (e.g. faster-whisper between
    segments) accept a cancel-check callable via ``set_cancel_check`` and raise
    this when it returns True. It is intentionally not a ``TranscriptionError``
    so callers can distinguish a user cancel from a real failure.
    """


class ITranscriber(ABC):
    @abstractmethod
    def transcribe_batch(self, audio_source: AudioInput) -> str:
        raise NotImplementedError

    def start_stream(
        self,
        on_partial: StreamingCallback | None = None,
        on_error: StreamingErrorCallback | None = None,
    ) -> None:
        raise NotImplementedError("Phase 2: streaming is not implemented.")

    def push_audio_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("Phase 2: streaming is not implemented.")

    def stop_stream(self) -> str:
        raise NotImplementedError("Phase 2: streaming is not implemented.")

    def abort_stream(self) -> None:
        raise NotImplementedError("Phase 2: streaming is not implemented.")


Transcriber = ITranscriber


class ProgressReporter:
    def __init__(self) -> None:
        self._progress_callback: ProgressCallback | None = None

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        self._progress_callback = callback

    def _emit_progress(self, text: str) -> None:
        if self._progress_callback is None:
            return
        try:
            self._progress_callback(text)
        except Exception:
            pass
