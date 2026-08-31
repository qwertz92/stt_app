"""Tests for the concurrent-transcription modes and cooperative cancel.

These exercise the controller's per-job delivery and abort handling without
real worker threads by swapping in a deferred executor and driving the result
signals directly.
"""

import logging
from dataclasses import replace

import pytest
from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)
from PySide6 import QtCore, QtGui

from stt_app.config import FALLBACK_HOTKEY
from stt_app.controller import _join_transcripts, _TranscriptionJob
from stt_app.settings_store import AppSettings
from stt_app.text_inserter import TextInsertionError
from stt_app.transcript_history import TranscriptHistoryStore


class DeferredExecutor:
    """Captures submitted work without running it."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return

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
        # A faster-whisper size explicitly: several of these tests switch to
        # streaming, and the default local model is the batch-only Parakeet.
        model_size="small",
        # These tests drive the queue with synthetic silent audio, which the
        # silence gate would (correctly) refuse to transcribe.
        silence_gate_enabled=False,
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
    assert len(overlay.queue_updates[-1]) == 1
    assert overlay.queue_updates[-1][0][0] == token_a
    assert overlay.queue_updates[-1][0][1].startswith("#1 · ")
    assert overlay.queue_updates[-1][0][1].endswith("local · small")
    controller.shutdown()
    _ = app


def test_stop_recording_reveals_overlay_on_hotkey_press(monkeypatch, tmp_path):
    """Stopping a recording surfaces the (floating) overlay immediately.

    The overlay is brought forward on the stop press itself — via the same
    non-activating reveal used on start — so a floating overlay sitting behind
    other windows shows the new Processing state right away instead of only
    after the transcript finishes.
    """
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    controller.start_recording()
    reveals_after_start = overlay.reveal_calls
    assert reveals_after_start >= 1

    controller.stop_recording()

    # Stopping adds its own reveal (not only the later result reveal), and the
    # overlay is in the Processing state the reveal makes visible.
    assert overlay.reveal_calls == reveals_after_start + 1
    assert overlay.states[-1] == ("Processing", "Transcribing audio...")
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
    assert inserter.calls == []
    assert overlay.states[-1][0] == "Listening"
    assert overlay.queue_updates[-1][0][0] == token_a
    assert "Pending insert" in overlay.queue_updates[-1][0][1]
    assert controller._active_request_token is None

    controller.stop_recording()
    token_b = controller._active_request_token

    assert inserter.calls == []
    assert controller._jobs[token_a].insertion_deferred is True

    controller._on_transcription_ready("transcript B", request_token=token_b)

    # Inserted into each recording's captured target in token order.
    assert inserter.calls == [
        ("transcript A", 321, "auto"),
        ("transcript B", 333, "auto"),
    ]
    controller.shutdown()
    _ = app


def test_start_recording_keeps_new_target_when_old_result_arrives_during_start(
    monkeypatch,
    tmp_path,
):
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    focus.captured = 111
    focus.captured_focus = 222
    focus.captured_caret = 333
    focus.current = 111
    focus.current_focus = 222
    focus.current_caret = 333

    def restore_target_window(hwnd):
        focus.restore_calls.append(hwnd)
        if hwnd == 987:
            focus.captured = 987
            focus.captured_focus = 654
            focus.captured_caret = 321
            focus.current = 987
            focus.current_focus = 654
            focus.current_caret = 321
        elif hwnd == 111:
            focus.captured = 111
            focus.captured_focus = 222
            focus.captured_caret = 333
            focus.current = 111
            focus.current_focus = 222
            focus.current_caret = 333
        return True

    focus.restore_target_window = restore_target_window
    processed = {"done": False}

    def process_events(*_args):
        if processed["done"]:
            return
        processed["done"] = True
        controller._on_transcription_ready("transcript A", request_token=token_a)
        assert inserter.calls == []
        assert focus.restore_calls == []

    monkeypatch.setattr(QtCore.QCoreApplication, "processEvents", process_events)

    controller.start_recording()

    assert inserter.calls == []
    assert [job.token for job, _text in controller._deferred_background_results] == [
        token_a
    ]
    assert controller._jobs[token_a].insertion_deferred is True
    assert focus.restore_calls == []
    assert controller._target_window_handle == 111
    assert controller._target_focus_signature == (111, 222, 333)

    controller.stop_recording()
    token_b = controller._active_request_token

    assert inserter.calls == []
    assert focus.restore_calls == []

    controller._on_transcription_ready("transcript B", request_token=token_b)

    assert inserter.calls == [
        ("transcript A", 321, "auto"),
        ("transcript B", 333, "auto"),
    ]
    assert focus.restore_calls == [987, 111]
    controller.shutdown()
    _ = app


def test_background_insert_waits_until_active_recording_stops(
    monkeypatch,
    tmp_path,
):
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()

    controller._on_transcription_ready("transcript A", request_token=token_a)

    assert [e.text for e in history.load()] == ["transcript A"]
    assert inserter.calls == []
    assert controller._deferred_background_results
    assert overlay.queue_updates[-1][0][0] == token_a
    assert "Pending insert" in overlay.queue_updates[-1][0][1]
    assert overlay.states[-1][0] == "Listening"

    controller.stop_recording()
    token_b = controller._active_request_token

    assert inserter.calls == []
    assert controller._deferred_background_results
    assert token_a in controller._jobs

    controller._on_transcription_ready("transcript B", request_token=token_b)

    assert inserter.calls == [
        ("transcript A", 321, "auto"),
        ("transcript B", 321, "auto"),
    ]
    assert controller._deferred_background_results == []
    assert token_a not in controller._jobs
    assert token_b not in controller._jobs
    controller.shutdown()
    _ = app


def test_cancel_deferred_background_insert_drops_pending_paste(
    monkeypatch,
    tmp_path,
):
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)

    assert controller._deferred_background_results
    controller.cancel_queued_transcription(token_a)

    assert controller._deferred_background_results == []
    assert token_a not in controller._jobs
    assert overlay.queue_updates[-1] == []
    assert [e.text for e in history.load()] == ["transcript A"]

    controller.stop_recording()

    assert inserter.calls == []
    controller.shutdown()
    _ = app


def test_hotkey_during_recording_start_stops_after_start(
    monkeypatch,
    tmp_path,
):
    controller, app, _overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    single_shots = []

    def run_single_shot(_msec, callback):
        single_shots.append(callback)
        callback()

    processed = {"done": False}

    def process_events(*_args):
        if processed["done"]:
            return
        processed["done"] = True
        controller.toggle_recording()

    monkeypatch.setattr(QtCore.QCoreApplication, "processEvents", process_events)
    monkeypatch.setattr(QtCore.QTimer, "singleShot", run_single_shot)

    controller.toggle_recording()

    assert len(FakeCapture.instances) == 1
    assert FakeCapture.instances[0].stopped is True
    assert controller._audio_capture is None
    assert controller._active_request_token is not None
    assert len(controller._executor.calls) == 1
    assert single_shots
    controller.shutdown()
    _ = app


def test_history_mode_keeps_but_does_not_insert(monkeypatch, tmp_path):
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
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


def test_background_insert_failure_does_not_overwrite_clipboard(
    monkeypatch,
    tmp_path,
):
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    class FakeClipboard:
        def __init__(self):
            self.value = "user clipboard"

        def setText(self, text):
            self.value = text

        def text(self):
            return self.value

    clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: clipboard)

    def insert_text_with_options(
        text,
        target_hwnd=None,
        paste_mode="auto",
        restore_clipboard=True,
    ):
        inserter.calls.append((text, target_hwnd, paste_mode))
        if text == "transcript A":
            raise TextInsertionError("failed insert")
        return True

    inserter.insert_text_with_options = insert_text_with_options

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token

    controller._on_transcription_ready("transcript B", request_token=token_b)

    assert {e.text for e in history.load()} == {"transcript A", "transcript B"}
    assert clipboard.text() == "user clipboard"
    controller.shutdown()
    _ = app


def test_deferred_background_insert_flushes_when_current_job_fails(
    monkeypatch,
    tmp_path,
):
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token

    assert inserter.calls == []

    controller._on_transcription_failed("provider failed", request_token=token_b)

    assert [e.text for e in history.load()] == ["transcript A"]
    assert inserter.calls == [("transcript A", 321, "auto")]
    assert controller._deferred_background_results == []
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


def test_background_progress_does_not_override_new_recording_overlay(
    monkeypatch,
    tmp_path,
):
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    prior_state_count = len(overlay.states)

    controller._on_transcription_progress_result(token_a, "old job still working")

    assert len(overlay.states) == prior_state_count
    assert overlay.states[-1][0] == "Listening"
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


def test_canceled_job_progress_does_not_restore_processing_overlay(
    monkeypatch,
    tmp_path,
):
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.cancel_queued_transcription(token_a)
    prior_state_count = len(overlay.states)

    controller._on_transcription_progress_result(token_a, "canceling old job")

    assert len(overlay.states) == prior_state_count
    assert overlay.states[-1] == ("Done", "Transcription canceled.")
    controller.shutdown()
    _ = app


def test_transcription_canceled_signal_removes_job(monkeypatch, tmp_path):
    controller, app, _overlay, _inserter, _focus, history = _make_queue_controller(
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


def test_clear_queue_does_not_paste_the_rows_it_is_clearing(
    monkeypatch,
    tmp_path,
):
    """The button says clear, so nothing may be typed into the document.

    Clear queue cancelled the jobs one at a time, and a single row's cancel
    deliberately flushes the deferred inserts beside it -- otherwise the ✕ on
    one row would strand the finished transcripts on the others. On the first
    iteration those others were still pending, so clearing a queue that held
    two finished transcripts and one running job pasted both of them. Which
    ones survived depended purely on the order the loop reached them in: a row
    cancelled before the flush was discarded, one cancelled after it was
    typed. Stopping every job first makes the flush find nothing.
    """
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("transcript A.", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token
    controller.start_recording()
    controller._on_transcription_ready("transcript B.", request_token=token_b)
    controller.stop_recording()

    # Two finished-but-not-inserted rows plus the running one.
    assert len(controller._deferred_background_results) == 2
    assert len(controller._jobs) == 3
    assert inserter.calls == []

    controller.clear_transcription_queue()

    assert inserter.calls == [], (
        f"Clear queue pasted the cleared transcripts: {inserter.calls}"
    )
    assert controller._deferred_background_results == []
    # Nothing is destroyed: both transcripts were saved to history when they
    # finished, which is what the queue rows were waiting on top of.
    assert [e.text for e in history.load()] == ["transcript A.", "transcript B."]
    controller.shutdown()
    _ = app


def test_cancelling_one_row_still_delivers_the_others(monkeypatch, tmp_path):
    """The per-row ✕ keeps its own behaviour, which is the opposite one.

    Cancelling one row must not strand the finished transcripts beside it, so
    that path still flushes. Only Clear queue changed.
    """
    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("transcript A.", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token

    controller.cancel_queued_transcription(token_b)

    assert inserter.calls == [("transcript A.", 321, "auto")]
    controller.shutdown()
    _ = app


def test_cancel_recording_flushes_deferred_background_insert(
    monkeypatch,
    tmp_path,
):
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    # A new recording supersedes A and blocks A's insert while it is active.
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    assert controller._deferred_background_results
    assert inserter.calls == []

    # Canceling the blocking recording must deliver the deferred insert instead
    # of leaving it pending until some later recording.
    controller.cancel_current_action()

    assert controller._audio_capture is None
    assert controller._deferred_background_results == []
    assert token_a not in controller._jobs
    assert inserter.calls == [("transcript A", 321, "auto")]
    assert [e.text for e in history.load()] == ["transcript A"]
    controller.shutdown()
    _ = app


def test_cancel_recording_delivers_deferred_insert_despite_active_transcription(
    monkeypatch,
    tmp_path,
):
    """Cancel (Ctrl+Alt+F12) delivers completed pending inserts immediately.

    Regression: with a finished transcript deferred as "Insert Pending" and an
    unrelated newer transcription still running, canceling the active recording
    left the completed one stuck behind the running transcription (blocked by
    ``_active_request_token``) until it finished — up to a minute later, which
    reads as "deleted, only in history". An explicit cancel now delivers the
    completed result right away (into its own captured window); an active
    recording/capture still blocks insertion, and the running transcription
    delivers itself later with no duplicate.
    """
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token1 = _record_and_stop(controller)
    # A second recording supersedes msg1; msg2 becomes the active transcription.
    controller.start_recording()
    controller.stop_recording()
    token2 = controller._active_request_token
    # msg1 finishes while msg2 is still transcribing -> deferred (Insert Pending).
    controller._on_transcription_ready("msg1", request_token=token1)
    assert controller._deferred_background_results
    assert controller._active_request_token == token2
    assert inserter.calls == []

    # A third recording is active while msg2 still transcribes.
    controller.start_recording()
    assert controller._audio_capture is not None

    # Cancel the active recording via the cancel hotkey. The completed msg1 must
    # be delivered now, not left pending behind the still-running msg2.
    controller.cancel_current_action()

    assert controller._audio_capture is None
    assert controller._deferred_background_results == []
    assert inserter.calls == [("msg1", 321, "auto")]

    # msg2 finishing later still delivers itself, with no duplicate msg1.
    controller._on_transcription_ready("msg2", request_token=token2)
    assert inserter.calls == [("msg1", 321, "auto"), ("msg2", 321, "auto")]
    assert [e.text for e in history.load()] == ["msg1", "msg2"]
    controller.shutdown()
    _ = app


def test_cancel_newest_queued_flushes_earlier_deferred_insert(
    monkeypatch,
    tmp_path,
):
    """Canceling the newest (foreground) job still delivers earlier ones.

    Regression: a completed transcript deferred behind the live session was
    left stuck when the blocking foreground job was canceled from the overlay
    queue row, so nothing was inserted at all — not even the earlier recording
    that had already finished and should have been pasted.
    """
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    # A second recording supersedes A and defers A's insert behind the live
    # session; A finishes while B is still being recorded.
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token

    assert controller._deferred_background_results
    assert controller._jobs[token_a].insertion_deferred is True
    assert inserter.calls == []

    # Cancel the newest (foreground) transcription B from the overlay queue.
    # The earlier finished transcript A must still be delivered.
    controller.cancel_queued_transcription(token_b)

    # B is aborting (kept for its winding-down worker); A was flushed + inserted.
    assert controller._jobs[token_b].aborting is True
    assert token_a not in controller._jobs
    assert controller._deferred_background_results == []
    assert inserter.calls == [("transcript A", 321, "auto")]
    assert [e.text for e in history.load()] == ["transcript A"]
    assert overlay.states[-1] == ("Done", "Transcription canceled.")
    controller.shutdown()
    _ = app


def test_immediate_background_insert_delivers_while_transcribing(
    monkeypatch,
    tmp_path,
):
    """With immediate_background_insert on, a finished queued result inserts
    right away even while another transcription is still running."""
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(
        controller._settings, immediate_background_insert=True
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller.stop_recording()
    token_b = controller._active_request_token
    # A finishes while B is still transcribing: inserted immediately, not
    # deferred behind B.
    controller._on_transcription_ready("msg A", request_token=token_a)
    assert inserter.calls == [("msg A", 321, "auto")]
    assert controller._deferred_background_results == []

    controller._on_transcription_ready("msg B", request_token=token_b)
    assert inserter.calls[-1] == ("msg B", 321, "auto")
    assert [e.text for e in history.load()] == ["msg A", "msg B"]
    controller.shutdown()
    _ = app


def test_immediate_insert_during_batch_recording(monkeypatch, tmp_path):
    """A finished result pastes the moment it is ready, even while a new batch
    recording is running: focus is restored to the finished job's window (the
    original queue behavior; the held-modifier Ctrl+V fix made it safe)."""
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(
        controller._settings, immediate_background_insert=True
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    assert controller._audio_capture is not None
    focus.current = 555  # even after switching windows mid-recording

    controller._on_transcription_ready("msg A", request_token=token_a)

    assert inserter.calls == [("msg A", 321, "auto")]
    assert controller._deferred_background_results == []
    assert focus.restore_calls == [987]
    controller.shutdown()
    _ = app


def test_immediate_insert_blocked_during_streaming_recording(
    monkeypatch,
    tmp_path,
):
    """A streaming recording never allows mid-recording background pastes."""
    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(
        controller._settings, immediate_background_insert=True
    )

    token_a = _record_and_stop(controller)
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    assert controller._streaming_recording is True

    controller._on_transcription_ready("msg A", request_token=token_a)

    assert inserter.calls == []
    assert controller._deferred_background_results
    controller.shutdown()
    _ = app


def test_streaming_abort_preserves_partial_transcript(monkeypatch, tmp_path):
    """An aborted stream keeps its partial transcript in history and overlay.

    Regression: a focus-change or cancel abort dropped everything already
    transcribed from the UI and history; only the text pasted so far survived
    in the target window.
    """
    controller, app, overlay, _inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    assert controller._streaming_recording is True

    controller._on_transcription_partial("hello world this is a partial")
    controller._abort_streaming_session(
        "Streaming aborted: target window focus changed.",
        beep=False,
        finalize_stream=False,
    )

    assert [e.text for e in history.load()] == ["hello world this is a partial"]
    assert overlay.states[-1][0] == "Error"
    assert "Partial transcript" in overlay.states[-1][1]
    assert controller._last_transcript == "hello world this is a partial"
    controller.shutdown()
    _ = app


def test_silence_gate_skips_transcription_of_silent_recording(
    monkeypatch,
    tmp_path,
):
    import io
    import wave

    import numpy as np

    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(
        controller._settings, silence_gate_enabled=True
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
    silence = buffer.getvalue()
    monkeypatch.setattr(FakeCapture, "stop", lambda _self: silence)

    controller.start_recording()
    controller.stop_recording()

    # A real, decodable but silent recording is what the gate is for.
    assert controller._active_request_token is None
    assert controller._executor.calls == []
    assert overlay.states[-1][0] == "Done"
    assert "No speech detected" in overlay.states[-1][1]
    controller.shutdown()
    _ = app


def test_silence_gate_passes_recording_with_speech(monkeypatch, tmp_path):
    import io
    import wave

    import numpy as np

    controller, app, _overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(
        controller._settings, silence_gate_enabled=True
    )

    audio = np.zeros(16000, dtype=np.float32)
    audio[:1600] = 0.05  # whisper-level burst above the default threshold
    pcm = (audio * 32767.0).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(pcm.tobytes())

    controller.start_recording()
    FakeCapture.instances[-1]._wav_bytes = buffer.getvalue()
    controller.stop_recording()

    assert controller._active_request_token is not None
    assert len(controller._executor.calls) == 1
    controller.shutdown()
    _ = app


def test_insert_target_current_window_pastes_at_focus_at_insert_time(
    monkeypatch,
    tmp_path,
):
    """insert_target=current_window sends the transcript to the control that
    is focused when the result is ready, not the recording-start snapshot."""
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(
        controller._settings, insert_target="current_window"
    )

    token_a = _record_and_stop(controller)
    # The user moves to another window before the transcript is ready.
    focus.current = 111
    focus.current_focus = 222
    focus.current_caret = 333

    controller._on_transcription_ready("msg A", request_token=token_a)

    assert inserter.calls == [("msg A", 333, "auto")]
    controller.shutdown()
    _ = app


def test_deferred_inserts_coalesce_into_one_paste_per_target(
    monkeypatch,
    tmp_path,
):
    """Queued results for the same window flush as a single paste.

    Each separate paste is its own clipboard set/paste/restore cycle and thus
    its own race window against the target app; flushing six queued results as
    six pastes meant six chances to lose one. Same-target results are joined
    (space-separated) and inserted in one cycle instead.
    """
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("transcript A.", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token
    controller.start_recording()
    controller._on_transcription_ready("transcript B.", request_token=token_b)
    controller.stop_recording()
    token_c = controller._active_request_token

    assert len(controller._deferred_background_results) == 2
    assert inserter.calls == []

    controller._on_transcription_ready("transcript C.", request_token=token_c)

    assert inserter.calls == [
        ("transcript A. transcript B.", 321, "auto"),
        ("transcript C.", 321, "auto"),
    ]
    assert [e.text for e in history.load()] == [
        "transcript A.",
        "transcript B.",
        "transcript C.",
    ]
    controller.shutdown()
    _ = app


def test_deferred_inserts_flush_per_target_window(monkeypatch, tmp_path):
    """Queued results for different windows stay separate pastes."""
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    focus.captured = 111
    focus.captured_focus = 222
    focus.captured_caret = 333
    controller.start_recording()
    controller._on_transcription_ready("msg A", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token
    focus.captured = 444
    focus.captured_focus = 555
    focus.captured_caret = 666
    controller.start_recording()
    controller._on_transcription_ready("msg B", request_token=token_b)
    controller.stop_recording()
    token_c = controller._active_request_token

    controller._on_transcription_ready("msg C", request_token=token_c)

    assert inserter.calls == [
        ("msg A", 321, "auto"),
        ("msg B", 333, "auto"),
        ("msg C", 666, "auto"),
    ]
    controller.shutdown()
    _ = app


def test_cancel_during_pending_stream_finalize_unblocks_recording(
    monkeypatch,
    tmp_path,
):
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(controller._settings, mode="streaming")

    controller.start_recording()
    controller.stop_recording()
    token = controller._active_request_token
    assert controller._streaming_recording is True

    controller.cancel_current_action()

    assert controller._streaming_recording is False
    assert controller._active_request_token is None
    assert overlay.states[-1] == ("Done", "Transcription canceled.")

    # The next recording must start instead of waiting forever on the
    # canceled finalize ("Streaming transcript is still finalizing.").
    captures_before = len(FakeCapture.instances)
    controller.toggle_recording()
    assert controller._audio_capture is not None
    assert len(FakeCapture.instances) == captures_before + 1

    # A finalize transcript that still arrives stays history-only and must
    # not reset the new live session.
    controller._on_transcription_ready("stream final", request_token=token)
    assert [e.text for e in history.load()] == ["stream final"]
    assert inserter.calls == []
    assert controller._audio_capture is not None
    assert controller._streaming_recording is True
    controller.shutdown()
    _ = app


def test_cancel_stream_finalize_queue_row_unblocks_recording(
    monkeypatch,
    tmp_path,
):
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(controller._settings, mode="streaming")

    controller.start_recording()
    controller.stop_recording()
    token = controller._active_request_token

    controller.cancel_queued_transcription(token)

    assert controller._streaming_recording is False
    assert overlay.states[-1] == ("Done", "Transcription canceled.")

    controller.toggle_recording()
    assert controller._audio_capture is not None
    controller.shutdown()
    _ = app


def test_streaming_cancel_flushes_deferred_background_insert(
    monkeypatch,
    tmp_path,
):
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    assert controller._deferred_background_results
    assert inserter.calls == []

    # Canceling the live streaming session removes the capture that blocked
    # A's insert; the deferred result must be delivered, not left pending.
    controller.cancel_current_action()

    assert controller._audio_capture is None
    assert controller._streaming_recording is False
    assert controller._deferred_background_results == []
    assert token_a not in controller._jobs
    assert inserter.calls == [("transcript A", 321, "auto")]
    assert [e.text for e in history.load()] == ["transcript A"]
    controller.shutdown()
    _ = app


def test_stream_runtime_failure_flushes_deferred_background_insert(
    monkeypatch,
    tmp_path,
):
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    assert controller._deferred_background_results

    controller._on_stream_runtime_failed("stream died")

    assert controller._audio_capture is None
    assert controller._deferred_background_results == []
    assert token_a not in controller._jobs
    assert inserter.calls == [("transcript A", 321, "auto")]
    assert [e.text for e in history.load()] == ["transcript A"]
    assert overlay.states[-1][0] == "Error"
    controller.shutdown()
    _ = app


def test_stream_runtime_failure_keeps_the_partial_transcript(monkeypatch, tmp_path):
    """An explicit abort deliberately keeps what was already transcribed. A
    dying stream runtime must too: otherwise the text exists only as the part
    already pasted into the target window, with nothing in history and nothing
    for the overlay Copy action."""
    controller, app, overlay, _inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    controller._stream_text_state.live_text = "half a sentence already spoken"

    controller._on_stream_runtime_failed("stream died")

    assert [e.text for e in history.load()] == ["half a sentence already spoken"]
    assert controller._last_transcript == "half a sentence already spoken"
    assert overlay.states[-1][0] == "Error"
    controller.shutdown()
    _ = app


def test_stream_runtime_failure_always_tears_down_the_capture(monkeypatch, tmp_path):
    """The history write is conditional on a pending finalize; the teardown is
    not. Gating both abandoned a live capture, its transcriber and its runtime
    lease, so the microphone kept recording after the overlay said Error."""
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    capture = controller._audio_capture
    assert capture is not None
    # Pretend a finalize is already in flight for this session.
    controller._jobs[999] = _TranscriptionJob(
        token=999,
        engine="local",
        model=controller._settings.model_size,
        mode="streaming",
        settings=controller._settings,
        target_handle=None,
        target_signature=None,
    )

    controller._on_stream_runtime_failed("stream died")

    assert controller._audio_capture is None
    assert capture.stopped is True
    assert controller._active_stream_transcriber is None
    assert overlay.states[-1][0] == "Error"
    controller.shutdown()
    _ = app


def test_background_failure_keeps_live_recording_session(monkeypatch, tmp_path):
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    reported: list[str] = []
    controller.background_transcription_failed.connect(reported.append)

    token_a = _record_and_stop(controller)
    controller.start_recording()

    controller._on_transcription_failed("provider down", request_token=token_a)

    assert overlay.states[-1][0] == "Listening"
    assert controller._audio_capture is not None
    assert token_a not in controller._jobs
    # The failed job's audio stays available for a manual retry.
    assert controller._last_failed_wav_bytes == b"RIFF"
    # The live session keeps the overlay, but the failure is still reported and
    # names the recording it belongs to.
    assert len(reported) == 1
    assert "provider down" in reported[0]
    assert "Recording " in reported[0]
    assert "Retry" in reported[0]
    controller.shutdown()
    _ = app


def test_background_failure_is_shown_when_no_session_owns_the_overlay(
    monkeypatch,
    tmp_path,
):
    """An idle overlay must show the failure instead of staying silent."""
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="history"
    )
    reported: list[str] = []
    controller.background_transcription_failed.connect(reported.append)

    token_a = _record_and_stop(controller)
    _record_and_stop(controller)
    # The newer job already delivered, so nothing owns the overlay any more
    # when the older queued job finally fails.
    controller._active_request_token = None

    controller._on_transcription_failed("provider down", request_token=token_a)

    assert len(reported) == 1
    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "provider down" in detail
    controller.shutdown()
    _ = app


def test_clear_queue_reflects_foreground_cancel_in_overlay(monkeypatch, tmp_path):
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    assert overlay.states[-1][0] == "Processing"

    controller.clear_transcription_queue()

    job = controller._jobs.get(token_a)
    assert job is not None and job.aborting is True
    assert overlay.queue_updates[-1] == []
    # The canceled foreground job must not leave a stale "Processing" state.
    assert overlay.states[-1] == ("Done", "Transcription canceled.")
    controller.shutdown()
    _ = app


def test_reload_settings_defers_transcriber_cache_reset_during_active_job(
    monkeypatch,
    tmp_path,
):
    controller, app, _overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    sentinel = object()
    closed: list[object] = []
    monkeypatch.setattr(controller, "_close_cached_transcriber", closed.append)
    monkeypatch.setattr(
        controller,
        "_get_or_create_transcriber",
        lambda _settings: sentinel,
    )
    runtime_lease = controller._acquire_transcriber_runtime(controller.settings)
    controller._transcriber_cache = sentinel
    controller._transcriber_cache_key = controller._transcriber_identity(
        controller.settings
    )

    # Saving a setting the runtime is built from, while the job is active,
    # must not close the runtime now.
    controller._settings_store._settings = replace(
        controller.settings, model_size="medium"
    )
    controller.reload_settings(re_register_hotkey=False)

    assert controller._pending_transcriber_cache_reset is True
    assert controller._transcriber_cache is sentinel
    assert closed == []

    # Releasing the actual runtime lease applies the deferred reset before a
    # later transcriber can be built.
    runtime_lease.release()

    assert closed == [sentinel]
    assert controller._pending_transcriber_cache_reset is False
    assert controller._transcriber_cache is None
    controller.shutdown()
    _ = app


def test_empty_batch_transcript_is_a_retryable_error(monkeypatch, tmp_path):
    """A model that returns no text is not 'no speech' and must not vanish."""
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._last_transcript = "previous transcript"

    token = _record_and_stop(controller)
    controller._on_transcription_ready("   ", request_token=token)

    assert overlay.states[-1][0] == "Error"
    assert "no text" in overlay.states[-1][1].lower()
    assert "Retry" in overlay.states[-1][1]
    assert controller._last_failed_wav_bytes == b"RIFF"
    assert controller._last_transcript == "previous transcript"
    assert inserter.calls == []
    assert history.load() == []
    assert token not in controller._jobs
    controller.shutdown()
    _ = app


def test_empty_background_transcript_is_reported(monkeypatch, tmp_path):
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    reported: list[str] = []
    controller.background_transcription_failed.connect(reported.append)

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("", request_token=token_a)

    assert overlay.states[-1][0] == "Listening"
    assert controller._audio_capture is not None
    assert token_a not in controller._jobs
    assert controller._last_failed_wav_bytes == b"RIFF"
    assert inserter.calls == []
    assert history.load() == []
    assert len(reported) == 1
    assert "no text" in reported[0].lower()
    assert "Retry" in reported[0]
    controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    ("label", "texts", "expected"),
    [
        ("no whitespace at either boundary", ["one", "two"], "one two"),
        ("a trailing space on the left", ["one ", "two"], "one two"),
        ("a leading space on the right", ["one", " two"], "one two"),
        ("a newline boundary", ["one\n", "two"], "one\ntwo"),
        ("an empty result in the middle", ["one", "", "two"], "one two"),
        ("a single result", ["only"], "only"),
        ("nothing at all", [], ""),
    ],
)
def test_joining_queued_transcripts_never_doubles_a_boundary(label, texts, expected):
    """One space between messages, and only where there is not one already.

    This is the only path that joins separate completed queue messages, and
    every other insert path passes its text through untouched -- so a space
    added unconditionally here shows up as a double space in the document
    with nothing else to blame.
    """
    assert _join_transcripts(texts) == expected, label


def test_current_window_insertion_coalesces_results_aimed_at_different_targets(
    monkeypatch, tmp_path
):
    """With `insert_target=current_window` every result goes to one place.

    Each separate paste is its own clipboard set/paste/restore cycle and thus
    its own race window against the target application, which is what the
    coalescing exists to avoid. The grouping key is the *recording's* captured
    target, so without `single_group` two recordings made in different windows
    still produced two pastes even though both were about to be aimed at
    whatever is focused now.
    """
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(controller._settings, insert_target="current_window")

    # Two recordings that both finish while a third is running, so both are
    # deferred and flushed together -- the only path that coalesces. Each is
    # made in a *different* window, which is what the per-target grouping keys
    # on and what `single_group` has to override.
    token_a = _record_and_stop(controller)

    focus.captured = 111
    focus.captured_focus = 222
    focus.captured_caret = 333
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token

    focus.captured = 555
    focus.captured_focus = 666
    focus.captured_caret = 777
    controller.start_recording()
    controller._on_transcription_ready("transcript B", request_token=token_b)
    controller.stop_recording()
    token_c = controller._active_request_token
    assert (
        controller._jobs[token_a].target_signature
        != controller._jobs[token_b].target_signature
    ), "the two recordings captured the same window, so nothing is coalesced"

    controller._on_transcription_ready("transcript C", request_token=token_c)

    pastes = [call[0] for call in inserter.calls if "transcript" in call[0]]
    assert pastes == ["transcript A transcript B", "transcript C"], (
        f"the deferred results were not coalesced into one paste: {pastes}"
    )
    controller.shutdown()
    _ = app


def _streaming_controller_with_live_text(monkeypatch, tmp_path, text):
    """A streaming session that has produced live text and is finalizing."""
    controller, app, overlay, inserter, focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    # What the provider has delivered so far -- the only in-app copy of the
    # dictation while the finalize is in flight.
    controller._on_transcription_partial(text)
    controller.stop_recording()

    class _CancellableFuture:
        """The finalize worker has been submitted but has not started.

        `DeferredExecutor.submit` returns None, so `_request_job_stop` would
        take its "already running" branch and never reach the terminal
        handling -- the opposite of the case this models.
        """

        def cancel(self):
            return True

    job = controller._jobs.get(controller._active_request_token)
    assert job is not None
    job.future = _CancellableFuture()
    return controller, app, overlay, inserter, focus, history


@pytest.mark.parametrize(
    "cancel",
    [
        pytest.param(
            lambda c, token: c.cancel_current_action(), id="cancel-button-or-hotkey"
        ),
        pytest.param(
            lambda c, token: c.cancel_queued_transcription(token), id="queue-row-x"
        ),
        pytest.param(
            lambda c, _token: c.clear_transcription_queue(), id="clear-queue"
        ),
    ],
)
def test_cancelling_a_pending_stream_finalize_keeps_the_dictation(
    monkeypatch, tmp_path, cancel
):
    """One second earlier the same press saved the text; here it destroyed it.

    `_request_job_stop` cleared the streaming session state to unblock the
    next recording, and that state held the only in-app copy: the worker then
    takes its `canceled_before_start` arm and calls `abort_stream()` rather
    than `stop_stream()`, so the provider never returns the text either.
    """
    spoken = "hallo das ist ein langer diktattext"
    controller, app, _overlay, _inserter, _focus, history = (
        _streaming_controller_with_live_text(monkeypatch, tmp_path, spoken)
    )
    token = controller._active_request_token

    cancel(controller, token)

    assert [e.text for e in history.load()] == [spoken], (
        "the whole dictation was discarded by the cancel"
    )
    assert controller._last_transcript == spoken, "Copy had nothing to offer"
    assert controller._streaming_recording is False
    controller.shutdown()
    _ = app


def test_a_finalize_that_still_delivers_writes_only_one_history_entry(
    monkeypatch, tmp_path
):
    """The stash must never become a second entry for one dictation."""
    controller, app, _overlay, _inserter, _focus, history = (
        _streaming_controller_with_live_text(monkeypatch, tmp_path, "teil eins")
    )
    token = controller._active_request_token

    controller._on_transcription_ready("teil eins und zwei", request_token=token)

    assert [e.text for e in history.load()] == ["teil eins und zwei"]
    controller.shutdown()
    _ = app


def test_a_finalize_the_worker_had_already_started_also_keeps_its_partial(
    monkeypatch, tmp_path
):
    """The other half of the cancel race: the worker was past its check.

    It then calls `abort_stream()` from its `canceled_before_start` arm and
    emits `canceled`, so no text arrives that way either.
    """
    spoken = "der zweite lange diktattext"
    controller, app, _overlay, _inserter, _focus, history = (
        _streaming_controller_with_live_text(monkeypatch, tmp_path, spoken)
    )
    token = controller._active_request_token
    controller._jobs[token].future = None  # `future.cancel()` returns False

    controller.cancel_current_action()
    assert [e.text for e in history.load()] == [], "written before the worker ended"

    controller._on_transcription_canceled_result(token)

    assert [e.text for e in history.load()] == [spoken]
    assert controller._last_transcript == spoken
    controller.shutdown()
    _ = app


def test_a_worker_that_delivered_text_does_not_also_write_the_stash(
    monkeypatch, tmp_path
):
    """The other half of the cancel race, when `stop_stream()` did return text.

    `future.cancel()` fails once the worker is past its `aborting` check, so
    it stops the stream normally and its transcript arrives in the background.
    Leaving the stash set then wrote a second history entry for one dictation.
    """
    controller, app, _overlay, _inserter, _focus, history = (
        _streaming_controller_with_live_text(monkeypatch, tmp_path, "teil eins")
    )
    token = controller._active_request_token
    controller._jobs[token].future = None  # `future.cancel()` returns False

    controller.cancel_current_action()
    assert controller._jobs[token].stashed_partial == "teil eins"

    controller._on_transcription_ready("teil eins und zwei", request_token=token)

    assert [e.text for e in history.load()] == ["teil eins und zwei"]
    controller.shutdown()
    _ = app


@pytest.mark.parametrize("finished_state", ["Done", "Error"])
def test_the_preload_poll_does_not_paint_over_a_finished_result(
    monkeypatch, tmp_path, finished_state
):
    """The poll repaints the overlay every 600 ms while a model loads, and it
    only checked for an active *recording*. A preload running while a queued
    transcription finished -- the user changes the model in Settings while one
    is still in flight -- therefore replaced the transcript, or the failure
    reason plus the Retry/Insert action that is the only way to recover the
    recording, with "Loading model...". `_overlay_session_active` cannot see
    this either: a delivered result has already cleared its request token.
    """
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._overlay.set_state(finished_state, "the transcript the user needs")
    before = len(overlay.states)

    class _RunningPreload:
        @staticmethod
        def done() -> bool:
            return False

    controller._preload_future = _RunningPreload()
    controller._on_preload_progress_poll()

    assert len(overlay.states) == before, (
        f"the poll overwrote the {finished_state} state with "
        f"{overlay.states[-1]!r}"
    )
    controller._preload_future = None
    controller.shutdown()
    _ = app


def test_the_preload_poll_still_reports_progress_over_idle(monkeypatch, tmp_path):
    """The other half: replacing Idle with the load progress is the whole point
    of the poll, so the guard must not stop that."""
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._overlay.set_state("Idle", "Idle.")

    class _RunningPreload:
        @staticmethod
        def done() -> bool:
            return False

    controller._preload_future = _RunningPreload()
    controller._on_preload_progress_poll()

    assert overlay.states[-1][0] == "Processing"
    controller._preload_future = None
    controller.shutdown()
    _ = app


def test_a_cancel_does_not_paint_over_a_transcript_that_could_not_be_pasted(
    monkeypatch, tmp_path
):
    """Cancel flushes the deferred inserts on purpose, and an insert failing in
    that flush paints an Error carrying the transcript and the Insert action --
    the only way left to get the text into the document. "Nothing to cancel."
    then replaced both, one statement later, and the transcript existed only in
    history and a tray notification.
    """
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    token = _record_and_stop(controller)
    # A newer recording takes over, so the older result is delivered in the
    # background -- and deferred, because a capture is running.
    controller.start_recording()
    inserter.should_fail = True
    controller._on_transcription_ready("der ganze diktierte text", request_token=token)
    assert controller._deferred_background_results, "the result was not deferred"
    controller.stop_recording()
    assert controller._deferred_background_results, "it was delivered too early"

    # Cancel the now-active transcription. That flush is what fails to paste.
    controller.cancel_current_action()

    assert overlay.states[-1][0] == "Error", (
        f"the cancel message replaced the failure report: {overlay.states[-1]!r}"
    )
    assert "der ganze diktierte text" in overlay.states[-1][1]
    assert any("der ganze diktierte text" in e.text for e in history.load())
    controller.shutdown()
    _ = app


def _streaming_session_with_a_pending_finalize(controller):
    """Start a streaming dictation and register a finalize as in flight."""
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    job = _TranscriptionJob(
        token=999,
        engine="local",
        model=controller._settings.model_size,
        mode="streaming",
        settings=controller._settings,
        target_handle=None,
        target_signature=None,
    )
    controller._jobs[999] = job
    return job


def test_a_dying_runtime_stashes_the_partial_for_a_finalize_that_delivers_nothing(
    monkeypatch, tmp_path
):
    """"A finalize will deliver this" is not "a finalize did deliver".

    The guard exists so one dictation does not get two history entries, and it
    is right about that. But the reset that follows wiped the live text, and
    the rescue in `_on_transcription_ready` reads that same emptied state -- so
    a finalize that then returned nothing left the whole dictation nowhere at
    all. Stash it on the job, exactly as `_request_job_stop` already does:
    `_finish_transcription_job` writes a stash that nothing cleared, and every
    path that delivers real text clears it first.
    """
    controller, app, _overlay, _inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    job = _streaming_session_with_a_pending_finalize(controller)
    controller._stream_text_state.live_text = "ein ganzer satz den ich diktiert habe"

    controller._on_stream_runtime_failed("stream died")

    assert job.stashed_partial == "ein ganzer satz den ich diktiert habe", (
        "the partial was dropped instead of handed to the pending finalize"
    )
    assert [e.text for e in history.load()] == [], "it was written twice"

    # The finalize now delivers nothing, which is exactly when the stash is
    # the only remaining copy.
    controller._finish_transcription_job(999)

    assert [e.text for e in history.load()] == [
        "ein ganzer satz den ich diktiert habe"
    ]
    controller.shutdown()
    _ = app


def test_a_dying_runtime_leaves_a_pending_finalize_its_committed_text(
    monkeypatch, tmp_path
):
    """The reset must not empty what the finalize will be measured against.

    With `committed_text` cleared and `_active_session_mode` flipped to
    "batch", `_on_transcription_ready` took the batch branch and pasted the
    whole dictation on top of the text already inserted live. The streaming
    branch would have done the same: `finalize_append_only` computes its
    insertion against that same emptied `committed_text`.
    """
    controller, app, _overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    _streaming_session_with_a_pending_finalize(controller)
    controller._stream_text_state.live_text = "das ist ein laengerer"
    controller._stream_text_state.committed_text = "das ist ein laengerer"

    controller._on_stream_runtime_failed("stream died")

    assert controller._active_session_mode == "streaming", (
        "the pending finalize will now be delivered as a batch result"
    )
    assert controller._stream_committed_text == "das ist ein laengerer", (
        "the finalize will now compute its insertion against an empty prefix"
    )
    insertion, _final = controller._stream_text_state.finalize_append_only(
        "das ist ein laengerer diktierter satz"
    )
    assert insertion == " diktierter satz", (
        f"the whole dictation would be pasted a second time: {insertion!r}"
    )
    controller.shutdown()
    _ = app


def test_a_dying_runtime_with_no_finalize_still_resets_the_session(
    monkeypatch, tmp_path
):
    """`keep_session_text` is for a pending finalize only.

    With nothing in flight the partial goes straight to history and the
    session must be fully cleared, or the next dictation starts on the last
    one's committed prefix.
    """
    controller, app, _overlay, _inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    controller._stream_text_state.live_text = "halber satz"
    controller._stream_text_state.committed_text = "halber satz"

    controller._on_stream_runtime_failed("stream died")

    assert [e.text for e in history.load()] == ["halber satz"]
    assert controller._active_session_mode == "batch"
    assert controller._stream_committed_text == ""
    controller.shutdown()
    _ = app


def test_a_background_failure_gives_the_active_token_back(monkeypatch, tmp_path):
    """The one terminal handler that kept the token of a job it just buried.

    A job is delivered as *background* while it is still the active token
    whenever a newer recording is running, which is precisely the window this
    covers. Both sibling terminal handlers clear a matching token; this arm
    did not, so the token outlived the job. If the new recording then submits
    nothing -- silence-gated, cancelled, a watchdog abort -- it is never
    cleared again for the rest of the session, and it is read by two places:
    `_should_defer_background_insertion` defers every later queued transcript
    forever, and `_overlay_session_active` answers True forever, which is what
    makes `show_idle_status` swallow a failed hotkey registration.
    """
    controller, app, _overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    assert controller._active_request_token == token_a, "precondition"

    controller._on_transcription_failed("provider down", request_token=token_a)

    assert controller._active_request_token is None
    assert token_a not in controller._jobs

    # The new recording produces nothing, so nothing else will ever clear it.
    controller.cancel_current_action()

    assert controller._should_defer_background_insertion() is False
    assert controller._overlay_session_active() is False
    controller.shutdown()
    _ = app


def test_a_background_failure_leaves_a_newer_active_token_alone(monkeypatch, tmp_path):
    """The clear is guarded, and the guard is what makes it safe.

    An older job failing must not clear the token of the newer job that has
    since taken the foreground -- that would hand the overlay away from a live
    transcription and let queued results paste over it.
    """
    controller, app, _overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="history"
    )

    token_a = _record_and_stop(controller)
    token_b = _record_and_stop(controller)
    assert token_a != token_b
    assert controller._active_request_token == token_b, "precondition"

    controller._on_transcription_failed("provider down", request_token=token_a)

    assert controller._active_request_token == token_b
    controller.shutdown()
    _ = app
