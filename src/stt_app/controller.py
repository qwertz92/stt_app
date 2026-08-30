from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from PySide6 import QtCore, QtGui

from . import audio_devices
from .app_paths import resolve_recordings_dir
from .audio_capture import AudioCapture, AudioCaptureError, WarmMicrophoneStream
from .audio_device_listener import AudioDeviceChangeListener
from .config import (
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
    DEFAULT_SILENCE_GATE_THRESHOLD,
    DEFAULT_START_BEEP_TONE,
    DOC_MODELS_PATH,
    FALLBACK_HOTKEYS,
    HOTKEY_RECLAIM_INTERVAL_MS,
    INSERT_TARGET_CURRENT_WINDOW,
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
    STREAMING_REVISION_WORD_WINDOW,
    STREAMING_STABLE_WORD_GUARD,
    VAD_ENERGY_THRESHOLD_MIN,
    VAD_MAX_SILENCE_MS,
    VAD_MIN_SPEECH_MS,
    VALID_START_BEEP_TONES,
    language_modes_for_selection,
    nemotron_provider_order,
    supports_streaming,
)
from .hotkey import HotkeyManager, HotkeyRegistrationError, parse_hotkey
from .last_recording_store import LastRecordingStore
from .local_model_download import (
    model_download_process_error,
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
from .settings_store import AppSettings, SettingsStore
from .streaming_text import (
    StreamingTextState,
    normalize_stream_text,
)
from .text_inserter import (
    TextInserter,
    TextInsertionError,
    TextMayHaveBeenPastedError,
)
from .transcriber import create_transcriber
from .transcriber.base import TranscriptionCanceled, TranscriptionError
from .transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore
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
_EMPTY_MODEL_TRANSCRIPT_MESSAGE = (
    "The model returned no text for this recording."
)

# The stages of a local model preload. They fail, progress and finish for
# entirely different reasons, and only the download has measurable progress --
# a queued preload and a running load both have none, for opposite reasons.
_PRELOAD_PHASE_QUEUED = "queued"
_PRELOAD_PHASE_DOWNLOAD = "download"
_PRELOAD_PHASE_LOAD = "load"


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
    source_audio_path: str = ""
    future: object | None = None
    # The provider handshake thread, when this job finalizes a stream that may
    # still be connecting. The worker joins it before calling `stop_stream()`.
    connect_thread: object | None = None
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
}

# Which ``AppSettings`` field carries the model name each remote engine sends.
# Only one is ever read, so they collapse into a single identity slot: editing
# the Groq model must not reload a loaded local model.
_ENGINE_MODEL_FIELDS: dict[str, str] = {
    "assemblyai": "assemblyai_model",
    "openai": "openai_model",
    "groq": "groq_model",
    "deepgram": "deepgram_model",
    "elevenlabs": "elevenlabs_model",
    "azure": "azure_speech_model",
    "funasr": "funasr_model",
}

# Remote engines that pass the biasing prompt through to their provider. The
# rest expose no such input, so the setting cannot change their runtime.
_ENGINES_USING_CUSTOM_VOCABULARY = frozenset(
    {DEFAULT_ENGINE, "assemblyai", "openai", "groq", "deepgram"}
)


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
    custom_vocabulary: str = ""
    silence_gate_enabled: bool = False
    silence_gate_threshold: float = 0.0
    # Remote engines only. One slot, because exactly one provider's model is
    # read for a given engine.
    remote_model: str = ""
    azure_endpoint: str = ""
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
    stream_runtime_failed = QtCore.Signal(str)
    # generation, ok, error text -- a remote handshake finished off-thread
    stream_connect_finished = QtCore.Signal(int, bool, str)
    stream_abort_requested = QtCore.Signal(str, bool)
    model_preload_done = QtCore.Signal(int, bool, str)  # generation, success, message
    # A queued transcription failed while a newer session owns the overlay.
    background_transcription_failed = QtCore.Signal(str)
    # A queued transcription succeeded but its text could not be pasted. Without
    # this the loss was visible only in the log file.
    background_insertion_failed = QtCore.Signal(str)
    # Emitted from MMDevice API worker threads; the queued connection marshals
    # the reaction onto the Qt thread.
    audio_devices_changed = QtCore.Signal(str)

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
    ) -> None:
        super().__init__()
        self._settings_store = settings_store
        self._hotkey_manager = hotkey_manager
        self._cancel_hotkey_manager = cancel_hotkey_manager
        self._show_overlay_hotkey_manager = show_overlay_hotkey_manager
        self._repaste_hotkey_manager = repaste_hotkey_manager
        self._overlay = overlay
        self._text_inserter = text_inserter
        self._logger = logger
        self._window_focus_helper = window_focus_helper or Win32WindowFocusHelper()
        self._secret_store = secret_store
        self._history_store = history_store or TranscriptHistoryStore()
        self._last_recording_store = last_recording_store or LastRecordingStore()

        self._settings: AppSettings = self._settings_store.load()
        self._audio_capture: AudioCapture | None = None
        self._warm_mic_stream: WarmMicrophoneStream | None = None
        self._audio_device_listener: AudioDeviceChangeListener | None = None
        self._pending_audio_device_refresh = False
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
        self._last_transcript: str = ""
        self._last_history_entry: TranscriptHistoryEntry | None = None
        self._last_failed_wav_bytes: bytes = b""
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
        self._active_stream_transcriber = None
        self._active_stream_runtime_lease: _TranscriberRuntimeLease | None = None
        self._active_stream_settings: AppSettings | None = None
        self._stream_chunk_error_reported = False
        self._stream_abort_requested = False
        # True while another window holds focus during a live stream: the
        # session keeps recording, but nothing is pasted until it stops.
        self._stream_insertion_suspended = False
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
        self._audio_device_change_timer = QtCore.QTimer(self)
        self._audio_device_change_timer.setSingleShot(True)
        self._audio_device_change_timer.setInterval(AUDIO_DEVICE_CHANGE_SETTLE_MS)
        self._audio_device_change_timer.timeout.connect(
            self._on_audio_device_change_settled
        )
        self._preload_progress_timer = QtCore.QTimer(self)
        self._preload_progress_timer.setInterval(600)
        self._preload_progress_timer.timeout.connect(self._on_preload_progress_poll)
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
        if self._shutdown_started:
            return
        self._shutdown_started = True
        try:
            self._hotkey_manager.unregister()
        except Exception:
            self._logger.exception("Failed to unregister recording hotkey")
        if self._cancel_hotkey_manager is not None:
            try:
                self._cancel_hotkey_manager.unregister()
            except Exception:
                self._logger.exception("Failed to unregister cancel hotkey")
        if self._show_overlay_hotkey_manager is not None:
            try:
                self._show_overlay_hotkey_manager.unregister()
            except Exception:
                self._logger.exception("Failed to unregister show-overlay hotkey")
        if self._repaste_hotkey_manager is not None:
            try:
                self._repaste_hotkey_manager.unregister()
            except Exception:
                self._logger.exception("Failed to unregister re-paste hotkey")
        self._focus_poll_timer.stop()
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
        if self._audio_capture is not None:
            try:
                self._audio_capture.stop()
            except Exception:
                pass
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
                self._logger.exception(
                    "Failed to shut down the %s executor", name
                )

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
        self._sync_warm_microphone_stream()
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
            self._hotkey_registration_ok = self._register_hotkey_with_fallback()
            self._cancel_hotkey_registration_ok = self._register_cancel_hotkey()
            self._show_overlay_hotkey_registration_ok = (
                self._register_show_overlay_hotkey()
            )
            self._repaste_hotkey_registration_ok = self._register_repaste_hotkey()
        else:
            self._hotkey_registration_ok = True
            self._hotkey_notice = None
            self._cancel_hotkey_registration_ok = True
            self._cancel_hotkey_notice = None
            self._show_overlay_hotkey_registration_ok = True
            self._show_overlay_hotkey_notice = None
            self._repaste_hotkey_registration_ok = True
            self._repaste_hotkey_notice = None

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
        if not self._hotkey_registration_ok:
            self._overlay.set_state(
                "Error",
                self._hotkey_notice or "Hotkey registration failed.",
            )
            return
        if not self._cancel_hotkey_registration_ok:
            self._overlay.set_state(
                "Error",
                self._cancel_hotkey_notice or "Cancel hotkey registration failed.",
            )
            return
        if not self._show_overlay_hotkey_registration_ok:
            self._overlay.set_state(
                "Error",
                self._show_overlay_hotkey_notice
                or "Show-overlay hotkey registration failed.",
            )
            return
        if not self._repaste_hotkey_registration_ok:
            self._overlay.set_state(
                "Error",
                self._repaste_hotkey_notice
                or "Re-paste hotkey registration failed.",
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
        self._overlay.set_state("Idle", detail)

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
                self._overlay.set_state(
                    "Error",
                    f"Model is still {self._preload_phase_word()}. Streaming "
                    "starts after the selected model is ready.",
                )
                return

            preload_failure = self._model_preload_failure(self._settings)
            if preload_failure is not None:
                self._overlay.set_state("Error", preload_failure)
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
                self._overlay.set_state(
                    "Error",
                    detail,
                )
                return

            # Do not invite the user to speak until the microphone has actually
            # started. Opening a cold device (or a remote streaming session) can
            # take seconds on a locked-down machine, and audio spoken before
            # ``capture.start()`` completes is irretrievably lost.
            self._set_listening_overlay(
                "Starting dictation. Please wait for the 'Speak now' message."
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

    def _set_listening_overlay(self, detail: str) -> None:
        self._overlay.set_state("Listening", detail, compact=True)
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
        with self._stream_preconnect_lock:
            self._stream_preconnect_chunks = []
            self._stream_preconnect_dropped = False

        def _connect() -> None:
            try:
                transcriber.start_stream(
                    on_partial=self._emit_stream_partial,
                    on_error=self._emit_stream_runtime_failure,
                )
            except BaseException as exc:
                # Stop the audio callback before dropping the buffer.
                # Otherwise it falls through to push_audio_chunk on a
                # transcriber whose start_stream just raised, and *that*
                # error reaches the user first -- "Streaming chunk push
                # failed: session is not active" instead of the real cause,
                # such as an invalid API key.
                self._stream_chunk_error_reported = True
                self._discard_preconnect_buffer(generation)
                self.stream_connect_finished.emit(
                    generation, False, self._stream_connect_error_text(exc)
                )
                return
            # Flush here, on this thread, while still ordered ahead of any
            # further callback: `_on_stream_audio_chunk` keeps appending to the
            # buffer until it is cleared under the same lock.
            failure = self._flush_preconnect_buffer(generation, transcriber)
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
        """Hand buffered audio to the now-ready stream. Returns an error text."""
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
                try:
                    transcriber.push_audio_chunk(chunk)
                except Exception as exc:
                    self._logger.exception("Failed to flush buffered stream audio")
                    return f"Streaming chunk push failed: {exc}"
        if dropped:
            self._logger.warning(
                "streaming_preconnect_buffer_overflow: the provider took too "
                "long to connect and buffered audio was dropped."
            )
        return None

    def _discard_preconnect_buffer(self, generation: int) -> None:
        with self._stream_preconnect_lock:
            if generation == self._stream_connect_generation:
                self._stream_preconnect_chunks = None

    def _on_stream_connect_finished(
        self, generation: int, ok: bool, error_text: str
    ) -> None:
        if generation != self._stream_connect_generation:
            return  # a newer session replaced this one while it connected
        if not ok:
            self._on_stream_runtime_failed(error_text or "Streaming failed to start.")
            return
        if not self._streaming_recording or self._stream_abort_requested:
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
        self._audio_callback_watchdog_timer.start(
            AUDIO_CAPTURE_FIRST_CALLBACK_TIMEOUT_MS
        )

    def _cancel_audio_callback_watchdog(
        self,
        capture: AudioCapture | None = None,
    ) -> None:
        if (
            capture is not None
            and self._audio_callback_watchdog_capture is not capture
        ):
            return
        self._audio_callback_watchdog_timer.stop()
        self._audio_callback_watchdog_capture = None

    def _on_audio_callback_watchdog_timeout(self) -> None:
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

        self._logger.error(
            "audio_capture_callback_timeout mode=%s timeout_ms=%d "
            "warm_stream=%s callback_count=%d",
            self._active_session_mode,
            AUDIO_CAPTURE_FIRST_CALLBACK_TIMEOUT_MS,
            warm_stream,
            callback_count,
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
        wav_bytes, _ = self._stop_active_capture(persist_audio=False)
        self._last_failed_wav_bytes = bytes(wav_bytes)
        if wav_bytes and self._persist_last_recording_audio(wav_bytes):
            try:
                self._last_recording_store.mark_failed(detail)
            except Exception:
                self._logger.exception("Failed to mark stalled recording")
        self._reset_streaming_state()
        self._overlay.set_state("Error", detail)
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
        input_device_name = str(
            getattr(self._settings, "input_device_name", "") or ""
        )
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
            opened = stream.opened_device_key
            selected = str(
                getattr(self._settings, "input_device_name", "") or ""
            )
            if opened is None:
                # Not running (earlier open failed or still starting); a
                # settings save is a natural retry point.
                self._start_warm_microphone_stream_async()
            elif opened != selected:
                stream.request_restart()

    def _warm_microphone_device(self) -> tuple[str, int | None]:
        """Resolve the currently selected microphone for a warm-stream open."""
        name = str(getattr(self._settings, "input_device_name", "") or "")
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

    def _on_audio_device_change_settled(self) -> None:
        if self._shutdown_started:
            return
        if (
            self._audio_capture is not None
            or self._recording_start_in_progress
            or self._recording_stop_in_progress
        ):
            # Never touch devices mid-recording; retried once the capture
            # stops via _maybe_resume_pending_audio_device_refresh.
            self._pending_audio_device_refresh = True
            return
        self._pending_audio_device_refresh = False
        threading.Thread(
            target=self._refresh_audio_devices_worker,
            name="stt_app_audio_device_refresh",
            daemon=True,
        ).start()

    def _refresh_audio_devices_worker(self) -> None:
        """Close the idle warm stream, re-enumerate PortAudio, reopen warm.

        Runs off the Qt thread because PortAudio calls can block for seconds
        on locked-down audio stacks. ``try_refresh_input_devices`` refuses to
        re-initialize while any stream is live, so a recording that slips in
        concurrently is never torn down; the refresh is retried later instead.
        """
        warm = self._warm_mic_stream
        if warm is not None and not warm.close_if_idle():
            self._pending_audio_device_refresh = True
            return
        if not audio_devices.try_refresh_input_devices(self._logger):
            self._pending_audio_device_refresh = True
        if self._shutdown_started:
            return
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
            self._persist_last_recording_audio(wav_bytes)
            source_audio_path = self._save_recording_artifacts(capture, wav_bytes)

            if self._streaming_recording:
                self._focus_poll_timer.stop()
                if self._stream_abort_requested:
                    self._abort_streaming_session(
                        "Streaming aborted.",
                        beep=False,
                        finalize_stream=False,
                    )
                    return
                self._overlay.set_state(
                    "Processing", "Finalizing streaming transcript..."
                )
                self._submit_stream_finalize(source_audio_path=source_audio_path)
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

            if self._silence_gate_blocks(wav_bytes):
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
            )
        finally:
            pending_toggles = self._pending_toggle_after_stop_count
            self._pending_toggle_after_stop_count = 0
            self._recording_stop_in_progress = False
            self._flush_deferred_background_results()
            self._maybe_resume_pending_audio_device_refresh()
            if pending_toggles % 2 == 1 and self._audio_capture is None:
                self._logger.info(
                    "Applying queued hotkey start after recording stop completed."
                )
                QtCore.QTimer.singleShot(0, self.start_recording)

    def _silence_gate_blocks(self, wav_bytes: bytes) -> bool:
        """Skip transcription when the recording never rises above silence.

        Speech models hallucinate words from pure silence, so an opt-in gate
        checks the loudest 100 ms window of the recording against a
        user-tunable threshold (kept low so whispering still passes). The
        measured level is always logged so the threshold is easy to tune, and
        a gated recording stays available as the last recording for a manual
        retry via History -> Use last recording.
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
        self._logger.info(
            "recording_peak_level level=%.4f silence_gate_enabled=%s threshold=%.4f",
            peak_level,
            enabled,
            threshold,
        )
        if not enabled or peak_level >= threshold:
            return False
        try:
            self._last_recording_store.mark_canceled(
                "Recording skipped by the silence gate."
            )
        except Exception:
            self._logger.exception("Failed to mark silence-gated recording")
        self._overlay.set_state(
            "Done",
            (
                f"No speech detected (loudest 100 ms {peak_level:.4f}, gate "
                f"{threshold:.4f}). Nothing was transcribed; the recording is "
                "kept. If this was speech, lower the silence gate in "
                "Settings -> Audio & Recording."
            ),
        )
        return True

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
        synchronous and there is no capture to keep the tone away from —
        blocking the Qt thread for 50-150 ms after every insert would be pure
        latency.
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
        threading.Thread(
            target=self._play_tone,
            args=(tone,),
            name="stt_app_completion_beep",
            daemon=True,
        ).start()

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
        if not wav_bytes:
            return False
        try:
            self._last_recording_store.save_recording(
                wav_bytes,
                keep_after_success=self._settings.save_last_wav,
            )
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
        keep = max(1, int(keep_count or 1))
        try:
            files = [
                os.path.join(directory, name)
                for name in os.listdir(directory)
                if _ARCHIVED_RECORDING_NAME_RE.fullmatch(name)
            ]
        except OSError:
            return
        files.sort(key=lambda path: os.path.getmtime(path))
        while len(files) > keep:
            oldest = files.pop(0)
            try:
                os.remove(oldest)
            except OSError:
                break

    def _reset_streaming_state(self) -> None:
        self._focus_poll_timer.stop()
        # Retire any handshake still in flight. Bumping the generation is what
        # stops a late flush from pushing this session's audio into the next
        # one, and stops its completion signal from touching the overlay.
        self._stream_connect_generation += 1
        with self._stream_preconnect_lock:
            self._stream_preconnect_chunks = None
            self._stream_preconnect_dropped = False
        self._stream_abort_requested = False
        self._stream_insertion_suspended = False
        self._stream_insert_failures = 0
        self._stream_text_state.reset()
        self._active_batch_settings = None
        self._active_session_mode = "batch"
        self._streaming_recording = False
        self._target_window_handle = None
        self._target_focus_signature = None

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
                # same runtime a second time -- and that second raise replaced the
                # original, so the caller got the close's exception instead of the
                # `TranscriptionCanceled` this branch exists to deliver.
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
            self._logger.exception(
                "Failed to restart the warm microphone after resume"
            )

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
        if settings.engine == "groq":
            return settings.groq_model
        if settings.engine == "openai":
            return settings.openai_model
        if settings.engine == "deepgram":
            return getattr(settings, "deepgram_model", "")
        if settings.engine == "assemblyai":
            return getattr(settings, "assemblyai_model", "")
        if settings.engine == "elevenlabs":
            return getattr(settings, "elevenlabs_model", "")
        if settings.engine == "azure":
            return getattr(settings, "azure_speech_model", "")
        if settings.engine == "funasr":
            return getattr(settings, "funasr_model", "")
        return settings.model_size

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

    def _mark_last_recording_completed(self) -> None:
        try:
            self._last_recording_store.mark_completed()
        except Exception:
            self._logger.exception("Failed to finalize last recording state")

    def _promote_request_audio_for_retry(self, request_token: int) -> bool:
        payload = self._request_audio_by_token.pop(request_token, None)
        if payload is None:
            return False
        wav_bytes, _settings = payload
        self._last_failed_wav_bytes = wav_bytes
        return True

    def _drop_request_audio(self, request_token: int) -> None:
        self._request_audio_by_token.pop(request_token, None)

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
        return (
            self._active_request_token == request_token
            and not self._new_recording_active()
            and not (job is not None and job.aborting)
        )

    def _register_transcription_job(
        self,
        request_token: int,
        settings: AppSettings,
        mode: str,
        *,
        source_audio_path: str = "",
    ) -> _TranscriptionJob:
        """Track a submitted transcription for the queue and target insertion.

        The current target window/signature are snapshotted now so the result
        can later be inserted into the window that was focused for this
        recording, even after a newer recording reused the shared target state.
        """
        job = _TranscriptionJob(
            token=request_token,
            engine=settings.engine,
            model=self._selected_model_name(settings),
            mode=mode,
            settings=replace(settings),
            target_handle=self._target_window_handle,
            target_signature=self._target_focus_signature,
            source_recording_id=self._current_last_recording_id(),
            source_audio_path=str(source_audio_path or "").strip(),
        )
        self._jobs[request_token] = job
        self._update_queue_overlay()
        return job

    def _finish_transcription_job(self, request_token: int | None) -> None:
        if request_token is None:
            return
        self._remove_deferred_background_result(request_token)
        job = self._jobs.pop(request_token, None)
        if job is not None:
            job.insertion_deferred = False
            self._update_queue_overlay()

    def _queue_job_label(
        self,
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
        status = " · Pending insert" if job.insertion_deferred else ""
        return f"{rank_label} · {timestamp} · {provider}{status}"

    def _update_queue_overlay(self) -> None:
        setter = getattr(self._overlay, "set_transcription_queue", None)
        if not callable(setter):
            return
        visible_jobs = [job for job in self._jobs.values() if not job.aborting]
        total = len(visible_jobs)
        items = [
            (
                job.token,
                self._queue_job_label(job, rank=index, total=total),
            )
            for index, job in enumerate(visible_jobs, start=1)
        ]
        setter(items)

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
        job.aborting = True
        job.background_delivery = delivery
        if job.insertion_deferred:
            self._remove_deferred_background_result(request_token)
            job.insertion_deferred = False
            self._finish_transcription_job(request_token)
            return
        if (
            request_token == self._active_request_token
            and job.mode == "streaming"
            and self._streaming_recording
        ):
            # This job is the pending streaming finalize; stopping it ends the
            # streaming session. Clear the session state so the next recording
            # is not blocked waiting on a finalize that now resolves
            # history-only in the background.
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
        is kept in history rather than discarded.
        """
        if request_token not in self._jobs:
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
        self._flush_deferred_background_results(ignore_active_transcription=True)
        if was_active and not self._new_recording_active():
            # The foreground transcription was canceled; reflect it in the
            # main overlay area instead of leaving a stale "Processing".
            self._overlay.set_state("Done", "Transcription canceled.")

    def clear_transcription_queue(self) -> None:
        """Cancel every queued/running transcription."""
        for token in list(self._jobs.keys()):
            self.cancel_queued_transcription(token)

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
        try:
            self._last_recording_store.mark_transcribing(
                engine=settings.engine,
                model=self._selected_model_name(settings),
                mode=settings.mode,
            )
        except Exception:
            self._logger.exception("Failed to mark last recording as transcribing")
        job.future = self._executor.submit(
            self._transcribe_worker,
            request_token,
            wav_bytes,
            settings,
            job,
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
        if request_token is not None and self._has_undelivered_older_job(
            request_token
        ):
            return self._executor
        return self._stream_finalize_executor

    def _submit_stream_finalize(self, *, source_audio_path: str = "") -> None:
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
        )
        job.runtime_transcriber = transcriber
        job.runtime_lease = runtime_lease
        # Hand the in-flight handshake to the worker so it can wait for it
        # before stopping the stream. Retire it here too: nothing that arrives
        # after this point belongs to a session that is being finalized.
        job.connect_thread = self._stream_connect_thread
        self._stream_connect_thread = None
        self._stream_connect_generation += 1
        with self._stream_preconnect_lock:
            self._stream_preconnect_chunks = None
        self._logger.info(
            "transcription_submitted token=%s mode=streaming engine=%s model=%s "
            "recording_id=%s",
            request_token,
            settings.engine,
            self._selected_model_name(settings),
            job.source_recording_id or "n/a",
        )
        try:
            self._last_recording_store.mark_transcribing(
                engine=settings.engine,
                model=self._selected_model_name(settings),
                mode=settings.mode,
            )
        except Exception:
            self._logger.exception("Failed to mark streaming recording as transcribing")
        job.future = self._stream_finalize_executor_for(
            settings,
            request_token=request_token,
        ).submit(self._finalize_stream_worker, request_token, transcriber, job)
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

    def _retry_guidance(self, *, has_retry_audio: bool | None = None) -> str:
        retry_available = (
            bool(self._last_failed_wav_bytes)
            if has_retry_audio is None
            else bool(has_retry_audio)
        )
        last_recording_available = self._selectable_last_recording_path() is not None
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
        self._overlay.set_state("Processing", "Loading selected model...")
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
        preload_future = self._preload_executor.submit(
            self._preload_model_worker,
            settings,
            generation,
            key,
        )
        with self._preload_result_lock:
            if generation == self._preload_generation:
                self._preload_future = preload_future
        if preload_future is not None and not preload_future.done():
            self._preload_progress_timer.start()
        else:
            self._preload_progress_timer.stop()

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
                "transcription waits for this model. Use Cancel to abort."
            )
        if model_download_coordinator().waiting_for_other_process():
            # Progress is directory growth, and another process owns the
            # directory -- so the bar would sit at a frozen 0% and claim to
            # be downloading. Say what is actually happening instead.
            return (
                f"Waiting for another program to finish using the model "
                f"cache before downloading '{model_name}'. You can start "
                "recording now; transcription waits for this model. Use "
                "Cancel to abort."
            )
        phase = self._current_preload_phase()
        if phase == _PRELOAD_PHASE_QUEUED:
            return (
                f"Waiting for the previous model before preparing "
                f"'{model_name}'. You can start recording now; transcription "
                "waits for this model."
            )
        if phase == _PRELOAD_PHASE_LOAD:
            # Nothing is being fetched any more. The progress bar measures
            # directory growth, so during the load it printed a frozen
            # "approx. 100%" next to the word "Downloading" for a model that
            # was already complete on disk.
            return (
                f"Loading '{model_name}' into memory. You can start recording "
                "now; transcription waits for this model."
            )
        downloaded_bytes = estimate_cached_model_bytes(
            model_name,
            getattr(self._settings, "model_dir", ""),
        )

        progress = self._preload_speed_tracker.measure(
            model_name,
            downloaded_bytes,
        )
        detail = format_model_download_progress(
            progress,
            include_progress_bar=True,
        )

        return (
            f"{detail} You can start recording now; transcription waits for "
            "this model. Use Cancel to abort download."
        )

    @QtCore.Slot()
    def _on_preload_progress_poll(self) -> None:
        preload = self._preload_future
        if preload is None or preload.done():
            self._preload_progress_timer.stop()
            return

        # Do not overwrite listening/processing states of an active session.
        if (
            self._audio_capture is not None
            or self._streaming_recording
            or self._recording_start_in_progress
        ):
            return

        try:
            detail = self._preload_progress_detail()
        except Exception:
            detail = "Loading model..."
        self._overlay.set_state("Processing", detail, compact=False)

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
                    self._set_transcriber_cancel_check(
                        runtime_lease.transcriber, None
                    )
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
            if generation != self._preload_generation:
                self._logger.info(
                    "Ignoring stale model preload completion generation=%s current=%s",
                    generation,
                    self._preload_generation,
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
                self._overlay.set_state("Done", "Model preload canceled.")
                QtCore.QTimer.singleShot(1200, self.show_idle_status)
            return

        if success:
            self._logger.info("Model preload: %s", message)
            if not session_active:
                self._overlay.set_state(
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
                if not session_active:
                    self._overlay.set_state("Done", message)
                    QtCore.QTimer.singleShot(1200, self.show_idle_status)
            else:
                if session_active:
                    self._logger.warning(
                        "Suppressing preload error overlay during active session: %s",
                        message,
                    )
                else:
                    self._overlay.set_state("Error", message)

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
            if not str(text or "").strip() and (
                job is None or job.mode != "streaming"
            ):
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
            self._logger.exception(
                "Transcription worker raised %s", type(exc).__name__
            )
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
                total_elapsed_ms = round((time.perf_counter() - worker_started_at) * 1000)
                runtime_device = str(getattr(transcriber, "runtime_device", "") or "")
                gpu_available = getattr(transcriber, "gpu_available", "")
                runtime_details = str(
                    getattr(transcriber, "runtime_details_text", "") or ""
                )
                result_chars = (
                    len(terminal_payload) if terminal_kind == "ready" else 0
                )
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
                        self._logger.exception("Failed to clear transcriber cancel hook")
                    try:
                        self._set_transcriber_progress_callback(transcriber, None)
                    except BaseException:
                        self._logger.exception("Failed to clear transcriber progress hook")
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

    def _await_stream_connect(self, job: _TranscriptionJob | None) -> None:
        """Wait for an in-flight handshake before stopping the stream.

        Runs on the finalize worker, never the Qt thread. Stopping a provider
        whose `start_stream` has not returned yet is not a no-op: the stop is
        rejected because the session is not active *yet*, and the handshake then
        completes and publishes a socket nobody owns, which blocks every later
        dictation with "Streaming session already active". The wait is bounded
        so a provider that hangs cannot hang the finalize with it.
        """
        thread = getattr(job, "connect_thread", None) if job is not None else None
        if thread is None or not thread.is_alive():
            return
        self._logger.info("Waiting for the streaming handshake before stopping it.")
        thread.join(timeout=STREAMING_CONNECT_JOIN_TIMEOUT_S)
        if thread.is_alive():
            self._logger.warning(
                "The streaming handshake did not finish within %.1fs; stopping "
                "anyway.",
                STREAMING_CONNECT_JOIN_TIMEOUT_S,
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
                self._await_stream_connect(job)
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

    def _emit_stream_runtime_failure(self, error_text: str) -> None:
        message = str(error_text or "Streaming failed.").strip()
        self.stream_runtime_failed.emit(message or "Streaming failed.")

    def _has_pending_streaming_job(self) -> bool:
        """Whether a streaming finalize is still in flight and will deliver text.

        An aborting job does not count: it is being canceled and will not
        deliver, so treating it as pending would drop the partial transcript
        this guard exists to avoid duplicating.
        """
        return any(
            job.mode == "streaming" and not job.aborting
            for job in self._jobs.values()
        )

    def _current_streaming_partial_text(self) -> str:
        """Best-known transcript of the live streaming session.

        Shared by the abort and runtime-failure paths so the two cannot drift on
        which field wins; both must keep what was already transcribed.
        """
        return normalize_stream_text(
            self._stream_text_state.live_text
            or self._stream_text_state.last_partial_text
        )

    def _stop_active_capture(self, *, persist_audio: bool) -> tuple[bytes, str]:
        """Stop the active capture and return its audio plus the retained path."""
        capture = self._audio_capture
        self._audio_capture = None
        self._cancel_audio_callback_watchdog(capture)
        if capture is None:
            return b"", ""

        wav_bytes = b""
        try:
            wav_bytes = capture.stop()
        except Exception:
            self._logger.exception("Failed to stop active audio capture")

        source_audio_path = self._save_recording_artifacts(capture, wav_bytes)
        if persist_audio and wav_bytes:
            self._persist_last_recording_audio(wav_bytes)
        return wav_bytes, source_audio_path

    def _teardown_active_stream_runtime(
        self, *, preserve_audio: bool
    ) -> tuple[bytes, str]:
        wav_bytes, source_audio_path = self._stop_active_capture(
            persist_audio=preserve_audio
        )

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
        if self._audio_capture is None:
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
            self._emit_stream_runtime_failure(f"Streaming chunk push failed: {exc}")

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
            # it did not.
            if engine in _ENGINES_USING_CUSTOM_VOCABULARY
            or engine not in _ENGINE_MODEL_FIELDS
            else ""
        )
        if engine == DEFAULT_ENGINE or engine not in _ENGINE_MODEL_FIELDS:
            # `create_transcriber` falls back to the local path for an unknown
            # engine, so an unknown one must produce the local identity too.
            #
            # "local" is four different runtimes and they read different
            # settings, so the per-engine scoping above has to continue one
            # level down: listing all ten fields made Parakeet reload its
            # 670 MB model when the user typed a custom-vocabulary term that
            # onnx-asr never receives. The branches below mirror
            # `_create_local_transcriber` exactly -- keep them in step.
            model_size = settings.model_size
            common = {
                "engine": engine,
                "model_size": model_size,
                "offline_mode": bool(getattr(settings, "offline_mode", False)),
                "model_dir": getattr(settings, "model_dir", ""),
            }
            if model_size in LOCAL_ONNX_ASR_MODEL_SIZES:
                # onnx-asr takes nothing else, and is CPU-only, so the device
                # policy never reaches it either.
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
                            getattr(settings, "local_onnx_device", "")
                        )
                    ),
                )
            if model_size in LOCAL_WEBGPU_MODEL_SIZES:
                return _TranscriberIdentity(
                    **common,
                    local_onnx_device=getattr(settings, "local_onnx_device", ""),
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
        return _TranscriberIdentity(
            engine=engine,
            custom_vocabulary=vocabulary,
            remote_model=str(
                getattr(settings, _ENGINE_MODEL_FIELDS[engine], "") or ""
            ),
            azure_endpoint=(
                getattr(settings, "azure_endpoint", "") if engine == "azure" else ""
            ),
            has_api_key=bool(getattr(settings, _ENGINE_KEY_FLAGS[engine], False)),
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
                self._close_cached_transcriber(self._transcriber_cache)
                self._transcriber_cache = create_transcriber(
                    settings, secret_store=self._secret_store
                )
                self._transcriber_cache_key = cache_key
            # Apply the language of *this* job's settings snapshot. Acquisition
            # is serialized by the runtime lock, so a reused runtime can never
            # transcribe with a stale language. Every provider implements this
            # through ITranscriber; the lookup keeps duck-typed transcribers
            # (tests, future in-process adapters) working.
            apply_language = getattr(
                self._transcriber_cache, "set_language_mode", None
            )
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
                self._drop_request_audio(request_token)
                if self._active_request_token == request_token:
                    self._active_request_token = None
                    self._last_transcribe_settings = None
                should_finish = self._handle_background_transcription_ready(job, text)
                if should_finish:
                    self._finish_transcription_job(request_token)
                return
            self._active_request_token = None
            self._drop_request_audio(request_token)
            self._last_failed_wav_bytes = b""

        self._finish_transcription_job(request_token)
        # A foreground result is about to claim the overlay. A deferred insert
        # that fails inside this flush must therefore not paint an Error state
        # that is overwritten a few statements later — it would flash and be
        # gone. Its notification still fires, so the failure is never silent.
        self._foreground_delivery_pending = True
        try:
            self._flush_deferred_background_results()
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
        # Only a real transcript replaces the last one. Assigning before the
        # empty check meant a streaming session that produced nothing wiped
        # the previous dictation from the tray's "Insert last transcript
        # again", which then reported "No transcript available" while that
        # dictation was still sitting in history. The batch silence-gate path
        # already gets this right.
        if text.strip():
            self._last_transcript = text

        if not text.strip():
            self._mark_last_recording_completed()
            self._overlay.set_state("Done", "No speech detected.")
            self._reveal_overlay_result(is_error=False)
            self._last_transcribe_settings = None
            self._reset_streaming_state()
            return

        used_settings = (
            self._last_transcribe_settings or stream_settings or self._settings
        )
        self._append_transcript_history(
            text,
            used_settings,
            session_mode,
            source_recording_id=(job.source_recording_id if job is not None else None),
            source_audio_path=(job.source_audio_path if job is not None else ""),
        )

        if session_mode == "streaming":
            final_insertion, final_text = self._stream_text_state.finalize_append_only(
                text
            )
            if final_insertion and not self._insert_text_at_target(
                final_insertion,
                restore_focus=True,
                target_handle=target_handle,
                target_signature=target_signature,
            ):
                self._reveal_overlay_result(is_error=True)
                self._mark_last_recording_completed()
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
            ):
                self._reveal_overlay_result(is_error=True)
                self._mark_last_recording_completed()
                self._last_transcribe_settings = None
                self._reset_streaming_state()
                return

            self._overlay.set_state("Done", text)
            self._play_completion_beep()

        # Bring the (possibly floating/hidden) overlay forward so the finished
        # transcript is actually visible for a quick confirmation.
        self._reveal_overlay_result(is_error=False)
        if self._settings.keep_transcript_in_clipboard:
            QtGui.QGuiApplication.clipboard().setText(text)
        self._mark_last_recording_completed()
        self._last_transcribe_settings = None
        self._reset_streaming_state()

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
            return True
        self._append_transcript_history(
            text,
            job.settings,
            job.mode,
            source_recording_id=job.source_recording_id,
            source_audio_path=job.source_audio_path,
            track_for_edit=False,
        )
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
            self._insert_background_transcription(job, text)
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

        Requires ``immediate_background_insert``. A streaming recording never
        allows it: live partial inserts already write at the caret and a
        focus change suspends them. A batch recording allows it — the
        microphone does not care about a paste, the new recording's own
        target was already snapshotted at its start, and focus is restored to
        the finished job's window like in any other delivery. The historical
        failures around inserting near a hotkey press were the held-modifier
        Ctrl+V corruption, which the inserter's modifier-release wait fixed.
        """
        if job is None:
            return False
        if not bool(getattr(self._settings, "immediate_background_insert", False)):
            return False
        return not self._streaming_recording

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
    ) -> bool:
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
        if not inserted:
            self._report_background_insertion_failure(
                job,
                text,
                job_count=job_count,
            )
        else:
            self._play_completion_beep()
        return inserted

    def _report_background_insertion_failure(
        self,
        job: _TranscriptionJob,
        text: str,
        *,
        job_count: int = 1,
    ) -> None:
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
        identity = (
            f"{job_count} queued transcriptions were"
            if job_count > 1
            else f"{self._job_identity(job)} was"
        )
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
        self.background_insertion_failed.emit(message)
        if self._overlay_session_active() or self._foreground_delivery_pending:
            # A newer session owns the overlay (or is one statement away from
            # claiming it); its own transcript must stay the one that
            # Copy/Insert act on. The notification above is the report then.
            return
        # Nothing newer is on screen, so this transcript becomes what the
        # overlay shows — and therefore what Copy and Insert act on.
        transcript = text.strip()
        self._last_transcript = transcript
        detail = f"{message}\n\n{transcript}" if transcript else message
        self._overlay.set_state(
            "Error",
            detail,
            copy_text=text,
            error_action=(
                OVERLAY_ERROR_ACTION_NONE
                if may_have_pasted
                else OVERLAY_ERROR_ACTION_INSERT
            ),
        )
        self._reveal_overlay_result(is_error=True)

    def _flush_deferred_background_results(
        self,
        *,
        ignore_active_transcription: bool = False,
    ) -> None:
        if not self._deferred_background_results:
            return
        # Deferral is per job: with an active capture, only results targeting
        # the current foreground window may insert (immediate mode); the rest
        # stay queued for the next flush.
        pending = []
        still_deferred = []
        for job, text in sorted(
            self._deferred_background_results, key=lambda item: item[0].token
        ):
            if self._should_defer_background_insertion(
                ignore_active_transcription=ignore_active_transcription,
                job=job,
            ):
                still_deferred.append((job, text))
            else:
                pending.append((job, text))
        self._deferred_background_results = still_deferred
        if not pending:
            return
        # Coalesce results that target the same window into one paste: each
        # separate paste is its own clipboard set/paste/restore cycle and thus
        # its own race window against the target app, so six queued results
        # used to mean six chances to lose one.
        for jobs, text in self._coalesced_deferred_inserts(
            pending,
            # With current-window insertion every result goes to the same
            # (current) target anyway, so one paste covers them all.
            single_group=self._insert_target_is_current_window(),
        ):
            for job in jobs:
                job.insertion_deferred = False
            if len(jobs) > 1:
                self._logger.info(
                    "Coalescing %d deferred transcription inserts into one "
                    "paste. tokens=%s",
                    len(jobs),
                    [job.token for job in jobs],
                )
            try:
                self._insert_background_transcription(
                    jobs[0],
                    text,
                    job_count=len(jobs),
                )
            except Exception:
                self._logger.exception(
                    "Failed to insert deferred background transcription; "
                    "saved to history only. tokens=%s",
                    [job.token for job in jobs],
                )
                self._report_background_insertion_failure(
                    jobs[0],
                    text,
                    job_count=len(jobs),
                )
            for job in jobs:
                self._finish_transcription_job(job.token)

    @staticmethod
    def _coalesced_deferred_inserts(
        pending: list[tuple[_TranscriptionJob, str]],
        *,
        single_group: bool = False,
    ) -> list[tuple[list[_TranscriptionJob], str]]:
        """Group token-ordered deferred results by their insertion target."""
        groups: list[tuple[list[_TranscriptionJob], list[str]]] = []
        index_by_target: dict[tuple, int] = {}
        for job, text in pending:
            key = (
                (None, None)
                if single_group
                else (job.target_handle, job.target_signature)
            )
            index = index_by_target.get(key)
            if index is None:
                index_by_target[key] = len(groups)
                groups.append(([job], [text]))
            else:
                groups[index][0].append(job)
                groups[index][1].append(text)
        return [(jobs, _join_transcripts(texts)) for jobs, texts in groups]

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
            self._overlay.set_state("Error", message)
            self._reveal_overlay_result(is_error=True)

    def _on_transcription_failed(
        self,
        error_text: str,
        *,
        request_token: int | None = None,
    ) -> None:
        preserved_audio = bool(self._last_failed_wav_bytes)
        if request_token is not None:
            job = self._jobs.get(request_token)
            if not self._is_foreground_transcription(request_token, job):
                # A queued transcription failed while a newer session is
                # active. Keep the live session's overlay state untouched and
                # keep the audio for a manual retry, but never let the failure
                # pass silently: an unreported failure looks exactly like a
                # recording that was simply never transcribed.
                retry_available = self._promote_request_audio_for_retry(
                    request_token
                )
                self._report_background_failure(job, error_text, retry_available)
                self._finish_transcription_job(request_token)
                self._flush_deferred_background_results()
                return
            self._active_request_token = None
            preserved_audio = self._promote_request_audio_for_retry(request_token)
            if not preserved_audio:
                self._last_failed_wav_bytes = b""

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
        if runtime_stream_failed:
            # Only the *history write* is conditional. The teardown must always
            # run: gating it too abandoned a live capture, its transcriber and
            # its runtime lease, so the microphone kept recording after the
            # overlay already said Error.
            if not self._has_pending_streaming_job():
                # A finalize already in flight will deliver this session's text
                # itself; saving the partial too would write two history
                # entries for one dictation. Providers do reach that state:
                # AssemblyAI and Deepgram both record a socket error and still
                # return the accumulated text from stop_stream().
                partial_transcript = self._current_streaming_partial_text()
            wav_bytes, partial_source_audio_path = (
                self._teardown_active_stream_runtime(preserve_audio=True)
            )
            if wav_bytes:
                self._last_failed_wav_bytes = bytes(wav_bytes)
                preserved_audio = True
        self._streaming_recording = False
        self._active_stream_transcriber = None
        self._active_stream_settings = None
        self._last_transcribe_settings = None
        self._reset_streaming_state()
        kept_detail = ""
        if partial_transcript.strip():
            self._append_transcript_history(
                partial_transcript,
                partial_settings,
                "streaming",
                source_audio_path=partial_source_audio_path,
            )
            self._last_transcript = partial_transcript
            kept_detail = " The text transcribed so far was saved to history."
        # The failed session no longer blocks queued inserts; flush after the
        # stream/capture teardown above so a deferred result is not left
        # pending behind a capture that was just removed.
        self._flush_deferred_background_results()
        try:
            self._last_recording_store.mark_failed(error_text)
        except Exception:
            self._logger.exception("Failed to persist last recording failure state")
        self._overlay.set_state(
            "Error",
            f"{error_text} {self._retry_guidance(has_retry_audio=preserved_audio)}"
            f"{kept_detail}",
            copy_text=partial_transcript or None,
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
        if STREAMING_LIVE_INSERT_ENABLED and not self._stream_insertion_suspended:
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
                        self._request_stream_abort(
                            "Streaming aborted: the target window kept "
                            "rejecting inserted text.",
                            beep=STREAMING_BEEP_ON_ABORT,
                        )
                        return
                    self._logger.debug(
                        "Live insert failed (%d/%d); retrying on the next "
                        "partial.",
                        self._stream_insert_failures,
                        STREAMING_LIVE_INSERT_RETRY_LIMIT,
                    )
                    return
        else:
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

    @QtCore.Slot(str)
    def _on_stream_runtime_failed(self, error_text: str) -> None:
        if self._shutdown_started:
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
                wav_bytes = capture.stop()
            except Exception:
                self._logger.exception("Failed to stop audio capture during abort")
        source_audio_path = ""
        if capture is not None:
            source_audio_path = self._save_recording_artifacts(capture, wav_bytes)
        if preserve_audio and wav_bytes:
            self._persist_last_recording_audio(wav_bytes)
            try:
                self._last_recording_store.mark_canceled(reason)
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
                source_audio_path=source_audio_path,
            )
            self._last_transcript = partial_transcript
            self._overlay.set_state(
                "Error",
                f"{reason} Partial transcript (saved to history): {partial_transcript}",
            )
        else:
            self._overlay.set_state("Error", reason)
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
    ) -> bool:
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
            if may_have_pasted:
                # The text is probably already in the document. Offering the
                # usual Insert action would paste it a second time, and
                # copying it over the clipboard is pointless when the paste
                # itself succeeded -- only the cleanup failed. Report it and
                # leave the transcript alone.
                self._logger.warning(
                    "Insertion reported a post-paste failure: %s", exc
                )
                if show_overlay_error:
                    self._overlay.set_state(
                        "Error",
                        f"{exc} The text was most likely inserted; check the "
                        "target window before inserting it again.",
                        copy_text=insertion_text,
                        # Explicitly no action. Omitting this leaves Retry,
                        # which re-transcribes the last *failed* recording --
                        # cleared only on the foreground ready path, so from
                        # a re-paste it can be an entirely different one and
                        # lands on top of the text just inserted.
                        error_action=OVERLAY_ERROR_ACTION_NONE,
                    )
                return False
            allow_clipboard_fallback = bool(
                getattr(exc, "allow_clipboard_fallback", True)
            )
            if copy_on_error and allow_clipboard_fallback:
                QtGui.QGuiApplication.clipboard().setText(insertion_text)
            if show_overlay_error:
                detail = str(exc)
                if copy_on_error and allow_clipboard_fallback:
                    detail = f"{detail} Transcript copied to clipboard."
                elif copy_on_error:
                    detail = (
                        f"{detail} Transcript saved to history; current "
                        "clipboard left untouched."
                    )
                # Show what was transcribed: the text is otherwise invisible
                # until it is inserted again, which is exactly when the user
                # needs to see and be able to copy it.
                preview = insertion_text.strip()
                if preview:
                    detail = f"{detail}\n\n{preview}"
                # The transcription itself succeeded, so Retry (which
                # re-transcribes) has nothing to work with; offer inserting the
                # transcript again instead.
                self._overlay.set_state(
                    "Error",
                    detail,
                    copy_text=insertion_text,
                    error_action=OVERLAY_ERROR_ACTION_INSERT,
                )
            self._logger.exception("Text insertion failed")
            return False
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
        """Insert the last transcript again into the currently focused window.

        Tray action and optional global hotkey. Uses the normal insertion
        path (paste-mode and clipboard semantics from settings, modifier
        release wait in the inserter), but targets the current focus instead
        of a recording snapshot. Blocked while a recording is active so the
        paste cannot interfere with a capture or live streaming inserts.
        """
        text = self._last_transcript
        if not text.strip():
            self.show_overlay_error("No transcript available to insert yet.")
            return
        if (
            self._recording_start_in_progress
            or self._recording_stop_in_progress
            or self._audio_capture is not None
            or self._streaming_recording
        ):
            self.show_overlay_error(
                "Finish the current recording before inserting the last "
                "transcript again."
            )
            return
        if self._insert_text_at_target(
            text,
            restore_focus=False,
            target_handle=None,
            target_signature=None,
        ):
            self._overlay.set_state("Done", text)
            self._reveal_overlay_result(is_error=False)
            self._play_completion_beep()
        else:
            self._reveal_overlay_result(is_error=True)

    def show_overlay_notice(self, message: str) -> None:
        """Confirm a completed action on the overlay and return to Idle.

        Tray actions have no other feedback surface, so a copy that silently
        succeeds is indistinguishable from one that did nothing.
        """
        if self._overlay_session_active():
            return
        self._overlay.set_state("Done", str(message))
        self._reveal_overlay_result(is_error=False)
        QtCore.QTimer.singleShot(OVERLAY_NOTICE_MS, self.show_idle_status)

    def show_overlay_error(self, message: str) -> None:
        """Surface a transient error on the overlay without exposing the
        overlay widget to callers (kept so main.py does not reach into
        ``_overlay`` directly)."""
        self._overlay.set_state("Error", str(message))
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
            self._overlay.set_state("Error", "No transcript available to edit.")
            return False

        from .transcript_edit_dialog import TranscriptEditDialog

        next_text = TranscriptEditDialog.get_text(parent, current_text)
        if next_text is None or next_text == current_text:
            return False

        entry = self._last_history_entry
        if entry is None:
            self._overlay.set_state(
                "Error",
                "No saved history entry is available for this transcript.",
            )
            return False

        updated = self._history_store.update_entry_text(entry, next_text)
        if updated <= 0:
            self._overlay.set_state(
                "Error",
                "The saved history entry could not be updated.",
            )
            return False

        self._last_history_entry = replace(entry, text=next_text.strip())
        self._last_transcript = next_text.strip()
        self._overlay.set_state("Done", self._last_transcript, compact=False)
        if self._settings.keep_transcript_in_clipboard:
            QtGui.QGuiApplication.clipboard().setText(self._last_transcript)
        return True

    def retry_last_transcription(self) -> bool:
        if not self._last_failed_wav_bytes:
            self._overlay.set_state("Error", "No failed transcription to retry.")
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
        self._submit_batch_transcription(self._last_failed_wav_bytes, settings)
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
            runtime_lease = self._acquire_transcriber_runtime(
                settings,
                allow_isolated=False,
            )
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
            capture = self._audio_capture
            self._audio_capture = None
            self._cancel_audio_callback_watchdog(capture)
            wav_bytes = b""
            try:
                wav_bytes = capture.stop()
            except Exception:
                pass
            self._persist_last_recording_audio(wav_bytes)
            self._save_recording_artifacts(capture, wav_bytes)
            self._logger.info(
                "recording_canceled_before_transcription audio_bytes=%d",
                len(wav_bytes),
            )
            if wav_bytes:
                try:
                    self._last_recording_store.mark_canceled(
                        "Recording canceled before transcription."
                    )
                except Exception:
                    self._logger.exception("Failed to mark canceled recording")
            self._active_batch_settings = None
            self._overlay.set_state(
                "Done",
                f"Recording canceled. {self._retry_guidance(has_retry_audio=False)}",
            )
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
            try:
                self._last_recording_store.mark_canceled(
                    "Transcription canceled by user."
                )
            except Exception:
                self._logger.exception("Failed to mark canceled transcription")
            # Clearing the active transcription may unblock deferred background
            # inserts that were waiting behind it; deliver every completed one now.
            self._flush_deferred_background_results(ignore_active_transcription=True)
            self._overlay.set_state("Done", "Transcription canceled.")
            return

        # Preloading can intentionally overlap a recording or a queued batch
        # transcription. It is therefore lower priority than the user's active
        # session and is canceled only when there is no recording/job to stop.
        if self._cancel_model_preload_if_running():
            return

        # Nothing active to cancel, but the hotkey should still deliver any
        # completed results that are stuck pending insertion.
        self._flush_deferred_background_results(ignore_active_transcription=True)
        self._overlay.set_state("Done", "Nothing to cancel.")

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
        except Exception:
            self._logger.exception("Failed to persist overlay opacity")

    def set_overlay_always_on_top(self, enabled: bool) -> None:
        normalized = bool(enabled)
        if bool(getattr(self._settings, "overlay_always_on_top", True)) == normalized:
            return
        self._settings = replace(self._settings, overlay_always_on_top=normalized)
        try:
            self._settings_store.save(self._settings)
        except Exception:
            self._logger.exception("Failed to persist overlay always-on-top mode")

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
        except Exception:
            self._logger.exception("Failed to persist transcription language")
        self._sync_overlay_language_options()
        # No runtime teardown and no preload: the language is a per-request
        # parameter for every engine and is applied when the next job acquires
        # the runtime. Reloading here made a mistyped language selection block
        # the correction behind a full model load, and switching language for a
        # single recording evicted the model that the next dictation needs.

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
        self._overlay.set_state("Processing", "Canceling model download...")
        return True

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
                        if not coordinator.has_explicit_interest(
                            model_name, model_dir
                        ):
                            from .transcriber.local_faster_whisper import (
                                cleanup_incomplete_model_download,
                            )

                            cleanup_incomplete_model_download(model_name, model_dir)
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
            self._logger.warning(
                "Preferred hotkey %s unavailable: %s", preferred, exc
            )

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
                self._logger.exception(
                    "Failed to unregister disabled re-paste hotkey"
                )
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

    def refresh_hotkey_registration(self) -> None:
        """Re-register global hotkeys after Windows resumes or opens Explorer."""
        self._hotkey_registration_ok = self._register_hotkey_with_fallback()
        self._cancel_hotkey_registration_ok = self._register_cancel_hotkey()
        self._show_overlay_hotkey_registration_ok = (
            self._register_show_overlay_hotkey()
        )
        self._repaste_hotkey_registration_ok = self._register_repaste_hotkey()
        if not (
            self._hotkey_registration_ok
            and self._cancel_hotkey_registration_ok
            and self._show_overlay_hotkey_registration_ok
            and self._repaste_hotkey_registration_ok
        ):
            self._logger.warning("Global hotkey refresh did not fully succeed.")
