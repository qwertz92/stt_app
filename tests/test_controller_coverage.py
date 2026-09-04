"""Additional controller coverage tests — shutdown, start_recording edge cases,
transcription_worker error branches, streaming abort, focus poll."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
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
)
from conftest import (
    make_controller as _make_controller,
)

from stt_app import controller as controller_module
from stt_app.audio_capture import AudioCaptureError
from stt_app.config import (
    DEFAULT_ENGINE,
    DEFAULT_MODEL_SIZE,
    FALLBACK_HOTKEY,
    OVERLAY_ERROR_ACTION_INSERT,
)
from stt_app.last_recording_store import LastRecordingStore
from stt_app.settings_store import AppSettings
from stt_app.transcriber.base import TranscriptionCanceled, TranscriptionError
from stt_app.transcript_history import TranscriptHistoryStore


class _NotAnException(BaseException):
    """A `BaseException` that is not an `Exception`, like `KeyboardInterrupt`.

    The whole family of defects in this area is "the guard is one class too
    narrow", so these tests need a class the narrow guard misses. Using
    `KeyboardInterrupt` itself invites pytest and the terminal to treat the
    run as interrupted.
    """


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
    controller._settings_store._settings = replace(
        controller.settings, model_size="medium"
    )
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


@pytest.mark.parametrize(
    "model_size",
    ["cohere-transcribe-03-2026", "parakeet-tdt-0.6b-v3", "canary-1b-v2"],
)
def test_start_recording_rejects_streaming_for_batch_only_local_model(
    monkeypatch, model_size
):
    """The refusal must name the *model*, and must not contradict itself.

    Only the ONNX/WebGPU models took the local branch, so Parakeet and Canary
    -- batch-only through a different runtime -- fell into the remote branch
    and were told to "use local" while the local engine was already selected.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        mode="streaming",
        model_size=model_size,
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
    detail = overlay.states[-1][1]
    assert model_size in detail
    assert "batch mode" in detail.lower()
    # It must not tell a local user to "use local".
    assert "selected provider" not in detail
    controller.shutdown()
    _ = app


def test_start_recording_rejects_streaming_for_a_batch_only_provider():
    """The other branch still has to talk about the provider, not a model."""
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="openai",
        mode="streaming",
        has_openai_key=True,
    )
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    controller.start_recording()

    assert overlay.states[-1][0] == "Error"
    assert "selected provider" in overlay.states[-1][1]
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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


def _preloading_controller(monkeypatch, model_size="large-v3-turbo"):
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        mode="batch",
        model_size=model_size,
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller._preload_future = _RunningFuture()
    controller._preload_target_key = controller._model_preload_key(settings)
    return controller, app, overlay, settings


def test_the_recording_message_names_the_preload_phase_it_is_actually_in(monkeypatch):
    """A preload downloads first and then loads. Both were reported as
    "still loading", which understated a multi-gigabyte fetch."""
    controller, app, overlay, _settings = _preloading_controller(monkeypatch)
    controller._preload_phase = (
        controller._preload_generation,
        controller_module._PRELOAD_PHASE_DOWNLOAD,
    )

    controller.start_recording()

    assert "is still downloading" in overlay.states[-1][1]
    assert "is still loading" not in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_the_recording_message_says_loading_once_the_download_is_done(monkeypatch):
    controller, app, overlay, _settings = _preloading_controller(monkeypatch)
    controller._preload_phase = (
        controller._preload_generation,
        controller_module._PRELOAD_PHASE_LOAD,
    )

    controller.start_recording()

    assert "is still loading" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_a_preload_that_is_only_loading_never_claims_to_be_downloading(monkeypatch):
    """The progress bar measures directory growth. During the load phase
    nothing grows, so a fully downloaded model printed a frozen
    "Downloading ... approx. 100%"."""
    controller, app, _overlay, _settings = _preloading_controller(monkeypatch)
    controller._preload_target_model = "large-v3-turbo"
    controller._preload_phase = (
        controller._preload_generation,
        controller_module._PRELOAD_PHASE_LOAD,
    )

    detail = controller._preload_progress_detail()

    assert "Loading 'large-v3-turbo' into memory." in detail
    assert "ownload" not in detail
    assert "%" not in detail
    controller.shutdown()
    _ = app


def test_the_download_phase_still_reports_measured_progress(monkeypatch):
    controller, app, _overlay, _settings = _preloading_controller(monkeypatch)
    controller._preload_target_model = "large-v3-turbo"
    controller._preload_phase = (
        controller._preload_generation,
        controller_module._PRELOAD_PHASE_DOWNLOAD,
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_faster_whisper.estimate_cached_model_bytes",
        lambda *_args, **_kwargs: 100 * 1024 * 1024,
    )

    detail = controller._preload_progress_detail()

    assert "ownload" in detail
    controller.shutdown()
    _ = app


def test_a_stale_phase_from_an_earlier_preload_is_ignored(monkeypatch):
    """Phases are generation-scoped: a worker retired by a newer preload must
    not describe what the current one is doing."""
    controller, app, _overlay, _settings = _preloading_controller(monkeypatch)
    controller._preload_phase = (
        controller._preload_generation - 1,
        controller_module._PRELOAD_PHASE_DOWNLOAD,
    )

    assert controller._current_preload_phase() == ""
    assert controller._preload_phase_word() == "loading"
    controller.shutdown()
    _ = app


def test_the_preload_worker_stamps_each_phase_as_it_reaches_it(monkeypatch):
    """The phase words are only honest if the worker actually stamps them.

    Every other test in this group sets ``_preload_phase`` by hand, so all of
    them would still pass if the worker stamped nothing at all.
    """
    controller, app, _overlay, settings = _preloading_controller(monkeypatch)
    generation = controller._preload_generation
    key = controller._model_preload_key(settings)
    seen: list[str] = []

    def fake_download(_settings, _generation):
        seen.append(controller._current_preload_phase())

    class FakeLease:
        transcriber = None

        def release(self):
            return None

    def fake_acquire(_settings, allow_isolated=True):
        seen.append(controller._current_preload_phase())
        return FakeLease()

    monkeypatch.setattr(controller, "_download_model_for_preload", fake_download)
    monkeypatch.setattr(controller, "_acquire_transcriber_runtime", fake_acquire)

    controller._preload_model_worker(settings, generation, key)

    assert seen == [
        controller_module._PRELOAD_PHASE_DOWNLOAD,
        controller_module._PRELOAD_PHASE_LOAD,
    ]
    controller.shutdown()
    _ = app


def test_a_queued_preload_does_not_claim_to_be_downloading(monkeypatch):
    """One preload worker runs at a time, so a second one waits.

    Stamping DOWNLOAD at submit printed a frozen "Downloading ... approx.
    100%" for a model nothing was fetching yet -- exactly the message the user
    reported seeing for an already complete download.
    """
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
    submitted: list[tuple] = []

    class BlockedExecutor:
        def submit(self, *args, **kwargs):
            submitted.append(args)
            return _RunningFuture()

        def shutdown(self, *args, **kwargs):
            return None

    controller._preload_executor = BlockedExecutor()

    controller._start_local_model_preload()

    assert submitted
    assert (
        controller._current_preload_phase()
        == controller_module._PRELOAD_PHASE_QUEUED
    )
    assert controller._preload_phase_word() == "waiting for another model to finish"
    detail = controller._preload_progress_detail()
    assert "Waiting for the previous model" in detail
    assert "ownload" not in detail
    assert "%" not in detail
    controller.shutdown()
    _ = app


def test_a_download_canceled_during_a_preload_is_not_a_broken_model(monkeypatch):
    """A cancel from the download slot must not be recorded as a failure.

    The transcriber's own load path downloads through the single machine-wide
    slot, and the cancel check installed for the preload raises there.
    Reported as "could not be loaded", it was also *persisted* for that
    preload key, so the next dictation re-raised the stored failure instead of
    retrying.

    The raise has to come out of `preload_model()`, which is where the load
    path actually runs; an earlier version of this test threw from
    `_acquire_transcriber_runtime` instead and so never covered the branch
    that holds a lease.
    """
    controller, app, _overlay, settings = _preloading_controller(monkeypatch)
    generation = controller._preload_generation
    key = controller._model_preload_key(settings)
    done: list[tuple[int, bool, str]] = []
    controller.model_preload_done.connect(
        lambda gen, ok, message: done.append((gen, ok, message))
    )
    released: list[bool] = []
    installed: list[object] = []

    class CanceledTranscriber:
        def set_cancel_check(self, cancel_check):
            installed.append(cancel_check)

        def preload_model(self):
            raise TranscriptionCanceled("Model download canceled.")

    class Lease:
        transcriber = CanceledTranscriber()

        def release(self):
            released.append(True)

    monkeypatch.setattr(
        controller, "_download_model_for_preload", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        controller, "_acquire_transcriber_runtime", lambda *_a, **_k: Lease()
    )

    controller._preload_model_worker(settings, generation, key)

    assert done and done[-1][1] is False
    assert "canceled" in done[-1][2].lower()
    assert "could not be loaded" not in done[-1][2]
    # Nothing persisted, so the next save retries instead of re-raising.
    with controller._preload_result_lock:
        assert controller._preload_results[key][1] is None
    # `None` is the success sentinel, and the cached key already matches this
    # snapshot, so without condemning the runtime a half-loaded model would be
    # reported as preloaded and never retried.
    with controller._transcriber_runtime_state_lock:
        assert controller._pending_transcriber_cache_reset is True
    # ...and the point of condemning it: the next save preloads again instead
    # of treating the canceled model as ready. One thing has to be arranged for
    # that: the executor future this helper leaves running must be finished, or
    # the "already being prepared" branch answers False first.
    #
    # Pinning `_transcriber_cache_key` was tried here and removed as dead. The
    # claim was that the final `cached_key != identity` comparison would
    # otherwise answer on its own, but that comparison is the *last* branch of
    # `_local_model_preload_needed`; both the failed-result branch and the
    # condemned-runtime branch above it return True first, so the comparison is
    # never reached either way.
    with controller._preload_result_lock:
        controller._preload_future = None
    assert controller._local_model_preload_needed(settings) is True
    assert released == [True]
    # Installed for the load, then cleared: the runtime is shared and cached
    # for the app's lifetime, so a leaked check would cancel the next job.
    assert len(installed) == 2 and installed[-1] is None
    controller.shutdown()
    _ = app


def test_a_cancel_that_surfaces_as_a_plain_error_still_condemns_the_runtime(
    monkeypatch,
):
    """Not every cancelled load raises `TranscriptionCanceled`.

    A load that fails for an ordinary reason while a cancel is pending -- a
    corrupt snapshot, a missing Node runtime, a process that will not spawn --
    lands in the `except Exception` arm, whose cancel branch returned from
    *above* the condemnation, while the `TranscriptionCanceled` arm above it
    and the `BaseException` arm below it both condemn unconditionally.

    (This docstring first named the Cohere/Granite child-kill as the case.
    Checked: that raises `TranscriptionCanceled`, which the arm above already
    handles, and it lives in `transcribe_batch` -- that runtime's
    `preload_model` is `_ensure_process()` with no cancel check at all.)

    The consequence is the one the `TranscriptionCanceled` arm's own comment
    describes: `None` is the *success* sentinel for
    `_local_model_preload_needed`, and the cached key already matches this
    settings snapshot, so a half-loaded runtime was reported as preloaded,
    left in the cache, and used by the next dictation.
    """
    controller, app, _overlay, settings = _preloading_controller(monkeypatch)
    generation = controller._preload_generation
    key = controller._model_preload_key(settings)
    done: list[tuple[int, bool, str]] = []
    controller.model_preload_done.connect(
        lambda gen, ok, message: done.append((gen, ok, message))
    )
    released: list[bool] = []

    class KilledTranscriber:
        def set_cancel_check(self, cancel_check):
            pass

        def preload_model(self):
            # The cancel arrives *during* the load, which is the real
            # sequence. Marking the generation beforehand makes the worker's
            # own pre-acquire check return first, so this never runs and the
            # arm under test is never reached.
            controller._cancel_preload_generation(generation)
            raise RuntimeError("the model runner exited")

    class Lease:
        transcriber = KilledTranscriber()

        def release(self):
            released.append(True)

    monkeypatch.setattr(
        controller, "_download_model_for_preload", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        controller, "_acquire_transcriber_runtime", lambda *_a, **_k: Lease()
    )

    controller._preload_model_worker(settings, generation, key)

    assert done and done[-1][1] is False
    assert "canceled" in done[-1][2].lower(), done
    assert "could not be loaded" not in done[-1][2]
    with controller._preload_result_lock:
        assert controller._preload_results[key][1] is None
    with controller._transcriber_runtime_state_lock:
        assert controller._pending_transcriber_cache_reset is True, (
            "the half-loaded runtime stayed in the cache and was reported as "
            "preloaded, so the next dictation transcribed with it"
        )
    with controller._preload_result_lock:
        controller._preload_future = None
    assert controller._local_model_preload_needed(settings) is True
    assert released == [True]
    controller.shutdown()
    _ = app


def test_the_preload_download_can_be_canceled_from_the_overlay(monkeypatch):
    """Cancel must reach the transcriber's *own* download, not just ours.

    The overlay's Cancel kills the preload's download subprocess, but a model
    that looks cached (or whose preload download failed and was swallowed as a
    warning) is fetched from the transcriber's load path instead, which waits
    on the machine-wide slot. Without a cancel check that wait is interruptible
    only by process shutdown, so Cancel did nothing at all.
    """
    controller, app, _overlay, settings = _preloading_controller(monkeypatch)
    generation = controller._preload_generation
    key = controller._model_preload_key(settings)
    seen: list[bool] = []

    class ProbingTranscriber:
        def __init__(self):
            self._cancel_check = None

        def set_cancel_check(self, cancel_check):
            self._cancel_check = cancel_check

        def preload_model(self):
            # Stands in for `run_coordinated_download(cancel_check=...)`.
            assert self._cancel_check is not None, "no cancel check was installed"
            seen.append(self._cancel_check())
            controller._cancel_preload_generation(generation)
            seen.append(self._cancel_check())

    transcriber = ProbingTranscriber()

    class Lease:
        def release(self):
            return None

    lease = Lease()
    lease.transcriber = transcriber
    monkeypatch.setattr(
        controller, "_download_model_for_preload", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        controller, "_acquire_transcriber_runtime", lambda *_a, **_k: lease
    )

    controller._preload_model_worker(settings, generation, key)

    assert seen == [False, True]
    assert transcriber._cancel_check is None
    controller.shutdown()
    _ = app


def test_a_finished_preload_stops_describing_a_phase(monkeypatch):
    """`_current_preload_phase` promises "" when no preload is running.

    Leaving the last phase behind meant it kept answering "load" for the rest
    of the session, so any later reader would describe a preload that ended.
    """
    controller, app, _overlay, _settings = _preloading_controller(monkeypatch)
    generation = controller._preload_generation
    controller._preload_phase = (generation, controller_module._PRELOAD_PHASE_LOAD)
    assert controller._current_preload_phase() == controller_module._PRELOAD_PHASE_LOAD

    controller._on_model_preload_done(generation, True, "Model loaded: x")

    assert controller._current_preload_phase() == ""
    controller.shutdown()
    _ = app


def test_switching_to_a_remote_engine_stops_describing_a_phase(monkeypatch):
    """The second place that ends a preload without a completion signal.

    `on_settings_changed` cancels the running preload outright when the new
    engine is remote, so `_on_model_preload_done` never runs for it. Leaving
    the phase behind made `_preload_phase_word()` keep answering "downloading"
    for a fetch that had been canceled.
    """
    controller, app, _overlay, settings = _preloading_controller(monkeypatch)
    controller._preload_phase = (
        controller._preload_generation,
        controller_module._PRELOAD_PHASE_DOWNLOAD,
    )
    assert controller._preload_phase_word() == "downloading"
    controller._settings_store._settings = replace(settings, engine="openai")

    controller.on_settings_changed()

    assert controller._current_preload_phase() == ""
    controller.shutdown()
    _ = app


def test_a_preload_never_hides_a_failed_hotkey_registration(monkeypatch):
    """`reload_settings` calls `show_idle_status` to reprint the hotkey.

    Gating the whole method on a running preload swallowed the four
    registration errors too -- the one thing a background model load must not
    hide, because the user's hotkey then silently does nothing.
    """
    controller, app, overlay, _settings = _preloading_controller(monkeypatch)
    controller._hotkey_registration_ok = False
    controller._hotkey_notice = "Ctrl+Alt+D is held by another program."

    controller.show_idle_status()

    assert overlay.states[-1][0] == "Error"
    assert "another program" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_a_running_preload_still_suppresses_the_plain_idle_line(monkeypatch):
    """The other half: with the hotkeys fine, the progress text stays."""
    controller, app, overlay, _settings = _preloading_controller(monkeypatch)
    controller._hotkey_registration_ok = True
    controller._cancel_hotkey_registration_ok = True
    controller._show_overlay_hotkey_registration_ok = True
    controller._repaste_hotkey_registration_ok = True

    controller.show_idle_status()

    assert overlay.states == []
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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


def test_transcribe_worker_still_reports_when_the_lease_close_dies(monkeypatch):
    """The terminal signal sits after the `finally` that releases the lease.

    So a lease whose close raised took the whole result with it: no
    `transcription_ready`, no `transcription_failed`, and the overlay left in
    Processing with no error and no Retry for the rest of the session. The
    guard is in `release()` rather than in each of the three workers that have
    this shape, and this is the integration check that it holds.
    """
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._executor = ImmediateExecutor()
    ready: list[tuple[int, str]] = []
    controller.transcription_ready.connect(
        lambda token, text: ready.append((token, text))
    )

    class Runtime:
        def transcribe_batch(self, wav):
            return "the dictated words"

        def close(self):
            raise BaseException("close died")

    def _lease(*_args, **_kwargs):
        controller._increment_transcriber_runtime_count()
        return controller_module._TranscriberRuntimeLease(
            controller,
            Runtime(),
            owns_shared_lock=False,
            close_on_release=True,
        )

    monkeypatch.setattr(controller, "_acquire_transcriber_runtime", _lease)

    settings_snapshot = AppSettings(engine="local", hotkey=FALLBACK_HOTKEY)
    controller._active_request_token = 1
    controller._transcribe_worker(1, b"audio", settings_snapshot)

    assert ready == [(1, "the dictated words")], (
        "the transcript was produced and then lost with the close error; the "
        f"overlay is still on {overlay.states[-1:]}"
    )
    assert controller._transcription_runtime_active() is False
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


def test_a_canceled_finalize_reports_a_cancel_even_if_the_abort_dies(monkeypatch):
    """What the abort does cannot change the fact that this was a cancel.

    `terminal_kind = "canceled"` used to be assigned *below* the abort, whose
    own `except Exception` does not cover a `BaseException` -- and
    `abort_stream()` drains a provider socket and runs its callbacks, so one
    escaping from a callback left `terminal_kind` at its `"failed"`
    initialiser. The user who had just pressed Cancel then got a tray
    notification reading "Recording ... failed: Unexpected streaming error".
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    canceled: list[int] = []
    failed: list[tuple[int, str]] = []
    controller.transcription_canceled.connect(canceled.append)
    controller.transcription_failed.connect(
        lambda token, message: failed.append((token, message))
    )

    class DyingTranscriber:
        def abort_stream(self):
            raise BaseException("the provider callback died")

    job = controller._register_transcription_job(7, settings, "streaming")
    job.aborting = True

    controller._finalize_stream_worker(7, DyingTranscriber(), job)

    assert canceled == [7], (failed, canceled)
    assert failed == [], "a cancel was reported to the user as a failure"
    controller.shutdown()
    _ = app


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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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
    # The default model, whatever it is -- this test is about the recording
    # being persisted and marked, not about which model transcribes it.
    assert last_recording_store.transcribing == [
        ("local", DEFAULT_MODEL_SIZE, "batch")
    ]
    assert submitted == [(1, b"RIFF", "batch", DEFAULT_MODEL_SIZE)]
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


def test_a_cancel_whose_capture_cannot_be_stopped_says_so(caplog):
    """A cancel must report a capture that refuses to stop, not swallow it.

    `AudioCapture.stop` concatenates every recorded chunk and encodes the WAV,
    so it raises on exactly the recording that is worth the most -- a long one
    that no longer fits in memory. The normal stop path logs that; this path
    caught it and did nothing, so the recording vanished while its own log
    line reported `audio_bytes=0`, which is what an instant cancel looks like.
    """
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)

    class _RefusingCapture(FakeCapture):
        def stop(self):
            raise RuntimeError("PortAudio refused to stop the stream")

    controller._audio_capture = _RefusingCapture()
    controller._streaming_recording = False

    with caplog.at_level(logging.ERROR, logger="test.controller"):
        controller.cancel_current_action()

    assert controller._audio_capture is None
    assert "Failed to stop active audio capture" in caplog.text
    # The cancel itself still completes.
    assert overlay.states[-1][0] == "Done"
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
    """It pastes into the resolved caret window, never at "whatever is in front".

    The target used to be `None`, i.e. the live foreground -- and this action's
    main entry point is the tray menu, which must call `SetForegroundWindow` on
    our own hidden host window before it opens. So the paste aimed at that
    window, and reported success.
    """
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    focus_helper = FakeWindowFocusHelper()
    controller, app = _make_controller(
        overlay=overlay,
        text_inserter=inserter,
        window_focus_helper=focus_helper,
    )
    beeps: list[bool] = []
    monkeypatch.setattr(
        controller, "_play_completion_beep", lambda: beeps.append(True)
    )
    controller._last_transcript = "hello again"

    controller.repaste_last_transcript()

    assert inserter.calls[-1] == (
        "hello again",
        focus_helper.current_caret,
        "auto",
    )
    assert focus_helper.restore_calls[-1] == focus_helper.current
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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


def test_a_remote_finalize_waits_behind_an_older_job_it_would_overtake():
    """The fast lane must not paste a later dictation before an earlier one.

    Everything else runs on the single shared worker, so results are delivered
    in recording order. A remote finalize on its own worker can finish while an
    older local job is still transcribing -- reachable by switching the engine
    between two dictations -- and would then be pasted first.
    """
    controller, app = _make_controller()
    try:
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

        # An older recording is still being transcribed.
        older_token = controller._next_request_token()
        older = controller._register_transcription_job(
            older_token,
            AppSettings(engine=DEFAULT_ENGINE),
            "batch",
        )
        older.future = concurrent.futures.Future()

        controller._active_stream_settings = AppSettings(engine="deepgram")
        controller._active_stream_transcriber = FakeStreamingTranscriber()
        controller._submit_stream_finalize()
        assert used == ["shared"], (
            "a remote finalize overtook an older, still running transcription"
        )

        # Once every older job has produced its result the fast lane is safe
        # again: a foreground delivery flushes older results before its own
        # paste. That includes the finalize job just submitted above, which is
        # itself an older job for the next one.
        for pending in list(controller._jobs.values()):
            if pending.future is not None and not pending.future.done():
                pending.future.set_result("older text")
        used.clear()
        controller._active_stream_settings = AppSettings(engine="deepgram")
        controller._active_stream_transcriber = FakeStreamingTranscriber()
        controller._submit_stream_finalize()
        assert used == ["finalize"]
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
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


def test_a_slow_handshake_is_still_aborted_when_the_microphone_fails(monkeypatch):
    """The abort must survive the case it exists for.

    With an instant fake transcriber the abort runs synchronously and every
    test passes. Only a handshake that is still in flight takes the detached
    path -- and there a generation check could never match, because the caller
    bumps the same counter one statement later. The abort was therefore skipped
    every time it actually mattered, the provider socket stayed published, and
    every later dictation failed with "Streaming session already active" until
    the app was restarted.
    """
    released = threading.Event()

    class _SlowToConnect(FakeStreamingTranscriber):
        def start_stream(self, on_partial=None, on_error=None):
            released.wait(timeout=5.0)
            return super().start_stream(on_partial=on_partial, on_error=on_error)

    transcriber = _SlowToConnect()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCaptureFails)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda _s, **kw: transcriber
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(
            AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A faster-whisper size explicitly: the default local model is
        # the batch-only Parakeet, which the controller refuses to stream.
        model_size="small",
    )
        ),
        overlay=FakeOverlay(),
    )
    try:
        controller.start_recording()
        # The capture failed while the handshake was still running.
        released.set()

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not transcriber.aborted:
            app.processEvents()
            time.sleep(0.01)

        assert transcriber.started is True, "the handshake never ran"
        assert transcriber.aborted is True, (
            "the provider session was left published; every later dictation "
            "would fail with 'Streaming session already active'"
        )
    finally:
        released.set()
        controller.shutdown()
    _ = app


def test_a_stale_abort_does_not_tear_down_a_newer_session():
    """The skip branch, which had no test at all.

    Two guards were already wrong here. A generation counter could never
    match, because the teardown bumps it one statement later. Object identity
    was wrong in both directions: `_active_stream_transcriber` is briefly None
    while the next session starts, and it is a shared cached object either
    way. The token is set only by a new handshake, so it answers exactly the
    question that matters.
    """
    controller, app = _make_controller(overlay=FakeOverlay())
    try:
        # (a) Nothing newer: the orphan must be aborted.
        orphan = FakeStreamingTranscriber()
        controller._stream_connect_token = object()
        controller._stream_connect_thread = None
        controller._teardown_pending_stream_connect(orphan)
        assert orphan.aborted is True, "an orphaned handshake was left published"

        # (b) A newer handshake claims the slot while the abort is still
        # joining. The detached aborter must leave it alone.
        superseded = FakeStreamingTranscriber()
        release = threading.Event()
        joining = threading.Event()

        def _slow_handshake():
            joining.set()
            release.wait(timeout=5.0)

        thread = threading.Thread(target=_slow_handshake, daemon=True)
        controller._stream_connect_token = object()
        controller._stream_connect_thread = thread
        thread.start()
        assert joining.wait(timeout=5.0)

        controller._teardown_pending_stream_connect(superseded)
        # A newer handshake starts before the aborter wakes.
        controller._stream_connect_token = object()
        release.set()
        thread.join(timeout=5.0)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not superseded.aborted:
            time.sleep(0.01)

        assert superseded.aborted is False, (
            "the abort tore down a session that a newer handshake owns"
        )
    finally:
        controller.shutdown()
    _ = app


def test_an_empty_streaming_result_keeps_the_previous_transcript():
    """A dictation that produced nothing must not erase the one before it.

    `_last_transcript` is what the tray's "Insert last transcript again" and
    the overlay's Copy act on. Assigning it before the empty check meant an
    empty streaming session reported "No transcript available" while the
    earlier dictation was still in history. The batch silence-gate path
    already got this right.
    """
    controller, app = _make_controller(overlay=FakeOverlay())
    try:
        controller._last_transcript = "die vorherige diktierte nachricht"
        controller._active_session_mode = "streaming"
        controller._streaming_recording = True

        controller._on_transcription_ready("")

        assert controller._last_transcript == "die vorherige diktierte nachricht", (
            "an empty streaming result erased the previous transcript"
        )
    finally:
        controller.shutdown()
    _ = app


@pytest.mark.parametrize("failure", [RuntimeError, _NotAnException])
def test_a_raising_cancel_hook_clear_still_releases_the_runtime(
    monkeypatch, caplog, failure
):
    """The clear runs in the preload's `finally`, right before `release()`.

    Unguarded, a setter that raises there skips the release and strands
    `_transcriber_runtime_lock` for the process lifetime: every later preload
    and audio import blocks forever, and every dictation quietly builds its
    own isolated multi-gigabyte runtime instead.

    Both classes, because the guard was `except Exception` and the release was
    a sibling statement rather than a `finally`: a `BaseException` from the
    setter walked straight past both. The preload's own body already has an
    `except BaseException` arm for exactly this reason; the `finally` below it
    re-opened the hole.
    """
    controller, app, _overlay, settings = _preloading_controller(monkeypatch)
    generation = controller._preload_generation
    key = controller._model_preload_key(settings)
    released: list[bool] = []
    calls: list[object] = []

    class HostileTranscriber:
        def set_cancel_check(self, cancel_check):
            calls.append(cancel_check)
            if cancel_check is None:
                raise failure("a setter that raises on the clear")

        def preload_model(self):
            return None

    class Lease:
        transcriber = HostileTranscriber()

        def release(self):
            released.append(True)

    monkeypatch.setattr(
        controller, "_download_model_for_preload", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        controller, "_acquire_transcriber_runtime", lambda *_a, **_k: Lease()
    )

    with caplog.at_level(logging.ERROR):
        controller._preload_model_worker(settings, generation, key)

    assert calls[-1] is None, "the clear was never attempted"
    assert released == [True], "the runtime lease was stranded"
    assert any(
        "cancel hook" in record.getMessage() for record in caplog.records
    ), "the failed clear was swallowed without a log line"
    controller.shutdown()
    _ = app


def test_a_failed_microphone_open_does_not_strand_the_runtime_lock(monkeypatch):
    """The capture is built after the lease block and before it is stored.

    Nothing owns the lease in that window. No statement in
    `_build_audio_capture` is known to raise today -- the stored threshold is
    already coerced and clamped by `AppSettings.from_dict` -- so this guards
    depth rather than a live trigger, and it is worth it because a leak strands
    `_transcriber_runtime_lock` for the process lifetime: every later preload
    and audio import blocks forever, every dictation quietly builds its own
    isolated runtime, and `_transcription_runtime_active()` stays True so no
    deferred cache reset ever runs.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        model_size="small",
    )
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

    def _explode(*_args, **_kwargs):
        raise ValueError("could not read vad_energy_threshold")

    monkeypatch.setattr(controller, "_build_audio_capture", _explode)

    controller.start_recording()
    app.processEvents()

    assert overlay.states[-1][0] == "Error"
    assert controller._transcription_runtime_active() is False, (
        "the runtime is still marked in use after a failed start"
    )
    # The decisive one: a later shared acquisition must not block.
    acquired = controller._transcriber_runtime_lock.acquire(timeout=2.0)
    try:
        assert acquired is True, "the runtime lock was stranded"
    finally:
        if acquired:
            controller._transcriber_runtime_lock.release()
    controller.shutdown()
    _ = app


def test_a_base_exception_in_the_worker_still_resolves_the_job(monkeypatch):
    """The terminal signal is emitted after the `try`, not inside it.

    Anything escaping that block leaves the overlay in Processing with no
    error and no Retry, and the job never resolves -- for the rest of the
    session. A cancel check or progress callback raising a `BaseException`
    is the reachable supplier.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)

    class HostileTranscriber:
        runtime_device = "cpu"

        def set_cancel_check(self, _cancel_check):
            return None

        def set_progress_callback(self, _callback):
            return None

        def transcribe_batch(self, _audio):
            raise BaseException("a callback that raises outside Exception")

        def close(self):
            return None

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: HostileTranscriber(),
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    failures: list[tuple[int, str]] = []
    controller.transcription_failed.connect(
        lambda token, message: failures.append((token, message))
    )

    token = controller._active_request_token or 1
    controller._transcribe_worker(token, b"RIFF", settings)
    app.processEvents()

    assert failures, "the job never resolved; the overlay would stay in Processing"
    assert "BaseException" in failures[-1][1]
    controller.shutdown()
    _ = app


def test_a_failed_microphone_open_does_not_orphan_the_provider_session(monkeypatch):
    """Releasing the lease is not the whole cleanup.

    `_begin_stream_connect` has already spawned the handshake, so `start_stream`
    completes and publishes a session nobody owns. Every streaming provider
    refuses a second session, so the *next* dictation fails with "Streaming
    session already active" and its audio is only preserved for Retry -- and a
    remote provider's socket stays open in the meantime.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        model_size="small",
    )
    overlay = FakeOverlay()
    transcriber = FakeStreamingTranscriber()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: transcriber,
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    def _explode(*_args, **_kwargs):
        raise ValueError("could not open the microphone")

    monkeypatch.setattr(controller, "_build_audio_capture", _explode)

    controller.start_recording()
    app.processEvents()

    assert overlay.states[-1][0] == "Error"
    assert transcriber.started is False or transcriber.aborted is True, (
        "the handshake published a session that nothing owns: started="
        f"{transcriber.started} aborted={transcriber.aborted}"
    )
    controller.shutdown()
    _ = app


def test_a_base_exception_while_acquiring_the_runtime_does_not_strand_the_lock(
    monkeypatch,
):
    """`_acquire_transcriber_runtime` cleans up under `except Exception`.

    Its two arms only undo their own bookkeeping and re-raise, so the narrower
    catch bought nothing and cost everything: a `BaseException` from
    `create_transcriber` skipped `release()` and left
    `_transcriber_runtime_lock` held for the process lifetime. The worker's own
    `finally` cannot help -- the lease is still `None` when the acquire raises.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    def _explode(*_args, **_kwargs):
        raise BaseException("runtime construction died")

    monkeypatch.setattr(controller, "_get_or_create_transcriber", _explode)

    with pytest.raises(BaseException, match="runtime construction died"):
        controller._acquire_transcriber_runtime(settings)

    assert controller._transcription_runtime_active() is False, (
        "the runtime is still marked in use after a failed acquisition"
    )
    acquired = controller._transcriber_runtime_lock.acquire(timeout=2.0)
    try:
        assert acquired is True, "the runtime lock was stranded"
    finally:
        if acquired:
            controller._transcriber_runtime_lock.release()
    controller.shutdown()
    _ = app


def test_a_base_exception_from_the_isolated_runtime_does_not_leak_the_use_count(
    monkeypatch,
):
    """The other arm of `_acquire_transcriber_runtime`, which had no test.

    The existing test patches `_get_or_create_transcriber`, which only the
    *shared-lock* arm calls. The isolated arm is the one that actually reaches
    `create_transcriber`, and its guard was never exercised. It holds no lock,
    but the runtime count it increments gates
    `_transcription_runtime_active()`, and while that reads True no deferred
    transcriber-cache reset can ever run.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    def _explode(*_args, **_kwargs):
        raise BaseException("isolated runtime construction died")

    monkeypatch.setattr("stt_app.controller.create_transcriber", _explode)

    # Hold the shared lock so the acquisition is forced down the isolated arm.
    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True
    try:
        with pytest.raises(BaseException, match="isolated runtime construction died"):
            controller._acquire_transcriber_runtime(settings)
    finally:
        controller._transcriber_runtime_lock.release()

    assert controller._transcription_runtime_active() is False, (
        "the isolated arm left the runtime marked in use"
    )
    controller.shutdown()
    _ = app


def test_a_lease_that_cannot_be_constructed_closes_the_isolated_runtime(monkeypatch):
    """The guard was widened to span the lease constructor; nothing drove it.

    `create_transcriber` raising was already covered -- the pre-widening code
    decremented on that path too, so the existing test passes against it. What
    the widening actually added is coverage for a raise *between* the
    successful construction and the lease, and that arm has a second job the
    shared arm does not: the runtime it just built is isolated, so no cache
    holds it. A Node child process or an ONNX session would stay alive with
    nothing able to close it.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    closed: list[str] = []

    class Runtime:
        def close(self):
            closed.append("closed")

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda *_a, **_k: Runtime()
    )

    def _explode(*_args, **_kwargs):
        raise BaseException("lease construction died")

    monkeypatch.setattr("stt_app.controller._TranscriberRuntimeLease", _explode)

    # Hold the shared lock so the acquisition is forced down the isolated arm.
    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True
    try:
        with pytest.raises(BaseException, match="lease construction died"):
            controller._acquire_transcriber_runtime(settings)
    finally:
        controller._transcriber_runtime_lock.release()

    assert closed == ["closed"], (
        "the isolated runtime was orphaned: no lease and no cache reference, "
        "so nothing can ever close it"
    )
    assert controller._transcription_runtime_active() is False, (
        "the isolated arm left the runtime marked in use"
    )
    controller.shutdown()
    _ = app


def test_a_runtime_that_refuses_to_close_still_gives_back_the_use_count(monkeypatch):
    """The close must not be able to skip the decrement.

    `_close_cached_transcriber` swallows `Exception`, so only a `BaseException`
    out of `close()` reaches this -- and while the count stays raised
    `_transcription_runtime_active()` reads True, which blocks every deferred
    transcriber-cache reset for the process lifetime.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    class StubbornRuntime:
        def close(self):
            raise BaseException("close died")

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda *_a, **_k: StubbornRuntime()
    )

    def _explode(*_args, **_kwargs):
        raise BaseException("lease construction died")

    monkeypatch.setattr("stt_app.controller._TranscriberRuntimeLease", _explode)

    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True
    try:
        with pytest.raises(BaseException):
            controller._acquire_transcriber_runtime(settings)
    finally:
        controller._transcriber_runtime_lock.release()

    assert controller._transcription_runtime_active() is False
    controller.shutdown()
    _ = app


def test_a_base_exception_starting_the_stream_does_not_strand_the_runtime_lock(
    monkeypatch,
):
    """The outermost frame that holds the lease caught only `Exception`.

    Two guards below this one were widened to `BaseException` precisely to
    stop the lease being stranded; this block acquires the very lease they
    protect and `_begin_stream_connect` starts a thread, so `Thread.start`
    raising `RuntimeError`... is an `Exception`. What is not: anything a
    provider callback can raise on the way out. While
    `_transcriber_runtime_lock` is held, every later preload and audio import
    blocks forever and every dictation builds its own isolated runtime.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        model_size="small",
        mode="streaming",
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda *_a, **_k: FakeStreamingTranscriber(),
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    def _explode(*_args, **_kwargs):
        raise BaseException("the handshake died")

    monkeypatch.setattr(controller, "_begin_stream_connect", _explode)

    with pytest.raises(BaseException, match="the handshake died"):
        controller._start_streaming_recording()

    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True, (
        "the shared runtime lock was stranded for the process lifetime"
    )
    controller._transcriber_runtime_lock.release()
    assert controller._transcription_runtime_active() is False
    controller.shutdown()
    _ = app


def test_a_base_exception_starting_the_stream_tears_down_the_handshake(monkeypatch):
    """Releasing the lease is not enough; the handshake is already running.

    `_begin_stream_connect` has spawned it by the time anything below can
    fail, so `start_stream` completes and publishes a session nobody owns.
    Every streaming provider refuses a second session, so the next dictation
    fails with "Streaming session already active" until the app restarts, and
    a remote provider's socket stays open and billed until then. The two arms
    below the capture guard have done this teardown for that reason; the
    `BaseException` arm was added without it.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        model_size="small",
        mode="streaming",
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda *_a, **_k: FakeStreamingTranscriber(),
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    torn_down: list[object] = []

    def _explode(*_args, **_kwargs):
        raise BaseException("the handshake died")

    monkeypatch.setattr(controller, "_begin_stream_connect", _explode)
    monkeypatch.setattr(
        controller,
        "_teardown_pending_stream_connect",
        lambda transcriber: torn_down.append(transcriber),
    )

    with pytest.raises(BaseException, match="the handshake died"):
        controller._start_streaming_recording()

    assert len(torn_down) == 1, (
        "the handshake was left published on the shared transcriber, so every "
        "later dictation is refused with 'Streaming session already active'"
    )
    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True
    controller._transcriber_runtime_lock.release()
    controller.shutdown()
    _ = app


def test_a_base_exception_before_the_transcriber_exists_reports_the_real_failure(
    monkeypatch,
):
    """The teardown arm is reachable before `transcriber` is bound.

    `_acquire_transcriber_runtime` is inside the same `try`, so a
    `BaseException` from it reaches the arm that tears the handshake down,
    before `transcriber` would otherwise exist.

    What this pins is the behaviour: the real failure reaches the caller and
    the shared lock comes back. It does **not** pin the pre-binding of
    `transcriber` -- measured, removing that line and its `is not None` guard
    together leaves this test green, because the teardown's own
    `except BaseException` catches the `UnboundLocalError` from evaluating the
    argument. The binding is kept for the log, not for the control flow.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        model_size="small",
        mode="streaming",
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    def _explode(*_args, **_kwargs):
        raise BaseException("acquisition died")

    monkeypatch.setattr(controller, "_acquire_transcriber_runtime", _explode)

    with pytest.raises(BaseException, match="acquisition died"):
        controller._start_streaming_recording()

    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True
    controller._transcriber_runtime_lock.release()
    controller.shutdown()
    _ = app


def test_a_lease_whose_close_dies_still_hands_the_runtime_back(monkeypatch):
    """The close and the hand-back were two statements with no `finally`.

    `_close_cached_transcriber` swallows `Exception` but not `BaseException`,
    and `release()` marks itself released before either runs, so a close that
    died skipped `_release_transcriber_runtime` permanently and a retry was a
    no-op. That strands `_transcriber_runtime_lock` for the process lifetime:
    every later preload and audio import blocks forever, every dictation
    silently builds its own isolated runtime, and no deferred cache reset runs.

    It is the same hazard as the guard in `_acquire_transcriber_runtime`, one
    frame further out, and it is the shared root of three symptoms: every
    worker calls `release()` from a `finally` that sits *outside* its own
    `except BaseException` arm, so the escaping exception also swallowed the
    terminal signal.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    class StubbornRuntime:
        def close(self):
            raise BaseException("close died")

    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True
    controller._increment_transcriber_runtime_count()
    lease = controller_module._TranscriberRuntimeLease(
        controller,
        StubbornRuntime(),
        owns_shared_lock=True,
        close_on_release=True,
    )

    # It must not raise either: every caller reaches `release()` from a
    # `finally` that sits outside its own `except BaseException` arm and emits
    # its terminal signal afterwards, so a raising release swallowed that
    # signal -- the overlay stuck in Processing with no error and no Retry.
    lease.release()

    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True, (
        "the shared runtime lock was stranded for the process lifetime"
    )
    controller._transcriber_runtime_lock.release()
    assert controller._transcription_runtime_active() is False
    controller.shutdown()
    _ = app


def test_a_deferred_reset_that_dies_still_lets_the_worker_report(monkeypatch):
    """Guarding only the lease's own close left the same failure a second door.

    `release()` hands back through `_release_transcriber_runtime`, and that
    applies the deferred cache reset -- which closes the *cached* transcriber
    through the same `_close_cached_transcriber` that swallows `Exception` but
    not `BaseException`. So the exact failure the `try` above it was added to
    survive still escaped, and the caller is a worker that emits its terminal
    signal after this call: overlay stuck in Processing with no error and no
    Retry, or `_streaming_recording` stuck True.

    The lease's own runtime is deliberately fine here and
    `close_on_release=False`, so only the deferred reset can be the cause.

    Two things must survive the swallow: the admission lock, handed back
    inside that method's own `finally` before anything can escape, and the
    pending flag, which a failed reset leaves set so the next release retries.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    class StubbornRuntime:
        def close(self):
            raise BaseException("close died")

    controller._transcriber_cache = StubbornRuntime()
    with controller._transcriber_runtime_state_lock:
        controller._pending_transcriber_cache_reset = True

    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True
    controller._increment_transcriber_runtime_count()
    lease = controller_module._TranscriberRuntimeLease(
        controller,
        object(),
        owns_shared_lock=True,
        close_on_release=False,
    )

    lease.release()

    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True, (
        "the shared runtime lock was stranded for the process lifetime"
    )
    controller._transcriber_runtime_lock.release()
    assert controller._transcription_runtime_active() is False
    with controller._transcriber_runtime_state_lock:
        assert controller._pending_transcriber_cache_reset is True, (
            "a reset that failed must stay pending so the next release retries"
        )

    # The stubborn runtime is still cached, and shutdown would close it again.
    controller._transcriber_cache = None
    controller.shutdown()
    _ = app


def test_a_shutdown_during_construction_closes_the_runtime_exactly_once(monkeypatch):
    """Clearing `orphan` after the close let the except arm close it again.

    `_close_cached_transcriber` swallows `Exception` but not `BaseException`,
    so a close that died left `orphan` still set, the outer arm closed the same
    runtime a second time, and that second raise replaced the first -- the
    caller got the close's exception instead of the `TranscriptionCanceled`
    this branch exists to deliver.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    closes: list[str] = []

    class Runtime:
        def close(self):
            # It has to *raise*: `_close_cached_transcriber` swallows
            # `Exception`, so with a close that succeeds the ordering of the
            # two statements cannot matter and the test proves nothing. A
            # first version of this test used a quiet close and passed under
            # its own mutation.
            closes.append("close")
            raise BaseException("close died")

    def _create(*_args, **_kwargs):
        # Shutdown observed *during* construction: the only way into that
        # branch, and the real sequence when the user quits mid-load.
        controller._shutdown_started = True
        return Runtime()

    monkeypatch.setattr("stt_app.controller.create_transcriber", _create)

    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True
    try:
        # The close's own exception is what escapes -- it is a `BaseException`
        # and more urgent than the shutdown notice. What must not happen is a
        # *second* close of the same runtime.
        with pytest.raises(BaseException, match="close died"):
            controller._acquire_transcriber_runtime(settings)
    finally:
        controller._transcriber_runtime_lock.release()

    assert closes == ["close"], f"the runtime was closed {len(closes)} times"
    controller._shutdown_started = False
    controller.shutdown()
    _ = app


def test_a_lease_that_cannot_be_constructed_frees_the_shared_lock(monkeypatch):
    """The shared arm's half of the widened guard, which had no test either.

    Before the guard was widened, `close_on_release` and the
    `_TranscriberRuntimeLease` construction sat *below* the `try`, so a raise
    in either released nothing: `_transcriber_runtime_lock` stayed held for the
    process lifetime, every later preload and audio import blocked forever, and
    `_transcription_runtime_active()` stayed True so no deferred cache reset
    could run. The isolated arm has its own test; this is the shared one, which
    is the arm that actually holds the lock.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    class Runtime:
        pass

    monkeypatch.setattr(
        controller, "_get_or_create_transcriber", lambda *_a, **_k: Runtime()
    )

    def _explode(*_args, **_kwargs):
        raise BaseException("lease construction died")

    monkeypatch.setattr("stt_app.controller._TranscriberRuntimeLease", _explode)

    # No lock held here, so the acquisition takes the shared arm.
    with pytest.raises(BaseException, match="lease construction died"):
        controller._acquire_transcriber_runtime(settings)

    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True, (
        "the shared runtime lock was never given back"
    )
    controller._transcriber_runtime_lock.release()
    assert controller._transcription_runtime_active() is False
    controller.shutdown()
    _ = app


def test_a_failed_capture_start_releases_the_lease_even_if_the_teardown_raises(
    monkeypatch,
):
    """The *second* capture-failure arm, which the sibling test never reaches.

    Both arms were widened together, but the existing test fails
    `_build_audio_capture`, which returns before `capture.start()` is ever
    called -- so reverting this arm's guard left the whole suite green.
    Reaching it needs the capture to be built successfully and then fail on
    `start()` with `AudioCaptureError`, which is what a microphone grabbed by
    another application actually does.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        model_size="small",
        mode="streaming",
    )
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: FakeStreamingTranscriber(),
    )

    class _RefusingCapture(FakeCapture):
        def start(self):
            raise AudioCaptureError("the microphone is in use by another program")

    monkeypatch.setattr(
        controller, "_build_audio_capture", lambda **_kwargs: _RefusingCapture()
    )

    def _explode_teardown(*_args, **_kwargs):
        raise RuntimeError("could not start the abort thread")

    monkeypatch.setattr(
        controller, "_teardown_pending_stream_connect", _explode_teardown
    )

    controller.toggle_recording()
    app.processEvents()

    assert overlay.states and overlay.states[-1][0] == "Error", (
        "the capture failure was never reported: the exception escaped the Qt "
        f"slot and left the overlay where it was: {overlay.states[-1:]}"
    )
    assert controller._transcription_runtime_active() is False, (
        "the runtime stayed marked in use after the capture failed"
    )
    acquired = controller._transcriber_runtime_lock.acquire(timeout=2.0)
    try:
        assert acquired is True, "the runtime lock was stranded"
    finally:
        if acquired:
            controller._transcriber_runtime_lock.release()
    controller.shutdown()
    _ = app


def test_a_failed_streaming_capture_releases_the_lease_even_if_the_teardown_raises(
    monkeypatch,
):
    """The teardown was put *in front of* `release()`, which made this reachable.

    Before that change `runtime_lease.release()` was the first statement in
    the arm, so nothing could come between the failure and the release. The
    teardown reaches provider code (`abort_stream`) and starts a thread, and
    `Thread.start` raises `RuntimeError` when the process cannot create one --
    so a plain `Exception` was enough to strand `_transcriber_runtime_lock`
    for the process lifetime: every later preload and audio import blocks
    forever, and every dictation silently builds its own isolated runtime.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        model_size="small",
        mode="streaming",
    )
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: FakeStreamingTranscriber(),
    )

    def _explode_capture(*_args, **_kwargs):
        raise RuntimeError("microphone is gone")

    def _explode_teardown(*_args, **_kwargs):
        raise RuntimeError("could not start the abort thread")

    monkeypatch.setattr(controller, "_build_audio_capture", _explode_capture)
    monkeypatch.setattr(
        controller, "_teardown_pending_stream_connect", _explode_teardown
    )

    controller.toggle_recording()
    app.processEvents()

    assert overlay.states and overlay.states[-1][0] == "Error", (
        "the capture failure was never reported: the exception escaped the Qt "
        f"slot and left the overlay where it was: {overlay.states[-1:]}"
    )
    assert controller._transcription_runtime_active() is False, (
        "the runtime stayed marked in use after the capture failed"
    )
    acquired = controller._transcriber_runtime_lock.acquire(timeout=2.0)
    try:
        assert acquired is True, "the runtime lock was stranded"
    finally:
        if acquired:
            controller._transcriber_runtime_lock.release()
    controller.shutdown()
    _ = app


def test_the_stream_connect_teardown_never_raises_into_its_caller(monkeypatch):
    """Every caller is an error path with a lease to release."""
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    class _ExplodingTranscriber:
        def abort_stream(self):
            raise BaseException("provider abort exploded")

    # No connect thread, so `_abort` runs inline -- the common case, because
    # the handshake has usually finished by the time the capture fails.
    controller._teardown_pending_stream_connect(_ExplodingTranscriber())

    controller.shutdown()
    _ = app


def test_a_canceled_preload_that_dies_with_a_base_exception_is_not_a_broken_model():
    """A cancel must not be persisted as a load failure.

    `_record_model_preload_result(key, generation, failure)` *stores* that
    string and `toggle_recording` reads it before every dictation, so the
    user who pressed Cancel got a hard "could not be loaded" error on their
    next recording instead of a retry. The `except Exception` arm checks for
    this; the `except BaseException` arm was copied from it without the check.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    done: list[tuple[int, bool, str]] = []
    controller.model_preload_done.connect(
        lambda generation, ok, message: done.append((generation, ok, message))
    )

    generation = controller._preload_generation

    def _explode(*_args, **_kwargs):
        # The cancel has to arrive *during* the load, which is the real
        # sequence: the user presses Cancel while the model is loading and the
        # load dies. Marking the generation canceled beforehand instead makes
        # the worker's own pre-acquire check return first, so this function is
        # never called and the arm under test never runs -- the test then
        # passes with or without the fix.
        controller._cancel_preload_generation(generation)
        raise BaseException("preload died")

    controller._acquire_transcriber_runtime = _explode  # type: ignore[assignment]
    key = controller._model_preload_key(settings)

    controller._preload_model_worker(settings, generation, key)

    assert done and done[0][1] is False
    assert "canceled" in done[0][2].lower(), done
    assert controller._model_preload_failure(settings) is None, (
        "a cancel was persisted as a broken model, so the next dictation "
        "re-raises it instead of retrying"
    )
    controller.shutdown()
    _ = app


def test_a_base_exception_in_the_stream_finalizer_still_resolves_the_session(
    monkeypatch,
):
    """The terminal signal sits after the `finally` here too.

    Without a last-resort arm the session never resolves: `_streaming_recording`
    stays True and every later hotkey press is refused with "Streaming
    transcript is still finalizing" until Cancel or a restart. `stop_stream()`
    drains a provider socket and runs its callbacks, which is the supplier.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        model_size="small",
    )
    overlay = FakeOverlay()
    transcriber = FakeStreamingTranscriber(stop_raises=BaseException("socket died"))
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: transcriber,
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    terminal: list[tuple[str, object]] = []
    controller.transcription_failed.connect(
        lambda token, text: terminal.append(("failed", text))
    )
    controller.transcription_ready.connect(
        lambda token, text: terminal.append(("ready", text))
    )
    controller.transcription_canceled.connect(
        lambda token: terminal.append(("canceled", None))
    )

    controller.start_recording()
    app.processEvents()
    controller.stop_recording()
    for _ in range(60):
        app.processEvents()
        if terminal:
            break
        time.sleep(0.05)

    assert terminal, (
        "the streaming session never resolved: no terminal signal was emitted, "
        "so every later hotkey press is refused"
    )
    assert terminal[0][0] == "failed"
    controller.shutdown()
    _ = app


def test_a_failed_microphone_open_in_batch_mode_shows_an_error(monkeypatch):
    """The batch path built its capture outside any `try`.

    PySide6 prints the traceback and continues, so the app survived showing
    "Listening" -- "Starting dictation. Please wait..." -- forever, with no
    error text at all and every retry reproducing it.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    def _explode(*_args, **_kwargs):
        raise ValueError("could not read vad_energy_threshold")

    monkeypatch.setattr(controller, "_build_audio_capture", _explode)

    controller.start_recording()
    app.processEvents()

    assert overlay.states[-1][0] == "Error", (
        f"the overlay is stuck on {overlay.states[-1][0]!r} with no error text"
    )
    assert controller._audio_capture is None
    controller.shutdown()
    _ = app


def test_a_base_exception_in_the_preload_still_reports_the_preload_as_done(
    monkeypatch,
):
    """`model_preload_done` is emitted after the `except` arms, not in `finally`.

    Without a last-resort arm the preload never resolves, so `_preload_phase`
    keeps answering for a preload that ended -- breaking its documented "empty
    when none is running" contract -- and the recording-start notice names a
    phase forever.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)

    class _DyingTranscriber(FakeStreamingTranscriber):
        def preload_model(self):
            raise BaseException("preload died")

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: _DyingTranscriber(),
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    done: list[tuple[int, bool, str]] = []
    controller.model_preload_done.connect(
        lambda generation, ok, message: done.append((generation, ok, message))
    )

    generation = controller._preload_generation
    key = controller._model_preload_key(settings)
    controller._preload_model_worker(settings, generation, key)
    app.processEvents()

    assert done, "the preload never resolved: model_preload_done was not emitted"
    assert done[0][1] is False
    app.processEvents()
    assert controller._current_preload_phase() == "", (
        "a preload that ended still reports a phase: "
        f"{controller._current_preload_phase()!r}"
    )
    controller.shutdown()
    _ = app


def test_a_runtime_that_cannot_be_closed_is_still_evicted_from_the_cache():
    """A close that raises must not leave the dead runtime in the cache.

    `_close_cached_transcriber` swallows `Exception` but not `BaseException`,
    and the two statements that clear the cache sat *after* it -- so the same
    dead object was handed to every later acquisition and closed again on
    every reset. Not a one-off failure: the app was permanently unable to
    transcribe, one traceback per attempt.

    This is also the only eviction a *replaced* API key gets. `has_*_key` does
    not change when a key is swapped for a different value, so the identity is
    byte-identical and the reset is the whole invalidation -- a runtime holding
    a revoked credential went on serving requests.
    """
    controller, app = _make_controller()
    closes: list[str] = []

    class Runtime:
        def close(self):
            closes.append("close")
            raise _NotAnException("close died")

    controller._transcriber_cache = Runtime()
    controller._transcriber_cache_key = ("local", "small")

    assert controller._transcriber_runtime_lock.acquire(timeout=2.0) is True
    try:
        with pytest.raises(_NotAnException):
            controller._reset_transcriber_cache_locked()
    finally:
        controller._transcriber_runtime_lock.release()

    assert closes == ["close"], "the close was never attempted"
    assert controller._transcriber_cache is None, (
        "the runtime that could not be closed is still cached, so every later "
        "acquisition gets the same dead object"
    )
    assert controller._transcriber_cache_key is None
    controller.shutdown()
    _ = app


def test_a_failing_use_count_decrement_still_frees_the_admission_lock(monkeypatch):
    """The shared arm handed the two resources back as bare statements.

    The decrement came first and is the riskier of the two, so a raise from it
    stranded `_transcriber_runtime_lock` for the process lifetime -- the exact
    outcome the comment above that arm says it exists to make impossible. The
    isolated arm below it already used `try/finally` and says why.
    """
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, engine="local", model_size="small")
    controller, app = _make_controller(settings_store=FakeSettingsStore(settings))

    def _no_transcriber(_settings):
        raise RuntimeError("the runtime could not be built")

    def _explode_decrement():
        raise _NotAnException("the decrement died")

    monkeypatch.setattr(controller, "_get_or_create_transcriber", _no_transcriber)
    monkeypatch.setattr(
        controller, "_decrement_transcriber_runtime_count", _explode_decrement
    )

    with pytest.raises(_NotAnException):
        controller._acquire_transcriber_runtime(settings, allow_isolated=False)

    acquired = controller._transcriber_runtime_lock.acquire(timeout=2.0)
    try:
        assert acquired is True, (
            "the admission lock was stranded, so every later preload and audio "
            "import blocks forever"
        )
    finally:
        if acquired:
            controller._transcriber_runtime_lock.release()
    controller.shutdown()
    _ = app


def test_a_failing_lock_release_still_gives_the_use_count_back(monkeypatch):
    """The other half of the same pair, in `_release_transcriber_runtime`.

    The count gates `_transcription_runtime_active()`, which blocks every
    deferred cache reset while it reads True -- so losing the decrement means
    a changed model or a replaced API key never takes effect again.
    """
    controller, app = _make_controller()

    class _HostileLock:
        def release(self):
            raise _NotAnException("release died")

    controller._increment_transcriber_runtime_count()
    assert controller._transcription_runtime_active() is True
    monkeypatch.setattr(controller, "_transcriber_runtime_lock", _HostileLock())

    with pytest.raises(_NotAnException):
        controller._release_transcriber_runtime(owns_shared_lock=True)

    assert controller._transcription_runtime_active() is False, (
        "the use count was never given back"
    )
    controller._shutdown_started = True
    _ = app


def test_a_failing_teardown_step_still_shuts_the_executors_down(monkeypatch):
    """`shutdown()` is wired to `aboutToQuit`, so this is the last chance.

    Every other teardown step in it carries its own guard; the last four
    statements did not, so a failure in the first skipped all three executor
    shutdowns and left the transcription, stream-finalize and preload workers
    running past application exit.
    """
    controller, app = _make_controller()
    shut: list[str] = []

    class _RecordingExecutor:
        def __init__(self, name):
            self._name = name

        def shutdown(self, wait=True, cancel_futures=False):
            shut.append(self._name)

    controller._executor = _RecordingExecutor("transcription")
    controller._stream_finalize_executor = _RecordingExecutor("stream")
    controller._preload_executor = _RecordingExecutor("preload")

    def _explode():
        raise _NotAnException("the streaming reset died")

    monkeypatch.setattr(controller, "_reset_streaming_state", _explode)

    controller.shutdown()

    assert shut == ["transcription", "stream", "preload"], (
        f"the executors were left running past shutdown: {shut}"
    )
    _ = app


def test_a_failing_cache_reset_after_resume_still_restarts_the_microphone(
    monkeypatch,
):
    """The three resume steps are independent and ran as one sequence.

    A failing cache reset therefore also cost the warm-microphone restart, and
    audio devices commonly change identity across suspend -- so the next
    recording attached to a stream opened against a device that no longer
    exists.
    """
    controller, app = _make_controller()
    ran: list[str] = []

    monkeypatch.setattr(
        controller, "refresh_hotkey_registration", lambda: ran.append("hotkeys")
    )

    def _explode():
        raise _NotAnException("the resume cache reset died")

    monkeypatch.setattr(
        controller, "_reset_resume_sensitive_transcriber_cache", _explode
    )
    monkeypatch.setattr(
        controller,
        "_restart_warm_microphone_stream_after_resume",
        lambda: ran.append("warm stream"),
    )

    controller.handle_system_resume()

    assert ran == ["hotkeys", "warm stream"], (
        f"a failing step took the others with it: {ran}"
    )
    controller.shutdown()
    _ = app


def test_a_worker_whose_diagnostics_die_still_releases_and_reports(monkeypatch):
    """Forty lines of diagnostics ran between the result and `release()`.

    Three `getattr` property reads off the transcriber, a `len`, a log call and
    the two hook clears, with `runtime_lease.release()` as the last statement.
    A raise from any of them stranded the admission lock for the process
    lifetime *and* skipped the terminal signal below the `finally`, leaving the
    overlay in Processing with no error and no Retry -- from an ordinary
    `Exception`, not only an interrupt.
    """
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    controller._executor = ImmediateExecutor()
    ready: list[tuple[int, str]] = []
    controller.transcription_ready.connect(
        lambda token, text: ready.append((token, text))
    )

    class Runtime:
        def transcribe_batch(self, wav):
            return "the dictated words"

        @property
        def runtime_device(self):
            raise RuntimeError("the device property exploded")

    def _lease(*_args, **_kwargs):
        controller._increment_transcriber_runtime_count()
        return controller_module._TranscriberRuntimeLease(
            controller,
            Runtime(),
            owns_shared_lock=False,
            close_on_release=False,
        )

    monkeypatch.setattr(controller, "_acquire_transcriber_runtime", _lease)

    settings_snapshot = AppSettings(engine="local", hotkey=FALLBACK_HOTKEY)
    controller._active_request_token = 1
    controller._transcribe_worker(1, b"audio", settings_snapshot)

    assert ready == [(1, "the dictated words")], (
        "the transcript was produced and then lost in the bookkeeping; the "
        f"overlay is still on {overlay.states[-1:]}"
    )
    assert controller._transcription_runtime_active() is False, (
        "the runtime stayed marked in use, so no deferred cache reset can run"
    )
    controller.shutdown()
    _ = app


def test_a_resume_teardown_evicts_the_runtime_even_when_its_close_dies():
    """The resume path is a second copy of the same eviction, and it drifted.

    It exists precisely because a GPU runtime may be unusable after a suspend,
    which is also when its `close()` is most likely to fail -- and with the
    close in front of the two clears, that left the dead runtime in the cache
    and the next dictation was handed it straight back.
    """
    controller, app = _make_controller()
    closes: list[str] = []

    class Runtime:
        model_size = "cohere-transcribe-03-2026"
        runtime_device = "webgpu"

        def close(self):
            closes.append("close")
            raise _NotAnException("close died after resume")

    controller._transcriber_cache = Runtime()
    controller._transcriber_cache_key = None

    with pytest.raises(_NotAnException):
        controller._reset_resume_sensitive_transcriber_cache()

    assert closes == ["close"], "the teardown branch was never reached"
    assert controller._transcriber_cache is None, (
        "the runtime that could not be closed is still cached after resume"
    )
    acquired = controller._transcriber_runtime_lock.acquire(timeout=2.0)
    try:
        assert acquired is True, "the admission lock was stranded"
    finally:
        if acquired:
            controller._transcriber_runtime_lock.release()
    controller.shutdown()
    _ = app


@pytest.mark.parametrize("mode", ["batch", "streaming"])
@pytest.mark.parametrize(
    ("label", "repairable", "expected_refreshes"),
    [
        ("PortAudio is not answering", True, 1),
        ("the microphone is in use by another program", False, 0),
    ],
)
def test_a_capture_failure_re_enumerates_only_when_that_can_repair_it(
    monkeypatch, mode, label, repairable, expected_refreshes
):
    """A silent PortAudio is a state this app can undo; a busy mic is not.

    `try_refresh_input_devices` terminates PortAudio and returns False when
    the following initialize fails, leaving it down for the process lifetime
    -- after which *every* recording fails the same way and the microphone
    picker is empty. Re-enumerating initializes it again, so the repair turns
    a permanently deaf app into one failed recording. Requesting it for an
    unrelated failure would be the opposite mistake: tearing PortAudio down
    and back up because another program holds the microphone.

    Both capture-failure arms are driven, because the two are separate code
    and a guard added to one of them has already been missed before.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        model_size="small",
        mode=mode,
    )
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: FakeStreamingTranscriber(),
    )

    class _RefusingCapture(FakeCapture):
        def start(self):
            raise AudioCaptureError(
                label, audio_system_unavailable=repairable
            )

    monkeypatch.setattr(
        controller, "_build_audio_capture", lambda **_kwargs: _RefusingCapture()
    )
    refreshes: list[int] = []
    monkeypatch.setattr(
        controller,
        "request_audio_device_refresh",
        lambda: refreshes.append(1),
    )

    controller.toggle_recording()
    app.processEvents()

    assert overlay.states and overlay.states[-1][0] == "Error"
    assert overlay.states[-1][1] == label, (
        "the wording the user sees must be the one the capture raised"
    )
    assert len(refreshes) == expected_refreshes, (
        f"{mode}: {len(refreshes)} re-enumerations for {label!r}"
    )
    controller.shutdown()
    _ = app


class _RefusingExecutor:
    """An executor that cannot schedule anything.

    `ThreadPoolExecutor.submit` raises `RuntimeError` once the pool has been
    shut down, and again when the interpreter cannot start a worker thread.
    """

    def __init__(self, message="cannot schedule new futures after shutdown"):
        self.message = message

    def submit(self, *_args, **_kwargs):
        raise RuntimeError(self.message)

    def shutdown(self, *_args, **_kwargs):
        pass


def test_a_stream_finalize_that_cannot_be_scheduled_hands_the_runtime_back():
    """Nothing else would ever run the worker's `finally`.

    The job carries the live stream's runtime lease, so an unscheduled worker
    holds `_transcriber_runtime_lock` for the process lifetime: every later
    dictation silently builds its own isolated runtime, every preload waits
    forever for a lease nobody owns, and the queue row sits at "Processing"
    with no error and no Retry.
    """
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    try:
        controller._executor = _RefusingExecutor()
        controller._stream_finalize_executor = _RefusingExecutor()
        controller._increment_transcriber_runtime_count()
        lease = controller_module._TranscriberRuntimeLease(
            controller,
            object(),
            owns_shared_lock=False,
            close_on_release=False,
        )
        controller._active_stream_settings = AppSettings(engine="deepgram")
        controller._active_stream_transcriber = FakeStreamingTranscriber()
        controller._active_stream_runtime_lease = lease
        controller._streaming_recording = True

        controller._submit_stream_finalize()

        assert lease._released is True, "the runtime lease was never handed back"
        assert controller._transcription_runtime_active() is False
        assert controller._jobs == {}, "the queue row was left behind"
        assert controller._active_request_token is None
        assert overlay.states[-1][0] == "Error", overlay.states[-1:]
        assert controller._streaming_recording is False
    finally:
        controller.shutdown()
    _ = app


def test_a_batch_job_that_cannot_be_scheduled_is_reported():
    """Same shape without a lease: the row would sit at "Processing" forever.

    The audio still has to survive for a manual retry -- an unschedulable
    worker is exactly the case where the recording is the only copy.
    """
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    try:
        controller._executor = _RefusingExecutor()

        controller._submit_batch_transcription(b"RIFFaudio", AppSettings(engine="local"))

        assert controller._jobs == {}, "the queue row was left behind"
        assert controller._active_request_token is None
        assert overlay.states[-1][0] == "Error", overlay.states[-1:]
        assert controller._last_failed_wav_bytes == b"RIFFaudio", (
            "the only copy of the recording was dropped"
        )
    finally:
        controller.shutdown()
    _ = app


def test_a_failed_replacement_never_leaves_the_closed_runtime_cached(monkeypatch):
    """Evict before closing -- the rule AGENTS.md states, at its third site.

    `create_transcriber` raises for a missing API key or an absent model, and
    the old runtime had already been closed by then while still installed as
    the cache under its old key. Switching back to the previous settings then
    handed that closed runtime straight to the next dictation.
    """
    controller, app = _make_controller()
    try:
        closed: list[str] = []

        class Runtime:
            def __init__(self, label):
                self.label = label

            def close(self):
                closed.append(self.label)

            def set_language_mode(self, mode):
                pass

        built: list[str] = []

        def _create(settings, **_kwargs):
            built.append(settings.model_size)
            if settings.model_size == "medium":
                raise RuntimeError("the model is not installed")
            return Runtime(settings.model_size)

        monkeypatch.setattr(controller_module, "create_transcriber", _create)
        small = AppSettings(engine="local", model_size="small")
        medium = AppSettings(engine="local", model_size="medium")

        first = controller._get_or_create_transcriber(small)
        assert isinstance(first, Runtime)

        with pytest.raises(RuntimeError):
            controller._get_or_create_transcriber(medium)

        assert closed == ["small"]
        assert controller._transcriber_cache is None, (
            "the closed runtime is still installed as the cache"
        )
        # The key has to go with it. `_local_model_preload_needed` reads the
        # key alone, so a stale one beside an empty cache answers "already
        # loaded" and no preload is started -- the next dictation then loads
        # the model on the transcription worker with no progress shown.
        #
        # Recording a successful preload first is what makes that reachable:
        # with no result stored the method returns True on its own (never
        # preloaded), so the assertion below held whether the key was cleared
        # or not.
        with controller._preload_result_lock:
            controller._preload_results[controller._model_preload_key(small)] = (
                controller._preload_generation,
                None,
            )
        assert controller._local_model_preload_needed(small) is True, (
            "an empty cache still reported the model as loaded"
        )

        again = controller._get_or_create_transcriber(small)
        assert again is not first, "a closed runtime was handed back"
        assert built == ["small", "medium", "small"]
    finally:
        controller.shutdown()
    _ = app


def test_an_empty_streaming_finalize_keeps_what_was_already_transcribed(tmp_path):
    """The third road to a wiped streaming transcript, and the last one open.

    An explicit abort and a dying stream runtime both rescue the live text
    before the reset wipes it. A finalize that returns nothing did not: the
    overlay said "No speech detected", history got no entry, Copy had nothing,
    and the whole dictation existed only as the part already pasted into the
    document. Reachable when a provider's `stop_stream` returns an empty
    string after a socket problem, which is exactly when the live text is the
    only copy left.
    """
    history = TranscriptHistoryStore(tmp_path / "history.json")
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    controller, app = _make_controller(
        overlay=overlay,
        text_inserter=inserter,
        history_store=history,
    )
    try:
        controller._active_session_mode = "streaming"
        controller._stream_committed_text = "der erste teil"
        controller._stream_live_text = "der erste teil und der zweite"
        controller._stream_last_partial_text = "der erste teil und der zweite"
        controller._target_window_handle = 555
        controller._target_focus_signature = (555, 556, 557)

        controller._on_transcription_ready("")

        assert [entry.text for entry in history.load()] == [
            "der erste teil und der zweite"
        ], "the live transcript was not saved anywhere"
        assert controller._last_transcript == "der erste teil und der zweite"
        assert overlay.states[-1][0] == "Done"
        assert "No speech detected" not in overlay.states[-1][1]
        # The tail past what was already pasted still has to be inserted.
        assert inserter.calls[-1][0] == " und der zweite", inserter.calls
    finally:
        controller.shutdown()
    _ = app


def test_a_streaming_session_that_really_said_nothing_still_reports_it(caplog):
    """The other direction: no live text means no rescue and no history entry."""
    overlay = FakeOverlay()
    controller, app = _make_controller(overlay=overlay)
    try:
        controller._active_session_mode = "streaming"

        with caplog.at_level(logging.INFO, logger="test.controller"):
            controller._on_transcription_ready("")

        assert overlay.states[-1] == ("Done", "No speech detected.")
        # And it must not claim to have kept anything.
        assert "streaming_finalize_empty" not in caplog.text, caplog.text
    finally:
        controller.shutdown()
    _ = app


def test_repaste_refuses_when_no_foreign_window_is_known():
    """`None` is the honest answer, and it must not become "paste anyway".

    `get_foreground_window` returns `None` while one of our own tool windows
    holds the foreground and nothing foreign has been remembered yet -- a fresh
    session whose first action is the tray menu. Pasting then went to our own
    hidden host window and still reported "Done".
    """
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    focus_helper = FakeWindowFocusHelper()
    focus_helper.current = None
    focus_helper.current_focus = None
    focus_helper.current_caret = None
    controller, app = _make_controller(
        overlay=overlay,
        text_inserter=inserter,
        window_focus_helper=focus_helper,
    )
    controller._last_transcript = "hello again"
    before = list(inserter.calls)

    controller.repaste_last_transcript()

    assert inserter.calls == before, "it pasted with no known target"
    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "No window to insert into" in detail
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# The overlay's Insert action re-pastes what failed, not the last transcript
# ---------------------------------------------------------------------------


def _streaming_session_with_a_pasted_prefix(*, inserter):
    """A live streaming dictation whose first half already reached the document."""
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
        model_size="small",
        mode="streaming",
        silence_gate_enabled=False,
    )
    overlay = FakeOverlay()
    focus_helper = FakeWindowFocusHelper()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        text_inserter=inserter,
        window_focus_helper=focus_helper,
    )
    controller._active_session_mode = "streaming"
    controller._streaming_recording = True
    controller._stream_text_state.committed_text = "erster teil"
    controller._stream_text_state.live_text = "erster teil"
    controller._target_window_handle = 123
    controller._target_focus_signature = None
    return controller, app, overlay, focus_helper


def test_the_overlay_insert_re_pastes_only_the_text_that_failed():
    """A streaming finalize inserts the tail past `committed_text`; so must Insert.

    The Error state's Insert action was wired to `repaste_last_transcript`,
    which reads `_last_transcript` -- the whole dictation. Measured: the
    finalize inserted ' zweiter teil', the overlay offered Insert for exactly
    that text, and pressing it pasted 'erster teil zweiter teil' on top of the
    'erster teil' already in the document.
    """
    inserter = FakeTextInserter()
    controller, app, overlay, _focus = _streaming_session_with_a_pasted_prefix(
        inserter=inserter
    )
    inserter.should_fail = True

    controller._on_transcription_ready("erster teil zweiter teil")

    failed_text = inserter.calls[-1][0]
    assert failed_text == " zweiter teil"
    state, _detail = overlay.states[-1]
    assert state == "Error"
    assert overlay.state_kwargs[-1]["error_action"] == OVERLAY_ERROR_ACTION_INSERT
    assert overlay.state_kwargs[-1]["copy_text"] == failed_text
    assert controller._last_transcript == "erster teil zweiter teil"

    inserter.should_fail = False
    controller.insert_failed_text()

    assert inserter.calls[-1][0] == failed_text
    assert overlay.states[-1] == ("Done", failed_text)
    controller.shutdown()
    _ = app


def test_the_tray_re_paste_still_pastes_the_whole_transcript_after_a_failed_finalize():
    """The tray action and the re-paste hotkey mean "the last transcript"."""
    inserter = FakeTextInserter()
    controller, app, _overlay, _focus = _streaming_session_with_a_pasted_prefix(
        inserter=inserter
    )
    inserter.should_fail = True
    controller._on_transcription_ready("erster teil zweiter teil")

    inserter.should_fail = False
    controller.repaste_last_transcript()

    assert inserter.calls[-1][0] == "erster teil zweiter teil"
    controller.shutdown()
    _ = app


def test_the_overlay_insert_falls_back_to_the_last_transcript_when_nothing_failed():
    """With no failed insert on record, Insert behaves like the tray action."""
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    focus_helper = FakeWindowFocusHelper()
    controller, app = _make_controller(
        overlay=overlay, text_inserter=inserter, window_focus_helper=focus_helper
    )
    controller._last_transcript = "hello again"

    controller.insert_failed_text()

    assert inserter.calls[-1] == ("hello again", focus_helper.current_caret, "auto")
    assert overlay.states[-1] == ("Done", "hello again")
    controller.shutdown()
    _ = app


def test_a_successful_re_paste_retires_the_failed_insert_offer():
    """Once the failed text is in the document, Insert must not offer it again."""
    inserter = FakeTextInserter()
    controller, app, _overlay, _focus = _streaming_session_with_a_pasted_prefix(
        inserter=inserter
    )
    inserter.should_fail = True
    controller._on_transcription_ready("erster teil zweiter teil")
    inserter.should_fail = False
    controller.insert_failed_text()
    assert inserter.calls[-1][0] == " zweiter teil"

    controller.insert_failed_text()

    assert inserter.calls[-1][0] == "erster teil zweiter teil"
    controller.shutdown()
    _ = app


def test_a_new_recording_retires_the_failed_insert_offer(monkeypatch):
    """A failed insert from one dictation must not be pasted after the next one.

    Batch settings so the recording start needs no stream; the failed insert
    is produced through the real error arm of `_insert_text_at_target`.
    """
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
        engine="local",
        mode="batch",
    )
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        text_inserter=inserter,
    )
    controller._last_transcript = "erster teil zweiter teil"
    inserter.should_fail = True
    assert controller._insert_text_at_target(" zweiter teil", restore_focus=True) is False
    assert overlay.state_kwargs[-1]["error_action"] == OVERLAY_ERROR_ACTION_INSERT
    inserter.should_fail = False

    controller.start_recording()
    controller.cancel_current_action()
    controller.insert_failed_text()

    assert inserter.calls[-1][0] == "erster teil zweiter teil"
    controller.shutdown()
    _ = app


def test_the_overlay_insert_after_a_background_failure_pastes_what_is_displayed():
    """A stale offer from an earlier failed insert must not outlive a newer one.

    Reachable without a new recording in between: a streaming finalize insert
    fails (Insert offers its tail), then the cancel hotkey's "nothing to
    cancel" path flushes a queued older job whose insert fails too. The
    overlay now displays that job's transcript, so Insert must paste exactly
    that -- not the tail of the previous failure.
    """
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    controller, app = _make_controller(overlay=overlay, text_inserter=inserter)
    try:
        controller._last_transcript = "erster teil zweiter teil"
        inserter.should_fail = True
        assert (
            controller._insert_text_at_target(" zweiter teil", restore_focus=True)
            is False
        )
        job = controller._register_transcription_job(
            controller._next_request_token(),
            AppSettings(hotkey=FALLBACK_HOTKEY),
            "batch",
        )
        controller._report_background_insertion_failure(job, "der transkript")
        assert overlay.states[-1][0] == "Error"
        assert overlay.state_kwargs[-1]["error_action"] == OVERLAY_ERROR_ACTION_INSERT
        assert overlay.state_kwargs[-1]["copy_text"] == "der transkript"

        inserter.should_fail = False
        controller.insert_failed_text()

        assert inserter.calls[-1][0] == "der transkript"
    finally:
        controller.shutdown()
    _ = app
