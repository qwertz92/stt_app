from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtGui

from .app_paths import recordings_dir
from .audio_capture import AudioCapture, AudioCaptureError, WarmMicrophoneStream
from .config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    CONCURRENT_TRANSCRIPTION_MODE_CANCEL,
    CONCURRENT_TRANSCRIPTION_MODE_HISTORY,
    CONCURRENT_TRANSCRIPTION_MODE_INSERT,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_CONCURRENT_TRANSCRIPTION_MODE,
    DEFAULT_ENGINE,
    DEFAULT_INSERT_TARGET,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    INSERT_TARGET_CURRENT_WINDOW,
    DEFAULT_START_BEEP_TONE,
    DOC_MODELS_PATH,
    FALLBACK_HOTKEY,
    FASTER_WHISPER_MODEL_SIZES,
    LOCAL_WEBGPU_MODEL_SIZES,
    MODEL_ESTIMATED_SIZE_MB,
    STREAMING_ABORT_ON_FOCUS_CHANGE,
    STREAMING_ABORT_BEEP_DURATION_MS,
    STREAMING_ABORT_BEEP_HZ,
    STREAMING_BEEP_ON_ABORT,
    STREAMING_FOCUS_POLL_MS,
    STREAMING_LIVE_INSERT_ENABLED,
    STREAMING_OVERLAY_MAX_CHARS,
    STREAMING_REVISION_WORD_WINDOW,
    STREAMING_STABLE_WORD_GUARD,
    OVERLAY_OPACITY_MAX_PERCENT,
    OVERLAY_OPACITY_MIN_PERCENT,
    OVERLAY_RESULT_REVEAL_MS,
    OVERLAY_ERROR_REVEAL_MS,
    VALID_MODEL_SIZES,
    VAD_ENERGY_THRESHOLD_MIN,
    VALID_START_BEEP_TONES,
    VAD_MAX_SILENCE_MS,
    VAD_MIN_SPEECH_MS,
    language_modes_for_selection,
    supports_streaming,
)
from .hotkey import HotkeyManager, HotkeyRegistrationError
from .last_recording_store import LastRecordingStore
from .local_model_download import (
    model_download_process_error,
    start_model_download_process,
    terminate_model_download_process,
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
from .text_inserter import TextInserter, TextInsertionError
from .transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore
from .transcriber import create_transcriber
from .transcriber.base import TranscriptionCanceled, TranscriptionError
from .vad import EnergyVad, peak_windowed_rms_from_wav
from .window_focus import FocusSignature, Win32WindowFocusHelper, WindowFocusHelper

_ARCHIVED_RECORDING_NAME_RE = re.compile(
    r"^recording_[0-9]{8}_[0-9]{6}_[0-9]{6}\.wav$",
    re.IGNORECASE,
)
_NO_TRANSCRIPT_SPACE_BEFORE = frozenset(".,;:!?)]}")


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
    future: object | None = None
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
        controller: "DictationController",
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
        if self._close_on_release:
            self._controller._close_cached_transcriber(self.transcriber)
        self._controller._release_transcriber_runtime(
            owns_shared_lock=self._owns_shared_lock
        )


class DictationController(QtCore.QObject):
    vad_auto_stop_requested = QtCore.Signal()
    transcription_ready = QtCore.Signal(int, str)
    transcription_failed = QtCore.Signal(int, str)
    transcription_canceled = QtCore.Signal(int)
    transcription_progress = QtCore.Signal(int, str)
    transcription_partial = QtCore.Signal(str)
    stream_runtime_failed = QtCore.Signal(str)
    stream_abort_requested = QtCore.Signal(str, bool)
    model_preload_done = QtCore.Signal(bool, str)  # (success, message)

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
    ) -> None:
        super().__init__()
        self._settings_store = settings_store
        self._hotkey_manager = hotkey_manager
        self._cancel_hotkey_manager = cancel_hotkey_manager
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
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._preload_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._preload_future: concurrent.futures.Future | None = None
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
        self._cancel_hotkey_registration_ok = False
        self._cancel_hotkey_notice: str | None = None
        self._target_window_handle: int | None = None
        self._target_focus_signature: FocusSignature | None = None
        self._last_transcript: str = ""
        self._last_history_entry: TranscriptHistoryEntry | None = None
        self._last_insert_target_key: tuple[object, object] | None = None
        self._last_insert_ended_with_whitespace = True
        self._last_failed_wav_bytes: bytes = b""
        self._last_transcribe_settings: AppSettings | None = None
        self._active_batch_settings: AppSettings | None = None
        self._streaming_recording = False
        self._active_stream_transcriber = None
        self._active_stream_runtime_lease: _TranscriberRuntimeLease | None = None
        self._active_stream_settings: AppSettings | None = None
        self._stream_chunk_error_reported = False
        self._stream_abort_requested = False
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
        self._preload_progress_timer = QtCore.QTimer(self)
        self._preload_progress_timer.setInterval(600)
        self._preload_progress_timer.timeout.connect(self._on_preload_progress_poll)
        self._preload_target_model: str | None = None
        self._preload_speed_tracker = ModelDownloadSpeedTracker()
        self._preload_cancel_requested = False
        # Fallback model resolved by the preload worker (background thread).
        # Applied to ``self._settings`` from the Qt thread in
        # ``_on_model_preload_done`` so settings mutation and JSON persistence
        # never race with the main thread.
        self._pending_preload_fallback: str | None = None
        self._preload_download_process: subprocess.Popen | None = None
        self._preload_download_lock = threading.Lock()
        self._request_token_counter = 0
        self._active_request_token: int | None = None
        self._request_audio_by_token: dict[int, tuple[bytes, AppSettings]] = {}
        # In-flight transcription jobs (pending + running), insertion-ordered,
        # used for the overlay queue display, per-job target insertion, and
        # cooperative cancellation. A token is "live" while its job is present.
        self._jobs: dict[int, _TranscriptionJob] = {}
        self._deferred_background_results: list[tuple[_TranscriptionJob, str]] = []

        self.vad_auto_stop_requested.connect(self.stop_recording)
        self.transcription_ready.connect(self._on_transcription_ready_result)
        self.transcription_failed.connect(self._on_transcription_failed_result)
        self.transcription_canceled.connect(self._on_transcription_canceled_result)
        self.transcription_progress.connect(self._on_transcription_progress_result)
        self.transcription_partial.connect(self._on_transcription_partial)
        self.stream_runtime_failed.connect(self._on_stream_runtime_failed)
        self.stream_abort_requested.connect(self._on_stream_abort_requested)
        self.model_preload_done.connect(self._on_model_preload_done)

    @property
    def settings(self) -> AppSettings:
        return self._settings

    def initialize(self) -> None:
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
        self._focus_poll_timer.stop()
        self._preload_progress_timer.stop()
        self._preload_cancel_requested = True
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
        self._reset_streaming_state()
        self._reset_transcriber_cache()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._preload_executor.shutdown(wait=False, cancel_futures=True)

    def reload_settings(self, re_register_hotkey: bool = True) -> None:
        self._settings = self._settings_store.load()
        setter = getattr(self._secret_store, "set_insecure_fallback_enabled", None)
        if callable(setter):
            try:
                setter(bool(getattr(self._settings, "allow_insecure_key_storage", False)))
            except Exception:
                self._logger.exception("Failed to apply insecure key fallback setting")
        self._overlay.set_opacity_percent(self._settings.overlay_opacity_percent)
        self._overlay.set_always_on_top(
            bool(getattr(self._settings, "overlay_always_on_top", True))
        )
        self._sync_overlay_language_options()
        self._sync_warm_microphone_stream()
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
        if re_register_hotkey:
            self._hotkey_registration_ok = self._register_hotkey_with_fallback()
            self._cancel_hotkey_registration_ok = self._register_cancel_hotkey()
        else:
            self._hotkey_registration_ok = True
            self._hotkey_notice = None
            self._cancel_hotkey_registration_ok = True
            self._cancel_hotkey_notice = None

    def on_settings_changed(self) -> None:
        """Reload settings after user applies changes in the settings dialog.

        Re-registers the hotkey.  When the engine is local, triggers a
        background model preload so the first transcription is instant.
        """
        self.reload_settings(re_register_hotkey=True)
        if self._settings.engine == DEFAULT_ENGINE:
            self._start_local_model_preload()
        else:
            preload = self._preload_future
            self._preload_future = None
            self._preload_progress_timer.stop()
            self._preload_target_model = None
            self._preload_cancel_requested = False
            self._terminate_preload_download_process()
            if preload is not None and not preload.done():
                try:
                    preload.cancel()
                except Exception:
                    pass
            self.show_idle_status()

    def show_idle_status(self) -> None:
        if not self._hotkey_registration_ok:
            self._overlay.set_state(
                "Error",
                self._hotkey_notice or "Hotkey registration failed.",
            )
            return
        if not self._cancel_hotkey_registration_ok:
            self._overlay.set_state(
                "Error",
                self._cancel_hotkey_notice
                or "Cancel hotkey registration failed.",
            )
            return

        detail = f"Hotkey: {self._settings.hotkey}"
        if self._hotkey_notice:
            detail = f"{detail} ({self._hotkey_notice})"
        if self._settings.cancel_hotkey:
            detail = f"{detail} | Cancel: {self._settings.cancel_hotkey}"
            if self._cancel_hotkey_notice:
                detail = f"{detail} ({self._cancel_hotkey_notice})"
        self._overlay.set_state("Idle", detail)

    @QtCore.Slot()
    def toggle_recording(self) -> None:
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
            self._logger.info("Ignored start_recording while a capture is already active.")
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
            self._overlay.set_state(
                "Listening",
                "Starting recording...",
                compact=True,
            )
            self._overlay.ensure_compact_size()
            QtCore.QCoreApplication.processEvents(
                QtCore.QEventLoop.ExcludeUserInputEvents,
                25,
            )
            preload = self._preload_future
            preload_running = preload is not None and not preload.done()

            batch_settings: AppSettings | None = None
            fallback_notice = ""
            if preload_running and self._settings.engine == DEFAULT_ENGINE:
                if self._settings.mode == "streaming":
                    self._overlay.set_state(
                        "Error",
                        "Model is still loading. Streaming starts after the selected "
                        "model is ready.",
                    )
                    return

                fallback_settings = self._resolve_preload_fallback_settings()
                if fallback_settings is None:
                    try:
                        detail = self._preload_progress_detail(
                            include_fallback_hint=False
                        )
                    except Exception:
                        detail = "Selected model is still loading."
                    self._overlay.set_state(
                        "Error",
                        f"{detail} No cached fallback model available yet.",
                    )
                    return

                batch_settings = fallback_settings
                fallback_notice = (
                    f"Using fallback '{fallback_settings.model_size}' "
                    f"while '{self._settings.model_size}' loads."
                )
            # Check if the selected engine supports streaming mode.
            if self._settings.mode == "streaming" and not supports_streaming(
                self._settings.engine,
                self._settings.model_size,
            ):
                if (
                    self._settings.engine == DEFAULT_ENGINE
                    and self._settings.model_size in LOCAL_WEBGPU_MODEL_SIZES
                ):
                    detail = (
                        "Streaming is not available for the selected ONNX/WebGPU "
                        "local model. Switch to batch mode, or choose a "
                        "faster-whisper local model for streaming."
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
                batch_settings or replace(self._settings),
                fallback_notice=fallback_notice,
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

    def _start_batch_recording(
        self,
        settings_snapshot: AppSettings,
        *,
        fallback_notice: str = "",
    ) -> None:
        capture = self._build_audio_capture()
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
            return
        self._log_recording_start_timing(
            "batch", beep_ms, capture_started_at, capture
        )

        self._audio_capture = capture
        self._overlay.set_state(
            "Listening",
            " ".join(
                part
                for part in (
                    fallback_notice.strip(),
                    "Speak now. Press hotkey again to stop.",
                )
                if part
            ),
            compact=True,
        )

    def _start_streaming_recording(self) -> None:
        settings_snapshot = replace(self._settings)
        runtime_lease: _TranscriberRuntimeLease | None = None
        try:
            runtime_lease = self._acquire_transcriber_runtime(settings_snapshot)
            transcriber = runtime_lease.transcriber
            transcriber.start_stream(
                on_partial=self._emit_stream_partial,
                on_error=self._emit_stream_runtime_failure,
            )
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

        capture = self._build_audio_capture(chunk_callback=self._on_stream_audio_chunk)

        # Play beep BEFORE starting capture so the microphone does not
        # pick up the beep sound (winsound.Beep is synchronous/blocking).
        beep_started_at = time.perf_counter()
        self._play_start_beep()
        beep_ms = round((time.perf_counter() - beep_started_at) * 1000)

        capture_started_at = time.perf_counter()
        try:
            capture.start()
        except AudioCaptureError as exc:
            try:
                if hasattr(transcriber, "abort_stream"):
                    transcriber.abort_stream()
                else:
                    transcriber.stop_stream()
            except Exception:
                pass
            if runtime_lease is not None:
                runtime_lease.release()
            self._overlay.set_state("Error", str(exc))
            self._logger.exception("Audio capture failed to start")
            return

        self._log_recording_start_timing(
            "streaming", beep_ms, capture_started_at, capture
        )
        self._streaming_recording = True
        self._active_batch_settings = None
        self._stream_chunk_error_reported = False
        self._stream_abort_requested = False
        self._stream_text_state.reset()
        self._active_session_mode = "streaming"
        self._active_stream_transcriber = transcriber
        self._active_stream_runtime_lease = runtime_lease
        self._active_stream_settings = settings_snapshot
        self._audio_capture = capture
        if STREAMING_ABORT_ON_FOCUS_CHANGE:
            self._focus_poll_timer.start()
        self._overlay.set_state(
            "Listening",
            "Streaming active. Speak now, press hotkey to finalize.",
            compact=True,
        )

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
        warm = bool(getattr(capture, "_warm_attached", False))
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
        return AudioCapture(
            sample_rate=AUDIO_SAMPLE_RATE,
            channels=AUDIO_CHANNELS,
            vad=vad,
            auto_stop_callback=self._auto_stop_from_vad,
            chunk_callback=chunk_callback,
            logger=self._logger,
            warm_stream=self._warm_mic_stream,
        )

    def _sync_warm_microphone_stream(self) -> None:
        """Create or tear down the shared warm stream to match settings."""
        enabled = bool(getattr(self._settings, "keep_microphone_warm", False))
        if enabled and self._warm_mic_stream is None:
            self._warm_mic_stream = WarmMicrophoneStream(logger=self._logger)
            self._start_warm_microphone_stream_async()
        elif not enabled and self._warm_mic_stream is not None:
            stream = self._warm_mic_stream
            self._warm_mic_stream = None
            stream.close()

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
        if self._audio_capture is not None:
            # A recording is running on its own cold stream or the warm one;
            # do not yank the device from under it.
            return

        def _restart() -> None:
            stream.close()
            stream.ensure_started()

        threading.Thread(
            target=_restart,
            name="stt_app_warm_mic_resume",
            daemon=True,
        ).start()

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
            self._audio_capture = None
            wav_bytes = capture.stop()
            self._persist_last_recording_audio(wav_bytes)
            self._save_recording_artifacts(capture, wav_bytes)

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
                self._submit_stream_finalize()
                return

            if not wav_bytes:
                self._overlay.set_state("Error", "No audio captured.")
                self._active_batch_settings = None
                return

            if self._silence_gate_blocks(wav_bytes):
                self._active_batch_settings = None
                return

            settings_snapshot = self._active_batch_settings or replace(self._settings)
            self._active_batch_settings = None
            self._overlay.set_state("Processing", "Transcribing audio...")
            self._submit_batch_transcription(wav_bytes, settings_snapshot)
        finally:
            pending_toggles = self._pending_toggle_after_stop_count
            self._pending_toggle_after_stop_count = 0
            self._recording_stop_in_progress = False
            self._flush_deferred_background_results()
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
            peak_level = peak_windowed_rms_from_wav(wav_bytes)
        except Exception:
            self._logger.exception("Failed to measure recording peak level")
            return False
        threshold = float(
            getattr(
                self._settings,
                "silence_gate_threshold",
                DEFAULT_SILENCE_GATE_THRESHOLD,
            )
        )
        self._logger.info(
            "recording_peak_level level=%.4f silence_gate_enabled=%s "
            "threshold=%.4f",
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
                f"No speech detected (peak level {peak_level:.4f} below the "
                f"silence gate threshold {threshold:.4f}). The recording is "
                "kept as the last recording."
            ),
        )
        return True

    def _auto_stop_from_vad(self) -> None:
        self.vad_auto_stop_requested.emit()

    def _play_start_beep(self) -> None:
        if not self._settings.start_beep_enabled:
            return
        tone = (self._settings.start_beep_tone or DEFAULT_START_BEEP_TONE).strip().lower()
        if tone not in VALID_START_BEEP_TONES:
            tone = DEFAULT_START_BEEP_TONE
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
        configured = str(self._settings.recordings_dir or "").strip()
        if configured:
            return configured
        return str(recordings_dir())

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

    def _save_recording_artifacts(self, capture: AudioCapture, wav_bytes: bytes) -> None:
        if not wav_bytes:
            return

        if not self._settings.save_all_recordings:
            return

        try:
            root = self._resolve_recordings_dir()
            target_dir = os.path.abspath(root)
            os.makedirs(target_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = os.path.join(target_dir, f"recording_{stamp}.wav")
            capture.save_wav(Path(path), wav_bytes)
            self._prune_recordings(target_dir, self._settings.recordings_max_count)
        except Exception:
            self._logger.exception("Failed to archive recording")

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
        self._stream_abort_requested = False
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
            self._close_cached_transcriber(cached)
            self._transcriber_cache = None
            self._transcriber_cache_key = None
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
            self._increment_transcriber_runtime_count()
            try:
                with self._transcriber_runtime_state_lock:
                    reset_pending = self._pending_transcriber_cache_reset
                if reset_pending:
                    self._reset_transcriber_cache_locked()
                transcriber = self._get_or_create_transcriber(settings)
            except Exception:
                self._decrement_transcriber_runtime_count()
                self._transcriber_runtime_lock.release()
                raise
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

        if self._shutdown_started:
            raise TranscriptionCanceled("Application shutdown is in progress.")
        self._increment_transcriber_runtime_count()
        try:
            transcriber = create_transcriber(settings, secret_store=self._secret_store)
            if self._shutdown_started:
                self._close_cached_transcriber(transcriber)
                raise TranscriptionCanceled("Application shutdown is in progress.")
        except Exception:
            self._decrement_transcriber_runtime_count()
            raise
        return _TranscriberRuntimeLease(
            self,
            transcriber,
            owns_shared_lock=False,
            close_on_release=True,
        )

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
            if owns_shared_lock:
                self._transcriber_runtime_lock.release()
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
                cache_model = ""
                if isinstance(cache_key, tuple) and len(cache_key) > 1:
                    cache_model = str(cache_key[1] or "")
                should_reset = (
                    cached is not None
                    and (
                        cached_model in LOCAL_WEBGPU_MODEL_SIZES
                        or cache_model in LOCAL_WEBGPU_MODEL_SIZES
                    )
                )
                if not should_reset:
                    return
                self._logger.info(
                    "System resume detected; closing cached ONNX/WebGPU runtime "
                    "model=%s device=%s so GPU backends are recreated.",
                    cached_model or cache_model,
                    cached_device or "unknown",
                )
                self._close_cached_transcriber(cached)
                self._transcriber_cache = None
                self._transcriber_cache_key = None
        finally:
            self._transcriber_runtime_lock.release()

    def handle_system_resume(self) -> None:
        """Refresh Windows integrations and drop GPU runtimes after resume."""
        self.refresh_hotkey_registration()
        self._reset_resume_sensitive_transcriber_cache()
        # Audio devices commonly change identity across suspend; reopen the
        # warm stream so the next recording does not attach to a dead one.
        self._restart_warm_microphone_stream_after_resume()

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
            getattr(state, "recording_id", "")
            or getattr(state, "created_at", "")
        ).strip()

    def _append_transcript_history(
        self,
        text: str,
        settings: AppSettings,
        mode: str,
        *,
        source_recording_id: str | None = None,
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
        return (
            self._audio_capture is not None
            or self._recording_start_in_progress
        )

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
            self._jobs[token].background_delivery = (
                CONCURRENT_TRANSCRIPTION_MODE_HISTORY
            )
        elif mode == CONCURRENT_TRANSCRIPTION_MODE_CANCEL:
            self._request_job_stop(
                token,
                delivery=CONCURRENT_TRANSCRIPTION_MODE_HISTORY,
            )

    def _submit_batch_transcription(
        self,
        wav_bytes: bytes,
        settings: AppSettings,
    ) -> None:
        request_token = self._next_request_token()
        self._active_request_token = request_token
        self._last_transcribe_settings = replace(settings)
        self._store_request_audio(request_token, wav_bytes, settings)
        job = self._register_transcription_job(request_token, settings, "batch")
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

    def _submit_stream_finalize(self) -> None:
        request_token = self._next_request_token()
        self._active_request_token = request_token
        settings = self._active_stream_settings or replace(self._settings)
        self._last_transcribe_settings = replace(settings)
        transcriber = self._active_stream_transcriber
        self._active_stream_transcriber = None
        runtime_lease = self._active_stream_runtime_lease
        self._active_stream_runtime_lease = None
        job = self._register_transcription_job(request_token, settings, "streaming")
        job.runtime_transcriber = transcriber
        job.runtime_lease = runtime_lease
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
        job.future = self._executor.submit(
            self._finalize_stream_worker, request_token, transcriber, job
        )
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

    def _start_local_model_preload(self) -> None:
        if (
            self._settings.model_size in LOCAL_WEBGPU_MODEL_SIZES
            and not bool(getattr(self._settings, "keep_onnx_model_loaded", False))
        ):
            self._preload_progress_timer.stop()
            self._preload_target_model = None
            self._preload_future = None
            self._preload_cancel_requested = False
            self._terminate_preload_download_process()
            self.show_idle_status()
            return

        previous = self._preload_future
        self._preload_cancel_requested = False
        self._terminate_preload_download_process()
        if previous is not None and not previous.done():
            try:
                previous.cancel()
            except Exception:
                pass
        self._overlay.set_state("Processing", "Loading model...")
        self._preload_target_model = self._settings.model_size
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
        self._preload_future = self._preload_executor.submit(
            self._preload_model_worker
        )
        self._preload_progress_timer.start()

    def _select_cached_fallback_model(
        self,
        selected_model: str,
        cached_models: list[str],
    ) -> str | None:
        candidates = [
            m
            for m in cached_models
            if m != selected_model and m in FASTER_WHISPER_MODEL_SIZES
        ]
        if not candidates:
            return None

        selected_size = MODEL_ESTIMATED_SIZE_MB.get(selected_model)
        candidates_sorted = sorted(
            candidates,
            key=lambda name: MODEL_ESTIMATED_SIZE_MB.get(name, 0),
        )

        if selected_size is not None:
            smaller = [
                name
                for name in candidates_sorted
                if MODEL_ESTIMATED_SIZE_MB.get(name, 0) < selected_size
            ]
            if smaller:
                return smaller[-1]

        for name in reversed(VALID_MODEL_SIZES):
            if name in candidates:
                return name
        return candidates_sorted[-1]

    def _resolve_preload_fallback_settings(self) -> AppSettings | None:
        from .transcriber.local_faster_whisper import find_cached_models

        model_dir = getattr(self._settings, "model_dir", "")
        cached = find_cached_models(model_dir)
        fallback_model = self._select_cached_fallback_model(
            self._settings.model_size, cached
        )
        if fallback_model is None:
            return None
        return replace(self._settings, model_size=fallback_model)

    def _fallback_candidates_for_model(
        self,
        selected_model: str,
        cached_models: list[str],
    ) -> list[str]:
        candidates = [m for m in cached_models if m != selected_model]
        if not candidates:
            return []

        ordered: list[str] = []
        best = self._select_cached_fallback_model(selected_model, candidates)
        if best is not None:
            ordered.append(best)

        by_quality = sorted(
            candidates,
            key=lambda name: MODEL_ESTIMATED_SIZE_MB.get(name, 0),
            reverse=True,
        )
        for name in by_quality:
            if name not in ordered:
                ordered.append(name)
        return ordered

    def _preload_progress_detail(self, include_fallback_hint: bool = True) -> str:
        from .transcriber.local_faster_whisper import estimate_cached_model_bytes

        model_name = self._preload_target_model or self._settings.model_size
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

        if include_fallback_hint:
            detail = (
                f"{detail} Until it is ready, recordings use a cached fallback model "
                "if available. Use Cancel to abort download."
            )
        return detail

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

    def _preload_model_worker(self) -> None:
        """Background worker: eagerly load the configured local model."""
        from .transcriber.local_faster_whisper import (
            LocalFasterWhisperTranscriber,
            find_cached_models,
        )
        from .transcriber.local_nemotron import LocalNemotronTranscriber
        from .transcriber.local_webgpu_asr import LocalOnnxWebGpuTranscriber

        settings = self._settings
        try:
            self._download_model_for_preload(settings)
        except RuntimeError as exc:
            if self._preload_cancel_requested:
                self.model_preload_done.emit(False, str(exc))
                return
            # Download failed but cached models may still be usable.
            self._logger.warning("Model download failed: %s", exc)

        try:
            runtime_lease = self._acquire_transcriber_runtime(
                settings,
                allow_isolated=False,
            )
            try:
                transcriber = runtime_lease.transcriber
                if isinstance(
                    transcriber,
                    (
                        LocalFasterWhisperTranscriber,
                        LocalNemotronTranscriber,
                        LocalOnnxWebGpuTranscriber,
                    ),
                ):
                    transcriber.preload_model()
            finally:
                runtime_lease.release()
            self.model_preload_done.emit(True, f"Model loaded: {settings.model_size}")
            return
        except Exception as exc:
            self._logger.warning(
                "Model preload failed for %s: %s", settings.model_size, exc
            )

        # Attempt fallback: check for any locally cached model.
        model_dir = getattr(settings, "model_dir", "")
        cached = find_cached_models(model_dir)

        if not cached:
            self.model_preload_done.emit(
                False,
                f"Model '{settings.model_size}' could not be loaded and no "
                f"local models found. See {DOC_MODELS_PATH}",
            )
            return

        # Try fallback candidates from closest-smaller to best available quality.
        preferred_fallback_order = self._fallback_candidates_for_model(
            settings.model_size, cached
        )

        for fallback in preferred_fallback_order:
            try:
                fallback_settings = replace(settings, model_size=fallback)
                self._reset_transcriber_cache()
                runtime_lease = self._acquire_transcriber_runtime(
                    fallback_settings,
                    allow_isolated=False,
                )
                try:
                    transcriber = runtime_lease.transcriber
                    if isinstance(
                        transcriber,
                        (
                            LocalFasterWhisperTranscriber,
                            LocalNemotronTranscriber,
                            LocalOnnxWebGpuTranscriber,
                        ),
                    ):
                        transcriber.preload_model()
                finally:
                    runtime_lease.release()
                # Do NOT mutate ``self._settings`` or call
                # ``self._settings_store.save()`` here: this runs on the preload
                # worker thread and would race with Qt-thread reads/writes of
                # ``self._settings`` and concurrent JSON writes. Stage the
                # fallback model name for ``_on_model_preload_done`` to apply
                # on the Qt thread instead.
                self._pending_preload_fallback = fallback
                self.model_preload_done.emit(
                    True,
                    f"Fallback: using '{fallback}' model "
                    f"('{settings.model_size}' unavailable). "
                    f"Available local models: {', '.join(cached)}",
                )
                return
            except Exception:
                self._logger.warning("Fallback model %s also failed", fallback)
                continue

        self.model_preload_done.emit(
            False,
            f"Model '{settings.model_size}' unavailable. "
            f"Found models ({', '.join(cached)}) but none could be loaded.",
        )

    @QtCore.Slot(bool, str)
    def _on_model_preload_done(self, success: bool, message: str) -> None:
        if self._shutdown_started:
            return
        self._preload_progress_timer.stop()
        self._preload_target_model = None
        self._terminate_preload_download_process()
        session_active = (
            self._audio_capture is not None
            or self._streaming_recording
            or self._recording_start_in_progress
        )

        # Apply a fallback model resolved by the preload worker. This runs on
        # the Qt thread, so mutating ``self._settings`` and persisting it is
        # safe and cannot race with Qt-thread reads or concurrent JSON writes.
        pending_fallback = self._pending_preload_fallback
        self._pending_preload_fallback = None
        ready_model = pending_fallback or self._settings.model_size
        if pending_fallback is not None:
            self._settings = replace(self._settings, model_size=pending_fallback)
            try:
                self._settings_store.save(self._settings)
            except Exception:
                self._logger.exception(
                    "Failed to persist fallback model setting: %s",
                    pending_fallback,
                )

        if self._preload_cancel_requested:
            self._preload_cancel_requested = False
            if not session_active:
                self._overlay.set_state("Done", "Model preload canceled.")
                QtCore.QTimer.singleShot(1200, self.show_idle_status)
            return

        if success:
            self._logger.info("Model preload: %s", message)
            if "Fallback" in message:
                if session_active:
                    self._logger.info(
                        "Suppressing preload fallback notice during active session: %s",
                        message,
                    )
                else:
                    self._overlay.set_state("Error", message)
            else:
                if not session_active:
                    self._overlay.set_state(
                        "Done",
                        f"Model '{ready_model}' is ready. Next transcription uses it.",
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

            runtime_lease = self._acquire_transcriber_runtime(settings)
            transcriber = runtime_lease.transcriber
            init_elapsed_ms = round(
                (time.perf_counter() - init_started_at) * 1000
            )
            self._set_transcriber_progress_callback(
                transcriber,
                lambda detail: self.transcription_progress.emit(
                    request_token,
                    str(detail),
                ),
            )
            if job is not None:
                self._set_transcriber_cancel_check(
                    transcriber, lambda: job.aborting
                )
            transcribe_started_at = time.perf_counter()
            text = transcriber.transcribe_batch(wav_bytes)
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
        finally:
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
            self._logger.info(
                "transcription_timing engine=%s model=%s init_ms=%d "
                "transcribe_ms=%d total_ms=%d audio_bytes=%d outcome=%s "
                "runtime_device=%s gpu_available=%s runtime_details=%s",
                settings.engine,
                self._selected_model_name(settings),
                init_elapsed_ms,
                transcribe_elapsed_ms,
                total_elapsed_ms,
                len(wav_bytes),
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
                except Exception:
                    self._logger.exception("Failed to clear transcriber cancel hook")
                try:
                    self._set_transcriber_progress_callback(transcriber, None)
                except Exception:
                    self._logger.exception("Failed to clear transcriber progress hook")
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
                terminal_kind = "canceled"
                terminal_payload = ""
            else:
                if transcriber is None:
                    raise TranscriptionError("Streaming session was not initialized.")
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

    def _stop_active_capture(self, *, persist_audio: bool) -> bytes:
        capture = self._audio_capture
        self._audio_capture = None
        if capture is None:
            return b""

        wav_bytes = b""
        try:
            wav_bytes = capture.stop()
        except Exception:
            self._logger.exception("Failed to stop active audio capture")

        self._save_recording_artifacts(capture, wav_bytes)
        if persist_audio and wav_bytes:
            self._persist_last_recording_audio(wav_bytes)
        return wav_bytes

    def _teardown_active_stream_runtime(self, *, preserve_audio: bool) -> bytes:
        wav_bytes = self._stop_active_capture(persist_audio=preserve_audio)

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

        return wav_bytes

    def _on_stream_audio_chunk(self, chunk: bytes) -> None:
        """Called from the PortAudio callback thread — must be lightweight.

        Focus-change abort is handled by ``_focus_poll_timer`` on the Qt
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
        try:
            transcriber.push_audio_chunk(chunk)
        except Exception as exc:
            if self._stream_chunk_error_reported:
                return
            self._stream_chunk_error_reported = True
            self._stream_abort_requested = True
            self._logger.exception("Failed to push streaming audio chunk")
            self._emit_stream_runtime_failure(
                f"Streaming chunk push failed: {exc}"
            )

    def _get_or_create_transcriber(self, settings: AppSettings):
        cache_key = (
            settings.engine,
            settings.model_size,
            settings.language_mode,
            settings.vad_enabled,
            getattr(settings, "offline_mode", False),
            getattr(settings, "model_dir", ""),
            getattr(settings, "groq_model", ""),
            getattr(settings, "openai_model", ""),
            getattr(settings, "deepgram_model", ""),
            getattr(settings, "assemblyai_model", ""),
            getattr(settings, "elevenlabs_model", ""),
            getattr(settings, "azure_speech_model", ""),
            getattr(settings, "azure_endpoint", ""),
            getattr(settings, "funasr_model", ""),
            bool(getattr(settings, "keep_onnx_model_loaded", False)),
            bool(getattr(settings, "streaming_full_final_transcript", False)),
        )
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
        self._flush_deferred_background_results()

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
        self._last_transcript = text

        if not text.strip():
            self._mark_last_recording_completed()
            self._overlay.set_state("Done", "No speech detected.")
            self._reveal_overlay_result(is_error=False)
            self._last_transcribe_settings = None
            self._reset_streaming_state()
            return

        used_settings = (
            self._last_transcribe_settings
            or stream_settings
            or self._settings
        )
        self._append_transcript_history(text, used_settings, session_mode)

        if session_mode == "streaming":
            first_stream_insertion = not bool(self._stream_text_state.committed_text)
            final_insertion, final_text = self._stream_text_state.finalize_append_only(
                text
            )
            if final_insertion:
                if not self._insert_text_at_target(
                    final_insertion,
                    restore_focus=True,
                    target_handle=target_handle,
                    target_signature=target_signature,
                    separate_from_previous_transcript=first_stream_insertion,
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
                separate_from_previous_transcript=True,
            ):
                self._reveal_overlay_result(is_error=True)
                self._mark_last_recording_completed()
                self._last_transcribe_settings = None
                self._reset_streaming_state()
                return

            self._overlay.set_state("Done", text)

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
        if (
            self._recording_start_in_progress
            or self._recording_stop_in_progress
        ):
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
        focus change aborts the stream. A batch recording allows it — the
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
            separate_from_previous_transcript=True,
        )
        if not inserted:
            self._logger.warning(
                "Background transcription insertion failed; saved to history "
                "only. token=%s mode=%s engine=%s model=%s",
                job.token,
                job.mode,
                job.engine,
                job.model,
            )
        return inserted

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
                self._insert_background_transcription(jobs[0], text)
            except Exception:
                self._logger.exception(
                    "Failed to insert deferred background transcription; "
                    "saved to history only. tokens=%s",
                    [job.token for job in jobs],
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
        return [
            (jobs, _join_transcripts(texts))
            for jobs, texts in groups
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
        self._on_transcription_failed(error_text, request_token=request_token)

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
                # A queued/canceled transcription failed while a newer session
                # is active. Drop it quietly without disturbing the live session;
                # keep its audio available for a manual retry.
                self._promote_request_audio_for_retry(request_token)
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
        if runtime_stream_failed:
            wav_bytes = self._teardown_active_stream_runtime(preserve_audio=True)
            if wav_bytes:
                self._last_failed_wav_bytes = bytes(wav_bytes)
                preserved_audio = True
        self._streaming_recording = False
        self._active_stream_transcriber = None
        self._active_stream_settings = None
        self._last_transcribe_settings = None
        self._reset_streaming_state()
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
            f"{error_text} {self._retry_guidance(has_retry_audio=preserved_audio)}",
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
            first_stream_insertion = not bool(self._stream_text_state.committed_text)
            append = self._stream_text_state.apply_partial_append_only(text)
            display_text = append.display_text
            if append.insertion:
                if not self._insert_text_at_target(
                    append.insertion,
                    restore_focus=False,
                    copy_on_error=False,
                    show_overlay_error=False,
                    separate_from_previous_transcript=first_stream_insertion,
                ):
                    self._request_stream_abort(
                        "Streaming aborted: failed to insert live text.",
                        beep=STREAMING_BEEP_ON_ABORT,
                    )
                    return
        else:
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
        if not self._streaming_recording or self._stream_abort_requested:
            return
        if not STREAMING_ABORT_ON_FOCUS_CHANGE:
            return
        if self._is_stream_target_active():
            return
        self._request_stream_abort(
            "Streaming aborted: target window focus changed.",
            beep=STREAMING_BEEP_ON_ABORT,
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
        partial_transcript = normalize_stream_text(
            self._stream_text_state.live_text
            or self._stream_text_state.last_partial_text
        )
        partial_settings = self._active_stream_settings or replace(self._settings)

        self._focus_poll_timer.stop()
        capture = self._audio_capture
        self._audio_capture = None
        wav_bytes = b""
        if capture is not None:
            try:
                wav_bytes = capture.stop()
            except Exception:
                self._logger.exception("Failed to stop audio capture during abort")
        if capture is not None:
            self._save_recording_artifacts(capture, wav_bytes)
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
            self._logger.exception("Failed to stop/abort streaming transcriber during abort")
        finally:
            if runtime_lease is not None:
                runtime_lease.release()

        self._streaming_recording = False
        self._active_stream_settings = None
        self._reset_streaming_state()
        if partial_transcript.strip():
            self._append_transcript_history(
                partial_transcript, partial_settings, "streaming"
            )
            self._last_transcript = partial_transcript
            self._overlay.set_state(
                "Error",
                f"{reason} Partial transcript (saved to history): "
                f"{partial_transcript}",
            )
        else:
            self._overlay.set_state("Error", reason)
        self._reveal_overlay_result(is_error=True)
        # Aborting this session removed the capture that was blocking any
        # deferred background inserts; deliver every completed one now — even if
        # another transcription is still running — instead of leaving them stuck.
        self._flush_deferred_background_results(ignore_active_transcription=True)

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

    def _insert_text_at_target(
        self,
        text: str,
        *,
        restore_focus: bool,
        copy_on_error: bool = True,
        show_overlay_error: bool = True,
        separate_from_previous_transcript: bool = False,
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
        target_key = (handle or insert_hwnd, signature or insert_hwnd)
        insertion_text = self._with_transcript_separator(
            text,
            target_key=target_key,
            enabled=separate_from_previous_transcript,
        )
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
                self._overlay.set_state("Error", detail)
            self._logger.exception("Text insertion failed")
            return False
        self._last_insert_target_key = target_key
        self._last_insert_ended_with_whitespace = insertion_text[-1:].isspace()
        self._logger.info(
            "text_insertion outcome=success chars=%d target_hwnd=%s "
            "restore_focus=%s paste_mode=%s",
            len(insertion_text),
            insert_hwnd,
            restore_focus,
            self._settings.paste_mode,
        )
        return True

    def _with_transcript_separator(
        self,
        text: str,
        *,
        target_key: tuple[object, object],
        enabled: bool,
    ) -> str:
        """Separate consecutive app transcripts inserted into one target.

        Windows exposes neither a reliable cross-application caret offset nor
        the character immediately before it. The controller can, however,
        identify consecutive successful app inserts into the same focused
        control. Prefix exactly one space at that transcript boundary while
        leaving streaming deltas and punctuation continuations untouched.
        """
        insertion = str(text or "")
        if (
            enabled
            and self._last_insert_target_key == target_key
            and not self._last_insert_ended_with_whitespace
            and insertion
            and not insertion[0].isspace()
            and insertion[0] not in _NO_TRANSCRIPT_SPACE_BEFORE
        ):
            return f" {insertion}"
        return insertion

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
            int(self._settings.history_max_items)
            if limit is None
            else int(limit)
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
        managed_last_recording = self._last_recording_store.is_managed_audio_path(
            path
        )
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
            if text:
                self._append_transcript_history(
                    text,
                    settings,
                    "import",
                    source_recording_id=recording_id,
                    track_for_edit=False,
                )
            if managed_last_recording:
                self._last_recording_store.mark_completed(**conditional_transition)
            return True, text or "No speech detected."
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
            if transcriber is not None:
                try:
                    self._set_transcriber_progress_callback(transcriber, None)
                except Exception:
                    self._logger.exception(
                        "Failed to clear imported-transcription progress hook"
                    )
            if runtime_lease is not None:
                try:
                    runtime_lease.release()
                except Exception:
                    # Runtime cleanup must not discard a transcript that the
                    # provider already returned successfully.
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
        if self._cancel_model_preload_if_running():
            return

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
                self._last_recording_store.mark_canceled("Transcription canceled by user.")
            except Exception:
                self._logger.exception("Failed to mark canceled transcription")
            # Clearing the active transcription may unblock deferred background
            # inserts that were waiting behind it; deliver every completed one now.
            self._flush_deferred_background_results(ignore_active_transcription=True)
            self._overlay.set_state("Done", "Transcription canceled.")
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

        if self._transcription_runtime_active():
            self._pending_transcriber_cache_reset = True
            return
        self._reset_transcriber_cache()
        if self._settings.engine == DEFAULT_ENGINE:
            self._start_local_model_preload()

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
        self._terminate_preload_download_process()
        self._overlay.set_state("Processing", "Canceling model download...")
        return True

    def _set_preload_download_process(
        self,
        process: subprocess.Popen | None,
    ) -> None:
        with self._preload_download_lock:
            self._preload_download_process = process

    def _terminate_preload_download_process(self) -> None:
        with self._preload_download_lock:
            process = self._preload_download_process
            self._preload_download_process = None

        if process is None:
            return
        terminate_model_download_process(process)

    def _download_model_for_preload(self, settings: AppSettings) -> None:
        from .transcriber.local_faster_whisper import find_cached_models

        if self._preload_cancel_requested:
            raise RuntimeError("Model download canceled.")
        if getattr(settings, "offline_mode", False):
            return

        model_name = settings.model_size
        model_dir = getattr(settings, "model_dir", "")
        cached = find_cached_models(model_dir)
        if model_name in cached:
            return

        try:
            process = start_model_download_process(model_name, model_dir)
        except Exception as exc:
            raise RuntimeError(f"Failed to start model download: {exc}") from exc

        self._set_preload_download_process(process)
        try:
            while True:
                if self._preload_cancel_requested:
                    self._terminate_preload_download_process()
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
                    return
                time.sleep(0.2)
        finally:
            self._set_preload_download_process(None)

    def _register_hotkey_with_fallback(self) -> bool:
        preferred = self._settings.hotkey
        try:
            self._hotkey_manager.register(preferred)
            self._hotkey_notice = None
            return True
        except (HotkeyRegistrationError, ValueError):
            self._logger.exception("Failed to register preferred hotkey: %s", preferred)

        if preferred == FALLBACK_HOTKEY:
            self._hotkey_notice = f"Hotkey registration failed ({preferred}). Choose a different hotkey in Settings."
            return False

        try:
            self._hotkey_manager.register(FALLBACK_HOTKEY)
            self._settings = replace(self._settings, hotkey=FALLBACK_HOTKEY)
            try:
                self._settings_store.save(self._settings)
            except Exception:
                self._logger.exception("Failed to persist fallback hotkey")
            self._hotkey_notice = (
                f"Preferred hotkey '{preferred}' unavailable. "
                f"Using fallback '{FALLBACK_HOTKEY}'."
            )
            return True
        except (HotkeyRegistrationError, ValueError):
            self._logger.exception(
                "Fallback hotkey registration failed: %s", FALLBACK_HOTKEY
            )
            self._hotkey_notice = (
                "Hotkey registration failed for preferred and fallback hotkeys. "
                "Update hotkey in Settings."
            )
            return False

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

    def refresh_hotkey_registration(self) -> None:
        """Re-register global hotkeys after Windows resumes or opens Explorer."""
        self._hotkey_registration_ok = self._register_hotkey_with_fallback()
        self._cancel_hotkey_registration_ok = self._register_cancel_hotkey()
        if not self._hotkey_registration_ok or not self._cancel_hotkey_registration_ok:
            self._logger.warning("Global hotkey refresh did not fully succeed.")
