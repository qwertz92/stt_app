"""Tests for the concurrent-transcription modes and cooperative cancel.

These exercise the controller's per-job delivery and abort handling without
real worker threads by swapping in a deferred executor and driving the result
signals directly.
"""

import logging

from stt_app.settings_store import AppSettings
from stt_app.transcript_history import TranscriptHistoryStore
from stt_app.config import FALLBACK_HOTKEY

from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)


class DeferredExecutor:
    """Captures submitted work without running it."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return None

    def shutdown(self, wait=False, cancel_futures=False):
        pass


def _make_queue_controller(monkeypatch, tmp_path, *, mode):
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _s, **kw: FakeStreamingTranscriber(),
    )
    FakeCapture.instances = []
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
        concurrent_transcription_mode=mode,
    )
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    focus = FakeWindowFocusHelper()
    controller, app = make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        text_inserter=inserter,
        window_focus_helper=focus,
        history_store=history_store,
        logger=logging.getLogger("test.controller.queue"),
    )
    controller._executor = DeferredExecutor()
    return controller, app, overlay, inserter, focus, history_store


def _record_and_stop(controller):
    controller.start_recording()
    controller.stop_recording()
    return controller._active_request_token


def test_queue_overlay_lists_running_job(monkeypatch, tmp_path):
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    token_a = _record_and_stop(controller)
    assert overlay.queue_updates[-1] == [(token_a, "local · small")]
    controller.shutdown()
    _ = app


def test_insert_mode_keeps_and_inserts_background_result(monkeypatch, tmp_path):
    controller, app, overlay, inserter, focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    # Move focus so the next recording captures a different target window.
    focus.captured = 111
    focus.captured_focus = 222
    focus.captured_caret = 333

    controller.start_recording()  # new recording supersedes A in insert mode
    assert controller._audio_capture is not None

    controller._on_transcription_ready("transcript A", request_token=token_a)

    assert [e.text for e in history.load()] == ["transcript A"]
    # Inserted into the window focused when A was recorded (caret 321).
    assert inserter.calls[-1] == ("transcript A", 321, "auto")
    assert overlay.states[-1][0] == "Listening"
    assert overlay.queue_updates[-1] == []
    controller.shutdown()
    _ = app


def test_history_mode_keeps_but_does_not_insert(monkeypatch, tmp_path):
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="history"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    assert controller._jobs[token_a].background_delivery == "history"

    controller._on_transcription_ready("transcript A", request_token=token_a)

    assert [e.text for e in history.load()] == ["transcript A"]
    assert inserter.calls == []  # history only, never inserted
    controller.shutdown()
    _ = app


def test_cancel_mode_aborts_old_job_but_keeps_completed_in_history(
    monkeypatch, tmp_path
):
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="cancel"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()  # cancel mode: ask A to stop

    job = controller._jobs[token_a]
    assert job.aborting is True
    assert job.background_delivery == "history"
    # The aborting job is hidden from the queue overlay.
    assert overlay.queue_updates[-1] == []

    # If it still finishes, it is kept in history (never discarded).
    controller._on_transcription_ready("transcript A", request_token=token_a)
    assert [e.text for e in history.load()] == ["transcript A"]
    assert inserter.calls == []
    controller.shutdown()
    _ = app


def test_cancel_queued_transcription_keeps_completed_result_in_history(
    monkeypatch, tmp_path
):
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.cancel_queued_transcription(token_a)

    job = controller._jobs.get(token_a)
    assert job is not None and job.aborting is True
    assert overlay.queue_updates[-1] == []
    # Foreground cancel reflects in the main overlay area.
    assert overlay.states[-1] == ("Done", "Transcription canceled.")

    # A transcript that still finishes is kept in history, not inserted.
    controller._on_transcription_ready("late A", request_token=token_a)
    assert [e.text for e in history.load()] == ["late A"]
    assert inserter.calls == []
    controller.shutdown()
    _ = app


def test_transcription_canceled_signal_removes_job(monkeypatch, tmp_path):
    controller, app, overlay, _inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.cancel_queued_transcription(token_a)

    # Worker confirms it actually stopped before producing a transcript.
    controller._on_transcription_canceled_result(token_a)

    assert token_a not in controller._jobs
    assert controller._active_request_token is None
    assert history.load() == []
    controller.shutdown()
    _ = app


def test_clear_transcription_queue_aborts_all(monkeypatch, tmp_path):
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    token_b = _record_and_stop(controller)
    assert set(controller._jobs) == {token_a, token_b}

    controller.clear_transcription_queue()

    assert all(job.aborting for job in controller._jobs.values())
    assert overlay.queue_updates[-1] == []
    controller.shutdown()
    _ = app
