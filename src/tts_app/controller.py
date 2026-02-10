from __future__ import annotations

import concurrent.futures
import logging
import threading
from dataclasses import replace

from PySide6 import QtCore, QtGui

from .app_paths import debug_audio_path
from .audio_capture import AudioCapture, AudioCaptureError
from .config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    DEFAULT_ENGINE,
    FALLBACK_HOTKEY,
    STREAMING_ABORT_ON_FOCUS_CHANGE,
    STREAMING_ABORT_BEEP_DURATION_MS,
    STREAMING_ABORT_BEEP_HZ,
    STREAMING_BEEP_ON_ABORT,
    STREAMING_FOCUS_POLL_MS,
    STREAMING_LIVE_INSERT_ENABLED,
    STREAMING_OVERLAY_MAX_CHARS,
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
    transcription_partial = QtCore.Signal(str)
    stream_abort_requested = QtCore.Signal(str, bool)

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
        self._target_focus_signature: tuple[int | None, int | None] | None = None
        self._last_transcript: str = ""
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

        self.transcription_ready.connect(self._on_transcription_ready)
        self.transcription_failed.connect(self._on_transcription_failed)
        self.transcription_partial.connect(self._on_transcription_partial)
        self.stream_abort_requested.connect(self._on_stream_abort_requested)

    @property
    def settings(self) -> AppSettings:
        return self._settings

    def initialize(self) -> None:
        self.reload_settings(re_register_hotkey=True)
        self.show_idle_status()

    def shutdown(self) -> None:
        self._hotkey_manager.unregister()
        self._focus_poll_timer.stop()
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
        self._reset_streaming_state()
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
        if self._settings.engine != DEFAULT_ENGINE:
            self._overlay.set_state(
                "Error",
                "Remote providers are planned for Phase 2.",
            )
            return

        self._target_window_handle = self._window_focus_helper.capture_target_window()
        self._target_focus_signature = self._capture_target_signature()
        if self._settings.mode == "streaming":
            self._start_streaming_recording()
            return
        self._start_batch_recording()

    def _start_batch_recording(self) -> None:
        capture = self._build_audio_capture()
        try:
            capture.start()
        except AudioCaptureError as exc:
            self._overlay.set_state("Error", str(exc))
            self._logger.exception("Audio capture failed to start")
            return

        self._active_session_mode = "batch"
        self._streaming_recording = False
        self._stream_last_partial_text = ""
        self._audio_capture = capture
        self._overlay.set_state("Listening", "Speak now. Press hotkey again to stop.")

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
            vad = EnergyVad(
                sample_rate=AUDIO_SAMPLE_RATE,
                energy_threshold=VAD_ENERGY_THRESHOLD,
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

        if self._settings.save_last_wav and wav_bytes:
            path = debug_audio_path()
            try:
                capture.save_wav(path, wav_bytes)
            except Exception:
                self._logger.exception("Failed to save debug wav")

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
            return

        settings_snapshot = replace(self._settings)
        self._overlay.set_state("Processing", "Transcribing audio...")
        self._executor.submit(self._transcribe_worker, wav_bytes, settings_snapshot)

    def _auto_stop_from_vad(self) -> None:
        QtCore.QTimer.singleShot(0, self.stop_recording)

    def _reset_streaming_state(self) -> None:
        self._focus_poll_timer.stop()
        self._stream_abort_requested = False
        self._stream_committed_text = ""
        self._stream_last_partial_text = ""
        self._active_session_mode = "batch"
        self._streaming_recording = False
        self._target_window_handle = None
        self._target_focus_signature = None

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
        if self._audio_capture is None:
            return
        if self._stream_abort_requested:
            return
        if self._streaming_recording and STREAMING_ABORT_ON_FOCUS_CHANGE:
            if not self._is_stream_target_active():
                self._request_stream_abort(
                    "Streaming aborted: target window focus changed.",
                    beep=STREAMING_BEEP_ON_ABORT,
                )
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
        )
        if self._transcriber_cache is None or self._transcriber_cache_key != cache_key:
            self._transcriber_cache = create_transcriber(settings)
            self._transcriber_cache_key = cache_key
        return self._transcriber_cache

    @QtCore.Slot(str)
    def _on_transcription_ready(self, text: str) -> None:
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
        self._reset_streaming_state()

    @QtCore.Slot(str)
    def _on_transcription_failed(self, error_text: str) -> None:
        self._focus_poll_timer.stop()
        self._streaming_recording = False
        self._active_stream_transcriber = None
        self._active_stream_settings = None
        self._reset_streaming_state()
        self._overlay.set_state("Error", error_text)

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
            text = text[-STREAMING_OVERLAY_MAX_CHARS :]
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

            winsound.Beep(STREAMING_ABORT_BEEP_HZ, STREAMING_ABORT_BEEP_DURATION_MS)
            return
        except Exception:
            pass
        try:
            import winsound  # type: ignore

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

        current_foreground, current_focus = current_signature
        if target_signature is not None:
            target_foreground, target_focus = target_signature
            if (
                target_focus is not None
                and current_focus is not None
                and current_focus != target_focus
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

    def _capture_target_signature(self) -> tuple[int | None, int | None] | None:
        getter = getattr(self._window_focus_helper, "capture_target_signature", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                self._logger.exception("Failed to capture target focus signature")
                return None
        window = self._target_window_handle
        return (window, window) if window else None

    def _current_focus_signature(self) -> tuple[int | None, int | None] | None:
        getter = getattr(self._window_focus_helper, "get_focus_signature", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                self._logger.exception("Failed to read focus signature")
                return None
        foreground = self._current_foreground_window()
        return (foreground, foreground) if foreground else None

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
                self._window_focus_helper.restore_target_window(self._target_window_handle)
            except Exception:
                self._logger.exception("Failed to restore target window focus")
        try:
            self._text_inserter.insert_text_with_options(
                text,
                target_hwnd=self._target_window_handle,
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

    def _stream_tail(self, committed: str, latest: str) -> str:
        committed_text = self._normalize_stream_text(committed)
        latest_text = self._normalize_stream_text(latest)
        if not latest_text:
            return ""
        if not committed_text:
            return latest_text
        if latest_text.startswith(committed_text):
            return latest_text[len(committed_text) :].strip()
        committed_words = committed_text.split(" ")
        latest_words = latest_text.split(" ")
        max_overlap = min(len(committed_words), len(latest_words))
        lower_committed = [word.lower() for word in committed_words]
        lower_latest = [word.lower() for word in latest_words]

        overlap = 0
        for size in range(max_overlap, 0, -1):
            if lower_committed[-size:] == lower_latest[:size]:
                overlap = size
                break

        if overlap <= 0:
            return ""
        tail_words = latest_words[overlap:]
        return " ".join(tail_words).strip()

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
        stable_words = current_words[:stable_len]
        if stable_words[: len(committed_words)] == committed_words and len(stable_words) >= len(
            committed_words
        ):
            delta_words = stable_words[len(committed_words) :]
            delta = " ".join(delta_words).strip()
            next_committed = " ".join(stable_words).strip()
            return delta, self._normalize_stream_text(next_committed)

        fallback_delta = self._stream_tail(committed, current_partial)
        if not fallback_delta:
            return "", self._normalize_stream_text(committed)
        return fallback_delta, self._stream_join_text(committed, fallback_delta)

    def _best_stream_finalize_tail(self, committed: str, final_text: str) -> str:
        tail = self._stream_tail(committed, final_text)
        if tail:
            return tail
        return self._stream_tail(committed, self._stream_last_partial_text)

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
