from __future__ import annotations

import contextlib
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from ..config import DEFAULT_LANGUAGE_MODE, VALID_LANGUAGE_MODES

_base_logger = logging.getLogger(__name__)

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


@contextlib.contextmanager
def canceled_download_is_a_cancel():
    """Report a canceled model download as a canceled transcription.

    A transcriber that finds its model missing downloads it from its own load
    path, and that download waits for the single machine-wide slot. Pressing
    Cancel there raises ``ModelDownloadCanceled``, which every local engine
    then presented as "Failed to download ...", i.e. an error dialog for
    something the user had just asked for. It is also what a shutdown raises,
    which is not a failure either.
    """
    from ..model_download_coordinator import ModelDownloadCanceled

    try:
        yield
    except ModelDownloadCanceled as exc:
        raise TranscriptionCanceled(str(exc)) from exc


# Locale markers some models emit inline in their output. Nemotron does this
# in automatic-language mode: the transcript comes back with "<de-DE>" or
# "<|en|>" spliced into the words, and those tokens are then pasted straight
# into the user's document. They are model metadata that leaked through the
# decoder, never something that was said.
#
# The language subtag is matched against the codes this app actually
# supports, not against "any two or three letters". Shape alone is not
# enough: "<to-DO>", "<err-404>", "<job-ID>", "<btn-OK>" and "<|pad|>" all
# have a perfectly valid locale shape and are ordinary dictation, and they
# were being deleted without trace. A model can only announce a language the
# app knows about, so the real list is both the tightest and the correct
# filter -- of every false positive found, only "tr" (Turkish) is a real
# code, and a bare "<tr>" is excluded anyway.
_KNOWN_LANGUAGE_CODES = sorted(
    (code for code in VALID_LANGUAGE_MODES if code and code != "auto"),
    key=len,
    reverse=True,
)
# `(?i:...)` keeps the case-insensitivity on the language subtag only, so
# "<DE-DE>" is caught while "<as-is>" is not.
_LANGUAGE_ALTERNATION = "(?i:{})".format(
    "|".join(re.escape(code) for code in _KNOWN_LANGUAGE_CODES)
)
# Optional BCP-47 script ("Hans"), then a MANDATORY region: two UPPERCASE
# letters or three digits. The region case is load-bearing, and matching it
# case-insensitively was a real regression -- 23 of the supported language
# codes are ordinary English or German words ("as", "is", "it", "no", "so",
# "or", "be", "my", "am", "he", "hi", "id", "la", "da", "ne", "ka", "ha",
# "si", "ta", "te", "pa", "ba", "sa"), so "<as-is>", "<no-go>", "<is-ok>",
# "<my-id>" and "<so-so>" were being deleted out of real dictation. A model
# announcing a locale writes the region in upper case; a person dictating a
# hyphenated word does not.
#
# The region is mandatory because "<de>" on its own is indistinguishable
# from markup. A script-only tag ("<zh-Hans>") is therefore not matched --
# accepted, since no model in use emits that form.
_SUBTAGS = r"(?:-[A-Za-z]{4})?(?:-(?:[A-Z]{2}|[0-9]{3}))"
_LANGUAGE_TAG_PATTERN = re.compile(
    "|".join(
        (
            # <|en|>, <|de-DE|> -- the pipe wrapper is unambiguous on its own
            rf"<\|(?:{_LANGUAGE_ALTERNATION})(?:-[A-Za-z0-9]{{2,8}})*\|>",
            # <de-DE>, <zh-Hans-CN>, <es-419>
            rf"<(?:{_LANGUAGE_ALTERNATION}){_SUBTAGS}>",
        )
    ),
)


def strip_language_tags(text: str) -> str:
    """Remove inline locale markers a model leaked into its transcript.

    Whitespace-preserving on purpose. This runs per decoded chunk in the
    streaming path and the caller concatenates the results, so trimming the
    ends would weld the last word of one chunk onto the first word of the
    next ("Guten Tag" + " heute" becoming "Guten Tagheute").
    """
    if not text or "<" not in text:
        return text
    cleaned = _LANGUAGE_TAG_PATTERN.sub("", text)
    if cleaned == text:
        return text
    return re.sub(r"[ 	]{2,}", " ", cleaned)

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

    #: Latched once a cancel check has raised, so the traceback is logged
    #: exactly once per installed check instead of on every poll.
    _cancel_check_failed = False

    def set_cancel_check(self, cancel_check: Callable[[], bool] | None) -> None:
        """Install the callable polled during cancellable waits.

        Subclasses that can additionally stop *mid-decode* (faster-whisper,
        between segments) override this only to document that; the stored
        attribute is the same one.
        """
        self._cancel_check = cancel_check
        self._cancel_check_failed = False

    def _is_cancel_requested(self) -> bool:
        """Whether the installed check asks to stop, never raising.

        A check that raises must not fail the transcription: it is a
        controller-side callable, and losing a finished dictation because a
        cancel poll misbehaved is strictly worse than not being cancellable.
        The traceback is logged once per installed check -- the ONNX/WebGPU
        reader polls this every 0.25 s, so a broken check otherwise wrote the
        same traceback to the log several times a second for the whole run.
        """
        check = self._cancel_check
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:
            if not self._cancel_check_failed:
                self._cancel_check_failed = True
                _base_logger.exception(
                    "%s cancel check raised; ignoring it for this run.",
                    type(self).__name__,
                )
            return False

    def _raise_if_canceled(self) -> None:
        """Stop the current transcription if a cancel was requested."""
        if self._is_cancel_requested():
            raise TranscriptionCanceled()

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
