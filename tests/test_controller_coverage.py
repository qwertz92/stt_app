"""Additional controller coverage tests — shutdown, start_recording edge cases,
transcription_worker error branches, streaming abort, focus poll."""

from __future__ import annotations

import logging
import os
import threading
from unittest.mock import MagicMock

from stt_app.config import FALLBACK_HOTKEY
from stt_app.last_recording_store import LastRecordingStore
from stt_app.settings_store import AppSettings
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcript_history import TranscriptHistoryStore

from conftest import (
    FakeCapture,
    FakeCaptureFails,
    FakeHotkeyManager,
    FakeLastRecordingStore,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
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


def test_recording_prune_leaves_unmanaged_wav_files_untouched(tmp_path):
    controller, app = _make_controller()
    managed_old = tmp_path / "recording_20260711_100000_000001.wav"
    managed_new = tmp_path / "recording_20260711_100001_000002.wav"
    unrelated = tmp_path / "family-interview.wav"
    for path in (managed_old, managed_new, unrelated):
        path.write_bytes(b"audio")
    os.utime(managed_old, (1, 1))
    os.utime(unrelated, (2, 2))
    os.utime(managed_new, (3, 3))

    controller._prune_recordings(str(tmp_path), keep_count=1)

    assert unrelated.exists()
    assert not managed_old.exists()
    assert managed_new.exists()
    controller.shutdown()
    _ = app


def test_shutdown_defers_cached_runtime_close_until_worker_exits(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class BlockingTranscriber:
        def __init__(self):
            self.closed = False

        def set_progress_callback(self, _callback):
            return None

        def set_cancel_check(self, _callback):
            return None

        def transcribe_batch(self, _wav):
            entered.set()
            assert release.wait(timeout=2.0)
            return "late result"

        def close(self):
            self.closed = True

    transcriber = BlockingTranscriber()
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: transcriber,
    )
    controller, app = _make_controller()
    controller._submit_batch_transcription(b"RIFF", controller.settings)
    job = next(iter(controller._jobs.values()))
    assert entered.wait(timeout=2.0)

    states_before_shutdown = list(controller._overlay.states)
    controller.shutdown()

    assert transcriber.closed is False
    release.set()
    job.future.result(timeout=2.0)
    assert transcriber.closed is True
    # The worker finishes after shutdown but does not emit a terminal UI update.
    assert controller._overlay.states == states_before_shutdown
    _ = app


def test_overlapping_runtime_uses_isolated_instance_without_closing_shared(
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()

    class BlockingTranscriber:
        def __init__(self, *, blocking: bool):
            self.blocking = blocking
            self.closed = False

        def set_progress_callback(self, _callback):
            return None

        def set_cancel_check(self, _callback):
            return None

        def transcribe_batch(self, _wav):
            if self.blocking:
                entered.set()
                assert release.wait(timeout=2.0)
            return "done"

        def close(self):
            self.closed = True

    created = []

    def create(_settings, **_kwargs):
        transcriber = BlockingTranscriber(blocking=not created)
        created.append(transcriber)
        return transcriber

    monkeypatch.setattr("stt_app.controller.create_transcriber", create)
    controller, app = _make_controller()
    controller._submit_batch_transcription(b"RIFF", controller.settings)
    job = next(iter(controller._jobs.values()))
    assert entered.wait(timeout=2.0)

    controller.cancel_current_action()
    assert controller._active_request_token is None
    controller.reload_settings(re_register_hotkey=False)
    isolated_lease = controller._acquire_transcriber_runtime(controller.settings)

    assert len(created) == 2
    assert isolated_lease.transcriber is created[1]
    assert created[0].closed is False
    isolated_lease.release()
    assert created[1].closed is True
    assert created[0].closed is False

    release.set()
    job.future.result(timeout=2.0)
    assert created[0].closed is True
    controller.shutdown()
    _ = app


def test_preload_runtime_waits_off_thread_for_shared_cache(monkeypatch):
    class CachedTranscriber:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    transcriber = CachedTranscriber()
    create_calls = []

    def create(_settings, **_kwargs):
        create_calls.append(True)
        return transcriber

    monkeypatch.setattr("stt_app.controller.create_transcriber", create)
    controller, app = _make_controller()
    shared_lease = controller._acquire_transcriber_runtime(controller.settings)
    attempting = threading.Event()
    acquired = threading.Event()
    finished = threading.Event()

    def acquire_preload_lease():
        attempting.set()
        preload_lease = controller._acquire_transcriber_runtime(
            controller.settings,
            allow_isolated=False,
        )
        acquired.set()
        preload_lease.release()
        finished.set()

    thread = threading.Thread(target=acquire_preload_lease)
    thread.start()
    assert attempting.wait(timeout=2.0)
    assert acquired.is_set() is False

    shared_lease.release()

    assert acquired.wait(timeout=2.0)
    assert finished.wait(timeout=2.0)
    thread.join(timeout=2.0)
    assert create_calls == [True]
    assert transcriber.closed is False
    controller.shutdown()
    assert transcriber.closed is True
    _ = app


def test_worker_terminal_signal_follows_cleanup_and_deferred_close(monkeypatch):
    cleanup_state = {
        "cancel": object(),
        "progress": object(),
        "closed": False,
    }

    class CleanupTranscriber:
        def set_cancel_check(self, callback):
            cleanup_state["cancel"] = callback

        def set_progress_callback(self, callback):
            cleanup_state["progress"] = callback

        def transcribe_batch(self, _wav):
            controller._reset_transcriber_cache()
            return "done"

        def close(self):
            cleanup_state["closed"] = True

    transcriber = CleanupTranscriber()
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: transcriber,
    )
    controller, app = _make_controller()
    settings = controller.settings
    job = controller._register_transcription_job(1, settings, "batch")
    observed = []
    controller.transcription_ready.connect(
        lambda _token, _text: observed.append(dict(cleanup_state))
    )

    controller._transcribe_worker(1, b"RIFF", settings, job)

    assert observed == [
        {
            "cancel": None,
            "progress": None,
            "closed": True,
        }
    ]
    controller.shutdown()
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


def test_start_recording_rejects_streaming_for_batch_only_local_model(monkeypatch):
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        mode="streaming",
        model_size="cohere-transcribe-03-2026",
    )
    overlay = FakeOverlay()
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("batch-only model should be rejected before creation")
        ),
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    controller.start_recording()

    assert overlay.states[-1][0] == "Error"
    assert "ONNX/WebGPU" in overlay.states[-1][1]
    assert "batch mode" in overlay.states[-1][1].lower()
    controller.shutdown()
    _ = app


def test_start_recording_temporarily_reveals_non_pinned_overlay(monkeypatch):
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        mode="batch",
        overlay_always_on_top=False,
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    controller.start_recording()

    assert overlay.reveal_calls == 1
    controller.shutdown()
    _ = app


def test_start_recording_reasserts_pinned_overlay_foreground(monkeypatch):
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        mode="batch",
        overlay_always_on_top=True,
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    controller.start_recording()

    assert overlay.reveal_calls == 1
    controller.shutdown()
    _ = app


class _RunningFuture:
    def done(self):
        return False


def test_start_recording_keeps_selected_model_while_preloading(monkeypatch):
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        mode="batch",
        model_size="large-v3-turbo",
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller._preload_future = _RunningFuture()
    controller._preload_target_key = controller._model_preload_key(settings)

    controller.start_recording()

    assert controller._audio_capture is not None
    assert controller._active_batch_settings is not None
    assert controller._active_batch_settings.model_size == "large-v3-turbo"
    assert overlay.states[-1][0] == "Listening"
    assert "transcription will wait" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_set_overlay_always_on_top_persists_setting():
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        overlay_always_on_top=True,
    )
    store = FakeSettingsStore(settings)
    controller, app = _make_controller(settings_store=store)

    controller.set_overlay_always_on_top(False)

    assert controller.settings.overlay_always_on_top is False
    assert store.saved is not None
    assert store.saved.overlay_always_on_top is False
    controller.shutdown()
    _ = app


def test_start_recording_preload_never_requires_fallback(monkeypatch):
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
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller._preload_future = _RunningFuture()
    controller._preload_target_key = controller._model_preload_key(settings)

    controller.start_recording()

    assert controller._audio_capture is not None
    assert controller._active_batch_settings.model_size == "large-v3-turbo"
    assert "fallback" not in overlay.states[-1][1].lower()
    controller.shutdown()
    _ = app


def test_start_recording_remote_not_blocked_by_stale_local_preload(monkeypatch):
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="groq",
        mode="batch",
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
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


def test_start_recording_forces_compact_listening_state(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="batch")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    controller.start_recording()

    assert overlay.compact_calls >= 1
    assert overlay.states[0][0] == "Listening"
    assert overlay.state_kwargs[0].get("compact") is True
    assert [state for state in overlay.states if state[0] == "Listening"] == [
        ("Listening", "Speak now. Press hotkey again to stop.")
    ]
    controller.shutdown()
    _ = app


def test_start_streaming_renders_one_listening_state(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: FakeStreamingTranscriber(),
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    controller.start_recording()

    assert [state for state in overlay.states if state[0] == "Listening"] == [
        ("Listening", "Streaming active. Speak now, press hotkey to finalize.")
    ]
    controller.shutdown()
    _ = app


def test_start_batch_recording_audio_capture_error(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="batch")
    overlay = FakeOverlay()
    FakeCapture.instances = []
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCaptureFails)
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

        def broken_start(on_partial=None, on_error=None):
            raise TranscriptionError("model not loaded")

        t.start_stream = broken_start
        return t

    monkeypatch.setattr("stt_app.controller.create_transcriber", fail_transcriber)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller.start_recording()
    assert overlay.states[-1][0] == "Error"
    assert "model not loaded" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_preload_progress_poll_skips_during_recording_start():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._preload_future = _RunningFuture()
    controller._recording_start_in_progress = True

    controller._on_preload_progress_poll()

    assert overlay.states == []
    controller.shutdown()
    _ = app


def test_start_streaming_audio_capture_error_stops_transcriber(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    transcriber = FakeStreamingTranscriber()

    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCaptureFails)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda _s, **kw: transcriber
    )

    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller.start_recording()
    assert transcriber.started is True
    assert transcriber.aborted is True  # cleaned up without blocking finalize path
    assert transcriber.stopped is False
    assert overlay.states[-1][0] == "Error"
    controller.shutdown()
    _ = app


def test_start_recording_waits_while_stream_finalize_is_in_progress(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    create_calls = {"count": 0}

    def fake_create(_settings, **_kw):
        create_calls["count"] += 1
        return FakeStreamingTranscriber()

    monkeypatch.setattr("stt_app.controller.create_transcriber", fake_create)

    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller._streaming_recording = True
    controller._audio_capture = None

    controller.start_recording()

    assert create_calls["count"] == 0
    assert overlay.states[-1][0] == "Processing"
    assert "finalizing" in overlay.states[-1][1].lower()
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
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)

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


def test_audio_callback_watchdog_logs_and_stops_empty_batch_capture(
    monkeypatch,
    caplog,
):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="batch")
    overlay = FakeOverlay()
    FakeCapture.instances = []
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    with caplog.at_level(logging.ERROR, logger="test.controller"):
        controller.start_recording()
        capture = FakeCapture.instances[-1]
        capture._wav_bytes = b""

        controller._on_audio_callback_watchdog_timeout()

    assert capture.stopped is True
    assert controller._audio_capture is None
    assert overlay.states[-1] == (
        "Error",
        "Microphone capture started but did not deliver audio. Please retry.",
    )
    assert "audio_capture_callback_timeout mode=batch" in caplog.text
    assert "audio_capture_empty mode=batch" in caplog.text
    controller.shutdown()
    _ = app


def test_audio_callback_watchdog_aborts_streaming_capture(monkeypatch, caplog):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    transcriber = FakeStreamingTranscriber()
    FakeCapture.instances = []
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda _settings, **_kwargs: transcriber
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    with caplog.at_level(logging.ERROR, logger="test.controller"):
        controller.start_recording()
        capture = FakeCapture.instances[-1]
        controller._on_audio_callback_watchdog_timeout()

    assert capture.stopped is True
    assert transcriber.aborted is True
    assert controller._audio_capture is None
    assert overlay.states[-1][0] == "Error"
    assert "Microphone capture started but did not deliver audio" in overlay.states[-1][1]
    assert "audio_capture_callback_timeout mode=streaming" in caplog.text
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

    controller._get_or_create_transcriber = (  # type: ignore[method-assign]
        lambda _settings: PlaceholderTranscriber()
    )

    settings_snapshot = AppSettings(engine="openai", hotkey=FALLBACK_HOTKEY)
    controller._transcribe_worker(1, b"audio", settings_snapshot)

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

    controller._get_or_create_transcriber = (  # type: ignore[method-assign]
        lambda _settings: BrokenTranscriber()
    )

    settings_snapshot = AppSettings(engine="local", hotkey=FALLBACK_HOTKEY)
    controller._transcribe_worker(1, b"audio", settings_snapshot)

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
    controller._finalize_stream_worker(1, None)
    assert overlay.states[-1][0] == "Error"
    assert "not initialized" in overlay.states[-1][1].lower()
    controller.shutdown()
    _ = app


def test_finalize_stream_worker_exception_emits_error():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    transcriber = FakeStreamingTranscriber(stop_raises=RuntimeError("boom"))
    controller._finalize_stream_worker(1, transcriber)
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


def test_on_transcription_partial_ignored_after_abort_requested():
    controller, app = _make_controller(overlay=FakeOverlay())
    controller._streaming_recording = True
    controller._audio_capture = object()
    controller._stream_abort_requested = True
    controller._stream_live_text = "hello world"

    controller._on_transcription_partial("hello world again")

    assert controller._stream_live_text == "hello world"
    controller.shutdown()
    _ = app


def test_stream_runtime_failure_cleans_up_active_session(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    last_recording_store = FakeLastRecordingStore()
    transcriber = FakeStreamingTranscriber(push_raises=RuntimeError("push failed"))
    FakeCapture.instances = []

    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda _s, **kw: transcriber
    )

    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        last_recording_store=last_recording_store,
    )
    controller.start_recording()

    capture = FakeCapture.instances[-1]
    capture.chunk_callback(b"data")

    assert capture.stopped is True
    assert controller._audio_capture is None
    assert transcriber.aborted is True
    assert controller._last_failed_wav_bytes == b"RIFF"
    assert last_recording_store.saved == [(b"RIFF", False)]
    assert last_recording_store.failed == ["Streaming chunk push failed: push failed"]
    assert overlay.states[-1][0] == "Error"
    assert "preserved in memory" in overlay.states[-1][1].lower()

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
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
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
    controller._last_failed_wav_bytes = b"wav-bytes"
    controller._executor = ImmediateExecutor()
    controller._settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="openai",
        openai_model="gpt-4o-transcribe",
    )

    def fake_worker(request_token, wav_bytes, snapshot, job=None):
        captured.append(
            (request_token, wav_bytes, snapshot.engine, snapshot.openai_model)
        )

    controller._transcribe_worker = fake_worker  # type: ignore[method-assign]

    ok = controller.retry_last_transcription()

    assert ok is True
    assert captured == [(1, b"wav-bytes", "openai", "gpt-4o-transcribe")]
    controller.shutdown()
    _ = app


def test_stop_recording_persists_last_recording_and_marks_transcribing(monkeypatch):
    overlay = FakeOverlay()
    last_recording_store = FakeLastRecordingStore()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        overlay=overlay,
        last_recording_store=last_recording_store,
    )
    controller._executor = ImmediateExecutor()
    submitted = []

    def fake_worker(request_token, wav_bytes, snapshot, job=None):
        submitted.append((request_token, wav_bytes, snapshot.mode, snapshot.model_size))

    controller._transcribe_worker = fake_worker  # type: ignore[method-assign]

    controller.start_recording()
    controller.stop_recording()

    assert last_recording_store.saved == [(b"RIFF", False)]
    assert last_recording_store.transcribing == [("local", "small", "batch")]
    assert submitted == [(1, b"RIFF", "batch", "small")]
    controller.shutdown()
    _ = app


def test_vad_auto_stop_marshals_stop_recording_to_qt_thread():
    controller, app = _make_controller()
    main_thread_id = threading.get_ident()

    class _ThreadTrackingCapture(FakeCapture):
        def __init__(self):
            super().__init__()
            self._wav_bytes = b""
            self.stop_thread_id = None

        def stop(self):
            self.stop_thread_id = threading.get_ident()
            return super().stop()

    capture = _ThreadTrackingCapture()
    controller._audio_capture = capture
    worker = threading.Thread(target=controller._auto_stop_from_vad)

    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert capture.stop_thread_id is None

    app.processEvents()

    assert capture.stop_thread_id == main_thread_id
    assert controller._audio_capture is None
    controller.shutdown()
    _ = app


def test_cancel_current_action_stops_active_batch_recording():
    overlay = FakeOverlay()
    last_recording_store = FakeLastRecordingStore()
    controller, app = _make_controller(
        overlay=overlay,
        last_recording_store=last_recording_store,
    )
    capture = FakeCapture()
    controller._audio_capture = capture
    controller._streaming_recording = False
    controller._preload_future = _RunningFuture()

    controller.cancel_current_action()

    assert capture.stopped is True
    assert controller._audio_capture is None
    assert overlay.states[-1][0] == "Done"
    assert "canceled" in overlay.states[-1][1].lower()
    assert "last recording" in overlay.states[-1][1].lower()
    assert last_recording_store.saved == [(b"RIFF", False)]
    assert last_recording_store.canceled == ["Recording canceled before transcription."]
    assert controller._preload_cancel_requested is False
    controller.shutdown()
    _ = app


def test_cancel_current_action_marks_inflight_transcription_as_canceled():
    overlay = FakeOverlay()
    last_recording_store = FakeLastRecordingStore()
    controller, app = _make_controller(
        overlay=overlay,
        last_recording_store=last_recording_store,
    )
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, model_size="small")
    controller._active_request_token = 7
    controller._register_transcription_job(7, settings, "batch")
    controller._preload_future = _RunningFuture()
    last_recording_store._available = True

    controller.cancel_current_action()

    assert overlay.states[-1] == ("Done", "Transcription canceled.")
    assert controller._jobs[7].aborting is True
    assert controller._active_request_token is None
    assert last_recording_store.canceled == ["Transcription canceled by user."]
    assert controller._preload_cancel_requested is False
    controller.shutdown()
    _ = app


def test_cancel_current_action_keeps_completed_transcript_in_history(tmp_path):
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    history = TranscriptHistoryStore(tmp_path / "history.json")
    controller, app = _make_controller(
        overlay=overlay,
        text_inserter=inserter,
        history_store=history,
        last_recording_store=FakeLastRecordingStore(),
    )
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, model_size="small")
    controller._active_request_token = 9
    controller._register_transcription_job(9, settings, "batch")

    controller.cancel_current_action()
    assert overlay.states[-1] == ("Done", "Transcription canceled.")

    # A transcript that still finishes after cancel is kept in history, not
    # inserted into whatever window is focused now.
    controller._on_transcription_ready("finished anyway", request_token=9)
    assert [e.text for e in history.load()] == ["finished anyway"]
    assert inserter.calls == []
    controller.shutdown()
    _ = app


def test_transcribe_audio_file_marks_managed_last_recording_completed(
    monkeypatch,
    tmp_path,
):
    last_path = tmp_path / "last_recording.wav"
    last_path.write_bytes(b"RIFF")
    last_recording_store = FakeLastRecordingStore(str(last_path))
    last_recording_store._available = True
    controller, app = _make_controller(last_recording_store=last_recording_store)

    class _FakeTranscriber:
        def transcribe_batch(self, _path):
            return "import text"

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: _FakeTranscriber(),
    )

    ok, text = controller.transcribe_audio_file(
        str(last_path),
        settings_override=AppSettings(
            hotkey=FALLBACK_HOTKEY,
            engine="deepgram",
            deepgram_model="nova-2",
        ),
    )

    assert ok is True
    assert text == "import text"
    assert last_recording_store.transcribing == [("deepgram", "nova-2", "import")]
    assert last_recording_store.completed == 1
    controller.shutdown()
    _ = app


def test_transcribe_audio_file_marks_managed_last_recording_failed(
    monkeypatch,
    tmp_path,
):
    last_path = tmp_path / "last_recording.wav"
    last_path.write_bytes(b"RIFF")
    last_recording_store = FakeLastRecordingStore(str(last_path))
    last_recording_store._available = True
    controller, app = _make_controller(last_recording_store=last_recording_store)

    class _FakeTranscriber:
        def transcribe_batch(self, _path):
            raise RuntimeError("provider failed")

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: _FakeTranscriber(),
    )

    ok, text = controller.transcribe_audio_file(str(last_path))

    assert ok is False
    assert "provider failed" in text
    assert last_recording_store.failed == ["provider failed"]
    controller.shutdown()
    _ = app


def test_transcribe_audio_file_waits_for_controller_transcription_lane(
    monkeypatch,
    tmp_path,
):
    controller, app = _make_controller()
    release_lane = threading.Event()
    lane_started = threading.Event()
    import_started = threading.Event()
    result: list[tuple[bool, str]] = []

    def _occupy_lane():
        lane_started.set()
        assert release_lane.wait(timeout=2)

    class _FakeTranscriber:
        def transcribe_batch(self, _source):
            import_started.set()
            return "serialized import"

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: _FakeTranscriber(),
    )
    controller._executor.submit(_occupy_lane)
    assert lane_started.wait(timeout=2)
    audio_path = tmp_path / "external.wav"
    audio_path.write_bytes(b"RIFF")
    import_thread = threading.Thread(
        target=lambda: result.append(controller.transcribe_audio_file(str(audio_path)))
    )

    import_thread.start()
    assert not import_started.wait(timeout=0.1)
    release_lane.set()
    import_thread.join(timeout=2)

    assert not import_thread.is_alive()
    assert import_started.is_set()
    assert result == [(True, "serialized import")]
    controller.shutdown()
    _ = app


def test_managed_import_snapshot_cannot_complete_a_newer_recording(
    monkeypatch,
    tmp_path,
):
    history = TranscriptHistoryStore(tmp_path / "history.json")
    last_store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    first = last_store.save_recording(b"RIFF-first", keep_after_success=False)
    controller, app = _make_controller(
        history_store=history,
        last_recording_store=last_store,
    )
    inference_started = threading.Event()
    release_inference = threading.Event()
    received_sources: list[bytes] = []
    result: list[tuple[bool, str]] = []

    class _FakeTranscriber:
        def transcribe_batch(self, source):
            received_sources.append(bytes(source))
            inference_started.set()
            assert release_inference.wait(timeout=2)
            return "first recording transcript"

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: _FakeTranscriber(),
    )
    import_thread = threading.Thread(
        target=lambda: result.append(
            controller.transcribe_audio_file(str(last_store.audio_path))
        )
    )

    import_thread.start()
    assert inference_started.wait(timeout=2)
    second = last_store.save_recording(b"RIFF-second", keep_after_success=False)
    release_inference.set()
    import_thread.join(timeout=2)

    assert result == [(True, "first recording transcript")]
    assert received_sources == [b"RIFF-first"]
    current = last_store.load()
    assert current is not None
    assert current.recording_id == second.recording_id
    assert current.status == "captured"
    assert last_store.audio_path.read_bytes() == b"RIFF-second"
    entries = history.load()
    assert [entry.source_recording_id for entry in entries] == [first.recording_id]
    assert [entry.source_audio_path for entry in entries] == [""]
    controller.shutdown()
    _ = app


def test_import_runtime_close_failure_keeps_successful_transcript(
    monkeypatch,
    tmp_path,
):
    controller, app = _make_controller()

    class _FakeTranscriber:
        def transcribe_batch(self, _source):
            return "successful transcript"

        def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: _FakeTranscriber(),
    )
    audio_path = tmp_path / "external.wav"
    audio_path.write_bytes(b"RIFF")

    result = controller.transcribe_audio_file(
        str(audio_path),
        settings_override=AppSettings(
            hotkey=FALLBACK_HOTKEY,
            model_size="cohere-transcribe-03-2026",
        ),
    )

    assert result == (True, "successful transcript")
    controller.shutdown()
    _ = app


def test_canceled_stale_transcription_result_is_ignored_during_new_recording(
    monkeypatch,
):
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(overlay=overlay)

    controller._active_request_token = 4
    controller._request_audio_by_token[4] = (
        b"wav-bytes",
        AppSettings(hotkey=FALLBACK_HOTKEY, model_size="small"),
    )

    controller.start_recording()
    prior_state_count = len(overlay.states)

    controller._on_transcription_ready("old transcript", request_token=4)

    assert len(overlay.states) == prior_state_count
    assert overlay.states[-1][0] == "Listening"
    controller.shutdown()
    _ = app


def test_transcription_progress_updates_overlay_for_active_request():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._active_request_token = 7

    controller._on_transcription_progress_result(
        7,
        "ONNX runtime active on WebGPU.",
    )

    assert overlay.states[-1] == ("Processing", "ONNX runtime active on WebGPU.")
    assert overlay.state_kwargs[-1] == {"compact": False}

    state_count = len(overlay.states)
    controller._active_request_token = 8
    controller._on_transcription_progress_result(7, "stale")

    assert len(overlay.states) == state_count
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
        "stt_app.transcriber.local_faster_whisper.find_cached_models",
        lambda _model_dir="": ["small"],
    )

    started = []
    monkeypatch.setattr(
        "stt_app.controller.subprocess.Popen",
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


def test_download_model_for_preload_uses_cancellable_worker(monkeypatch):
    controller, app = _make_controller()
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, model_size="small")
    calls: list[tuple[str, str]] = []

    class _Process:
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self):
            return "", ""

    monkeypatch.setattr(
        "stt_app.transcriber.local_faster_whisper.find_cached_models",
        lambda _model_dir="": [],
    )
    monkeypatch.setattr(
        "stt_app.controller.start_model_download_process",
        lambda model_name, model_dir="": (
            calls.append((model_name, model_dir)) or _Process()
        ),
    )

    controller._download_model_for_preload(settings)

    assert calls == [("small", "")]
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
    assert "Cancel hotkey registration failed" in (
        controller._cancel_hotkey_notice or ""
    )
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
