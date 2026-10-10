from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from PySide6 import QtCore, QtGui

from . import audio_devices, local_runtime_support, silero_vad
from .app_paths import resolve_recordings_dir
from .audio_capture import AudioCapture, AudioCaptureError, WarmMicrophoneStream
from .audio_device_listener import AudioDeviceChangeListener
from .config import (
    AUDIO_CAPTURE_FIRST_CALLBACK_HARD_TIMEOUT_MS,
    AUDIO_CAPTURE_FIRST_CALLBACK_TIMEOUT_MS,
    AUDIO_CHANNELS,
    AUDIO_DEVICE_CHANGE_SETTLE_MS,
    AUDIO_SAMPLE_RATE,
    CONCURRENT_TRANSCRIPTION_MODE_CANCEL,
    CONCURRENT_TRANSCRIPTION_MODE_HISTORY,
    CONCURRENT_TRANSCRIPTION_MODE_INSERT,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_COMPLETION_BEEP_TONE,
    DEFAULT_CONCURRENT_TRANSCRIPTION_MODE,
    DEFAULT_ENGINE,
    DEFAULT_INSERT_TARGET,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    DEFAULT_START_BEEP_TONE,
    DOC_MODELS_PATH,
    FALLBACK_HOTKEYS,
    HOTKEY_RECLAIM_INTERVAL_MS,
    INSERT_TARGET_CURRENT_WINDOW,
    LOCAL_GRANITE_CTC_MODEL_SIZES,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_ONNX_ASR_MODEL_SIZES,
    LOCAL_WEBGPU_MODEL_SIZES,
    OVERLAY_ERROR_ACTION_INSERT,
    OVERLAY_ERROR_ACTION_NONE,
    OVERLAY_ERROR_REVEAL_MS,
    OVERLAY_NOTICE_MS,
    OVERLAY_OPACITY_MAX_PERCENT,
    OVERLAY_OPACITY_MIN_PERCENT,
    OVERLAY_RESULT_REVEAL_MS,
    PASTE_TARGET_CHECK_TIMEOUT_MS,
    QUEUE_ROW_KIND_TRANSCRIPTION,
    QUEUE_ROW_KIND_UNDELIVERED,
    RECORDINGS_MAX_COUNT_UNLIMITED,
    REMOTE_BATCH_MAX_PART_SECONDS,
    RETRY_OLDER_FAILURES_MAX,
    SILERO_BATCH_MAX_SCAN_S,
    SILERO_BATCH_MIN_PROBABILITY,
    SILERO_BATCH_QUIET_SPEECH_GAIN,
    SILERO_BATCH_STOP_AFTER_SPEECH_S,
    STREAMING_ABORT_BEEP_DURATION_MS,
    STREAMING_ABORT_BEEP_HZ,
    STREAMING_ABORT_ON_FOCUS_CHANGE,
    STREAMING_BEEP_ON_ABORT,
    STREAMING_CONNECT_JOIN_TIMEOUT_S,
    STREAMING_FOCUS_POLL_MS,
    STREAMING_LIVE_INSERT_ENABLED,
    STREAMING_LIVE_INSERT_RETRY_LIMIT,
    STREAMING_OVERLAY_MAX_CHARS,
    STREAMING_PRECONNECT_BUFFER_MAX_BYTES,
    STREAMING_PRECONNECT_FLUSH_PUT_TIMEOUT_S,
    STREAMING_REVISION_WORD_WINDOW,
    STREAMING_STABLE_WORD_GUARD,
    TRAY_CANCEL_ACTION_LABEL,
    TRAY_REPASTE_ACTION_LABEL,
    VAD_ENERGY_THRESHOLD_MIN,
    VAD_MAX_SILENCE_MS,
    VAD_MIN_SPEECH_MS,
    VALID_START_BEEP_TONES,
    language_modes_for_selection,
    nemotron_provider_order,
    supports_custom_vocabulary,
    supports_streaming,
)
from .hotkey import (
    HotkeyManager,
    HotkeyRegistrationError,
    message_clock_ms,
    message_time_not_after,
    parse_hotkey,
)
from .last_recording_store import LastRecordingStore
from .local_model_download import (
    DownloadBytesSample,
    model_download_process_error,
    model_download_process_progress,
    release_model_download_process,
    start_model_download_process,
    terminate_model_download_process,
)
from .model_download_coordinator import (
    ACQUIRE_JOINED,
    ModelDownloadCanceled,
    model_download_coordinator,
)
from .model_download_progress import (
    ModelDownloadSpeedTracker,
    format_model_download_progress,
)
from .overlay_ui import OverlayUI
from .paste_target_check import (
    VERDICT_NOT_TEXT_FIELD,
    VERDICT_UNKNOWN,
    PasteTargetCheck,
)
from .settings_store import _REMOTE_MODEL_FIELDS as _ENGINE_MODEL_FIELDS
from .settings_store import _REMOTE_REGION_FIELDS as _ENGINE_REGION_FIELDS
from .settings_store import AppSettings, SettingsStore, preferred_onnx_device
from .streaming_text import (
    StreamingTextState,
    normalize_stream_text,
    retarget_tail,
    tail_prefix,
)
from .text_inserter import (
    TextInserter,
    TextInsertionError,
    TextMayHaveBeenPastedError,
)
from .transcriber import create_transcriber
from .transcriber.base import (
    TranscriptionCanceled,
    TranscriptionError,
    request_transcription_shutdown,
    transcript_has_gap,
)
from .transcript_history import (
    TranscriptHistoryEntry,
    TranscriptHistoryStore,
    edited_entry,
)
from .unfinished_recordings import UnfinishedRecording, UnfinishedRecordingStore
from .vad import EnergyVad, measure_peak_windowed_rms
from .window_focus import FocusSignature, Win32WindowFocusHelper, WindowFocusHelper

_ARCHIVED_RECORDING_NAME_RE = re.compile(
    r"^recording_[0-9]{8}_[0-9]{6}_[0-9]{6}\.wav$",
    re.IGNORECASE,
)

# A finished batch/import run that produced no text is a model miss, not
# silence. The silence gate already skipped true quiet recordings. Treat the
# empty result as a failure so Retry keeps the audio and the overlay does not
# look like the recording never happened.
_EMPTY_MODEL_TRANSCRIPT_MESSAGE = "The model returned no text for this recording."

# The last-recording error for a transcript with a gap marker. The recording
# is kept rather than completed, so the stretch the marker names can still be
# listened to or transcribed again.
_GAP_KEPT_RECORDING_MESSAGE = (
    "Part of the recording returned no text; the recording was kept."
)

# The stages of a local model preload. They fail, progress and finish for
# entirely different reasons, and only the download has measurable progress --
# a queued preload and a running load both have none, for opposite reasons.
_PRELOAD_PHASE_QUEUED = "queued"
_PRELOAD_PHASE_DOWNLOAD = "download"
_PRELOAD_PHASE_LOAD = "load"


def _local_model_display_name(model_name: str) -> str:
    """The model's name as every picker in the app spells it.

    The overlay used to name a download by its settings id ("Downloading
    'granite-speech-5.0-470m-turboctc'"), which matches nothing the user can
    see on screen. The label table lives with the settings dialog's helpers,
    where the pickers read it; the import is inside the function so that
    module stays off the controller's own import path until a progress line
    needs a name.
    """
    from .settings_dialog_helpers import local_model_short_label

    return local_model_short_label(model_name)


# How much of a transcript a "Not inserted" queue row quotes, enough to tell
# the rows apart; the whole text is in history and is what the re-paste pastes.
_UNDELIVERED_PREVIEW_CHARS = 48


def _pace_ms(wait_s: float) -> int:
    """A paste-pace wait as whole milliseconds, rounded up, at least 1.

    Rounded to the microsecond first: the wait is a difference of two
    monotonic readings, and 1.1 s arrives as 1.1000000000000227, which a
    bare ceiling turns into 1101 ms.
    """
    return max(1, math.ceil(round(wait_s * 1000.0, 3)))


def _join_transcripts(texts: list[str]) -> str:
    """Join transcripts for one paste, separating them by a single space
    unless a boundary already carries whitespace."""
    joined = ""
    for text in texts:
        if not text:
            continue
        if joined and not joined[-1].isspace() and not text[0].isspace():
            joined += " "
        joined += text
    return joined


@dataclass(slots=True)
class _TranscriptionJob:
    """A submitted transcription tracked for the queue and per-job insertion.

    Each recording captures its own target window so a queued transcription
    can be inserted into the window that was focused when it was recorded,
    even after the user has moved on to another recording.
    """

    token: int
    engine: str
    model: str
    mode: str
    settings: AppSettings
    target_handle: int | None
    target_signature: FocusSignature | None
    created_at: datetime = field(default_factory=datetime.now)
    source_recording_id: str = ""
    # False for a job with no recording id -- bytes the managed store never
    # received (a retry whose retained identity is unknown, a road whose
    # persist was refused) or a slot it could not name: its terminal marks
    # leave the store alone, because the slot may hold someone else's
    # recording and the unconditional write an empty id otherwise means
    # would relabel or delete it.
    marks_last_recording: bool = True
    source_audio_path: str = ""
    future: object | None = None
    # The provider handshake thread, when this job finalizes a stream that may
    # still be connecting. The worker joins it before calling `stop_stream()`.
    connect_thread: object | None = None
    # The generation of that handshake, under which its failure is recorded
    # for this worker to consume.
    connect_generation: int = 0
    # How a non-foreground (queued/background) result is delivered:
    # "insert" -> save to history and insert into target_handle;
    # "history" -> save to history only.
    background_delivery: str = "insert"
    # When True, the worker should stop this transcription's compute as soon as
    # possible (checked cooperatively by transcribers that support it) and never
    # start it if it has not begun.
    aborting: bool = False
    insertion_deferred: bool = False
    runtime_transcriber: object | None = None
    runtime_lease: object | None = None
    # The live streaming transcript as it stood when this finalize was
    # submitted. It is the only in-app copy while the finalize is in
    # flight, and the finalize can end without producing text at all --
    # a cancel aborts the stream instead of stopping it, and both
    # AssemblyAI and Deepgram can return an empty string after a socket
    # problem. Cleared by whoever delivers real text; written to history
    # by `_finish_transcription_job` if nobody did.
    stashed_partial: str = ""
    # The history entry a background delivery appended for this job, so a
    # later takeover of the overlay (a failed queued paste) can move the
    # Edit target together with the text it shows.
    history_entry: TranscriptHistoryEntry | None = None
    # True once this finished result has been held back by the paste pace
    # (`_flush_deferred_background_results`): it waits only for the end of
    # the previous paste's restore window, never again for the transcription
    # that may be running by then -- it may be the foreground result itself.
    paste_paced: bool = False
    # True while the pace is the only thing holding this result back: it is
    # pasted within `CLIPBOARD_RESTORE_DELAY_S`, so the queue panel does not
    # list it and Clear queue or its Cancel cannot drop it. Cleared when a
    # recording or a window defers it again, which lists it as before.
    pace_held: bool = False


@dataclass(frozen=True, slots=True)
class PendingQuitWork:
    """What a quit right now would cut short (`quit_pending_work`)."""

    # A microphone capture is open, or a start or stop is in progress.
    recording: bool = False
    # Jobs still producing a transcript: running, queued, or finalizing.
    transcribing: int = 0
    # Finished transcripts held for their paste (a window, a recording, the
    # paste pace), plus a re-paste the pace holds. They are in history.
    waiting_to_insert: int = 0
    # Listed rows whose paste failed or is doubtful. They are in history.
    not_inserted: int = 0
    # Failed recordings held for Retry. Quitting keeps their audio.
    failed: int = 0
    # The last paste's clipboard-restore window is still open, or its target
    # check still runs. Quitting now flushes the restore at once, and a
    # target that reads the clipboard late pastes the old clipboard instead.
    paste_settling: bool = False

    @property
    def can_wait(self) -> bool:
        """Whether a wait is not over yet."""
        return self._delivers_something or self.paste_settling

    @property
    def asks_before_quit(self) -> bool:
        # Not for a settling paste alone: that ends within
        # `CLIPBOARD_RESTORE_DELAY_S`, and quitting right after a dictation
        # is the ordinary way to end one.
        return self._delivers_something or self.not_inserted > 0

    @property
    def _delivers_something(self) -> bool:
        return self.recording or self.transcribing > 0 or self.waiting_to_insert > 0


@dataclass(slots=True)
class _UndeliveredInsert:
    """A finished transcript whose paste failed or could not be confirmed.

    Kept until it is inserted or dismissed, one per failed paste, and shown
    as a row of the overlay's queue panel. The single Insert offer
    (`_insert_action_text`) held one text: a later failure replaced it, the
    next recording start cleared it, and while a transcription ran it was
    never painted at all -- with several queued results on a slow machine
    the owner noticed only later which transcripts were missing (field
    report, 2026-10-01). The text is in history either way.
    """

    # Negative, so a row's id can never be a live job's token.
    row_id: int
    text: str
    # The paste keystroke went out and only the cleanup failed: listed, but
    # never pasted again by the re-paste (no double paste).
    may_have_pasted: bool
    created_at: datetime
    # The paste reported success, but the focused element showed no caret
    # (`paste_target_check`): most likely nothing was inserted. Insertable
    # like a failed paste -- the user decides after looking at the window --
    # but never joined to failed rows by the re-paste, and dropped by the
    # next paste that goes out (`_drop_superseded_doubtful_rows`).
    outside_text_field: bool = False
    # The transcripts `text` joins, each with its history entry (or None),
    # oldest first: one for a single result, several for a coalesced paste.
    # An edit of one of those entries rewrites its part and `text`
    # (`_follow_transcript_edit`), so the re-paste pastes the edit.
    parts: tuple[tuple[TranscriptHistoryEntry | None, str], ...] = ()

    @property
    def history_entry(self) -> TranscriptHistoryEntry | None:
        """The row's own entry; None for a coalesced paste of several."""
        return self.parts[0][0] if len(self.parts) == 1 else None


@dataclass(slots=True, frozen=True)
class _PasteCheck:
    """A paste that reported success, waiting for its target check.

    `paste_target_check` answers on a worker thread; until then the
    completion tone waits, and a "not a text field" verdict reports the paste
    as doubtful instead (`_report_paste_outside_text_field`).
    """

    text: str
    history_entry: TranscriptHistoryEntry | None
    created_at: datetime
    # "The transcript was" / "Recording 12:00:01 (local · base) was" /
    # "3 queued transcriptions were": the subject of the report.
    identity: str
    # A queued paste, or one made while a session owned the overlay: the
    # tray always carries its report.
    background: bool
    # The overlay's `(state, detail)` right after the paste, when that is
    # the paste's own "Done" or Idle; None when it is anything else -- a
    # queued paste paints nothing, so the overlay may hold another job's
    # finished result. A doubtful report paints only over exactly this;
    # anything newer, or None, keeps the screen and the tray carries it.
    overlay_shown: tuple[str, str] | None
    # Whether a report painted on the overlay moves the shown transcript and
    # its Edit target to this paste, as a failed queued paste does. False
    # where the overlay already shows this text with the right pair.
    takes_shown_pair: bool
    # The window that had the foreground for the paste; a check that finds
    # another one in front answers "unknown".
    foreground: int | None
    # `_paste_serial` right after this paste. A verdict for an older paste
    # than the newest lists no row: the newer paste has superseded it.
    paste_serial: int
    # The capture open at the paste (an immediate-mode queued paste lands
    # mid-recording), so a recording started while the check ran can be
    # told apart from that one: the delayed tone would reach its microphone.
    capture: object | None
    # A coalesced paste's results with their entries, for the doubtful row
    # (`_UndeliveredInsert.parts`); empty for a single result.
    parts: tuple[tuple[TranscriptHistoryEntry | None, str], ...] = ()


@dataclass(slots=True, frozen=True)
class _PendingRepaste:
    """A re-paste the user asked for inside the previous paste's restore
    window, held for `_on_paste_pace_timeout` like every other paste."""

    text: str
    display_entry: object
    undelivered: tuple[_UndeliveredInsert, ...]
    offer_rows: tuple[_UndeliveredInsert, ...] = ()
    # Held until the streaming dictation into the paste's window has ended
    # (`_repaste`), not by the pace; `_reset_streaming_state` lets it go.
    after_stream: bool = False


class _TranscriberRuntimeLease:
    """Ownership of a shared or isolated transcriber runtime.

    A lease may be acquired on the Qt thread for a live stream and released by
    the finalize worker, so it deliberately uses an idempotent primitive-lock
    guard rather than thread-affine ownership.
    """

    def __init__(
        self,
        controller: DictationController,
        transcriber: object,
        *,
        owns_shared_lock: bool,
        close_on_release: bool,
    ) -> None:
        self.transcriber = transcriber
        self._controller = controller
        self._owns_shared_lock = owns_shared_lock
        self._close_on_release = close_on_release
        self._release_lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        # The hand-back is in a `finally`, and that is the whole point of this
        # method. `_close_cached_transcriber` swallows `Exception` but not
        # `BaseException`, and `_released` is already True above, so a close
        # that dies used to skip `_release_transcriber_runtime` permanently:
        # `_transcriber_runtime_lock` stranded for the process lifetime, every
        # later preload and audio import blocked forever, every dictation
        # silently building its own isolated runtime, and no deferred cache
        # reset ever running. Worse, every caller reaches this from a
        # `finally` that sits *outside* its own `except BaseException` arm --
        # `_transcribe_worker`, `_finalize_stream_worker` and
        # `_preload_model_worker` all emit their terminal signal after it --
        # so the escaping exception also swallowed the signal, leaving the
        # overlay in Processing with no error and no Retry, or
        # `_streaming_recording` stuck True so every later hotkey press was
        # refused. One `finally` here fixes all four call sites.
        try:
            if self._close_on_release:
                self._controller._close_cached_transcriber(self.transcriber)
        except BaseException:
            # Logged and dropped, not re-raised. Handing the runtime back is
            # this method's contract; closing is best-effort cleanup on top of
            # it, and every caller reaches `release()` from a `finally` that
            # sits *outside* its own `except BaseException` arm --
            # `_transcribe_worker`, `_finalize_stream_worker` and
            # `_preload_model_worker` all emit their terminal signal after it.
            # So a close that raised did not merely fail to close: it swallowed
            # the terminal signal, leaving the overlay in Processing with no
            # error and no Retry for the rest of the session, or
            # `_streaming_recording` stuck True so every later hotkey press was
            # refused with "Streaming transcript is still finalizing".
            # `_close_cached_transcriber` already swallows `Exception`, so this
            # only widens an existing decision to `BaseException`.
            self._controller._logger.exception(
                "Failed to close a released transcriber runtime"
            )
        finally:
            # Guarded for the same reason the close above is, and it is not
            # theoretical: `_release_transcriber_runtime` applies the deferred
            # cache reset, which closes the *cached* transcriber through the
            # same `_close_cached_transcriber` that swallows `Exception` but
            # not `BaseException`. So the exact failure this method was
            # rewritten to survive -- a `close()` raising a `BaseException` --
            # still reached the caller by the other door, and the caller is a
            # worker that emits its terminal signal after this call.
            #
            # Nothing is stranded by swallowing it. Both the admission lock
            # and the use count are handed back inside that method's own
            # nested `finally`s, so neither can be skipped by the other
            # failing; the one remaining way the lock is not handed back is
            # `Lock.release()` itself raising, which happens only for a lock
            # that was not held. A reset that failed leaves
            # `_pending_transcriber_cache_reset` set, so the next release
            # retries it.
            try:
                self._controller._release_transcriber_runtime(
                    owns_shared_lock=self._owns_shared_lock
                )
            except BaseException:
                self._controller._logger.exception(
                    "Failed to complete a transcriber runtime release"
                )


# Which provider's API key each engine reads, and therefore whose presence is
# part of that engine's runtime identity. The local engine reads none.
_ENGINE_KEY_FLAGS: dict[str, str] = {
    "assemblyai": "has_assemblyai_key",
    "openai": "has_openai_key",
    "groq": "has_groq_key",
    "deepgram": "has_deepgram_key",
    "elevenlabs": "has_elevenlabs_key",
    "azure": "has_azure_key",
    "funasr": "has_funasr_key",
    "custom": "has_custom_key",
    "speechmatics": "has_speechmatics_key",
    "mistral": "has_mistral_key",
}

# `_ENGINE_MODEL_FIELDS` and `_ENGINE_REGION_FIELDS` (imported from the
# store) say which field carries each remote engine's model and region.
# Only the selected engine's is read, so each collapses into one identity
# slot: editing the Groq model must not reload a loaded local model.


class _TranscriberIdentity(NamedTuple):
    """Named form of what ``create_transcriber`` bakes into a runtime.

    A plain tuple would do for the equality comparison this is used for, but
    the credential path also has to ask *which engine* is currently loaded, and
    reading that out of an anonymous slot is the kind of assumption that breaks
    silently when a field is inserted.

    Every field is optional and defaults to a neutral value, because the
    identity is built **per engine**: a field the selected engine never reads
    stays at its default. Without that, editing an Azure endpoint threw away a
    multi-gigabyte local model that had never heard of Azure.
    """

    engine: str
    model_size: str = ""
    vad_enabled: bool = False
    offline_mode: bool = False
    model_dir: str = ""
    keep_onnx_model_loaded: bool = False
    streaming_full_final_transcript: bool = False
    local_onnx_device: str = ""
    # The device a benchmark measured as fastest for the selected model, which
    # is the device `auto` then starts with. Its own slot rather than folded
    # into `local_onnx_device`, because for the Node runtime the two reach the
    # constructor as two separate arguments.
    onnx_preferred_device: str = ""
    custom_vocabulary: str = ""
    silence_gate_enabled: bool = False
    silence_gate_threshold: float = 0.0
    # Remote engines only. One slot, because exactly one provider's model is
    # read for a given engine.
    remote_model: str = ""
    azure_endpoint: str = ""
    remote_region: str = ""
    # The custom endpoint's constructor arguments besides its model and key.
    custom_endpoint: str = ""
    custom_api_mode: str = ""
    custom_key_command: str = ""
    # Not the key itself -- keys never enter ``AppSettings``. This is whether
    # the engine has one *at all*: losing or gaining a key changes what the
    # runtime can do, while replacing one with a different value is invisible
    # here and is handled by ``provider_keys_changed``. The storage flag is
    # included because switching it off can make a stored key unreadable
    # without any key operation happening.
    has_api_key: bool = False
    allow_insecure_key_storage: bool = False


class DictationController(QtCore.QObject):
    vad_auto_stop_requested = QtCore.Signal()
    transcription_ready = QtCore.Signal(int, str)
    transcription_failed = QtCore.Signal(int, str)
    transcription_canceled = QtCore.Signal(int)
    transcription_progress = QtCore.Signal(int, str)
    transcription_partial = QtCore.Signal(str)
    # connect token, error text -- the token identifies the streaming session
    # the failure belongs to, so a provider that calls back after its session
    # was cancelled cannot be mistaken for the live one.
    stream_runtime_failed = QtCore.Signal(object, str)
    # generation, ok, error text -- a remote handshake finished off-thread
    stream_connect_finished = QtCore.Signal(int, bool, str)
    stream_abort_requested = QtCore.Signal(str, bool)
    model_preload_done = QtCore.Signal(int, bool, str)  # generation, success, message
    # A queued transcription failed while a newer session owns the overlay.
    background_transcription_failed = QtCore.Signal(str)
    # A queued transcription succeeded but its text could not be pasted. Without
    # this the loss was visible only in the log file.
    background_insertion_failed = QtCore.Signal(str)
    # An error raised while a recording or a transcription owns the overlay:
    # shown as a tray notification, never painted over the live session.
    busy_overlay_error = QtCore.Signal(str)
    # The user's clipboard could not be put back after a paste, even after the
    # inserter's retries. Emitted from the restore's timer thread; the queued
    # connection carries it to the tray on the Qt thread.
    clipboard_restore_failed = QtCore.Signal(str)
    # Emitted from MMDevice API worker threads; the queued connection marshals
    # the reaction onto the Qt thread.
    audio_devices_changed = QtCore.Signal(str)
    # A paste target check's answer: (check id, verdict, detail). Emitted from
    # `PasteTargetCheck`'s worker thread; the queued connection brings it to
    # the Qt thread.
    paste_target_checked = QtCore.Signal(int, str, str)
    # Emitted from the device-refresh worker once PortAudio re-enumerated, so
    # the overlay's microphone caption names the current default device.
    audio_devices_refreshed = QtCore.Signal()
    # A recording started while the quit window waited ("Wait and insert"):
    # the quit is called off (owner decision 2026-10-09), and the quit window
    # (`quit_dialog.QuitCoordinator`) closes.
    quit_canceled_by_recording = QtCore.Signal()
    # A recording from the startup notice was transcribed into history:
    # (text, history entry, recorded at). Emitted from the notice's worker
    # thread; the queued connection lists it on the Qt thread.
    unfinished_transcript_saved = QtCore.Signal(str, object, object)

    def __init__(
        self,
        settings_store: SettingsStore,
        hotkey_manager: HotkeyManager,
        cancel_hotkey_manager: HotkeyManager | None,
        overlay: OverlayUI,
        text_inserter: TextInserter,
        logger: logging.Logger,
        window_focus_helper: WindowFocusHelper | None = None,
        secret_store=None,
        history_store: TranscriptHistoryStore | None = None,
        last_recording_store: LastRecordingStore | None = None,
        show_overlay_hotkey_manager: HotkeyManager | None = None,
        repaste_hotkey_manager: HotkeyManager | None = None,
        paste_target_check: PasteTargetCheck | None = None,
        unfinished_recording_store: UnfinishedRecordingStore | None = None,
    ) -> None:
        super().__init__()
        self._settings_store = settings_store
        self._hotkey_manager = hotkey_manager
        self._cancel_hotkey_manager = cancel_hotkey_manager
        self._show_overlay_hotkey_manager = show_overlay_hotkey_manager
        self._repaste_hotkey_manager = repaste_hotkey_manager
        self._overlay = overlay
        self._text_inserter = text_inserter
        register_restore_failure = getattr(
            text_inserter, "set_restore_failure_handler", None
        )
        if callable(register_restore_failure):
            register_restore_failure(self.clipboard_restore_failed.emit)
        self._logger = logger
        self._window_focus_helper = window_focus_helper or Win32WindowFocusHelper()
        self._secret_store = secret_store
        self._history_store = history_store or TranscriptHistoryStore()
        self._last_recording_store = last_recording_store or LastRecordingStore()
        self._unfinished_recording_store = (
            unfinished_recording_store or UnfinishedRecordingStore()
        )
        # True while a quit waits for the pending work (`hold_for_quit`): new
        # recordings are refused, or the wait could never end.
        self._quit_hold = False
        # When each recording a job was registered for ended, by recording id,
        # so a failure kept for Retry is kept at quit under its own time.
        self._recorded_at_by_recording_id: dict[str, datetime] = {}

        self._settings: AppSettings = self._settings_store.load()
        self._warm_up_speech_check()
        self._audio_capture: AudioCapture | None = None
        self._warm_mic_stream: WarmMicrophoneStream | None = None
        self._audio_device_listener: AudioDeviceChangeListener | None = None
        self._pending_audio_device_refresh = False
        self._audio_device_refresh_lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Remote streaming finalizes get their own worker. `_executor` is
        # deliberately single-threaded so two local models never load at once,
        # but a remote finalize runs no model at all -- `stop_stream()` drains a
        # socket. Sharing the queue meant that pressing stop on an AssemblyAI or
        # Deepgram dictation left it "Processing" until an unrelated local batch
        # transcription ahead of it in the queue had finished. Still one worker,
        # so remote finalizes stay serialized among themselves.
        self._stream_finalize_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1
        )
        self._preload_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._preload_future: concurrent.futures.Future | None = None
        self._preload_generation = 0
        self._preload_target_key: tuple[object, ...] | None = None
        self._preload_result_lock = threading.Lock()
        self._preload_canceled_generations: set[int] = set()
        # Generation -> the sentence about partials a canceled preload's
        # cleanup could not remove, read once by `_on_model_preload_done`.
        self._preload_cleanup_notes: dict[int, str] = {}
        self._preload_results: dict[tuple[object, ...], tuple[int, str | None]] = {}
        self._transcriber_cache_lock = threading.Lock()
        # Preload, batch inference, and a live stream may all request the cached
        # transcriber. One lease owns that shared instance; overlapping work gets
        # an isolated runtime instead of blocking the Qt thread or replacing an
        # in-use cache. A plain Lock is intentional: a streaming lease can be
        # acquired on the Qt thread and released by its finalize worker.
        self._transcriber_runtime_lock = threading.Lock()
        self._transcriber_runtime_state_lock = threading.Lock()
        self._transcriber_runtime_in_use = threading.Event()
        self._transcriber_runtime_active_count = 0
        self._transcriber_cache_key = None
        self._transcriber_cache = None
        # Set when a settings reload happens while a lease owns the cached
        # transcriber. The owner applies the reset on release; an isolated owner
        # leaves it for the next shared-cache acquisition. Either way the
        # in-flight runtime is never closed out from under active work.
        self._pending_transcriber_cache_reset = False
        self._shutdown_started = False
        self._hotkey_registration_ok = False
        self._hotkey_notice: str | None = None
        # Which hotkey is actually registered right now. May differ from
        # settings.hotkey while another program holds the preferred one.
        self._active_hotkey: str = ""
        self._hotkey_reclaim_timer = QtCore.QTimer(self)
        self._hotkey_reclaim_timer.setInterval(HOTKEY_RECLAIM_INTERVAL_MS)
        self._hotkey_reclaim_timer.timeout.connect(self._reclaim_preferred_hotkey)
        self._cancel_hotkey_registration_ok = False
        self._cancel_hotkey_notice: str | None = None
        self._show_overlay_hotkey_registration_ok = False
        self._show_overlay_hotkey_notice: str | None = None
        self._repaste_hotkey_registration_ok = False
        self._repaste_hotkey_notice: str | None = None
        self._target_window_handle: int | None = None
        self._target_focus_signature: FocusSignature | None = None
        # What the overlay shows and Copy/Edit act on; a property, because
        # every write of it also retires `_delivered_after_shown` below.
        self._last_transcript = ""
        # The text a background paste delivered after the shown transcript,
        # with its single history entry or None for a coalesced paste. The
        # re-paste pastes this one: it is the last text that reached a
        # window, while Copy and Edit keep the shown transcript.
        self._delivered_after_shown: (
            tuple[str, TranscriptHistoryEntry | None] | None
        ) = None
        # Finished transcripts whose paste failed or could not be confirmed,
        # oldest first; see `_UndeliveredInsert`.
        self._undelivered_inserts: list[_UndeliveredInsert] = []
        self._undelivered_row_counter = 0
        # The message the last failed `_insert_text_at_target` reported, for a
        # caller that has to carry it to the tray instead of the overlay.
        self._last_insert_error_text = ""
        # What the overlay's Insert action re-pastes: the text of the insert
        # that failed, which is not always the last transcript -- a streaming
        # finalize inserts only the tail past `committed_text`. Written by the
        # two paths that paint an Error carrying `OVERLAY_ERROR_ACTION_INSERT`,
        # cleared by a successful re-paste and by the next recording start.
        self._insert_action_text: str = ""
        # Whether that text's insert failed *after* the paste keystroke, so
        # the offer withholds Insert. Recorded beside the offer by the paths
        # that create it: `_last_insert_may_have_pasted` below is per insert
        # attempt, and one flush pastes several queued transcripts.
        self._insert_offer_may_have_pasted = False
        # The listed rows the offer's text was built from, so the overlay's
        # Insert retires those rows and no other: two dictations with the
        # same text are two rows. Several when a re-paste of several waiting
        # rows failed and painted the offer for their joined text.
        self._insert_action_rows: tuple[_UndeliveredInsert, ...] = ()
        # The history entry the offer's text belongs to when it has no rows:
        # the dictation whose streaming tail it is, or the dictation a failed
        # re-paste pasted whole; None when not known. Edit and an edit's
        # follow-up reach a row-less offer only through it -- matched by
        # text, a failed F10 of another dictation's "okay." was taken for the
        # tail of the shown "Alles okay." (`_edit_reaches`,
        # `_follow_transcript_edit`).
        self._insert_action_entry: TranscriptHistoryEntry | None = None
        # The job whose result is the shown transcript, so a re-paste of it
        # can tell that this very result is still held in the paste queue.
        self._shown_transcript_token: int | None = None
        # The listed row of the shown transcript's own failed paste, so the
        # re-paste fallback refuses exactly that dictation when its keystroke
        # may have gone out, and not another one with the same text.
        self._shown_transcript_row: _UndeliveredInsert | None = None
        # Whether the last `_insert_text_at_target` call painted the Insert
        # offer, so the caller can attach the rows that failure is about.
        self._last_insert_offered = False
        # The foreground window of the last paste that reported success, for
        # its target check (`_check_paste_target`).
        self._last_insert_foreground: int | None = None
        # Counts the pastes that reported success, streaming inserts included.
        self._paste_serial = 0
        # `(state, detail)` the offer painter wrote last, None once a plain
        # status replaced it. The preload progress poll compares it with
        # what the overlay shows, to tell the painter's own Error from a
        # result it must not repaint.
        self._offer_painted: tuple[str, str] | None = None
        self._last_history_entry: TranscriptHistoryEntry | None = None
        self._last_failed_wav_bytes: bytes = b""
        # The managed recording those bytes are, as the store knows it: the
        # failed job's own id, or the id `save_recording` handed back when
        # the abort roads persisted them; "" when that write failed and the
        # store's slot holds someone else's recording. A retry hands it to
        # its job, so its marks never touch the slot's current holder.
        self._last_failed_recording_id: str = ""
        # Failures the slot replaced, oldest first, as (bytes, recording id):
        # retryable once the slot's own failure is resolved
        # (`_hold_failed_audio_for_retry`, `_retire_retry_audio_delivered_by`).
        self._older_failed_audio: list[tuple[bytes, str]] = []
        # The id the store handed back on the last `_persist_last_recording_audio`
        # call, "" when nothing was written. Read by the two abort roads that
        # retain the bytes they just persisted for Retry.
        self._last_persisted_recording_id: str = ""
        self._last_transcribe_settings: AppSettings | None = None
        self._active_batch_settings: AppSettings | None = None
        self._streaming_recording = False
        # Audio captured while a remote provider is still connecting. The
        # microphone is opened first so no speech is lost; these bytes are
        # handed over in order the moment the stream is ready.
        # Set by `_insert_text_at_target` when a failure happened after the
        # paste keystroke, so the streaming retry cannot duplicate text.
        self._last_insert_may_have_pasted = False
        self._stream_preconnect_lock = threading.Lock()
        self._stream_preconnect_chunks: list[bytes] | None = None
        self._stream_preconnect_dropped = False
        self._stream_connect_generation = 0
        self._stream_connect_thread: threading.Thread | None = None
        self._stream_connect_token: object | None = None
        # The live streaming session's identity; see `_begin_stream_connect`
        # for why this is not the same field as the connect token.
        self._stream_session_token: object | None = None
        # True between `_submit_stream_finalize` and the reset that ends the
        # session. The stop no longer retires the handshake (the buffered
        # audio still has to reach the provider), so a handshake that lands
        # afterwards reaches `_on_stream_connect_finished` with a matching
        # generation while `_streaming_recording` is still True -- and its
        # success arm would paint "Streaming active. Speak now" over
        # "Finalizing streaming transcript...".
        self._stream_finalize_pending = False
        # Error text by handshake generation, written by the connect thread
        # when the handshake or its flush fails and consumed by the finalize
        # worker after it joined that thread. While a finalize is pending the
        # failure is that worker's to report -- once, with the handshake's
        # own cause -- and not the connect signal's; see
        # `_on_stream_connect_finished`. Keyed rather than one slot, and never
        # cleared with the session: a cancel resets the session while the
        # worker is still parked in its join, and the next dictation's own
        # handshake may fail before the worker has read its record.
        self._stream_connect_failures: dict[int, str] = {}
        self._active_stream_transcriber = None
        self._active_stream_runtime_lease: _TranscriberRuntimeLease | None = None
        self._active_stream_settings: AppSettings | None = None
        self._stream_chunk_error_reported = False
        self._stream_abort_requested = False
        # True while another window holds focus during a live stream: the
        # session keeps recording, but nothing is pasted until it stops.
        self._stream_insertion_suspended = False
        # True while live insertion waits for an earlier result for the same
        # window (`_stream_live_insert_held`); kept to log each change once.
        self._stream_insert_held = False
        # A re-paste went into another window during the stream: its next
        # live insert waits for that paste's restore window.
        self._stream_waits_for_paste_pace = False
        # The finalize a stream's runtime failure left in flight
        # (`_reset_streaming_state(keep_session_text=True)`): it still pastes
        # the tail, so a re-paste held for the stream waits for it too.
        self._stream_kept_finalize_token: int | None = None
        # Consecutive failed live inserts in the current streaming session.
        self._stream_insert_failures = 0
        self._stream_text_state = StreamingTextState(
            stable_word_guard=STREAMING_STABLE_WORD_GUARD,
            revision_word_window=STREAMING_REVISION_WORD_WINDOW,
        )
        self._recording_start_in_progress = False
        self._recording_stop_in_progress = False
        self._pending_toggle_after_start_count = 0
        self._pending_toggle_after_stop_count = 0
        # `message_clock_ms()` when a stop that waited for the microphone's
        # backlog returned: record-hotkey presses stamped no later were made
        # during that wait (`toggle_recording_from_hotkey`).
        self._stop_wait_ended_ms: int | None = None
        self._active_session_mode = "batch"
        self._focus_poll_timer = QtCore.QTimer(self)
        self._focus_poll_timer.setInterval(STREAMING_FOCUS_POLL_MS)
        self._focus_poll_timer.timeout.connect(self._on_stream_focus_poll)
        self._audio_callback_watchdog_timer = QtCore.QTimer(self)
        self._audio_callback_watchdog_timer.setSingleShot(True)
        self._audio_callback_watchdog_timer.timeout.connect(
            self._on_audio_callback_watchdog_timeout
        )
        self._audio_callback_watchdog_capture: AudioCapture | None = None
        # True once the first timeout found a stream PortAudio still runs and
        # re-armed for the hard limit (`_on_audio_callback_watchdog_timeout`).
        self._audio_callback_watchdog_extended = False
        # The capture `stop_recording` is stopping: its stop may wait for the
        # backlog of a starved callback thread, and those blocks must still
        # reach the streaming transcriber (`_on_stream_audio_chunk`).
        self._stopping_capture: AudioCapture | None = None
        self._audio_device_change_timer = QtCore.QTimer(self)
        self._audio_device_change_timer.setSingleShot(True)
        self._audio_device_change_timer.setInterval(AUDIO_DEVICE_CHANGE_SETTLE_MS)
        self._audio_device_change_timer.timeout.connect(
            self._on_audio_device_change_settled
        )
        self._preload_progress_timer = QtCore.QTimer(self)
        self._preload_progress_timer.setInterval(600)
        self._preload_progress_timer.timeout.connect(self._on_preload_progress_poll)
        # Flushes the results the paste pace held back, at the end of the
        # previous paste's clipboard-restore window.
        self._paste_pace_timer = QtCore.QTimer(self)
        self._paste_pace_timer.setSingleShot(True)
        self._paste_pace_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._paste_pace_timer.timeout.connect(self._on_paste_pace_timeout)
        # One re-paste (tray, hotkey or the overlay's Insert) asked for while
        # the restore window was open; a later request replaces it.
        self._pending_repaste: _PendingRepaste | None = None
        # Asks after a successful paste whether the focus was a text field;
        # None (the default, and every test that does not pass one) means no
        # check: every paste is reported as before.
        self._paste_target_check = paste_target_check
        self._paste_checks: dict[int, _PasteCheck] = {}
        self._paste_check_counter = 0
        self._preload_target_model: str | None = None
        self._preload_speed_tracker = ModelDownloadSpeedTracker()
        self._preload_cancel_requested = False
        self._preload_download_process: subprocess.Popen | None = None
        self._preload_downloading_model: str | None = None
        self._preload_downloading_dir: str = ""
        # Which half of the preload is running, as (generation, phase). A
        # preload downloads first and then loads the model into memory, and the
        # two take very different amounts of time for different reasons -- an
        # ONNX/Node runtime load is minutes of work with nothing arriving on
        # disk. Reporting both as "Downloading" printed a frozen "approx. 100%"
        # for an already complete model.
        self._preload_phase: tuple[int, str] | None = None
        self._preload_download_lock = threading.Lock()
        self._request_token_counter = 0
        self._active_request_token: int | None = None
        self._request_audio_by_token: dict[int, tuple[bytes, AppSettings]] = {}
        # In-flight transcription jobs (pending + running), insertion-ordered,
        # used for the overlay queue display, per-job target insertion, and
        # cooperative cancellation. A token is "live" while its job is present.
        self._jobs: dict[int, _TranscriptionJob] = {}
        self._deferred_background_results: list[tuple[_TranscriptionJob, str]] = []
        # True while a foreground result is between clearing its session state
        # and writing its own overlay state; a background report must not paint
        # into that gap (see _report_background_insertion_failure).
        self._foreground_delivery_pending = False

        self.vad_auto_stop_requested.connect(self.stop_recording)
        self.transcription_ready.connect(self._on_transcription_ready_result)
        self.transcription_failed.connect(self._on_transcription_failed_result)
        self.transcription_canceled.connect(self._on_transcription_canceled_result)
        self.transcription_progress.connect(self._on_transcription_progress_result)
        self.transcription_partial.connect(self._on_transcription_partial)
        self.stream_runtime_failed.connect(self._on_stream_runtime_failed)
        self.stream_connect_finished.connect(self._on_stream_connect_finished)
        self.stream_abort_requested.connect(self._on_stream_abort_requested)
        self.model_preload_done.connect(self._on_model_preload_done)
        self.audio_devices_changed.connect(self._on_audio_devices_changed)
        self.paste_target_checked.connect(self._on_paste_target_checked)
        self.audio_devices_refreshed.connect(self.refresh_overlay_microphone_options)
        self.unfinished_transcript_saved.connect(self._list_unfinished_transcript)

    @property
    def settings(self) -> AppSettings:
        return self._settings

    def initialize(self) -> None:
        self._start_audio_device_listener()
        self.reload_settings(re_register_hotkey=True)
        if self._settings.engine == DEFAULT_ENGINE:
            self._start_local_model_preload()
        else:
            self._preload_progress_timer.stop()
            self._preload_target_model = None
            self._preload_future = None
            self.show_idle_status()

    def shutdown(self) -> None:
        # Before anything else: the executor shutdown below cannot stop a
        # worker that is already inside a remote poll, and the interpreter
        # joins that worker at exit. The poll loops end within a slice once
        # this is set.
        request_transcription_shutdown()
        if self._shutdown_started:
            return
        self._shutdown_started = True
        # Early, and before the slow teardown below: the clipboard restore
        # after a paste waits on a daemon timer, which exit kills. Without
        # this the user's clipboard keeps the last transcript for good -- and
        # quitting right after a dictation is the ordinary way to end one.
        self._flush_pending_clipboard_restore()
        self._release_all_global_hotkeys()
        self._focus_poll_timer.stop()
        # A paced paste still waiting is dropped with the process; its text
        # is in history like every delivered transcript.
        self._paste_pace_timer.stop()
        self._pending_repaste = None
        # A check still running answers into nothing; its paste stands.
        self._paste_checks.clear()
        if self._paste_target_check is not None:
            self._paste_target_check.close()
        self._cancel_audio_callback_watchdog()
        self._audio_device_change_timer.stop()
        listener = self._audio_device_listener
        self._audio_device_listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                self._logger.exception("Failed to stop audio device listener")
        self._preload_progress_timer.stop()
        self._preload_cancel_requested = True
        self._cancel_preload_generation(self._preload_generation)
        self._terminate_preload_download_process()
        capture_wav = b""
        if self._audio_capture is not None:
            try:
                # No backlog wait at quit (`AudioCapture.stop`).
                capture_wav = self._audio_capture.stop(drain=False) or b""
            except Exception:
                self._logger.exception("Failed to stop the open capture at shutdown")
            self._audio_capture = None
        if self._warm_mic_stream is not None:
            try:
                self._warm_mic_stream.close()
            except Exception:
                pass
            self._warm_mic_stream = None
        active_stream = self._active_stream_transcriber
        self._active_stream_transcriber = None
        active_stream_lease = self._active_stream_runtime_lease
        self._active_stream_runtime_lease = None
        try:
            if active_stream is not None:
                # Abort, never stop: shutdown runs on the Qt main thread (wired
                # to app.aboutToQuit), and stop_stream() joins the worker with no
                # timeout while it runs the final transcription — the whole
                # recording when stream_final_full_pass is on. Quitting mid
                # dictation froze the UI for as long as that pass took. The
                # result is discarded here either way, so there is nothing to
                # gain by waiting for it. Every other teardown path already
                # prefers abort_stream().
                if hasattr(active_stream, "abort_stream"):
                    active_stream.abort_stream()
                else:
                    active_stream.stop_stream()
        except Exception:
            pass
        finally:
            if active_stream_lease is not None:
                active_stream_lease.release()
        self._active_stream_settings = None
        # Before the jobs below are marked aborting and their audio dropped.
        try:
            self._keep_unfinished_recordings(capture_wav)
        except BaseException:
            self._logger.exception("Failed to keep the unfinished recordings")
        for job in list(self._jobs.values()):
            job.aborting = True
            future = job.future
            canceled_before_start = False
            if future is not None:
                try:
                    canceled_before_start = bool(future.cancel())
                except Exception:
                    canceled_before_start = False
            if canceled_before_start:
                self._release_stream_job_runtime(job, abort=True)
        preload_future = self._preload_future
        self._preload_future = None
        if preload_future is not None:
            try:
                preload_future.cancel()
            except Exception:
                pass
        self._active_request_token = None
        self._request_audio_by_token.clear()
        self._jobs.clear()
        self._deferred_background_results.clear()
        self._undelivered_inserts.clear()
        # Every other teardown step above carries its own guard; these did
        # not, so a failure in the first of them skipped all three executor
        # shutdowns and left the transcription, stream-finalize and preload
        # workers running past `aboutToQuit`. `BaseException`, because the
        # process is quitting either way and there is nothing left to hand the
        # interrupt to.
        try:
            self._reset_streaming_state()
        except BaseException:
            self._logger.exception("Failed to reset streaming state at shutdown")
        try:
            self._reset_transcriber_cache()
        except BaseException:
            self._logger.exception("Failed to reset the runtime cache at shutdown")
        for name, executor in (
            ("transcription", self._executor),
            ("stream finalize", self._stream_finalize_executor),
            ("preload", self._preload_executor),
        ):
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except BaseException:
                self._logger.exception("Failed to shut down the %s executor", name)

    def _keep_unfinished_recordings(self, capture_wav: bytes) -> None:
        """Write every recording without a transcript to the unfinished store.

        The open capture, each job still transcribing, and the failures held
        for Retry: their audio is otherwise only in memory, because the
        managed last recording holds the newest recording alone. The next
        start offers them (`main._offer_unfinished_recordings`). A job whose
        transcript is finished and only waits for its paste is not one of
        them -- its text is in history -- and neither is a job the user
        cancelled. One recording reached by two roads (a retry running for
        the slot's bytes) is written once.
        """
        now = datetime.now()  # noqa: DTZ005 (local wall clock, as the queue rows show it)
        candidates: list[tuple[bytes, str, datetime]] = []
        if capture_wav:
            candidates.append((capture_wav, "", now))
        for job in self._jobs.values():
            if job.aborting or job.insertion_deferred:
                continue
            payload = self._request_audio_by_token.get(job.token)
            if payload is not None:
                candidates.append((payload[0], job.source_recording_id, job.created_at))
        for wav_bytes, recording_id in (
            (self._last_failed_wav_bytes, self._last_failed_recording_id),
            *self._older_failed_audio,
        ):
            if wav_bytes:
                recorded_at = self._recorded_at_by_recording_id.get(recording_id, now)
                candidates.append((wav_bytes, recording_id, recorded_at))
        seen_ids: set[str] = set()
        seen_unnamed: list[bytes] = []
        kept = failed = 0
        for wav_bytes, recording_id, recorded_at in candidates:
            if recording_id:
                if recording_id in seen_ids:
                    continue
                seen_ids.add(recording_id)
            else:
                if any(wav_bytes == other for other in seen_unnamed):
                    continue
                seen_unnamed.append(wav_bytes)
            try:
                path = self._unfinished_recording_store.save(
                    wav_bytes,
                    recording_id=recording_id or uuid4().hex,
                    recorded_at=recorded_at,
                )
            except (OSError, ValueError):
                failed += 1
                self._logger.exception(
                    "Failed to keep an unfinished recording. recording_id=%s",
                    recording_id or "n/a",
                )
                continue
            kept += path is not None
        if kept or failed:
            self._logger.info(
                "unfinished_recordings_kept count=%d failed=%d dir=%s",
                kept,
                failed,
                self._unfinished_recording_store.directory,
            )

    def quit_pending_work(self) -> PendingQuitWork:
        """What quitting now would cut short; see `PendingQuitWork`."""
        live = [job for job in self._jobs.values() if not job.aborting]
        waiting = sum(1 for job in live if job.insertion_deferred)
        return PendingQuitWork(
            recording=(
                self._audio_capture is not None
                or self._recording_start_in_progress
                or self._recording_stop_in_progress
            ),
            transcribing=len(live) - waiting,
            waiting_to_insert=waiting + (self._pending_repaste is not None),
            not_inserted=len(self._undelivered_inserts),
            failed=bool(self._last_failed_wav_bytes) + len(self._older_failed_audio),
            paste_settling=self._paste_pace_wait_s() > 0.0 or bool(self._paste_checks),
        )

    def hold_for_quit(self) -> None:
        """Let the pending work finish for a quit: stop the open recording,
        which is then transcribed and inserted as usual.

        Idempotent; the quit window calls it on every poll, so a capture
        that finished opening after the first call is stopped too. A new
        recording started while the hold stands calls the quit off
        (`start_recording`, `quit_canceled_by_recording`).
        """
        self._quit_hold = True
        if (
            self._audio_capture is not None
            and not self._recording_start_in_progress
            and not self._recording_stop_in_progress
        ):
            self._logger.info("quit_wait_stops_recording")
            with self._overlay_batch():
                self.stop_recording()

    def release_quit_hold(self) -> None:
        """The quit was called off: dictation works again."""
        self._quit_hold = False

    def transcribe_unfinished_recording(
        self,
        recording: UnfinishedRecording,
        progress_callback: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        """Transcribe a recording an earlier session left, into history only.

        Blocking: the startup notice calls it on a worker thread. Nothing is
        pasted -- the window it was dictated for is long gone -- but the
        transcript is listed as not inserted (owner's idea 2026-10-09), so
        the re-paste pastes it at the current caret when the user wants it
        (`_list_unfinished_transcript`). Once the transcript is in history
        the file is kept or deleted as the recording settings say
        (`_retain_unfinished_audio`), unless the transcript carries a gap
        marker: then it stays, like a dictation's recording with a gap
        (`_mark_last_recording_completed`), and the entry points at it. A
        failure keeps the file. Returns ``(ok, transcript or error)``.
        """
        path = str(recording.path)
        if not os.path.isfile(path):
            return False, "The recording is no longer available."
        settings = replace(self._settings, mode="batch")
        try:
            text = (
                self._executor.submit(
                    self._transcribe_import_worker,
                    path,
                    settings,
                    progress_callback,
                )
                .result()
                .strip()
            )
        except Exception as exc:
            self._logger.exception("Failed to transcribe an unfinished recording")
            return False, str(exc) or type(exc).__name__
        if not text:
            return False, _EMPTY_MODEL_TRANSCRIPT_MESSAGE
        gap = transcript_has_gap(text)
        # Where the audio stays, decided before the entry is written so the
        # entry can point at it. A gap transcript's file stays where it is,
        # out of reach of the archive's retention count.
        kept_path = (
            os.path.abspath(path) if gap else self._retain_unfinished_audio(recording)
        )
        entry = self._append_transcript_history(
            text,
            settings,
            "import",
            source_recording_id=recording.recording_id,
            source_audio_path=kept_path,
            track_for_edit=False,
        )
        if entry is None:
            if kept_path and kept_path != os.path.abspath(path):
                # Back where the next start offers it again.
                self._return_unfinished_audio(kept_path, path)
            return False, (
                "The transcript could not be saved to history (see the log); "
                f"the recording was kept. Transcript: {text}"
            )
        if not kept_path:
            try:
                self._unfinished_recording_store.discard(recording)
            except OSError:
                # Its transcript is in history, so the next start deletes it.
                self._logger.exception("Failed to delete a transcribed recording")
        self._logger.info(
            "unfinished_recording_transcribed recording_id=%s chars=%d kept=%s",
            recording.recording_id,
            len(text),
            kept_path or "no",
        )
        self.unfinished_transcript_saved.emit(text, entry, recording.recorded_at)
        return True, text

    @QtCore.Slot(str, object, object)
    def _list_unfinished_transcript(
        self,
        text: str,
        entry: TranscriptHistoryEntry | None,
        recorded_at: datetime | None,
    ) -> None:
        """List a transcript from the startup notice as not inserted.

        A row like a failed paste's: the queue panel and the not-inserted
        count show it, the re-paste joins it with the others and retires it
        once pasted, Dismiss drops it, an edit of its entry reaches it. Its
        time is the recording's own (local wall clock, as the file name
        says).
        """
        self._record_undelivered_insert(
            text,
            may_have_pasted=False,
            created_at=(
                recorded_at.astimezone()
                if recorded_at is not None
                else datetime.now().astimezone()
            ),
            history_entry=entry,
        )

    def _retain_unfinished_audio(self, recording: UnfinishedRecording) -> str:
        """Keep a transcribed unfinished recording as the settings keep any
        recording; the path it went to, or "" when nothing keeps it.

        Like a dictation's audio (owner's request 2026-10-09; it was always
        deleted): with "Archive every recording" it joins the archive under
        the archive's name, counts as its newest file and the retention
        count applies (`_prune_recordings`); with only "Keep last recording
        after successful transcription" it goes to the recordings folder
        under its own name, which no count prunes -- the managed last
        recording is one slot that holds the newest dictation and must not be
        overwritten by an older recording. A move that fails leaves the file
        where it was and answers that path (logged): the entry points at it,
        and the next start deletes no file an entry points at. Nothing after
        a successful move may stop the history write (the notice no longer
        offers a moved file, so its transcript would be lost): a failed mtime
        update or prune is logged, and the answer is where the file is.
        """
        if self._settings.save_all_recordings:
            try:
                target_dir = os.path.abspath(self._resolve_recordings_dir())
                os.makedirs(target_dir, exist_ok=True)
                recorded_at = recording.recorded_at or datetime.fromtimestamp(  # noqa: DTZ006 (local time on purpose, like the archive's names)
                    os.path.getmtime(recording.path)
                )
                stamp = recorded_at.strftime("%Y%m%d_%H%M%S")
                counter = 0
                target = os.path.join(target_dir, f"recording_{stamp}_000000.wav")
                while os.path.exists(target):
                    counter += 1
                    target = os.path.join(
                        target_dir, f"recording_{stamp}_{counter:06d}.wav"
                    )
                shutil.move(str(recording.path), target)
            except OSError:
                self._logger.exception("Failed to archive a transcribed recording")
                return os.path.abspath(recording.path)
            try:
                # The newest archived file now: the prune goes by age, and
                # the file's own age (the quit that kept it) could make it
                # the one deleted, under the entry pointing at it -- so no
                # prune when this fails.
                os.utime(target)
                self._prune_recordings(target_dir, self._settings.recordings_max_count)
            except Exception:
                self._logger.exception("Failed to prune the recordings archive")
            return target
        if self._settings.save_last_wav:
            try:
                return os.path.abspath(
                    self._unfinished_recording_store.move_to(
                        recording, Path(self._resolve_recordings_dir())
                    )
                )
            except OSError:
                self._logger.exception("Failed to keep a transcribed recording")
                return os.path.abspath(recording.path)
        return ""

    def _return_unfinished_audio(self, kept_path: str, original: str) -> None:
        """Undo `_retain_unfinished_audio` when the transcript was not saved."""
        try:
            shutil.move(kept_path, original)
        except OSError:
            self._logger.exception(
                "Failed to return a recording to the unfinished folder. path=%s",
                kept_path,
            )

    def _flush_pending_clipboard_restore(self) -> None:
        """Put the user's clipboard back before the process goes away.

        `getattr`, because the inserter is injected and not every double
        carries the method; guarded, because nothing about a clipboard restore
        may stop a shutdown that has already started -- the clipboard can be
        held open by another program at exactly this moment.
        """
        flush = getattr(self._text_inserter, "flush_pending_restore", None)
        if not callable(flush):
            return
        try:
            flush()
        except Exception:
            self._logger.exception("Failed to flush the pending clipboard restore")

    def _invalidate_transcriber_runtime(self) -> None:
        """Drop the cached runtime, or hand the drop to whoever is using it."""
        if self._transcription_runtime_active():
            # A batch worker or an active stream still holds the cached
            # transcriber. Closing it now could break that in-flight run (e.g.
            # a keep-loaded ONNX subprocess or a live Nemotron stream). Defer the
            # reset. The active shared lease applies it during release; an
            # isolated lease leaves it for the next shared-cache acquisition.
            # Changed settings and API keys therefore take effect without
            # closing a runtime that is still executing.
            with self._transcriber_runtime_state_lock:
                self._pending_transcriber_cache_reset = True
        else:
            self._reset_transcriber_cache()

    def invalidate_transcriber_credentials(
        self,
        providers: Sequence[str] | None = None,
    ) -> None:
        """Drop the cached runtime when a key *it actually uses* changed.

        Keys are read from the secret store while a transcriber is built and
        are not part of ``AppSettings``, so ``_transcriber_identity`` cannot see
        a key that was *replaced* with a different value — only one that was
        added or removed flips a ``has_*_key`` flag. The settings dialog
        therefore reports a key change explicitly through its own signal.

        A key belongs to exactly one engine, so this must not be a blanket
        invalidation: a loaded local model reads no API key at all, and a Groq
        runtime does not care about an OpenAI key. Throwing either away would
        cost a multi-gigabyte reload for a credential it never touches.
        Selecting that provider later changes ``settings.engine``, which the
        identity does see.
        """
        with self._transcriber_cache_lock:
            cached_key = self._transcriber_cache_key
        if cached_key is None:
            return
        if not isinstance(cached_key, _TranscriberIdentity):
            # Something other than an identity is cached. Rather than skip the
            # invalidation because a name lookup missed -- which would leave a
            # runtime holding a revoked key -- fall back to the safe direction.
            self._logger.warning(
                "Cached transcriber key is %s, not a runtime identity; "
                "invalidating unconditionally.",
                type(cached_key).__name__,
            )
            self._invalidate_transcriber_runtime()
            return
        loaded_engine = cached_key.engine
        if loaded_engine == DEFAULT_ENGINE:
            # A local model, which uses no credentials.
            return
        if isinstance(providers, str):
            # A bare string is iterable: {"g", "r", "o", "q"} matches no engine
            # and would silently invalidate nothing.
            providers = [providers]
        changed = {str(name) for name in providers or ()}
        if changed and loaded_engine not in changed:
            return
        self._logger.info(
            "Credentials changed for the loaded '%s' runtime; rebuilding it.",
            loaded_engine,
        )
        self._invalidate_transcriber_runtime()

    def reload_settings(self, re_register_hotkey: bool = True) -> None:
        previous_settings = self._settings
        self._settings = self._settings_store.load()
        setter = getattr(self._secret_store, "set_insecure_fallback_enabled", None)
        if callable(setter):
            try:
                setter(
                    bool(getattr(self._settings, "allow_insecure_key_storage", False))
                )
            except Exception:
                self._logger.exception("Failed to apply insecure key fallback setting")
        self._overlay.set_opacity_percent(self._settings.overlay_opacity_percent)
        self._overlay.set_always_on_top(
            bool(getattr(self._settings, "overlay_always_on_top", True))
        )
        self._sync_overlay_language_options()
        self.refresh_overlay_microphone_options()
        self._sync_warm_microphone_stream()
        self._warm_up_speech_check()
        # Only tear the loaded runtime down when the saved settings would build
        # a different one. Before this, *every* save closed it — overlay
        # opacity, a hotkey, the completion tone — and the preload that follows
        # reloaded a multi-gigabyte local model for a setting no transcriber
        # reads. That is the same needless reload a language change used to
        # cause, for a much larger set of settings.
        if self._transcriber_identity(previous_settings) != self._transcriber_identity(
            self._settings
        ):
            self._invalidate_transcriber_runtime()
        if re_register_hotkey:
            # A save is the one moment a combination moves between our own
            # hotkeys, so this is the path the release pass exists for.
            self._register_all_global_hotkeys()
        else:
            self._hotkey_registration_ok = True
            self._hotkey_notice = None
            self._cancel_hotkey_registration_ok = True
            self._cancel_hotkey_notice = None
            self._show_overlay_hotkey_registration_ok = True
            self._show_overlay_hotkey_notice = None
            self._repaste_hotkey_registration_ok = True
            self._repaste_hotkey_notice = None
            # The badge names the re-paste hotkey, which this reload may
            # change (`_register_all_global_hotkeys` refreshes it otherwise).
            self._update_not_inserted_badge()

    def on_settings_changed(self) -> None:
        """Reload settings after user applies changes in the settings dialog.

        Re-registers the hotkey.  When the engine is local and the saved
        settings describe a runtime that is not already loaded, triggers a
        background model preload so the first transcription is instant.
        """
        self.reload_settings(re_register_hotkey=True)
        if self._settings.engine == DEFAULT_ENGINE:
            if self._local_model_preload_needed(self._settings):
                self._start_local_model_preload()
            else:
                # The loaded runtime still matches the saved settings, so there
                # is nothing to load. Refresh the idle line anyway: it prints
                # the hotkey that is actually registered, which this very save
                # may have changed.
                self.show_idle_status()
        else:
            preload = self._preload_future
            self._preload_future = None
            self._preload_progress_timer.stop()
            self._preload_target_model = None
            with self._preload_result_lock:
                self._preload_phase = None
            self._cancel_preload_generation(self._preload_generation)
            self._preload_cancel_requested = False
            self._terminate_preload_download_process()
            if preload is not None and not preload.done():
                try:
                    preload.cancel()
                except Exception:
                    pass
            self.show_idle_status()

    def _overlay_session_active(self) -> bool:
        """True while the overlay belongs to a recording/transcription."""
        return (
            self._audio_capture is not None
            or self._streaming_recording
            or self._recording_start_in_progress
            or self._recording_stop_in_progress
            or self._active_request_token is not None
        )

    def show_idle_status(self) -> None:
        # Delayed callers (the preload timers) decided to return to Idle when
        # nothing was running. By the time they fire the user may have started
        # dictating, and overwriting "Listening" with "Idle" made it look as if
        # nothing was being recorded — pressing the hotkey again to "start"
        # then really stopped the running capture mid-sentence.
        if self._overlay_session_active():
            return
        # Every write below goes through `_paint_status_keeping_offer`: a
        # failed insert's text stays on screen with its Insert button through
        # a settings save, a resume and the preload's delayed return to idle.
        if not self._hotkey_registration_ok:
            self._paint_status_keeping_offer(
                "Error",
                self._hotkey_notice or "Hotkey registration failed.",
            )
            return
        if not self._cancel_hotkey_registration_ok:
            self._paint_status_keeping_offer(
                "Error",
                self._cancel_hotkey_notice or "Cancel hotkey registration failed.",
            )
            return
        if not self._show_overlay_hotkey_registration_ok:
            self._paint_status_keeping_offer(
                "Error",
                self._show_overlay_hotkey_notice
                or "Show-overlay hotkey registration failed.",
            )
            return
        if not self._repaste_hotkey_registration_ok:
            self._paint_status_keeping_offer(
                "Error",
                self._repaste_hotkey_notice or "Re-paste hotkey registration failed.",
            )
            return
        if self._preload_owns_overlay():
            # A running preload writes the status line every 600 ms. Replacing
            # it with "Idle" only produces two content swaps and two window
            # resizes before the next tick repaints the same progress.
            #
            # Below the hotkey branches on purpose: a failed registration is
            # the one thing a preload must not hide. `on_settings_changed`
            # calls this specifically to reprint a hotkey the save may have
            # changed, and gating that above the error branches swallowed it.
            # `_on_preload_progress_poll` carries the notice for the rest of
            # the preload, because it repaints this line every 600 ms.
            return

        # Show what is actually registered. The stored preference is kept even
        # while a fallback is active, so printing settings.hotkey here would
        # name a key that does nothing.
        detail = f"Hotkey: {self._active_hotkey or self._settings.hotkey}"
        if self._hotkey_notice:
            detail = f"{detail} — {self._hotkey_notice}"
        if self._settings.cancel_hotkey:
            detail = f"{detail} | Cancel: {self._settings.cancel_hotkey}"
            if self._cancel_hotkey_notice:
                detail = f"{detail} ({self._cancel_hotkey_notice})"
        show_overlay_hotkey = str(
            getattr(self._settings, "show_overlay_hotkey", "") or ""
        )
        if show_overlay_hotkey:
            detail = f"{detail} | Overlay: {show_overlay_hotkey}"
            if self._show_overlay_hotkey_notice:
                detail = f"{detail} ({self._show_overlay_hotkey_notice})"
        repaste_hotkey = str(getattr(self._settings, "repaste_hotkey", "") or "")
        if repaste_hotkey:
            detail = f"{detail} | Re-paste: {repaste_hotkey}"
            if self._repaste_hotkey_notice:
                detail = f"{detail} ({self._repaste_hotkey_notice})"
        self._paint_status_keeping_offer("Idle", detail)

    @contextlib.contextmanager
    def _overlay_batch(self):
        """Group overlay changes of one event into a single visual update.

        Most transitions touch the queue panel *and* the state text (a finished
        transcription clears its queue row and then publishes the transcript).
        Applied separately, the overlay resizes twice and briefly shows the
        previous content at the new size.
        """
        batch = getattr(self._overlay, "batched_update", None)
        if not callable(batch):
            yield
            return
        with batch():
            yield

    @QtCore.Slot()
    def toggle_recording(self) -> None:
        """Start or stop dictation (hotkey, tray and overlay entry point)."""
        with self._overlay_batch():
            self._toggle_recording()

    def toggle_recording_from_hotkey(self, message_time_ms: int) -> None:
        """The record hotkey's entry point; `message_time_ms` is its `MSG.time`.

        WM_HOTKEY is handled on the Qt thread, which a stop waiting for a
        starved microphone's backlog holds for seconds: a press made then is
        dispatched after the stop returned and would start a new recording
        the user never meant (review round 2 P2) -- most likely a second
        press because nothing seemed to happen. Such presses are dropped by
        their message time; a press after the wait toggles as usual.
        """
        ended = self._stop_wait_ended_ms
        if ended is not None:
            if message_time_not_after(message_time_ms, ended):
                self._logger.info(
                    "hotkey_press_during_stop_wait_ignored pressed_ms_before_end=%d",
                    (ended - message_time_ms) % (1 << 32),
                )
                return
            # A later press: the wait is over for good, and the wrapping
            # clock must not compare against a mark weeks old.
            self._stop_wait_ended_ms = None
        self.toggle_recording()

    def _toggle_recording(self) -> None:
        if self._recording_start_in_progress:
            self._pending_toggle_after_start_count += 1
            self._logger.info(
                "Queued hotkey toggle while recording start is in progress. "
                "pending_toggles=%s",
                self._pending_toggle_after_start_count,
            )
            return
        if self._recording_stop_in_progress:
            self._pending_toggle_after_stop_count += 1
            self._logger.info(
                "Queued hotkey toggle while recording stop is in progress. "
                "pending_toggles=%s",
                self._pending_toggle_after_stop_count,
            )
            return
        if self._audio_capture is None and self._streaming_recording:
            # Surface the overlay so this feedback is visible on the hotkey press
            # even when the overlay is floating and sitting behind other windows.
            self._overlay.reveal_temporarily()
            self._overlay.set_state(
                "Processing",
                "Streaming transcript is still finalizing. Please wait.",
            )
            return
        if self._audio_capture is None:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self) -> None:
        if self._recording_start_in_progress:
            self._logger.info("Ignored nested start_recording while start is active.")
            return
        if self._audio_capture is not None:
            # A recording is already active. This can happen when a queued
            # ``singleShot(0, self.start_recording)`` (from a prior stop's
            # toggle-parity drain) fires after the user already started a new
            # recording via the hotkey. Bail out instead of clobbering the
            # active capture.
            self._logger.info(
                "Ignored start_recording while a capture is already active."
            )
            return
        if self._audio_capture is None and self._streaming_recording:
            # Surface the overlay so this feedback is visible even when the
            # overlay is floating and sitting behind other windows.
            self._overlay.reveal_temporarily()
            self._overlay.set_state(
                "Processing",
                "Streaming transcript is still finalizing. Please wait.",
            )
            return
        if self._quit_hold:
            # The quit window waits for the pending work to end ("Wait and
            # insert"). Dictating again calls the quit off (owner decision
            # 2026-10-09; it used to be refused until "Don't quit"): the hold
            # goes first, so the window's next poll cannot stop this
            # recording, and the window closes on the signal. The note goes to
            # the tray, since this recording takes the overlay. A start
            # refused further down leaves the quit called off all the same.
            self._quit_hold = False
            self._logger.info("quit_canceled reason=recording_requested")
            self.quit_canceled_by_recording.emit()
            # Worded for the request, not the recording: a start refused
            # further down (a model still loading) calls the quit off too.
            self.busy_overlay_error.emit(
                "Quit canceled by the request to record. The app keeps "
                "running; choose Quit in the tray again to quit."
            )
        self._recording_start_in_progress = True
        try:
            start_target_handle = self._window_focus_helper.capture_target_window()
            start_target_signature = self._capture_target_signature(
                fallback_window=start_target_handle
            )
            self._apply_concurrent_mode_to_active_job()
            self._overlay.reveal_temporarily()
            preload = self._preload_future
            preload_running = (
                preload is not None
                and not preload.done()
                and self._matching_model_preload_running(self._settings)
            )

            if (
                preload_running
                and self._settings.engine == DEFAULT_ENGINE
                and self._settings.mode == "streaming"
            ):
                self._refuse_recording_start(
                    f"Model is still {self._preload_phase_word()}. Streaming "
                    "starts after the selected model is ready."
                )
                return

            preload_failure = self._model_preload_failure(self._settings)
            if preload_failure is not None:
                self._refuse_recording_start(preload_failure)
                return
            # Check if the selected engine supports streaming mode.
            if self._settings.mode == "streaming" and not supports_streaming(
                self._settings.engine,
                self._settings.model_size,
            ):
                if self._settings.engine == DEFAULT_ENGINE:
                    # Any batch-only *local* model, not just the ONNX/WebGPU
                    # ones: Parakeet and Canary are batch-only too and fell
                    # into the branch below, which told a user who is already
                    # on the local engine to "use local".
                    detail = (
                        f"Streaming is not available for the local model "
                        f"'{self._settings.model_size}'. Switch to batch mode, "
                        "or choose a faster-whisper or Nemotron local model "
                        "for streaming."
                    )
                else:
                    detail = (
                        "Streaming is not available for the selected provider. "
                        "Switch to batch mode, or use local/AssemblyAI/Deepgram "
                        "for streaming."
                    )
                self._refuse_recording_start(detail)
                return

            # Past every refusal: this dictation now takes the overlay, and
            # with it retires the previous Error state's Insert offer. Cleared
            # at the top of this method it went with every *refused* start
            # too (see `_refuse_recording_start`).
            self._retire_insert_offer()
            # Do not invite the user to speak until the microphone has actually
            # started. Opening a cold device (or a remote streaming session) can
            # take seconds on a locked-down machine, and audio spoken before
            # ``capture.start()`` completes is irretrievably lost.
            self._set_listening_overlay(
                "Starting dictation. Please wait for the 'Speak now' message.",
                starting=True,
            )
            QtCore.QCoreApplication.processEvents(
                QtCore.QEventLoop.ExcludeUserInputEvents,
                25,
            )
            # Qt can settle pending stylesheet/layout work while events drain.
            self._overlay.ensure_compact_size()

            self._target_window_handle = start_target_handle
            self._target_focus_signature = start_target_signature
            if start_target_handle:
                try:
                    current_window = self._current_foreground_window()
                    if current_window not in {None, start_target_handle}:
                        self._window_focus_helper.restore_target_window(
                            start_target_handle
                        )
                except Exception:
                    self._logger.exception(
                        "Failed to restore recording target after pending events"
                    )
            if self._settings.mode == "streaming":
                self._start_streaming_recording()
                return

            self._start_batch_recording(
                replace(self._settings),
                waiting_for_model=preload_running,
                preload_phase_word=self._preload_phase_word(),
            )
        finally:
            pending_toggles = self._pending_toggle_after_start_count
            self._pending_toggle_after_start_count = 0
            self._recording_start_in_progress = False
            self._flush_deferred_background_results()
            if pending_toggles % 2 == 1 and self._audio_capture is not None:
                self._logger.info(
                    "Applying queued hotkey stop after recording start completed."
                )
                QtCore.QTimer.singleShot(0, self.stop_recording)

    def _set_listening_overlay(self, detail: str, *, starting: bool = False) -> None:
        self._overlay.set_state("Listening", detail, compact=True, starting=starting)
        self._overlay.ensure_compact_size()

    def _start_batch_recording(
        self,
        settings_snapshot: AppSettings,
        *,
        waiting_for_model: bool = False,
        preload_phase_word: str = "loading",
    ) -> None:
        try:
            capture = self._build_audio_capture()
        except Exception as exc:
            # The caller has already shown "Listening". Without this the
            # exception escapes the Qt slot, PySide6 prints a traceback and
            # continues, and the overlay stays on "Starting dictation. Please
            # wait..." forever with no error and no way to tell what happened
            # -- every retry reproducing it. The streaming path guards the
            # same call for the same reason.
            self._logger.exception("Failed to open the microphone")
            self._overlay.set_state("Error", f"Failed to start recording: {exc}")
            return
        self._active_batch_settings = settings_snapshot
        self._active_session_mode = "batch"
        self._streaming_recording = False
        self._stream_text_state.reset()

        # Play beep BEFORE starting capture so the microphone does not
        # pick up the beep sound (winsound.Beep is synchronous/blocking).
        beep_started_at = time.perf_counter()
        self._play_start_beep()
        beep_ms = round((time.perf_counter() - beep_started_at) * 1000)

        capture_started_at = time.perf_counter()
        try:
            capture.start()
        except AudioCaptureError as exc:
            self._active_batch_settings = None
            self._overlay.set_state("Error", str(exc))
            self._logger.exception("Audio capture failed to start")
            self._repair_audio_system_if_needed(exc)
            return
        self._log_recording_start_timing("batch", beep_ms, capture_started_at, capture)

        self._audio_capture = capture
        self._arm_audio_callback_watchdog(capture)
        self._set_listening_overlay(
            " ".join(
                part
                for part in (
                    (
                        f"Selected model '{settings_snapshot.model_size}' is still "
                        f"{preload_phase_word}. You can record now; transcription "
                        "will wait for it."
                        if waiting_for_model
                        else ""
                    ),
                    "Speak now. Press hotkey again to stop.",
                )
                if part
            )
        )

    def _begin_stream_connect(self, transcriber) -> None:
        """Start the provider's stream without blocking the Qt thread.

        A remote provider's `start_stream` performs a network handshake:
        Deepgram waits up to 8 s for its socket and the AssemblyAI SDK connects
        synchronously. Called inline that froze the whole UI -- overlay, tray
        and settings -- for the entire handshake, right at the moment the user
        pressed the hotkey and expected to start talking.

        The microphone is therefore opened immediately and the handshake runs on
        a worker thread. Audio recorded in the meantime is buffered and handed
        over in order once the stream is ready, so nothing is lost; before this
        the same seconds were lost anyway, only with a frozen window on top.

        Local engines connect to nothing and return immediately, so they simply
        pass straight through this path.
        """
        self._stream_connect_generation += 1
        generation = self._stream_connect_generation
        # Identifies THIS handshake. Only a later handshake replaces it, so a
        # detached aborter can tell "my session is still the published one"
        # from "a newer session owns this transcriber" without depending on
        # counters the teardown path also touches, or on
        # `_active_stream_transcriber`, which is briefly None while the next
        # session is starting and is a shared cached object either way.
        connect_token = object()
        self._stream_connect_token = connect_token
        # The same object under a second name, because the two answer
        # different questions and therefore need different lifetimes.
        # `_stream_connect_token` answers "has a NEWER HANDSHAKE replaced
        # mine", which a detached aborter still has to be able to ask after
        # its session was torn down, so nothing but `_begin_stream_connect`
        # may write it. `_stream_session_token` answers "is the session that
        # produced this event still the live one", so every path that ends a
        # session clears it -- otherwise a cancelled session's token still
        # matched, and its provider's late `on_error` tore down whatever the
        # user had started since.
        self._stream_session_token = connect_token
        with self._stream_preconnect_lock:
            self._stream_preconnect_chunks = []
            self._stream_preconnect_dropped = False
            # A recorded failure lives until the finalize that joined its
            # handshake reads it; what no registered job can still read is
            # dropped here, at the one place a handshake begins.
            still_awaited = {job.connect_generation for job in self._jobs.values()}
            self._stream_connect_failures = {
                recorded: cause
                for recorded, cause in self._stream_connect_failures.items()
                if recorded in still_awaited
            }

        def _on_runtime_error(error_text: str) -> None:
            # The token is captured, not read when the callback fires: a
            # provider whose session was cancelled still owns this closure and
            # must be answered with the identity of the session it was
            # registered for, never with whatever is live by then.
            self._emit_stream_runtime_failure(connect_token, error_text)

        def _connect() -> None:
            try:
                transcriber.start_stream(
                    on_partial=self._emit_stream_partial,
                    on_error=_on_runtime_error,
                )
            except BaseException as exc:
                self._retire_failed_stream_connect(generation)
                error_text = self._stream_connect_error_text(exc)
                # Recorded before the emit: a finalize worker joining this
                # thread reads it right after the join returns.
                self._record_stream_connect_failure(generation, error_text)
                self.stream_connect_finished.emit(generation, False, error_text)
                return
            # Flush here, on this thread, while still ordered ahead of any
            # further callback: `_on_stream_audio_chunk` keeps appending to the
            # buffer until it is cleared under the same lock.
            failure = self._flush_preconnect_buffer(generation, transcriber)
            if failure is not None:
                self._record_stream_connect_failure(generation, failure)
            self.stream_connect_finished.emit(
                generation, failure is None, failure or ""
            )

        thread = threading.Thread(
            target=_connect,
            name="stt-stream-connect",
            daemon=True,
        )
        # Kept so the finalizer can wait for the handshake. Calling
        # `stop_stream()` while `start_stream()` is still running leaves the
        # provider's session half-published: the stop raises "not active", the
        # connect thread then publishes an ownerless socket and marks it
        # active, and every later dictation fails with "session already
        # active" for the rest of the app's life.
        self._stream_connect_thread = thread
        thread.start()

    @staticmethod
    def _stream_connect_error_text(exc: BaseException) -> str:
        if isinstance(exc, NotImplementedError):
            return str(exc) or "Streaming is not supported by this engine."
        if isinstance(exc, TranscriptionError):
            return str(exc) or "Streaming failed to start."
        return f"Failed to start streaming: {exc}"

    def _flush_preconnect_buffer(self, generation: int, transcriber) -> str | None:
        """Hand buffered audio to the now-ready stream. Returns an error text.

        Runs on the connect worker thread, never on the PortAudio callback, so
        it is allowed to wait -- and it has to. Deepgram's send queue holds 32
        chunks (3.2 s of audio) while this buffer may hold 62.5 s, and a bare
        loop of nonblocking pushes emptied it far too fast for the provider's
        sender thread to be scheduled at all: measured, 33 pushes complete in
        about 45 us against CPython's 5 ms switch interval, so chunk 33 was
        rejected and the dictation failed on a socket that had just connected.
        Each push therefore carries
        `STREAMING_PRECONNECT_FLUSH_PUT_TIMEOUT_S` as a wait budget, which a
        provider with no bounded queue simply ignores.

        The generation is re-checked between chunks: waiting means the flush
        can now outlive the session that started it, and a retired or
        cancelled session must stop pushing rather than spend a budget per
        remaining chunk on audio nobody wants.
        """
        while True:
            with self._stream_preconnect_lock:
                if generation != self._stream_connect_generation:
                    # A newer session owns the buffer now. Refusing to push is
                    # not enough -- clearing it here destroyed the *live*
                    # session's buffer, after which its audio bypassed
                    # buffering and was pushed into a transcriber that was
                    # still connecting, killing the brand-new dictation.
                    return None
                pending = self._stream_preconnect_chunks
                if pending is None:
                    return None
                if not pending:
                    # Empty and still current: clear the flag under the lock so
                    # the next callback pushes directly and cannot overtake us.
                    self._stream_preconnect_chunks = None
                    dropped = self._stream_preconnect_dropped
                    break
                self._stream_preconnect_chunks = []
            for chunk in pending:
                if generation != self._stream_connect_generation:
                    return None
                try:
                    transcriber.push_audio_chunk(
                        chunk,
                        block_timeout_s=STREAMING_PRECONNECT_FLUSH_PUT_TIMEOUT_S,
                    )
                except Exception as exc:
                    self._logger.exception("Failed to flush buffered stream audio")
                    return f"Streaming chunk push failed: {exc}"
        if dropped:
            self._logger.warning(
                "streaming_preconnect_buffer_overflow: the provider took too "
                "long to connect and buffered audio was dropped."
            )
        return None

    def _retire_failed_stream_connect(self, generation: int) -> None:
        """Silence the audio callback and drop the buffer of ONE handshake.

        Both writes belong to the handshake whose `start_stream` raised, so
        both are guarded by its generation and made under one hold of the
        lock. `_stream_chunk_error_reported` used to be written with no
        generation check at all, while the buffer drop beside it and the
        completion signal's handler both had one: a session the user had
        already cancelled therefore set the flag for the session that replaced
        it, and since only `_start_streaming_recording` ever clears the flag,
        that session dropped every audio chunk from then on and still
        delivered its one recorded chunk as a successful "Done".

        The flag comes first for the same reason it always did: otherwise the
        callback falls through to `push_audio_chunk` on a transcriber whose
        `start_stream` just raised, and "Streaming chunk push failed: session
        is not active" reaches the user instead of the real cause, such as an
        invalid API key.
        """
        with self._stream_preconnect_lock:
            if generation != self._stream_connect_generation:
                return
            self._stream_chunk_error_reported = True
            self._stream_preconnect_chunks = None

    def _record_stream_connect_failure(self, generation: int, error_text: str) -> None:
        """Keep a failed handshake's cause for the finalize that joins it.

        Under the handshake's own generation, and not gated on the live one:
        a cancel during the pending finalize runs `_reset_streaming_state`,
        which bumps the generation, while the worker is still parked in its
        join -- gated, a failure arriving after the cancel was refused, and
        the reset cleared one recorded before it, so the worker found
        nothing either way, called `stop_stream()` on a session never
        published and reported "Streaming session is not active" for a
        handshake that had failed on an invalid key (the wave-12
        concurrency lens). The record is the joining finalize's to consume.
        """
        with self._stream_preconnect_lock:
            self._stream_connect_failures[generation] = error_text

    def _stream_connect_failure_for(self, job: _TranscriptionJob | None) -> str | None:
        """Take the recorded failure of the handshake `job` finalizes, if any."""
        if job is None:
            return None
        with self._stream_preconnect_lock:
            return self._stream_connect_failures.pop(job.connect_generation, None)

    def _on_stream_connect_finished(
        self, generation: int, ok: bool, error_text: str
    ) -> None:
        if generation != self._stream_connect_generation:
            return  # a newer session replaced this one while it connected
        if not ok:
            if self._stream_finalize_pending:
                # The dictation has been stopped, and its finalize worker
                # joins this very handshake before it does anything else,
                # so it finds the failure recorded and reports it -- once,
                # with the cause. Reporting it here as well tore the
                # session down under that worker, whose own `stop_stream()`
                # on a provider that never started then arrived as a second
                # report: "Streaming session is not active" painted over
                # the invalid key, and a second failure mark on the
                # recording.
                self._logger.debug(
                    "stream_connect_failed_after_stop: the finalize reports it"
                )
                return
            # The generation check above already established that this is the
            # live session, so the live session token is this handshake's own.
            self._on_stream_runtime_failed(
                self._stream_session_token,
                error_text or "Streaming failed to start.",
            )
            return
        if not self._streaming_recording or self._stream_abort_requested:
            return
        if self._stream_finalize_pending:
            # The dictation has been stopped and its finalize is in flight;
            # the handshake only landed late enough for its buffered audio to
            # be handed over. `_streaming_recording` is still True until the
            # result is delivered, so without this the line below would
            # replace "Finalizing streaming transcript..." with an invitation
            # to speak into a session the user has already ended.
            return
        if not (
            self._stream_text_state.live_text
            or self._stream_text_state.last_partial_text
        ):
            self._set_listening_overlay(
                "Streaming active. Speak now, press hotkey to finalize."
            )

    def _start_streaming_recording(self) -> None:
        settings_snapshot = replace(self._settings)
        runtime_lease: _TranscriberRuntimeLease | None = None
        # Bound before the `try` because the `BaseException` arm below tears
        # the handshake down, and that arm is also reachable from
        # `_acquire_transcriber_runtime` -- i.e. before `transcriber` would
        # otherwise exist.
        #
        # Not load-bearing, and measured as such: the teardown sits inside its
        # own `try`/`except BaseException`, and the argument is evaluated
        # there too, so an `UnboundLocalError` is caught and the observable
        # behaviour is identical (a mutation removing both this line and the
        # `is not None` guard leaves the test green). What it buys is an
        # honest log -- without it every such failure records a full
        # "Failed to tear down the stream connect" traceback that is really
        # our own unbound local, which would send the next reader after the
        # wrong thing entirely.
        transcriber = None
        try:
            # Cleared before the handshake starts, not after. The connect
            # thread sets this flag when `start_stream` fails, and it wins
            # the race against a later reset from this thread every time
            # (measured 200/200), so resetting afterwards silently undid the
            # very protection it exists for.
            self._stream_chunk_error_reported = False
            runtime_lease = self._acquire_transcriber_runtime(settings_snapshot)
            transcriber = runtime_lease.transcriber
            self._begin_stream_connect(transcriber)
        except NotImplementedError as exc:
            if runtime_lease is not None:
                runtime_lease.release()
            self._overlay.set_state("Error", str(exc))
            return
        except TranscriptionError as exc:
            if runtime_lease is not None:
                runtime_lease.release()
            self._overlay.set_state("Error", str(exc))
            return
        except Exception as exc:
            if runtime_lease is not None:
                runtime_lease.release()
            self._logger.exception("Failed to start streaming transcriber")
            self._overlay.set_state("Error", f"Failed to start streaming: {exc}")
            return
        except BaseException:
            # The same shape the two arms below the capture guard already use,
            # one frame up. This is the outermost frame that holds the lease,
            # and `_begin_stream_connect` starts a thread -- so a
            # `BaseException` escaping here strands
            # `_transcriber_runtime_lock` for the process lifetime, which is
            # exactly the outcome the guards below were added to make
            # unreachable. Bookkeeping only, then re-raise: a `BaseException`
            # on the Qt thread must not be turned into an overlay message.
            #
            # The teardown is here for the same reason the two arms below the
            # capture guard have it: `_begin_stream_connect` has already
            # spawned the handshake, so releasing the lease alone lets
            # `start_stream` finish and publish a session nobody owns -- every
            # later dictation then fails with "Streaming session already
            # active" while a remote provider's socket stays open and billed.
            # It runs before the release and cannot raise past it, which is
            # the ordering the sibling arms had to be corrected to.
            try:
                if transcriber is not None:
                    self._teardown_pending_stream_connect(transcriber)
            except BaseException:
                self._logger.exception(
                    "Failed to tear down the stream connect after a "
                    "BaseException starting the stream"
                )
            finally:
                if runtime_lease is not None:
                    runtime_lease.release()
            raise

        try:
            # Inside its own guard: nothing owns the lease between the block
            # above and `_active_stream_runtime_lease` below. No current
            # statement in `_build_audio_capture` is known to raise --
            # `AppSettings.from_dict` already coerces and clamps
            # `vad_energy_threshold`, and the `EnergyVad` and `AudioCapture`
            # constructors are attribute assignment -- so this is depth, not a
            # fix for a live trigger. It is kept because the blast radius is
            # out of all proportion to the cost: leaking the lease strands
            # `_transcriber_runtime_lock` for the process lifetime: every later
            # preload and audio import blocks forever, every dictation builds
            # its own isolated runtime, and `_transcription_runtime_active()`
            # stays True so no deferred cache reset ever runs.
            capture = self._build_audio_capture(
                chunk_callback=self._on_stream_audio_chunk
            )
        except Exception as exc:
            # The handshake is already running (`_begin_stream_connect` above),
            # so releasing the lease is not enough: `start_stream` completes
            # and publishes a session nobody owns, and the next dictation is
            # refused with "Streaming session already active" while a remote
            # provider's socket stays open and billed. Same teardown as the
            # `AudioCaptureError` arm below, for the same reason.
            # `release()` used to be the first statement here, so nothing
            # could come between the failure and it. Putting the teardown in
            # front made two things reachable: the teardown reaches provider
            # code (`abort_stream`) and starts a thread, and `Thread.start`
            # raises `RuntimeError` when the process cannot create one. That
            # stranded `_transcriber_runtime_lock` for the process lifetime
            # *and* escaped this Qt slot, so the overlay sat on "Listening"
            # with no error -- the exact state this arm exists to replace.
            # The helper is exception-tight too; this is the second layer,
            # kept because the blast radius is out of proportion to the cost.
            try:
                self._teardown_pending_stream_connect(transcriber)
            except BaseException:
                self._logger.exception(
                    "Failed to tear down the stream connect after a capture failure"
                )
            finally:
                runtime_lease.release()
            self._logger.exception("Failed to open the microphone for streaming")
            self._overlay.set_state("Error", f"Failed to start recording: {exc}")
            return

        # Publish the session before starting PortAudio. A stream is allowed to
        # deliver its first callback from inside ``start()``; publishing these
        # references afterward silently dropped those first audio blocks.
        self._stream_abort_requested = False
        self._stream_insertion_suspended = False
        self._stream_insert_held = False
        self._stream_insert_failures = 0
        self._stream_text_state.reset()
        self._active_session_mode = "streaming"
        self._streaming_recording = True
        self._active_stream_transcriber = transcriber
        self._active_stream_runtime_lease = runtime_lease
        self._active_stream_settings = settings_snapshot
        self._audio_capture = capture

        # Play beep BEFORE starting capture so the microphone does not
        # pick up the beep sound (winsound.Beep is synchronous/blocking).
        beep_started_at = time.perf_counter()
        self._play_start_beep()
        beep_ms = round((time.perf_counter() - beep_started_at) * 1000)

        capture_started_at = time.perf_counter()
        try:
            capture.start()
        except AudioCaptureError as exc:
            self._audio_capture = None
            self._active_stream_transcriber = None
            self._active_stream_runtime_lease = None
            self._active_stream_settings = None
            # The handshake was started microseconds ago and is almost certainly
            # still running. Aborting now is not a no-op: the abort finds no
            # session, `start_stream` then publishes one anyway, and it is
            # orphaned -- every later dictation fails with "Streaming session
            # already active" until the app is restarted. So one microphone
            # failure used to disable streaming for good.
            # See the arm above: report the capture failure whatever the
            # best-effort teardown does, and never leave the lease behind.
            try:
                self._teardown_pending_stream_connect(transcriber)
            except BaseException:
                self._logger.exception(
                    "Failed to tear down the stream connect after a capture failure"
                )
            finally:
                if runtime_lease is not None:
                    runtime_lease.release()
            self._reset_streaming_state()
            self._overlay.set_state("Error", str(exc))
            self._logger.exception("Audio capture failed to start")
            self._repair_audio_system_if_needed(exc)
            return

        self._log_recording_start_timing(
            "streaming", beep_ms, capture_started_at, capture
        )
        self._active_batch_settings = None
        self._arm_audio_callback_watchdog(capture)
        # Always armed. The poll itself decides what a focus change means
        # (abort, or suspend insertion); gating the timer on the abort flag
        # left the suspension unreachable and dropped the protection
        # entirely -- live partials then pasted into whatever window
        # happened to be in front.
        self._focus_poll_timer.start()
        if not (
            self._stream_text_state.live_text
            or self._stream_text_state.last_partial_text
        ):
            with self._stream_preconnect_lock:
                still_connecting = self._stream_preconnect_chunks is not None
            self._set_listening_overlay(
                # Honest about the handshake, and explicit that speaking now is
                # safe -- the audio is being buffered, not thrown away.
                "Connecting to the speech service. You can speak now."
                if still_connecting
                else "Streaming active. Speak now, press hotkey to finalize."
            )

    def _teardown_pending_stream_connect(self, transcriber) -> None:
        """Abort a stream whose handshake may still be in flight.

        Waits for `start_stream` to finish first, off the Qt thread, so the
        abort acts on a session that actually exists. The wait is bounded, and
        the abort runs either way.
        """
        thread = self._stream_connect_thread
        self._stream_connect_thread = None
        self._stream_connect_generation += 1
        # Abandoning the handshake abandons the session, so its identity stops
        # matching here too. `_stream_connect_token` is deliberately NOT
        # cleared: the detached aborter below reads it to tell a newer
        # handshake from its own.
        self._stream_session_token = None
        with self._stream_preconnect_lock:
            self._stream_preconnect_chunks = None

        # The lease is released right after this returns, which puts the
        # transcriber back in the shared cache -- so by the time a detached
        # aborter wakes, the object it holds may be the one a NEW session is
        # using, and aborting then kills the new dictation.
        #
        # Two guards have already been wrong here. The connect generation
        # could never match, because the caller bumps it one statement later,
        # so the abort was skipped every time the detached path was taken and
        # the provider socket stayed published. Object identity was wrong in
        # both directions: `_active_stream_transcriber` is None for a moment
        # while the next session starts (the abort then tore down a session
        # that was starting), and it is a shared cached object, so it says
        # nothing about whether *this* handshake is still the published one.
        #
        # The token is set only by `_begin_stream_connect`, i.e. only a newer
        # handshake can invalidate it.
        token = self._stream_connect_token

        def _abort() -> None:
            if thread is not None and thread.is_alive():
                thread.join(timeout=STREAMING_CONNECT_JOIN_TIMEOUT_S)
            if token is not None and self._stream_connect_token is not token:
                self._logger.info(
                    "Skipping a stale stream abort: a newer session owns "
                    "this transcriber now."
                )
                return
            try:
                if hasattr(transcriber, "abort_stream"):
                    transcriber.abort_stream()
                else:
                    transcriber.stop_stream()
            except Exception:
                self._logger.exception("Failed to abort a pending stream connect")

        # Best-effort cleanup by definition: it exists to abandon a handshake
        # nobody wants any more. Every caller is an error path that still has
        # a lease to release and an overlay to update, so raising here would
        # replace one failure with a worse one. `Thread.start` raises
        # `RuntimeError` when the process cannot create another thread, and
        # `_abort` runs provider code inline whenever the connect thread has
        # already finished -- which is the common case.
        try:
            if thread is None or not thread.is_alive():
                _abort()
                return
            threading.Thread(
                target=_abort, name="stt-stream-connect-abort", daemon=True
            ).start()
        except BaseException:
            self._logger.exception("Failed to tear down a pending stream connect")

    @staticmethod
    def _audio_capture_runtime_context(capture: AudioCapture) -> tuple[bool, int]:
        """Snapshot diagnostics before ``capture.stop()`` mutates its state."""
        try:
            warm_value = capture.uses_warm_stream
        except (AttributeError, RuntimeError):
            warm_value = getattr(capture, "_warm_attached", False)
        try:
            callback_count = max(0, int(getattr(capture, "callback_count", 0)))
        except (TypeError, ValueError, RuntimeError):
            callback_count = 0
        return bool(warm_value), callback_count

    def _log_recording_start_timing(
        self,
        mode: str,
        beep_ms: int,
        capture_started_at: float,
        capture: AudioCapture,
    ) -> None:
        """Diagnose slow recording starts (audio is lost until capture runs).

        On locked-down machines opening the microphone can take seconds; this
        makes the culprit visible in the log so 'my first words are cut off'
        reports can be verified and the keep_microphone_warm option suggested.
        """
        capture_ms = round((time.perf_counter() - capture_started_at) * 1000)
        warm, _callback_count = self._audio_capture_runtime_context(capture)
        level = logging.WARNING if capture_ms >= 500 else logging.INFO
        self._logger.log(
            level,
            "recording_start_timing mode=%s beep_ms=%d capture_start_ms=%d "
            "warm_stream=%s%s",
            mode,
            beep_ms,
            capture_ms,
            warm,
            (
                " (slow microphone open; speech before this point is lost — "
                "consider enabling keep_microphone_warm)"
                if capture_ms >= 500 and not warm
                else ""
            ),
        )

    def _arm_audio_callback_watchdog(self, capture: AudioCapture) -> None:
        self._audio_callback_watchdog_capture = capture
        self._audio_callback_watchdog_extended = False
        self._audio_callback_watchdog_timer.start(
            AUDIO_CAPTURE_FIRST_CALLBACK_TIMEOUT_MS
        )

    def _cancel_audio_callback_watchdog(
        self,
        capture: AudioCapture | None = None,
    ) -> None:
        if capture is not None and self._audio_callback_watchdog_capture is not capture:
            return
        self._audio_callback_watchdog_timer.stop()
        self._audio_callback_watchdog_capture = None

    def _on_audio_callback_watchdog_timeout(self) -> None:
        """No first block yet: wait for a starved stream, abort a dead one.

        Two stages. At `AUDIO_CAPTURE_FIRST_CALLBACK_TIMEOUT_MS` a stream
        that PortAudio still reports active is starved rather than dead -- on
        a machine at 100% CPU the callback thread is simply not scheduled,
        and the device keeps capturing into the `AUDIO_INPUT_BUFFER_S` buffer
        meanwhile -- so the stall is logged and the watchdog re-armed up to
        `AUDIO_CAPTURE_FIRST_CALLBACK_HARD_TIMEOUT_MS`. Aborting there, as
        this did at 2 s, threw away a recording whose audio was on its way:
        the field report "a recording stops on its own right away and a red
        error appears". A stream PortAudio reports stopped is aborted at
        once, and so is anything still silent at the hard limit. The overlay
        keeps "Speak now": the audio is buffered, so it stays true.
        """
        capture = self._audio_callback_watchdog_capture
        self._audio_callback_watchdog_capture = None
        if (
            self._shutdown_started
            or capture is None
            or capture is not self._audio_capture
        ):
            return

        warm_stream, callback_count = self._audio_capture_runtime_context(capture)
        try:
            has_received_audio = bool(capture.has_received_audio)
        except (AttributeError, RuntimeError):
            has_received_audio = callback_count > 0
        if has_received_audio:
            return

        try:
            stream_active = capture.stream_is_active()
        except (AttributeError, RuntimeError):
            stream_active = None
        if not self._audio_callback_watchdog_extended and stream_active is not False:
            self._audio_callback_watchdog_extended = True
            self._audio_callback_watchdog_capture = capture
            self._logger.warning(
                "audio_capture_callback_slow mode=%s waited_ms=%d warm_stream=%s "
                "stream_active=%s; waiting up to %d ms for the first block (a "
                "starved callback thread delivers late, its audio is buffered)",
                self._active_session_mode,
                AUDIO_CAPTURE_FIRST_CALLBACK_TIMEOUT_MS,
                warm_stream,
                stream_active,
                AUDIO_CAPTURE_FIRST_CALLBACK_HARD_TIMEOUT_MS,
            )
            self._audio_callback_watchdog_timer.start(
                AUDIO_CAPTURE_FIRST_CALLBACK_HARD_TIMEOUT_MS
                - AUDIO_CAPTURE_FIRST_CALLBACK_TIMEOUT_MS
            )
            return

        self._logger.error(
            "audio_capture_callback_timeout mode=%s timeout_ms=%d "
            "warm_stream=%s callback_count=%d stream_active=%s",
            self._active_session_mode,
            (
                AUDIO_CAPTURE_FIRST_CALLBACK_HARD_TIMEOUT_MS
                if self._audio_callback_watchdog_extended
                else AUDIO_CAPTURE_FIRST_CALLBACK_TIMEOUT_MS
            ),
            warm_stream,
            callback_count,
            stream_active,
        )
        detail = "Microphone capture started but did not deliver audio. Please retry."
        if warm_stream:
            # A dead warm stream (its device was switched or removed) would
            # otherwise fail every following recording too; restart it on the
            # freshly enumerated device list so the next attempt works.
            detail = (
                "Microphone capture started but did not deliver audio. "
                "Restarting the warm microphone stream - please retry. "
                "Disable Keep microphone warm if it repeats."
            )
        if self._streaming_recording:
            self._abort_streaming_session(
                detail,
                beep=False,
                finalize_stream=False,
            )
            if warm_stream:
                self.request_audio_device_refresh()
            return

        # This is an abort, not a normal stop. A first callback can race with
        # the timeout after the check above; routing through ``stop_recording``
        # would then submit those late bytes for transcription and show an Error
        # at the same time. Preserve any late bytes for Retry, but never submit
        # them automatically from the timeout path.
        wav_bytes, _ = self._stop_active_capture()
        if wav_bytes:
            # Late bytes replace the slot; none leave the older failure it
            # holds retryable, as the dying stream's road does. Written
            # unconditionally, a timeout with no late bytes emptied that
            # failure's only copy.
            persisted = self._persist_last_recording_audio(wav_bytes)
            self._hold_failed_audio_for_retry(
                wav_bytes, self._last_persisted_recording_id if persisted else ""
            )
            if persisted:
                try:
                    self._last_recording_store.mark_failed(
                        detail,
                        expected_recording_id=self._last_persisted_recording_id or None,
                    )
                except Exception:
                    self._logger.exception("Failed to mark stalled recording")
        self._reset_streaming_state()
        self._overlay.set_state(
            "Error",
            detail,
            # Retry transcribes the slot: the late bytes when there were any,
            # an older failure otherwise, under an Error about this recording.
            error_action=None if wav_bytes else OVERLAY_ERROR_ACTION_NONE,
        )
        self._reveal_overlay_result(is_error=True)
        self._flush_deferred_background_results()
        if warm_stream:
            self.request_audio_device_refresh()
        else:
            self._maybe_resume_pending_audio_device_refresh()

    def _build_audio_capture(self, chunk_callback=None) -> AudioCapture:
        vad = None
        if self._settings.vad_enabled:
            threshold = max(
                VAD_ENERGY_THRESHOLD_MIN,
                float(self._settings.vad_energy_threshold),
            )
            vad = EnergyVad(
                sample_rate=AUDIO_SAMPLE_RATE,
                energy_threshold=threshold,
                min_speech_ms=VAD_MIN_SPEECH_MS,
                max_silence_ms=VAD_MAX_SILENCE_MS,
            )
        input_device_name = str(getattr(self._settings, "input_device_name", "") or "")
        return AudioCapture(
            sample_rate=AUDIO_SAMPLE_RATE,
            channels=AUDIO_CHANNELS,
            vad=vad,
            auto_stop_callback=self._auto_stop_from_vad,
            chunk_callback=chunk_callback,
            logger=self._logger,
            warm_stream=self._warm_mic_stream,
            device_key=input_device_name,
            # Resolved at stream open, never earlier: indices are only valid
            # until the next PortAudio re-enumeration.
            device_resolver=lambda name=input_device_name: (
                audio_devices.resolve_input_device(name)
            ),
        )

    def _sync_warm_microphone_stream(self) -> None:
        """Create, retarget, or tear down the shared warm stream per settings."""
        enabled = bool(getattr(self._settings, "keep_microphone_warm", False))
        if enabled and self._warm_mic_stream is None:
            self._warm_mic_stream = WarmMicrophoneStream(
                logger=self._logger,
                device_provider=self._warm_microphone_device,
                selected_key_provider=self._warm_microphone_selected_device,
            )
            self._start_warm_microphone_stream_async()
        elif not enabled and self._warm_mic_stream is not None:
            stream = self._warm_mic_stream
            self._warm_mic_stream = None
            # Deferred while a recording is attached (the global hotkey works
            # with the settings dialog open); an immediate close would
            # silently cut off that recording's audio source. Idle streams
            # close on a worker thread so a slow audio stack cannot block Qt.
            stream.request_close()
        elif enabled and self._warm_mic_stream is not None:
            stream = self._warm_mic_stream
            # One snapshot: three property reads are three lock acquisitions,
            # and an open can finish between them.
            opened, resolving, opening = stream.device_state()
            selected = self._warm_microphone_selected_device()
            if opened is None and not opening:
                # Not running (an earlier open failed); a settings save is a
                # natural retry point.
                self._start_warm_microphone_stream_async()
            elif opened is None:
                # An open is in flight. `ensure_started` no-ops on its
                # `_starting` guard, so the old "retry" branch let the open
                # finish on the previous device and nothing ever restarted
                # it -- the warm stream was then pinned to the old microphone
                # for the session, `attach` refused it, and every recording
                # cold-opened. But `opened_device_key` is None for the whole
                # of an open, so "opened != selected" restarted the open on
                # *every* save -- an opacity or hotkey change discarded it
                # and paid the cold-open latency twice. Only an open that is
                # opening a different microphone is restarted. The stream
                # publishes the key it read *before* resolving it, so this
                # sees the device the open will produce for the whole of the
                # device query -- published only afterwards, a save landing
                # inside that query saw None, asked for no restart, and the
                # open finished on the old microphone (measured: the setting
                # said mic-B, the stream opened mic-A, `attach` refused it
                # for the rest of the session).
                if resolving is not None and resolving != selected:
                    stream.request_restart()
            elif opened != selected:
                stream.request_restart()

    def _warm_microphone_selected_device(self) -> str:
        """The persisted microphone key the next warm-stream open will use."""
        return str(getattr(self._settings, "input_device_name", "") or "")

    def _warm_microphone_device(self) -> tuple[str, int | None]:
        """Resolve the currently selected microphone for a warm-stream open."""
        name = self._warm_microphone_selected_device()
        return name, audio_devices.resolve_input_device(name)

    def _start_warm_microphone_stream_async(self) -> None:
        """Open the warm stream off the UI thread; opening can take seconds
        on locked-down machines, which is exactly what this feature hides."""
        stream = self._warm_mic_stream
        if stream is None:
            return
        threading.Thread(
            target=stream.ensure_started,
            name="stt_app_warm_mic",
            daemon=True,
        ).start()

    def _restart_warm_microphone_stream_after_resume(self) -> None:
        stream = self._warm_mic_stream
        if stream is None:
            return
        # request_restart defers while a recording is attached to the warm
        # stream and closes/reopens on a worker thread otherwise, so it cannot
        # yank the device from under an active or just-starting capture (the
        # old Qt-thread pre-check raced exactly that window).
        stream.request_restart()

    def _repair_audio_system_if_needed(self, exc: AudioCaptureError) -> None:
        """Re-enumerate when the failure was PortAudio not answering.

        That state is usually self-inflicted: a refresh whose ``sd._terminate``
        succeeded and whose ``sd._initialize`` failed leaves PortAudio down for
        the process lifetime, and every later recording fails identically.
        Re-enumerating initializes it again, so one failed recording becomes a
        hiccup instead of an app that stays deaf until it is restarted.
        """
        if not getattr(exc, "audio_system_unavailable", False):
            return
        self._logger.warning(
            "audio_system_unavailable_repair_requested mode=%s",
            self._active_session_mode,
        )
        self.request_audio_device_refresh()

    def request_audio_device_refresh(self) -> None:
        """Re-enumerate audio devices and restart the warm stream when idle.

        Public entry point for a manual refresh (settings dialog); device
        change notifications funnel into the same coalescing timer.
        """
        if not self._shutdown_started:
            self._audio_device_change_timer.start()

    def _start_audio_device_listener(self) -> None:
        if self._audio_device_listener is not None:
            return
        listener = AudioDeviceChangeListener(
            on_change=self.audio_devices_changed.emit,
            logger=self._logger,
        )
        if listener.start():
            self._audio_device_listener = listener
        else:
            self._logger.warning(
                "Audio device change notifications unavailable; microphone "
                "hot-plug and default-device switches need a manual refresh "
                "from Settings or an app restart."
            )

    def _on_audio_devices_changed(self, kind: str) -> None:
        # Runs on the Qt thread via the queued signal connection. One physical
        # event raises several notifications (per role/endpoint), so coalesce
        # before reacting.
        if self._shutdown_started:
            return
        self._logger.info("audio_device_change kind=%s", kind)
        self._audio_device_change_timer.start()

    def _capture_owns_the_microphone(self) -> bool:
        """A recording is starting, running or stopping."""
        return (
            self._audio_capture is not None
            or self._recording_start_in_progress
            or self._recording_stop_in_progress
        )

    def _on_audio_device_change_settled(self) -> None:
        if self._shutdown_started:
            return
        if self._capture_owns_the_microphone():
            # Never touch devices mid-recording; retried once the capture
            # stops via _maybe_resume_pending_audio_device_refresh.
            self._pending_audio_device_refresh = True
            return
        self._pending_audio_device_refresh = False
        try:
            threading.Thread(
                target=self._refresh_audio_devices_worker,
                name="stt_app_audio_device_refresh",
                daemon=True,
            ).start()
        except Exception as exc:
            # A starved interpreter cannot start another thread (`RuntimeError`,
            # or `MemoryError` when the stack cannot be allocated). Discharged
            # with no worker to run it, the refresh was simply gone -- a
            # hot-plugged or newly defaulted microphone stayed invisible
            # until the next device event -- and the exception escaped the
            # Qt slot. Owed again, it is retried at the next recording stop.
            self._pending_audio_device_refresh = True
            self._logger.warning(
                "Audio device refresh could not start its worker: %s", exc
            )

    def _refresh_audio_devices_worker(self) -> None:
        """Close the idle warm stream, re-enumerate PortAudio, reopen warm.

        Runs off the Qt thread because PortAudio calls can block for seconds
        on locked-down audio stacks. ``try_refresh_input_devices`` refuses to
        re-initialize while any stream is live, so a recording that slips in
        concurrently is never torn down; the refresh is retried later instead.
        """
        # One at a time. Several device notifications more than the settle
        # interval apart start one of these each; the PortAudio guard used
        # to serialize them, and it is deliberately not held across the
        # close any more (below), so they queue here instead of one closing
        # what the other has just reopened.
        with self._audio_device_refresh_lock:
            self._refresh_audio_devices_serialized()

    def _refresh_audio_devices_serialized(self) -> None:
        # The warm stream is closed *outside* the PortAudio guard. A cold
        # recording start takes that guard on the Qt thread, and a close on
        # a locked-down audio stack is bounded by nothing: held across it,
        # the guard froze the Qt thread for the whole close, after the start
        # beep had already played (measured: 3.0 s for a 3 s close, 30.0 s
        # for one that outlasted `_CLOSE_WAIT_S`, 0.000 s with the close
        # outside). Closing one stream while another opens is fine; only the
        # re-enumeration needs PortAudio to itself, and
        # `try_refresh_input_devices` holds the guard for exactly that, so a
        # warm open arriving during it waits and then opens on the fresh
        # list (`ensure_started` takes the guard before its gate).
        # This run discharges the refresh an earlier one left owed, because
        # it is about to perform it; its own refusals below arm the flag
        # again. Left armed, a refusal by the previous worker outlived this
        # one's success and the next recording stop closed, re-enumerated
        # and reopened the warm stream for a refresh that had already
        # happened. Cleared here and not at the end, so a device event
        # deferred on the Qt thread while this runs (a recording started
        # meanwhile) is not discharged by an enumeration that may predate
        # it.
        self._pending_audio_device_refresh = False
        warm = self._warm_mic_stream
        if warm is not None and not warm.close_if_idle():
            self._pending_audio_device_refresh = True
            return
        refreshed = audio_devices.try_refresh_input_devices(self._logger)
        if not refreshed:
            # Something registered a stream between the close and the
            # re-enumeration -- the gap the guard hold used to cover. A warm
            # open (a settings save's retry landing in that gap; a restart
            # helper's reopen is refused by the generation bump) is closed
            # by one more round. A recording's stream is left alone and the
            # refresh deferred to its stop, as before: `close_if_idle`
            # refuses while one is attached, and a cold capture is not the
            # warm stream's to close. Not gated on `is_running`: a stream is
            # registered while `_stream` is already None in exactly the
            # states `close_if_idle` waits for -- a helper closing it, an
            # open in flight, a superseded open's retirement -- and the
            # gate skipped the round for those (measured: a save's reopen
            # handed to a restart helper, one refused round, the refresh
            # deferred to the next recording stop).
            warm = self._warm_mic_stream
            if warm is not None and warm.close_if_idle():
                refreshed = audio_devices.try_refresh_input_devices(self._logger)
        if not refreshed:
            self._pending_audio_device_refresh = True
        if self._shutdown_started:
            return
        if refreshed:
            self.audio_devices_refreshed.emit()
        warm = self._warm_mic_stream
        if warm is not None:
            warm.ensure_started()

    def _maybe_resume_pending_audio_device_refresh(self) -> None:
        if self._pending_audio_device_refresh and not self._shutdown_started:
            self._pending_audio_device_refresh = False
            self._audio_device_change_timer.start()

    def stop_recording(self) -> None:
        if self._recording_stop_in_progress:
            return
        capture = self._audio_capture
        if capture is None:
            return

        self._recording_stop_in_progress = True
        waits = False
        try:
            # Bring the (possibly floating/hidden) overlay forward the moment the
            # hotkey stop is pressed, so the new state (Processing / Finalizing,
            # or an error) is visible immediately instead of only after the
            # transcript finishes. This reuses the same non-activating reveal as
            # recording start, so focus stays on the target window and the
            # pending insertion is unaffected.
            self._overlay.reveal_temporarily()
            self._cancel_audio_callback_watchdog(capture)
            self._audio_capture = None
            warm_stream, callback_count = self._audio_capture_runtime_context(capture)
            # Review F4: cleared above, `_audio_capture` made
            # `_on_stream_audio_chunk` drop every block the stop then waited
            # for. Only the PortAudio thread runs while this waits.
            self._stopping_capture = capture
            # Review round 2 P2: the wait holds the Qt thread for seconds,
            # through which the overlay kept saying "Speak now". Painted
            # directly: `processEvents` would deliver queued signals (a
            # stream abort, a finished job) into the middle of this stop.
            waits = capture.backlog_wait_expected()
            if waits:
                self._logger.info("audio_capture_stop_waits_for_backlog")
                self._overlay.set_state(
                    "Processing", "Collecting the microphone's delayed audio..."
                )
                self._overlay.paint_now()
            try:
                wav_bytes = capture.stop()
            except Exception as exc:
                self._logger.exception(
                    "Audio capture failed to stop mode=%s warm_stream=%s "
                    "callback_count=%d",
                    self._active_session_mode,
                    warm_stream,
                    callback_count,
                )
                self._active_batch_settings = None
                detail = f"Failed to stop microphone capture: {exc}"
                if self._streaming_recording:
                    self._abort_streaming_session(
                        detail,
                        beep=False,
                        finalize_stream=False,
                    )
                else:
                    self._overlay.set_state("Error", detail)
                return
            finally:
                self._stopping_capture = None
            persisted = self._persist_last_recording_audio(wav_bytes)
            source_audio_path = self._save_recording_artifacts(capture, wav_bytes)
            # The job's recording is the one this persist wrote, under the
            # id the write handed back. Registered from the store's slot, a
            # job whose write was refused -- a full disk, a locked file --
            # carried the previous recording's id: the finalize marked that
            # recording transcribing, and its retry's success completed,
            # with `save_last_wav` off deleted, a recording the user never
            # asked about (the wave-16 concurrency lens, on the real store).
            # "" says the store never received these bytes, and such a job
            # marks nothing. And the slot is not re-read for the id: a read
            # refused between the write and the registration -- a scanner
            # holding the freshly written state file -- answered "" while
            # the job kept marking, unkeyed, whatever the slot held when it
            # ended; demoted behind a newer recording, its completion
            # deleted that recording's audio and state (the wave-18 reach
            # lens, on the real store).
            source_recording_id = self._last_persisted_recording_id if persisted else ""

            if self._streaming_recording:
                self._focus_poll_timer.stop()
                if self._stream_abort_requested:
                    self._abort_streaming_session(
                        "Streaming aborted.",
                        beep=False,
                        finalize_stream=False,
                    )
                    return
                if not wav_bytes and not self._current_streaming_partial_text().strip():
                    # The microphone delivered nothing by the stop -- a
                    # starved callback thread whose backlog did not come in
                    # time. Finalized, the empty stream ended as "No speech
                    # detected", a success that hid the loss of everything
                    # said; batch reports the same case as an error. (The
                    # capture records every block it streams, so no bytes
                    # means nothing was streamed; the partial check only
                    # keeps a stream that did show text on its own road.)
                    self._logger.error(
                        "audio_capture_empty mode=streaming warm_stream=%s "
                        "callback_count=%d",
                        warm_stream,
                        callback_count,
                    )
                    self._abort_streaming_session(
                        "No audio captured: the microphone delivered nothing "
                        "before the stop.",
                        beep=False,
                        finalize_stream=False,
                    )
                    return
                self._overlay.set_state(
                    "Processing", "Finalizing streaming transcript..."
                )
                self._submit_stream_finalize(
                    source_audio_path=source_audio_path,
                    wav_bytes=wav_bytes,
                    source_recording_id=source_recording_id,
                )
                return

            if not wav_bytes:
                self._logger.error(
                    "audio_capture_empty mode=%s warm_stream=%s callback_count=%d",
                    self._active_session_mode,
                    warm_stream,
                    callback_count,
                )
                self._overlay.set_state("Error", "No audio captured.")
                self._active_batch_settings = None
                return

            if self._silence_gate_blocks(wav_bytes, persisted=persisted):
                self._active_batch_settings = None
                return

            settings_snapshot = self._active_batch_settings or replace(self._settings)
            self._active_batch_settings = None
            if self._matching_model_preload_running(settings_snapshot):
                self._overlay.set_state(
                    "Processing",
                    f"Waiting for selected model '{settings_snapshot.model_size}' "
                    "to finish loading, then transcribing audio...",
                )
            else:
                self._overlay.set_state("Processing", "Transcribing audio...")
            self._submit_batch_transcription(
                wav_bytes,
                settings_snapshot,
                source_audio_path=source_audio_path,
                source_recording_id=source_recording_id,
            )
        finally:
            pending_toggles = self._pending_toggle_after_stop_count
            self._pending_toggle_after_stop_count = 0
            self._recording_stop_in_progress = False
            self._flush_deferred_background_results()
            self._maybe_resume_pending_audio_device_refresh()
            if waits:
                # Taken when the whole stop has returned, not when the wait
                # did: the persist, the artifacts, the silence scan and the
                # submit after it hold the Qt thread too (review round 3 F3).
                self._stop_wait_ended_ms = message_clock_ms()
            if pending_toggles % 2 == 1 and self._audio_capture is None:
                self._logger.info(
                    "Applying queued hotkey start after recording stop completed."
                )
                QtCore.QTimer.singleShot(0, self.start_recording)

    def _silence_gate_blocks(self, wav_bytes: bytes, *, persisted: bool) -> bool:
        """Skip transcription when the recording never rises above silence.

        Speech models hallucinate words from pure silence, so an opt-in gate
        checks the loudest 100 ms window of the recording against a
        user-tunable threshold (kept low so whispering still passes). The
        measured level is always logged so the threshold is easy to tune, and
        a gated recording stays available as the last recording for a manual
        retry via History -> Use last recording -- when `persisted` says the
        stop road's write happened. The canceled mark is keyed by the id that
        write handed back and skipped otherwise: the slot then holds the
        previous recording, which the unkeyed mark relabelled canceled with
        this gate's text, and the text called audio the store never received
        kept (the wave-17 reach lens, on the real store).

        A recording loud enough for the level gate is also scored by the
        Silero speech model (`silero_vad`, thresholds and their measurements
        in `config.py`), because typing, a knock or room tone just above the
        gate are loud and are still not speech. It is skipped only when the
        gate is on and `SpeechCheck.no_speech` holds: a COMPLETE scan as
        recorded and a complete scan of the copy amplified by
        `SILERO_BATCH_QUIET_SPEECH_GAIN` both stayed below
        `SILERO_BATCH_MIN_PROBABILITY`. The second scan is what keeps quiet
        speech that a keystroke lifted over the level gate: Silero scores by
        level, and such speech scored as low as 0.025 as recorded. A scan the
        budget cut short, a measurement that failed and a graph that could not
        be loaded all leave the level gate's answer standing. The check runs
        only when its answer can skip -- the gate on and the level gate passed
        -- because it costs a scan on the Qt thread; every stop's
        `recording_peak_level` line says `silero_speech_seconds=not_run`
        otherwise, `loading` while the background load has not finished, and
        the measurement when it ran.
        """
        enabled = bool(getattr(self._settings, "silence_gate_enabled", False))
        try:
            peak_level = measure_peak_windowed_rms(wav_bytes)
        except Exception:
            self._logger.exception("Failed to measure recording peak level")
            return False
        if peak_level is None:
            # Unreadable audio is not silence: let it through so the failure
            # surfaces instead of the recording quietly disappearing.
            self._logger.warning(
                "recording_peak_level unmeasurable bytes=%d", len(wav_bytes or b"")
            )
            return False
        threshold = float(
            getattr(
                self._settings,
                "silence_gate_threshold",
                DEFAULT_SILENCE_GATE_THRESHOLD,
            )
        )
        speech: silero_vad.SpeechCheck | None = None
        speech_note = "not_run"
        if enabled and peak_level >= threshold:
            speech, speech_note = self._measure_speech_for_silence_gate(wav_bytes)
        self._logger.info(
            "recording_peak_level level=%.4f silence_gate_enabled=%s threshold=%.4f %s",
            peak_level,
            enabled,
            threshold,
            self._speech_measurement_log_fields(speech, speech_note),
        )
        if not enabled:
            return False
        kept = (
            "the recording is kept"
            if persisted
            else "the recording could not be kept as the last recording (see the log)"
        )
        if peak_level < threshold:
            reason = "level"
            store_text = "Recording skipped by the silence gate."
            detail = (
                f"No speech detected (loudest 100 ms {peak_level:.4f}, gate "
                f"{threshold:.4f}). Nothing was transcribed; {kept}. If this "
                "was speech, lower the silence gate in Settings -> Audio."
            )
        elif speech is not None and speech.no_speech:
            scored = max(
                speech.natural.max_probability,
                speech.amplified.max_probability if speech.amplified else 0.0,
            )
            reason = "speech_check"
            store_text = "Recording skipped by the speech check: no speech found."
            detail = (
                "No speech detected (the speech check scored it at most "
                f"{scored:.2f}, speech needs "
                f"{speech.min_probability:.2f}; loudest 100 ms "
                f"{peak_level:.4f}). Nothing was transcribed; {kept}. If this "
                "was speech, switch the silence gate off in Settings -> Audio."
            )
        else:
            return False
        self._logger.info(
            "silence_gate_skipped reason=%s level=%.4f threshold=%.4f "
            "silero_max_probability=%s",
            reason,
            peak_level,
            threshold,
            "unavailable"
            if speech is None
            else f"{speech.natural.max_probability:.3f}",
        )
        if persisted:
            try:
                self._last_recording_store.mark_canceled(
                    store_text,
                    expected_recording_id=self._last_persisted_recording_id or None,
                )
            except Exception:
                self._logger.exception("Failed to mark silence-gated recording")
        self._overlay.set_state("Done", detail)
        return True

    def _warm_up_speech_check(self) -> None:
        """Load the Silero session on a daemon thread while the gate is on.

        Building it imports ONNX Runtime and loads the graph -- 123-275 ms
        cold -- which must not happen inside a stop on the Qt thread. Called
        at start and on every settings reload; idempotent.
        """
        if bool(getattr(self._settings, "silence_gate_enabled", False)):
            silero_vad.start_loading()

    def _measure_speech_for_silence_gate(
        self, wav_bytes: bytes
    ) -> tuple[silero_vad.SpeechCheck | None, str]:
        """Silero's view of a stopped recording, and a log note when it has none.

        Bounded (`SILERO_BATCH_STOP_AFTER_SPEECH_S`, `SILERO_BATCH_MAX_SCAN_S`,
        both scans) because it runs on the Qt thread. A session not yet
        loaded is never built here: the stop answers "loading" -- unmeasured,
        which never skips -- and asks for the background load. Anything the
        scan raises is logged and read as unmeasured too.
        """
        if silero_vad.loaded_session() is None:
            silero_vad.start_loading()
            if silero_vad.load_failed_recently():
                return None, "unavailable"
            return None, "loading"
        try:
            checked = silero_vad.check_speech_wav(
                wav_bytes,
                min_probability=SILERO_BATCH_MIN_PROBABILITY,
                gain=SILERO_BATCH_QUIET_SPEECH_GAIN,
                stop_after_speech_s=SILERO_BATCH_STOP_AFTER_SPEECH_S,
                max_scan_s=SILERO_BATCH_MAX_SCAN_S,
            )
        except Exception:
            self._logger.exception("Failed to measure speech in the recording")
            return None, "unavailable"
        return checked, "" if checked is not None else "unavailable"

    @staticmethod
    def _speech_measurement_log_fields(
        speech: silero_vad.SpeechCheck | None, note: str = "unavailable"
    ) -> str:
        """The speech-check half of the `recording_peak_level` line.

        The fields describe the scan as recorded; `complete=False` marks one
        that stopped early -- on enough speech or on the budget -- so its
        speech seconds are a lower bound. `silero_amplified_max_probability`
        is `not_run` whenever the first scan settled the answer, and
        `unavailable` when the amplified scan was needed and failed. Without
        a measurement the line carries `note` instead: `not_run`, `loading`
        or `unavailable`.
        """
        if speech is None:
            return f"silero_speech_seconds={note}"
        natural = speech.natural
        if speech.amplified is not None:
            amplified = f"{speech.amplified.max_probability:.3f}"
        elif natural.complete and natural.max_probability < speech.min_probability:
            amplified = "unavailable"
        else:
            amplified = "not_run"
        return (
            f"silero_speech_seconds={natural.speech_seconds:.2f} "
            f"silero_max_probability={natural.max_probability:.3f} "
            f"silero_amplified_max_probability={amplified} "
            f"silero_scanned_s={natural.scanned_seconds:.1f}/"
            f"{natural.total_seconds:.1f} silero_complete={natural.complete}"
        )

    def _auto_stop_from_vad(self) -> None:
        """Voice-activity detection ended the recording on its own.

        Logged because it was not: a VAD stop looked exactly like a hotkey
        stop in the log, so a recording that ended by itself was
        indistinguishable from a bug. The user cannot tell either -- the
        overlay just moves on to Processing -- so the log is the only place
        this can be explained after the fact.
        """
        self._logger.info(
            "recording_auto_stopped_by_vad vad_enabled=%s energy_threshold=%s "
            "(no hotkey press; voice activity detection ended the recording)",
            getattr(self._settings, "vad_enabled", "n/a"),
            getattr(self._settings, "vad_energy_threshold", "n/a"),
        )
        self.vad_auto_stop_requested.emit()

    def _play_start_beep(self) -> None:
        if not self._settings.start_beep_enabled:
            return
        tone = (
            (self._settings.start_beep_tone or DEFAULT_START_BEEP_TONE).strip().lower()
        )
        if tone not in VALID_START_BEEP_TONES:
            tone = DEFAULT_START_BEEP_TONE
        # Deliberately synchronous: the recording-start path plays the tone
        # before opening the capture so the microphone cannot record it.
        self._play_tone(tone)

    def _play_completion_beep(self) -> None:
        """Play the completion tone after a successful transcript insertion.

        Runs on a short-lived worker thread because winsound.Beep is
        synchronous: blocking the Qt thread for 50-150 ms after every insert
        would be pure latency. Unlike the start tone it is not timed around
        a capture; a paste with a target check plays it only when the
        verdict arrives, and skips it if a new recording started by then
        (`_recording_started_since`).
        """
        if not getattr(self._settings, "completion_beep_enabled", False):
            return
        tone = (
            str(
                getattr(
                    self._settings,
                    "completion_beep_tone",
                    DEFAULT_COMPLETION_BEEP_TONE,
                )
                or DEFAULT_COMPLETION_BEEP_TONE
            )
            .strip()
            .lower()
        )
        if tone not in VALID_START_BEEP_TONES:
            tone = DEFAULT_COMPLETION_BEEP_TONE
        try:
            threading.Thread(
                target=self._play_tone,
                args=(tone,),
                name="stt_app_completion_beep",
                daemon=True,
            ).start()
        except Exception as exc:
            # A starved interpreter cannot start another thread (`RuntimeError`,
            # or `MemoryError` when the stack cannot be allocated). The tone
            # is a courtesy after a paste that already landed; raised out
            # of the deferred flush's success arm, this reported the pasted
            # transcript as not inserted and armed Insert, which pasted it
            # a second time.
            self._logger.warning("Completion tone skipped: %s", exc)

    @staticmethod
    def _play_tone(tone: str) -> None:
        try:
            import winsound  # type: ignore
        except ImportError:
            winsound = None

        if winsound is None:
            try:
                QtGui.QGuiApplication.beep()
            except Exception:
                pass
            return

        try:
            if tone == "high":
                winsound.Beep(1300, 80)
                return
            if tone == "chime":
                winsound.Beep(880, 55)
                winsound.Beep(1170, 70)
                return
            if tone == "system":
                winsound.MessageBeep(winsound.MB_OK)
                return
            winsound.Beep(980, 70)
        except Exception:
            try:
                QtGui.QGuiApplication.beep()
            except Exception:
                pass

    def _resolve_recordings_dir(self) -> str:
        return str(resolve_recordings_dir(self._settings.recordings_dir))

    def _selectable_last_recording_path(self) -> Path | None:
        archived_dir = (
            self._resolve_recordings_dir()
            if self._settings.save_all_recordings
            else None
        )
        return self._last_recording_store.selectable_path(archived_dir)

    def _persist_last_recording_audio(self, wav_bytes: bytes) -> bool:
        self._last_persisted_recording_id = ""
        if not wav_bytes:
            return False
        try:
            state = self._last_recording_store.save_recording(
                wav_bytes,
                keep_after_success=self._settings.save_last_wav,
            )
            self._last_persisted_recording_id = str(
                getattr(state, "recording_id", "") or ""
            ).strip()
            return True
        except Exception:
            self._logger.exception("Failed to persist last recording audio")
            return False

    def _save_recording_artifacts(
        self,
        capture: AudioCapture,
        wav_bytes: bytes,
    ) -> str:
        if not wav_bytes:
            return ""

        if not self._settings.save_all_recordings:
            return ""

        try:
            root = self._resolve_recordings_dir()
            target_dir = os.path.abspath(root)
            os.makedirs(target_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # noqa: DTZ005 (local time on purpose: this names a file the user browses)
            path = os.path.join(target_dir, f"recording_{stamp}.wav")
            capture.save_wav(Path(path), wav_bytes)
            self._prune_recordings(target_dir, self._settings.recordings_max_count)
            return path
        except Exception:
            self._logger.exception("Failed to archive recording")
            return ""

    def _prune_recordings(self, directory: str, keep_count: int) -> None:
        keep = int(keep_count or RECORDINGS_MAX_COUNT_UNLIMITED)
        if keep <= RECORDINGS_MAX_COUNT_UNLIMITED:
            # 0 is "keep every recording", so nothing is deleted at all. A
            # missing or negative count lands here too: erring towards keeping
            # the user's audio beats deleting all but the newest file.
            return
        try:
            names = os.listdir(directory)
        except OSError:
            return
        aged = []
        for name in names:
            if not _ARCHIVED_RECORDING_NAME_RE.fullmatch(name):
                continue
            path = os.path.join(directory, name)
            try:
                aged.append((os.path.getmtime(path), path))
            except OSError:
                # Deleted since the listing (another prune, the user): one
                # file fewer to keep count of, not a reason to stop.
                continue
        aged.sort()
        files = [path for _mtime, path in aged]
        while len(files) > keep:
            oldest = files.pop(0)
            try:
                os.remove(oldest)
            except OSError:
                break

    def _reset_streaming_state(self, *, keep_session_text: bool = False) -> None:
        """Clear the streaming session. `keep_session_text` spares two fields.

        Everything else here is about a runtime that is going away -- retiring
        the handshake, stopping the focus poll, dropping preconnect audio --
        and must run either way. The exception is a finalize that is still in
        flight: it will be measured against `committed_text` and delivered
        through the branch `_active_session_mode` selects, so wiping those two
        while it runs is what turned a dying socket into either a lost
        dictation or one pasted a second time on top of the live-inserted text.
        """
        self._focus_poll_timer.stop()
        # Retire any handshake still in flight. Bumping the generation is what
        # stops a late flush from pushing this session's audio into the next
        # one, and stops its completion signal from touching the overlay.
        # This is the only retirement on the normal road: the stop hands the
        # handshake to the finalize worker and leaves it running, so that the
        # buffered audio still reaches the provider before `stop_stream()`.
        self._stream_connect_generation += 1
        self._stream_finalize_pending = False
        # The session is over, so its identity stops matching: a provider that
        # fires `on_error` from here on is answering for a session nobody owns.
        self._stream_session_token = None
        with self._stream_preconnect_lock:
            self._stream_preconnect_chunks = None
            self._stream_preconnect_dropped = False
            # Not the recorded handshake failures: each belongs to the
            # finalize that joined its handshake, which may still be parked
            # in that join while this reset runs from a cancel.
        self._stream_abort_requested = False
        self._stream_insertion_suspended = False
        self._stream_insert_held = False
        self._stream_insert_failures = 0
        if not keep_session_text:
            self._stream_text_state.reset()
            self._active_session_mode = "batch"
        self._active_batch_settings = None
        self._streaming_recording = False
        self._target_window_handle = None
        self._target_focus_signature = None
        self._stream_waits_for_paste_pace = False
        if keep_session_text:
            finalize = self._pending_streaming_job()
            self._stream_kept_finalize_token = (
                finalize.token if finalize is not None else None
            )
        self._release_stream_held_repaste()

    def _stream_still_delivering(self) -> bool:
        """Whether a streaming dictation may still paste into its window:
        the stream itself, or the finalize its runtime failure left in
        flight, whose tail is still to come."""
        if self._streaming_recording:
            return True
        token = self._stream_kept_finalize_token
        job = self._jobs.get(token) if token is not None else None
        return job is not None and not job.aborting

    def _release_stream_held_repaste(self) -> None:
        """Let a re-paste held for a stream go once the stream is over.

        The pace timer runs it on the next turn of the event loop, after
        whatever the stream's end still paints and pastes, and after the
        restore window of the stream's own last paste. Called when the
        streaming state is reset and when a job ends, since a failure's
        pending finalize ends later than the reset.
        """
        pending = self._pending_repaste
        if (
            pending is not None
            and pending.after_stream
            and not self._stream_still_delivering()
            and not self._paste_pace_timer.isActive()
        ):
            self._paste_pace_timer.start(_pace_ms(self._paste_pace_wait_s()))

    @property
    def _stream_committed_text(self) -> str:
        return self._stream_text_state.committed_text

    @_stream_committed_text.setter
    def _stream_committed_text(self, value: str) -> None:
        self._stream_text_state.committed_text = str(value or "")

    @property
    def _stream_live_text(self) -> str:
        return self._stream_text_state.live_text

    @_stream_live_text.setter
    def _stream_live_text(self, value: str) -> None:
        self._stream_text_state.live_text = str(value or "")

    @property
    def _stream_last_partial_text(self) -> str:
        return self._stream_text_state.last_partial_text

    @_stream_last_partial_text.setter
    def _stream_last_partial_text(self, value: str) -> None:
        self._stream_text_state.last_partial_text = str(value or "")

    def _transcription_runtime_active(self) -> bool:
        """Whether the cached transcriber runtime is in use by a live session.

        True while a recording capture, an in-progress recording start, an
        active stream, or an in-flight transcription still holds the cached
        transcriber. Callers use this to avoid closing that runtime out from
        under an active worker/stream.
        """
        return (
            self._audio_capture is not None
            or self._recording_start_in_progress
            or self._streaming_recording
            or self._transcriber_runtime_in_use.is_set()
        )

    def _reset_transcriber_cache(self) -> None:
        """Close the cache now when idle, otherwise defer until lease release."""
        if not self._transcriber_runtime_lock.acquire(blocking=False):
            with self._transcriber_runtime_state_lock:
                self._pending_transcriber_cache_reset = True
            return
        try:
            self._reset_transcriber_cache_locked()
        finally:
            self._transcriber_runtime_lock.release()

    def _reset_transcriber_cache_locked(self) -> None:
        """Close the cache while the caller owns the runtime admission lock."""
        with self._transcriber_cache_lock:
            cached = self._transcriber_cache
            # Detached before it is closed. `_close_cached_transcriber`
            # swallows `Exception` but not `BaseException`, and with the close
            # in front a runtime that could not be closed stayed *in the
            # cache*: every later acquisition handed back the same dead object
            # and tried to close it again, so one failure made the app
            # permanently unable to transcribe instead of failing once. This
            # is also the only eviction a replaced API key gets -- the cache
            # key is unchanged there -- so a runtime holding a revoked
            # credential went on serving requests.
            self._transcriber_cache = None
            self._transcriber_cache_key = None
            self._close_cached_transcriber(cached)
        with self._transcriber_runtime_state_lock:
            self._pending_transcriber_cache_reset = False

    def _acquire_transcriber_runtime(
        self,
        settings: AppSettings,
        *,
        allow_isolated: bool = True,
    ) -> _TranscriberRuntimeLease:
        """Lease the shared cache or build an isolated overlapping runtime.

        Waiting for the shared cache on a normal request would freeze the Qt
        thread when a new stream starts while an older batch job is finishing.
        Such overlapping work receives a close-on-release runtime. Preload
        workers opt out and wait off-thread so a successful preload remains in
        the shared cache.
        """
        owns_shared_lock = self._transcriber_runtime_lock.acquire(
            blocking=not allow_isolated
        )
        if owns_shared_lock:
            if self._shutdown_started:
                self._transcriber_runtime_lock.release()
                raise TranscriptionCanceled("Application shutdown is in progress.")
            # The guard spans every statement between taking the lock and
            # handing it to a lease, not just the load. The increment used to
            # sit above the `try` and the lease construction below it, so a
            # raise in either -- `_increment_transcriber_runtime_count` takes
            # its own lock, and the lease is a constructor -- stranded exactly
            # what the guard exists to protect.
            incremented = False
            try:
                self._increment_transcriber_runtime_count()
                incremented = True
                with self._transcriber_runtime_state_lock:
                    reset_pending = self._pending_transcriber_cache_reset
                if reset_pending:
                    self._reset_transcriber_cache_locked()
                transcriber = self._get_or_create_transcriber(settings)
                close_on_release = (
                    settings.engine == DEFAULT_ENGINE
                    and settings.model_size in LOCAL_WEBGPU_MODEL_SIZES
                    and not bool(getattr(settings, "keep_onnx_model_loaded", False))
                )
                return _TranscriberRuntimeLease(
                    self,
                    transcriber,
                    owns_shared_lock=True,
                    close_on_release=close_on_release,
                )
            except BaseException:
                # BaseException, not Exception: this arm only undoes its own
                # bookkeeping and re-raises, so nothing is swallowed, and
                # missing one strands `_transcriber_runtime_lock` for the
                # process lifetime -- every later preload and import blocks
                # forever and `_transcription_runtime_active()` stays True.
                # `finally`, like the isolated arm below: the decrement is
                # the riskier of the two and it came first, so a raise from it
                # stranded the admission lock for the process lifetime -- the
                # outcome this arm exists to make impossible.
                try:
                    if incremented:
                        self._decrement_transcriber_runtime_count()
                finally:
                    self._transcriber_runtime_lock.release()
                raise

        if self._shutdown_started:
            raise TranscriptionCanceled("Application shutdown is in progress.")
        incremented = False
        # An isolated runtime nobody else can reach: unlike the shared arm
        # above, whose transcriber stays in the cache and is still valid for
        # the next caller, this one is owned by the lease that is about to be
        # built. If that construction raises, nothing else holds a reference,
        # so a Node child process or an ONNX session would stay alive with no
        # way to close it. Cleared as soon as an owner exists.
        orphan = None
        try:
            self._increment_transcriber_runtime_count()
            incremented = True
            transcriber = create_transcriber(settings, secret_store=self._secret_store)
            orphan = transcriber
            if self._shutdown_started:
                # Cleared *before* the close, not after. `_close_cached_transcriber`
                # swallows `Exception` but not `BaseException`, so with the clear
                # below it the except arm saw `orphan` still set and closed the
                # same runtime a second time. What that costs is the double close
                # itself and the original failure, which the second close's
                # exception replaced on its way out. It does not cost the
                # `TranscriptionCanceled` below: a close that raises skips that
                # `raise` either way, so the caller sees a close error in both
                # shapes -- with this clear it sees the first one.
                orphan = None
                self._close_cached_transcriber(transcriber)
                raise TranscriptionCanceled("Application shutdown is in progress.")
            lease = _TranscriberRuntimeLease(
                self,
                transcriber,
                owns_shared_lock=False,
                close_on_release=True,
            )
            orphan = None
            return lease
        except BaseException:
            # See the shared-lock arm above: cleanup-and-re-raise, so the
            # broader catch cannot hide anything. No lock to give back here --
            # this arm never took one -- but the runtime count still gates
            # `_transcription_runtime_active()`, which blocks every deferred
            # cache reset while it reads True. The close is in its own `try`
            # so that a runtime which fails to close cannot also skip the
            # decrement and strand that gate.
            try:
                if orphan is not None:
                    self._close_cached_transcriber(orphan)
            finally:
                if incremented:
                    self._decrement_transcriber_runtime_count()
            raise

    def _increment_transcriber_runtime_count(self) -> None:
        with self._transcriber_runtime_state_lock:
            self._transcriber_runtime_active_count += 1
            self._transcriber_runtime_in_use.set()

    def _decrement_transcriber_runtime_count(self) -> None:
        with self._transcriber_runtime_state_lock:
            self._transcriber_runtime_active_count = max(
                0,
                self._transcriber_runtime_active_count - 1,
            )
            if self._transcriber_runtime_active_count == 0:
                self._transcriber_runtime_in_use.clear()

    def _release_transcriber_runtime(self, *, owns_shared_lock: bool) -> None:
        """Release a runtime lease and apply resets deferred behind the cache."""
        try:
            if owns_shared_lock:
                with self._transcriber_runtime_state_lock:
                    reset_pending = self._pending_transcriber_cache_reset
                if reset_pending or self._shutdown_started:
                    self._reset_transcriber_cache_locked()
        finally:
            try:
                if owns_shared_lock:
                    self._transcriber_runtime_lock.release()
            finally:
                # Nested, so a failing lock release cannot also skip the
                # decrement: the count gates `_transcription_runtime_active()`,
                # which blocks every deferred cache reset while it reads True.
                self._decrement_transcriber_runtime_count()
        if owns_shared_lock:
            # A reset requester can set the pending flag after the pre-release
            # check but before the admission lock is dropped. Recheck through
            # the normal non-blocking path so shutdown cannot strand the cache.
            with self._transcriber_runtime_state_lock:
                reset_pending = self._pending_transcriber_cache_reset
            if reset_pending or self._shutdown_started:
                self._reset_transcriber_cache()

    def _reset_resume_sensitive_transcriber_cache(self) -> None:
        if self._transcription_runtime_active():
            self._logger.info(
                "System resume detected during an active session; keeping "
                "current transcriber runtime."
            )
            return

        if not self._transcriber_runtime_lock.acquire(blocking=False):
            self._logger.info(
                "System resume detected during an active shared runtime; keeping "
                "the current transcriber cache."
            )
            return
        try:
            with self._transcriber_cache_lock:
                cached = self._transcriber_cache
                cache_key = self._transcriber_cache_key
                cached_model = str(getattr(cached, "model_size", "") or "")
                cached_device = str(getattr(cached, "runtime_device", "") or "")
                # By name: an inserted field would silently move a positional
                # read onto the wrong value and stop this teardown from firing.
                cache_model = str(getattr(cache_key, "model_size", "") or "")
                should_reset = cached is not None and (
                    cached_model in LOCAL_WEBGPU_MODEL_SIZES
                    or cache_model in LOCAL_WEBGPU_MODEL_SIZES
                )
                if not should_reset:
                    return
                self._logger.info(
                    "System resume detected; closing cached ONNX/WebGPU runtime "
                    "model=%s device=%s so GPU backends are recreated.",
                    cached_model or cache_model,
                    cached_device or "unknown",
                )
                # Detached first, for the reason in
                # `_reset_transcriber_cache_locked`: a close that raises must
                # not leave the dead runtime in the cache.
                self._transcriber_cache = None
                self._transcriber_cache_key = None
                self._close_cached_transcriber(cached)
        finally:
            self._transcriber_runtime_lock.release()

    def handle_system_resume(self) -> None:
        """Refresh Windows integrations and drop GPU runtimes after resume.

        The three steps are independent, so each is reported on its own: with
        them in one unguarded sequence a failing cache reset also cost the
        warm-microphone restart, and the next recording then attached to a
        stream opened against a device that no longer exists.
        """
        try:
            self.refresh_hotkey_registration()
        except BaseException:
            self._logger.exception("Failed to refresh hotkeys after resume")
        try:
            self._reset_resume_sensitive_transcriber_cache()
        except BaseException:
            self._logger.exception("Failed to reset the runtime cache after resume")
        # Audio devices commonly change identity across suspend; reopen the
        # warm stream so the next recording does not attach to a dead one.
        try:
            self._restart_warm_microphone_stream_after_resume()
        except BaseException:
            self._logger.exception("Failed to restart the warm microphone after resume")

    def _close_cached_transcriber(self, transcriber) -> None:
        if transcriber is None or not hasattr(transcriber, "close"):
            return
        try:
            transcriber.close()
        except Exception:
            self._logger.exception("Failed to close cached transcriber")

    def _next_request_token(self) -> int:
        self._request_token_counter += 1
        return self._request_token_counter

    def _store_request_audio(
        self,
        request_token: int,
        wav_bytes: bytes,
        settings: AppSettings,
    ) -> None:
        self._request_audio_by_token[request_token] = (
            bytes(wav_bytes),
            replace(settings),
        )

    def _selected_model_name(self, settings: AppSettings) -> str:
        field = _ENGINE_MODEL_FIELDS.get(settings.engine)
        if field is None:
            return settings.model_size
        return str(getattr(settings, field, "") or "")

    def _current_last_recording_id(self) -> str:
        try:
            state = self._last_recording_store.load()
        except Exception:
            self._logger.exception("Failed to load last recording state")
            return ""
        if state is None:
            return ""
        return str(
            getattr(state, "recording_id", "") or getattr(state, "created_at", "")
        ).strip()

    def _append_transcript_history(
        self,
        text: str,
        settings: AppSettings,
        mode: str,
        *,
        source_recording_id: str | None = None,
        source_audio_path: str = "",
        track_for_edit: bool = True,
    ) -> TranscriptHistoryEntry | None:
        if not text.strip():
            return None
        try:
            source_id = (
                self._current_last_recording_id()
                if source_recording_id is None
                else source_recording_id
            )
            entry = TranscriptHistoryEntry.new(
                text=text,
                engine=settings.engine,
                model=self._selected_model_name(settings),
                mode=mode,
                source_recording_id=source_id,
                source_audio_path=source_audio_path,
            )
            self._history_store.add_entry(
                entry,
                settings.history_max_items,
            )
            if track_for_edit:
                self._last_history_entry = entry
            return entry
        except Exception:
            self._logger.exception("Failed to append transcript history")
            return None

    @property
    def _last_transcript(self) -> str:
        return self._shown_transcript

    @_last_transcript.setter
    def _last_transcript(self, text: str) -> None:
        # A newly shown transcript is also the newest delivered text, so a
        # background delivery recorded before it no longer decides what the
        # re-paste pastes. Every writer -- the foreground result, a rescued
        # partial, an edit, a takeover -- goes through here.
        self._shown_transcript = text
        self._delivered_after_shown = None
        self._shown_transcript_token = None
        self._shown_transcript_row = None

    def _set_last_transcript(
        self,
        text: str,
        entry: TranscriptHistoryEntry | None,
    ) -> None:
        """Move the overlay's transcript and its Edit target together.

        `_last_transcript` is what Copy and the re-paste act on and
        `_last_history_entry` is what Edit writes to; a writer that moves one
        without the other makes Edit offer one dictation's text and save it
        into another's entry. The foreground result path sets the pair
        through `_append_transcript_history(track_for_edit=True)`; every
        other writer that takes the text over goes through here, with `None`
        when the shown text has no single entry (a coalesced queue paste).
        """
        self._last_transcript = text
        self._last_history_entry = entry

    def _mark_last_recording_completed(
        self, job: _TranscriptionJob | None, text: str
    ) -> None:
        """Complete the last recording only while it is still the job's own.

        A transcript carrying a gap marker -- a part of a split remote
        recording that held sound came back empty (`transcribe_in_parts`) --
        is marked failed instead: the marker tells the user to listen to that
        stretch, and with `keep_after_success` off a completed recording is
        deleted. Failed keeps it reachable from History and Import.

        A job records the managed recording's id when it is registered. An
        older job that finishes after a newer recording was stored -- the
        newer one silence-gated, so nothing retargeted the active token --
        used to mark that newer recording completed, which with
        `keep_after_success` off deletes the audio the gate had just
        promised to keep. A job with no id -- bytes the store never
        received, or a slot it could not name -- marks nothing
        (`marks_last_recording`, derived from the id at registration since
        wave 18; an empty id used to keep the unconditional write); no job
        at all is the unconditional write.
        """
        if job is not None and not job.marks_last_recording:
            return
        if transcript_has_gap(text):
            self._mark_last_recording_failed(job, _GAP_KEPT_RECORDING_MESSAGE)
            return
        expected = job.source_recording_id if job is not None else None
        try:
            self._last_recording_store.mark_completed(
                expected_recording_id=expected or None
            )
        except Exception:
            self._logger.exception("Failed to finalize last recording state")

    def _mark_last_recording_transcribing(
        self, job: _TranscriptionJob, settings: AppSettings
    ) -> None:
        """Mark the job's own recording transcribing, keyed like every other
        mark. Unkeyed, a retry of an older failure relabelled the newest
        recording's slot -- the one its stop had just marked canceled -- as
        transcribing (the wave-14 concurrency lens)."""
        if not job.marks_last_recording:
            return
        try:
            self._last_recording_store.mark_transcribing(
                engine=settings.engine,
                model=self._selected_model_name(settings),
                mode=settings.mode,
                expected_recording_id=job.source_recording_id or None,
            )
        except Exception:
            self._logger.exception("Failed to mark last recording as transcribing")

    def _mark_last_recording_failed(
        self,
        job: _TranscriptionJob | None,
        error_text: str,
        *,
        session_recording_id: str = "",
    ) -> None:
        """Guarded like the completion mark: an older job failing after a
        newer recording was stored must not relabel that recording. No
        job is the live stream's own session, keyed by the id its persist
        handed back ("" for a store answering none keeps the unconditional
        write)."""
        if job is not None and not job.marks_last_recording:
            return
        expected = job.source_recording_id if job is not None else session_recording_id
        try:
            self._last_recording_store.mark_failed(
                error_text,
                expected_recording_id=expected or None,
            )
        except Exception:
            self._logger.exception("Failed to persist last recording failure state")

    def _promote_request_audio_for_retry(
        self, request_token: int, job: _TranscriptionJob | None
    ) -> bool:
        payload = self._request_audio_by_token.pop(request_token, None)
        if payload is None:
            return False
        wav_bytes, _settings = payload
        # The identity a retry of these bytes carries: the failed job's own
        # recording, recorded when it was registered -- never the store's
        # slot at retry time, which a newer recording may hold by then.
        self._hold_failed_audio_for_retry(
            wav_bytes, job.source_recording_id if job is not None else ""
        )
        return True

    def _hold_failed_audio_for_retry(self, wav_bytes: bytes, recording_id: str) -> None:
        """Make these bytes the Retry slot and keep the failure it replaces.

        Every writer of the slot comes through here. A failure used to
        replace the slot outright, so a queued dictation failing while another
        failure waited for Retry (or while its retry ran) took that one's only
        copy from memory. The replaced failure now waits behind the slot, up
        to `RETRY_OLDER_FAILURES_MAX`, and comes forward when the slot's own
        recording is resolved. The same failure promoted again (a failed
        retry) is the slot already and is not stacked.
        """
        held = (bytes(wav_bytes), recording_id)
        replaced = (self._last_failed_wav_bytes, self._last_failed_recording_id)
        older = [entry for entry in self._older_failed_audio if entry != held]
        if replaced[0] and replaced != held:
            older.append(replaced)
        while len(older) > RETRY_OLDER_FAILURES_MAX:
            dropped = older.pop(0)
            self._logger.warning(
                "retry_failure_dropped bytes=%d recording_id=%s: more than %d "
                "failed recordings are waiting for Retry",
                len(dropped[0]),
                dropped[1] or "n/a",
                RETRY_OLDER_FAILURES_MAX + 1,
            )
        self._older_failed_audio = older
        self._last_failed_wav_bytes, self._last_failed_recording_id = held

    def _drop_request_audio(self, request_token: int) -> None:
        self._request_audio_by_token.pop(request_token, None)

    def _retire_retry_audio_delivered_by(
        self, request_token: int, job: _TranscriptionJob | None
    ) -> None:
        """Drop a delivered job's request audio, and retire the retry slot
        only when that audio is the slot's own: the same bytes under the
        same recording id.

        The slot -- `_last_failed_wav_bytes` beside
        `_last_failed_recording_id` -- holds the most recent failure kept
        for Retry, and every foreground success used to empty it. A queued
        dictation that fails while the next one is already recorded is
        promoted there and reported with "The audio was kept -- use Retry
        to try again"; the next one's success then discarded those bytes,
        their only copy, because the store keeps one managed file and the
        newer recording's save had already replaced the older one's
        (measured on the real store; the wave-15 concurrency lens). A
        success retires the failure it resolves -- the retry of the slot's
        own bytes -- and any other recording's success leaves the slot
        alone.

        Its own is the id as well: the retry carries the slot's id (""
        included, for bytes the store never received), and bytes alone
        stood in for it, so byte-identical audio from a different
        recording retired the slot (the wave-16 reach and concurrency
        lenses). What the id cannot separate is two recordings the store
        never received carrying the same bytes: "" beside "".
        """
        payload = self._request_audio_by_token.pop(request_token, None)
        if payload is None:
            return
        wav_bytes, _settings = payload
        delivered_id = job.source_recording_id if job is not None else ""
        delivered = (wav_bytes, delivered_id)
        # A failure waiting behind the slot is resolved the same way: this
        # delivery is its retry.
        self._older_failed_audio = [
            entry for entry in self._older_failed_audio if entry != delivered
        ]
        if delivered != (self._last_failed_wav_bytes, self._last_failed_recording_id):
            return
        if not self._last_failed_wav_bytes:
            return
        # The newest of the failures behind it comes forward.
        (self._last_failed_wav_bytes, self._last_failed_recording_id) = (
            self._older_failed_audio.pop() if self._older_failed_audio else (b"", "")
        )

    # -- Transcription queue --------------------------------------------------

    def _new_recording_active(self) -> bool:
        """Whether a newer recording owns the live session.

        A pending streaming finalize keeps ``_streaming_recording`` True until
        its result is handled, so that flag must not count here; only an active
        capture or an in-progress recording start marks a queued job background.
        """
        return self._audio_capture is not None or self._recording_start_in_progress

    def _is_foreground_transcription(
        self,
        request_token: int | None,
        job: _TranscriptionJob | None = None,
    ) -> bool:
        """Whether a worker result/progress belongs to the live overlay session."""
        if request_token is None:
            return True
        if job is None:
            job = self._jobs.get(request_token)
        if (
            job is None
            and self._active_request_token is None
            and not self._new_recording_active()
        ):
            return True
        # A job a newer recording demoted to history-only stays that way even
        # when the newer recording submits nothing -- silence-gated, no audio,
        # cancelled -- and so never retargets the active token: pasting it
        # then is exactly what the user's `history` mode declined.
        return (
            self._active_request_token == request_token
            and not self._new_recording_active()
            and not (job is not None and job.aborting)
            and not (
                job is not None
                and job.background_delivery == CONCURRENT_TRANSCRIPTION_MODE_HISTORY
            )
        )

    def _register_transcription_job(
        self,
        request_token: int,
        settings: AppSettings,
        mode: str,
        *,
        source_audio_path: str = "",
        source_recording_id: str | None = None,
    ) -> _TranscriptionJob:
        """Track a submitted transcription for the queue and target insertion.

        The current target window/signature are snapshotted now so the result
        can later be inserted into the window that was focused for this
        recording, even after a newer recording reused the shared target state.

        Every road names the job's recording: the recording roads pass the
        id their persist handed back, "" when that write did not happen
        (the slot is then the previous recording's), and a retry the id of
        the recording whose bytes it resubmits. `None` resolves the store's
        slot -- no production road passes it since wave 18 -- and a slot
        that cannot be named, a read that raised or a state without an id,
        resolves to "". A job with "" marks nothing on any road: the mark
        an empty id would key is unconditional, and it relabelled or
        deleted whatever the slot held when the job ended (the wave-18
        reach lens, on the real store).
        """
        if source_recording_id is None:
            source_recording_id = self._current_last_recording_id()
        marks_last_recording = bool(source_recording_id)
        job = _TranscriptionJob(
            token=request_token,
            engine=settings.engine,
            model=self._selected_model_name(settings),
            mode=mode,
            settings=replace(settings),
            target_handle=self._target_window_handle,
            target_signature=self._target_focus_signature,
            source_recording_id=source_recording_id,
            marks_last_recording=marks_last_recording,
            source_audio_path=str(source_audio_path or "").strip(),
        )
        self._jobs[request_token] = job
        if source_recording_id:
            self._recorded_at_by_recording_id[source_recording_id] = job.created_at
        self._update_queue_overlay()
        return job

    def _finish_transcription_job(self, request_token: int | None) -> None:
        if request_token is None:
            return
        self._remove_deferred_background_result(request_token)
        job = self._jobs.pop(request_token, None)
        if job is not None:
            self._save_stashed_streaming_partial(job)
            job.insertion_deferred = False
            self._update_queue_overlay()
        if request_token == self._stream_kept_finalize_token:
            self._stream_kept_finalize_token = None
            self._release_stream_held_repaste()

    def _save_stashed_streaming_partial(self, job: _TranscriptionJob) -> None:
        """Keep a streaming dictation whose finalize produced nothing.

        Every path that delivers real text clears the stash first, so
        reaching this with one still set means the transcript exists
        nowhere else: pressing Cancel while the overlay says
        "Finalizing streaming transcript..." aborts the stream instead of
        stopping it, so the provider never returns the text either, and
        the session state that held it was cleared to unblock the next
        recording. A finished transcription is never discarded, and
        neither is a cancelled one's partial.
        """
        # No clear afterwards: the only caller has already popped the job.
        partial = (job.stashed_partial or "").strip()
        if not partial or self._shutdown_started:
            return
        self._logger.info(
            "streaming_partial_rescued token=%s chars=%d",
            job.token,
            len(partial),
        )
        entry = self._append_transcript_history(
            partial,
            job.settings,
            "streaming",
            source_recording_id=job.source_recording_id,
            source_audio_path=job.source_audio_path,
            track_for_edit=False,
        )
        # The partial becomes what Copy and the re-paste act on, so its own
        # entry becomes the Edit target with it; left behind, Edit offered
        # this text and wrote the edit into the previous dictation's entry.
        self._set_last_transcript(partial, entry)

    @staticmethod
    def _queue_job_label(
        job: _TranscriptionJob,
        *,
        rank: int,
        total: int,
    ) -> str:
        engine = (job.engine or "").strip() or "transcriber"
        model = (job.model or "").strip()
        rank_label = f"#{rank}/{total}" if total > 1 else "#1"
        if total > 1 and rank == 1:
            rank_label = f"{rank_label} Oldest"
        elif total > 1 and rank == total:
            rank_label = f"{rank_label} Newest"
        timestamp = job.created_at.strftime("%H:%M:%S")
        provider = f"{engine} · {model}" if model else engine
        # Before the provider and model: the overlay row is one elided line,
        # and with real model names a status at the end was cut off -- the one
        # word that tells a finished result waiting to be pasted from a
        # running transcription.
        status = "Pending insert · " if job.insertion_deferred else ""
        # The language the recording was made in (the job's own snapshot, not
        # the one selected now), a short code right after the time so the
        # elided row keeps it; the full name is not needed to tell two apart.
        language_mode = job.settings.language_mode
        language = (
            "Auto" if language_mode == DEFAULT_LANGUAGE_MODE else language_mode.upper()
        )
        return f"{rank_label} · {status}{timestamp} · {language} · {provider}"

    @staticmethod
    def _undelivered_row_label(entry: _UndeliveredInsert) -> str:
        """One queue row for a transcript that did not reach its window.

        Sent as `QUEUE_ROW_KIND_UNDELIVERED`, so the overlay gives the row a
        Dismiss button and its own colour; that button emits the same
        `queue_cancel_requested`, which `cancel_queued_transcription` answers
        by dismissing the row.
        """
        preview = " ".join(entry.text.split())
        if len(preview) > _UNDELIVERED_PREVIEW_CHARS:
            preview = preview[: _UNDELIVERED_PREVIEW_CHARS - 3].rstrip() + "..."
        if entry.may_have_pasted:
            status = "Possibly inserted, check the window"
        elif entry.outside_text_field:
            status = "Not in a text field"
        else:
            status = "Not inserted"
        timestamp = entry.created_at.strftime("%H:%M:%S")
        return f'{status} · {timestamp} · "{preview}"'

    def _update_queue_overlay(self) -> None:
        setter = getattr(self._overlay, "set_transcription_queue", None)
        if not callable(setter):
            return
        # A result the pace alone holds is pasted within the restore delay;
        # listed, it flashed the panel open and shut under a "Transcribing"
        # title for a paste the overlay already showed as Done.
        visible_jobs = [
            job for job in self._jobs.values() if not job.aborting and not job.pace_held
        ]
        total = len(visible_jobs)
        items = [
            (
                job.token,
                self._queue_job_label(job, rank=index, total=total),
                QUEUE_ROW_KIND_TRANSCRIPTION,
            )
            for index, job in enumerate(visible_jobs, start=1)
        ]
        items.extend(
            (
                entry.row_id,
                self._undelivered_row_label(entry),
                QUEUE_ROW_KIND_UNDELIVERED,
            )
            for entry in self._undelivered_inserts
        )
        setter(items)
        self._update_not_inserted_badge()

    def _update_not_inserted_badge(self) -> None:
        """Count what waits to be inserted on the overlay's amber badge.

        Owner's request 2026-10-09: with a long queue several pastes can
        fail, and the Insert offer of one of them is painted over by the
        next recording's Listening. The badge counts every insertable row --
        failed, "not in a text field", from the startup notice -- and names
        the re-paste hotkey while it is registered (the tray menu
        otherwise). A "possibly inserted" row is not counted: nothing pastes
        it again. It lives in the queue panel, which no state change
        touches, so it stays through the next recording.
        """
        setter = getattr(self._overlay, "set_not_inserted_badge", None)
        if not callable(setter):
            return
        count = len(self._insertable_undelivered())
        if not count:
            setter("", "")
            return
        how = self._registered_repaste_hotkey() or "tray menu"
        noun, them = (
            ("transcript was", "it") if count == 1 else ("transcripts were", "them")
        )
        setter(
            f"{count} not inserted · {how}",
            f"{count} {noun} not inserted: {self._repaste_how()} to insert "
            f"{them} at the caret, or Dismiss a row. Every transcript is in "
            "History.",
        )

    # -- Transcripts that did not reach their window --------------------------

    def _record_undelivered_insert(
        self,
        text: str,
        *,
        may_have_pasted: bool,
        created_at: datetime,
        history_entry: TranscriptHistoryEntry | None,
        outside_text_field: bool = False,
        parts: Sequence[tuple[TranscriptHistoryEntry | None, str]] = (),
    ) -> _UndeliveredInsert | None:
        """Keep a failed or doubtful paste listed until inserted or dismissed.

        ``parts`` are the results a coalesced paste joined, each with its
        entry; without them the text is one part with ``history_entry``.
        """
        transcript = text.strip()
        if not transcript or self._shutdown_started:
            return None
        self._undelivered_row_counter += 1
        entry = _UndeliveredInsert(
            row_id=-self._undelivered_row_counter,
            text=transcript,
            may_have_pasted=may_have_pasted,
            created_at=created_at,
            outside_text_field=outside_text_field,
            parts=tuple(parts) or ((history_entry, transcript),),
        )
        self._undelivered_inserts.append(entry)
        self._logger.info(
            "undelivered_insert_recorded row=%d may_have_pasted=%s "
            "outside_text_field=%s chars=%d waiting=%d",
            -self._undelivered_row_counter,
            may_have_pasted,
            outside_text_field,
            len(transcript),
            len(self._insertable_undelivered()),
        )
        self._update_queue_overlay()
        return entry

    def _insertable_undelivered(self) -> list[_UndeliveredInsert]:
        """The waiting transcripts the re-paste may paste: never one whose
        keystroke already went out."""
        return [
            entry for entry in self._undelivered_inserts if not entry.may_have_pasted
        ]

    def _repaste_rows(self) -> list[_UndeliveredInsert]:
        """The rows the re-paste hotkey pastes: the failed ones, joined.

        A "not in a text field" row only when no failed row waits. Its
        verdict can be wrong -- the text may be in the document -- so joining
        it to a failed paste pasted a text that had landed a second time (the
        2026-10-03 review). It is the newest paste's row, if listed at all:
        every later paste drops it (`_drop_superseded_doubtful_rows`).
        """
        insertable = self._insertable_undelivered()
        failed = [entry for entry in insertable if not entry.outside_text_field]
        return failed or insertable

    def _drop_superseded_doubtful_rows(
        self, keep: Sequence[_UndeliveredInsert] = ()
    ) -> None:
        """A paste went out: earlier "not in a text field" rows go.

        Whether such a paste missed cannot be settled later, and a window
        whose prompt always reads as no text field (a review probe got that
        on an Electron window 20 times of 20) would otherwise collect one row
        per dictation. The user saw the report when it was made, and the text
        stays in history. A later "text field" verdict in the same window
        is no proof the earlier element was one -- clicking into the field
        after pasting onto a button is the case the report exists for -- so
        the rule is "superseded by the next paste", not "disproved".

        A row a paced re-paste names stays: the user pressed F10 or Insert
        for it, and the queued paste the pace let go first dropped it, so the
        re-paste found nothing to paste (the 2026-10-03 second review). So
        does a row the paste in progress is built from (``keep``): when that
        paste's keystroke went out and its cleanup failed, the re-paste marks
        the row "possibly inserted" afterwards, and it must still be listed.
        """
        pending = self._pending_repaste
        requested = (
            (*pending.undelivered, *pending.offer_rows, *keep)
            if pending is not None
            else tuple(keep)
        )
        kept = [
            entry
            for entry in self._undelivered_inserts
            if not entry.outside_text_field
            or any(entry is named for named in requested)
        ]
        dropped = len(self._undelivered_inserts) - len(kept)
        if not dropped:
            return
        self._undelivered_inserts = kept
        self._logger.info("undelivered_insert_superseded rows=%d", dropped)
        self._update_queue_overlay()

    def _still_insertable(
        self, rows: Sequence[_UndeliveredInsert]
    ) -> tuple[_UndeliveredInsert, ...]:
        """Those of `rows` still listed and insertable, by identity."""
        insertable = self._insertable_undelivered()
        return tuple(row for row in rows if any(row is entry for entry in insertable))

    def _retire_undelivered(self, entries: Sequence[_UndeliveredInsert]) -> None:
        """Drop exactly the rows a successful paste was built from.

        By identity, never by text: two dictations of "okay." are two rows,
        and the overlay's Insert on one of them retired both (the 2026-10-01
        review). A "possibly inserted" row is never among them -- nothing
        pastes it -- so it stays listed until the user dismisses it.
        """
        delivered = {id(entry) for entry in entries}
        before = len(self._undelivered_inserts)
        self._undelivered_inserts = [
            entry for entry in self._undelivered_inserts if id(entry) not in delivered
        ]
        if len(self._undelivered_inserts) != before:
            self._update_queue_overlay()

    def _dismiss_undelivered(self, row_id: int | None = None) -> bool:
        """Dismiss one listed row, or every row when ``row_id`` is None."""
        before = len(self._undelivered_inserts)
        self._undelivered_inserts = [
            entry
            for entry in self._undelivered_inserts
            if row_id is not None and entry.row_id != row_id
        ]
        dismissed = before - len(self._undelivered_inserts)
        if dismissed:
            self._logger.info("undelivered_insert_dismissed rows=%d", dismissed)
            self._update_queue_overlay()
        return bool(dismissed)

    def _registered_repaste_hotkey(self) -> str:
        """The re-paste hotkey's label while it is registered, else "".

        Named only while registered: a combination another program holds
        does nothing when pressed.
        """
        hotkey = str(getattr(self._settings, "repaste_hotkey", "") or "").strip()
        return hotkey if hotkey and self._repaste_hotkey_registration_ok else ""

    def _repaste_how(self) -> str:
        """How the user inserts waiting transcripts, as a phrase."""
        how = f'choose "{TRAY_REPASTE_ACTION_LABEL}" in the tray menu'
        hotkey = self._registered_repaste_hotkey()
        return f"press {hotkey} or {how}" if hotkey else how

    def _undelivered_hint(self) -> str:
        """How many transcripts wait, and how to insert them; "" for none."""
        count = len(self._repaste_rows())
        if not count:
            return ""
        noun = "transcript is" if count == 1 else "transcripts are"
        them = "it" if count == 1 else "them"
        return (
            f"{count} {noun} waiting to be inserted: {self._repaste_how()} "
            f"to insert {them}."
        )

    def _mark_job_recording_canceled(
        self, job: _TranscriptionJob, *, foreground: bool
    ) -> None:
        """Record a job's cancel in the last-recording store, on every road.

        The cancel hotkey marked the recording canceled and the queue row's X
        did not: the job it stops is background from then on, and a
        background failure marks nothing, so the store kept "transcribing"
        for a job that had ended while the hotkey road wrote "canceled" (the
        wave-13 reach lens; the two roads differed in the state file alone,
        the recovery prompt reads the status only for "failed"). Keyed by the
        job's own recording id like the completion and failure marks, so the
        X on an older row never relabels the newest recording; a job whose id
        is unknown is marked only while it is the foreground one, which is
        what the hotkey always did. A job transcribing bytes the store never
        received marks nothing (`marks_last_recording`).
        """
        if not job.marks_last_recording:
            return
        expected = job.source_recording_id or None
        if expected is None and not foreground:
            return
        try:
            self._last_recording_store.mark_canceled(
                "Transcription canceled by user.",
                expected_recording_id=expected,
            )
        except Exception:
            self._logger.exception("Failed to mark canceled transcription")

    def _request_job_stop(self, request_token: int | None, *, delivery: str) -> None:
        """Request a real stop of an in-flight transcription.

        Sets the job's abort flag so a cooperative transcriber stops its compute
        and a not-yet-started worker skips it. The job stays registered until the
        worker resolves it: a result that still arrives is delivered per
        ``delivery`` (history-only here), and a worker that actually aborts emits
        ``transcription_canceled``. A future canceled before it starts is removed
        immediately.
        """
        if request_token is None:
            return
        job = self._jobs.get(request_token)
        if job is None:
            return
        # Read before the abort flag: a job is foreground while its token is
        # the live one and no newer recording has started.
        foreground = (
            request_token == self._active_request_token
            and not self._new_recording_active()
        )
        job.aborting = True
        job.background_delivery = delivery
        if job.insertion_deferred:
            # A finished transcript waiting to be pasted: its recording
            # completed, and a cancel mark would make that audio recoverable.
            self._remove_deferred_background_result(request_token)
            job.insertion_deferred = False
            self._finish_transcription_job(request_token)
            return
        self._mark_job_recording_canceled(job, foreground=foreground)
        if (
            request_token == self._active_request_token
            and job.mode == "streaming"
            and self._streaming_recording
        ):
            # This job is the pending streaming finalize; stopping it ends the
            # streaming session. Clear the session state so the next recording
            # is not blocked waiting on a finalize that now resolves
            # history-only in the background. Take the live transcript with
            # it: the reset below is what used to destroy the only in-app
            # copy, and the aborted stream returns none of its own.
            job.stashed_partial = (
                self._current_streaming_partial_text() or job.stashed_partial
            )
            self._active_stream_settings = None
            self._reset_streaming_state()
        if self._active_request_token == request_token:
            self._active_request_token = None
            self._last_transcribe_settings = None
        canceled_before_start = False
        future = job.future
        if future is not None:
            try:
                canceled_before_start = bool(future.cancel())
            except Exception:
                canceled_before_start = False
        if canceled_before_start:
            self._release_stream_job_runtime(job, abort=True)
            self._drop_request_audio(request_token)
            self._finish_transcription_job(request_token)
        else:
            # Hide the aborting row while the worker winds down.
            self._update_queue_overlay()

    def _remove_deferred_background_result(self, request_token: int) -> None:
        self._deferred_background_results = [
            (job, text)
            for job, text in self._deferred_background_results
            if job.token != request_token
        ]

    def cancel_queued_transcription(self, request_token: int) -> None:
        """Cancel a single queued/running transcription from the overlay.

        The compute is stopped where supported; a transcript that still finishes
        is kept in history rather than discarded. On a "Not inserted" row the
        same button dismisses the row; its transcript stays in history.
        """
        if self._dismiss_undelivered(request_token):
            return
        job = self._jobs.get(request_token)
        if job is None:
            return
        if job.pace_held:
            # Not listed, so only a row the panel showed a moment earlier
            # can ask; the overlay already shows this paste as Done.
            self._logger.info("queue_cancel_ignored_pace_held token=%d", request_token)
            return
        was_active = request_token == self._active_request_token
        self._request_job_stop(
            request_token,
            delivery=CONCURRENT_TRANSCRIPTION_MODE_HISTORY,
        )
        # Canceling a queued/foreground transcription is an explicit user action:
        # deliver every completed deferred insert now — even if another
        # transcription is still running — instead of leaving earlier finished
        # transcripts stuck pending. The flush still no-ops while a recording is
        # active (never insert mid-recording).
        reported_failure = self._flush_deferred_background_results(
            ignore_active_transcription=True
        )
        if was_active and not self._new_recording_active() and not reported_failure:
            # The foreground transcription was canceled; reflect it in the
            # main overlay area instead of leaving a stale "Processing".
            # Unless the flush above just reported a transcript that could not
            # be pasted: that Error carries the text and the Insert action, and
            # this line would replace both with three words.
            self._paint_status_keeping_offer("Done", "Transcription canceled.")

    def clear_transcription_queue(self) -> None:
        """Cancel every queued/running transcription.

        Every job is stopped before anything is delivered, which is what makes
        this different from cancelling the rows one at a time.
        `cancel_queued_transcription` flushes the deferred inserts on purpose,
        so that the X on one row does not strand the finished transcripts on
        the rows beside it -- but on the first iteration of a loop over every
        row, those rows are exactly what is about to be cancelled. Clearing a
        queue of two finished transcripts and one running job therefore pasted
        the second one into the focused window (measured: `transcript B.`),
        while the first, reached before the flush, was discarded. Stopping
        every job first removes each deferred entry with its job, so there is
        nothing left for a flush to deliver. The "Not inserted" rows go with
        the rest of the panel: their transcripts stay in history.
        """
        self._dismiss_undelivered()
        # A result only the pace holds is not in the panel being cleared: it
        # shows as Done and is pasted when the restore window ends.
        tokens = [token for token, job in self._jobs.items() if not job.pace_held]
        had_foreground = self._active_request_token in tokens
        for token in tokens:
            if token not in self._jobs:
                continue
            self._request_job_stop(
                token,
                delivery=CONCURRENT_TRANSCRIPTION_MODE_HISTORY,
            )
        if had_foreground and not self._new_recording_active():
            # The foreground transcription was canceled; reflect it in the
            # main overlay area instead of leaving a stale "Processing".
            self._paint_status_keeping_offer("Done", "Transcription canceled.")

    def _apply_concurrent_mode_to_active_job(self) -> None:
        """Apply the configured mode to the in-flight transcription when a new
        recording starts.

        The result is never discarded: ``insert`` keeps it inserting into its
        captured window, ``history`` switches it to history-only, and ``cancel``
        asks the compute to stop (a transcript that still finishes is kept in
        history).
        """
        token = self._active_request_token
        if token is None or token not in self._jobs:
            return
        mode = str(
            getattr(
                self._settings,
                "concurrent_transcription_mode",
                DEFAULT_CONCURRENT_TRANSCRIPTION_MODE,
            )
        )
        if mode == CONCURRENT_TRANSCRIPTION_MODE_HISTORY:
            self._jobs[
                token
            ].background_delivery = CONCURRENT_TRANSCRIPTION_MODE_HISTORY
        elif mode == CONCURRENT_TRANSCRIPTION_MODE_CANCEL:
            self._request_job_stop(
                token,
                delivery=CONCURRENT_TRANSCRIPTION_MODE_HISTORY,
            )

    def _submit_batch_transcription(
        self,
        wav_bytes: bytes,
        settings: AppSettings,
        *,
        source_audio_path: str = "",
        source_recording_id: str | None = None,
    ) -> None:
        request_token = self._next_request_token()
        self._active_request_token = request_token
        self._last_transcribe_settings = replace(settings)
        self._store_request_audio(request_token, wav_bytes, settings)
        job = self._register_transcription_job(
            request_token,
            settings,
            "batch",
            source_audio_path=source_audio_path,
            source_recording_id=source_recording_id,
        )
        self._logger.info(
            "transcription_submitted token=%s mode=batch engine=%s model=%s "
            "audio_bytes=%d recording_id=%s",
            request_token,
            settings.engine,
            self._selected_model_name(settings),
            len(wav_bytes),
            job.source_recording_id or "n/a",
        )
        self._mark_last_recording_transcribing(job, settings)
        try:
            job.future = self._executor.submit(
                self._transcribe_worker,
                request_token,
                wav_bytes,
                settings,
                job,
            )
        except Exception as exc:
            # Same as the streaming finalize above, minus the lease: no worker
            # means no terminal signal, so the queue row sat at "Processing"
            # for the rest of the session and the recording -- often the only
            # copy -- was never offered for a retry.
            self._logger.exception(
                "Failed to schedule the transcription. token=%s",
                request_token,
            )
            self._on_transcription_failed(
                f"The transcription could not be started: {exc}",
                request_token=request_token,
            )

    def _has_undelivered_older_job(self, request_token: int) -> bool:
        """True while a recording started before this one is still working."""
        for token, job in list(self._jobs.items()):
            if token >= request_token:
                continue
            future = job.future
            if future is None or not future.done():
                return True
        return False

    def _stream_finalize_executor_for(
        self,
        settings: AppSettings,
        *,
        request_token: int | None = None,
    ):
        """Pick the worker a stream finalize should run on.

        Local streaming re-transcribes audio with the loaded model, so it stays
        on the shared single worker that keeps model work serialized. A remote
        finalize only closes a WebSocket and returns the text the provider has
        already produced, so making it queue behind local model work is pure
        latency with nothing to protect.

        Order is the one thing that lane costs. Everything else runs on the
        single shared worker, so transcripts are delivered in the order their
        audio was recorded, and a foreground result additionally flushes the
        deferred older ones before pasting its own. Neither holds for a result
        that does not exist yet: a fast remote finalize can overtake an *older*
        job that is still transcribing — reachable by switching the engine
        between two dictations while the first one is still running — and the
        later dictation would be pasted first. While such a job exists the
        finalize joins the shared queue, which is exactly how it behaved before
        this lane was added.
        """
        if settings.engine == DEFAULT_ENGINE:
            return self._executor
        if request_token is not None and self._has_undelivered_older_job(request_token):
            return self._executor
        return self._stream_finalize_executor

    def _submit_stream_finalize(
        self,
        *,
        source_audio_path: str = "",
        wav_bytes: bytes = b"",
        source_recording_id: str | None = None,
    ) -> None:
        request_token = self._next_request_token()
        self._active_request_token = request_token
        settings = self._active_stream_settings or replace(self._settings)
        self._last_transcribe_settings = replace(settings)
        transcriber = self._active_stream_transcriber
        self._active_stream_transcriber = None
        runtime_lease = self._active_stream_runtime_lease
        self._active_stream_runtime_lease = None
        job = self._register_transcription_job(
            request_token,
            settings,
            "streaming",
            source_audio_path=source_audio_path,
            source_recording_id=source_recording_id,
        )
        job.runtime_transcriber = transcriber
        job.runtime_lease = runtime_lease
        if wav_bytes:
            # The session's audio, kept for Retry exactly as a batch job's
            # is: a finalize that fails promotes it under the job's own id.
            # With nothing registered the Error offered a Retry that
            # answered "No failed transcription to retry" while the slot
            # was emptied underneath (wave 15).
            self._store_request_audio(request_token, wav_bytes, settings)
        # Hand the in-flight handshake to the worker so it can wait for it
        # before stopping the stream.
        #
        # The generation and the preconnect buffer are deliberately left
        # alone. Retiring them here retired the session that is being
        # finalized: the connect thread's own `_flush_preconnect_buffer`
        # then failed its generation check and pushed nothing, and
        # `_finalize_stream_worker` -- which joins this very thread in
        # `_await_stream_connect` before calling `stop_stream()` -- finalized
        # a provider that had been handed no audio. Measured on the real
        # Deepgram provider: 20 buffered chunks, 0 binary frames on the
        # socket, an empty transcript, and because an empty transcript is the
        # silent-success branch, "Done / No speech detected" with the
        # recording marked completed and deleted (both retention settings
        # default to off). Retirement belongs where delivery already does it,
        # in `_reset_streaming_state`, which every terminal path runs.
        job.connect_thread = self._stream_connect_thread
        job.connect_generation = self._stream_connect_generation
        self._stream_connect_thread = None
        self._stream_finalize_pending = True
        self._logger.info(
            "transcription_submitted token=%s mode=streaming engine=%s model=%s "
            "recording_id=%s",
            request_token,
            settings.engine,
            self._selected_model_name(settings),
            job.source_recording_id or "n/a",
        )
        self._mark_last_recording_transcribing(job, settings)
        try:
            job.future = self._stream_finalize_executor_for(
                settings,
                request_token=request_token,
            ).submit(self._finalize_stream_worker, request_token, transcriber, job)
        except Exception as exc:
            # `Executor.submit` raises once the pool has been shut down and
            # again when the interpreter cannot start a worker thread, and this
            # job already owns the live stream's runtime lease. Nothing else
            # runs the worker's `finally`, so that lease is never handed back:
            # `_transcriber_runtime_lock` is held for the process lifetime,
            # every later dictation silently builds its own isolated runtime,
            # and a preload waits forever for a lease with no owner. The
            # exception also escaped into the Qt slot that pressed stop, so the
            # overlay stayed on Processing with no error and no Retry.
            self._logger.exception(
                "Failed to schedule the streaming finalize. token=%s",
                request_token,
            )
            self._release_stream_job_runtime(job, abort=True)
            self._on_transcription_failed(
                f"The transcript could not be finalized: {exc}",
                request_token=request_token,
            )
            return
        self._flush_deferred_background_results()

    def _release_stream_job_runtime(
        self,
        job: _TranscriptionJob,
        *,
        abort: bool,
    ) -> None:
        transcriber = job.runtime_transcriber
        runtime_lease = job.runtime_lease
        job.runtime_transcriber = None
        job.runtime_lease = None
        try:
            if abort and transcriber is not None:
                if hasattr(transcriber, "abort_stream"):
                    transcriber.abort_stream()
                else:
                    transcriber.stop_stream()
        except Exception:
            self._logger.exception("Failed to abort queued streaming runtime")
        finally:
            if isinstance(runtime_lease, _TranscriberRuntimeLease):
                runtime_lease.release()

    def _retry_guidance(
        self, *, has_retry_audio: bool | None = None, owns_last_recording: bool = True
    ) -> str:
        """The sentences after an error: Retry for `has_retry_audio`, and the
        last recording file only while it is the caller's own
        (`owns_last_recording`): a session that persisted nothing, or whose
        write was refused, named the previous recording as itself.
        """
        retry_available = (
            bool(self._last_failed_wav_bytes)
            if has_retry_audio is None
            else bool(has_retry_audio)
        )
        last_recording_available = (
            owns_last_recording and self._selectable_last_recording_path() is not None
        )
        if retry_available:
            parts = [
                "Captured audio is preserved in memory.",
                "Fix provider/settings if needed, then use Retry to transcribe the same recording again with the current settings.",
            ]
            if last_recording_available:
                parts.append(
                    "You can also use History -> Use last recording to transcribe the last recording file with another service."
                )
            return " ".join(parts)
        if last_recording_available:
            return (
                "This recording is still available as the last recording file. "
                "Use History -> Use last recording to transcribe it with the current settings or another service."
            )
        return "You can start a new recording and try again."

    # -- Model preloading -----------------------------------------------------

    @classmethod
    def _model_preload_key(cls, settings: AppSettings) -> _TranscriberIdentity:
        """Identity of the local runtime a preload prepares.

        Deliberately the *same* value as the transcriber cache key, not a
        parallel subset of it. While the two differed, a save that changed a
        field only the identity knew about condemned the loaded runtime and
        then skipped the preload that would rebuild it, because
        ``_local_model_preload_needed`` returns early while a preload with a
        "matching" key is running -- so the user was left on the Idle line with
        no model and no indication, and the next dictation paid a full cold
        load.
        """
        return cls._transcriber_identity(settings)

    def _set_preload_phase(self, generation: int, phase: str) -> None:
        with self._preload_result_lock:
            if generation == self._preload_generation:
                self._preload_phase = (generation, phase)

    def _preload_waits_for_another_model(
        self,
        model_name: str,
        model_dir: str,
    ) -> bool:
        """True while the preload wants the slot and another model holds it.

        The phase is already `download` at that point -- `_download_model_for
        _preload` sets it and *then* blocks inside `acquire` -- so every
        caller that renders download progress or the word "downloading" has to
        ask this as well.
        """
        if self._current_preload_phase() != _PRELOAD_PHASE_DOWNLOAD:
            return False
        return model_download_coordinator().downloading_other_model(
            model_name, model_dir
        )

    def _preload_phase_word(self) -> str:
        """What the preload is actually doing, for "the model is still ...".

        Saying "loading" while a multi-gigabyte fetch is running understates
        the wait; saying "downloading" during the load claims network activity
        for a model that is already complete on disk; and a preload still
        queued behind another one is doing neither, which is why it gets its
        own word rather than borrowing "loading".
        """
        phase = self._current_preload_phase()
        if phase == _PRELOAD_PHASE_DOWNLOAD:
            if self._preload_waits_for_another_model(
                self._preload_target_model or self._settings.model_size,
                str(getattr(self._settings, "model_dir", "") or ""),
            ):
                # Queued behind another model's download, so "downloading"
                # would claim network activity for a model nothing is
                # fetching.
                return "waiting for another model to finish"
            return "downloading"
        if phase == _PRELOAD_PHASE_QUEUED:
            return "waiting for another model to finish"
        return "loading"

    def _preload_owns_overlay(self) -> bool:
        """True while a running preload is writing the overlay's status line.

        ``show_idle_status`` would otherwise replace live download progress with
        "Idle" until the next 600 ms poll repaints it -- two content swaps and
        two window resizes for nothing.
        """
        preload = self._preload_future
        return preload is not None and not preload.done()

    def _current_preload_phase(self) -> str:
        """Phase of the preload that is running now, or "" when none is."""
        with self._preload_result_lock:
            phase = self._preload_phase
            generation = self._preload_generation
        if phase is None or phase[0] != generation:
            return ""
        return phase[1]

    def _matching_model_preload_running(self, settings: AppSettings) -> bool:
        if settings.engine != DEFAULT_ENGINE:
            return False
        key = self._model_preload_key(settings)
        with self._preload_result_lock:
            target_matches = self._preload_target_key == key
            preload = self._preload_future
        return bool(target_matches and preload is not None and not preload.done())

    def _local_model_preload_needed(self, settings: AppSettings) -> bool:
        """True unless the shared cache already holds exactly this runtime.

        A settings save that changes nothing a transcriber is built from leaves
        the preloaded model valid, so re-running the preload would close a
        loaded model and load the identical one again.
        """
        if settings.engine != DEFAULT_ENGINE:
            return False
        if self._matching_model_preload_running(settings):
            # This exact runtime is already being prepared; restarting would
            # cancel that generation and start the same load from the top.
            return False
        key = self._model_preload_key(settings)
        with self._preload_result_lock:
            result = self._preload_results.get(key)
        if result is None or result[1] is not None:
            # Never preloaded, or the last attempt for this key failed. Retry:
            # a save is exactly when the user expects a broken model to be
            # picked up again.
            return True
        with self._transcriber_runtime_state_lock:
            if self._pending_transcriber_cache_reset:
                # The runtime is still in use but already condemned, so the
                # successful preload above will not survive its release.
                return True
        with self._transcriber_cache_lock:
            cached_key = self._transcriber_cache_key
        return cached_key != self._transcriber_identity(settings)

    def _model_preload_failure(self, settings: AppSettings) -> str | None:
        if settings.engine != DEFAULT_ENGINE:
            return None
        key = self._model_preload_key(settings)
        with self._preload_result_lock:
            result = self._preload_results.get(key)
        if result is None:
            return None
        _generation, failure = result
        return failure

    def _record_model_preload_result(
        self,
        key: tuple[object, ...],
        generation: int,
        failure: str | None,
    ) -> None:
        with self._preload_result_lock:
            current = self._preload_results.get(key)
            if current is None or current[0] <= generation:
                self._preload_results[key] = (generation, failure)
            if len(self._preload_results) > 64:
                oldest_keys = sorted(
                    self._preload_results,
                    key=lambda result_key: self._preload_results[result_key][0],
                )
                for stale_key in oldest_keys[: len(self._preload_results) - 64]:
                    if stale_key != self._preload_target_key:
                        self._preload_results.pop(stale_key, None)

    def _cancel_preload_generation(self, generation: int) -> None:
        with self._preload_result_lock:
            self._preload_canceled_generations.add(generation)

    def _preload_generation_was_canceled(self, generation: int) -> bool:
        with self._preload_result_lock:
            return generation in self._preload_canceled_generations

    def _wait_for_selected_model_preload(
        self,
        settings: AppSettings,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        """Wait off the Qt thread for the exact selected local model preload.

        Batch transcription must never race a matching preload by constructing
        a second runtime, and must never silently substitute another model.
        """
        if settings.engine != DEFAULT_ENGINE:
            return
        key = self._model_preload_key(settings)
        with self._preload_result_lock:
            preload = self._preload_future if self._preload_target_key == key else None
        if preload is not None and hasattr(preload, "result"):
            try:
                while True:
                    if cancel_check is not None and cancel_check():
                        raise TranscriptionCanceled()
                    try:
                        if hasattr(preload, "done") and preload.done():
                            preload.result()
                        else:
                            preload.result(timeout=0.1)
                        break
                    except concurrent.futures.TimeoutError:
                        continue
            except concurrent.futures.CancelledError:
                pass
            except TranscriptionCanceled:
                raise
            except Exception as exc:
                self._logger.warning("Selected model preload worker failed: %s", exc)

        if cancel_check is not None and cancel_check():
            raise TranscriptionCanceled()

        failure = self._model_preload_failure(settings)
        if failure is not None:
            raise TranscriptionError(failure)

    def _start_local_model_preload(self) -> None:
        if self._settings.model_size in LOCAL_WEBGPU_MODEL_SIZES and not bool(
            getattr(self._settings, "keep_onnx_model_loaded", False)
        ):
            self._preload_progress_timer.stop()
            self._preload_target_model = None
            self._preload_future = None
            with self._preload_result_lock:
                self._preload_canceled_generations.add(self._preload_generation)
                self._preload_generation += 1
                self._preload_target_key = None
            self._preload_cancel_requested = False
            self._terminate_preload_download_process()
            self.show_idle_status()
            return

        previous = self._preload_future
        self._cancel_preload_generation(self._preload_generation)
        self._preload_cancel_requested = False
        self._terminate_preload_download_process()
        if previous is not None and not previous.done():
            try:
                previous.cancel()
            except Exception:
                pass
        settings = replace(self._settings)
        key = self._model_preload_key(settings)
        with self._preload_result_lock:
            self._preload_generation += 1
            generation = self._preload_generation
            self._preload_canceled_generations.discard(generation)
            self._preload_target_key = key
            self._preload_results[key] = (generation, None)
            # Not DOWNLOAD yet: `_preload_executor` runs one worker at a time,
            # so this one may sit queued behind an unrelated model's load for
            # minutes. Claiming the download phase there printed a frozen
            # "Downloading ... approx. 100%" for a model nothing was fetching.
            self._preload_phase = (generation, _PRELOAD_PHASE_QUEUED)
        self._paint_status_keeping_offer("Processing", "Loading selected model...")
        self._preload_target_model = settings.model_size
        try:
            from .transcriber.local_faster_whisper import estimate_cached_model_bytes

            preload_cached_bytes = estimate_cached_model_bytes(
                self._preload_target_model,
                getattr(self._settings, "model_dir", ""),
            )
        except Exception:
            preload_cached_bytes = 0
        self._preload_speed_tracker.reset(
            self._preload_target_model,
            preload_cached_bytes,
        )
        try:
            preload_future = self._preload_executor.submit(
                self._preload_model_worker,
                settings,
                generation,
                key,
            )
        except Exception as exc:
            # `Executor.submit` raises once the pool has been shut down and
            # when the interpreter cannot start a worker thread (`RuntimeError`,
            # or `MemoryError` when the stack cannot be allocated). The result
            # slot above already says "in progress" and the overlay says
            # "Loading", and no worker will ever complete this generation,
            # so letting it escape left both saying so for good. Report it
            # through the slot a failed worker uses: the failure is recorded
            # against the key, so the next dictation raises it instead of
            # substituting a model, and the next settings save retries.
            failure = f"Model preload could not be started: {exc}"
            self._logger.exception("Model preload worker could not be scheduled")
            # `previous` is still installed, and it is what
            # `_matching_model_preload_running` and `_preload_owns_overlay`
            # read. A worker that `cancel()` could not stop -- it had already
            # started -- kept both answering "running" for the *new* key, so
            # the save the user made to fix the problem found nothing to
            # retry until that stale worker finished -- for a preload worker,
            # its model load -- and the idle line stayed off the overlay for
            # as long.
            with self._preload_result_lock:
                self._preload_future = None
            self._record_model_preload_result(key, generation, failure)
            self._on_model_preload_done(generation, False, failure)
            return
        with self._preload_result_lock:
            if generation == self._preload_generation:
                self._preload_future = preload_future
        if preload_future is not None and not preload_future.done():
            self._preload_progress_timer.start()
        else:
            self._preload_progress_timer.stop()

    def _repaste_carries_offer(
        self,
        text: str,
        *,
        undelivered: Sequence[_UndeliveredInsert],
        shown_fallback: bool,
    ) -> bool:
        """Does this re-paste carry the pending offer's own dictation?

        Asked by `_repaste` before it pastes: a success retires the offer,
        and a failure after the keystroke marks it as possibly pasted
        (`_insert_offer_may_have_pasted`). A foreground or queued insert
        never carries it, so an unrelated transcript cannot mark it.

        - A paste of waiting rows (``undelivered``) carries an offer built
          from rows, by identity: every one of its rows still listed is
          among them. It never carries a row-less offer -- a streaming tail,
          or a failed re-paste of a text without a row -- since those rows
          are other dictations. Matched by text, F10 on a failed "Ich komme
          morgen." row carried a streaming tail "." or " morgen.", which
          then was no longer offered anywhere (2026-10-09 review).
        - Otherwise the offer itself (the overlay's Insert, or an F10 of
          the text whose re-paste failed) carries it, compared with the
          whitespace folded: the tail is inserted with a leading space.
        - And for a row-less offer, the tray's re-paste of the shown
          transcript (``shown_fallback``) carries it when the offer is its
          last words (`tail_prefix`): the whole dictation of which the tail
          is the part that failed. Equality was the test once; the tail's
          insert failed before the keystroke, the tray's re-paste failed
          after it, and the next repaint armed Insert for words that had
          just reached the window. A plain substring test was wider than
          that ("Wochenende" carried " ende").
        """
        offer = self._insert_action_text
        if not offer:
            return False
        own_rows = self._insert_action_rows
        if undelivered:
            listed = self._still_insertable(own_rows)
            return bool(listed) and all(
                any(row is pasted for pasted in undelivered) for row in listed
            )
        prefix = tail_prefix(text, offer)
        if prefix == "":
            return True
        return prefix is not None and shown_fallback and not own_rows

    def _edit_reaches(
        self,
        text: str,
        rows: Sequence[_UndeliveredInsert] = (),
        entry: TranscriptHistoryEntry | None = None,
    ) -> bool:
        """Whether the overlay's Edit, which edits the shown transcript and
        its history entry, also edits ``text`` -- a failed or doubtful paste
        of it, or the tail of it a streaming finalize could not insert.

        Then Edit is enabled on the Error that offers ``text`` (owner's rule
        2026-10-09): an edit there is what Insert and the re-paste paste.
        ``rows`` are the listed rows ``text`` is built from, if any: then
        the shown entry must be one of theirs. Matched by text alone, a
        failed F10 of another dictation's "okay." row enabled Edit, and the
        edit went to the shown "okay." entry while the row kept its text.
        Without rows, ``entry`` is the history entry ``text`` belongs to
        (`_insert_action_entry`), and it must be the shown one: a failed
        re-paste of another dictation's "okay." is no tail of a shown
        "Alles okay.".
        """
        shown = self._last_history_entry
        if shown is None or tail_prefix(self._last_transcript, text) is None:
            return False
        if rows:
            return any(
                part_entry == shown for row in rows for part_entry, _part in row.parts
            )
        return entry is not None and entry == shown

    def _preload_abort_hint(self, action: str) -> str:
        """The " Use Cancel to <action>." sentence, or the hotkey, or the tray.

        With an Insert offer pending the progress line is painted as an
        Error whose action slot holds Insert (or a disabled Cancel), so the
        button the sentence named was not on screen for the length of a
        multi-gigabyte fetch. The cancel hotkey still reaches
        `_cancel_model_preload_if_running`; without one -- none configured,
        or its registration failed -- nothing on the overlay aborts the
        download, and the tray's cancel entry, which every configuration
        has, is named. Dropping the sentence left a multi-gigabyte fetch
        with no way out on screen and no explanation.
        """
        if not self._insert_action_text:
            return f" Use Cancel to {action}."
        hotkey = str(self._settings.cancel_hotkey or "").strip()
        if hotkey and self._cancel_hotkey_registration_ok:
            return f" Press {hotkey} to {action}."
        return f" Use the tray's {TRAY_CANCEL_ACTION_LABEL} to {action}."

    def preload_download_progress(self) -> DownloadBytesSample | None:
        """The preload download's own byte counters, if it is reporting any.

        The Settings Local tab renders a controller-started download as its
        own, so it needs the same numbers this overlay line uses rather than
        a second measurement of the directory.
        """
        with self._preload_download_lock:
            process = self._preload_download_process
        return model_download_process_progress(process)

    def _preload_progress_detail(self) -> str:
        from .transcriber.local_faster_whisper import estimate_cached_model_bytes

        model_name = self._preload_target_model or self._settings.model_size
        model_dir = str(getattr(self._settings, "model_dir", "") or "")
        if self._preload_waits_for_another_model(model_name, model_dir):
            # Same reason as the cross-process case below, one layer in: the
            # slot is held by a *different* model, so nothing is writing to
            # this one's destination and any percentage rendered for it is
            # invented. Measured before this: a frozen "approx. 60% (919/1531
            # MB), measuring speed" for as long as the other download took.
            return (
                f"Waiting for another model download to finish before "
                f"downloading '{model_name}'. You can start recording now; "
                "transcription waits for this model."
                f"{self._preload_abort_hint('abort')}"
            )
        if model_download_coordinator().waiting_for_other_process():
            # Progress is directory growth, and another process owns the
            # directory -- so the bar would sit at a frozen 0% and claim to
            # be downloading. Say what is actually happening instead.
            return (
                f"Waiting for another program to finish using the model "
                f"cache before downloading '{model_name}'. You can start "
                "recording now; transcription waits for this model."
                f"{self._preload_abort_hint('abort')}"
            )
        phase = self._current_preload_phase()
        # Both lines below name the way out like the download lines do:
        # with an Insert offer pending the action slot holds Insert, so a
        # line without the sentence left the queued phase (minutes behind
        # another model's load) and the load phase with neither a Cancel
        # button nor a word about one on screen.
        if phase == _PRELOAD_PHASE_QUEUED:
            return (
                f"Waiting for the previous model before preparing "
                f"'{model_name}'. You can start recording now; transcription "
                "waits for this model."
                f"{self._preload_abort_hint('abort the preload')}"
            )
        if phase == _PRELOAD_PHASE_LOAD:
            # Nothing is being fetched any more. The progress bar measures
            # directory growth, so during the load it printed a frozen
            # "approx. 100%" next to the word "Downloading" for a model that
            # was already complete on disk.
            return (
                f"Loading '{model_name}' into memory. You can start recording "
                "now; transcription waits for this model."
                f"{self._preload_abort_hint('abort loading')}"
            )
        # The worker's own byte counters when it reports them, the size of
        # the download destination otherwise -- see
        # `model_download_progress` for why the directory alone was not good
        # enough once the Hub moved its repos onto Xet storage.
        sample = self.preload_download_progress()
        if sample is not None:
            downloaded_bytes, reported_total = (
                sample.downloaded_bytes,
                sample.total_bytes,
            )
        else:
            downloaded_bytes = estimate_cached_model_bytes(
                model_name,
                getattr(self._settings, "model_dir", ""),
            )
            reported_total = 0

        progress = self._preload_speed_tracker.measure(
            model_name,
            downloaded_bytes,
            reported_total_bytes=reported_total,
            display_name=_local_model_display_name(model_name),
            from_downloader=sample is not None,
        )
        detail = format_model_download_progress(
            progress,
            include_progress_bar=True,
        )

        return (
            f"{detail} You can start recording now; transcription waits for "
            f"this model.{self._preload_abort_hint('abort download')}"
        )

    @QtCore.Slot()
    def _on_preload_progress_poll(self) -> None:
        preload = self._preload_future
        if preload is None or preload.done():
            self._preload_progress_timer.stop()
            return

        # Do not overwrite the states of an active session. That includes a
        # foreground transcription in flight: after `stop_recording` there
        # is no capture and no stream, only the request token, and a model
        # changed in Settings while a dictation was still transcribing
        # painted the download progress over "Transcribing audio...".
        if self._overlay_session_active():
            return

        # Nor a finished one. Done carries the transcript, and Error carries
        # the reason plus the Retry or Insert action that is the only way to
        # recover the recording -- and this poll repaints every 600 ms, so a
        # preload running while a queued transcription finished (the user
        # changes the model in Settings while one is still in flight) replaced
        # both with "Loading model...". `_overlay_session_active` cannot see
        # this: a delivered result has already cleared its request token.
        # The one Error it does repaint is the offer the painter itself
        # wrote -- an insert still pending while a preload runs -- which
        # otherwise froze the progress line for the whole download.
        if (
            self._overlay.state in {"Done", "Error"}
            and not self._offer_painted_is_on_screen()
        ):
            return
        # What is on screen may be under the user's hands: `set_state`
        # scrolls back to the rest position and `setText` drops any
        # selection, so repainting every 600 ms made reading or selecting
        # the pending transcript beside an offer impossible for the length
        # of the download -- and, gated on the offer alone, the progress
        # line itself was still repainted over whatever the user had
        # selected in it. Scrolled or selected, the detail is left alone.
        if self._overlay.detail_is_being_read:
            return

        try:
            detail = self._preload_progress_detail()
        except Exception:
            detail = "Loading model..."
        self._paint_status_keeping_offer("Processing", detail, compact=False)

    def _preload_model_worker(
        self,
        settings: AppSettings,
        generation: int,
        key: tuple[object, ...],
    ) -> None:
        """Background worker: eagerly load the configured local model."""
        self._set_preload_phase(generation, _PRELOAD_PHASE_DOWNLOAD)
        try:
            self._download_model_for_preload(settings, generation)
        except RuntimeError as exc:
            if self._preload_generation_was_canceled(generation):
                self._record_model_preload_result(key, generation, None)
                self.model_preload_done.emit(generation, False, str(exc))
                return
            # Download failed but cached models may still be usable.
            self._logger.warning("Model download failed: %s", exc)

        if self._preload_generation_was_canceled(generation):
            self._record_model_preload_result(key, generation, None)
            self.model_preload_done.emit(generation, False, "Model preload canceled.")
            return

        self._set_preload_phase(generation, _PRELOAD_PHASE_LOAD)
        runtime_lease: _TranscriberRuntimeLease | None = None
        try:
            runtime_lease = self._acquire_transcriber_runtime(
                settings,
                allow_isolated=False,
            )
            if self._preload_generation_was_canceled(generation):
                self._record_model_preload_result(key, generation, None)
                self.model_preload_done.emit(
                    generation,
                    False,
                    "Model preload canceled.",
                )
                return
            transcriber = runtime_lease.transcriber
            # A transcriber that finds its model missing downloads it from its
            # own load path, and that download waits for the machine-wide slot
            # with *this* check as its only interrupt. Without it Cancel could
            # not reach the wait at all: the overlay's Cancel kills only the
            # preload's own download subprocess, so a load-path download --
            # which is the only one when the model looks cached, or when the
            # preload download failed and was swallowed as a warning above --
            # blocked until the other holder released the slot. Cleared in the
            # `finally` for the same reason a batch job clears it: the runtime
            # is shared and cached for the app's lifetime.
            self._set_transcriber_cancel_check(
                transcriber,
                lambda: self._preload_generation_was_canceled(generation),
            )
            # Any local runtime that can preload should: skipping one makes the
            # first dictation pay the full cold load while the overlay has
            # already announced "Model loaded", and a broken install goes
            # undetected until the user speaks.
            preload = getattr(transcriber, "preload_model", None)
            if callable(preload):
                preload()
        except TranscriptionCanceled:
            # The transcriber's own download reached the cancelled download
            # slot (the check installed above, or shutdown). That is not a
            # broken model: the branch below would both show "could not be
            # loaded" and *persist* that failure for this key, so the next
            # dictation would re-raise it instead of retrying.
            #
            # Condemn the runtime the way the failure branch does. `None` is
            # the *success* sentinel for `_local_model_preload_needed`, and
            # the cached key already matches this settings snapshot, so
            # without this a half-loaded runtime would be reported as
            # preloaded and never retried.
            if runtime_lease is not None:
                with self._transcriber_runtime_state_lock:
                    self._pending_transcriber_cache_reset = True
            self._record_model_preload_result(key, generation, None)
            self.model_preload_done.emit(generation, False, "Model preload canceled.")
            return
        except Exception as exc:
            # Condemn first, then decide what to report. The cancel branch
            # below returns, and it used to return from *above* this block --
            # so a cancel that surfaced as a plain exception left the
            # half-loaded runtime in the cache and recorded `None`, which is
            # the *success* sentinel `_local_model_preload_needed` reads. The
            # next dictation then transcribed with a partially initialized
            # runtime and never retried the load. The two arms on either side
            # of this one both condemn unconditionally; this one did not.
            #
            # Reachable whenever a load fails for an ordinary reason while a
            # cancel is pending -- a corrupt snapshot, a missing Node runtime,
            # a process that will not spawn. (An earlier version of this
            # comment named the Cohere/Granite child-kill as the case. That is
            # wrong twice over: the kill raises `TranscriptionCanceled`, which
            # the arm above already handles, and it lives in `transcribe_batch`
            # -- `local_webgpu_asr.preload_model` is `_ensure_process()` with
            # no cancel check at all.)
            if runtime_lease is not None:
                with self._transcriber_runtime_state_lock:
                    self._pending_transcriber_cache_reset = True
            if self._preload_generation_was_canceled(generation):
                self._record_model_preload_result(key, generation, None)
                self.model_preload_done.emit(
                    generation,
                    False,
                    "Model preload canceled.",
                )
                return
            self._logger.warning(
                "Model preload failed for %s: %s", settings.model_size, exc
            )
            failure = (
                f"Selected model '{settings.model_size}' could not be loaded: {exc}. "
                "No fallback model was used. Open Settings to retry or select "
                f"another model. See {DOC_MODELS_PATH}"
            )
            self._record_model_preload_result(key, generation, failure)
            self.model_preload_done.emit(generation, False, failure)
            return
        except BaseException as exc:
            # Without this the signal below is never emitted: the preload never
            # resolves, `_preload_phase` keeps answering for a preload that
            # ended -- breaking its documented "empty when none is running"
            # contract -- and the recording-start notice goes on naming a
            # phase forever.
            self._logger.exception("Model preload raised a BaseException")
            if runtime_lease is not None:
                with self._transcriber_runtime_state_lock:
                    self._pending_transcriber_cache_reset = True
            # Copied from the arm above, and the cancel check has to come with
            # it. `_record_model_preload_result(key, generation, failure)`
            # *persists* that string, and `toggle_recording` reads it before
            # every dictation -- so a user who pressed Cancel got a hard
            # "could not be loaded" error on their next recording instead of a
            # retry, which is exactly what the `TranscriptionCanceled` arm
            # above documents and avoids.
            if self._preload_generation_was_canceled(generation):
                self._record_model_preload_result(key, generation, None)
                self.model_preload_done.emit(
                    generation,
                    False,
                    "Model preload canceled.",
                )
                return
            failure = (
                f"Selected model '{settings.model_size}' could not be loaded: {exc}. "
                "No fallback model was used. Open Settings to retry or select "
                f"another model. See {DOC_MODELS_PATH}"
            )
            self._record_model_preload_result(key, generation, failure)
            self.model_preload_done.emit(generation, False, failure)
            return
        finally:
            if runtime_lease is not None:
                # Before the release, so the next owner of the shared runtime
                # never inherits this preload's generation check -- and
                # guarded, like the other two clear sites, because skipping
                # `release()` would strand `_transcriber_runtime_lock` for the
                # process lifetime: every later preload and import would block
                # forever and every dictation would quietly build its own
                # isolated multi-gigabyte runtime.
                try:
                    self._set_transcriber_cancel_check(runtime_lease.transcriber, None)
                except BaseException:
                    self._logger.exception(
                        "Failed to clear the preload transcriber cancel hook"
                    )
                finally:
                    runtime_lease.release()

        if self._preload_generation_was_canceled(generation):
            self._record_model_preload_result(key, generation, None)
            self.model_preload_done.emit(generation, False, "Model preload canceled.")
            return
        self._record_model_preload_result(key, generation, None)
        self.model_preload_done.emit(
            generation,
            True,
            f"Model loaded: {settings.model_size}",
        )

    @QtCore.Slot(int, bool, str)
    def _on_model_preload_done(
        self,
        generation: int,
        success: bool,
        message: str,
    ) -> None:
        if self._shutdown_started:
            return
        with self._preload_result_lock:
            self._preload_canceled_generations.discard(generation)
            cleanup_note = self._preload_cleanup_notes.pop(generation, "")
            if generation != self._preload_generation:
                self._logger.info(
                    "Ignoring stale model preload completion generation=%s current=%s",
                    generation,
                    self._preload_generation,
                )
                if cleanup_note:
                    # The model was switched while it downloaded, and the
                    # new preload's progress line owns the overlay: the
                    # partials a scanner still holds are logged, not painted.
                    self._logger.warning(
                        "Retired model preload generation=%s left partials:%s",
                        generation,
                        cleanup_note,
                    )
                return
            # Nothing is downloading or loading any more. Leaving the last
            # phase behind made `_current_preload_phase()` keep answering
            # "load" forever, contradicting its own "or empty when none is
            # running" contract for any later reader.
            self._preload_phase = None
        self._preload_progress_timer.stop()
        self._preload_target_model = None
        self._terminate_preload_download_process()
        session_active = self._overlay_session_active()

        ready_model = self._settings.model_size

        if self._preload_cancel_requested:
            self._preload_cancel_requested = False
            if not session_active:
                self._paint_status_keeping_offer(
                    "Done", "Model preload canceled." + cleanup_note
                )
                QtCore.QTimer.singleShot(1200, self.show_idle_status)
            return

        if success:
            self._logger.info("Model preload: %s", message)
            if not session_active:
                self._paint_status_keeping_offer(
                    "Done",
                    f"Model '{ready_model}' is ready.",
                )
                QtCore.QTimer.singleShot(1800, self.show_idle_status)
            else:
                self._logger.info(
                    "Model '%s' became ready during active recording.", ready_model
                )
        else:
            self._logger.warning("Model preload failed: %s", message)
            if "canceled" in message.lower():
                # A Settings save that leaves the local engine cancels the
                # running generation without bumping it and without
                # `_preload_cancel_requested`, so the worker's "Model
                # download canceled." arrives here. It carries the cleanup's
                # count like the explicit-cancel arm above; measured painted
                # without it on that road while a scanner held the partials.
                if not session_active:
                    self._paint_status_keeping_offer("Done", message + cleanup_note)
                    QtCore.QTimer.singleShot(1200, self.show_idle_status)
            else:
                if session_active:
                    self._logger.warning(
                        "Suppressing preload error overlay during active session: %s",
                        message,
                    )
                else:
                    self._paint_status_keeping_offer("Error", message)

    # -- Transcription workers ------------------------------------------------

    def _transcribe_worker(
        self,
        request_token: int,
        wav_bytes: bytes,
        settings: AppSettings,
        job: _TranscriptionJob | None = None,
    ) -> None:
        worker_started_at = time.perf_counter()
        init_started_at = worker_started_at
        transcriber = None
        runtime_lease: _TranscriberRuntimeLease | None = None
        init_elapsed_ms = 0
        transcribe_started_at: float | None = None
        outcome = "initialization_error"
        terminal_kind = "failed"
        terminal_payload = "Transcriber initialization failed."
        try:
            # Skip a job that was canceled before its compute/upload started.
            if job is not None and job.aborting:
                self._logger.info(
                    "transcription_skipped_before_start token=%s engine=%s model=%s "
                    "audio_bytes=%d",
                    request_token,
                    settings.engine,
                    self._selected_model_name(settings),
                    len(wav_bytes),
                )
                outcome = "canceled_before_start"
                terminal_kind = "canceled"
                terminal_payload = ""
                raise TranscriptionCanceled()

            self._wait_for_selected_model_preload(
                settings,
                cancel_check=lambda: bool(
                    self._shutdown_started or (job is not None and job.aborting)
                ),
            )
            # A live stream can still lease the shared runtime while this batch
            # job runs on the single worker lane. Allowing an exact-settings
            # isolated runtime avoids a cycle where the batch waits for the
            # stream lease while the queued stream finalizer waits for this job.
            runtime_lease = self._acquire_transcriber_runtime(settings)
            transcriber = runtime_lease.transcriber
            init_elapsed_ms = round((time.perf_counter() - init_started_at) * 1000)
            self._set_transcriber_progress_callback(
                transcriber,
                lambda detail: self.transcription_progress.emit(
                    request_token,
                    str(detail),
                ),
            )
            if job is not None:
                self._set_transcriber_cancel_check(transcriber, lambda: job.aborting)
            transcribe_started_at = time.perf_counter()
            text = transcriber.transcribe_batch(wav_bytes)
            if not str(text or "").strip() and (job is None or job.mode != "streaming"):
                outcome = "empty_transcript"
                terminal_kind = "failed"
                terminal_payload = _EMPTY_MODEL_TRANSCRIPT_MESSAGE
            else:
                outcome = "success"
                terminal_kind = "ready"
                terminal_payload = text
        except TranscriptionCanceled:
            outcome = "canceled"
            terminal_kind = "canceled"
            terminal_payload = ""
        except NotImplementedError as exc:
            outcome = "not_implemented"
            terminal_kind = "failed"
            terminal_payload = str(exc)
        except TranscriptionError as exc:
            outcome = "provider_error"
            terminal_kind = "failed"
            terminal_payload = str(exc)
        except FileNotFoundError as exc:
            outcome = "missing_file"
            self._logger.exception("Transcription failed due to missing file path")
            terminal_kind = "failed"
            terminal_payload = (
                "Transcription failed: missing file path. "
                "Check input path and TEMP/TMP folder configuration. "
                f"({exc})"
            )
        except Exception as exc:
            initialization_failed = transcribe_started_at is None
            outcome = (
                "initialization_error" if initialization_failed else "unexpected_error"
            )
            self._logger.exception(
                "Failed to create transcriber"
                if initialization_failed
                else "Unexpected transcription failure"
            )
            terminal_kind = "failed"
            terminal_payload = (
                f"Transcriber initialization failed: {exc}"
                if initialization_failed
                else f"Unexpected transcription error: {exc}"
            )
        except BaseException as exc:
            # Last resort, and deliberately not re-raised. The terminal signal
            # below sits after the `finally`, so anything escaping this block
            # leaves the overlay in Processing with no error and no Retry --
            # for the rest of the session, since the job never resolves. A
            # `BaseException` here can only come from a callback that raises
            # one (CPython delivers KeyboardInterrupt to the main thread only,
            # and a SystemExit on a worker thread just ends the thread), so
            # reporting it is strictly better than letting it vanish into the
            # Future.
            outcome = "unexpected_error"
            self._logger.exception("Transcription worker raised %s", type(exc).__name__)
            terminal_kind = "failed"
            terminal_payload = (
                f"Unexpected transcription error: {type(exc).__name__}: {exc}"
            )
        finally:
            # The release used to be the last statement of this block,
            # with roughly forty lines of diagnostics in front of it:
            # three `getattr` property reads off a transcriber, a `len`,
            # a log call and the two hook clears. A raise from any of
            # them skipped `release()` -- the admission lock stranded for
            # the process lifetime, `_transcription_runtime_active()`
            # True forever -- and skipped the terminal signal below,
            # leaving the overlay in Processing with no error and no
            # Retry. The hooks are still cleared *before* the release,
            # because a close during release must not race a live cancel
            # hook; only the guarantee is new.
            try:
                transcribe_elapsed_ms = (
                    round((time.perf_counter() - transcribe_started_at) * 1000)
                    if transcribe_started_at is not None
                    else 0
                )
                total_elapsed_ms = round(
                    (time.perf_counter() - worker_started_at) * 1000
                )
                runtime_device = str(getattr(transcriber, "runtime_device", "") or "")
                gpu_available = getattr(transcriber, "gpu_available", "")
                runtime_details = str(
                    getattr(transcriber, "runtime_details_text", "") or ""
                )
                result_chars = len(terminal_payload) if terminal_kind == "ready" else 0
                self._logger.info(
                    "transcription_timing engine=%s model=%s init_ms=%d "
                    "transcribe_ms=%d total_ms=%d audio_bytes=%d chars=%d "
                    "outcome=%s runtime_device=%s gpu_available=%s "
                    "runtime_details=%s",
                    settings.engine,
                    self._selected_model_name(settings),
                    init_elapsed_ms,
                    transcribe_elapsed_ms,
                    total_elapsed_ms,
                    len(wav_bytes),
                    result_chars,
                    outcome,
                    runtime_device or "n/a",
                    gpu_available if gpu_available != "" else "n/a",
                    runtime_details or "n/a",
                )
                if transcriber is not None:
                    # Clear the cancel hook and progress callback so they cannot
                    # leak into a cached transcriber's next request.  The closure
                    # captures ``request_token``; leaving it installed would let a
                    # later run surface stale progress or cancel state.
                    try:
                        self._set_transcriber_cancel_check(transcriber, None)
                    except BaseException:
                        self._logger.exception(
                            "Failed to clear transcriber cancel hook"
                        )
                    try:
                        self._set_transcriber_progress_callback(transcriber, None)
                    except BaseException:
                        self._logger.exception(
                            "Failed to clear transcriber progress hook"
                        )
            except BaseException:
                self._logger.exception("Transcription bookkeeping failed")
            finally:
                if runtime_lease is not None:
                    runtime_lease.release()

        # Cleanup, optional close, and runtime lease release must all complete
        # before the Qt thread is allowed to clear this job's active state.
        if self._shutdown_started:
            return
        if terminal_kind == "ready":
            self.transcription_ready.emit(request_token, terminal_payload)
        elif terminal_kind == "canceled":
            self.transcription_canceled.emit(request_token)
        else:
            self.transcription_failed.emit(request_token, terminal_payload)

    def _abort_stream_after_failed_connect(self, transcriber) -> None:
        try:
            if hasattr(transcriber, "abort_stream"):
                transcriber.abort_stream()
            else:
                transcriber.stop_stream()
        except Exception:
            # Expected after a handshake that raised: there is no session
            # to abort, and the provider says so.
            self._logger.debug(
                "Aborting the stream after a failed handshake was refused",
                exc_info=True,
            )

    def _await_stream_connect(self, job: _TranscriptionJob | None) -> bool:
        """Wait for an in-flight handshake before stopping the stream.

        Runs on the finalize worker, never the Qt thread. Stopping a provider
        whose `start_stream` has not returned yet is not a no-op: the stop is
        rejected because the session is not active *yet*, and the handshake then
        completes and publishes a socket nobody owns, which blocks every later
        dictation with "Streaming session already active". The wait is bounded
        so a provider that hangs cannot hang the finalize with it, and answers
        False when the bound ran out with the thread still running -- still
        connecting, or still handing the buffered audio over. The caller then
        abandons the session rather than stopping it beside that thread.
        """
        thread = getattr(job, "connect_thread", None) if job is not None else None
        if thread is None or not thread.is_alive():
            return True
        self._logger.info("Waiting for the streaming handshake before stopping it.")
        thread.join(timeout=STREAMING_CONNECT_JOIN_TIMEOUT_S)
        if thread.is_alive():
            self._logger.warning(
                "The streaming handshake did not finish within %.1fs; aborting "
                "the stream.",
                STREAMING_CONNECT_JOIN_TIMEOUT_S,
            )
            return False
        return True

    @staticmethod
    def _stream_handshake_outlived_the_stop_text() -> str:
        return (
            "The speech service was still connecting or still taking the "
            f"recorded audio {STREAMING_CONNECT_JOIN_TIMEOUT_S:g} s after the "
            "stop, so the transcript is incomplete."
        )

    def _finalize_stream_worker(
        self,
        request_token: int,
        transcriber,
        job: _TranscriptionJob | None = None,
    ) -> None:
        runtime_lease = (
            job.runtime_lease
            if job is not None
            and isinstance(job.runtime_lease, _TranscriberRuntimeLease)
            else None
        )
        terminal_kind = "failed"
        terminal_payload = "Streaming session was not initialized."
        try:
            canceled_before_start = job is not None and job.aborting
            if canceled_before_start:
                # Decided before the cleanup, not after it. These two lines
                # used to sit below the abort, whose `except Exception` does
                # not cover a `BaseException`; one escaping from a provider
                # callback then left `terminal_kind` at its "failed"
                # initialiser, so the user who pressed Cancel was told
                # "Recording ... failed: Unexpected streaming error" in a tray
                # notification. What the abort does or fails to do cannot
                # change the fact that this was a cancel.
                terminal_kind = "canceled"
                terminal_payload = ""
                self._logger.info(
                    "stream_finalize_skipped_before_start token=%s engine=%s model=%s",
                    request_token,
                    job.engine,
                    job.model,
                )
                if transcriber is not None:
                    try:
                        if hasattr(transcriber, "abort_stream"):
                            transcriber.abort_stream()
                        else:
                            transcriber.stop_stream()
                    except Exception:
                        self._logger.exception(
                            "Failed to abort canceled streaming finalization"
                        )
            else:
                if transcriber is None:
                    raise TranscriptionError("Streaming session was not initialized.")
                if not self._await_stream_connect(job):
                    # The connect thread outlived the bound: the provider is
                    # still connecting, or its sender stopped draining and
                    # the flush is waiting a budget per chunk. Stopping beside
                    # it used to end the dictation in "Done" with a transcript
                    # missing the audio the flush had not handed over yet
                    # (the stop retired the provider's queue, so it never
                    # was), the only trace a warning in the log; and on a
                    # handshake still running, in "Streaming session is not
                    # active", which names nothing the user can act on. The
                    # session is abandoned as a failure: the abort is what
                    # unblocks a provider parked in its connect wait, the
                    # message names the bound, the recording stays kept, and
                    # the retired thread pushes nothing more.
                    self._abort_stream_after_failed_connect(transcriber)
                    raise TranscriptionError(
                        self._stream_handshake_outlived_the_stop_text()
                    )
                connect_failure = self._stream_connect_failure_for(job)
                if connect_failure is not None:
                    # The handshake this job joined failed, or its flush
                    # did. Either way there is no session to stop for text:
                    # tear the provider down best-effort -- a session whose
                    # flush failed is still published, and every provider
                    # refuses a second one -- and report the cause.
                    self._abort_stream_after_failed_connect(transcriber)
                    raise TranscriptionError(connect_failure)
                text = transcriber.stop_stream()
                terminal_kind = "ready"
                terminal_payload = text
        except NotImplementedError as exc:
            terminal_payload = str(exc)
        except TranscriptionError as exc:
            terminal_payload = str(exc)
        except Exception as exc:
            self._logger.exception("Unexpected streaming finalization failure")
            terminal_payload = f"Unexpected streaming error: {exc}"
        except BaseException as exc:
            # Same last resort as `_transcribe_worker`, and worse here if it is
            # missing: the terminal signal sits after the `finally`, so an
            # escaping exception leaves `_streaming_recording` True and every
            # later hotkey press is refused with "Streaming transcript is still
            # finalizing" until Cancel or a restart. `stop_stream()` drains a
            # provider socket and runs its callbacks, so a callback raising a
            # BaseException reaches here.
            self._logger.exception("Streaming finalization raised a BaseException")
            terminal_payload = f"Unexpected streaming error: {exc}"
        finally:
            if runtime_lease is not None:
                runtime_lease.release()
            if job is not None:
                job.runtime_lease = None
                job.runtime_transcriber = None

        if self._shutdown_started:
            return
        if terminal_kind == "ready":
            self.transcription_ready.emit(request_token, terminal_payload)
        elif terminal_kind == "canceled":
            self.transcription_canceled.emit(request_token)
        else:
            self.transcription_failed.emit(request_token, terminal_payload)

    def _emit_stream_partial(self, text: str) -> None:
        self.transcription_partial.emit(text)

    def _emit_stream_runtime_failure(self, token: object, error_text: str) -> None:
        """Report a streaming runtime failure of the session `token` names."""
        message = str(error_text or "Streaming failed.").strip()
        self.stream_runtime_failed.emit(token, message or "Streaming failed.")

    def _pending_streaming_job(self) -> _TranscriptionJob | None:
        """The streaming finalize still in flight, if there is one.

        An aborting job does not count: it is being canceled and will not
        deliver, so treating it as pending would drop the partial transcript
        this guard exists to avoid duplicating.
        """
        for job in self._jobs.values():
            if job.mode == "streaming" and not job.aborting:
                return job
        return None

    def _has_pending_streaming_job(self) -> bool:
        """Whether a streaming finalize is still in flight and will deliver text."""
        return self._pending_streaming_job() is not None

    def _current_streaming_partial_text(self) -> str:
        """Best-known transcript of the live streaming session.

        Shared by the abort and runtime-failure paths so the two cannot drift on
        which field wins; both must keep what was already transcribed.
        """
        return normalize_stream_text(
            self._stream_text_state.live_text
            or self._stream_text_state.last_partial_text
        )

    def _stop_active_capture(self) -> tuple[bytes, str]:
        """Stop the active capture and return its audio plus the retained path.

        The caller persists the audio: it is the one that has to know
        whether that write happened, because the store's slot is the
        previous recording's when it did not.
        """
        capture = self._audio_capture
        self._audio_capture = None
        self._cancel_audio_callback_watchdog(capture)
        if capture is None:
            return b"", ""

        wav_bytes = b""
        try:
            # Not the user's stop: no backlog wait (`AudioCapture.stop`).
            wav_bytes = capture.stop(drain=False)
        except Exception:
            self._logger.exception("Failed to stop active audio capture")

        source_audio_path = self._save_recording_artifacts(capture, wav_bytes)
        return wav_bytes, source_audio_path

    def _teardown_active_stream_runtime(self) -> tuple[bytes, str]:
        """Stop the live stream's capture and abort its transcriber. The
        caller persists the audio handed back: it is the one that has to
        know whether that write happened."""
        wav_bytes, source_audio_path = self._stop_active_capture()

        transcriber = self._active_stream_transcriber
        self._active_stream_transcriber = None
        runtime_lease = self._active_stream_runtime_lease
        self._active_stream_runtime_lease = None
        try:
            if transcriber is not None:
                if hasattr(transcriber, "abort_stream"):
                    transcriber.abort_stream()
                else:
                    transcriber.stop_stream()
        except Exception:
            self._logger.exception("Failed to abort active streaming transcriber")
        finally:
            if runtime_lease is not None:
                runtime_lease.release()

        return wav_bytes, source_audio_path

    def _on_stream_audio_chunk(self, chunk: bytes) -> None:
        """Called from the PortAudio callback thread — must be lightweight.

        Focus changes are handled by ``_focus_poll_timer`` on the Qt
        main thread; we intentionally avoid Win32 API calls here because
        the PortAudio real-time thread must not block on system calls.
        """
        if self._audio_capture is None and self._stopping_capture is None:
            return
        if self._stream_abort_requested or self._stream_chunk_error_reported:
            return

        transcriber = self._active_stream_transcriber
        if transcriber is None:
            return
        # While a remote provider is still shaking hands the stream cannot take
        # audio yet. Buffer instead of dropping: the microphone is deliberately
        # opened before the handshake finishes so the user can start talking
        # immediately.
        with self._stream_preconnect_lock:
            pending = self._stream_preconnect_chunks
            if pending is not None:
                buffered = sum(len(item) for item in pending)
                if buffered + len(chunk) <= STREAMING_PRECONNECT_BUFFER_MAX_BYTES:
                    pending.append(chunk)
                else:
                    # Keep the *oldest* audio: it holds the first words, and a
                    # connection this slow is going to fail anyway.
                    self._stream_preconnect_dropped = True
                return
        try:
            transcriber.push_audio_chunk(chunk)
        except Exception as exc:
            if self._stream_chunk_error_reported:
                return
            self._stream_chunk_error_reported = True
            self._stream_abort_requested = True
            self._logger.exception("Failed to push streaming audio chunk")
            # This push went into `_active_stream_transcriber`, i.e. into the
            # session that is live right now, so the live session token is
            # its own.
            self._emit_stream_runtime_failure(
                self._stream_session_token, f"Streaming chunk push failed: {exc}"
            )

    @staticmethod
    def _transcriber_identity(settings: AppSettings) -> _TranscriberIdentity:
        """Everything ``create_transcriber`` bakes into the runtime it builds.

        Two settings snapshots with an equal identity produce interchangeable
        transcribers, so a loaded runtime may be reused across them and a save
        that changes nothing here must not tear it down.

        The identity is built **per engine** and leaves every field the chosen
        engine does not read at its default. Listing all of them unconditionally
        was itself the defect this exists to prevent, one level up: pasting an
        Azure endpoint unloaded a multi-gigabyte local model that never reads it.

        ``language_mode`` is deliberately absent: every provider reads the
        language when a request or stream starts, so a language change only has
        to be applied to the existing runtime (see ``set_language_mode`` in
        ``_get_or_create_transcriber``). Keying on it would throw away a loaded
        local model -- several GB and seconds of load time -- for a setting the
        runtime does not depend on. The API key *value* likewise never enters
        ``AppSettings``; replacing one is handled by
        ``invalidate_transcriber_credentials``, while gaining or losing one
        shows up here as ``has_api_key``.
        """
        engine = settings.engine
        vocabulary = (
            getattr(settings, "custom_vocabulary", "")
            # An unrecognised engine falls back to the local path in
            # `create_transcriber`, which *does* pass the vocabulary on, so it
            # has to read it here too. `SettingsStore.load()` coerces an
            # unknown engine to `local`, so this is a latent inconsistency
            # rather than a reachable one -- but the fallback below is written
            # to survive an unknown engine, and this was the one field where
            # it did not. A remote engine reads it only where its provider
            # receives it (`supports_custom_vocabulary`, per model: Speechmatics'
            # Melia 1 has no custom dictionary).
            if engine == DEFAULT_ENGINE
            or engine not in _ENGINE_MODEL_FIELDS
            or supports_custom_vocabulary(
                engine, getattr(settings, _ENGINE_MODEL_FIELDS[engine], "")
            )
            else ""
        )
        if engine == DEFAULT_ENGINE or engine not in _ENGINE_MODEL_FIELDS:
            # `create_transcriber` falls back to the local path for an unknown
            # engine, so an unknown one must produce the local identity too.
            #
            # "local" is five different runtimes and they read different
            # settings, so the per-engine scoping above has to continue one
            # level down: listing every local field unconditionally made
            # Parakeet reload its 670 MB model when the user typed a
            # custom-vocabulary term that onnx-asr never receives. The
            # branches below mirror `_create_local_transcriber` exactly --
            # keep them in step. (The count is deliberately not written out
            # here: it was "ten" for two fields longer than it was true.)
            model_size = settings.model_size
            common = {
                "engine": engine,
                "model_size": model_size,
                "offline_mode": bool(getattr(settings, "offline_mode", False)),
                "model_dir": getattr(settings, "model_dir", ""),
            }
            if (
                model_size in LOCAL_ONNX_ASR_MODEL_SIZES
                or model_size in LOCAL_GRANITE_CTC_MODEL_SIZES
            ):
                # onnx-asr and the Granite CTC graph take nothing else, and
                # both are CPU-only, so the device policy never reaches them
                # either.
                return _TranscriberIdentity(**common)
            if model_size in LOCAL_NEMOTRON_MODEL_SIZES:
                return _TranscriberIdentity(
                    **common,
                    vad_enabled=settings.vad_enabled,
                    # The *resolved* provider order, not the raw policy: the
                    # factory passes `nemotron_provider_order(...)`, and ORT
                    # GenAI has no WebGPU provider, so `gpu`, `dml` and
                    # `webgpu` all map onto `("dml",)`. Keeping the raw string
                    # made switching the picker between two of them close the
                    # loaded 793 MB model and preload the identical runtime --
                    # exactly the needless reload this identity exists to
                    # prevent.
                    local_onnx_device=",".join(
                        nemotron_provider_order(
                            getattr(settings, "local_onnx_device", ""),
                            preferred_onnx_device(settings),
                        )
                    ),
                )
            if model_size in LOCAL_WEBGPU_MODEL_SIZES:
                return _TranscriberIdentity(
                    **common,
                    local_onnx_device=getattr(settings, "local_onnx_device", ""),
                    onnx_preferred_device=preferred_onnx_device(settings),
                    # Not a constructor argument, but it decides whether
                    # `_get_or_create_transcriber` caches this runtime at all.
                    keep_onnx_model_loaded=bool(
                        getattr(settings, "keep_onnx_model_loaded", False)
                    ),
                )
            return _TranscriberIdentity(
                **common,
                vad_enabled=settings.vad_enabled,
                streaming_full_final_transcript=bool(
                    getattr(settings, "streaming_full_final_transcript", False)
                ),
                custom_vocabulary=vocabulary,
                silence_gate_enabled=bool(
                    getattr(settings, "silence_gate_enabled", True)
                ),
                silence_gate_threshold=float(
                    getattr(
                        settings,
                        "silence_gate_threshold",
                        DEFAULT_SILENCE_GATE_THRESHOLD,
                    )
                ),
            )
        custom_fields = (
            {
                "custom_endpoint": str(getattr(settings, "custom_endpoint", "") or ""),
                "custom_api_mode": str(getattr(settings, "custom_api_mode", "") or ""),
                "custom_key_command": str(
                    getattr(settings, "custom_key_command", "") or ""
                ),
            }
            if engine == "custom"
            else {}
        )
        return _TranscriberIdentity(
            engine=engine,
            custom_vocabulary=vocabulary,
            remote_model=str(getattr(settings, _ENGINE_MODEL_FIELDS[engine], "") or ""),
            azure_endpoint=(
                getattr(settings, "azure_endpoint", "") if engine == "azure" else ""
            ),
            # An engine that splits a long recording judges a part that came
            # back empty against the silence gate's threshold
            # (`transcribe_in_parts`); the others never read it.
            silence_gate_threshold=(
                float(
                    getattr(
                        settings,
                        "silence_gate_threshold",
                        DEFAULT_SILENCE_GATE_THRESHOLD,
                    )
                )
                if engine in REMOTE_BATCH_MAX_PART_SECONDS
                else 0.0
            ),
            remote_region=(
                str(getattr(settings, _ENGINE_REGION_FIELDS[engine], "") or "")
                if engine in _ENGINE_REGION_FIELDS
                else ""
            ),
            **custom_fields,
            # A key command is a credential source of its own: with one set,
            # the engine can run without a stored key.
            has_api_key=bool(getattr(settings, _ENGINE_KEY_FLAGS[engine], False))
            or bool(custom_fields.get("custom_key_command")),
            allow_insecure_key_storage=bool(
                getattr(settings, "allow_insecure_key_storage", False)
            ),
        )

    def _get_or_create_transcriber(self, settings: AppSettings):
        cache_key = self._transcriber_identity(settings)
        if (
            settings.engine == DEFAULT_ENGINE
            and settings.model_size in LOCAL_WEBGPU_MODEL_SIZES
            and not bool(getattr(settings, "keep_onnx_model_loaded", False))
        ):
            return create_transcriber(settings, secret_store=self._secret_store)
        with self._transcriber_cache_lock:
            if (
                self._transcriber_cache is None
                or self._transcriber_cache_key != cache_key
            ):
                # Evicted before it is closed, and before the replacement is
                # built. `create_transcriber` raises for a missing API key or
                # an absent model, and the closed runtime was then still
                # installed under its *old* key -- so switching back to the
                # previous settings handed that dead runtime to the next
                # dictation. The rule already held at
                # `_reset_transcriber_cache_locked` and
                # `_reset_resume_sensitive_transcriber_cache`; this was the
                # third site.
                stale = self._transcriber_cache
                self._transcriber_cache = None
                self._transcriber_cache_key = None
                self._close_cached_transcriber(stale)
                self._transcriber_cache = create_transcriber(
                    settings, secret_store=self._secret_store
                )
                self._transcriber_cache_key = cache_key
            # Apply the language of *this* job's settings snapshot. Acquisition
            # is serialized by the runtime lock, so a reused runtime can never
            # transcribe with a stale language. Every provider implements this
            # through ITranscriber; the lookup keeps duck-typed transcribers
            # (tests, future in-process adapters) working.
            apply_language = getattr(self._transcriber_cache, "set_language_mode", None)
            if callable(apply_language):
                apply_language(settings.language_mode)
            return self._transcriber_cache

    @QtCore.Slot(int, str)
    def _on_transcription_progress_result(
        self,
        request_token: int,
        detail: str,
    ) -> None:
        if self._shutdown_started:
            return
        if not self._is_foreground_transcription(request_token):
            return
        message = str(detail or "").strip()
        if not message:
            return
        self._overlay.set_state("Processing", message, compact=False)

    @QtCore.Slot(int, str)
    def _on_transcription_ready_result(self, request_token: int, text: str) -> None:
        if self._shutdown_started:
            return
        with self._overlay_batch():
            self._on_transcription_ready(text, request_token=request_token)

    def _on_transcription_ready(
        self,
        text: str,
        *,
        request_token: int | None = None,
    ) -> None:
        job: _TranscriptionJob | None = None
        if request_token is not None:
            job = self._jobs.get(request_token)
        session_mode = job.mode if job is not None else self._active_session_mode
        if not text.strip() and session_mode != "streaming":
            self._on_transcription_failed(
                _EMPTY_MODEL_TRANSCRIPT_MESSAGE,
                request_token=request_token,
            )
            return
        if request_token is not None:
            if not self._is_foreground_transcription(request_token, job):
                # A newer recording owns the live session, or this job was asked
                # to stop. Keep the live session untouched and deliver this
                # queued result on its own (history and/or its own window).
                # Its delivery resolves a failure it was the retry of, as on
                # the foreground road.
                self._retire_retry_audio_delivered_by(request_token, job)
                if self._active_request_token == request_token:
                    self._active_request_token = None
                    self._last_transcribe_settings = None
                should_finish = self._handle_background_transcription_ready(job, text)
                if should_finish:
                    self._finish_transcription_job(request_token)
                return
            self._active_request_token = None
            self._retire_retry_audio_delivered_by(request_token, job)

        self._finish_transcription_job(request_token)
        # A batch result with a job may join the deferred results bound for
        # its own window: the flush below holds that group back instead of
        # pasting it, and the result is appended to it further down, so the
        # whole group goes out as one paste. A second paste right after the
        # first, inside the same call, overwrote a clipboard the target may
        # not have read yet.
        joins_paste_queue = job is not None and job.mode != "streaming"
        hold_key = (
            self._paste_group_key(
                job, single_group=self._insert_target_is_current_window()
            )
            if joins_paste_queue
            else None
        )
        # A foreground result is about to claim the overlay. A deferred insert
        # that fails inside this flush must therefore not paint an Error state
        # that is overwritten a few statements later — it would flash and be
        # gone. Its notification still fires, so the failure is never silent.
        self._foreground_delivery_pending = True
        try:
            self._flush_deferred_background_results(hold_key=hold_key)
        finally:
            self._foreground_delivery_pending = False

        target_handle = (
            job.target_handle if job is not None else self._target_window_handle
        )
        target_signature = (
            job.target_signature if job is not None else self._target_focus_signature
        )
        target_handle, target_signature = self._resolve_insert_target(
            target_handle, target_signature
        )

        session_mode = self._active_session_mode
        self._focus_poll_timer.stop()
        self._streaming_recording = False
        stream_settings = self._active_stream_settings
        self._active_stream_transcriber = None
        self._active_stream_settings = None
        self._stream_abort_requested = False
        self._stream_insert_failures = 0
        if not text.strip() and session_mode == "streaming":
            # Naming the mode is locally explicit rather than load-bearing: the
            # early return at the top of this method already sends every
            # non-streaming empty result to `_on_transcription_failed`, and the
            # one way the two mode reads can disagree (a streaming job while
            # `_active_session_mode` says batch) has an empty streaming text
            # state anyway, because every writer of that value resets it.
            #
            # The third road to a wiped streaming transcript. An explicit
            # abort and a dying stream runtime both rescue the live text
            # before the reset below wipes it; a finalize that returned
            # nothing did not, so the overlay said "No speech detected",
            # history got no entry and Copy had nothing -- the dictation
            # survived only as the part already pasted into the document.
            # Both AssemblyAI and Deepgram can return an empty string from
            # `stop_stream` after a socket problem, which is exactly when the
            # live text is the only copy left. A session that really said
            # nothing has no live text either, so it still reports that.
            # Already normalized, so no strip is needed. The branch exists for
            # the log line: a session that really said nothing must not report
            # "keeping the live transcript (0 chars)".
            rescued = self._current_streaming_partial_text()
            if rescued:
                self._logger.info(
                    "streaming_finalize_empty: keeping the live transcript (%d chars).",
                    len(rescued),
                )
                text = rescued

        # Only a real transcript replaces the last one. Assigning before the
        # empty check meant a streaming session that produced nothing wiped
        # the previous dictation from the tray's "Insert last transcript
        # again", which then reported "No transcript available" while that
        # dictation was still sitting in history. The batch silence-gate path
        # already gets this right.
        if text.strip():
            self._last_transcript = text
            self._shown_transcript_token = job.token if job is not None else None

        if not text.strip():
            self._mark_last_recording_completed(job, text)
            self._overlay.set_state("Done", "No speech detected.")
            self._reveal_overlay_result(is_error=False)
            self._last_transcribe_settings = None
            self._reset_streaming_state()
            return

        used_settings = (
            self._last_transcribe_settings or stream_settings or self._settings
        )
        history_entry = self._append_transcript_history(
            text,
            used_settings,
            session_mode,
            source_recording_id=(job.source_recording_id if job is not None else None),
            source_audio_path=(job.source_audio_path if job is not None else ""),
        )

        if (
            joins_paste_queue
            and session_mode != "streaming"
            and (
                self._deferred_paste_group_exists(hold_key)
                or self._paste_pace_wait_s() > 0
            )
        ):
            self._deliver_foreground_through_paste_queue(job, text, history_entry)
            return

        if session_mode == "streaming":
            nothing_inserted = not self._stream_text_state.committed_text
            final_insertion, final_text = self._stream_text_state.finalize_append_only(
                text
            )
            if (
                final_insertion
                and nothing_inserted
                and job is not None
                and (
                    self._earlier_result_waits_for(job.target_handle, before=job.token)
                    or self._paste_pace_wait_s() > 0.0
                )
            ):
                # None of this dictation is in the document, so it is a paste
                # like a batch result's: behind an earlier result for its
                # window that the pace or a recording still holds (owner's
                # rule 2026-10-09, order per window; pasted at once it went
                # in ahead of that result), and after the previous paste's
                # restore window.
                self._deliver_foreground_through_paste_queue(
                    job, final_insertion, history_entry, shown=final_text
                )
                return
            # Otherwise exempt from the paste pace: the live inserts pace
            # themselves through the locked prefix, and this tail is the rest
            # of a dictation whose words are already in the document.
            if final_insertion and not self._insert_text_at_target(
                final_insertion,
                restore_focus=True,
                target_handle=target_handle,
                target_signature=target_signature,
                # The tail belongs to this dictation's entry.
                text_entry=history_entry,
            ):
                self._reveal_overlay_result(is_error=True)
                self._mark_last_recording_completed(job, text)
                self._last_transcribe_settings = None
                self._reset_streaming_state()
                return
            self._overlay.set_state("Done", final_text)
        else:
            if not self._insert_text_at_target(
                text,
                restore_focus=True,
                target_handle=target_handle,
                target_signature=target_signature,
                text_entry=history_entry,
            ):
                # The Error with its Insert is replaced by the next
                # recording; the row keeps the transcript visible after that.
                offered = self._last_insert_offered
                entry = self._record_undelivered_insert(
                    text,
                    may_have_pasted=self._last_insert_may_have_pasted,
                    created_at=(
                        job.created_at
                        if job is not None
                        else datetime.now().astimezone()
                    ),
                    history_entry=history_entry,
                )
                if offered and entry is not None:
                    self._insert_action_rows = (entry,)
                self._shown_transcript_row = entry
                self._reveal_overlay_result(is_error=True)
                self._mark_last_recording_completed(job, text)
                self._last_transcribe_settings = None
                self._reset_streaming_state()
                return

            self._overlay.set_state("Done", text)
            self._check_paste_target(
                self._paste_check(
                    text,
                    history_entry,
                    created_at=(
                        job.created_at
                        if job is not None
                        else datetime.now().astimezone()
                    ),
                    identity="The transcript was",
                    background=False,
                    takes_shown_pair=False,
                )
            )

        # Bring the (possibly floating/hidden) overlay forward so the finished
        # transcript is actually visible for a quick confirmation.
        self._reveal_overlay_result(is_error=False)
        if self._settings.keep_transcript_in_clipboard:
            QtGui.QGuiApplication.clipboard().setText(text)
        self._mark_last_recording_completed(job, text)
        self._last_transcribe_settings = None
        self._reset_streaming_state()

    def _deliver_foreground_through_paste_queue(
        self,
        job: _TranscriptionJob,
        text: str,
        history_entry: TranscriptHistoryEntry | None,
        *,
        shown: str | None = None,
    ) -> None:
        """Show a foreground result now and paste it with the paste queue.

        Taken when deferred results for the same window are waiting, or when
        the previous paste's restore window is still open. Everything the
        user sees happens now -- Done with this result, the history entry,
        Copy/Edit on it -- and only the paste joins the queue: one paste with
        the waiting results, at once or at the end of the window. A failed
        joined paste takes the coalesced queue paste's road
        (`_report_background_insertion_failure`): the Error shows, copies and
        offers Insert for the joined text, and Edit refuses.

        `keep_transcript_in_clipboard` is not applied separately here: the
        paste itself skips the restore then, so the clipboard ends up with
        the text that was pasted, the joined one when others went with it.

        A streaming result none of which was inserted live comes here too;
        ``shown`` is its finalized transcript, painted instead of the
        insertion text.
        """
        job.history_entry = history_entry
        job.insertion_deferred = True
        # A foreground result waits for nothing but the restore window: a
        # transcription started meanwhile must not hold it back.
        job.paste_paced = True
        self._jobs[job.token] = job
        self._deferred_background_results.append((job, text))
        self._overlay.set_state("Done", text if shown is None else shown)
        self._reveal_overlay_result(is_error=False)
        self._mark_last_recording_completed(job, text)
        self._last_transcribe_settings = None
        self._reset_streaming_state()
        self._flush_deferred_background_results()

    # -- Paste pace -----------------------------------------------------------

    @staticmethod
    def _paste_group_key(
        job: _TranscriptionJob, *, single_group: bool
    ) -> tuple[object, object]:
        """The window a deferred result is pasted into, as the coalescing key.

        With current-window insertion every result goes to the same (current)
        target, so one key covers them all.
        """
        if single_group:
            return (None, None)
        return (job.target_handle, job.target_signature)

    def _deferred_paste_group_exists(self, key: tuple[object, object] | None) -> bool:
        if key is None:
            return False
        single_group = self._insert_target_is_current_window()
        return any(
            self._paste_group_key(job, single_group=single_group) == key
            for job, _text in self._deferred_background_results
        )

    def _paste_pace_wait_s(self) -> float:
        """Seconds until the previous paste's clipboard-restore window ends.

        Read from the inserter, which knows when its last SendInput keystroke
        went out (`TextInserter.paste_pace_remaining_s`), rather than from a
        second clock here. 0.0 when it cannot say: an inserter without the
        method, one that raised, or an answer that is not a finite positive
        number -- pacing is a guard on top of the paste, never a reason to
        hold one back for good.
        """
        reader = getattr(self._text_inserter, "paste_pace_remaining_s", None)
        if not callable(reader):
            return 0.0
        try:
            remaining = float(reader())
        except Exception:
            self._logger.exception("Could not read the paste pace")
            return 0.0
        if not math.isfinite(remaining) or remaining <= 0.0:
            return 0.0
        return remaining

    @QtCore.Slot()
    def _on_paste_pace_timeout(self) -> None:
        """The restore window ended: paste what the pace held back.

        Held results first, in token order, then a re-paste the user asked
        for meanwhile -- which the flush's own paste may hold again for one
        more window.
        """
        self._paste_pace_timer.stop()
        if self._shutdown_started:
            return
        with self._overlay_batch():
            self._flush_deferred_background_results()
            self._run_pending_repaste()

    def _run_pending_repaste(self) -> None:
        pending = self._pending_repaste
        if pending is None:
            return
        if pending.after_stream and self._stream_still_delivering():
            # The pace timer ran for another paste; this one waits for the
            # stream's end (`_reset_streaming_state` lets it go).
            return
        self._pending_repaste = None
        text = pending.text
        display_entry = pending.display_entry
        undelivered = pending.undelivered
        if undelivered:
            # Rows dismissed or delivered meanwhile are not pasted.
            still_waiting = [
                entry
                for entry in self._insertable_undelivered()
                if any(entry is requested for requested in undelivered)
            ]
            if not still_waiting:
                self._logger.info("repaste_dropped reason=rows_gone")
                # Never end a user's F10 silently.
                self.show_overlay_error(
                    "The waiting transcripts were dismissed or inserted in the "
                    "meantime, so nothing was inserted."
                )
                return
            # Rebuilt from the rows as they are now: some may have gone, and
            # an edit may have changed a row's text meanwhile.
            text = _join_transcripts([entry.text for entry in still_waiting])
            display_entry = (
                still_waiting[0].history_entry if len(still_waiting) == 1 else None
            )
            undelivered = tuple(still_waiting)
        if pending.offer_rows:
            # The Insert's text rebuilt from its rows as they are now: an edit
            # saved meanwhile is what it pastes, and then still counts as
            # carrying the offer. Pasting the old text left the offer (now
            # the edit) pending, and its next Insert pasted the edit as well.
            text = _join_transcripts([row.text for row in pending.offer_rows])
        # Rows dismissed meanwhile drop out: the Insert still pastes their
        # text, but retires only the rows still listed.
        offer_rows = self._still_insertable(pending.offer_rows)
        self._repaste(
            text,
            display_entry=display_entry,
            undelivered=undelivered,
            offer_rows=offer_rows,
            announce_hold=not pending.after_stream,
        )

    def _handle_background_transcription_ready(
        self,
        job: _TranscriptionJob | None,
        text: str,
    ) -> bool:
        """Deliver a queued/canceled result while a newer session is active.

        The transcript is always saved to history (a finished transcription is
        never discarded). It is additionally inserted into the window that was
        focused when it was recorded only when the job's delivery is "insert"
        and it is a batch job. Streaming jobs already inserted their text live,
        and history-only / canceled jobs are not re-inserted. The live overlay
        state is left untouched for the active session.
        """
        if job is None or not text.strip():
            # The stash stays set, so `_finish_transcription_job` writes it.
            return True
        job.stashed_partial = ""
        job.history_entry = self._append_transcript_history(
            text,
            job.settings,
            job.mode,
            source_recording_id=job.source_recording_id,
            source_audio_path=job.source_audio_path,
            track_for_edit=False,
        )
        # The job's own recording is done with, as on the foreground road:
        # keyed by its id, so a newer recording in the slot is left alone,
        # and skipped for bytes the store never received. Unmarked, the
        # state file said "transcribing" for a transcript already in
        # history, and with `save_last_wav` off the audio stayed on disk
        # (the wave-17 concurrency lens, on the real store). Two exceptions:
        # a canceled job's late result keeps the cancel's mark -- the user's
        # decision, and the audio it keeps reachable for Import -- and a
        # transcript the history write refused is kept as audio, since with
        # `save_last_wav` off the completion mark deletes the only copy left.
        if job.history_entry is not None and not job.aborting:
            self._mark_last_recording_completed(job, text)
        if (
            job.background_delivery == CONCURRENT_TRANSCRIPTION_MODE_INSERT
            and job.mode != "streaming"
        ):
            if self._should_defer_background_insertion(job=job):
                job.insertion_deferred = True
                self._deferred_background_results.append((job, text))
                self._update_queue_overlay()
                self._logger.info(
                    "Deferred background transcription insertion until the "
                    "active recording stops. token=%s engine=%s model=%s",
                    job.token,
                    job.engine,
                    job.model,
                )
                return False
            # Through the deferred queue rather than a paste of its own: the
            # flush delivers it at once unless the previous paste's restore
            # window is still open, and then holds it back and joins it with
            # whatever else arrives for the same window. The flush finishes
            # the job itself, so the caller must not.
            job.insertion_deferred = True
            self._deferred_background_results.append((job, text))
            self._flush_deferred_background_results()
            return False
        return True

    def _should_defer_background_insertion(
        self,
        *,
        ignore_active_transcription: bool = False,
        job: _TranscriptionJob | None = None,
    ) -> bool:
        """Whether a completed background result must wait before insertion.

        An in-progress recording start/stop is always a hard blocker. An
        active capture normally is too — except with
        ``immediate_background_insert`` when the finished job targets the
        window that is already in the foreground: pasting there is exactly
        what the user is dictating into and requires no focus steal (see
        ``_can_insert_during_active_recording``). An in-flight foreground
        transcription normally also defers background inserts so the live
        session stays coherent, but an explicit user cancel passes
        ``ignore_active_transcription=True`` to deliver already-completed
        results immediately — each targets its own captured window, and
        delivering the older result now keeps token order intact — instead of
        leaving them stuck (looking "deleted") behind a transcription that can
        take a minute. With ``immediate_background_insert`` enabled, a running
        transcription never defers either: a finished queued result is
        inserted as soon as it completes. Jobs run serially on the single
        worker, so results still arrive (and insert) in token order.
        """
        if self._recording_start_in_progress or self._recording_stop_in_progress:
            return True
        if self._audio_capture is not None:
            return not self._can_insert_during_active_recording(job)
        if ignore_active_transcription:
            return False
        if bool(getattr(self._settings, "immediate_background_insert", False)):
            return False
        return self._active_request_token is not None

    def _can_insert_during_active_recording(
        self,
        job: _TranscriptionJob | None,
    ) -> bool:
        """Whether a finished queued result may paste while a capture runs.

        During a streaming recording only an earlier result for the stream's
        own window, ahead of the stream's first live insert
        (`_stream_lets_earlier_result_go_first`); otherwise live inserts
        write at the caret and a focus change suspends them. During a batch
        recording it requires ``immediate_background_insert``, and then the
        microphone does not care about a paste, the new recording's own
        target was already snapshotted at its start, and focus is restored to
        the finished job's window like in any other delivery. The historical
        failures around inserting near a hotkey press were the held-modifier
        Ctrl+V corruption, which the inserter's modifier-release wait fixed.
        """
        if job is None:
            return False
        if self._streaming_recording:
            return self._stream_lets_earlier_result_go_first(job)
        return bool(getattr(self._settings, "immediate_background_insert", False))

    def _stream_lets_earlier_result_go_first(self, job: _TranscriptionJob) -> bool:
        """Whether an earlier result may paste during a streaming capture.

        Only one for the stream's own window, and only while nothing of the
        stream is in the document (`committed_text` empty) and its window is
        in front: the result then lands where the stream will continue, in
        recording order (owner's rule 2026-10-09), and no focus is taken
        from a window the user switched to. Every other result waits for the
        stream to end, as before. The stream holds its own live inserts
        meanwhile (`_stream_live_insert_held`), independent of
        ``immediate_background_insert``.
        """
        return (
            not self._stream_text_state.committed_text
            and not self._stream_abort_requested
            and self._same_order_window(job.target_handle, self._target_window_handle)
            and self._is_stream_target_active()
        )

    def _same_order_window(self, handle: int | None, other: int | None) -> bool:
        """Whether two results go to one window, as far as their order goes.

        By the top-level window, not the coalescing key's focus and caret: a
        second text field of the same window is the same window to the
        user. With `current_window` insertion every result goes to whatever
        has the focus when it is pasted, so all of them count as one.
        """
        return self._insert_target_is_current_window() or handle == other

    def _streaming_window_has_focus(self) -> bool:
        """Whether a re-paste now would go into the streaming dictation's
        window, by the top-level window as the order rule counts it.

        An unknown answer -- no foreground, no stream target -- counts as
        yes: holding a paste until the stream ends costs a moment, landing it
        inside the streamed words costs the user a cleanup.
        """
        signature = self._current_focus_signature()
        current = signature[0] if signature else None
        stream_window = self._target_window_handle
        if not current or not stream_window:
            return True
        return self._same_order_window(current, stream_window)

    def _earlier_result_waits_for(
        self, handle: int | None, *, before: int | None = None
    ) -> bool:
        """Whether a result for window ``handle`` is still to be pasted.

        Jobs older than token ``before`` -- every job during a capture,
        whose own job does not exist until it stops -- that will paste
        their result: an insert delivery that was not stopped, and not a
        streaming finalize (its words went in live) unless it waits in the
        paste queue (`insertion_deferred`: nothing of it was inserted live,
        so it is pasted like a batch result). Transcribing, or done and held
        in the paste queue: both are still to come. One that fails, is
        stopped or is delivered to history leaves `_jobs` or stops
        matching, and holds nothing back any more.
        """
        return any(
            (before is None or job.token < before)
            and not job.aborting
            and (job.mode != "streaming" or job.insertion_deferred)
            and job.background_delivery == CONCURRENT_TRANSCRIPTION_MODE_INSERT
            and self._same_order_window(job.target_handle, handle)
            for job in self._jobs.values()
        )

    def _stream_live_insert_held(self) -> bool:
        """Whether the streaming dictation's next live insert must wait.

        Order per target window (owner's rule 2026-10-09): a batch result
        recorded before this dictation and still to be pasted into its window
        goes first, so the live words wait for it; when it is already done
        it is given the chance to go now
        (`_stream_lets_earlier_result_go_first`). An earlier result for
        another window holds nothing.
        The first live insert also waits for the previous paste's restore
        window (`_paste_pace_wait_s`): right behind the earlier result's
        paste it would overwrite a clipboard the window may read late. The
        words are not lost: `live_text` stays current and the next allowed
        insert, or the finalize, carries them.
        """
        handle = self._target_window_handle
        if self._deferred_background_results and self._earlier_result_waits_for(handle):
            self._flush_deferred_background_results()
        if self._stream_waits_for_paste_pace and self._paste_pace_wait_s() <= 0.0:
            self._stream_waits_for_paste_pace = False
        held = self._earlier_result_waits_for(handle) or (
            (
                not self._stream_text_state.committed_text
                or self._stream_waits_for_paste_pace
            )
            and self._paste_pace_wait_s() > 0.0
        )
        if held != self._stream_insert_held:
            self._stream_insert_held = held
            self._logger.info(
                "streaming_insertion_%s",
                "held: an earlier result or its paste comes first"
                if held
                else "released",
            )
        return held

    def _insert_target_is_current_window(self) -> bool:
        return (
            str(getattr(self._settings, "insert_target", DEFAULT_INSERT_TARGET))
            == INSERT_TARGET_CURRENT_WINDOW
        )

    def _resolve_insert_target(
        self,
        handle: int | None,
        signature: FocusSignature | None,
    ) -> tuple[int | None, FocusSignature | None]:
        """Apply the insert_target setting to a job's captured target.

        With ``current_window`` the transcript goes to whatever is focused at
        insert time; the recording-start snapshot stays the fallback when the
        current focus cannot be read.
        """
        if not self._insert_target_is_current_window():
            return handle, signature
        current_signature = self._current_focus_signature()
        current_handle = (
            current_signature[0]
            if current_signature is not None
            else self._current_foreground_window()
        )
        if current_signature is None and not current_handle:
            return handle, signature
        return current_handle or handle, current_signature or signature

    def _insert_background_transcription(
        self,
        job: _TranscriptionJob,
        text: str,
        *,
        job_count: int = 1,
        parts: Sequence[tuple[TranscriptHistoryEntry | None, str]] = (),
    ) -> tuple[bool, bool]:
        """Paste one delivered background transcript.

        Returns ``(inserted, claimed_overlay)``. The second half is what a
        caller that writes its own overlay state afterwards needs: a failure
        here paints an Error carrying the transcript and the Insert action, and
        overwriting it a statement later leaves the text in history alone.
        ``parts`` are a coalesced paste's results with their entries, which a
        row for it keeps (`_UndeliveredInsert.parts`).
        """
        target_handle, target_signature = self._resolve_insert_target(
            job.target_handle, job.target_signature
        )
        inserted = self._insert_text_at_target(
            text,
            restore_focus=True,
            copy_on_error=False,
            target_handle=target_handle,
            target_signature=target_signature,
            show_overlay_error=False,
        )
        if inserted:
            # The last text that reached a window, which the re-paste pastes
            # while the overlay keeps showing the foreground transcript. A
            # coalesced paste delivered its joined text and has no single
            # history entry.
            self._delivered_after_shown = (
                text,
                job.history_entry if job_count == 1 else None,
            )
            self._check_paste_target(
                self._paste_check(
                    text,
                    job.history_entry if job_count == 1 else None,
                    created_at=job.created_at,
                    identity=self._queued_paste_identity(job, job_count),
                    background=True,
                    takes_shown_pair=True,
                    parts=parts,
                )
            )
            return True, False
        claimed = self._report_background_insertion_failure(
            job,
            text,
            job_count=job_count,
            parts=parts,
        )
        return False, claimed

    def _queued_paste_identity(self, job: _TranscriptionJob, job_count: int) -> str:
        """The subject of a queued paste's report, with its own verb."""
        if job_count > 1:
            return f"{job_count} queued transcriptions were"
        return f"{self._job_identity(job)} was"

    def _report_background_insertion_failure(
        self,
        job: _TranscriptionJob,
        text: str,
        *,
        job_count: int = 1,
        parts: Sequence[tuple[TranscriptHistoryEntry | None, str]] = (),
    ) -> bool:
        """Surface a queued transcript that was produced but not pasted.

        The transcription itself succeeded, so nothing is retryable and the
        text is safe in history — but silence here is indistinguishable from a
        successful insert, which is exactly how a transcript goes missing
        unnoticed. Report it the same way a failed background transcription is
        reported: always a tray notification, plus the overlay when no live
        session owns it.
        """
        self._logger.warning(
            "background_insertion_failed; saved to history only. token=%s "
            "mode=%s engine=%s model=%s jobs=%d",
            job.token,
            job.mode,
            job.engine,
            job.model,
            job_count,
        )
        identity = self._queued_paste_identity(job, job_count)
        # The same distinction the foreground path makes: two failure paths
        # run *after* the paste keystroke, so the text is probably already
        # in the document. Claiming it "could not be inserted" and offering
        # an Insert button then pastes it a second time -- the duplicate
        # paste this class of bug keeps producing.
        may_have_pasted = bool(self._last_insert_may_have_pasted)
        if may_have_pasted:
            # `identity` already ends in its own verb ("... was" / "... were").
            message = (
                f"{identity} inserted, but the clipboard could not be "
                "restored afterwards. Check the target window before "
                "inserting it again. The text is saved in history."
            )
        else:
            message = (
                f"{identity} transcribed but could not be inserted. "
                "The text is saved in history."
            )
        # Listed until it is inserted or dismissed, whatever arrives after
        # it: the overlay's single offer below is replaced by the next
        # failure and the next recording, and is not painted at all while a
        # session owns the overlay.
        entry = self._record_undelivered_insert(
            text,
            may_have_pasted=may_have_pasted,
            created_at=job.created_at,
            history_entry=job.history_entry if job_count == 1 else None,
            parts=parts,
        )
        hint = self._undelivered_hint()
        if hint:
            message = f"{message} {hint}"
        self.background_insertion_failed.emit(message)
        if self._overlay_session_active() or self._foreground_delivery_pending:
            # A newer session owns the overlay (or is one statement away from
            # claiming it); its own transcript must stay the one that
            # Copy/Insert act on. The notification above is the report then.
            return False
        # Nothing newer is on screen, so this transcript becomes what the
        # overlay shows — and therefore what Copy and Insert act on. The
        # Edit target moves with the text: one queued transcript brings its
        # own entry, a coalesced paste of several has no single entry and
        # Edit refuses rather than writing onto an older dictation's.
        self._paint_insert_offer(
            message,
            text.strip(),
            row=entry,
            history_entry=job.history_entry if job_count == 1 else None,
            may_have_pasted=may_have_pasted,
            takes_shown_pair=True,
        )
        return True

    def _paint_insert_offer(
        self,
        message: str,
        transcript: str,
        *,
        row: _UndeliveredInsert | None,
        history_entry: TranscriptHistoryEntry | None,
        may_have_pasted: bool,
        takes_shown_pair: bool,
    ) -> None:
        """Show a transcript whose paste failed or is doubtful, with its offer.

        Insert is offered unless the keystroke may have pasted it already;
        it pastes exactly ``transcript`` and retires ``row`` on success.
        """
        if takes_shown_pair:
            self._set_last_transcript(transcript, history_entry)
        self._insert_action_text = transcript
        self._insert_action_rows = (row,) if row is not None else ()
        self._insert_action_entry = history_entry
        self._shown_transcript_row = row
        self._insert_offer_may_have_pasted = may_have_pasted
        detail = f"{message}\n\n{transcript}" if transcript else message
        # One value for Copy and Insert; a raw text here and the stripped
        # one there would let the two act on different strings.
        self._overlay.set_state(
            "Error",
            detail,
            copy_text=transcript,
            error_action=(
                OVERLAY_ERROR_ACTION_NONE
                if may_have_pasted
                else OVERLAY_ERROR_ACTION_INSERT
            ),
            editable=self._edit_reaches(
                transcript, (row,) if row is not None else (), history_entry
            ),
        )
        self._reveal_overlay_result(is_error=True)

    # -- Paste target check ---------------------------------------------------

    def _paste_check(
        self,
        text: str,
        history_entry: TranscriptHistoryEntry | None,
        *,
        created_at: datetime,
        identity: str,
        background: bool,
        takes_shown_pair: bool,
        parts: Sequence[tuple[TranscriptHistoryEntry | None, str]] = (),
    ) -> _PasteCheck:
        """The check of the paste that just reported success.

        Built after the paste's own overlay paint, so a doubtful report
        paints over exactly what the user saw for it and nothing newer --
        and only when that is this paste's own "Done" or an idle overlay.
        A queued paste paints nothing: the overlay then shows another job's
        result -- a failed paste's or a streaming tail's Insert offer, whose
        tail has no row -- and painting over it took that Insert away (the
        2026-10-03 review). A re-paste that kept another text's offer on
        screen (`_paint_status_keeping_offer`) is the same case.
        """
        shown = (self._overlay.state, self._overlay.detail)
        own = shown[0] == "Idle" or shown == ("Done", text)
        return _PasteCheck(
            text=text,
            history_entry=history_entry,
            created_at=created_at,
            identity=identity,
            background=background,
            overlay_shown=shown if own else None,
            takes_shown_pair=takes_shown_pair,
            foreground=self._last_insert_foreground,
            paste_serial=self._paste_serial,
            capture=self._audio_capture,
            parts=tuple(parts),
        )

    def _check_paste_target(
        self, pending: _PasteCheck, *, tone_if_refused: bool = True
    ) -> None:
        """A paste reported success: check its target, then play the tone.

        A SendInput Ctrl+V into a focused button, list item or page body
        reports success and inserts nothing (the r27 investigation: Edge
        fires its paste event even there). `PasteTargetCheck` asks on its
        worker whether the focus shows a caret; its answer arrives through
        `paste_target_checked`. Without a check -- none configured, one
        still running, a thread that cannot start -- the tone plays at once
        and nothing else changes; a re-paste passes ``tone_if_refused``
        False, since its check is refused exactly when it repeats a paste
        whose check still runs, and the tone would confirm nothing (a check
        that raised is not a refusal and always plays it). Nothing here
        waits.

        A check still waiting for a paste of the same text into the same
        window is dropped once this paste's own check has started: this
        paste repeated it, so the earlier verdict -- read before it -- would
        report as missing a text the user has just pasted again. When this
        check is refused, the earlier one stays and decides; dropping it
        then left neither paste with a verdict (the second review).
        """
        checker = self._paste_target_check
        if checker is None or self._shutdown_started:
            self._play_completion_beep()
            return
        self._paste_check_counter += 1
        check_id = self._paste_check_counter
        emit = self.paste_target_checked.emit
        try:
            started = checker.request(
                lambda reading: emit(check_id, reading.verdict, reading.evidence()),
                expected_foreground=pending.foreground,
            )
        except Exception:
            self._logger.exception("paste_target_check could not start")
            started = False
            # Not a refusal: no earlier check holds the worker, so there is no
            # verdict to wait for and the tone is as due as for any other paste.
            tone_if_refused = True
        if not started:
            self._log_paste_target_check(
                check_id, VERDICT_UNKNOWN, "note=not_started", pending
            )
            if tone_if_refused:
                self._play_completion_beep()
            return
        repeated = [
            waiting_id
            for waiting_id, waiting in self._paste_checks.items()
            if waiting.text.strip() == pending.text.strip()
            and waiting.foreground == pending.foreground
        ]
        for waiting_id in repeated:
            del self._paste_checks[waiting_id]
            self._logger.info("paste_target_check id=%d dropped=repeated", waiting_id)
        self._paste_checks[check_id] = pending
        QtCore.QTimer.singleShot(
            PASTE_TARGET_CHECK_TIMEOUT_MS,
            self,
            lambda: self._on_paste_target_checked(
                check_id, VERDICT_UNKNOWN, "note=timeout"
            ),
        )

    def _log_paste_target_check(
        self, check_id: int, verdict: str, evidence: str, pending: _PasteCheck
    ) -> None:
        """One INFO line per check: verdict, evidence, never the text.

        `evidence` is `CaretReading.evidence()` -- class names and caret
        answers -- or the reason no reading exists, so the log of real use
        shows which applications the check misjudges.
        """
        self._logger.info(
            "paste_target_check id=%d verdict=%s %s background=%s chars=%d",
            check_id,
            verdict,
            evidence,
            pending.background,
            len(pending.text),
        )

    @QtCore.Slot(int, str, str)
    def _on_paste_target_checked(
        self, check_id: int, verdict: str, evidence: str
    ) -> None:
        """The check's answer, or its timeout -- whichever comes first."""
        pending = self._paste_checks.pop(check_id, None)
        if pending is None:
            return
        self._log_paste_target_check(check_id, verdict, evidence, pending)
        if self._shutdown_started:
            return
        if verdict != VERDICT_NOT_TEXT_FIELD:
            if self._recording_started_since(pending):
                self._logger.info(
                    "completion_tone_skipped id=%d reason=recording", check_id
                )
                return
            self._play_completion_beep()
            return
        self._report_paste_outside_text_field(pending)

    def _recording_started_since(self, pending: _PasteCheck) -> bool:
        """Whether a recording other than the paste's own began meanwhile.

        Before the check the tone played right after the paste; waiting up
        to `PASTE_TARGET_CHECK_TIMEOUT_MS` for the verdict, it could play
        into the microphone of a recording the user started in between.
        """
        capture = self._audio_capture
        return self._recording_start_in_progress or (
            capture is not None and capture is not pending.capture
        )

    def _report_paste_outside_text_field(self, pending: _PasteCheck) -> None:
        """Report a paste whose focus did not look like a text field.

        No completion tone. Listed like a failed paste and insertable: the
        check can be wrong (an editor drawing its own caret), so the user
        decides after looking at the window, and a re-paste that inserts
        the text retires the row. The overlay shows it, with Insert, while
        it still shows what it showed right after the paste; otherwise --
        and for every queued paste -- the tray carries it. A verdict that
        arrives after a later paste went out lists no row and paints
        nothing: that paste superseded it (`_drop_superseded_doubtful_rows`).
        """
        transcript = pending.text.strip()
        superseded = pending.paste_serial != self._paste_serial
        entry = (
            None
            if superseded
            else self._record_undelivered_insert(
                transcript,
                may_have_pasted=False,
                created_at=pending.created_at,
                history_entry=pending.history_entry,
                outside_text_field=True,
                parts=pending.parts,
            )
        )
        message = (
            f"{pending.identity} pasted, but the focused element does not "
            "look like a text field."
        )
        overlay_free = (
            not superseded
            and not self._overlay_session_active()
            and not self._foreground_delivery_pending
            # A None snapshot (not this paste's screen) never matches.
            and (self._overlay.state, self._overlay.detail) == pending.overlay_shown
        )
        if pending.background or not overlay_free:
            tray = (
                f"{message} It may not have been inserted; the text is saved "
                "in history."
            )
            hint = self._undelivered_hint()
            if hint:
                tray = f"{tray} {hint}"
            self.background_insertion_failed.emit(tray)
        if not overlay_free:
            return
        self._paint_insert_offer(
            f"{message} If nothing appeared, click into the field and press Insert.",
            transcript,
            row=entry,
            history_entry=pending.history_entry,
            may_have_pasted=False,
            takes_shown_pair=pending.takes_shown_pair,
        )

    def _flush_deferred_background_results(
        self,
        *,
        ignore_active_transcription: bool = False,
        hold_key: tuple[object, object] | None = None,
    ) -> bool:
        """Deliver the completed results nothing is blocking any more.

        Returns True when a failed insert claimed the overlay's Error state.
        A caller that writes its own state afterwards has to know: an Error
        painted here carries the transcript and the Insert action that is the
        only way to recover it, and overwriting that one statement later left
        the transcript in history alone.

        The paste pace: a group that would paste while the previous paste's
        clipboard-restore window is still open (`_paste_pace_wait_s`) stays
        queued, marked `paste_paced`, and `_paste_pace_timer` flushes again
        when the window ends, so everything that arrived meanwhile for the
        same window goes out as one paste. A second paste inside the window
        overwrote the clipboard while the first target -- a renderer that
        reads it late -- had not read it yet. The window is global, because
        the clipboard is: a late reader of one window's paste reads whatever
        the clipboard holds then. Streaming live inserts never come through
        here; they pace themselves through the locked prefix. ``hold_key``
        holds that one group back unpasted for a foreground result that is
        about to join it.
        """
        if not self._deferred_background_results:
            return False
        single_group = self._insert_target_is_current_window()
        # Deferral is per job: with an active capture, only results targeting
        # the current foreground window may insert (immediate mode); the rest
        # stay queued for the next flush. A paced result waits for nothing but
        # the restore window -- it may be the foreground result itself, and a
        # transcription started since must not hold it back.
        pending = []
        still_deferred = []
        relisted = False
        for job, text in sorted(
            self._deferred_background_results, key=lambda item: item[0].token
        ):
            if self._should_defer_background_insertion(
                ignore_active_transcription=(
                    ignore_active_transcription or job.paste_paced
                ),
                job=job,
            ):
                # Deferred by a recording or a window again: listed as before.
                relisted = relisted or job.pace_held
                job.pace_held = False
                still_deferred.append((job, text))
            else:
                pending.append((job, text))
        self._deferred_background_results = still_deferred
        if not pending:
            if relisted:
                self._update_queue_overlay()
            return False
        # Coalesce results that target the same window into one paste: each
        # separate paste is its own clipboard set/paste/restore cycle and thus
        # its own race window against the target app, so six queued results
        # used to mean six chances to lose one.
        claimed_overlay = False
        paced_wait_s = 0.0
        for pairs, text, key in self._coalesced_deferred_inserts(
            pending,
            single_group=single_group,
        ):
            jobs = [job for job, _text in pairs]
            held = key == hold_key
            wait_s = 0.0 if held else self._paste_pace_wait_s()
            if held or wait_s > 0.0:
                for job in jobs:
                    job.insertion_deferred = True
                    job.paste_paced = job.paste_paced or not held
                    job.pace_held = job.pace_held or not held
                self._deferred_background_results.extend(pairs)
                if not held:
                    paced_wait_s = max(paced_wait_s, wait_s)
                    self._logger.info(
                        "paste_paced wait_ms=%d joined=%d tokens=%s",
                        _pace_ms(wait_s),
                        len(jobs),
                        [job.token for job in jobs],
                    )
                continue
            for job in jobs:
                job.insertion_deferred = False
            # Each result with its own entry, so an edit of one of them
            # reaches a row the joined paste leaves behind.
            parts = (
                tuple((job.history_entry, part.strip()) for job, part in pairs)
                if len(jobs) > 1
                else ()
            )
            if len(jobs) > 1:
                self._logger.info(
                    "Coalescing %d deferred transcription inserts into one "
                    "paste. tokens=%s",
                    len(jobs),
                    [job.token for job in jobs],
                )
            try:
                _inserted, claimed = self._insert_background_transcription(
                    jobs[0],
                    text,
                    job_count=len(jobs),
                    parts=parts,
                )
                claimed_overlay = claimed or claimed_overlay
            except Exception:
                self._logger.exception(
                    "Failed to insert deferred background transcription; "
                    "saved to history only. tokens=%s",
                    [job.token for job in jobs],
                )
                claimed_overlay = (
                    self._report_background_insertion_failure(
                        jobs[0],
                        text,
                        job_count=len(jobs),
                        parts=parts,
                    )
                    or claimed_overlay
                )
            for job in jobs:
                self._finish_transcription_job(job.token)
        if paced_wait_s > 0.0:
            # Restarted, never left running from an earlier hold: every flush
            # reads the window afresh, and the last read is the one to keep.
            self._paste_pace_timer.start(_pace_ms(paced_wait_s))
        # The held rows now read "Pending insert".
        self._update_queue_overlay()
        return claimed_overlay

    def _coalesced_deferred_inserts(
        self,
        pending: list[tuple[_TranscriptionJob, str]],
        *,
        single_group: bool = False,
    ) -> list[tuple[list[tuple[_TranscriptionJob, str]], str, tuple[object, object]]]:
        """Group token-ordered deferred results by their insertion target.

        Each group is ``(its results, their joined text, its target key)``.
        """
        groups: list[tuple[list[tuple[_TranscriptionJob, str]], tuple]] = []
        index_by_target: dict[tuple, int] = {}
        for job, text in pending:
            key = self._paste_group_key(job, single_group=single_group)
            index = index_by_target.get(key)
            if index is None:
                index_by_target[key] = len(groups)
                groups.append(([(job, text)], key))
            else:
                groups[index][0].append((job, text))
        return [
            (pairs, _join_transcripts([text for _job, text in pairs]), key)
            for pairs, key in groups
        ]

    @QtCore.Slot(int)
    def _on_transcription_canceled_result(self, request_token: int) -> None:
        """A worker confirmed it stopped before producing a transcript."""
        if self._shutdown_started:
            return
        self._drop_request_audio(request_token)
        if self._active_request_token == request_token:
            self._active_request_token = None
            self._last_transcribe_settings = None
        self._finish_transcription_job(request_token)
        self._flush_deferred_background_results()

    @QtCore.Slot(int, str)
    def _on_transcription_failed_result(
        self,
        request_token: int,
        error_text: str,
    ) -> None:
        if self._shutdown_started:
            return
        with self._overlay_batch():
            self._on_transcription_failed(error_text, request_token=request_token)

    def _job_identity(self, job: _TranscriptionJob | None) -> str:
        """Short, user-facing identity of a queued transcription."""
        if job is None:
            return "A queued transcription"
        engine = (job.engine or "").strip() or "transcriber"
        model = (job.model or "").strip()
        provider = f"{engine} · {model}" if model else engine
        return f"Recording {job.created_at.strftime('%H:%M:%S')} ({provider})"

    def _report_background_failure(
        self,
        job: _TranscriptionJob | None,
        error_text: str,
        retry_available: bool,
    ) -> None:
        message = f"{self._job_identity(job)} failed: {error_text}"
        if retry_available:
            message = f"{message} The audio was kept — use Retry to try again."
        self._logger.warning("background_transcription_failed %s", message)
        # Always notify (the tray notification survives an active session);
        # additionally show it on the overlay when no live session owns it.
        self.background_transcription_failed.emit(message)
        if not self._overlay_session_active():
            # Retry is right only for the failure's own promoted audio.
            self._overlay.set_state(
                "Error",
                message,
                error_action=None if retry_available else OVERLAY_ERROR_ACTION_NONE,
            )
            self._reveal_overlay_result(is_error=True)

    def _on_transcription_failed(
        self,
        error_text: str,
        *,
        request_token: int | None = None,
    ) -> None:
        # Whether this failure's own audio is in the retry slot, which is
        # what the Error's Retry button and its guidance are about. It
        # started as "the slot holds something", which for a failure with
        # no audio of its own described, and offered a Retry of, an older
        # failure (the wave-16 reach lens).
        preserved_audio = False
        job: _TranscriptionJob | None = None
        if request_token is not None:
            job = self._jobs.get(request_token)
            if not self._is_foreground_transcription(request_token, job):
                # A queued transcription failed while a newer session is
                # active. Keep the live session's overlay state untouched and
                # keep the audio for a manual retry, but never let the failure
                # pass silently: an unreported failure looks exactly like a
                # recording that was simply never transcribed.
                retry_available = self._promote_request_audio_for_retry(
                    request_token, job
                )
                # Keyed like the foreground's mark. The recovery prompt at
                # the next start reads "failed", and an unmarked queued
                # failure left "transcribing" behind and was never offered.
                # A canceled job's failure keeps the cancel's mark: the user
                # ended it, and the prompt must not offer it back.
                if job is not None and not job.aborting:
                    self._mark_last_recording_failed(job, error_text)
                self._report_background_failure(job, error_text, retry_available)
                # The same guarded clear both sibling terminal handlers do.
                # This one did not, and a job is non-foreground while it is
                # still the active token whenever a new recording is starting
                # -- so a failure delivered in that window left the token set
                # to a job that no longer exists. If the new recording then
                # submits nothing (silence-gated, no audio captured, cancelled,
                # a watchdog abort), it stays set for the rest of the session:
                # `_should_defer_background_insertion` ends in "is the token
                # set", so every later queued transcript is deferred and never
                # pasted, and `_overlay_session_active()` answers True forever,
                # which is what makes `show_idle_status` swallow a failed
                # hotkey registration.
                if self._active_request_token == request_token:
                    self._active_request_token = None
                self._finish_transcription_job(request_token)
                self._flush_deferred_background_results()
                return
            self._active_request_token = None
            # A failure whose audio was retained replaces the slot; one with
            # no bytes of its own leaves the previous failure retryable.
            # Clearing it here discarded a queued dictation's only copy
            # (wave 15; `_retire_retry_audio_delivered_by`).
            preserved_audio = self._promote_request_audio_for_retry(request_token, job)

        # The recording this session owns in the store: the job's own, and
        # for the live stream's death the id its teardown's persist hands
        # back below -- "" when nothing was persisted or the write failed,
        # where the store's slot is the previous recording's, which the
        # history entry below then named as its audio.
        session_recording_id = job.source_recording_id if job is not None else ""
        # Whether the store's last recording is this session's: the job's
        # own answer, and for the live stream's death the persist below.
        owns_last_recording = job is not None and job.marks_last_recording
        self._finish_transcription_job(request_token)
        self._focus_poll_timer.stop()
        runtime_stream_failed = (
            self._audio_capture is not None
            or self._active_stream_transcriber is not None
            or self._streaming_recording
        )
        # A dying stream runtime must keep what was already transcribed, exactly
        # like an explicit abort does: without this the text existed only as the
        # part already pasted into the target window, with nothing in history and
        # nothing for the overlay Copy action. Read before the reset below wipes
        # the streaming text state.
        partial_transcript = ""
        partial_source_audio_path = ""
        partial_settings = self._active_stream_settings or replace(self._settings)
        pending_finalize = None
        if runtime_stream_failed:
            # Only the *history write* is conditional. The teardown must always
            # run: gating it too abandoned a live capture, its transcriber and
            # its runtime lease, so the microphone kept recording after the
            # overlay already said Error.
            pending_finalize = self._pending_streaming_job()
            if pending_finalize is None:
                partial_transcript = self._current_streaming_partial_text()
            else:
                # A finalize already in flight will deliver this session's text
                # itself; saving the partial too would write two history
                # entries for one dictation. Providers do reach that state:
                # AssemblyAI and Deepgram both record a socket error and still
                # return the accumulated text from stop_stream().
                #
                # But "it will deliver" is not "it did": a finalize that then
                # returns nothing left the whole dictation nowhere, because the
                # reset below had already wiped the live text and the rescue in
                # `_on_transcription_ready` reads the same emptied state. Stash
                # it on the job instead -- `_finish_transcription_job` writes a
                # stash that nothing cleared, and every path that delivers real
                # text clears it first. Exactly what `_request_job_stop` does
                # in the same situation.
                pending_finalize.stashed_partial = (
                    self._current_streaming_partial_text()
                    or pending_finalize.stashed_partial
                )
            wav_bytes, partial_source_audio_path = (
                self._teardown_active_stream_runtime()
            )
            if wav_bytes:
                # "" when this write failed, and a retry of these bytes then
                # marks nothing; the store then holds the previous recording,
                # which this session does not own.
                owns_last_recording = self._persist_last_recording_audio(wav_bytes)
                self._hold_failed_audio_for_retry(
                    wav_bytes, self._last_persisted_recording_id
                )
                session_recording_id = self._last_persisted_recording_id
                preserved_audio = True
        self._streaming_recording = False
        self._active_stream_transcriber = None
        self._active_stream_settings = None
        self._last_transcribe_settings = None
        # Spare the two fields the pending finalize is measured against. With
        # them cleared it took the *batch* branch of `_on_transcription_ready`
        # and pasted the whole dictation on top of the text already inserted
        # live -- and the streaming branch would have done the same, because
        # `finalize_append_only` computes its insertion against a
        # `committed_text` the reset had emptied.
        self._reset_streaming_state(keep_session_text=pending_finalize is not None)
        kept_detail = ""
        if partial_transcript.strip():
            self._append_transcript_history(
                partial_transcript,
                partial_settings,
                "streaming",
                source_recording_id=session_recording_id,
                source_audio_path=partial_source_audio_path,
            )
            self._last_transcript = partial_transcript
            kept_detail = " The text transcribed so far was saved to history."
        # The failed session no longer blocks queued inserts; flush after the
        # stream/capture teardown above so a deferred result is not left
        # pending behind a capture that was just removed.
        self._flush_deferred_background_results()
        if job is not None:
            self._mark_last_recording_failed(job, error_text)
        elif owns_last_recording:
            # The live session's own recording, written by the persist above
            # and keyed by the id it handed back. A session that persisted
            # nothing -- no audio, a refused write -- owns no recording in
            # the store, and the unconditional mark relabelled the previous
            # one.
            self._mark_last_recording_failed(
                None,
                error_text,
                session_recording_id=self._last_persisted_recording_id,
            )
        guidance = self._retry_guidance(
            has_retry_audio=preserved_audio,
            owns_last_recording=owns_last_recording,
        )
        self._overlay.set_state(
            "Error",
            f"{error_text} {guidance}{kept_detail}",
            copy_text=partial_transcript or None,
            # `None` is the Retry button, which transcribes the slot: with no
            # audio of its own this failure leaves the slot to an older
            # failure, and the button transcribed that recording under an
            # Error about this one (the wave-16 reach lens). The tray's
            # "Retry transcription" keeps reaching the slot.
            error_action=None if preserved_audio else OVERLAY_ERROR_ACTION_NONE,
        )
        self._reveal_overlay_result(is_error=True)

    @QtCore.Slot(str)
    def _on_transcription_partial(self, partial_text: str) -> None:
        if self._shutdown_started:
            return
        if not self._streaming_recording or self._audio_capture is None:
            return
        if self._stream_abort_requested:
            return
        text = normalize_stream_text(partial_text)
        if not text:
            return
        display_text = text
        if STREAMING_ABORT_ON_FOCUS_CHANGE and not self._is_stream_target_active():
            self._request_stream_abort(
                "Streaming aborted: target window focus changed.",
                beep=STREAMING_BEEP_ON_ABORT,
            )
            return
        if STREAMING_LIVE_INSERT_ENABLED:
            # The timer ticks every STREAMING_FOCUS_POLL_MS; a partial that
            # arrives between a window switch and the next tick must not
            # paste into the new window, so the same check runs here first.
            self._on_stream_focus_poll()
        if (
            STREAMING_LIVE_INSERT_ENABLED
            and not self._stream_insertion_suspended
            and not self._stream_live_insert_held()
        ):
            previous_committed = self._stream_text_state.committed_text
            append = self._stream_text_state.apply_partial_append_only(text)
            display_text = append.display_text
            if append.insertion:
                if self._insert_text_at_target(
                    append.insertion,
                    restore_focus=False,
                    copy_on_error=False,
                    show_overlay_error=False,
                ):
                    self._stream_insert_failures = 0
                elif self._last_insert_may_have_pasted:
                    # The keystroke already went out and only the cleanup
                    # failed, so the words are probably in the document. Keep
                    # the commit: offering them again would paste them twice,
                    # which is worse than a missing clipboard restore.
                    self._stream_insert_failures = 0
                    self._logger.warning(
                        "Live insert reported a post-paste failure; keeping the "
                        "commit so the text is not inserted twice."
                    )
                else:
                    # Do not end the dictation over one failed paste. The usual
                    # cause is a modifier key still held down, which turns the
                    # injected Ctrl+V into Ctrl+Alt+V — transient, and fatal
                    # only because this used to abort. Take the commit back so
                    # the same words are offered again on the next partial.
                    self._stream_text_state.rollback_commit(previous_committed)
                    self._stream_insert_failures += 1
                    if (
                        self._stream_insert_failures
                        >= STREAMING_LIVE_INSERT_RETRY_LIMIT
                    ):
                        # With the last failure's reason: when it is the
                        # same every time (WM_PASTE refused for a Chromium
                        # window), it names the fix.
                        message = (
                            "Streaming aborted: the target window kept "
                            "rejecting inserted text."
                        )
                        self._request_stream_abort(
                            f"{message} {self._last_insert_error_text}".strip(),
                            beep=STREAMING_BEEP_ON_ABORT,
                        )
                        return
                    self._logger.debug(
                        "Live insert failed (%d/%d); retrying on the next partial.",
                        self._stream_insert_failures,
                        STREAMING_LIVE_INSERT_RETRY_LIMIT,
                    )
                    return
        else:
            # Suspended for another window, or held for an earlier result.
            # Keep the live text current even though nothing is pasted.
            # `_current_streaming_partial_text` prefers `live_text`, so
            # leaving it stale made an abort or a dropped socket save the
            # text from before the window switch and silently drop
            # everything dictated after it. Only `committed_text` stays
            # where it is -- that tracks what actually reached a document,
            # and the finalize inserts the whole tail past it at stop.
            self._stream_text_state.live_text = display_text
            self._stream_text_state.last_partial_text = display_text
            self._stream_last_partial_text = text
        if len(display_text) > STREAMING_OVERLAY_MAX_CHARS:
            display_text = display_text[-STREAMING_OVERLAY_MAX_CHARS:]
            display_text = f"...{display_text}".strip()
        self._overlay.set_state("Listening", f"Live: {display_text}")

    @QtCore.Slot(object, str)
    def _on_stream_runtime_failed(self, token: object, error_text: str) -> None:
        """Handle a streaming runtime failure of the session `token` names.

        The token is checked before anything else, because the activity test
        below cannot tell whose failure this is: it is satisfied by *any* live
        capture, so a provider that fired `on_error` after its session was
        cancelled tore down the ordinary batch recording the user had started
        since -- the microphone stopped and the overlay went from Listening to
        Error. The real Nemotron worker produced exactly that callback for an
        aborted run.

        The activity test is kept behind the identity test rather than
        replaced by it. `_stream_session_token` is set by
        `_begin_stream_connect`, i.e. before the capture exists, and the arm
        that handles a failing `_build_audio_capture` returns without
        `_reset_streaming_state` -- so a token can be current while no session
        is running, and that is the case the activity test still answers.

        A failure of the live session that arrives after the stop is the
        finalize worker's to report. The stop does not retire the session's
        callback -- the provider keeps it wired until its own `stop_stream()`
        takes the lock, and before that the worker is still queued or still
        joining the flush -- so a socket that died in that window passed both
        gates here, and `_on_transcription_failed` painted Error, marked the
        recording failed and reset the session under the worker. The worker's
        `stop_stream()` then delivered its transcript as a second, competing
        success (Done painted over Error, both marks on one recording), or its
        own failure as a second Error. Every provider's `stop_stream()`
        answers a dead socket with the text it has or, having none, with the
        failure it recorded, so the worker's terminal signal already carries
        this failure exactly once.
        """
        if self._shutdown_started:
            return
        if token is not self._stream_session_token:
            self._logger.info(
                "Ignoring a streaming runtime failure from a retired session: %s",
                error_text,
            )
            return
        if self._stream_finalize_pending:
            self._logger.info(
                "stream_runtime_failed_after_stop: the finalize reports it: %s",
                error_text,
            )
            return
        if not (
            self._audio_capture is not None
            or self._active_stream_transcriber is not None
            or self._streaming_recording
        ):
            return
        self._on_transcription_failed(error_text)

    @QtCore.Slot()
    def _on_stream_focus_poll(self) -> None:
        """React to the target window losing focus during a live stream.

        Live insertion writes at the caret, so once another window is in
        front the words would land in the wrong document. Ending the whole
        session for that was too blunt: users switch windows mid-thought,
        and a dictation that was still going lost its remaining flow. The
        session now keeps running with insertion suspended, and everything
        recorded meanwhile is delivered at stop into the window the
        recording started in.

        `STREAMING_ABORT_ON_FOCUS_CHANGE` restores the old hard abort.
        """
        if not self._streaming_recording or self._stream_abort_requested:
            return
        if self._is_stream_target_active():
            if self._stream_insertion_suspended:
                self._stream_insertion_suspended = False
                self._logger.info(
                    "streaming_insertion_resumed: the recording target is in "
                    "front again."
                )
            return
        if STREAMING_ABORT_ON_FOCUS_CHANGE:
            self._request_stream_abort(
                "Streaming aborted: target window focus changed.",
                beep=STREAMING_BEEP_ON_ABORT,
            )
            return
        if self._stream_insertion_suspended:
            return
        self._stream_insertion_suspended = True
        self._logger.info(
            "streaming_insertion_suspended: another window took focus; the "
            "dictation continues and the rest is inserted when it stops."
        )

    @QtCore.Slot(str, bool)
    def _on_stream_abort_requested(self, reason: str, beep: bool) -> None:
        if self._shutdown_started:
            return
        self._abort_streaming_session(
            reason,
            beep=beep,
            finalize_stream=False,
            preserve_audio=True,
        )

    def _request_stream_abort(self, reason: str, beep: bool) -> None:
        if self._stream_abort_requested:
            return
        self._stream_abort_requested = True
        emit_beep = beep
        if beep:
            try:
                threading.Thread(
                    target=self._play_abort_beep,
                    name="stt_app_abort_beep",
                    daemon=True,
                ).start()
                emit_beep = False
            except Exception:
                emit_beep = beep
        self.stream_abort_requested.emit(reason, emit_beep)

    def _abort_streaming_session(
        self,
        reason: str,
        *,
        beep: bool,
        finalize_stream: bool,
        preserve_audio: bool = False,
    ) -> None:
        if beep:
            self._play_abort_beep()

        # Capture the best-known live transcript before the state reset wipes
        # it: an aborted stream used to lose everything already transcribed
        # from the UI and history (only the text pasted so far survived in
        # the target window). A finished transcription is never discarded —
        # the same applies to an aborted one's partial text.
        partial_transcript = self._current_streaming_partial_text()
        partial_settings = self._active_stream_settings or replace(self._settings)

        self._focus_poll_timer.stop()
        capture = self._audio_capture
        self._audio_capture = None
        self._cancel_audio_callback_watchdog(capture)
        wav_bytes = b""
        if capture is not None:
            try:
                # Not the user's stop: no backlog wait (`AudioCapture.stop`).
                wav_bytes = capture.stop(drain=False)
            except Exception:
                self._logger.exception("Failed to stop audio capture during abort")
        source_audio_path = ""
        if capture is not None:
            source_audio_path = self._save_recording_artifacts(capture, wav_bytes)
        # The recording this session owns in the store: the one this persist
        # writes, keyed by the id it hands back; "" when nothing is persisted
        # or the write failed, where the slot is the previous recording's --
        # which the unkeyed mark relabelled canceled and the history entry
        # below named as its audio.
        session_recording_id = ""
        persisted = (
            preserve_audio
            and bool(wav_bytes)
            and self._persist_last_recording_audio(wav_bytes)
        )
        if persisted:
            session_recording_id = self._last_persisted_recording_id
            try:
                self._last_recording_store.mark_canceled(
                    reason, expected_recording_id=session_recording_id or None
                )
            except Exception:
                self._logger.exception("Failed to persist aborted streaming recording")

        transcriber = self._active_stream_transcriber
        self._active_stream_transcriber = None
        runtime_lease = self._active_stream_runtime_lease
        self._active_stream_runtime_lease = None
        try:
            if transcriber is not None:
                if finalize_stream:
                    transcriber.stop_stream()
                elif hasattr(transcriber, "abort_stream"):
                    transcriber.abort_stream()
                else:
                    transcriber.stop_stream()
        except Exception:
            self._logger.exception(
                "Failed to stop/abort streaming transcriber during abort"
            )
        finally:
            if runtime_lease is not None:
                runtime_lease.release()

        self._streaming_recording = False
        self._active_stream_settings = None
        self._reset_streaming_state()
        if partial_transcript.strip():
            self._append_transcript_history(
                partial_transcript,
                partial_settings,
                "streaming",
                source_recording_id=session_recording_id,
                source_audio_path=source_audio_path,
            )
            self._last_transcript = partial_transcript
            # Both paints: the abort never writes the retry slot, so the Retry
            # button (`error_action` None) transcribed whatever older failure
            # the slot held, or answered "No failed transcription to retry";
            # the aborted session's own audio is the last recording file.
            self._overlay.set_state(
                "Error",
                f"{reason} Partial transcript (saved to history): {partial_transcript}",
                error_action=OVERLAY_ERROR_ACTION_NONE,
            )
        else:
            self._overlay.set_state(
                "Error", reason, error_action=OVERLAY_ERROR_ACTION_NONE
            )
        self._reveal_overlay_result(is_error=True)
        # Aborting this session removed the capture that was blocking any
        # deferred background inserts; deliver every completed one now — even if
        # another transcription is still running — instead of leaving them stuck.
        self._flush_deferred_background_results(ignore_active_transcription=True)
        self._maybe_resume_pending_audio_device_refresh()

    def _play_abort_beep(self) -> None:
        try:
            import winsound  # type: ignore
        except ImportError:
            winsound = None

        if winsound is not None:
            try:
                winsound.Beep(STREAMING_ABORT_BEEP_HZ, STREAMING_ABORT_BEEP_DURATION_MS)
                return
            except Exception:
                pass
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                return
            except Exception:
                pass

        try:
            QtGui.QGuiApplication.beep()
        except Exception:
            pass

    def _is_stream_target_active(self) -> bool:
        target_window = self._target_window_handle
        target_signature = self._target_focus_signature
        if not target_window and target_signature is None:
            return True
        current_signature = self._current_focus_signature()
        if current_signature is None:
            return True

        current_foreground, current_focus, current_caret = current_signature
        if target_signature is not None:
            target_foreground, target_focus, target_caret = target_signature
            if (
                target_focus is not None
                and current_focus is not None
                and current_focus != target_focus
            ):
                return False
            if (
                target_caret is not None
                and current_caret is not None
                and current_caret != target_caret
            ):
                return False
            return target_foreground in {None, current_foreground}

        return current_foreground in {None, target_window}

    def _current_foreground_window(self) -> int | None:
        getter = getattr(self._window_focus_helper, "get_foreground_window", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                self._logger.exception("Failed to read foreground window")
                return None
        return self._window_focus_helper.capture_target_window()

    def _refuse_recording_start(self, detail: str) -> None:
        """Paint a refusal without discarding a pending Insert offer.

        Nothing started, so the failed text of the previous Error state is
        still the last thing to recover. The refusal used to replace that
        state outright while the offer had already been cleared at the top of
        `start_recording`, so one refused hotkey press -- typically "Model is
        still loading" right after a dictation -- left the tail of a failed
        streaming finalize reachable only through the whole-dictation
        re-paste, which pastes it on top of the prefix already in the
        document. The pending text stays on screen, with Copy and Insert
        acting on exactly it.
        """
        self._paint_status_keeping_offer("Error", detail)

    def _paint_status_keeping_offer(
        self, state: str, detail: str, *, compact: bool | None = None
    ) -> None:
        """Write a status that is not a session result, keeping the offer.

        `_insert_action_text` is the text of an insert that failed, and the
        overlay's Insert button -- shown only by an Error state carrying
        `OVERLAY_ERROR_ACTION_INSERT` -- is the one entry point that pastes
        exactly that text (the tray re-paste pastes the whole dictation,
        which for a streaming tail lands on top of the prefix already in the
        document). Every status writer that is not itself a result therefore
        goes through here: the idle line, the hotkey notices, "Nothing to
        cancel." and "Transcription canceled.", the preload's start,
        progress, cancel and ready/failed lines, the tray's copy notices,
        the Edit and Retry refusals, and the re-paste refusals. Each of
        them used to paint plainly and hide the button while the text stayed
        pending, so a save, a resume, a Cancel press, a queue row's X, a
        finished preload, a tray copy or "No window to insert into" made the
        tail unrecoverable.

        The offer carries its own action. The insert paths that fail *after*
        the paste keystroke went out -- six raise sites of
        `TextMayHaveBeenPastedError` in `text_inserter.py`: four literal,
        one through the `combined_error` alias and one through the
        `_ClipboardContentionAfterPaste` subclass -- deliberately
        withhold Insert, because the text is most likely in the document
        already; a repaint that read
        the pending text alone upgraded that to an Insert button, and
        pressing it pasted the transcript a second time (measured through
        the overlay's own Insert and through a queued transcript's flush).
        `_insert_offer_may_have_pasted` is recorded beside the offer by the
        paths that create it and decides here: the text stays readable and
        copyable, the button stays hidden, and the wording says which of
        the two it is. It is the offer's own flag, not the per-attempt
        `_last_insert_may_have_pasted`, which the next insert resets: one
        flush pastes several queued transcripts, and a second paste that
        succeeded re-armed the Insert of the first, whose paste had gone
        out (measured: nine writers offering a duplicate paste), while a
        different transcript's post-paste failure hid the button of a tail
        that had never reached a window.
        """
        pending = self._insert_action_text
        if not pending:
            self._offer_painted = None
            if compact is None:
                self._overlay.set_state(state, detail)
            else:
                self._overlay.set_state(state, detail, compact=compact)
            return
        if self._insert_offer_may_have_pasted:
            painted = (
                f"{detail}\n\nPossibly inserted already -- check the target "
                f"window before inserting it again:\n{pending}"
            )
            self._overlay.set_state(
                "Error",
                painted,
                copy_text=pending,
                error_action=OVERLAY_ERROR_ACTION_NONE,
                editable=self._edit_reaches(
                    pending, self._insert_action_rows, self._insert_action_entry
                ),
            )
        else:
            painted = f"{detail}\n\nStill not inserted:\n{pending}"
            self._overlay.set_state(
                "Error",
                painted,
                copy_text=pending,
                error_action=OVERLAY_ERROR_ACTION_INSERT,
                editable=self._edit_reaches(
                    pending, self._insert_action_rows, self._insert_action_entry
                ),
            )
        self._offer_painted = ("Error", painted)

    def _offer_painted_is_on_screen(self) -> bool:
        """True while the overlay still shows the painter's own last offer.

        The preload progress poll must not repaint a Done or Error result,
        and the offer is an Error, so the poll has to tell the two apart: a
        result replaces the painter's text, an untouched offer matches it.
        """
        painted = self._offer_painted
        return painted is not None and painted == (
            self._overlay.state,
            self._overlay.detail,
        )

    def _retire_insert_offer(self) -> None:
        """Drop the pending Insert offer, its flag and its painted record."""
        self._insert_action_text = ""
        self._insert_action_rows = ()
        self._insert_action_entry = None
        self._insert_offer_may_have_pasted = False
        self._offer_painted = None

    @QtCore.Slot(str)
    def on_overlay_detail_cleared(self, copy_text: str) -> None:
        """The overlay's Clear was pressed on a state offering `copy_text`.

        Clear on the pending offer dismisses it. The overlay's own clear
        wrote Idle without the controller learning of it, so the offer came
        straight back: the preload progress poll repainted it 600 ms after
        the press, and every later status writer re-offered it. Only the
        offer that was on screen is retired -- one hidden behind a later
        Error is kept: that Clear was aimed at the Error, not at an offer
        the user may have seen before it or never.
        """
        if copy_text and copy_text == self._insert_action_text:
            self._retire_insert_offer()

    def _capture_target_signature(
        self,
        fallback_window: int | None = None,
    ) -> FocusSignature | None:
        getter = getattr(self._window_focus_helper, "capture_target_signature", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                self._logger.exception("Failed to capture target focus signature")
                return None
        window = fallback_window
        if window is None:
            window = self._target_window_handle
        return (window, window, window) if window else None

    def _current_focus_signature(self) -> FocusSignature | None:
        getter = getattr(self._window_focus_helper, "get_focus_signature", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                self._logger.exception("Failed to read focus signature")
                return None
        foreground = self._current_foreground_window()
        return (foreground, foreground, foreground) if foreground else None

    _UNSET_TARGET = object()

    def note_foreground_window(self) -> None:
        """Record the current foreground before one of our own windows takes it.

        The tray menu calls `SetForegroundWindow` on its hidden host window as
        the notification-icon contract requires, so by the time a menu action
        runs the foreground is ours. Wired to the tray's `activated` signal,
        which fires first.
        """
        note = getattr(self._window_focus_helper, "note_foreground_window", None)
        if note is None:
            return
        try:
            note()
        except Exception:
            self._logger.debug("Could not note the foreground window", exc_info=True)

    def _insert_text_at_target(
        self,
        text: str,
        *,
        restore_focus: bool,
        copy_on_error: bool = True,
        show_overlay_error: bool = True,
        target_handle=_UNSET_TARGET,
        target_signature=_UNSET_TARGET,
        carries_offer: bool = False,
        keep_rows: Sequence[_UndeliveredInsert] = (),
        text_entry: TranscriptHistoryEntry | None = None,
    ) -> bool:
        """Paste ``text``; True when it was inserted.

        ``keep_rows`` are the listed rows the text is built from, which a
        paste whose keystroke went out must not drop as superseded.
        ``carries_offer`` is `_repaste_carries_offer`'s answer for it.
        ``text_entry`` is the history entry ``text`` belongs to, the whole
        of it or the dictation it is the tail of, for an offer this paints
        (`_insert_action_entry`); None when not known.
        """
        if not text.strip():
            return True
        handle = (
            self._target_window_handle
            if target_handle is self._UNSET_TARGET
            else target_handle
        )
        signature = (
            self._target_focus_signature
            if target_signature is self._UNSET_TARGET
            else target_signature
        )
        insert_hwnd = self._target_insert_window(signature, handle)
        insertion_text = str(text)
        self._last_insert_may_have_pasted = False
        self._last_insert_error_text = ""
        self._last_insert_offered = False
        self._last_insert_foreground = None
        paste_foreground: int | None = None
        try:
            if restore_focus and handle:
                try:
                    restored = bool(
                        self._window_focus_helper.restore_target_window(handle)
                    )
                except Exception as exc:
                    self._logger.exception("Failed to restore target window focus")
                    raise TextInsertionError(
                        "Target window focus could not be restored; transcript was "
                        "not pasted into another window."
                    ) from exc
                expected_foreground = (
                    signature[0]
                    if isinstance(signature, tuple) and signature
                    else handle
                )
                paste_foreground = expected_foreground
                current_foreground = self._current_foreground_window()
                if not restored or (
                    expected_foreground
                    and current_foreground is not None
                    and current_foreground != expected_foreground
                ):
                    raise TextInsertionError(
                        "Target window focus could not be restored; transcript was "
                        "not pasted into another window."
                    )
            self._text_inserter.insert_text_with_options(
                insertion_text,
                target_hwnd=insert_hwnd,
                paste_mode=self._settings.paste_mode,
                # When the transcript should stay in the clipboard anyway,
                # skip the restore: a paste the target processes late then
                # still reads the transcript instead of the restored previous
                # clipboard content.
                restore_clipboard=not bool(
                    getattr(self._settings, "keep_transcript_in_clipboard", False)
                ),
            )
        except TextInsertionError as exc:
            # Two failure paths happen *after* the paste keystroke went out, so
            # the target may already hold the text. The streaming retry has to
            # know, or it offers the same words again and they land twice.
            may_have_pasted = isinstance(exc, TextMayHaveBeenPastedError)
            self._last_insert_may_have_pasted = may_have_pasted
            if may_have_pasted and carries_offer:
                # The pending offer's own re-paste, or the tray's re-paste of
                # the dictation the offer is the tail of: its text is now the
                # one most likely in the document, whatever a later,
                # unrelated insert does to the per-attempt flag above. Only
                # `_repaste` passes `carries_offer`: a queued transcript
                # that merely contained the tail's word used to mark it here.
                self._insert_offer_may_have_pasted = True
            if may_have_pasted:
                # The text is probably already in the document. Offering the
                # usual Insert action would paste it a second time, and
                # copying it over the clipboard is pointless when the paste
                # itself succeeded -- only the cleanup failed. Report it and
                # leave the transcript alone.
                self._logger.warning("Insertion reported a post-paste failure: %s", exc)
                self._last_insert_error_text = (
                    f"{exc} The text was most likely inserted; check the "
                    "target window before inserting it again."
                )
                if show_overlay_error:
                    self._overlay.set_state(
                        "Error",
                        self._last_insert_error_text,
                        copy_text=insertion_text,
                        # Explicitly no action. Omitting this leaves Retry,
                        # which re-transcribes the last *failed* recording --
                        # cleared only on the foreground ready path, so from
                        # a re-paste it can be an entirely different one and
                        # lands on top of the text just inserted.
                        error_action=OVERLAY_ERROR_ACTION_NONE,
                        # The edit reaches history and Copy; nothing pastes
                        # a text whose keystroke went out.
                        editable=self._edit_reaches(
                            insertion_text, keep_rows, text_entry
                        ),
                    )
                # The keystroke went out, so this is a paste like any other for
                # the doubtful rows before it: the text in front of it is as
                # stale as after a clean paste.
                self._paste_serial += 1
                self._drop_superseded_doubtful_rows(keep_rows)
                return False
            allow_clipboard_fallback = bool(
                getattr(exc, "allow_clipboard_fallback", True)
            )
            if copy_on_error and allow_clipboard_fallback:
                QtGui.QGuiApplication.clipboard().setText(insertion_text)
            detail = str(exc)
            if copy_on_error and allow_clipboard_fallback:
                detail = f"{detail} Transcript copied to clipboard."
            elif copy_on_error:
                detail = (
                    f"{detail} Transcript saved to history; current "
                    "clipboard left untouched."
                )
            self._last_insert_error_text = detail
            if show_overlay_error:
                # Show what was transcribed: the text is otherwise invisible
                # until it is inserted again, which is exactly when the user
                # needs to see and be able to copy it.
                preview = insertion_text.strip()
                if preview:
                    detail = f"{detail}\n\n{preview}"
                # The transcription itself succeeded, so Retry (which
                # re-transcribes) has nothing to work with; offer inserting the
                # transcript again instead. Its rows, if any, are attached by
                # the caller, which knows them (`_last_insert_offered`).
                self._insert_action_text = insertion_text
                self._insert_action_rows = ()
                self._insert_action_entry = text_entry
                self._insert_offer_may_have_pasted = False
                self._last_insert_offered = True
                self._overlay.set_state(
                    "Error",
                    detail,
                    copy_text=insertion_text,
                    error_action=OVERLAY_ERROR_ACTION_INSERT,
                    editable=self._edit_reaches(insertion_text, keep_rows, text_entry),
                )
            self._logger.exception("Text insertion failed")
            return False
        self._last_insert_foreground = (
            paste_foreground or self._current_foreground_window()
        )
        self._paste_serial += 1
        self._drop_superseded_doubtful_rows(keep_rows)
        self._logger.info(
            "text_insertion outcome=success chars=%d target_hwnd=%s "
            "restore_focus=%s paste_mode=%s",
            len(insertion_text),
            insert_hwnd,
            restore_focus,
            self._settings.paste_mode,
        )
        return True

    def _target_insert_window(
        self,
        signature: FocusSignature | None,
        handle: int | None,
    ) -> int | None:
        if signature is not None:
            _foreground, focus_hwnd, caret_hwnd = signature
            if caret_hwnd:
                return caret_hwnd
            if focus_hwnd:
                return focus_hwnd
        return handle

    def copy_last_transcript_to_clipboard(self) -> bool:
        if not self._last_transcript.strip():
            return False
        QtGui.QGuiApplication.clipboard().setText(self._last_transcript)
        return True

    def repaste_last_transcript(self) -> None:
        """Insert what is still missing, else the last delivered text again.

        Tray action and optional global hotkey, into the currently focused
        window through the normal insertion path (paste-mode and clipboard
        semantics from settings, the modifier-release wait in the inserter).

        Transcripts whose paste failed come first: when any are listed (and
        not "possibly inserted", which is never pasted twice), this pastes
        all of them as one paste, oldest first; a "not in a text field" row
        only when it is the one waiting (`_repaste_rows`). Otherwise it
        pastes the last text that reached a window -- a queued result pasted
        in the background after the shown transcript, else the shown one.

        Allowed while a transcription is in flight, without touching the
        overlay that transcription owns (field report, 2026-10-01: the
        wave-13 refusal left the hotkey dead for as long as anything was
        transcribing, which on a slow machine with a queue was minutes).
        Allowed during an open batch capture too (owner's decision,
        2026-10-01). During a streaming recording or its pending finalize
        (owner's request 2026-10-09): into another window at once, into the
        stream's own window held until the stream has ended, and the tray
        says so. Refused, with the reason on the tray or the overlay, only
        while a recording starts or stops.
        """
        waiting = self._repaste_rows()
        if waiting:
            self._repaste(
                _join_transcripts([entry.text for entry in waiting]),
                display_entry=(waiting[0].history_entry if len(waiting) == 1 else None),
                undelivered=waiting,
            )
            return
        delivered = self._delivered_after_shown
        if delivered is not None:
            text, entry = delivered
            self._repaste(text, display_entry=entry)
            return
        self._repaste_last_unless_possibly_inserted()

    def _repaste_last_unless_possibly_inserted(self) -> None:
        """Re-paste `_last_transcript`, unless its own paste may have landed.

        A paste whose keystroke went out is never pasted again: its row says
        "Possibly inserted, check the window", and pasting the same text from
        the fallback would put it in the document twice.
        """
        text = self._last_transcript
        held = self._shown_transcript_token
        if held is not None and any(
            job.token == held for job, _text in self._deferred_background_results
        ):
            # This very result is still in the paste queue -- the pace holds
            # it -- and pasting it now as well put it in the document twice.
            # Told through the tray: the overlay shows that result as Done.
            self._logger.info("repaste_skipped reason=queued token=%d", held)
            self.busy_overlay_error.emit(
                "The last transcript is about to be inserted; it is not "
                "inserted a second time."
            )
            return
        row = self._shown_transcript_row
        if (
            row is not None
            and row.may_have_pasted
            and any(row is entry for entry in self._undelivered_inserts)
        ):
            # By identity: another dictation with the same text whose paste
            # may have landed is a different paste, and refusing this one for
            # it left the shown transcript impossible to insert.
            self.show_overlay_error(
                "The last transcript may already have been inserted, so it is "
                "not inserted again. Check the window; the transcript is in "
                "history."
            )
            return
        self._repaste(text)

    def insert_failed_text(self) -> None:
        """Insert the text the overlay's Error state offered Insert for.

        That is the text of the insert that failed, and only that. A
        streaming finalize inserts only the tail past `committed_text` -- the
        rest is already in the document, pasted live -- and its Error state
        carries that tail as `copy_text`. The overlay's Insert used to be wired
        to `repaste_last_transcript`, which reads `_last_transcript`, the whole
        dictation: measured, the finalize inserted ' zweiter teil' and Insert
        then pasted 'erster teil zweiter teil' on top of the text already in
        the document. The batch case is unaffected either way, because there
        the failed insert and the last transcript are the same text -- which
        is also why the fallback below is safe.
        """
        if self._insert_action_text:
            self._repaste(
                self._insert_action_text,
                offer_rows=self._still_insertable(self._insert_action_rows),
            )
            return
        self._repaste_last_unless_possibly_inserted()

    _KEEP_DISPLAY = object()

    def _repaste(
        self,
        text: str,
        *,
        display_entry=_KEEP_DISPLAY,
        undelivered: Sequence[_UndeliveredInsert] = (),
        offer_rows: Sequence[_UndeliveredInsert] = (),
        announce_hold: bool = True,
    ) -> None:
        """Paste ``text`` into the focused window on the user's request.

        ``display_entry`` is given when the text is not the shown transcript
        (a background delivery, waiting transcripts): a paste with no session
        on screen then shows it, and Copy and Edit move to it with that entry
        (None for a joined text, so Edit refuses). ``undelivered`` are the
        listed rows the text was built from; a successful paste retires them.
        ``offer_rows`` are the overlay Insert's own rows: retired on success
        like them, without making this a queued paste (its failure still
        copies the text, as the Insert always did). A failure that paints the
        offer again hands it all of these rows, so its next Insert retires
        them too. ``announce_hold`` False: a held request run again, whose
        hold the user was told about already.
        """
        if not text.strip():
            self.show_overlay_error("No transcript available to insert yet.")
            return
        what = (
            "the transcripts that were not inserted"
            if undelivered
            else "the last transcript again"
        )
        # Allowed during an open batch capture (owner's decision,
        # 2026-10-01): the paste goes to the current focus with its own
        # target, leaves the recording's target snapshot alone and reports
        # through the tray, as during a transcription in flight.
        if self._recording_start_in_progress or self._recording_stop_in_progress:
            # The start or stop is taking the recording's target snapshot,
            # and a paste then races it.
            message = (
                f"Wait for the recording to start or stop before inserting {what}."
            )
            hint = self._undelivered_hint()
            if hint:
                message = f"{message} {hint}"
            self.show_overlay_error(message)
            return
        if self._stream_still_delivering() and self._streaming_window_has_focus():
            # Live inserts write at that caret while the microphone is open,
            # and a pending finalize still inserts its tail past the text
            # already there: a paste now would land inside the streamed
            # words or in front of the tail. Held until the stream has ended
            # (owner's request 2026-10-09: it was refused, and the user had
            # to press it again), then pasted at whatever has the focus.
            self._pending_repaste = _PendingRepaste(
                text=text,
                display_entry=display_entry,
                undelivered=tuple(undelivered),
                offer_rows=tuple(offer_rows),
                after_stream=True,
            )
            self._logger.info("repaste_held reason=streaming rows=%d", len(undelivered))
            if announce_hold:
                held = (
                    "The transcripts that were not inserted will be inserted"
                    if undelivered
                    else "The last transcript will be inserted again"
                )
                self.show_overlay_error(
                    f"{held} when the streaming dictation into this window has "
                    "finished."
                )
            return
        wait_s = self._paste_pace_wait_s()
        if wait_s > 0.0:
            # Through the pace like every other paste: a second paste inside
            # the previous one's restore window overwrites a clipboard a late
            # reader has not read yet. Nothing waits on the Qt thread; the
            # timer runs it, after the results the pace holds.
            self._pending_repaste = _PendingRepaste(
                text=text,
                display_entry=display_entry,
                undelivered=tuple(undelivered),
                offer_rows=tuple(offer_rows),
            )
            self._logger.info(
                "repaste_paced wait_ms=%d rows=%d", _pace_ms(wait_s), len(undelivered)
            )
            self._paste_pace_timer.start(_pace_ms(wait_s))
            return
        # Resolve the target instead of pasting at whatever holds the
        # foreground. This action's main entry point is the tray menu, and the
        # notification-icon contract requires `SetForegroundWindow` on our own
        # hidden 0x0 host window before the menu opens -- so at the moment the
        # action runs, the foreground *is* ours. With no target handle,
        # `SendInput` delivered Ctrl+V to that hidden window and `WM_PASTE`
        # went to `_get_focused_hwnd()`, a raw `GetForegroundWindow()` with no
        # own-window filter, which hands the same handle straight back.
        # Measured: `window_focus.get_foreground_window()` answers `None` for
        # that window while `text_inserter._get_focused_hwnd()` returns it.
        # The insert then reported success, so the overlay said "Done" and the
        # completion tone played for a paste that reached nothing.
        #
        # `_current_focus_signature` is the resolver that refuses our own
        # windows and answers with the last foreign one instead --
        # `note_foreground_window`, wired to the tray's `activated` signal,
        # exists precisely to have recorded it before the menu took over.
        signature = self._current_focus_signature()
        target = signature[0] if signature else None
        if not target:
            # `show_overlay_error` reveals the overlay itself on its paint
            # road and must not on its tray road; a second reveal here
            # restarted the timer and, during a session, brought a
            # "Listening" overlay to the front for an error the tray carried.
            self.show_overlay_error(
                "No window to insert into. Click into the window you want the "
                "transcript in, then try again."
            )
            return
        # A foreground transcription in flight owns the overlay: the paste
        # goes out, and its outcome reaches the tray instead of painting over
        # "Processing" (the wave-13 reach lens saw "Done" with an older text
        # replace it, and a failed paste paint "Error" over it).
        session = self._overlay_session_active()
        rows = [*undelivered, *offer_rows]
        # Decided before the paste: a successful one retires its rows.
        carried = self._repaste_carries_offer(
            text,
            undelivered=undelivered,
            shown_fallback=(
                display_entry is self._KEEP_DISPLAY
                and not rows
                and text == self._last_transcript
            ),
        )
        # The entry the text belongs to, for an offer a failure paints: a
        # background delivery's or a row's, the shown transcript's, or the
        # pending offer's own (its Insert, a streaming tail included).
        if display_entry is not self._KEEP_DISPLAY:
            text_entry = display_entry
        elif text == self._last_transcript:
            text_entry = self._last_history_entry
        elif carried:
            text_entry = self._insert_action_entry
        else:
            text_entry = None
        inserted = self._insert_text_at_target(
            text,
            restore_focus=True,
            # Waiting transcripts are queued pastes: like every queued paste
            # a failure leaves the user's clipboard alone; the text is in
            # history and stays listed.
            copy_on_error=not undelivered,
            show_overlay_error=not session,
            target_handle=target,
            target_signature=signature,
            carries_offer=carried,
            keep_rows=rows,
            text_entry=text_entry,
        )
        if not inserted:
            if self._last_insert_offered:
                # The failure painted the offer for this text: it covers the
                # same rows, so its Insert retires them on success instead of
                # leaving them listed for the re-paste to paste once more.
                self._insert_action_rows = tuple(rows)
            if rows and self._last_insert_may_have_pasted:
                # The keystroke went out: listed still, never offered again.
                for entry in rows:
                    entry.may_have_pasted = True
                self._update_queue_overlay()
            if session:
                self.show_overlay_error(self._last_insert_error_text)
            else:
                self._reveal_overlay_result(is_error=True)
            return
        self._retire_undelivered(rows)
        if carried:
            self._retire_insert_offer()
        if display_entry is not self._KEEP_DISPLAY:
            entry_of_text = display_entry
        elif text == self._last_transcript:
            entry_of_text = self._last_history_entry
        else:
            # The offer's text (a streaming tail) has no entry of its own.
            entry_of_text = None
        # Each pasted row's results with their entries, for a doubtful row
        # (`_UndeliveredInsert.parts`): without them a joined F10 that the
        # check reports listed `(None, joined text)`, which no edit reached.
        # Only when they make up the pasted text -- an Insert's rows
        # dismissed meanwhile are pasted but no longer listed.
        row_parts = tuple(part for row in rows for part in row.parts)
        if _join_transcripts([part for _entry, part in row_parts]) != text.strip():
            row_parts = ()
        pasted_at = datetime.now().astimezone()
        if self._streaming_recording:
            # Into another window: the stream's next live insert would
            # overwrite the clipboard that window may still read from
            # (`_stream_live_insert_held`).
            self._stream_waits_for_paste_pace = True
        if session:
            if display_entry is not self._KEEP_DISPLAY:
                self._delivered_after_shown = (text, display_entry)
            self._check_paste_target(
                self._paste_check(
                    text,
                    entry_of_text,
                    created_at=pasted_at,
                    identity="The transcript was",
                    background=True,
                    takes_shown_pair=False,
                    parts=row_parts,
                ),
                tone_if_refused=False,
            )
            return
        if display_entry is not self._KEEP_DISPLAY:
            self._set_last_transcript(text, display_entry)
        if carried:
            self._overlay.set_state("Done", text)
        else:
            # `_last_transcript` has moved on -- a queued streaming job
            # that failed rescues its partial into it -- so the tray
            # pasted text the offer is no part of. Retiring the offer
            # here took the tail's Insert away while the tail had
            # reached no window (measured: the overlay's Insert then
            # pasted the unrelated text instead).
            self._paint_status_keeping_offer("Done", text)
        self._reveal_overlay_result(is_error=False)
        self._check_paste_target(
            self._paste_check(
                text,
                entry_of_text,
                created_at=pasted_at,
                identity="The transcript was",
                background=False,
                takes_shown_pair=False,
                parts=row_parts,
            ),
            tone_if_refused=False,
        )

    def show_overlay_notice(self, message: str) -> None:
        """Confirm a completed action on the overlay and return to Idle.

        Tray actions have no other feedback surface, so a copy that silently
        succeeds is indistinguishable from one that did nothing.
        """
        if self._overlay_session_active():
            return
        self._paint_status_keeping_offer("Done", str(message))
        self._reveal_overlay_result(is_error=False)
        QtCore.QTimer.singleShot(OVERLAY_NOTICE_MS, self.show_idle_status)

    def show_overlay_error(self, message: str) -> None:
        """Surface a transient error on the overlay without exposing the
        overlay widget to callers (kept so main.py does not reach into
        ``_overlay`` directly).

        Through the offer-keeping painter: "No window to insert into" is
        what the overlay's own Insert answers when the user has not clicked
        into a document yet, and painted plainly it hid the button the user
        was about to press again.

        Never over a live session. Painted while a recording or a
        transcription owned the overlay, an unrelated refusal -- the opacity
        slider's save refused by a locked `settings.json` mid-dictation --
        replaced "Listening" with "Error" while the microphone kept recording
        underneath, and in batch mode nothing repaints "Listening" before the
        stop (the wave-12 reach lens). Such an error goes to the tray, the
        surface that survives a live session, as a queued job's failure does.
        """
        text = str(message)
        if self._overlay_session_active():
            self._logger.warning("overlay_error_while_session_active %s", text)
            self.busy_overlay_error.emit(text)
            return
        self._paint_status_keeping_offer("Error", text)
        self._reveal_overlay_result(is_error=True)

    def _reveal_overlay_result(self, *, is_error: bool) -> None:
        """Bring the overlay to the foreground after a finished transcription.

        A floating (non-pinned) overlay can sit behind other windows and, being
        a tool window, is not reachable via Alt+Tab. Reveal it briefly on
        success so the result is seen, and for longer on errors/insert failures
        so the transcript can still be copied from the overlay.
        """
        duration = OVERLAY_ERROR_REVEAL_MS if is_error else OVERLAY_RESULT_REVEAL_MS
        try:
            self._overlay.reveal_temporarily(duration)
        except Exception:
            self._logger.exception("Failed to reveal overlay for result")

    def bring_overlay_to_front(self) -> None:
        """Manually bring the overlay to the foreground (tray action).

        Reliable escape hatch when the overlay is floating and hidden behind
        another window; reuses the longer reveal window so there is time to act.
        """
        try:
            self._overlay.reveal_temporarily(OVERLAY_ERROR_REVEAL_MS)
        except Exception:
            self._logger.exception("Failed to bring overlay to front")

    def edit_last_transcript(self, parent=None) -> bool:
        current_text = self._last_transcript.strip()
        if not current_text:
            self._paint_status_keeping_offer(
                "Error", "No transcript available to edit."
            )
            return False
        # The entry is read beside the text it belongs to, before the dialog:
        # `get_text` runs a modal `exec()`, a nested Qt loop that keeps
        # delivering transcription results, and each one moves both fields.
        # Read afterwards, the entry was the newer dictation's, and the edit
        # of the text the dialog had offered was written onto it.
        entry = self._last_history_entry
        if entry is None:
            self._paint_status_keeping_offer(
                "Error",
                "No saved history entry is available for this transcript.",
            )
            return False

        from .transcript_edit_dialog import TranscriptEditDialog

        next_text = TranscriptEditDialog.get_text(parent, current_text)
        if next_text is None or next_text == current_text:
            return False

        try:
            updated = self._history_store.update_entry_text(entry, next_text)
        except Exception as exc:
            # A store that could not read its file refuses to write it
            # (`persistence.StoreUnavailableError`), and that refusal arrives
            # here on the Qt thread. Unguarded it escaped into the slot, whose
            # traceback goes to a stderr a windowed build does not have: the
            # Edit button did nothing at all, twice in a row, with no way to
            # tell a refusal from a broken button. Painted like the two
            # refusals above, so a pending insert offer survives it.
            self._logger.exception("Failed to save the edited transcript")
            self._paint_status_keeping_offer("Error", str(exc))
            return False
        if updated <= 0:
            self._paint_status_keeping_offer(
                "Error",
                "The saved history entry could not be updated.",
            )
            return False

        shown_followed, offer = self._follow_transcript_edit(
            entry, edited_entry(entry, next_text)
        )
        if not shown_followed:
            # A result delivered while the dialog was open owns the overlay
            # and the Copy/Edit pair now; the edit is saved in history and
            # must neither repaint that result nor take the pair back.
            self._logger.info(
                "transcript_edit_saved_behind_newer_result chars=%d",
                len(next_text.strip()),
            )
            return True
        # The edited transcript is what the re-paste pastes again, as it was
        # when the edit went through `_set_last_transcript`.
        self._delivered_after_shown = None
        if not self._overlay_session_active():
            # A recording started while the dialog was open owns the overlay;
            # the confirmation must not paint over "Listening".
            self._paint_after_edit(offer)
        if self._settings.keep_transcript_in_clipboard:
            QtGui.QGuiApplication.clipboard().setText(self._last_transcript)
        return True

    def on_history_entry_edited(
        self,
        original: TranscriptHistoryEntry,
        updated: TranscriptHistoryEntry,
    ) -> None:
        """A history editor saved ``updated`` over ``original``.

        Called by the History dialog and the Settings History tab after
        their store write. What still waits to be inserted from that entry
        follows the edit (`_follow_transcript_edit`). The overlay is
        repainted only where it shows the old text -- the offer or the shown
        transcript -- and never over a live session.
        """
        offer_before = self._insert_action_text
        shown_before = self._last_transcript
        state = self._overlay.state
        detail = self._overlay.detail
        copy_text = getattr(self._overlay, "copy_text", None)
        shown_followed, offer = self._follow_transcript_edit(original, updated)
        if self._overlay_session_active():
            return
        showed_offer = bool(offer) and state == "Error" and copy_text == offer_before
        showed_transcript = (
            shown_followed and state == "Done" and detail == shown_before
        )
        if showed_offer or showed_transcript:
            self._paint_after_edit(offer)

    def _paint_after_edit(self, offer: str) -> None:
        """Confirm an edit of the shown transcript, with what became of the
        offer (`_follow_transcript_edit`); a pending offer stays on screen
        with its Insert, now for the edited text."""
        if offer == "followed":
            self._paint_status_keeping_offer("Done", "Transcript edited.")
        elif offer == "kept":
            self._paint_status_keeping_offer(
                "Done",
                "Transcript edited in history. The edit changed words already "
                "in the window, so Insert still inserts the part that was "
                "missing before the edit.",
            )
        else:
            # Not a session result: the overlay Edit button's confirmation
            # (there is no tray Edit action), painted plainly, hid a pending
            # offer exactly as the refusals above did.
            self._paint_status_keeping_offer(
                "Done", self._last_transcript, compact=False
            )

    def _follow_transcript_edit(
        self,
        original: TranscriptHistoryEntry,
        updated: TranscriptHistoryEntry,
    ) -> tuple[bool, str]:
        """Make what still waits to be inserted from ``original`` paste ``updated``.

        Owner's rule 2026-10-09: a result is normally inserted right after
        it is transcribed, so an edit matters for insertion only while it
        was not -- a result still in the paste queue, a waiting-insert row,
        the Insert offer. Each of those follows the edit, and so do the
        shown transcript (Copy, the re-paste fallback, Edit) -- also when it
        is a coalesced row's joined text, which has no entry of its own --
        and the last background delivery (the re-paste). Entries match by
        value, as the store's own `update_entry` does: a history editor
        holds a copy read back from the file.

        Nothing here pastes. A row whose keystroke may have gone out takes
        the edited text for its label and stays unpastable, and the shown
        transcript keeps its row and its queued token, so the re-paste
        fallback still refuses or skips it exactly as before the edit.

        Returns whether the shown transcript followed, and what became of
        the offer: "followed"; "retired" when the edit removed the part that
        was missing; "kept" when it changed words a streaming finalize had
        already inserted, so the missing part can no longer be told; ""
        when the offer is not this entry's.
        """
        new_text = updated.text.strip()

        def is_original(entry: TranscriptHistoryEntry | None) -> bool:
            return entry is not None and entry == original

        old_shown = self._last_transcript
        shown_row = self._shown_transcript_row
        shows_joined_row = False
        changed_rows: list[_UndeliveredInsert] = []
        for row in self._undelivered_inserts:
            if not any(is_original(entry) for entry, _part in row.parts):
                continue
            if row is shown_row and row.text == old_shown.strip():
                shows_joined_row = self._last_history_entry is None
            row.parts = tuple(
                (updated, new_text) if is_original(entry) else (entry, part)
                for entry, part in row.parts
            )
            row.text = _join_transcripts([part for _entry, part in row.parts])
            changed_rows.append(row)
        # A paste whose target check still runs becomes a row on a "not a
        # text field" verdict, built from the check's copy: it takes the
        # edit too, or that row kept the old text and no later edit
        # reached it.
        for check_id, check in list(self._paste_checks.items()):
            check_parts = check.parts or ((check.history_entry, check.text.strip()),)
            if not any(is_original(entry) for entry, _part in check_parts):
                continue
            check_parts = tuple(
                (updated, new_text) if is_original(entry) else (entry, part)
                for entry, part in check_parts
            )
            self._paste_checks[check_id] = replace(
                check,
                text=_join_transcripts([part for _entry, part in check_parts]),
                history_entry=(
                    updated if is_original(check.history_entry) else check.history_entry
                ),
                parts=check_parts if check.parts else (),
            )
        queued = 0
        for index, (job, _text) in enumerate(self._deferred_background_results):
            if is_original(job.history_entry):
                job.history_entry = updated
                self._deferred_background_results[index] = (job, new_text)
                queued += 1
        delivered = self._delivered_after_shown
        if delivered is not None and is_original(delivered[1]):
            self._delivered_after_shown = (new_text, updated)
        shown_followed = is_original(self._last_history_entry)
        # Not through `_set_last_transcript`: its setter forgets the shown
        # transcript's row and queued token, and the re-paste fallback reads
        # both to never paste that dictation twice.
        if shown_followed:
            self._shown_transcript = new_text
            self._last_history_entry = updated
        elif shows_joined_row:
            # The shown transcript is a coalesced row's joined text, with no
            # entry of its own: Copy yields the row as edited (owner's rule
            # 2026-10-09). Edit still refuses there -- no single entry.
            self._shown_transcript = shown_row.text
            shown_followed = True
        offer = ""
        pending = self._insert_action_text
        offer_rows = self._insert_action_rows
        if pending and offer_rows:
            if any(row is changed for row in offer_rows for changed in changed_rows):
                self._insert_action_text = _join_transcripts(
                    [row.text for row in offer_rows]
                )
                offer = "followed"
        elif pending and is_original(self._insert_action_entry):
            # A row-less offer follows its own dictation only: a streaming
            # tail of it, or all of it after a failed re-paste. Matched
            # against the shown transcript instead, a failed F10 of another
            # dictation's "okay." was retargeted to a fragment of the shown
            # "Alles okay." edit.
            self._insert_action_entry = updated
            tail = retarget_tail(original.text, new_text, pending)
            if tail:
                self._insert_action_text = tail
                offer = "followed"
            elif tail == "":
                self._retire_insert_offer()
                offer = "retired"
            elif tail_prefix(original.text, pending) is not None:
                offer = "kept"
        if changed_rows:
            self._update_queue_overlay()
        self._logger.info(
            "transcript_edit_followed rows=%d queued=%d shown=%s offer=%s",
            len(changed_rows),
            queued,
            shown_followed,
            offer or "none",
        )
        return shown_followed, offer

    def retry_last_transcription(self) -> bool:
        if not self._last_failed_wav_bytes:
            self._paint_status_keeping_offer(
                "Error", "No failed transcription to retry."
            )
            return False
        if (
            self._recording_start_in_progress
            or self._recording_stop_in_progress
            or self._audio_capture is not None
        ):
            # A retry stops the running transcription and paints
            # "Processing"; from the tray while the microphone was open it
            # did both over a live recording (wave 15). Deliberately not
            # `_streaming_recording`: a pending finalize keeps that flag
            # with the microphone closed, and it is a transcription in
            # flight, which the retry stops by design. The refusal reaches
            # the tray, since the recording owns the overlay.
            self.show_overlay_error(
                "Finish the current recording before retrying the last failed "
                "transcription."
            )
            return False
        settings = replace(self._settings)
        # Stop any still-running transcription before retrying; if it finishes
        # anyway it is kept in history rather than discarded.
        self._request_job_stop(
            self._active_request_token,
            delivery=CONCURRENT_TRANSCRIPTION_MODE_HISTORY,
        )
        self._overlay.set_state(
            "Processing",
            "Retrying transcription with current settings...",
        )
        # The retry names the recording whose bytes it resubmits. Registered
        # from the store's slot, the job took the newest recording's id --
        # the one the stop above had just marked canceled -- and its
        # completion then deleted that recording's audio and state.
        self._submit_batch_transcription(
            self._last_failed_wav_bytes,
            settings,
            source_recording_id=self._last_failed_recording_id,
        )
        return True

    def recent_transcriptions(self, limit: int | None = None):
        max_items = (
            int(self._settings.history_max_items) if limit is None else int(limit)
        )
        return self._history_store.recent_entries(max_items)

    def transcribe_audio_file(
        self,
        file_path: str,
        settings_override: AppSettings | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        """Transcribe a file through the controller's serialized worker lane."""
        path = str(file_path or "").strip()
        if not path:
            return False, "No file path provided."
        if not os.path.isfile(path):
            return False, "Selected file does not exist."
        managed_last_recording = self._last_recording_store.is_managed_audio_path(path)
        managed_snapshot = None
        if managed_last_recording:
            snapshotter = getattr(
                self._last_recording_store,
                "snapshot_managed_recording",
                None,
            )
            if callable(snapshotter):
                managed_snapshot = snapshotter(path)
                if managed_snapshot is None:
                    return False, "The last recording is no longer available."
        recording_id = (
            str(getattr(managed_snapshot, "recording_id", "") or "").strip()
            if managed_snapshot is not None
            else self._current_last_recording_id()
            if managed_last_recording
            else ""
        )
        audio_source: str | bytes = (
            bytes(managed_snapshot.audio_bytes)
            if managed_snapshot is not None
            else path
        )
        conditional_transition = (
            {"expected_recording_id": recording_id}
            if managed_snapshot is not None
            else {}
        )
        try:
            base_settings = settings_override or self._settings
            settings = replace(base_settings, mode="batch")
            if managed_last_recording:
                self._last_recording_store.mark_transcribing(
                    engine=settings.engine,
                    model=self._selected_model_name(settings),
                    mode="import",
                    **conditional_transition,
                )
            future = self._executor.submit(
                self._transcribe_import_worker,
                audio_source,
                settings,
                progress_callback,
            )
            text = future.result().strip()
            if not text:
                if managed_last_recording:
                    self._last_recording_store.mark_failed(
                        _EMPTY_MODEL_TRANSCRIPT_MESSAGE,
                        **conditional_transition,
                    )
                return False, _EMPTY_MODEL_TRANSCRIPT_MESSAGE
            self._append_transcript_history(
                text,
                settings,
                "import",
                source_recording_id=recording_id,
                source_audio_path=(
                    "" if managed_last_recording else os.path.abspath(path)
                ),
                track_for_edit=False,
            )
            if managed_last_recording:
                if transcript_has_gap(text):
                    # Kept, like a dictation whose transcript has a gap
                    # (`_mark_last_recording_completed`).
                    self._last_recording_store.mark_failed(
                        _GAP_KEPT_RECORDING_MESSAGE,
                        **conditional_transition,
                    )
                else:
                    self._last_recording_store.mark_completed(**conditional_transition)
            return True, text
        except Exception as exc:
            self._logger.exception("Failed to transcribe imported file")
            if managed_last_recording:
                try:
                    self._last_recording_store.mark_failed(
                        str(exc),
                        **conditional_transition,
                    )
                except Exception:
                    self._logger.exception(
                        "Failed to persist imported recording failure state"
                    )
            return False, str(exc)

    def _transcribe_import_worker(
        self,
        audio_source: str | bytes,
        settings: AppSettings,
        progress_callback: Callable[[str], None] | None,
    ) -> str:
        """Run an import while owning the normal transcriber runtime lane."""
        runtime_lease: _TranscriberRuntimeLease | None = None
        transcriber = None
        try:
            # Isolated is allowed here for the same reason `_transcribe_worker`
            # allows it: this runs on the single shared worker, a live stream
            # holds the shared lease for its whole session, and the stream's
            # own finalize is queued onto that same worker. Waiting for the
            # shared lease here is therefore a cycle -- measured on the real
            # controller, the import blocked forever, the finalize never
            # started, and the overlay sat on "Finalizing streaming
            # transcript" with every later hotkey press refused. Nothing but
            # the Cancel hotkey or quitting recovered it.
            runtime_lease = self._acquire_transcriber_runtime(settings)
            transcriber = runtime_lease.transcriber
            if progress_callback is not None:
                self._set_transcriber_progress_callback(
                    transcriber,
                    progress_callback,
                )
            return str(transcriber.transcribe_batch(audio_source) or "")
        finally:
            try:
                if transcriber is not None:
                    self._set_transcriber_progress_callback(transcriber, None)
            except BaseException:
                self._logger.exception(
                    "Failed to clear imported-transcription progress hook"
                )
            finally:
                if runtime_lease is not None:
                    try:
                        runtime_lease.release()
                    except BaseException:
                        # Runtime cleanup must not discard a transcript that the
                        # provider already returned successfully, and must never
                        # skip the release: `_transcriber_runtime_lock` would be
                        # stranded for the process lifetime.
                        self._logger.exception(
                            "Failed to release imported-transcription runtime"
                        )

    @staticmethod
    def _set_transcriber_progress_callback(
        transcriber: object,
        callback: Callable[[str], None] | None,
    ) -> None:
        setter = getattr(transcriber, "set_progress_callback", None)
        if callable(setter):
            setter(callback)

    @staticmethod
    def _set_transcriber_cancel_check(
        transcriber: object,
        cancel_check: Callable[[], bool] | None,
    ) -> None:
        setter = getattr(transcriber, "set_cancel_check", None)
        if callable(setter):
            setter(cancel_check)

    def cancel_current_action(self) -> None:
        # Cancel active recording first.
        if self._audio_capture is not None:
            if self._streaming_recording:
                self._abort_streaming_session(
                    "Streaming canceled.",
                    beep=False,
                    finalize_stream=False,
                    preserve_audio=True,
                )
                return
            # The shared helper rather than a copy of it. This block used to
            # inline the same detach/stop/persist/archive sequence but caught a
            # failing `capture.stop()` and did nothing, while the normal stop
            # path logs it. `stop()` concatenates every recorded chunk and
            # encodes the WAV, so it raises on exactly the recording that is
            # worth the most -- a long one that no longer fits in memory -- and
            # the very next log line then reported `audio_bytes=0`, which is
            # what an instant cancel looks like.
            wav_bytes, _source_audio_path = self._stop_active_capture()
            self._logger.info(
                "recording_canceled_before_transcription audio_bytes=%d",
                len(wav_bytes),
            )
            # The recording this cancel owns in the store is the one its
            # persist writes, keyed by the id handed back. A refused write
            # leaves the slot to the previous recording, which the unkeyed
            # mark relabelled canceled and the text called "this recording".
            persisted = bool(wav_bytes) and self._persist_last_recording_audio(
                wav_bytes
            )
            if persisted:
                try:
                    self._last_recording_store.mark_canceled(
                        "Recording canceled before transcription.",
                        expected_recording_id=self._last_persisted_recording_id or None,
                    )
                except Exception:
                    self._logger.exception("Failed to mark canceled recording")
            self._active_batch_settings = None
            guidance = self._retry_guidance(
                has_retry_audio=False, owns_last_recording=persisted
            )
            self._overlay.set_state("Done", f"Recording canceled. {guidance}")
            self._reset_streaming_state()
            # Canceling this recording removed the capture that was blocking any
            # deferred background inserts. Deliver every completed one now — even
            # if an unrelated transcription is still running — instead of leaving
            # them stuck as "Insert Pending" behind a transcription that can take
            # a minute (which reads as "deleted, only in history").
            self._flush_deferred_background_results(ignore_active_transcription=True)
            return

        request_token = self._active_request_token
        if request_token is not None:
            had_job = request_token in self._jobs
            # Request a real stop; a transcript that still finishes is kept in
            # history rather than discarded.
            self._request_job_stop(
                request_token,
                delivery=CONCURRENT_TRANSCRIPTION_MODE_HISTORY,
            )
            if self._active_request_token == request_token:
                self._active_request_token = None
                self._last_transcribe_settings = None
            if not had_job:
                self._drop_request_audio(request_token)
                # No job to carry the mark through `_request_job_stop`.
                try:
                    self._last_recording_store.mark_canceled(
                        "Transcription canceled by user."
                    )
                except Exception:
                    self._logger.exception("Failed to mark canceled transcription")
            # Clearing the active transcription may unblock deferred background
            # inserts that were waiting behind it; deliver every completed one now.
            if not self._flush_deferred_background_results(
                ignore_active_transcription=True
            ):
                self._paint_status_keeping_offer("Done", "Transcription canceled.")
            return

        # Preloading can intentionally overlap a recording or a queued batch
        # transcription. It is therefore lower priority than the user's active
        # session and is canceled only when there is no recording/job to stop.
        if self._cancel_model_preload_if_running():
            return

        # Nothing active to cancel, but the hotkey should still deliver any
        # completed results that are stuck pending insertion.
        if not self._flush_deferred_background_results(
            ignore_active_transcription=True
        ):
            self._paint_status_keeping_offer("Done", "Nothing to cancel.")

    def set_overlay_opacity_percent(self, value: int) -> None:
        clamped = max(
            OVERLAY_OPACITY_MIN_PERCENT,
            min(OVERLAY_OPACITY_MAX_PERCENT, int(value)),
        )
        if int(self._settings.overlay_opacity_percent) == clamped:
            return
        self._settings = replace(self._settings, overlay_opacity_percent=clamped)
        try:
            self._settings_store.save(self._settings)
        except Exception as exc:
            self._logger.exception("Failed to persist overlay opacity")
            self._report_unsaved_overlay_setting("The overlay opacity", exc)

    def set_overlay_always_on_top(self, enabled: bool) -> None:
        normalized = bool(enabled)
        if bool(getattr(self._settings, "overlay_always_on_top", True)) == normalized:
            return
        self._settings = replace(self._settings, overlay_always_on_top=normalized)
        try:
            self._settings_store.save(self._settings)
        except Exception as exc:
            self._logger.exception("Failed to persist overlay always-on-top mode")
            self._report_unsaved_overlay_setting("The overlay pin mode", exc)

    def _report_unsaved_overlay_setting(self, what: str, exc: Exception) -> None:
        """Say on the overlay that one of its own controls could not save.

        The opacity slider, the pin button and the Lang menu write straight
        to the store, and the store refuses while its file cannot be read
        (`persistence.StoreUnavailableError`). Only the log heard that
        refusal: the session kept the new value, the file kept the old one,
        and which of the two was real showed at the next start. Reported
        through the same road as the tray's own errors -- the overlay,
        keeping a pending insert offer, or the tray while a recording or a
        transcription owns the overlay (`show_overlay_error`); the in-memory
        value stays, as every other refused save keeps what the user chose.
        """
        self.show_overlay_error(f"{what} was not saved. {exc}")

    def _sync_overlay_language_options(self) -> None:
        supported_modes = language_modes_for_selection(
            self._settings.engine,
            self._settings.model_size,
            self._settings.mode,
        )
        self._overlay.set_language_options(
            supported_modes,
            self._settings.language_mode,
        )

    def set_language_mode(self, mode: str) -> None:
        normalized = str(mode or "").strip().lower()
        supported_modes = language_modes_for_selection(
            self._settings.engine,
            self._settings.model_size,
            self._settings.mode,
        )
        if normalized not in supported_modes:
            self._sync_overlay_language_options()
            return
        if self._settings.language_mode == normalized:
            self._sync_overlay_language_options()
            return

        self._settings = replace(self._settings, language_mode=normalized)
        try:
            self._settings_store.save(self._settings)
        except Exception as exc:
            self._logger.exception("Failed to persist transcription language")
            self._report_unsaved_overlay_setting("The language selection", exc)
        self._sync_overlay_language_options()
        # No runtime teardown and no preload: the language is a per-request
        # parameter for every engine and is applied when the next job acquires
        # the runtime. Reloading here made a mistyped language selection block
        # the correction behind a full model load, and switching language for a
        # single recording evicted the model that the next dictation needs.

    def refresh_overlay_microphone_options(self) -> None:
        """Give the overlay's microphone menu today's devices and selection.

        Run on every settings load, after a device re-enumeration, and when
        the menu is about to open (`OverlayUI.microphone_menu_requested`),
        so a device plugged in since the last refresh is listed.
        `query_input_devices` takes no lock, so this never waits on a
        re-enumeration in progress; it then offers what PortAudio answers.
        """
        selected = self._warm_microphone_selected_device()
        choices = audio_devices.input_device_choices(selected)
        self._overlay.set_microphone_options(
            choices.entries, selected, choices.default_name
        )

    def set_input_device_name(self, name: str) -> None:
        """The overlay's microphone menu: persist the pick, retarget warm.

        Like `set_language_mode`, it writes the setting straight to the
        store and reports a refused save on the overlay. Refused while a
        recording owns the microphone (the overlay disables the button while
        Listening; this covers a pick from a menu left open when a hotkey
        started one): the running capture keeps its device either way, and
        the menu is put back to the setting.
        """
        normalized = str(name or "").strip()
        if (
            self._capture_owns_the_microphone()
            or normalized == self._warm_microphone_selected_device()
        ):
            self.refresh_overlay_microphone_options()
            return
        self._settings = replace(self._settings, input_device_name=normalized)
        try:
            self._settings_store.save(self._settings)
        except Exception as exc:
            self._logger.exception("Failed to persist the microphone selection")
            self._report_unsaved_overlay_setting("The microphone selection", exc)
        self.refresh_overlay_microphone_options()
        # The next cold capture reads the setting (`_build_audio_capture`);
        # a warm stream on the previous device is restarted on the new one.
        self._sync_warm_microphone_stream()

    def set_history_max_items(self, value: int) -> None:
        normalized = max(0, int(value))
        if int(self._settings.history_max_items) == normalized:
            return
        self._settings = replace(self._settings, history_max_items=normalized)

    def _cancel_model_preload_if_running(self) -> bool:
        preload = self._preload_future
        if preload is None or preload.done():
            return False

        self._preload_cancel_requested = True
        self._cancel_preload_generation(self._preload_generation)
        self._terminate_preload_download_process()
        self._paint_status_keeping_offer("Processing", "Canceling model download...")
        return True

    def _note_preload_cleanup(self, generation: int, left_files: int) -> None:
        """Record that a canceled preload left partials it could not remove.

        The Local tab's drain says so; this road ignored the same count and
        painted "Model preload canceled." over gigabytes a scanner still
        held. Keyed by generation so a retired worker's note cannot describe
        the current preload's cancel.
        """
        plural = "" if left_files == 1 else "s"
        with self._preload_result_lock:
            self._preload_cleanup_notes[generation] = (
                f" {left_files} incomplete file{plural} could not be removed: "
                "still in use."
            )

    def _set_preload_download_process(
        self,
        process: subprocess.Popen | None,
        model_dir: str = "",
    ) -> None:
        with self._preload_download_lock:
            self._preload_download_process = process
            if process is None:
                self._preload_downloading_model = None
                self._preload_downloading_dir = ""
            else:
                self._preload_downloading_model = self._preload_target_model
                self._preload_downloading_dir = str(model_dir or "")

    def preload_downloading_model(self) -> tuple[str, str] | None:
        """Model and target directory the preload is downloading, if any.

        The settings dialog runs its own download queue and knows nothing about
        this one. Selecting a missing model and saving starts a download here,
        which then ran invisibly: the Local tab still listed the model as "Not
        downloaded" while bytes were arriving, and a second download started
        from that tab competed with it for the same link. The directory is part
        of the answer because the dialog can be pointed at a different Model Dir
        than the one this download is filling.
        """
        with self._preload_download_lock:
            name = self._preload_downloading_model
            if not name:
                return None
            return name, self._preload_downloading_dir

    def _terminate_preload_download_process(self) -> None:
        with self._preload_download_lock:
            process = self._preload_download_process
            self._preload_download_process = None
            self._preload_downloading_model = None
            self._preload_downloading_dir = ""

        if process is None:
            return
        terminate_model_download_process(process)
        # Nobody reads a canceled download's error message, so the reaping the
        # Models tab gets for free out of `model_download_process_error` never
        # happened here: the reader thread, the stdout pipe and the spooled
        # stderr file stayed with this process until the `Popen` was
        # collected. This releases them and waits for nothing -- five of the
        # six callers of this method are on the Qt thread.
        release_model_download_process(process)

    def _download_model_for_preload(
        self,
        settings: AppSettings,
        generation: int | None = None,
    ) -> None:
        from .transcriber.local_faster_whisper import find_cached_models

        use_legacy_cancel_flag = generation is None
        generation = self._preload_generation if generation is None else generation
        if self._preload_generation_was_canceled(generation) or (
            use_legacy_cancel_flag and self._preload_cancel_requested
        ):
            raise RuntimeError("Model download canceled.")
        # Before any download: on Windows ARM64 (no CTranslate2) the preload
        # fetched a whole Whisper model only to fail loading it afterwards.
        reason = local_runtime_support.unavailable_reason(settings.model_size)
        if reason:
            raise RuntimeError(reason)
        if getattr(settings, "offline_mode", False):
            return

        model_name = settings.model_size
        model_dir = getattr(settings, "model_dir", "")
        cached = find_cached_models(model_dir)
        if model_name in cached:
            return

        # Every download in the process goes through one coordinator. Without
        # it this path and the Local tab each spawned a worker against the same
        # cache directory: the second one then measured a directory the first
        # owned and sat at 0% forever.
        def _canceled() -> bool:
            return self._preload_generation_was_canceled(generation) or (
                use_legacy_cancel_flag and self._preload_cancel_requested
            )

        coordinator = model_download_coordinator()
        try:
            outcome = coordinator.acquire(
                model_name,
                model_dir,
                explicit=False,
                cancel_check=_canceled,
            )
        except ModelDownloadCanceled as exc:
            raise RuntimeError("Model download canceled.") from exc
        if outcome == ACQUIRE_JOINED:
            # The Local tab (or an earlier preload) just finished this exact
            # model while we waited; nothing left to fetch.
            return

        succeeded = False
        try:
            try:
                process = start_model_download_process(model_name, model_dir)
            except Exception as exc:
                raise RuntimeError(f"Failed to start model download: {exc}") from exc

            self._set_preload_download_process(process, model_dir)
            try:
                while True:
                    if _canceled():
                        self._terminate_preload_download_process()
                        # Keep the partial bytes when the user explicitly asked
                        # for this model in the Local tab: that request is
                        # waiting to resume from them, and wiping them made a
                        # multi-gigabyte download restart from zero.
                        if not coordinator.has_explicit_interest(model_name, model_dir):
                            from .transcriber.local_faster_whisper import (
                                cleanup_incomplete_model_download,
                            )

                            left = cleanup_incomplete_model_download(
                                model_name, model_dir
                            ).left_files
                            if left:
                                self._note_preload_cleanup(generation, left)
                        raise RuntimeError("Model download canceled.")
                    returncode = process.poll()
                    if returncode is not None:
                        if returncode != 0:
                            detail = model_download_process_error(process)
                            suffix = f": {detail}" if detail else "."
                            raise RuntimeError(
                                f"Model download failed for '{model_name}'{suffix}"
                            )
                        model_download_process_error(process)
                        succeeded = True
                        return
                    time.sleep(0.2)
            finally:
                self._set_preload_download_process(None)
        finally:
            coordinator.release(model_name, model_dir, succeeded=succeeded)

    def _register_hotkey_with_fallback(self) -> bool:
        """Register the recording hotkey, falling back if it is already taken.

        The user's chosen hotkey is never overwritten in settings. Another
        process holding it (a terminal, an IDE) is a temporary condition, and
        persisting the fallback used to make it permanent: once the other app
        closed, the app had already forgotten what the user actually wanted.
        The preference stays, the fallback is a runtime-only substitution, and
        `_reclaim_preferred_hotkey` keeps trying to take the real one back.
        """
        preferred = self._settings.hotkey
        try:
            self._hotkey_manager.register(preferred)
            self._hotkey_notice = None
            self._active_hotkey = preferred
            self._stop_hotkey_reclaim()
            return True
        except (HotkeyRegistrationError, ValueError) as exc:
            self._logger.warning("Preferred hotkey %s unavailable: %s", preferred, exc)

        # Never take a combination the user has assigned to one of this
        # app's own optional hotkeys. `_register_hotkey_with_fallback` runs
        # first, so a fallback that collided would make the cancel, overlay
        # or re-paste registration fail afterwards with "in use by another
        # program" -- the other program being this one.
        # Compare the parsed (modifiers, key) pair, not the typed string.
        # "Ctrl+Win+F9", "Win+Ctrl+F9", "Control+Win+F9" and "Ctrl + Win + F9"
        # are the same hotkey to Windows but four different strings, so a
        # hand-edited settings.json walked straight past a text comparison.
        reserved = set()
        for combo in (
            getattr(self._settings, "cancel_hotkey", ""),
            getattr(self._settings, "show_overlay_hotkey", ""),
            getattr(self._settings, "repaste_hotkey", ""),
        ):
            if not combo or not combo.strip():
                continue
            try:
                reserved.add(parse_hotkey(combo))
            except (ValueError, TypeError):
                # An unparsable stored hotkey cannot be registered either,
                # so it cannot collide with anything.
                continue
        for fallback in FALLBACK_HOTKEYS:
            if fallback == preferred:
                continue
            try:
                fallback_key = parse_hotkey(fallback)
            except (ValueError, TypeError):
                self._logger.error(
                    "Fallback hotkey %s is not a valid combination.", fallback
                )
                continue
            if fallback_key in reserved:
                self._logger.info(
                    "Skipping fallback hotkey %s: it is assigned to another "
                    "action in this app.",
                    fallback,
                )
                continue
            try:
                self._hotkey_manager.register(fallback)
            except (HotkeyRegistrationError, ValueError) as exc:
                self._logger.warning(
                    "Fallback hotkey %s unavailable: %s", fallback, exc
                )
                continue
            self._active_hotkey = fallback
            self._hotkey_notice = (
                f"'{preferred}' is used by another program; taken back "
                "automatically once it is free."
            )
            self._start_hotkey_reclaim()
            return True

        self._active_hotkey = ""
        self._hotkey_notice = (
            f"'{preferred}' and every fallback are in use by other programs. "
            "Pick a different hotkey in Settings."
        )
        self._start_hotkey_reclaim()
        return False

    def _start_hotkey_reclaim(self) -> None:
        timer = getattr(self, "_hotkey_reclaim_timer", None)
        if timer is not None and not timer.isActive():
            timer.start()

    def _stop_hotkey_reclaim(self) -> None:
        timer = getattr(self, "_hotkey_reclaim_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()

    @QtCore.Slot()
    def _reclaim_preferred_hotkey(self) -> None:
        """Take the preferred hotkey back once the other program releases it."""
        if self._shutdown_started:
            self._stop_hotkey_reclaim()
            return
        preferred = self._settings.hotkey
        if self._active_hotkey == preferred:
            self._stop_hotkey_reclaim()
            return
        if not self._active_hotkey:
            # Nothing is registered at all: the preferred key and every
            # fallback were busy at startup. Retrying only the preferred one
            # means the app never notices when a *fallback* frees up, and
            # the user stays with no hotkey until they open Settings and
            # save. Re-run the whole chain instead.
            if self._transcription_runtime_active():
                return
            if self._register_hotkey_with_fallback():
                self._hotkey_registration_ok = True
                self.show_idle_status()
            return
        # Never swap the binding out from under a running dictation.
        if self._transcription_runtime_active():
            return
        # `HotkeyManager.register` unregisters the current binding *before*
        # trying the new one and does not put it back on failure. Attempting a
        # reclaim that fails therefore destroys the working fallback and leaves
        # the user with no hotkey at all — worse than the problem this timer
        # exists to solve, and invisible, because the idle line would still
        # advertise the fallback.
        previously_active = self._active_hotkey
        try:
            self._hotkey_manager.register(preferred)
        except (HotkeyRegistrationError, ValueError):
            self._restore_hotkey_after_failed_reclaim(previously_active)
            return
        self._active_hotkey = preferred
        self._hotkey_notice = None
        # A total registration failure earlier left this False, and
        # `show_idle_status` short-circuits to the Error state while it is —
        # so a successful reclaim would fix the hotkey but pin the overlay to
        # "Hotkey registration failed" until the user saved Settings.
        self._hotkey_registration_ok = True
        self._stop_hotkey_reclaim()
        self._logger.info("Reclaimed the preferred hotkey %s", preferred)
        self.show_idle_status()

    def _restore_hotkey_after_failed_reclaim(self, previously_active: str) -> None:
        """Put the fallback back after a failed attempt to reclaim the preferred key."""
        if not previously_active:
            return
        try:
            self._hotkey_manager.register(previously_active)
        except (HotkeyRegistrationError, ValueError):
            self._logger.error(
                "Reclaiming %r failed and the fallback %r could not be restored; "
                "no recording hotkey is registered.",
                self._settings.hotkey,
                previously_active,
            )
            self._active_hotkey = ""
            self._hotkey_registration_ok = False
            self._hotkey_notice = (
                f"'{previously_active}' was lost while trying to take "
                f"'{self._settings.hotkey}' back, and neither could be "
                "registered. Pick a different hotkey in Settings."
            )
            self.show_idle_status()
            return
        self._logger.debug(
            "Preferred hotkey %r still in use; kept the fallback %r.",
            self._settings.hotkey,
            previously_active,
        )

    def _register_cancel_hotkey(self) -> bool:
        manager = self._cancel_hotkey_manager
        if manager is None:
            self._cancel_hotkey_notice = None
            return True

        cancel_hotkey = (self._settings.cancel_hotkey or "").strip()
        if not cancel_hotkey:
            self._cancel_hotkey_notice = None
            try:
                manager.unregister()
                return True
            except HotkeyRegistrationError:
                self._logger.exception("Failed to unregister disabled cancel hotkey")
                self._cancel_hotkey_notice = (
                    "The disabled cancel hotkey could not be unregistered. "
                    "Restart the app before reusing that key combination."
                )
                return False

        try:
            manager.register(cancel_hotkey)
            self._cancel_hotkey_notice = None
            return True
        except (HotkeyRegistrationError, ValueError):
            self._logger.exception(
                "Failed to register cancel hotkey: %s", cancel_hotkey
            )
            self._cancel_hotkey_notice = (
                f"Cancel hotkey registration failed ({cancel_hotkey}). "
                f"Use another key combo (default: {DEFAULT_CANCEL_HOTKEY})."
            )
            return False

    def _register_repaste_hotkey(self) -> bool:
        manager = self._repaste_hotkey_manager
        if manager is None:
            self._repaste_hotkey_notice = None
            return True

        repaste_hotkey = (
            str(getattr(self._settings, "repaste_hotkey", "") or "")
        ).strip()
        if not repaste_hotkey:
            self._repaste_hotkey_notice = None
            try:
                manager.unregister()
                return True
            except HotkeyRegistrationError:
                self._logger.exception("Failed to unregister disabled re-paste hotkey")
                self._repaste_hotkey_notice = (
                    "The disabled re-paste hotkey could not be unregistered. "
                    "Restart the app before reusing that key combination."
                )
                return False

        try:
            manager.register(repaste_hotkey)
            self._repaste_hotkey_notice = None
            return True
        except (HotkeyRegistrationError, ValueError):
            self._logger.exception(
                "Failed to register re-paste hotkey: %s", repaste_hotkey
            )
            self._repaste_hotkey_notice = (
                f"Re-paste hotkey registration failed ({repaste_hotkey}). "
                "Use another key combo or clear it in Settings."
            )
            return False

    def _register_show_overlay_hotkey(self) -> bool:
        manager = self._show_overlay_hotkey_manager
        if manager is None:
            self._show_overlay_hotkey_notice = None
            return True

        show_overlay_hotkey = (self._settings.show_overlay_hotkey or "").strip()
        if not show_overlay_hotkey:
            self._show_overlay_hotkey_notice = None
            try:
                manager.unregister()
                return True
            except HotkeyRegistrationError:
                self._logger.exception(
                    "Failed to unregister disabled show-overlay hotkey"
                )
                self._show_overlay_hotkey_notice = (
                    "The disabled show-overlay hotkey could not be unregistered. "
                    "Restart the app before reusing that key combination."
                )
                return False

        try:
            manager.register(show_overlay_hotkey)
            self._show_overlay_hotkey_notice = None
            return True
        except (HotkeyRegistrationError, ValueError):
            self._logger.exception(
                "Failed to register show-overlay hotkey: %s", show_overlay_hotkey
            )
            self._show_overlay_hotkey_notice = (
                f"Show-overlay hotkey registration failed ({show_overlay_hotkey}). "
                "Use another key combo or clear it in Settings."
            )
            return False

    def _release_all_global_hotkeys(self) -> None:
        """Give every combination this app holds back to Windows.

        `HotkeyManager.register` unregisters only its *own* id, and Windows
        refuses a combination another id already holds -- including one of
        ours. Registering the four in id order therefore fails for any change
        that moves a combination from a later id onto an earlier one: the
        show-overlay hotkey taking over the re-paste hotkey's combination, or
        the two being swapped. Save-time validation does not see it, because
        the *saved* set has no conflict; the collision exists only during the
        re-registration. Measured against the real `RegisterHotKey` with both
        ids on one thread: error 1409, the show-overlay hotkey left
        unregistered, and the combination it wanted free the moment the later
        id gave it up. Only the recording hotkey recovers by itself, through
        the reclaim timer -- the other three stay dead until the app restarts.

        A manager whose `unregister` fails keeps holding its combination and
        stays marked registered, which is the pre-existing behaviour: whichever
        registration then collides reports it through its own notice. Keep
        releasing the rest regardless.

        The recording entry is inert for the registration pass -- `register`
        unregisters its own id first and nothing registers before id 1, so
        removing it survives every collision test. `shutdown` shares this
        helper and does need it, which is what pins it.
        """
        managers = (
            ("recording", self._hotkey_manager),
            ("cancel", self._cancel_hotkey_manager),
            ("show-overlay", self._show_overlay_hotkey_manager),
            ("re-paste", self._repaste_hotkey_manager),
        )
        for label, manager in managers:
            if manager is None:
                continue
            try:
                manager.unregister()
            except Exception:
                self._logger.exception("Failed to unregister %s hotkey", label)

    def _register_all_global_hotkeys(self) -> bool:
        """Release every combination we hold, then claim the configured four."""
        self._release_all_global_hotkeys()
        self._hotkey_registration_ok = self._register_hotkey_with_fallback()
        self._cancel_hotkey_registration_ok = self._register_cancel_hotkey()
        self._show_overlay_hotkey_registration_ok = self._register_show_overlay_hotkey()
        self._repaste_hotkey_registration_ok = self._register_repaste_hotkey()
        # The badge names the re-paste hotkey only while it is registered:
        # a save, the startup and every resume land here.
        self._update_not_inserted_badge()
        return (
            self._hotkey_registration_ok
            and self._cancel_hotkey_registration_ok
            and self._show_overlay_hotkey_registration_ok
            and self._repaste_hotkey_registration_ok
        )

    def _hotkey_registration_state(self) -> tuple:
        """Everything the idle line prints about the four registrations."""
        return (
            self._active_hotkey,
            self._hotkey_registration_ok,
            self._hotkey_notice,
            self._cancel_hotkey_registration_ok,
            self._cancel_hotkey_notice,
            self._show_overlay_hotkey_registration_ok,
            self._show_overlay_hotkey_notice,
            self._repaste_hotkey_registration_ok,
            self._repaste_hotkey_notice,
        )

    def refresh_hotkey_registration(self) -> None:
        """Re-register global hotkeys after Windows resumes or opens Explorer.

        Repainted only when the registration state changed. Every other
        writer of that state repaints the idle line (`reload_settings`, the
        reclaim timer, a failed reclaim); this one did not, so a resume that
        substituted a fallback -- or lost every combination -- left the
        overlay advertising a key that no longer fired, and one that repaired
        an earlier failure left it on Error until the next save. But an
        *unconditional* repaint was the wrong fix: this runs after every wake
        from sleep -- with `restore_visibility` right behind it -- and 500 ms
        after the two "open recordings folder" buttons, and it painted "Idle"
        over a finished Done transcript and over the Error whose Insert
        button is the only way to recover a failed streaming tail. A resume
        that changed nothing now paints nothing, as before; one that did
        repaints through `show_idle_status`, which keeps a pending Insert
        offer in the line it paints.
        """
        before = self._hotkey_registration_state()
        if not self._register_all_global_hotkeys():
            self._logger.warning("Global hotkey refresh did not fully succeed.")
        if self._hotkey_registration_state() != before:
            self.show_idle_status()
