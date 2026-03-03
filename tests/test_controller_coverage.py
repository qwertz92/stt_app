"""Additional controller coverage tests — shutdown, start_recording edge cases,
transcription_worker error branches, streaming abort, focus poll."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from PySide6 import QtWidgets

from tts_app.config import FALLBACK_HOTKEY
from tts_app.controller import DictationController
from tts_app.settings_store import AppSettings
from tts_app.text_inserter import TextInsertionError
from tts_app.transcriber.base import TranscriptionError

from conftest import (
    FakeCapture,
    FakeCaptureFails,
    FakeHotkeyManager,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    ImmediateExecutor,
    make_controller as _make_controller,
)


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


class _RunningFuture:
    def done(self):
        return False


def test_start_recording_uses_cached_fallback_while_preloading(monkeypatch):
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        mode="batch",
        model_size="large-v3-turbo",
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("tts_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller._preload_future = _RunningFuture()
    fallback_settings = AppSettings(
        hotkey=settings.hotkey,
        engine=settings.engine,
        mode=settings.mode,
        model_size="small",
    )
    controller._resolve_preload_fallback_settings = (  # type: ignore[method-assign]
        lambda: fallback_settings
    )
    controller._preload_progress_detail = (  # type: ignore[method-assign]
        lambda include_fallback_hint=False: "Downloading 'large-v3-turbo' 20%"
    )

    controller.start_recording()

    assert controller._audio_capture is not None
    assert controller._active_batch_settings is not None
    assert controller._active_batch_settings.model_size == "small"
    assert overlay.states[-1][0] == "Listening"
    assert "fallback 'small'" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_start_recording_preload_without_fallback_shows_error():
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        mode="batch",
        model_size="large-v3-turbo",
    )
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller._preload_future = _RunningFuture()
    controller._resolve_preload_fallback_settings = lambda: None  # type: ignore[method-assign]
    controller._preload_progress_detail = (  # type: ignore[method-assign]
        lambda include_fallback_hint=False: "Downloading 'large-v3-turbo' 20%"
    )

    controller.start_recording()

    assert overlay.states[-1][0] == "Error"
    assert "No cached fallback model available yet" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_start_recording_remote_not_blocked_by_stale_local_preload(monkeypatch):
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="groq",
        mode="batch",
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("tts_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller._preload_future = _RunningFuture()

    controller.start_recording()

    assert controller._audio_capture is not None
    assert overlay.states[-1][0] == "Listening"
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
    controller._transcriber_cache_key = (
        "openai",
        "small",
        "auto",
        False,
        False,
        "",
        "whisper-large-v3-turbo",
        "gpt-4o-mini-transcribe",
    )

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
    controller._transcriber_cache_key = (
        "local",
        "small",
        "auto",
        False,
        False,
        "",
        "whisper-large-v3-turbo",
        "gpt-4o-mini-transcribe",
    )

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
        cancel_hotkey_manager=FakeHotkeyManager(),
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
# Retry / cancel actions
# ---------------------------------------------------------------------------


def test_retry_last_transcription_returns_false_without_failed_audio():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)

    ok = controller.retry_last_transcription()

    assert ok is False
    assert overlay.states[-1][0] == "Error"
    assert "No failed transcription" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_retry_last_transcription_resubmits_failed_audio():
    controller, app = _make_controller()
    captured = []
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, model_size="small")
    controller._last_failed_wav_bytes = b"wav-bytes"
    controller._last_failed_settings = settings
    controller._executor = ImmediateExecutor()

    def fake_worker(wav_bytes, snapshot):
        captured.append((wav_bytes, snapshot.model_size))

    controller._transcribe_worker = fake_worker  # type: ignore[method-assign]

    ok = controller.retry_last_transcription()

    assert ok is True
    assert captured == [(b"wav-bytes", "small")]
    controller.shutdown()
    _ = app


def test_cancel_current_action_stops_active_batch_recording():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    capture = FakeCapture()
    controller._audio_capture = capture
    controller._streaming_recording = False

    controller.cancel_current_action()

    assert capture.stopped is True
    assert controller._audio_capture is None
    assert overlay.states[-1][0] == "Done"
    assert "canceled" in overlay.states[-1][1].lower()
    controller.shutdown()
    _ = app


def test_cancel_current_action_marks_inflight_transcription_as_canceled():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._active_session_mode = "streaming"
    controller._streaming_recording = True

    controller.cancel_current_action()
    controller._on_transcription_failed("transcriber failed")

    assert controller._transcription_cancel_requested is False
    assert controller._active_session_mode == "batch"
    assert controller._streaming_recording is False
    assert overlay.states[-1][0] == "Done"
    assert "canceled" in overlay.states[-1][1].lower()
    controller.shutdown()
    _ = app


def test_cancel_current_action_cancels_running_preload():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._preload_future = _RunningFuture()
    terminated = []
    controller._terminate_preload_download_process = (  # type: ignore[method-assign]
        lambda: terminated.append(True)
    )

    controller.cancel_current_action()

    assert controller._preload_cancel_requested is True
    assert terminated == [True]
    assert overlay.states[-1][0] == "Processing"
    assert "Canceling model download" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_download_model_for_preload_skips_when_cached(monkeypatch):
    controller, app = _make_controller()
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, model_size="small")
    monkeypatch.setattr(
        "tts_app.transcriber.local_faster_whisper.find_cached_models",
        lambda _model_dir="": ["small"],
    )

    started = []
    monkeypatch.setattr(
        "tts_app.controller.subprocess.Popen",
        lambda *args, **kwargs: started.append(True),
    )

    controller._download_model_for_preload(settings)

    assert started == []
    controller.shutdown()
    _ = app


def test_download_model_for_preload_can_be_canceled():
    controller, app = _make_controller()
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, model_size="small")
    controller._preload_cancel_requested = True

    try:
        controller._download_model_for_preload(settings)
        raised = False
    except RuntimeError as exc:
        raised = "canceled" in str(exc).lower()

    assert raised is True
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# Cancel hotkey registration
# ---------------------------------------------------------------------------


def test_register_cancel_hotkey_success():
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, cancel_hotkey="Ctrl+Alt+F12")
    manager = FakeHotkeyManager()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        cancel_hotkey_manager=manager,
    )

    ok = controller._register_cancel_hotkey()

    assert ok is True
    assert manager.calls[-1] == "Ctrl+Alt+F12"
    controller.shutdown()
    _ = app


def test_register_cancel_hotkey_failure_sets_notice():
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, cancel_hotkey="Ctrl+Shift+X")
    manager = FakeHotkeyManager()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        cancel_hotkey_manager=manager,
    )

    ok = controller._register_cancel_hotkey()

    assert ok is False
    assert "Cancel hotkey registration failed" in (controller._cancel_hotkey_notice or "")
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
