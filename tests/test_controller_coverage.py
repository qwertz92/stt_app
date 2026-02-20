"""Additional controller coverage tests — shutdown, start_recording edge cases,
transcription_worker error branches, streaming abort, focus poll."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from PySide6 import QtCore, QtGui, QtWidgets

from tts_app.config import DEFAULT_HOTKEY, FALLBACK_HOTKEY
from tts_app.controller import DictationController
from tts_app.settings_store import AppSettings
from tts_app.text_inserter import TextInsertionError
from tts_app.transcriber.base import TranscriptionError


# -- Re-use fakes from test_controller.py ------------------------------------


class FakeSettingsStore:
    def __init__(self, settings):
        self._settings = settings
        self.saved = None

    def load(self):
        return self._settings

    def save(self, settings):
        self.saved = settings


class FakeHotkeyManager:
    def __init__(self):
        self.calls = []

    def register(self, hotkey):
        self.calls.append(hotkey)
        if hotkey != FALLBACK_HOTKEY:
            raise ValueError("blocked")

    def unregister(self):
        pass


class FakeOverlay:
    def __init__(self):
        self.states = []

    def set_state(self, state, detail=""):
        self.states.append((state, detail))


class FakeTextInserter:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.calls = []

    def insert_text(self, text, target_hwnd=None):
        self.calls.append((text, target_hwnd))
        if self.should_fail:
            raise TextInsertionError("failed insert")
        return True

    def insert_text_with_options(self, text, target_hwnd=None, paste_mode="auto"):
        self.calls.append((text, target_hwnd, paste_mode))
        if self.should_fail:
            raise TextInsertionError("failed insert")
        return True


class FakeWindowFocusHelper:
    def __init__(self):
        self.captured = 987
        self.captured_focus = 654
        self.captured_caret = 321
        self.current = 987
        self.current_focus = 654
        self.current_caret = 321
        self.restore_calls = []

    def capture_target_window(self):
        return self.captured

    def capture_target_signature(self):
        focus = self.captured_focus or self.captured
        caret = self.captured_caret or focus
        return (self.captured, focus, caret)

    def get_foreground_window(self):
        return self.current

    def get_focus_signature(self):
        focus = self.current_focus or self.current
        caret = self.current_caret or focus
        return (self.current, focus, caret)

    def restore_target_window(self, hwnd):
        self.restore_calls.append(hwnd)
        return True


class ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return None

    def shutdown(self, wait=False, cancel_futures=False):
        pass


class FakeStreamingTranscriber:
    def __init__(self, *, stop_raises=None, push_raises=None):
        self.started = False
        self.stopped = False
        self.aborted = False
        self.chunks = []
        self.on_partial = None
        self._stop_raises = stop_raises
        self._push_raises = push_raises

    def transcribe_batch(self, audio_source):
        return "batch"

    def start_stream(self, on_partial=None):
        self.started = True
        self.on_partial = on_partial

    def push_audio_chunk(self, chunk: bytes):
        if self._push_raises:
            raise self._push_raises
        self.chunks.append(chunk)
        if self.on_partial is not None:
            self.on_partial("stream")

    def stop_stream(self):
        self.stopped = True
        if self._stop_raises:
            raise self._stop_raises
        return "stream final"

    def abort_stream(self):
        self.aborted = True


class FakeCapture:
    instances = []

    def __init__(self, *args, **kwargs):
        self.chunk_callback = kwargs.get("chunk_callback")
        self.started = False
        self.stopped = False
        self._wav_bytes = b"RIFF"
        FakeCapture.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return self._wav_bytes

    def save_wav(self, path, wav_bytes):
        self.last_saved_path = path
        self.last_saved_bytes = wav_bytes


class FakeCaptureFails(FakeCapture):
    def start(self):
        from tts_app.audio_capture import AudioCaptureError
        raise AudioCaptureError("no mic")


def _make_controller(**kwargs):
    """Helper to create a DictationController with sensible defaults."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    defaults = dict(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)),
        hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.coverage"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    defaults.update(kwargs)
    return DictationController(**defaults), app


# ---------------------------------------------------------------------------
# Shutdown tests
# ---------------------------------------------------------------------------


def test_shutdown_stops_active_audio_capture():
    controller, app = _make_controller()
    fake_capture = FakeCapture()
    controller._audio_capture = fake_capture
    controller.shutdown()
    assert fake_capture.stopped is True
    assert controller._audio_capture is None
    _ = app


def test_shutdown_stops_active_stream_transcriber():
    controller, app = _make_controller()
    transcriber = FakeStreamingTranscriber()
    controller._active_stream_transcriber = transcriber
    controller._active_stream_settings = AppSettings()
    controller.shutdown()
    assert transcriber.stopped is True
    assert controller._active_stream_transcriber is None
    _ = app


def test_shutdown_cancels_preload_future():
    controller, app = _make_controller()
    mock_future = MagicMock()
    controller._preload_future = mock_future
    controller.shutdown()
    mock_future.cancel.assert_called_once()
    _ = app


# ---------------------------------------------------------------------------
# start_recording edge cases
# ---------------------------------------------------------------------------


def test_start_recording_rejects_streaming_for_remote_engine():
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="groq", mode="streaming")
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller.start_recording()
    assert overlay.states[-1][0] == "Error"
    assert "Streaming" in overlay.states[-1][1] or "streaming" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_start_batch_recording_audio_capture_error(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="batch")
    overlay = FakeOverlay()
    FakeCapture.instances = []
    monkeypatch.setattr("tts_app.controller.AudioCapture", FakeCaptureFails)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller.start_recording()
    assert overlay.states[-1][0] == "Error"
    assert "no mic" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_start_streaming_transcriber_error_shows_overlay_error(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()

    def fail_transcriber(_s, **kw):
        t = FakeStreamingTranscriber()
        original_start = t.start_stream

        def broken_start(on_partial=None):
            raise TranscriptionError("model not loaded")

        t.start_stream = broken_start
        return t

    monkeypatch.setattr("tts_app.controller.create_transcriber", fail_transcriber)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller.start_recording()
    assert overlay.states[-1][0] == "Error"
    assert "model not loaded" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_start_streaming_audio_capture_error_stops_transcriber(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    transcriber = FakeStreamingTranscriber()

    monkeypatch.setattr("tts_app.controller.AudioCapture", FakeCaptureFails)
    monkeypatch.setattr("tts_app.controller.create_transcriber", lambda _s, **kw: transcriber)

    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller.start_recording()
    assert transcriber.started is True
    assert transcriber.stopped is True  # cleaned up after capture failure
    assert overlay.states[-1][0] == "Error"
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# stop_recording edge cases
# ---------------------------------------------------------------------------


def test_stop_recording_no_audio_shows_error(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="batch")
    overlay = FakeOverlay()
    empty_capture = FakeCapture()
    empty_capture._wav_bytes = b""
    FakeCapture.instances = []
    monkeypatch.setattr("tts_app.controller.AudioCapture", FakeCapture)

    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller._audio_capture = empty_capture
    controller._streaming_recording = False
    controller.stop_recording()
    assert overlay.states[-1][0] == "Error"
    assert "No audio captured" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_stop_recording_streaming_with_abort_requested(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    transcriber = FakeStreamingTranscriber()
    capture = FakeCapture()

    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller._audio_capture = capture
    controller._streaming_recording = True
    controller._stream_abort_requested = True
    controller._active_stream_transcriber = transcriber
    controller.stop_recording()
    # abort path taken — should show Error, not finalize
    assert overlay.states[-1][0] == "Error"
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# _transcribe_worker error branches
# ---------------------------------------------------------------------------


def test_transcribe_worker_emits_not_implemented_error():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._executor = ImmediateExecutor()

    class PlaceholderTranscriber:
        def transcribe_batch(self, wav):
            raise NotImplementedError("OpenAI provider not implemented yet")

    controller._transcriber_cache = PlaceholderTranscriber()
    controller._transcriber_cache_key = ("openai", "small", "auto", True, False, "")

    settings_snapshot = AppSettings(engine="openai", hotkey=FALLBACK_HOTKEY)
    controller._transcribe_worker(b"audio", settings_snapshot)

    assert overlay.states[-1][0] == "Error"
    assert "not implemented" in overlay.states[-1][1].lower()
    controller.shutdown()
    _ = app


def test_transcribe_worker_emits_unexpected_error():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._executor = ImmediateExecutor()

    class BrokenTranscriber:
        def transcribe_batch(self, wav):
            raise RuntimeError("something went wrong")

    controller._transcriber_cache = BrokenTranscriber()
    controller._transcriber_cache_key = ("local", "small", "auto", True, False, "")

    settings_snapshot = AppSettings(engine="local", hotkey=FALLBACK_HOTKEY)
    controller._transcribe_worker(b"audio", settings_snapshot)

    assert overlay.states[-1][0] == "Error"
    assert "Unexpected" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# _finalize_stream_worker error branches
# ---------------------------------------------------------------------------


def test_finalize_stream_worker_no_transcriber_emits_error():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._active_stream_transcriber = None
    controller._finalize_stream_worker()
    assert overlay.states[-1][0] == "Error"
    assert "not initialized" in overlay.states[-1][1].lower()
    controller.shutdown()
    _ = app


def test_finalize_stream_worker_exception_emits_error():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._active_stream_transcriber = FakeStreamingTranscriber(
        stop_raises=RuntimeError("boom")
    )
    controller._finalize_stream_worker()
    assert overlay.states[-1][0] == "Error"
    assert "Unexpected" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# _on_stream_audio_chunk edge cases
# ---------------------------------------------------------------------------


def test_on_stream_audio_chunk_skips_when_no_capture():
    controller, app = _make_controller()
    controller._audio_capture = None
    controller._active_stream_transcriber = FakeStreamingTranscriber()
    # Should not raise
    controller._on_stream_audio_chunk(b"data")
    controller.shutdown()
    _ = app


def test_on_stream_audio_chunk_skips_when_abort_requested():
    controller, app = _make_controller()
    controller._audio_capture = FakeCapture()
    transcriber = FakeStreamingTranscriber()
    controller._active_stream_transcriber = transcriber
    controller._stream_abort_requested = True
    controller._on_stream_audio_chunk(b"data")
    assert transcriber.chunks == []
    controller.shutdown()
    _ = app


def test_on_stream_audio_chunk_reports_push_error_once():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._audio_capture = FakeCapture()
    controller._active_stream_transcriber = FakeStreamingTranscriber(
        push_raises=RuntimeError("push failed")
    )
    controller._stream_chunk_error_reported = False

    controller._on_stream_audio_chunk(b"data")
    error_count_1 = sum(1 for s, _ in overlay.states if s == "Error")
    # Second push should NOT emit another error
    controller._on_stream_audio_chunk(b"data2")
    error_count_2 = sum(1 for s, _ in overlay.states if s == "Error")
    assert error_count_2 == error_count_1  # Only reported once

    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# _on_transcription_ready streaming: no speech
# ---------------------------------------------------------------------------


def test_on_transcription_ready_streaming_no_speech():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._active_session_mode = "streaming"
    controller._stream_committed_text = ""
    controller._target_window_handle = 555
    controller._on_transcription_ready("   ")
    assert overlay.states[-1][0] == "Done"
    assert "No speech" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# _abort_streaming_session: finalize_stream=True and no abort_stream
# ---------------------------------------------------------------------------


def test_abort_streaming_session_with_finalize(monkeypatch):
    controller, app = _make_controller()
    monkeypatch.setattr(controller, "_play_abort_beep", lambda: None)
    transcriber = FakeStreamingTranscriber()
    controller._active_stream_transcriber = transcriber
    controller._audio_capture = FakeCapture()
    controller._streaming_recording = True

    controller._abort_streaming_session("test reason", beep=False, finalize_stream=True)
    assert transcriber.stopped is True
    assert transcriber.aborted is False
    controller.shutdown()
    _ = app


def test_abort_streaming_session_without_abort_stream_method(monkeypatch):
    """If transcriber doesn't have abort_stream, falls back to stop_stream."""
    controller, app = _make_controller()
    monkeypatch.setattr(controller, "_play_abort_beep", lambda: None)

    class NoAbortTranscriber:
        def __init__(self):
            self.stopped = False

        def stop_stream(self):
            self.stopped = True
            return "final"

    transcriber = NoAbortTranscriber()
    controller._active_stream_transcriber = transcriber
    controller._audio_capture = FakeCapture()
    controller._streaming_recording = True

    controller._abort_streaming_session("test", beep=False, finalize_stream=False)
    assert transcriber.stopped is True
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# _play_abort_beep (Linux fallback to Qt beep)
# ---------------------------------------------------------------------------


def test_play_abort_beep_does_not_raise_on_linux():
    """On Linux, winsound is unavailable. _play_abort_beep should not raise."""
    controller, app = _make_controller()
    # Should complete without error (falls back to Qt beep or silently passes)
    controller._play_abort_beep()
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# _register_hotkey_with_fallback when preferred == fallback
# ---------------------------------------------------------------------------


def test_register_hotkey_fails_when_preferred_equals_fallback():
    settings = AppSettings(hotkey=FALLBACK_HOTKEY)

    class AlwaysFailHotkey:
        def register(self, hotkey):
            raise ValueError("blocked")

        def unregister(self):
            pass

    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=AlwaysFailHotkey(),
        overlay=overlay,
    )
    result = controller._register_hotkey_with_fallback()
    assert result is False
    assert "Choose a different hotkey" in (controller._hotkey_notice or "")
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# toggle_recording delegates correctly
# ---------------------------------------------------------------------------


def test_toggle_starts_then_stops(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="batch")
    overlay = FakeOverlay()
    FakeCapture.instances = []
    monkeypatch.setattr("tts_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller._executor = ImmediateExecutor()

    # First toggle: should start recording
    controller.toggle_recording()
    assert controller._audio_capture is not None

    # Second toggle: should stop recording
    controller.toggle_recording()
    assert controller._audio_capture is None
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# _on_transcription_failed
# ---------------------------------------------------------------------------


def test_on_transcription_failed_shows_error():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._on_transcription_failed("Something went wrong")
    assert overlay.states[-1][0] == "Error"
    assert "Something went wrong" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# _on_stream_focus_poll early-return paths
# ---------------------------------------------------------------------------


def test_focus_poll_exits_early_when_not_streaming():
    controller, app = _make_controller()
    controller._streaming_recording = False
    # Should not raise
    controller._on_stream_focus_poll()
    controller.shutdown()
    _ = app


def test_focus_poll_exits_early_when_already_aborted():
    controller, app = _make_controller()
    controller._streaming_recording = True
    controller._stream_abort_requested = True
    # Should not trigger another abort
    controller._on_stream_focus_poll()
    controller.shutdown()
    _ = app
