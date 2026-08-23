"""Additional controller coverage tests — shutdown, start_recording edge cases,
transcription_worker error branches, streaming abort, focus poll."""

from __future__ import annotations

import logging

import pytest
import os
import concurrent.futures
import threading
import time
from unittest.mock import MagicMock

from stt_app.config import DEFAULT_ENGINE, FALLBACK_HOTKEY
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


def test_shutdown_aborts_active_stream_transcriber_without_waiting_for_finalize():
    """Shutdown runs on the Qt main thread, and stop_stream() joins the worker
    with no timeout while it runs the final transcription. Quitting mid
    dictation froze the UI for the length of that pass, and the result is
    discarded here anyway."""
    controller, app = _make_controller()
    transcriber = FakeStreamingTranscriber()
    controller._active_stream_transcriber = transcriber
    controller._active_stream_settings = AppSettings()
    controller.shutdown()
    assert transcriber.aborted is True
    assert transcriber.stopped is False
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


def test_start_recording_waits_to_invite_speech_until_capture_started(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="batch")
    overlay = FakeOverlay()
    states_at_capture_start: list[tuple[str, str]] = []

    class StateAwareCapture(FakeCapture):
        def start(self):
            states_at_capture_start.append(overlay.states[-1])
            super().start()

    monkeypatch.setattr("stt_app.controller.AudioCapture", StateAwareCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    controller.start_recording()

    # The compact geometry is asserted before and after the event drain, then
    # once more for the ready-to-speak detail after capture.start() succeeds.
    assert overlay.compact_calls == 3
    assert states_at_capture_start == [
        (
            "Listening",
            "Starting dictation. Please wait for the 'Speak now' message.",
        )
    ]
    assert overlay.states[0][0] == "Listening"
    assert overlay.state_kwargs[0].get("compact") is True
    assert [state for state in overlay.states if state[0] == "Listening"] == [
        (
            "Listening",
            "Starting dictation. Please wait for the 'Speak now' message.",
        ),
        ("Listening", "Speak now. Press hotkey again to stop.")
    ]
    controller.shutdown()
    _ = app


def test_start_streaming_waits_to_invite_speech_until_capture_started(monkeypatch):
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
    # The handshake runs off the Qt thread, so the invitation to speak
    # arrives through a queued signal. Asserting synchronously passed only
    # because this fake connects instantly; a 5 ms delay was enough to make
    # it flake, and every real remote provider takes far longer.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and len(
        [state for state in overlay.states if state[0] == "Listening"]
    ) < 2:
        app.processEvents()
        time.sleep(0.01)

    listening = [state for state in overlay.states if state[0] == "Listening"]
    assert listening[0] == (
        "Listening",
        "Starting dictation. Please wait for the 'Speak now' message.",
    )
    # A local engine connects to nothing, so it reaches the live message; a
    # remote one shows the connecting message first. Both are valid here.
    assert listening[-1][1] in {
        "Streaming active. Speak now, press hotkey to finalize.",
        "Connecting to the speech service. You can speak now.",
    }
    controller.shutdown()
    _ = app


def test_start_streaming_forwards_audio_delivered_inside_capture_start(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    transcriber = FakeStreamingTranscriber()

    class ImmediateCallbackCapture(FakeCapture):
        def start(self):
            super().start()
            assert self.chunk_callback is not None
            self.chunk_callback(b"first audio block")

    monkeypatch.setattr("stt_app.controller.AudioCapture", ImmediateCallbackCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: transcriber,
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    controller.start_recording()
    # Audio delivered from inside capture.start() is buffered while the
    # provider connects and handed over afterwards, so wait for the flush
    # instead of assuming the push already happened.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not transcriber.chunks:
        app.processEvents()
        time.sleep(0.01)

    assert transcriber.chunks == [b"first audio block"]
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
    # The handshake runs off the Qt thread now (a remote provider blocks for
    # seconds), so the failure arrives through a queued signal rather than
    # inline. Pump until it lands instead of asserting on the same call stack.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and overlay.states[-1][0] != "Error":
        app.processEvents()
        time.sleep(0.01)

    assert overlay.states[-1][0] == "Error"
    assert "model not loaded" in overlay.states[-1][1]
    # And the failed start must not leave the microphone or the session behind.
    assert controller._audio_capture is None
    assert controller._streaming_recording is False
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
    # The abort waits for the provider handshake first and therefore runs on
    # its own thread -- it must not block the Qt thread for up to
    # STREAMING_CONNECT_JOIN_TIMEOUT_S. Asserting synchronously here passed
    # only by luck and failed intermittently in a full run.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not transcriber.aborted:
        app.processEvents()
        time.sleep(0.01)
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


def test_audio_callback_watchdog_aborts_batch_without_transcribing_late_audio(
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
        # Simulate the exact timeout race: bytes arrive after the watchdog's
        # callback-count check but before capture.stop() snapshots the buffer.
        capture._wav_bytes = b"late audio"

        controller._on_audio_callback_watchdog_timeout()

    assert capture.stopped is True
    assert controller._audio_capture is None
    assert overlay.states[-1] == (
        "Error",
        "Microphone capture started but did not deliver audio. Please retry.",
    )
    assert "audio_capture_callback_timeout mode=batch" in caplog.text
    assert "audio_capture_empty mode=batch" not in caplog.text
    assert controller._jobs == {}
    assert controller._last_failed_wav_bytes == b"late audio"
    controller.shutdown()
    _ = app


def test_audio_callback_watchdog_ignores_capture_that_received_audio(monkeypatch):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="batch")
    overlay = FakeOverlay()
    FakeCapture.instances = []
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller.start_recording()
    capture = FakeCapture.instances[-1]
    capture.has_received_audio = True

    controller._on_audio_callback_watchdog_timeout()

    assert capture.stopped is False
    assert controller._audio_capture is capture
    controller.shutdown()
    _ = app


def test_stop_recording_logs_pre_stop_warm_stream_context(caplog):
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="batch")
    overlay = FakeOverlay()

    class WarmCapture(FakeCapture):
        def __init__(self):
            super().__init__()
            self.uses_warm_stream = True
            self.callback_count = 7
            self._wav_bytes = b""

        def stop(self):
            self.uses_warm_stream = False
            return super().stop()

    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    capture = WarmCapture()
    controller._audio_capture = capture

    with caplog.at_level(logging.ERROR, logger="test.controller"):
        controller.stop_recording()

    assert "audio_capture_empty mode=batch warm_stream=True callback_count=7" in (
        caplog.text
    )
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


def test_transcribe_worker_empty_batch_text_is_a_failure():
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._executor = ImmediateExecutor()
    controller._last_transcript = "kept"

    class EmptyTranscriber:
        def transcribe_batch(self, wav):
            return "  "

    controller._get_or_create_transcriber = (  # type: ignore[method-assign]
        lambda _settings: EmptyTranscriber()
    )

    settings_snapshot = AppSettings(engine="local", hotkey=FALLBACK_HOTKEY)
    job = controller._register_transcription_job(3, settings_snapshot, "batch")
    controller._active_request_token = 3
    controller._store_request_audio(3, b"spoken-audio", settings_snapshot)
    controller._transcribe_worker(3, b"spoken-audio", settings_snapshot, job)

    assert overlay.states[-1][0] == "Error"
    assert "no text" in overlay.states[-1][1].lower()
    assert controller._last_failed_wav_bytes == b"spoken-audio"
    assert controller._last_transcript == "kept"
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
    assert "Pick a different hotkey" in (controller._hotkey_notice or "")
    # Nothing is registered, so the reclaim timer must keep trying.
    assert controller._hotkey_reclaim_timer.isActive()
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


def test_transcribe_audio_file_empty_model_text_is_a_failure(
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
            return "  "

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: _FakeTranscriber(),
    )

    ok, text = controller.transcribe_audio_file(str(last_path))

    assert ok is False
    assert "no text" in text.lower()
    assert last_recording_store.failed == [text]
    assert last_recording_store.completed == 0
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
# Show-overlay hotkey registration
# ---------------------------------------------------------------------------


class _AcceptAllHotkeyManager(FakeHotkeyManager):
    def __init__(self):
        super().__init__()
        self.unregister_calls = 0

    def register(self, hotkey):
        self.calls.append(hotkey)

    def unregister(self):
        self.unregister_calls += 1


def test_register_show_overlay_hotkey_success_and_idle_detail():
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        show_overlay_hotkey="Ctrl+Alt+F11",
    )
    manager = _AcceptAllHotkeyManager()
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        show_overlay_hotkey_manager=manager,
        overlay=overlay,
    )

    ok = controller._register_show_overlay_hotkey()

    assert ok is True
    assert manager.calls[-1] == "Ctrl+Alt+F11"

    controller._hotkey_registration_ok = True
    controller._cancel_hotkey_registration_ok = True
    controller._show_overlay_hotkey_registration_ok = True
    controller._repaste_hotkey_registration_ok = True
    controller.show_idle_status()
    state, detail = overlay.states[-1]
    assert state == "Idle"
    assert "Overlay: Ctrl+Alt+F11" in detail
    controller.shutdown()
    _ = app


def test_register_show_overlay_hotkey_failure_sets_notice():
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        show_overlay_hotkey="Ctrl+Shift+X",
    )
    manager = FakeHotkeyManager()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        show_overlay_hotkey_manager=manager,
    )

    ok = controller._register_show_overlay_hotkey()

    assert ok is False
    assert "Show-overlay hotkey registration failed" in (
        controller._show_overlay_hotkey_notice or ""
    )
    controller.shutdown()
    _ = app


def test_register_show_overlay_hotkey_disabled_unregisters():
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, show_overlay_hotkey="")
    manager = _AcceptAllHotkeyManager()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        show_overlay_hotkey_manager=manager,
    )

    ok = controller._register_show_overlay_hotkey()

    assert ok is True
    assert manager.calls == []
    assert manager.unregister_calls >= 1
    assert controller._show_overlay_hotkey_notice is None
    controller.shutdown()
    _ = app


def test_refresh_hotkey_registration_includes_show_overlay():
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        show_overlay_hotkey="Ctrl+Alt+F11",
    )
    manager = _AcceptAllHotkeyManager()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        show_overlay_hotkey_manager=manager,
    )

    controller.refresh_hotkey_registration()

    assert manager.calls[-1] == "Ctrl+Alt+F11"
    assert controller._show_overlay_hotkey_registration_ok is True
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# Re-paste hotkey registration and action
# ---------------------------------------------------------------------------


def test_register_repaste_hotkey_and_refresh():
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        repaste_hotkey="Ctrl+Alt+F9",
    )
    manager = _AcceptAllHotkeyManager()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        repaste_hotkey_manager=manager,
    )

    assert controller._register_repaste_hotkey() is True
    assert manager.calls[-1] == "Ctrl+Alt+F9"

    controller.refresh_hotkey_registration()
    assert controller._repaste_hotkey_registration_ok is True
    controller.shutdown()
    _ = app


def test_register_repaste_hotkey_disabled_unregisters():
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, repaste_hotkey="")
    manager = _AcceptAllHotkeyManager()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        repaste_hotkey_manager=manager,
    )

    assert controller._register_repaste_hotkey() is True
    assert manager.calls == []
    assert manager.unregister_calls >= 1
    controller.shutdown()
    _ = app


def test_repaste_last_transcript_inserts_into_current_window(monkeypatch):
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    controller, app = _make_controller(overlay=overlay, text_inserter=inserter)
    beeps: list[bool] = []
    monkeypatch.setattr(
        controller, "_play_completion_beep", lambda: beeps.append(True)
    )
    controller._last_transcript = "hello again"

    controller.repaste_last_transcript()

    assert inserter.calls[-1] == ("hello again", None, "auto")
    state, detail = overlay.states[-1]
    assert state == "Done"
    assert detail == "hello again"
    assert beeps == [True]
    controller.shutdown()
    _ = app


def test_repaste_last_transcript_without_transcript_shows_error():
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    controller, app = _make_controller(overlay=overlay, text_inserter=inserter)

    controller.repaste_last_transcript()

    assert inserter.calls == []
    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "No transcript" in detail
    controller.shutdown()
    _ = app


def test_repaste_last_transcript_blocked_while_recording():
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    controller, app = _make_controller(overlay=overlay, text_inserter=inserter)
    controller._last_transcript = "hello again"
    controller._audio_capture = FakeCapture()

    controller.repaste_last_transcript()

    assert inserter.calls == []
    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "recording" in detail.lower()
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# Completion tone
# ---------------------------------------------------------------------------


class _ImmediateThread:
    def __init__(self, target=None, args=(), **_kwargs):
        self._target = target
        self._args = args

    def start(self):
        if self._target is not None:
            self._target(*self._args)


def test_completion_beep_disabled_by_default_is_silent(monkeypatch):
    controller, app = _make_controller()
    tones: list[str] = []
    monkeypatch.setattr(controller, "_play_tone", lambda tone: tones.append(tone))

    controller._play_completion_beep()

    assert tones == []
    controller.shutdown()
    _ = app


def test_completion_beep_enabled_plays_selected_tone(monkeypatch):
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        completion_beep_enabled=True,
        completion_beep_tone="high",
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
    )
    tones: list[str] = []
    monkeypatch.setattr(controller, "_play_tone", lambda tone: tones.append(tone))
    monkeypatch.setattr("stt_app.controller.threading.Thread", _ImmediateThread)

    controller._play_completion_beep()

    assert tones == ["high"]
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


def test_the_preferred_hotkey_is_reclaimed_once_it_is_free():
    """Another program holding the hotkey is temporary. The app keeps the
    user's choice, runs on a fallback, and takes the real one back on its own
    instead of leaving them on a substitute forever."""
    from stt_app.config import DEFAULT_HOTKEY, FALLBACK_HOTKEYS

    class BusyThenFree:
        def __init__(self):
            self.calls = []
            self.preferred_free = False

        def register(self, hotkey):
            self.calls.append(hotkey)
            if hotkey == DEFAULT_HOTKEY and not self.preferred_free:
                raise ValueError("already registered by another program")

        def unregister(self):
            pass

    manager = BusyThenFree()
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(AppSettings(hotkey=DEFAULT_HOTKEY)),
        hotkey_manager=manager,
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
    )

    assert controller._register_hotkey_with_fallback() is True
    assert controller._active_hotkey == FALLBACK_HOTKEYS[0]
    assert controller.settings.hotkey == DEFAULT_HOTKEY, "the choice must survive"
    assert controller._hotkey_reclaim_timer.isActive()

    # The other program exits; the next tick should take the hotkey back.
    manager.preferred_free = True
    controller._reclaim_preferred_hotkey()

    assert controller._active_hotkey == DEFAULT_HOTKEY
    assert controller._hotkey_notice is None
    assert not controller._hotkey_reclaim_timer.isActive()
    controller.shutdown()
    _ = app


def test_a_slow_streaming_handshake_does_not_block_the_qt_thread(monkeypatch):
    """Pressing the hotkey must not freeze the UI while a provider connects.

    Deepgram waits up to 8 s for its socket and the AssemblyAI SDK connects
    synchronously. Called on the Qt thread that froze the overlay, the tray and
    the settings dialog for the whole handshake.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    release_connect = threading.Event()
    connect_entered = threading.Event()

    def slow_transcriber(_s, **kw):
        transcriber = FakeStreamingTranscriber()

        def slow_start(on_partial=None, on_error=None):
            connect_entered.set()
            assert release_connect.wait(timeout=30), "connect was never released"

        transcriber.start_stream = slow_start
        return transcriber

    monkeypatch.setattr("stt_app.controller.create_transcriber", slow_transcriber)
    # Without this the test opens the real microphone: CI runners have no
    # capture device, and locally it switches the developer's mic on
    # mid-suite.
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    try:
        started = time.monotonic()
        controller.start_recording()
        elapsed = time.monotonic() - started

        assert connect_entered.wait(timeout=10), "the handshake never started"
        assert elapsed < 2.0, (
            f"start_recording blocked the Qt thread for {elapsed:.1f}s"
        )
        # The microphone is already open, so the user can talk immediately.
        assert controller._audio_capture is not None
        assert controller._streaming_recording is True
    finally:
        release_connect.set()
        controller.shutdown()
    _ = app


def test_audio_recorded_while_connecting_is_delivered_in_order(monkeypatch):
    """Speech during the handshake must reach the provider, not be dropped.

    The microphone is opened before the provider is ready precisely so the
    first words survive; that only helps if the buffered audio is handed over
    afterwards, in the order it was recorded.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    release_connect = threading.Event()
    connect_entered = threading.Event()
    pushed = []

    def slow_transcriber(_s, **kw):
        transcriber = FakeStreamingTranscriber()

        def slow_start(on_partial=None, on_error=None):
            connect_entered.set()
            assert release_connect.wait(timeout=30)

        transcriber.start_stream = slow_start
        transcriber.push_audio_chunk = pushed.append
        return transcriber

    monkeypatch.setattr("stt_app.controller.create_transcriber", slow_transcriber)
    # Without this the test opens the real microphone: CI runners have no
    # capture device, and locally it switches the developer's mic on
    # mid-suite.
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    try:
        controller.start_recording()
        assert connect_entered.wait(timeout=10)

        for index in range(4):
            controller._on_stream_audio_chunk(bytes([index]) * 8)
        assert pushed == [], "audio reached the provider before it was connected"

        release_connect.set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and len(pushed) < 4:
            app.processEvents()
            time.sleep(0.01)

        assert pushed == [bytes([index]) * 8 for index in range(4)]

        # And once connected, later chunks go straight through in order.
        controller._on_stream_audio_chunk(b"\x09" * 8)
        assert pushed[-1] == b"\x09" * 8
    finally:
        release_connect.set()
        controller.shutdown()
    _ = app


def test_a_remote_stream_finalize_does_not_queue_behind_a_local_batch_job():
    """Stopping a remote dictation must not wait for unrelated model work.

    `_executor` has one worker so two local models never load at once. A remote
    finalize loads nothing -- it closes a socket -- so sharing that queue only
    meant the transcript appeared minutes late when a local batch job happened
    to be running.
    """
    controller, app = _make_controller()
    try:
        local = AppSettings(engine=DEFAULT_ENGINE)
        remote = AppSettings(engine="deepgram")

        assert controller._stream_finalize_executor_for(local) is controller._executor
        assert (
            controller._stream_finalize_executor_for(remote)
            is controller._stream_finalize_executor
        )
        assert controller._stream_finalize_executor is not controller._executor

        # Testing the selector alone proves nothing: the whole fix can be
        # undone by changing the submit site back to `self._executor`, and
        # a selector-only assertion stays green. Exercise the real submit.
        used = []

        class _RecordingExecutor:
            def __init__(self, label):
                self.label = label

            def submit(self, *args, **kwargs):
                used.append(self.label)
                return concurrent.futures.Future()

            def shutdown(self, *args, **kwargs):
                pass

        controller._executor = _RecordingExecutor("shared")
        controller._stream_finalize_executor = _RecordingExecutor("finalize")
        controller._active_stream_settings = remote
        controller._active_stream_transcriber = FakeStreamingTranscriber()
        controller._submit_stream_finalize()
        assert used == ["finalize"], (
            "a remote finalize was submitted to the shared model worker"
        )

        used.clear()
        controller._active_stream_settings = local
        controller._active_stream_transcriber = FakeStreamingTranscriber()
        controller._submit_stream_finalize()
        assert used == ["shared"], (
            "local streaming must keep finalizing on the shared worker"
        )
    finally:
        controller.shutdown()
    _ = app


def test_shutdown_stops_the_stream_finalize_worker():
    """A forgotten executor keeps a non-daemon thread alive past quit."""
    controller, app = _make_controller()
    controller.shutdown()
    assert controller._stream_finalize_executor._shutdown is True
    _ = app


def test_a_failed_reclaim_keeps_the_working_fallback_hotkey():
    """A reclaim attempt must never cost the user the hotkey they already have.

    `HotkeyManager.register` unregisters the current binding before trying the
    new one and does not restore it on failure, so a reclaim tick that fails
    used to leave no hotkey registered at all -- while the idle line kept
    advertising the fallback that no longer existed.
    """
    from stt_app.config import FALLBACK_HOTKEYS
    from stt_app.hotkey import HotkeyRegistrationError

    registered: list[str] = []

    class _PreferredStaysBusy:
        hotkey_id = 1

        def __init__(self):
            self.is_registered = False

        def register(self, hotkey):
            # Mirror the real manager: drop the current binding first.
            self.is_registered = False
            if registered:
                registered.pop()
            if hotkey == "Ctrl+Alt+Space":
                raise HotkeyRegistrationError("in use by another program")
            registered.append(hotkey)
            self.is_registered = True

        def unregister(self):
            self.is_registered = False
            if registered:
                registered.pop()

    manager = _PreferredStaysBusy()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(AppSettings(hotkey="Ctrl+Alt+Space")),
        hotkey_manager=manager,
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
    )
    try:
        assert controller._register_hotkey_with_fallback() is True
        assert registered == [FALLBACK_HOTKEYS[0]], registered
        assert controller._active_hotkey == FALLBACK_HOTKEYS[0]

        controller._reclaim_preferred_hotkey()

        assert registered == [FALLBACK_HOTKEYS[0]], (
            "the failed reclaim left the user with no registered hotkey"
        )
        assert manager.is_registered is True
        assert controller._active_hotkey == FALLBACK_HOTKEYS[0]
    finally:
        controller.shutdown()
    _ = app


def test_a_fallback_never_steals_the_users_own_other_hotkeys():
    """The recording fallback runs first, so a collision breaks the rest.

    If the user assigned Ctrl+Alt+F9 to Cancel and the recording hotkey falls
    back onto it, the cancel registration afterwards fails with "in use by
    another program" -- the other program being this app.
    """
    from stt_app.config import FALLBACK_HOTKEYS
    from stt_app.hotkey import HotkeyRegistrationError

    registered: list[str] = []

    class _OnlyPreferredIsBusy:
        hotkey_id = 1
        is_registered = False

        def register(self, hotkey):
            if hotkey == "Ctrl+Alt+Space":
                raise HotkeyRegistrationError("in use by another program")
            registered.append(hotkey)
            self.is_registered = True

        def unregister(self):
            self.is_registered = False

    manager = _OnlyPreferredIsBusy()
    settings = AppSettings(
        hotkey="Ctrl+Alt+Space",
        cancel_hotkey=FALLBACK_HOTKEYS[0],
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=manager,
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
    )
    try:
        registered.clear()
        assert controller._register_hotkey_with_fallback() is True
        assert registered == [FALLBACK_HOTKEYS[1]], (
            f"took {registered}, but {FALLBACK_HOTKEYS[0]} is the user's "
            "cancel hotkey"
        )
    finally:
        controller.shutdown()
    _ = app


def test_the_reclaim_recovers_when_a_fallback_frees_up_after_a_total_failure():
    """With nothing registered, retrying only the preferred key never recovers.

    If every key is busy at startup the app has no hotkey at all. The reclaim
    timer used to retry `settings.hotkey` alone, so a fallback becoming free a
    minute later went unnoticed and the user stayed stuck until they opened
    Settings and saved.
    """
    from stt_app.config import FALLBACK_HOTKEYS
    from stt_app.hotkey import HotkeyRegistrationError

    registered: list[str] = []

    class _EverythingBusyAtFirst:
        hotkey_id = 1
        is_registered = False

        def __init__(self):
            self.free = set()

        def register(self, hotkey):
            if hotkey not in self.free:
                raise HotkeyRegistrationError("in use by another program")
            registered.append(hotkey)
            self.is_registered = True

        def unregister(self):
            self.is_registered = False

    manager = _EverythingBusyAtFirst()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(AppSettings(hotkey="Ctrl+Alt+Space")),
        hotkey_manager=manager,
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
    )
    try:
        registered.clear()
        assert controller._register_hotkey_with_fallback() is False
        assert controller._active_hotkey == ""
        assert controller._hotkey_reclaim_timer.isActive()

        # A fallback -- not the preferred key -- becomes available.
        manager.free.add(FALLBACK_HOTKEYS[2])
        controller._reclaim_preferred_hotkey()

        assert registered == [FALLBACK_HOTKEYS[2]], (
            "the reclaim only ever retried the preferred hotkey"
        )
        assert controller._active_hotkey == FALLBACK_HOTKEYS[2]
    finally:
        controller.shutdown()
    _ = app


def test_a_vad_auto_stop_is_logged(caplog):
    """A recording that ends by itself must say so in the log.

    Without this a VAD stop is indistinguishable from a hotkey stop, so a user
    reporting "it stopped on its own and transcribed garbage" cannot be
    answered from their log at all -- which is exactly what happened.
    """
    controller, app = _make_controller()
    try:
        with caplog.at_level(logging.INFO):
            controller._auto_stop_from_vad()
        assert any(
            "recording_auto_stopped_by_vad" in record.getMessage()
            for record in caplog.records
        ), "a VAD auto-stop left no trace in the log"
    finally:
        controller.shutdown()
    _ = app


def test_switching_windows_during_a_stream_suspends_insertion_instead_of_aborting(
    monkeypatch,
):
    """Tabbing away mid-dictation must not end the session.

    Live insertion writes at the caret, so once another window is in front the
    words would land in the wrong document -- but ending the whole dictation
    for that was far more disruptive than the problem: people switch windows
    mid-thought, and the rest of what they said was simply gone. The recording
    now continues with insertion suspended.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    overlay = FakeOverlay()
    transcriber = FakeStreamingTranscriber()
    focus_helper = FakeWindowFocusHelper()

    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda _s, **kw: transcriber
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        window_focus_helper=focus_helper,
    )
    try:
        controller.start_recording()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not controller._streaming_recording:
            app.processEvents()
            time.sleep(0.01)
        assert controller._streaming_recording is True

        focus_helper.current = 987654  # the user switches to another window
        controller._on_stream_focus_poll()

        assert controller._streaming_recording is True, "the dictation was ended"
        assert transcriber.aborted is False
        assert controller._audio_capture is not None, "the microphone was closed"
        assert controller._stream_insertion_suspended is True

        # And it resumes on its own when the target comes back to the front.
        focus_helper.current = focus_helper.captured
        controller._on_stream_focus_poll()
        assert controller._stream_insertion_suspended is False
    finally:
        controller.shutdown()
    _ = app


def test_a_suspended_stream_does_not_paste_into_the_other_window(monkeypatch):
    """Suspended means suspended: no partial may reach the inserter."""
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    inserter = FakeTextInserter()
    transcriber = FakeStreamingTranscriber()
    focus_helper = FakeWindowFocusHelper()

    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda _s, **kw: transcriber
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=FakeOverlay(),
        text_inserter=inserter,
        window_focus_helper=focus_helper,
    )
    try:
        controller.start_recording()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not controller._streaming_recording:
            app.processEvents()
            time.sleep(0.01)

        # Enough stable words that the locked prefix actually advances: it
        # only commits words that survived a previous partial minus the
        # stability guard and revision window.
        base = "das ist ein laengerer satz mit vielen stabilen woertern"
        controller._on_transcription_partial(base)
        controller._on_transcription_partial(base + " und noch mehr davon")
        pasted_before = len(inserter.calls)
        assert pasted_before >= 1, "live insertion was not running to begin with"

        focus_helper.current = 987654
        controller._on_stream_focus_poll()
        controller._on_transcription_partial(base + " und noch mehr davon hier")
        controller._on_transcription_partial(base + " und noch mehr davon hier auch")

        assert len(inserter.calls) == pasted_before, (
            f"pasted into the foreign window while suspended: {inserter.calls}"
        )
    finally:
        controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    "spelling",
    [
        "Ctrl+Win+F9",
        "Win+Ctrl+F9",
        "Control+Win+F9",
        "Ctrl + Win + F9",
        "ctrl+win+f9",
        "  Ctrl+Win+F9  ",
    ],
)
def test_the_fallback_guard_recognises_every_spelling_of_the_same_hotkey(spelling):
    """Windows sees one hotkey; a text comparison sees six different strings.

    The guard exists so a fallback never steals the key the user assigned to
    Cancel. Comparing the typed string let a hand-edited settings.json walk
    straight past it and reintroduce the collision.
    """
    from stt_app.config import FALLBACK_HOTKEYS
    from stt_app.hotkey import HotkeyRegistrationError

    registered: list[str] = []

    class _OnlyPreferredIsBusy:
        hotkey_id = 1
        is_registered = False

        def register(self, hotkey):
            if hotkey == "Ctrl+Alt+Space":
                raise HotkeyRegistrationError("in use by another program")
            registered.append(hotkey)
            self.is_registered = True

        def unregister(self):
            self.is_registered = False

    controller, app = _make_controller(
        settings_store=FakeSettingsStore(
            AppSettings(hotkey="Ctrl+Alt+Space", cancel_hotkey=spelling)
        ),
        hotkey_manager=_OnlyPreferredIsBusy(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
    )
    try:
        registered.clear()
        assert controller._register_hotkey_with_fallback() is True
        assert registered == [FALLBACK_HOTKEYS[1]], (
            f"cancel is bound to {spelling!r}, but the recording fallback took "
            f"{registered} anyway"
        )
    finally:
        controller.shutdown()
    _ = app


@pytest.mark.parametrize("archive_enabled", [True, False])
def test_a_cancelled_recording_follows_the_archive_setting(
    monkeypatch, tmp_path, archive_enabled
):
    """Cancelling with the hotkey must not be a special case for the audio.

    Answers a direct user question: a separate "keep audio even when
    cancelled" option is not needed, because "Archive every recording to
    folder" already covers the cancel path, and the last-recording slot keeps
    it for Retry either way. Neither can help if the app crashes mid-recording
    -- the WAV only exists in memory until capture.stop() returns.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        save_all_recordings=archive_enabled,
        recordings_dir=str(tmp_path / "recordings"),
    )
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    FakeCapture.instances = []
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=FakeOverlay(),
    )
    try:
        controller.start_recording()
        assert controller._audio_capture is not None
        controller.cancel_current_action()

        capture = FakeCapture.instances[-1]
        archived_path = getattr(capture, "last_saved_path", None)
        if archive_enabled:
            assert archived_path is not None, (
                "the cancelled recording was not archived"
            )
            assert str(tmp_path / "recordings") in str(archived_path)
        else:
            assert archived_path is None, (
                f"archiving is off, yet {archived_path} was written"
            )
    finally:
        controller.shutdown()
    _ = app


def test_the_focus_poll_timer_is_actually_armed_when_a_stream_starts(monkeypatch):
    """Wiring guard for the whole focus feature.

    Both suspension tests call `_on_stream_focus_poll()` directly, so neither
    notices whether anything ever calls it. Guarding the timer's start on
    STREAMING_ABORT_ON_FOCUS_CHANGE once made the suspension dead code *and*
    removed the abort it replaced, so live partials pasted into whatever window
    was in front -- and the suite stayed green.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, mode="streaming")
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _s, **kw: FakeStreamingTranscriber(),
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings), overlay=FakeOverlay()
    )
    try:
        assert controller._focus_poll_timer.isActive() is False
        controller.start_recording()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not controller._streaming_recording:
            app.processEvents()
            time.sleep(0.01)

        assert controller._focus_poll_timer.isActive() is True, (
            "nothing polls the focus, so a window switch is never noticed"
        )
        controller.stop_recording()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and controller._focus_poll_timer.isActive():
            app.processEvents()
            time.sleep(0.01)
        assert controller._focus_poll_timer.isActive() is False, (
            "the focus poll kept running after the recording stopped"
        )
    finally:
        controller.shutdown()
    _ = app


def test_a_post_paste_background_failure_offers_no_action_at_all(monkeypatch):
    """Wiring guard: Retry here re-transcribes a *different* recording.

    `error_action=None` does not mean "no action" -- the overlay treats
    anything that is not Insert as Retry. Retry re-runs the last FAILED
    recording, which is cleared only on the foreground path, so it can paste an
    older recording on top of text that is already in the document.
    """
    from stt_app.config import OVERLAY_ERROR_ACTION_INSERT, OVERLAY_ERROR_ACTION_NONE

    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    try:
        job = controller._register_transcription_job(
            controller._next_request_token(),
            AppSettings(hotkey=FALLBACK_HOTKEY),
            "batch",
        )

        controller._last_insert_may_have_pasted = True
        controller._report_background_insertion_failure(job, "der transkript")
        state, detail = overlay.states[-1][0], overlay.states[-1][1]
        action = overlay.state_kwargs[-1].get("error_action")
        assert state == "Error"
        assert action == OVERLAY_ERROR_ACTION_NONE, (
            f"offered {action!r}; Retry would re-transcribe another recording"
        )
        assert "was was" not in detail, f"duplicated verb in: {detail!r}"
        assert "inserted, but the clipboard" in detail

        controller._last_insert_may_have_pasted = False
        controller._report_background_insertion_failure(job, "der transkript")
        assert overlay.state_kwargs[-1].get("error_action") == (
            OVERLAY_ERROR_ACTION_INSERT
        )
    finally:
        controller.shutdown()
    _ = app


def test_a_foreground_post_paste_failure_also_offers_no_action(monkeypatch):
    """The twin of the background case, missed when that one was fixed.

    Reachable through "Insert last transcript again": a post-paste failure
    there left a Retry button, and Retry re-transcribes the last FAILED
    recording -- cleared only on the foreground ready path, so it can be an
    entirely different recording pasted on top of what was just inserted.
    """
    from stt_app.config import OVERLAY_ERROR_ACTION_NONE
    from stt_app.text_inserter import TextMayHaveBeenPastedError

    overlay = FakeOverlay()

    class _PastedThenFailed:
        def insert_text_with_options(self, *args, **kwargs):
            raise TextMayHaveBeenPastedError(
                "Text pasted but clipboard restore failed: busy"
            )

        def insert_text(self, *args, **kwargs):
            raise TextMayHaveBeenPastedError("Text pasted but restore failed")

    controller, app = _make_controller(overlay=overlay, text_inserter=_PastedThenFailed())
    try:
        assert controller._insert_text_at_target("hallo welt", restore_focus=False) is (
            False
        )
        assert overlay.states[-1][0] == "Error"
        assert overlay.state_kwargs[-1].get("error_action") == (
            OVERLAY_ERROR_ACTION_NONE
        ), "a Retry button here re-transcribes a different recording"
    finally:
        controller.shutdown()
    _ = app
