from __future__ import annotations

import concurrent.futures
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from dataclasses import replace
from pathlib import Path

from PySide6 import QtCore, QtGui

from .app_paths import debug_audio_path, recordings_dir
from .audio_capture import AudioCapture, AudioCaptureError
from .config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_ENGINE,
    DEFAULT_START_BEEP_TONE,
    DOC_MODELS_PATH,
    FALLBACK_HOTKEY,
    MODEL_ESTIMATED_SIZE_MB,
    STREAMING_ABORT_ON_FOCUS_CHANGE,
    STREAMING_ABORT_BEEP_DURATION_MS,
    STREAMING_ABORT_BEEP_HZ,
    STREAMING_BEEP_ON_ABORT,
    STREAMING_ENGINES,
    STREAMING_FOCUS_POLL_MS,
    STREAMING_LIVE_INSERT_ENABLED,
    STREAMING_OVERLAY_MAX_CHARS,
    STREAMING_STABLE_WORD_GUARD,
    OVERLAY_OPACITY_MAX_PERCENT,
    OVERLAY_OPACITY_MIN_PERCENT,
    VALID_MODEL_SIZES,
    VAD_ENERGY_THRESHOLD_MIN,
    VALID_START_BEEP_TONES,
    VAD_MAX_SILENCE_MS,
    VAD_MIN_SPEECH_MS,
)
from .hotkey import HotkeyManager, HotkeyRegistrationError
from .overlay_ui import OverlayUI
from .settings_store import AppSettings, SettingsStore
from .text_inserter import TextInserter, TextInsertionError
from .transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore
from .transcriber import create_transcriber
from .transcriber.base import TranscriptionError
from .vad import EnergyVad
from .window_focus import FocusSignature, Win32WindowFocusHelper, WindowFocusHelper


class DictationController(QtCore.QObject):
    transcription_ready = QtCore.Signal(str)
    transcription_failed = QtCore.Signal(str)
    transcription_partial = QtCore.Signal(str)
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

        self._settings: AppSettings = self._settings_store.load()
        self._audio_capture: AudioCapture | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._preload_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._preload_future: concurrent.futures.Future | None = None
        self._transcriber_cache_lock = threading.Lock()
        self._transcriber_cache_key = None
        self._transcriber_cache = None
        self._hotkey_registration_ok = False
        self._hotkey_notice: str | None = None
        self._cancel_hotkey_registration_ok = False
        self._cancel_hotkey_notice: str | None = None
        self._target_window_handle: int | None = None
        self._target_focus_signature: FocusSignature | None = None
        self._last_transcript: str = ""
        self._last_failed_wav_bytes: bytes = b""
        self._last_failed_settings: AppSettings | None = None
        self._last_transcribe_settings: AppSettings | None = None
        self._transcription_cancel_requested = False
        self._active_batch_settings: AppSettings | None = None
        self._streaming_recording = False
        self._active_stream_transcriber = None
        self._active_stream_settings: AppSettings | None = None
        self._stream_chunk_error_reported = False
        self._stream_abort_requested = False
        self._stream_committed_text = ""
        self._stream_last_partial_text = ""
        self._active_session_mode = "batch"
        self._focus_poll_timer = QtCore.QTimer(self)
        self._focus_poll_timer.setInterval(STREAMING_FOCUS_POLL_MS)
        self._focus_poll_timer.timeout.connect(self._on_stream_focus_poll)
        self._preload_progress_timer = QtCore.QTimer(self)
        self._preload_progress_timer.setInterval(600)
        self._preload_progress_timer.timeout.connect(self._on_preload_progress_poll)
        self._preload_target_model: str | None = None
        self._preload_last_bytes: int = 0
        self._preload_last_poll_at: float = 0.0
        self._preload_cancel_requested = False
        self._preload_download_process: subprocess.Popen | None = None
        self._preload_download_lock = threading.Lock()

        self.transcription_ready.connect(self._on_transcription_ready)
        self.transcription_failed.connect(self._on_transcription_failed)
        self.transcription_partial.connect(self._on_transcription_partial)
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
        self._hotkey_manager.unregister()
        if self._cancel_hotkey_manager is not None:
            self._cancel_hotkey_manager.unregister()
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
        if self._active_stream_transcriber is not None:
            try:
                self._active_stream_transcriber.stop_stream()
            except Exception:
                pass
            self._active_stream_transcriber = None
            self._active_stream_settings = None
        preload_future = self._preload_future
        self._preload_future = None
        if preload_future is not None:
            try:
                preload_future.cancel()
            except Exception:
                pass
        self._reset_streaming_state()
        self._executor.shutdown(wait=False, cancel_futures=False)
        self._preload_executor.shutdown(wait=False, cancel_futures=False)

    def reload_settings(self, re_register_hotkey: bool = True) -> None:
        self._settings = self._settings_store.load()
        setter = getattr(self._secret_store, "set_insecure_fallback_enabled", None)
        if callable(setter):
            try:
                setter(bool(getattr(self._settings, "allow_insecure_key_storage", False)))
            except Exception:
                self._logger.exception("Failed to apply insecure key fallback setting")
        self._overlay.set_opacity_percent(self._settings.overlay_opacity_percent)
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
        if self._audio_capture is None:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self) -> None:
        self._overlay.ensure_compact_size()
        self._overlay.set_state("Listening", "Starting recording...")
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
        if (
            self._settings.engine not in STREAMING_ENGINES
            and self._settings.mode == "streaming"
        ):
            self._overlay.set_state(
                "Error",
                "Streaming is not available for the selected provider. "
                "Switch to batch mode, or use local/AssemblyAI/Deepgram for streaming.",
            )
            return

        self._target_window_handle = self._window_focus_helper.capture_target_window()
        self._target_focus_signature = self._capture_target_signature()
        if self._settings.mode == "streaming":
            self._start_streaming_recording()
            return
        self._start_batch_recording(
            batch_settings or replace(self._settings),
            fallback_notice=fallback_notice,
        )

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
        self._stream_last_partial_text = ""

        # Play beep BEFORE starting capture so the microphone does not
        # pick up the beep sound (winsound.Beep is synchronous/blocking).
        self._play_start_beep()

        try:
            capture.start()
        except AudioCaptureError as exc:
            self._active_batch_settings = None
            self._overlay.set_state("Error", str(exc))
            self._logger.exception("Audio capture failed to start")
            return

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
        )

    def _start_streaming_recording(self) -> None:
        settings_snapshot = replace(self._settings)
        try:
            transcriber = self._get_or_create_transcriber(settings_snapshot)
            transcriber.start_stream(on_partial=self._emit_stream_partial)
        except NotImplementedError as exc:
            self._overlay.set_state("Error", str(exc))
            return
        except TranscriptionError as exc:
            self._overlay.set_state("Error", str(exc))
            return
        except Exception as exc:
            self._logger.exception("Failed to start streaming transcriber")
            self._overlay.set_state("Error", f"Failed to start streaming: {exc}")
            return

        capture = self._build_audio_capture(chunk_callback=self._on_stream_audio_chunk)

        # Play beep BEFORE starting capture so the microphone does not
        # pick up the beep sound (winsound.Beep is synchronous/blocking).
        self._play_start_beep()

        try:
            capture.start()
        except AudioCaptureError as exc:
            try:
                transcriber.stop_stream()
            except Exception:
                pass
            self._overlay.set_state("Error", str(exc))
            self._logger.exception("Audio capture failed to start")
            return

        self._streaming_recording = True
        self._active_batch_settings = None
        self._stream_chunk_error_reported = False
        self._stream_abort_requested = False
        self._stream_committed_text = ""
        self._stream_last_partial_text = ""
        self._active_session_mode = "streaming"
        self._active_stream_transcriber = transcriber
        self._active_stream_settings = settings_snapshot
        self._audio_capture = capture
        if STREAMING_ABORT_ON_FOCUS_CHANGE:
            self._focus_poll_timer.start()
        self._overlay.set_state(
            "Listening",
            "Streaming active. Speak now, press hotkey to finalize.",
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
        )

    def stop_recording(self) -> None:
        capture = self._audio_capture
        if capture is None:
            return

        self._audio_capture = None
        wav_bytes = capture.stop()
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
            self._overlay.set_state("Processing", "Finalizing streaming transcript...")
            self._executor.submit(self._finalize_stream_worker)
            return

        if not wav_bytes:
            self._overlay.set_state("Error", "No audio captured.")
            self._active_batch_settings = None
            return

        settings_snapshot = self._active_batch_settings or replace(self._settings)
        self._active_batch_settings = None
        self._last_transcribe_settings = replace(settings_snapshot)
        self._transcription_cancel_requested = False
        self._overlay.set_state("Processing", "Transcribing audio...")
        self._executor.submit(self._transcribe_worker, wav_bytes, settings_snapshot)

    def _auto_stop_from_vad(self) -> None:
        QtCore.QTimer.singleShot(0, self.stop_recording)

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

    def _save_recording_artifacts(self, capture: AudioCapture, wav_bytes: bytes) -> None:
        if not wav_bytes:
            return

        if self._settings.save_last_wav:
            path = debug_audio_path()
            try:
                capture.save_wav(path, wav_bytes)
            except Exception:
                self._logger.exception("Failed to save debug wav")

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
                if name.lower().endswith(".wav")
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
        self._stream_committed_text = ""
        self._stream_last_partial_text = ""
        self._active_batch_settings = None
        self._active_session_mode = "batch"
        self._streaming_recording = False
        self._target_window_handle = None
        self._target_focus_signature = None

    def _reset_transcriber_cache(self) -> None:
        with self._transcriber_cache_lock:
            self._transcriber_cache = None
            self._transcriber_cache_key = None

    # -- Model preloading -----------------------------------------------------

    def _start_local_model_preload(self) -> None:
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

            self._preload_last_bytes = estimate_cached_model_bytes(
                self._preload_target_model,
                getattr(self._settings, "model_dir", ""),
            )
        except Exception:
            self._preload_last_bytes = 0
        self._preload_last_poll_at = time.monotonic()
        self._preload_future = self._preload_executor.submit(
            self._preload_model_worker
        )
        self._preload_progress_timer.start()

    @staticmethod
    def _format_progress_bar(progress: float, width: int = 18) -> str:
        clamped = max(0.0, min(1.0, float(progress)))
        filled = int(round(clamped * width))
        return f"[{'#' * filled}{'.' * (width - filled)}]"

    def _select_cached_fallback_model(
        self,
        selected_model: str,
        cached_models: list[str],
    ) -> str | None:
        candidates = [m for m in cached_models if m != selected_model]
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
        estimated_mb = MODEL_ESTIMATED_SIZE_MB.get(model_name)
        total_bytes = int(estimated_mb * 1_000_000) if estimated_mb else 0
        downloaded_bytes = estimate_cached_model_bytes(
            model_name,
            getattr(self._settings, "model_dir", ""),
        )

        now = time.monotonic()
        speed_bps = 0.0
        if self._preload_last_poll_at > 0.0 and now > self._preload_last_poll_at:
            delta_bytes = max(0, downloaded_bytes - self._preload_last_bytes)
            delta_s = now - self._preload_last_poll_at
            if delta_s > 0:
                speed_bps = delta_bytes / delta_s
        self._preload_last_poll_at = now
        self._preload_last_bytes = downloaded_bytes

        downloaded_mb = downloaded_bytes / 1_000_000.0
        speed_mb_s = speed_bps / 1_000_000.0

        if total_bytes > 0:
            progress = max(0.0, min(1.0, downloaded_bytes / float(total_bytes)))
            percent = int(round(progress * 100))
            bar = self._format_progress_bar(progress)
            detail = (
                f"Downloading '{model_name}' {percent}% {bar} "
                f"({downloaded_mb:.0f}/{estimated_mb:.0f} MB, {speed_mb_s:.1f} MB/s)."
            )
        else:
            detail = (
                f"Downloading '{model_name}'... "
                f"{downloaded_mb:.0f} MB cached, {speed_mb_s:.1f} MB/s."
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
        if self._audio_capture is not None or self._streaming_recording:
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

        settings = self._settings
        download_failed = False
        try:
            self._download_model_for_preload(settings)
        except RuntimeError as exc:
            if self._preload_cancel_requested:
                self.model_preload_done.emit(False, str(exc))
                return
            # Download failed but cached models may still be usable.
            self._logger.warning("Model download failed: %s", exc)
            download_failed = True

        try:
            transcriber = self._get_or_create_transcriber(settings)
            if isinstance(transcriber, LocalFasterWhisperTranscriber):
                transcriber.preload_model()
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
                transcriber = self._get_or_create_transcriber(fallback_settings)
                if isinstance(transcriber, LocalFasterWhisperTranscriber):
                    transcriber.preload_model()
                self._settings = fallback_settings
                try:
                    self._settings_store.save(fallback_settings)
                except Exception:
                    self._logger.exception(
                        "Failed to persist fallback model setting: %s",
                        fallback,
                    )
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
        self._preload_progress_timer.stop()
        ready_model = self._preload_target_model or self._settings.model_size
        self._preload_target_model = None
        self._terminate_preload_download_process()

        if self._preload_cancel_requested:
            self._preload_cancel_requested = False
            if self._audio_capture is None and not self._streaming_recording:
                self._overlay.set_state("Done", "Model preload canceled.")
                QtCore.QTimer.singleShot(1200, self.show_idle_status)
            return

        if success:
            self._logger.info("Model preload: %s", message)
            if "Fallback" in message:
                self._overlay.set_state("Error", message)
            else:
                if self._audio_capture is None and not self._streaming_recording:
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
                self._overlay.set_state("Done", message)
                QtCore.QTimer.singleShot(1200, self.show_idle_status)
            else:
                self._overlay.set_state("Error", message)

    # -- Transcription workers ------------------------------------------------

    def _transcribe_worker(self, wav_bytes: bytes, settings: AppSettings) -> None:
        try:
            transcriber = self._get_or_create_transcriber(settings)
        except TranscriptionError as exc:
            self._last_failed_wav_bytes = bytes(wav_bytes)
            self._last_failed_settings = replace(settings)
            self.transcription_failed.emit(str(exc))
            return
        except Exception as exc:
            self._last_failed_wav_bytes = bytes(wav_bytes)
            self._last_failed_settings = replace(settings)
            self._logger.exception("Failed to create transcriber")
            self.transcription_failed.emit(
                f"Transcriber initialization failed: {exc}"
            )
            return

        try:
            text = transcriber.transcribe_batch(wav_bytes)
            if not self._transcription_cancel_requested:
                self._last_failed_wav_bytes = b""
                self._last_failed_settings = None
            self.transcription_ready.emit(text)
        except NotImplementedError as exc:
            self._last_failed_wav_bytes = bytes(wav_bytes)
            self._last_failed_settings = replace(settings)
            self.transcription_failed.emit(str(exc))
        except TranscriptionError as exc:
            self._last_failed_wav_bytes = bytes(wav_bytes)
            self._last_failed_settings = replace(settings)
            self.transcription_failed.emit(str(exc))
        except FileNotFoundError as exc:
            self._last_failed_wav_bytes = bytes(wav_bytes)
            self._last_failed_settings = replace(settings)
            self._logger.exception("Transcription failed due to missing file path")
            self.transcription_failed.emit(
                "Transcription failed: missing file path. "
                "Check input path and TEMP/TMP folder configuration. "
                f"({exc})"
            )
        except Exception as exc:
            self._last_failed_wav_bytes = bytes(wav_bytes)
            self._last_failed_settings = replace(settings)
            self._logger.exception("Unexpected transcription failure")
            self.transcription_failed.emit(f"Unexpected transcription error: {exc}")

    def _finalize_stream_worker(self) -> None:
        try:
            transcriber = self._active_stream_transcriber
            if transcriber is None:
                raise TranscriptionError("Streaming session was not initialized.")
            text = transcriber.stop_stream()
            self.transcription_ready.emit(text)
        except NotImplementedError as exc:
            self.transcription_failed.emit(str(exc))
        except TranscriptionError as exc:
            self.transcription_failed.emit(str(exc))
        except Exception as exc:
            self._logger.exception("Unexpected streaming finalization failure")
            self.transcription_failed.emit(f"Unexpected streaming error: {exc}")
        finally:
            self._focus_poll_timer.stop()
            self._active_stream_transcriber = None
            self._active_stream_settings = None
            self._streaming_recording = False

    def _emit_stream_partial(self, text: str) -> None:
        self.transcription_partial.emit(text)

    def _on_stream_audio_chunk(self, chunk: bytes) -> None:
        """Called from the PortAudio callback thread — must be lightweight.

        Focus-change abort is handled by ``_focus_poll_timer`` on the Qt
        main thread; we intentionally avoid Win32 API calls here because
        the PortAudio real-time thread must not block on system calls.
        """
        if self._audio_capture is None:
            return
        if self._stream_abort_requested:
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
            self._logger.exception("Failed to push streaming audio chunk")
            self.transcription_failed.emit(f"Streaming chunk push failed: {exc}")

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
        )
        with self._transcriber_cache_lock:
            if (
                self._transcriber_cache is None
                or self._transcriber_cache_key != cache_key
            ):
                self._transcriber_cache = create_transcriber(
                    settings, secret_store=self._secret_store
                )
                self._transcriber_cache_key = cache_key
            return self._transcriber_cache

    @QtCore.Slot(str)
    def _on_transcription_ready(self, text: str) -> None:
        if self._transcription_cancel_requested:
            self._transcription_cancel_requested = False
            self._last_transcribe_settings = None
            self._overlay.set_state("Done", "Transcription canceled.")
            self._reset_streaming_state()
            return

        session_mode = self._active_session_mode
        self._focus_poll_timer.stop()
        self._streaming_recording = False
        self._active_stream_transcriber = None
        self._active_stream_settings = None
        self._stream_abort_requested = False
        self._last_transcript = text

        if not text.strip():
            self._overlay.set_state("Done", "No speech detected.")
            self._reset_streaming_state()
            return

        if session_mode == "streaming":
            final_text = self._normalize_stream_text(text)
            committed = self._stream_committed_text
            tail = self._best_stream_finalize_tail(committed, final_text)
            if tail:
                insertion = self._stream_insertion_text(committed, tail)
                if not self._insert_text_at_target(insertion, restore_focus=True):
                    self._reset_streaming_state()
                    return
                self._stream_committed_text = self._stream_join_text(committed, tail)
            self._overlay.set_state("Done", final_text)
        else:
            if not self._insert_text_at_target(text, restore_focus=True):
                self._reset_streaming_state()
                return

            self._overlay.set_state("Done", text)

        if self._settings.keep_transcript_in_clipboard:
            QtGui.QGuiApplication.clipboard().setText(text)
        try:
            used = self._last_transcribe_settings or self._settings
            model_name = used.model_size
            if used.engine == "groq":
                model_name = used.groq_model
            elif used.engine == "openai":
                model_name = used.openai_model
            entry = TranscriptHistoryEntry.new(
                text=text,
                engine=used.engine,
                model=model_name,
                mode=session_mode,
            )
            self._history_store.add_entry(entry, used.history_max_items)
        except Exception:
            self._logger.exception("Failed to append transcript history")
        finally:
            self._last_transcribe_settings = None
        self._reset_streaming_state()

    @QtCore.Slot(str)
    def _on_transcription_failed(self, error_text: str) -> None:
        if self._transcription_cancel_requested:
            self._transcription_cancel_requested = False
            self._last_transcribe_settings = None
            self._overlay.set_state("Done", "Transcription canceled.")
            self._reset_streaming_state()
            return
        self._focus_poll_timer.stop()
        self._streaming_recording = False
        self._active_stream_transcriber = None
        self._active_stream_settings = None
        self._last_transcribe_settings = None
        self._reset_streaming_state()
        if self._settings.save_last_wav and self._last_failed_wav_bytes:
            try:
                debug_audio_path().write_bytes(self._last_failed_wav_bytes)
            except Exception:
                self._logger.exception("Failed to persist failed recording WAV")
        self._overlay.set_state(
            "Error",
            f"{error_text} Use Retry to run the same audio again.",
        )

    @QtCore.Slot(str)
    def _on_transcription_partial(self, partial_text: str) -> None:
        if not self._streaming_recording or self._audio_capture is None:
            return
        text = self._normalize_stream_text(partial_text)
        if not text:
            return
        if STREAMING_ABORT_ON_FOCUS_CHANGE and not self._is_stream_target_active():
            self._request_stream_abort(
                "Streaming aborted: target window focus changed.",
                beep=STREAMING_BEEP_ON_ABORT,
            )
            return
        previous_partial = self._stream_last_partial_text
        self._stream_last_partial_text = text
        if STREAMING_LIVE_INSERT_ENABLED:
            delta, next_committed = self._compute_stream_live_delta(
                self._stream_committed_text,
                previous_partial,
                text,
            )
            if delta:
                insertion = self._stream_insertion_text(
                    self._stream_committed_text,
                    delta,
                )
                if not self._insert_text_at_target(
                    insertion,
                    restore_focus=False,
                    copy_on_error=False,
                    show_overlay_error=False,
                ):
                    self._request_stream_abort(
                        "Streaming aborted: failed to insert live text.",
                        beep=STREAMING_BEEP_ON_ABORT,
                    )
                    return
                self._stream_committed_text = next_committed
        if len(text) > STREAMING_OVERLAY_MAX_CHARS:
            text = text[-STREAMING_OVERLAY_MAX_CHARS:]
            text = f"...{text}".strip()
        self._overlay.set_state("Listening", f"Live: {text}")

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
        self._abort_streaming_session(reason, beep=beep, finalize_stream=False)

    def _request_stream_abort(self, reason: str, beep: bool) -> None:
        if self._stream_abort_requested:
            return
        self._stream_abort_requested = True
        emit_beep = beep
        if beep:
            try:
                threading.Thread(
                    target=self._play_abort_beep,
                    name="tts_app_abort_beep",
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
    ) -> None:
        if beep:
            self._play_abort_beep()

        self._focus_poll_timer.stop()
        capture = self._audio_capture
        self._audio_capture = None
        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass

        transcriber = self._active_stream_transcriber
        self._active_stream_transcriber = None
        if transcriber is not None:
            try:
                if finalize_stream:
                    transcriber.stop_stream()
                elif hasattr(transcriber, "abort_stream"):
                    transcriber.abort_stream()
                else:
                    transcriber.stop_stream()
            except Exception:
                pass

        self._streaming_recording = False
        self._active_stream_settings = None
        self._reset_streaming_state()
        self._overlay.set_state("Error", reason)

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

    def _capture_target_signature(self) -> FocusSignature | None:
        getter = getattr(self._window_focus_helper, "capture_target_signature", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                self._logger.exception("Failed to capture target focus signature")
                return None
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

    def _insert_text_at_target(
        self,
        text: str,
        *,
        restore_focus: bool,
        copy_on_error: bool = True,
        show_overlay_error: bool = True,
    ) -> bool:
        if not text.strip():
            return True
        if restore_focus:
            try:
                self._window_focus_helper.restore_target_window(
                    self._target_window_handle
                )
            except Exception:
                self._logger.exception("Failed to restore target window focus")
        insert_hwnd = self._target_insert_window()
        try:
            self._text_inserter.insert_text_with_options(
                text,
                target_hwnd=insert_hwnd,
                paste_mode=self._settings.paste_mode,
            )
        except TextInsertionError as exc:
            if copy_on_error:
                QtGui.QGuiApplication.clipboard().setText(text)
            if show_overlay_error:
                detail = str(exc)
                if copy_on_error:
                    detail = f"{detail} Transcript copied to clipboard."
                self._overlay.set_state("Error", detail)
            self._logger.exception("Text insertion failed")
            return False
        return True

    def _target_insert_window(self) -> int | None:
        signature = self._target_focus_signature
        if signature is not None:
            _foreground, focus_hwnd, caret_hwnd = signature
            if caret_hwnd:
                return caret_hwnd
            if focus_hwnd:
                return focus_hwnd
        return self._target_window_handle

    def _normalize_stream_text(self, text: str) -> str:
        tokens = str(text or "").strip().split()
        return " ".join(tokens).strip()

    def _stream_insertion_text(self, committed: str, tail: str) -> str:
        new_part = self._normalize_stream_text(tail)
        if not new_part:
            return ""
        if not self._normalize_stream_text(committed):
            return new_part
        if new_part[:1] in {".", ",", ";", ":", "!", "?", ")", "]", "}"}:
            return new_part
        return f" {new_part}"

    def _stream_join_text(self, committed: str, tail: str) -> str:
        base = self._normalize_stream_text(committed)
        insertion = self._stream_insertion_text(base, tail)
        combined = f"{base}{insertion}"
        return self._normalize_stream_text(combined)

    def _split_stream_words(self, text: str) -> list[str]:
        normalized = self._normalize_stream_text(text)
        if not normalized:
            return []
        return normalized.split(" ")

    def _common_prefix_len(self, left: list[str], right: list[str]) -> int:
        size = min(len(left), len(right))
        for idx in range(size):
            if left[idx].lower() != right[idx].lower():
                return idx
        return size

    def _suffix_prefix_overlap_len(self, left: list[str], right: list[str]) -> int:
        if not left or not right:
            return 0
        max_size = min(len(left), len(right))
        overlap = 0
        for size in range(1, max_size + 1):
            if [token.lower() for token in left[-size:]] == [
                token.lower() for token in right[:size]
            ]:
                overlap = size
        return overlap

    def _compute_stream_live_delta(
        self,
        committed: str,
        previous_partial: str,
        current_partial: str,
    ) -> tuple[str, str]:
        committed_words = self._split_stream_words(committed)
        previous_words = self._split_stream_words(previous_partial)
        current_words = self._split_stream_words(current_partial)
        if not current_words:
            return "", self._normalize_stream_text(committed)
        if not previous_words:
            # First partial is unstable; wait for one confirmation window.
            return "", self._normalize_stream_text(committed)

        stable_len = self._common_prefix_len(previous_words, current_words)
        guard = max(0, int(STREAMING_STABLE_WORD_GUARD))
        stable_commit_len = max(0, stable_len - guard)
        if stable_commit_len <= 0:
            return "", self._normalize_stream_text(committed)

        stable_commit_words = current_words[:stable_commit_len]
        committed_prefix_len = self._common_prefix_len(
            committed_words, stable_commit_words
        )
        stable_tail_words = stable_commit_words[committed_prefix_len:]
        committed_tail_words = committed_words[committed_prefix_len:]
        overlap_len = self._suffix_prefix_overlap_len(
            committed_tail_words, stable_tail_words
        )
        delta_words = stable_tail_words[overlap_len:]
        if not delta_words:
            return "", self._normalize_stream_text(committed)
        delta_text = " ".join(delta_words).strip()
        return delta_text, self._stream_join_text(committed, delta_text)

    def _best_stream_finalize_tail(self, committed: str, final_text: str) -> str:
        committed_words = self._split_stream_words(committed)
        best_tail = ""
        best_score = -1
        for candidate in (final_text, self._stream_last_partial_text):
            candidate_words = self._split_stream_words(candidate)
            if not candidate_words:
                continue
            prefix_len = self._common_prefix_len(committed_words, candidate_words)
            candidate_tail = candidate_words[prefix_len:]
            committed_tail = committed_words[prefix_len:]
            overlap_len = self._suffix_prefix_overlap_len(
                committed_tail, candidate_tail
            )
            delta_words = candidate_tail[overlap_len:]
            if delta_words:
                score = prefix_len + overlap_len
                # Prefer candidates that genuinely extend already committed text.
                if prefix_len < len(committed_words) and overlap_len == 0:
                    score -= 1
                if score > best_score:
                    best_score = score
                    best_tail = " ".join(delta_words).strip()
        return best_tail

    def copy_last_transcript_to_clipboard(self) -> bool:
        if not self._last_transcript.strip():
            return False
        QtGui.QGuiApplication.clipboard().setText(self._last_transcript)
        return True

    def retry_last_transcription(self) -> bool:
        if not self._last_failed_wav_bytes:
            self._overlay.set_state("Error", "No failed transcription to retry.")
            return False
        settings = self._last_failed_settings or replace(self._settings)
        self._transcription_cancel_requested = False
        self._overlay.set_state("Processing", "Retrying transcription...")
        self._executor.submit(
            self._transcribe_worker, self._last_failed_wav_bytes, settings
        )
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
    ) -> tuple[bool, str]:
        """Transcribe a file directly without live recording."""
        path = str(file_path or "").strip()
        if not path:
            return False, "No file path provided."
        if not os.path.isfile(path):
            return False, "Selected file does not exist."
        try:
            base_settings = settings_override or self._settings
            settings = replace(base_settings, mode="batch")
            transcriber = create_transcriber(settings, secret_store=self._secret_store)
            text = transcriber.transcribe_batch(path).strip()
            if text:
                self._history_store.add_entry(
                    TranscriptHistoryEntry.new(
                        text=text,
                        engine=settings.engine,
                        model=(
                            settings.groq_model
                            if settings.engine == "groq"
                            else settings.openai_model
                            if settings.engine == "openai"
                            else settings.model_size
                        ),
                        mode="import",
                    ),
                    settings.history_max_items,
                )
            return True, text or "No speech detected."
        except Exception as exc:
            self._logger.exception("Failed to transcribe imported file")
            return False, str(exc)

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
                )
                return
            capture = self._audio_capture
            self._audio_capture = None
            try:
                capture.stop()
            except Exception:
                pass
            self._active_batch_settings = None
            self._overlay.set_state("Done", "Recording canceled.")
            self._reset_streaming_state()
            return

        # Cancel in-progress transcription result delivery (best-effort).
        self._transcription_cancel_requested = True
        self._overlay.set_state("Done", "Transcription canceled.")

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
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

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

        script_path = Path(__file__).resolve().parents[2] / "scripts" / "download_model.py"
        if not script_path.is_file():
            raise RuntimeError(
                "Model is not cached and download helper is missing. "
                "Use scripts/download_model.py manually."
            )

        command = [sys.executable, str(script_path), "--model", model_name]
        if model_dir:
            command.extend(["--output-dir", model_dir])

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to start model download: {exc}") from exc

        self._set_preload_download_process(process)
        try:
            while True:
                if self._preload_cancel_requested:
                    self._terminate_preload_download_process()
                    raise RuntimeError("Model download canceled.")
                returncode = process.poll()
                if returncode is not None:
                    if returncode != 0:
                        raise RuntimeError(
                            f"Model download failed for '{model_name}'."
                        )
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
            self._settings.hotkey = FALLBACK_HOTKEY
            self._settings_store.save(self._settings)
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
            manager.unregister()
            return True

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
