from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from ..config import DEFAULT_LANGUAGE_MODE

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


# Locale markers some models emit inline in their output. Nemotron does this
# in automatic-language mode: the transcript comes back with "<de-DE>" or
# "<|en|>" spliced into the words, and those tokens are then pasted straight
# into the user's document. They are model metadata that leaked through the
# decoder, never something that was said.
#
# Only the two forms actually observed are matched, and deliberately not a
# bare "<xx>": that shape is indistinguishable from ordinary markup, and a
# dictation about HTML would lose "<div>", "<br>" or "<tr>" -- "tr" is even a
# real language code. Requiring either the pipe wrapper or a region subtag
# separates the two cases without guessing.
_LANGUAGE_TAG_PATTERN = re.compile(
    "|".join(
        (
            # <|en|>, <|de|>
            r"<\|[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\|>",
            # <de-DE>, <zh-Hans-CN>
            r"<[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})+>",
        )
    )
)


def strip_language_tags(text: str) -> str:
    """Remove inline locale markers a model leaked into its transcript."""
    if not text or "<" not in text:
        return text
    cleaned = _LANGUAGE_TAG_PATTERN.sub(" ", text)
    if cleaned == text:
        return text
    # Collapse only the whitespace the removal introduced, and keep the
    # original leading/trailing shape so callers that join segments are
    # unaffected.
    return re.sub(r"[ 	]{2,}", " ", cleaned).strip()

class ITranscriber(ABC):
    #: Polled while the transcriber waits for something interruptible. Every
    #: engine now needs one, not just the ones that can stop mid-decode: since
    #: the download slot became machine-wide, a model fetch can wait on another
    #: *process* -- a benchmark worker pulling several GB, say -- and without a
    #: cancel check that wait is unbreakable. It runs on the single
    #: transcription worker, so it would block every later dictation too, with
    #: the overlay stuck in Processing and Cancel unable to do anything.
    _cancel_check: Callable[[], bool] | None = None

    @abstractmethod
    def transcribe_batch(self, audio_source: AudioInput) -> str:
        raise NotImplementedError

    def set_cancel_check(self, cancel_check: Callable[[], bool] | None) -> None:
        """Install the callable polled during cancellable waits.

        Subclasses that can additionally stop *mid-decode* (faster-whisper,
        between segments) override this only to document that; the stored
        attribute is the same one.
        """
        self._cancel_check = cancel_check

    def set_language_mode(self, mode: str) -> None:
        """Apply a language selection to an already-created transcriber.

        Every provider reads the language when a request or stream starts, so
        a language change must never force a new runtime object (for local
        models that would mean reloading the whole model). Subclasses that
        restrict the accepted values override ``_normalize_language_mode``.
        """
        self._language_mode = self._normalize_language_mode(mode)

    def _normalize_language_mode(self, mode: str) -> str:
        return str(mode or DEFAULT_LANGUAGE_MODE).strip().lower()

    def start_stream(
        self,
        on_partial: StreamingCallback | None = None,
        on_error: StreamingErrorCallback | None = None,
    ) -> None:
        raise NotImplementedError("Streaming is not supported by this engine.")

    def push_audio_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("Streaming is not supported by this engine.")

    def stop_stream(self) -> str:
        raise NotImplementedError("Streaming is not supported by this engine.")

    def abort_stream(self) -> None:
        raise NotImplementedError("Streaming is not supported by this engine.")


Transcriber = ITranscriber


class ProgressReporter:
    _logger = logging.getLogger(__name__)

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
            self._logger.debug("Progress callback raised", exc_info=True)
