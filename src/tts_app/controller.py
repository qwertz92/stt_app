from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import replace

from PySide6 import QtCore, QtGui

from .app_paths import debug_audio_path
from .audio_capture import AudioCapture, AudioCaptureError
from .config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    DEFAULT_ENGINE,
    DEFAULT_MODE,
    FALLBACK_HOTKEY,
    VAD_ENERGY_THRESHOLD,
    VAD_MAX_SILENCE_MS,
    VAD_MIN_SPEECH_MS,
)
from .hotkey import HotkeyManager, HotkeyRegistrationError
from .overlay_ui import OverlayUI
from .settings_store import AppSettings, SettingsStore
from .text_inserter import TextInserter, TextInsertionError
from .transcriber import create_transcriber
from .transcriber.base import TranscriptionError
from .vad import EnergyVad
from .window_focus import Win32WindowFocusHelper, WindowFocusHelper


class DictationController(QtCore.QObject):
    transcription_ready = QtCore.Signal(str)
    transcription_failed = QtCore.Signal(str)

    def __init__(
        self,
        settings_store: SettingsStore,
        hotkey_manager: HotkeyManager,
        overlay: OverlayUI,
        text_inserter: TextInserter,
        logger: logging.Logger,
        window_focus_helper: WindowFocusHelper | None = None,
    ) -> None:
        super().__init__()
        self._settings_store = settings_store
        self._hotkey_manager = hotkey_manager
        self._overlay = overlay
        self._text_inserter = text_inserter
        self._logger = logger
        self._window_focus_helper = window_focus_helper or Win32WindowFocusHelper()

        self._settings: AppSettings = self._settings_store.load()
        self._audio_capture: AudioCapture | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._transcriber_cache_key = None
        self._transcriber_cache = None
        self._hotkey_registration_ok = False
        self._hotkey_notice: str | None = None
        self._target_window_handle: int | None = None
        self._last_transcript: str = ""

        self.transcription_ready.connect(self._on_transcription_ready)
        self.transcription_failed.connect(self._on_transcription_failed)

    @property
    def settings(self) -> AppSettings:
        return self._settings

    def initialize(self) -> None:
        self.reload_settings(re_register_hotkey=True)
        self.show_idle_status()

    def shutdown(self) -> None:
        self._hotkey_manager.unregister()
        if self._audio_capture is not None:
            try:
                self._audio_capture.stop()
            except Exception:
                pass
            self._audio_capture = None
        self._executor.shutdown(wait=False, cancel_futures=False)

    def reload_settings(self, re_register_hotkey: bool = True) -> None:
        self._settings = self._settings_store.load()
        if re_register_hotkey:
            self._hotkey_registration_ok = self._register_hotkey_with_fallback()
        else:
            self._hotkey_registration_ok = True
            self._hotkey_notice = None

    def show_idle_status(self) -> None:
        if not self._hotkey_registration_ok:
            self._overlay.set_state(
                "Error",
                self._hotkey_notice or "Hotkey registration failed.",
            )
            return

        detail = f"Hotkey: {self._settings.hotkey}"
        if self._hotkey_notice:
            detail = f"{detail} ({self._hotkey_notice})"
        self._overlay.set_state("Idle", detail)

    @QtCore.Slot()
    def toggle_recording(self) -> None:
        if self._audio_capture is None:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self) -> None:
        if self._settings.mode != DEFAULT_MODE:
            self._overlay.set_state("Error", "Streaming mode is planned for Phase 2.")
            return

        if self._settings.engine != DEFAULT_ENGINE:
            self._overlay.set_state(
                "Error",
                "Remote providers are planned for Phase 2.",
            )
            return

        vad = None
        self._target_window_handle = self._window_focus_helper.capture_target_window()
        if self._settings.vad_enabled:
            vad = EnergyVad(
                sample_rate=AUDIO_SAMPLE_RATE,
                energy_threshold=VAD_ENERGY_THRESHOLD,
                min_speech_ms=VAD_MIN_SPEECH_MS,
                max_silence_ms=VAD_MAX_SILENCE_MS,
            )
        capture = AudioCapture(
            sample_rate=AUDIO_SAMPLE_RATE,
            channels=AUDIO_CHANNELS,
            vad=vad,
            auto_stop_callback=self._auto_stop_from_vad,
            logger=self._logger,
        )

        try:
            capture.start()
        except AudioCaptureError as exc:
            self._overlay.set_state("Error", str(exc))
            self._logger.exception("Audio capture failed to start")
            return

        self._audio_capture = capture
        self._overlay.set_state("Listening", "Speak now. Press hotkey again to stop.")

    def stop_recording(self) -> None:
        capture = self._audio_capture
        if capture is None:
            return

        self._audio_capture = None
        wav_bytes = capture.stop()

        if not wav_bytes:
            self._overlay.set_state("Error", "No audio captured.")
            return

        if self._settings.save_last_wav:
            path = debug_audio_path()
            try:
                capture.save_wav(path, wav_bytes)
            except Exception:
                self._logger.exception("Failed to save debug wav")

        settings_snapshot = replace(self._settings)
        self._overlay.set_state("Processing", "Transcribing audio...")

        self._executor.submit(self._transcribe_worker, wav_bytes, settings_snapshot)

    def _auto_stop_from_vad(self) -> None:
        QtCore.QTimer.singleShot(0, self.stop_recording)

    def _transcribe_worker(self, wav_bytes: bytes, settings: AppSettings) -> None:
        try:
            transcriber = self._get_or_create_transcriber(settings)
            text = transcriber.transcribe_batch(wav_bytes)
            self.transcription_ready.emit(text)
        except NotImplementedError as exc:
            self.transcription_failed.emit(str(exc))
        except TranscriptionError as exc:
            self.transcription_failed.emit(str(exc))
        except Exception as exc:
            self._logger.exception("Unexpected transcription failure")
            self.transcription_failed.emit(f"Unexpected transcription error: {exc}")

    def _get_or_create_transcriber(self, settings: AppSettings):
        cache_key = (
            settings.engine,
            settings.model_size,
            settings.language_mode,
            settings.vad_enabled,
        )
        if self._transcriber_cache is None or self._transcriber_cache_key != cache_key:
            self._transcriber_cache = create_transcriber(settings)
            self._transcriber_cache_key = cache_key
        return self._transcriber_cache

    @QtCore.Slot(str)
    def _on_transcription_ready(self, text: str) -> None:
        self._last_transcript = text

        if not text.strip():
            self._overlay.set_state("Done", "No speech detected.")
            self._target_window_handle = None
            return

        try:
            self._window_focus_helper.restore_target_window(self._target_window_handle)
        except Exception:
            self._logger.exception("Failed to restore target window focus")
        try:
            self._text_inserter.insert_text(
                text,
                target_hwnd=self._target_window_handle,
            )
        except TextInsertionError as exc:
            QtGui.QGuiApplication.clipboard().setText(text)
            self._overlay.set_state(
                "Error",
                f"{exc} Transcript copied to clipboard.",
            )
            self._logger.exception("Text insertion failed")
            self._target_window_handle = None
            return

        self._overlay.set_state("Done", text)
        self._target_window_handle = None

    @QtCore.Slot(str)
    def _on_transcription_failed(self, error_text: str) -> None:
        self._overlay.set_state("Error", error_text)

    def copy_last_transcript_to_clipboard(self) -> bool:
        if not self._last_transcript.strip():
            return False
        QtGui.QGuiApplication.clipboard().setText(self._last_transcript)
        return True

    def _register_hotkey_with_fallback(self) -> bool:
        preferred = self._settings.hotkey
        try:
            self._hotkey_manager.register(preferred)
            self._hotkey_notice = None
            return True
        except (HotkeyRegistrationError, ValueError):
            self._logger.exception("Failed to register preferred hotkey: %s", preferred)

        if preferred == FALLBACK_HOTKEY:
            self._hotkey_notice = (
                f"Hotkey registration failed ({preferred}). Choose a different hotkey in Settings."
            )
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
