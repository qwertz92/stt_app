"""Tests for the concurrent-transcription modes and cooperative cancel.

These exercise the controller's per-job delivery and abort handling without
real worker threads by swapping in a deferred executor and driving the result
signals directly.
"""

import logging
import re
import threading
import time
from dataclasses import replace

import numpy as np
import pytest
from conftest import (
    FakeCapture,
    FakeLastRecordingStore,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)
from PySide6 import QtCore, QtGui
from speech_fixtures import (
    after_a_pause,
    at_peak_level,
    concat,
    key_clack,
    low_thump,
    room_tone,
    speech_excerpts,
    typing,
    wav_bytes,
)

from stt_app.config import (
    CLIPBOARD_RESTORE_DELAY_S,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    FALLBACK_HOTKEY,
    QUEUE_ROW_KIND_TRANSCRIPTION,
    QUEUE_ROW_KIND_UNDELIVERED,
    SILENCE_GATE_THRESHOLD_MIN,
)
from stt_app.controller import _join_transcripts, _TranscriptionJob
from stt_app.settings_store import AppSettings
from stt_app.text_inserter import TextInsertionError, TextMayHaveBeenPastedError
from stt_app.transcript_history import TranscriptHistoryStore
from stt_app.vad import measure_peak_windowed_rms


class DeferredExecutor:
    """Captures submitted work without running it."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return

    def shutdown(self, wait=False, cancel_futures=False):
        pass


def _make_queue_controller(
    monkeypatch,
    tmp_path,
    *,
    mode,
    inserter=None,
    silence_gate_enabled=False,
    **settings_overrides,
):
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
        silence_gate_enabled=silence_gate_enabled,
        **settings_overrides,
    )
    overlay = FakeOverlay()
    inserter = inserter if inserter is not None else FakeTextInserter()
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


class PacedTextInserter(FakeTextInserter):
    """A fake inserter that answers the paste pace the way `TextInserter` does.

    It keeps its own clock: a test moves `now`, and every paste that returns
    records its keystroke time. `paste_pace_remaining_s` is written out here
    rather than borrowed from `TextInserter`, so these tests pin the
    controller's half of the contract and nothing of the inserter's.
    """

    def __init__(self):
        super().__init__()
        self.now = 1000.0
        self.last_keystroke_at = None
        self.paste_times = []

    def insert_text_with_options(
        self,
        text,
        target_hwnd=None,
        paste_mode="auto",
        restore_clipboard=True,
    ):
        result = super().insert_text_with_options(
            text,
            target_hwnd=target_hwnd,
            paste_mode=paste_mode,
            restore_clipboard=restore_clipboard,
        )
        self.paste_times.append(self.now)
        self.last_keystroke_at = self.now
        return result

    def paste_pace_remaining_s(self):
        if self.last_keystroke_at is None:
            return 0.0
        elapsed = self.now - self.last_keystroke_at
        return max(0.0, CLIPBOARD_RESTORE_DELAY_S - elapsed)


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

    # One paste for both: B's own paste used to follow A's within the same
    # call, i.e. a second clipboard write while the target may not yet have
    # read the first one.
    assert inserter.calls == [("transcript A transcript B", 321, "auto")]
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
        if "transcript A" in text:
            raise TextInsertionError("failed insert")
        return True

    inserter.insert_text_with_options = insert_text_with_options

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token

    controller._on_transcription_ready("transcript B", request_token=token_b)

    # The foreground result joins the deferred one, so the failing paste is
    # the joined text -- and the failure still leaves the clipboard alone.
    assert inserter.calls == [("transcript A transcript B", 321, "auto")]
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
    """A streaming recording allows no mid-recording background paste into
    another window. (One for its own window goes ahead of the stream's first
    live insert, for the order: `test_controller_edit_and_order.py`.)"""
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(
        controller._settings, immediate_background_insert=True
    )

    focus.captured = 111
    token_a = _record_and_stop(controller)
    focus.captured = 987
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
    controller._settings = replace(controller._settings, silence_gate_enabled=True)
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
    controller._settings = replace(controller._settings, silence_gate_enabled=True)

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


# --- The Silero speech check behind the level gate -------------------------
#
# The level gate skips a recording whose loudest 100 ms stays below the
# threshold; everything louder used to be transcribed, typing and a fan
# included, and a speech model hallucinates words from those. The speech
# check skips a recording the level gate admits only when a COMPLETE scan
# finds no window reaching SILERO_BATCH_MIN_PROBABILITY. The non-speech
# signals are SYNTHETIC (tests/speech_fixtures.py); the speech is the six
# LibriSpeech excerpts.


def _gated_controller(
    monkeypatch, tmp_path, *, enabled=True, wait_for_speech_model=True
):
    # The gate is in the settings the controller starts with, so its start
    # loads the speech model in the background as the app's does.
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", silence_gate_enabled=enabled
    )
    # A stop before the load finished would leave the recording unmeasured,
    # which is not what these tests are about.
    if wait_for_speech_model:
        _join_speech_model_loader()
    return controller, app, overlay


def _stop_with(controller, wav):
    controller.start_recording()
    FakeCapture.instances[-1]._wav_bytes = wav
    controller.stop_recording()


def _peak_level_lines(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("recording_peak_level")
    ]


_NON_SPEECH_THE_LEVEL_GATE_ADMITS = {
    "typing-120wpm": lambda: typing(120, 3.0),
    "room-tone-42dBFS": lambda: room_tone(3.0, rms=0.008),
    "thump-after-a-pause": lambda: after_a_pause(low_thump()),
}


@pytest.mark.parametrize("name", sorted(_NON_SPEECH_THE_LEVEL_GATE_ADMITS))
def test_the_speech_check_skips_a_recording_the_level_gate_admits(
    monkeypatch, tmp_path, caplog, real_silero, name
):
    samples = _NON_SPEECH_THE_LEVEL_GATE_ADMITS[name]()
    wav = wav_bytes(samples)
    assert measure_peak_windowed_rms(wav) >= DEFAULT_SILENCE_GATE_THRESHOLD
    controller, app, overlay = _gated_controller(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO)

    _stop_with(controller, wav)

    assert controller._executor.calls == []
    assert controller._active_request_token is None
    state, detail = overlay.states[-1]
    assert state == "Done"
    assert detail.startswith("No speech detected")
    assert "speech check" in detail
    assert "Settings -> Audio" in detail
    skip_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("silence_gate_skipped")
    ]
    assert len(skip_lines) == 1
    assert "reason=speech_check" in skip_lines[0]
    assert "level=" in skip_lines[0]
    assert "silero_max_probability=" in skip_lines[0]
    # A skip needs both scans, so the amplified one ran and is logged.
    peak_lines = _peak_level_lines(caplog)
    assert len(peak_lines) == 1
    assert re.search(r"silero_amplified_max_probability=0\.\d{3}", peak_lines[0])
    controller.shutdown()
    _ = app


def _speech_recordings():
    cases = []
    for name, samples in sorted(speech_excerpts().items()):
        cases.append((f"{name}-alone", samples))
        cases.append((f"{name}-after-a-pause", after_a_pause(samples)))
        quiet = (samples.astype(np.float32) * 10 ** (-20 / 20)).astype(np.int16)
        cases.append((f"{name}-at-20dB-after-a-pause", after_a_pause(quiet)))
    return cases


@pytest.mark.parametrize(
    "samples",
    [case[1] for case in _speech_recordings()],
    ids=[case[0] for case in _speech_recordings()],
)
def test_the_speech_check_never_skips_a_recorded_word(
    monkeypatch, tmp_path, real_silero, samples
):
    wav = wav_bytes(samples)
    if measure_peak_windowed_rms(wav) < DEFAULT_SILENCE_GATE_THRESHOLD:
        pytest.skip("the level gate refuses this recording before the speech check")
    controller, app, _overlay = _gated_controller(monkeypatch, tmp_path)

    _stop_with(controller, wav)

    assert len(controller._executor.calls) == 1
    controller.shutdown()
    _ = app


@pytest.mark.parametrize("name", sorted(speech_excerpts()))
def test_quiet_speech_lifted_over_the_level_gate_by_a_keystroke_is_not_skipped(
    monkeypatch, tmp_path, real_silero, name
):
    # A word whose loudest 100 ms is the quietest level the Audio tab lets the
    # silence gate admit, followed by the stop key's clack: the clack lifts
    # the recording over the default level gate, and Silero, which scores by
    # level, gave five of the six words 0.06-0.14 at their own level -- below
    # the cut. faster-whisper medium still transcribes speech that quiet, so
    # skipping it would drop a real dictation. The scan of the copy amplified
    # by SILERO_BATCH_QUIET_SPEECH_GAIN scores every one of them 0.80 or more.
    word = at_peak_level(speech_excerpts()[name], SILENCE_GATE_THRESHOLD_MIN)
    silence = np.zeros(1600, dtype=np.int16)
    wav = wav_bytes(concat(word, silence, key_clack(), silence[:800]))
    assert measure_peak_windowed_rms(wav) >= DEFAULT_SILENCE_GATE_THRESHOLD
    controller, app, _overlay = _gated_controller(monkeypatch, tmp_path)

    _stop_with(controller, wav)

    assert len(controller._executor.calls) == 1
    controller.shutdown()
    _ = app


def test_every_batch_stop_logs_the_speech_measurement(
    monkeypatch, tmp_path, caplog, real_silero
):
    controller, app, _overlay = _gated_controller(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO)

    _stop_with(controller, wav_bytes(after_a_pause(speech_excerpts()["word_220ms"])))

    lines = _peak_level_lines(caplog)
    assert len(lines) == 1
    assert "silero_speech_seconds=" in lines[0]
    assert "silero_max_probability=" in lines[0]
    assert "silero_speech_seconds=unavailable" not in lines[0]
    # Speech settles the answer on the first scan; the second never runs.
    assert "silero_amplified_max_probability=not_run" in lines[0]
    assert len(controller._executor.calls) == 1
    controller.shutdown()
    _ = app


def test_an_unavailable_speech_check_never_skips(
    monkeypatch, tmp_path, caplog, real_silero
):
    # The graph cannot be loaded -- the faster_whisper asset is missing.
    from stt_app import silero_vad

    silero_vad.reset_silero_for_tests()
    monkeypatch.setattr(silero_vad, "silero_asset_path", lambda: None)
    controller, app, _overlay = _gated_controller(monkeypatch, tmp_path)
    assert silero_vad.load_failed_recently()
    caplog.set_level(logging.INFO)

    _stop_with(controller, wav_bytes(typing(120, 3.0)))

    assert len(controller._executor.calls) == 1
    lines = _peak_level_lines(caplog)
    assert len(lines) == 1
    assert "silero_speech_seconds=unavailable" in lines[0]
    controller.shutdown()
    _ = app


def test_a_speech_check_that_raises_never_skips(monkeypatch, tmp_path, real_silero):
    from stt_app import silero_vad

    def broken(*_args, **_kwargs):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(silero_vad, "check_speech_wav", broken)
    controller, app, _overlay = _gated_controller(monkeypatch, tmp_path)

    _stop_with(controller, wav_bytes(typing(120, 3.0)))

    assert len(controller._executor.calls) == 1
    controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    ("enabled", "make"),
    [
        (False, lambda: typing(120, 3.0)),
        (True, lambda: room_tone(3.0)),
    ],
    ids=["gate-off", "below-the-level-gate"],
)
def test_the_speech_check_runs_only_when_its_answer_can_skip(
    monkeypatch, tmp_path, caplog, real_silero, enabled, make
):
    # The check costs a scan on the Qt thread at every stop. With the gate off
    # nothing can be skipped, and below the level threshold the level gate has
    # already skipped the recording: in both its answer would decide nothing.
    from stt_app import silero_vad

    calls = []
    real_check = silero_vad.check_speech_wav

    def counting_check(*args, **kwargs):
        calls.append(1)
        return real_check(*args, **kwargs)

    monkeypatch.setattr(silero_vad, "check_speech_wav", counting_check)
    controller, app, _overlay = _gated_controller(
        monkeypatch, tmp_path, enabled=enabled
    )
    caplog.set_level(logging.INFO)

    _stop_with(controller, wav_bytes(make()))

    assert calls == []
    # Gate off: transcribed as before. Below the level gate: skipped by it.
    assert len(controller._executor.calls) == (0 if enabled else 1)
    lines = _peak_level_lines(caplog)
    assert len(lines) == 1
    assert "silero_speech_seconds=not_run" in lines[0]
    controller.shutdown()
    _ = app


def _join_speech_model_loader():
    from stt_app import silero_vad

    loader = getattr(silero_vad, "_loader_thread", None)
    if loader is not None:
        loader.join(timeout=10)
        assert not loader.is_alive(), "the speech model loader did not finish"


def test_the_first_stop_does_not_build_the_speech_model_on_its_thread(
    monkeypatch, tmp_path, caplog, real_silero
):
    # Building the session imports ONNX Runtime and loads the graph: 123-275 ms
    # cold. A stop arriving before the background load has finished must not
    # do that work on the Qt thread; it leaves the recording unmeasured, which
    # never skips it.
    from stt_app import silero_vad

    silero_vad.reset_silero_for_tests()
    caller = threading.get_ident()
    release = threading.Event()
    builders = []
    real_build = silero_vad._build_session

    def recording_build():
        builders.append(threading.get_ident())
        if threading.get_ident() != caller:
            release.wait(timeout=10)
        return real_build()

    monkeypatch.setattr(silero_vad, "_build_session", recording_build)
    controller, app, overlay = _gated_controller(
        monkeypatch, tmp_path, wait_for_speech_model=False
    )
    caplog.set_level(logging.INFO)
    try:
        started = time.monotonic()
        _stop_with(controller, wav_bytes(typing(120, 3.0)))
        elapsed = time.monotonic() - started

        assert caller not in builders
        assert len(controller._executor.calls) == 1
        # The build is held for up to 10 s; a stop that waited for it -- on
        # the loader's lock, say -- would take that long.
        assert elapsed < 2.0, f"the stop waited {elapsed:.2f} s for the build"
        lines = _peak_level_lines(caplog)
        assert len(lines) == 1
        assert "silero_speech_seconds=loading" in lines[0]
    finally:
        release.set()
        _join_speech_model_loader()
        controller.shutdown()
    _ = app, overlay


def test_the_speech_model_loads_in_the_background_when_the_controller_starts(
    monkeypatch, tmp_path, real_silero
):
    from stt_app import silero_vad

    silero_vad.reset_silero_for_tests()
    caller = threading.get_ident()
    builders = []
    real_build = silero_vad._build_session

    def recording_build():
        builders.append(threading.get_ident())
        return real_build()

    monkeypatch.setattr(silero_vad, "_build_session", recording_build)
    controller, app, _overlay = _gated_controller(
        monkeypatch, tmp_path, wait_for_speech_model=False
    )
    try:
        _join_speech_model_loader()
        assert builders, "the controller never started loading the speech model"
        assert caller not in builders

        _stop_with(controller, wav_bytes(typing(120, 3.0)))

        # Loaded before the stop, so the stop measured and skipped the typing.
        assert controller._executor.calls == []
    finally:
        controller.shutdown()
    _ = app


def test_switching_the_silence_gate_on_loads_the_speech_model(
    monkeypatch, tmp_path, real_silero
):
    # With the gate off at start nothing is loaded; a settings save that
    # switches it on must start the background load, or the first gated stop
    # after it would go unmeasured.
    from stt_app import silero_vad

    silero_vad.reset_silero_for_tests()
    controller, app, _overlay = _gated_controller(
        monkeypatch, tmp_path, enabled=False, wait_for_speech_model=False
    )
    try:
        _join_speech_model_loader()
        assert silero_vad.loaded_session() is None

        controller._settings_store._settings = replace(
            controller.settings, silence_gate_enabled=True
        )
        controller.reload_settings(re_register_hotkey=False)
        _join_speech_model_loader()

        assert silero_vad.loaded_session() is not None
    finally:
        controller.shutdown()
    _ = app


def test_a_scan_cut_short_by_its_budget_never_skips(monkeypatch, tmp_path, real_silero):
    # Past the budget the rest of the recording was never looked at, so a
    # low score so far proves nothing about it.
    monkeypatch.setattr("stt_app.controller.SILERO_BATCH_MAX_SCAN_S", 1.0)
    controller, app, _overlay = _gated_controller(monkeypatch, tmp_path)

    _stop_with(controller, wav_bytes(typing(120, 3.0)))

    assert len(controller._executor.calls) == 1
    controller.shutdown()
    _ = app


def test_a_speech_check_skip_marks_the_recording_its_persist_wrote(
    monkeypatch, tmp_path, real_silero
):
    controller, app, overlay = _gated_controller(monkeypatch, tmp_path)
    store = FakeLastRecordingStore()
    controller._last_recording_store = store

    _stop_with(controller, wav_bytes(typing(120, 3.0)))

    assert controller._executor.calls == []
    assert store.canceled_ids == [controller._last_persisted_recording_id]
    assert controller._last_persisted_recording_id
    assert "speech check" in store.canceled[-1]
    assert "the recording is kept" in overlay.states[-1][1]
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
    controller._settings = replace(controller._settings, insert_target="current_window")

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
    (space-separated) and inserted in one cycle instead -- and the foreground
    result that triggered the flush joins them too. It used to follow as a
    second paste within the same call, overwriting the clipboard while the
    target may not have read the first one yet.
    """
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    beeps: list[None] = []
    monkeypatch.setattr(controller, "_play_completion_beep", lambda: beeps.append(None))

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
        ("transcript A. transcript B. transcript C.", 321, "auto"),
    ]
    # The overlay still shows the foreground result, and Copy/Edit act on it.
    assert overlay.states[-1] == ("Done", "transcript C.")
    assert controller._last_transcript == "transcript C."
    assert controller._last_history_entry is not None
    assert controller._last_history_entry.text == "transcript C."
    # One paste, one completion tone.
    assert len(beeps) == 1
    assert [e.text for e in history.load()] == [
        "transcript A.",
        "transcript B.",
        "transcript C.",
    ]
    assert controller._deferred_background_results == []
    assert controller._jobs == {}
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


class SelectiveTextInserter(PacedTextInserter):
    """Fails the pastes whose text contains one of the configured words.

    `fail` raises the ordinary `TextInsertionError` (nothing reached the
    target); `maybe` raises `TextMayHaveBeenPastedError` (the keystroke went
    out and only the cleanup failed).
    """

    def __init__(self):
        super().__init__()
        self.fail: set[str] = set()
        self.maybe: set[str] = set()

    def insert_text_with_options(
        self,
        text,
        target_hwnd=None,
        paste_mode="auto",
        restore_clipboard=True,
    ):
        if any(word in text for word in self.maybe):
            self.calls.append((text, target_hwnd, paste_mode))
            self.paste_times.append(self.now)
            self.last_keystroke_at = self.now
            raise TextMayHaveBeenPastedError("clipboard restore failed")
        if any(word in text for word in self.fail):
            self.calls.append((text, target_hwnd, paste_mode))
            raise TextInsertionError("Target window focus could not be restored.")
        return super().insert_text_with_options(
            text,
            target_hwnd=target_hwnd,
            paste_mode=paste_mode,
            restore_clipboard=restore_clipboard,
        )


def _fake_clipboard(monkeypatch):
    """Keep a failing insert's clipboard fallback off the real clipboard."""

    class FakeClipboard:
        def __init__(self):
            self.texts = []

        def setText(self, text):
            self.texts.append(text)

    clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: clipboard)
    return clipboard


def _record_several(controller, count):
    return [_record_and_stop(controller) for _ in range(count)]


def _queue_labels(overlay):
    return [label for _token, label in overlay.queue_updates[-1]]


# -- Paste pacing ------------------------------------------------------------


def test_a_background_result_inside_the_restore_window_waits_and_coalesces(
    monkeypatch, tmp_path, caplog
):
    """Two results 300 ms apart after a paste go out as one paste, later.

    A SendInput paste returns at the keystroke and the clipboard is restored
    `CLIPBOARD_RESTORE_DELAY_S` afterwards, because a target may read the
    clipboard that late. A second paste inside that window overwrote the
    clipboard the first target had not read yet. Results arriving inside
    the window wait for its end and are joined into one paste.
    """
    caplog.set_level(logging.INFO, logger="test.controller.queue")
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=PacedTextInserter(),
        immediate_background_insert=True,
    )
    try:
        token_a, token_b, token_c, token_d = _record_several(controller, 4)
        assert controller._active_request_token == token_d

        inserter.now = 1000.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        assert inserter.calls == [("transcript A.", 321, "auto")]

        inserter.now = 1000.2
        controller._on_transcription_ready("transcript B.", request_token=token_b)
        assert len(inserter.calls) == 1, inserter.calls
        assert controller._paste_pace_timer.isActive()
        assert controller._paste_pace_timer.interval() == 1300

        inserter.now = 1000.5
        controller._on_transcription_ready("transcript C.", request_token=token_c)
        assert len(inserter.calls) == 1, inserter.calls
        assert controller._paste_pace_timer.interval() == 1000
        assert [job.token for job, _ in controller._deferred_background_results] == [
            token_b,
            token_c,
        ]
        assert "paste_paced wait_ms=1300 joined=1" in caplog.text
        assert "paste_paced wait_ms=1000 joined=2" in caplog.text

        inserter.now = 1001.5
        controller._on_paste_pace_timeout()

        assert inserter.calls[1:] == [("transcript B. transcript C.", 321, "auto")]
        assert inserter.paste_times == [1000.0, 1001.5]
        assert controller._deferred_background_results == []
        assert set(controller._jobs) == {token_d}
        assert [e.text for e in history.load()] == [
            "transcript A.",
            "transcript B.",
            "transcript C.",
        ]
    finally:
        controller.shutdown()
    _ = app


def test_a_background_result_outside_the_restore_window_pastes_at_once(
    monkeypatch, tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="test.controller.queue")
    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=PacedTextInserter(),
        immediate_background_insert=True,
    )
    try:
        token_a, token_b, _token_c = _record_several(controller, 3)
        inserter.now = 1000.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        inserter.now = 1001.5
        controller._on_transcription_ready("transcript B.", request_token=token_b)

        assert inserter.calls == [
            ("transcript A.", 321, "auto"),
            ("transcript B.", 321, "auto"),
        ]
        assert not controller._paste_pace_timer.isActive()
        assert "paste_paced" not in caplog.text
    finally:
        controller.shutdown()
    _ = app


def test_paced_results_for_different_targets_stay_separate_pastes(
    monkeypatch, tmp_path
):
    """The window is global (one clipboard); the coalescing stays per target.

    A late reader of the first paste reads whatever the clipboard holds then,
    whichever window the next paste is aimed at, so a paste into another
    window waits as well; but it is never joined with text for a different
    window, and the two go out one window apart.
    """
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=PacedTextInserter(),
        immediate_background_insert=True,
    )
    try:
        token_a = _record_and_stop(controller)
        focus.captured, focus.captured_focus, focus.captured_caret = 111, 222, 333
        token_b = _record_and_stop(controller)
        focus.captured, focus.captured_focus, focus.captured_caret = 987, 654, 321
        token_c = _record_and_stop(controller)
        _record_and_stop(controller)

        inserter.now = 1000.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        inserter.now = 1000.2
        controller._on_transcription_ready("transcript B.", request_token=token_b)
        inserter.now = 1000.4
        controller._on_transcription_ready("transcript C.", request_token=token_c)
        assert len(inserter.calls) == 1
        assert controller._paste_pace_timer.interval() == 1100

        inserter.now = 1001.5
        controller._on_paste_pace_timeout()
        assert inserter.calls[1:] == [("transcript B.", 333, "auto")]
        assert controller._paste_pace_timer.isActive()
        assert controller._paste_pace_timer.interval() == 1500

        inserter.now = 1003.0
        controller._on_paste_pace_timeout()
        assert inserter.calls[2:] == [("transcript C.", 321, "auto")]
        assert inserter.paste_times == [1000.0, 1001.5, 1003.0]
    finally:
        controller.shutdown()
    _ = app


def test_a_paced_foreground_result_shows_at_once_and_pastes_after_the_window(
    monkeypatch, tmp_path
):
    """The overlay, history and Copy/Edit do not wait; only the paste does.

    The paste then goes out at the end of the window even though a newer
    transcription is in flight by then: it was the foreground result, and
    nothing about a newer job changes where it belongs.
    """
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=PacedTextInserter()
    )
    beeps: list[None] = []
    monkeypatch.setattr(controller, "_play_completion_beep", lambda: beeps.append(None))
    try:
        token_a = _record_and_stop(controller)
        inserter.now = 1000.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        assert inserter.calls == [("transcript A.", 321, "auto")]
        assert beeps == [None]

        token_b = _record_and_stop(controller)
        inserter.now = 1000.4
        controller._on_transcription_ready("transcript B.", request_token=token_b)

        assert len(inserter.calls) == 1
        assert overlay.states[-1] == ("Done", "transcript B.")
        assert controller._last_transcript == "transcript B."
        assert controller._last_history_entry.text == "transcript B."
        assert [e.text for e in history.load()] == ["transcript A.", "transcript B."]
        # Held by the pace alone, so not listed as a queue row.
        assert _queue_labels(overlay) == []
        assert beeps == [None]

        token_c = _record_and_stop(controller)
        assert controller._active_request_token == token_c

        inserter.now = 1001.5
        controller._on_paste_pace_timeout()

        assert inserter.calls[1:] == [("transcript B.", 321, "auto")]
        assert beeps == [None, None]
        assert set(controller._jobs) == {token_c}
        assert overlay.states[-1] == ("Processing", "Transcribing audio...")
    finally:
        controller.shutdown()
    _ = app


def test_a_joined_foreground_paste_that_fails_offers_the_joined_text(
    monkeypatch, tmp_path
):
    """The existing rule for a coalesced queue paste, now for the foreground.

    The failed paste carried both transcripts, so the Error shows, copies and
    offers Insert for both; there is no single history entry, so Edit
    refuses, and the clipboard is left alone as for every queued paste.
    """

    class FakeClipboard:
        value = "user clipboard"

        def setText(self, text):
            self.value = text

        def text(self):
            return self.value

    clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: clipboard)
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=FakeTextInserter(True)
    )
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    try:
        token_a = _record_and_stop(controller)
        controller.start_recording()
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        controller.stop_recording()
        token_b = controller._active_request_token

        controller._on_transcription_ready("transcript B.", request_token=token_b)

        joined = "transcript A. transcript B."
        assert inserter.calls == [(joined, 321, "auto")]
        state, detail = overlay.states[-1]
        assert state == "Error"
        assert detail.endswith(joined)
        kwargs = overlay.state_kwargs[-1]
        assert kwargs["copy_text"] == joined
        assert kwargs["error_action"] == "insert"
        assert controller._last_transcript == joined
        assert controller._insert_action_text == joined
        assert controller._last_history_entry is None
        assert clipboard.text() == "user clipboard"
        assert "2 queued transcriptions" in messages[-1]
    finally:
        controller.shutdown()
    _ = app


def test_shutdown_stops_the_paste_pace_timer(monkeypatch, tmp_path):
    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=PacedTextInserter(),
        immediate_background_insert=True,
    )
    token_a, token_b, _token_c = _record_several(controller, 3)
    inserter.now = 1000.0
    controller._on_transcription_ready("transcript A.", request_token=token_a)
    inserter.now = 1000.2
    controller._on_transcription_ready("transcript B.", request_token=token_b)
    assert controller._paste_pace_timer.isActive()

    controller.shutdown()

    assert not controller._paste_pace_timer.isActive()
    _ = app


# -- Re-paste of the last delivered text -------------------------------------


def test_a_re_paste_after_a_background_success_pastes_that_jobs_text(
    monkeypatch, tmp_path
):
    """ "Insert transcript again" means the text that reached a window last.

    A queued result pasted in the background is the last thing that landed
    in a document, while the overlay keeps showing the foreground result:
    the re-paste pastes the queued one, Copy and Edit keep the shown one.
    Once the next foreground result is on screen it is the last delivered
    text again.
    """
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", immediate_background_insert=True
    )
    try:
        token_x = _record_and_stop(controller)
        controller._on_transcription_ready("transcript X.", request_token=token_x)
        token_a = _record_and_stop(controller)
        token_b = _record_and_stop(controller)
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        assert inserter.calls[-1] == ("transcript A.", 321, "auto")
        painted = list(overlay.states)

        controller.repaste_last_transcript()

        assert inserter.calls[-1] == ("transcript A.", 321, "auto")
        assert len(inserter.calls) == 3
        assert overlay.states == painted
        assert controller._last_transcript == "transcript X."
        assert controller._last_history_entry.text == "transcript X."

        controller._on_transcription_ready("transcript B.", request_token=token_b)
        controller.repaste_last_transcript()
        assert inserter.calls[-1] == ("transcript B.", 321, "auto")
        assert overlay.states[-1] == ("Done", "transcript B.")
    finally:
        controller.shutdown()
    _ = app


def test_a_re_paste_after_a_coalesced_background_paste_pastes_the_joined_text(
    monkeypatch, tmp_path
):
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    try:
        token_a = _record_and_stop(controller)
        controller.start_recording()
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        controller.stop_recording()
        token_b = controller._active_request_token
        controller.start_recording()
        controller._on_transcription_ready("transcript B.", request_token=token_b)

        controller.cancel_current_action()

        joined = "transcript A. transcript B."
        assert inserter.calls == [(joined, 321, "auto")]

        controller.repaste_last_transcript()

        assert inserter.calls[-1] == (joined, 321, "auto")
        assert overlay.states[-1] == ("Done", joined)
        assert controller._last_transcript == joined
        assert controller._last_history_entry is None
    finally:
        controller.shutdown()
    _ = app


# -- Transcripts that were not inserted (field report, 2026-10-01) -----------


def _not_inserted_rows(overlay):
    return [
        (token, label)
        for token, label in overlay.queue_updates[-1]
        if "inserted" in label.lower() and "pending insert" not in label.lower()
    ]


def test_every_failed_queued_paste_stays_listed_and_is_not_overwritten(
    monkeypatch, tmp_path
):
    """Six queued results, two of them fail: both stay marked until handled.

    The single Insert offer held one text, a later failure replaced it, and
    with a transcription running it was never painted at all -- the owner
    noticed only later that two transcripts were missing from his prompt.
    Each failed paste now keeps a queue row of its own, and the tray message
    says how many are waiting and how to insert them.
    """
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript B.", "transcript E."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=inserter,
        immediate_background_insert=True,
        repaste_hotkey="F10",
    )
    controller._repaste_hotkey_registration_ok = True
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    try:
        tokens = _record_several(controller, 7)
        painted = list(overlay.states)
        for index, name in enumerate("ABCDEF"):
            inserter.now += 10.0
            controller._on_transcription_ready(
                f"transcript {name}.", request_token=tokens[index]
            )

        rows = _not_inserted_rows(overlay)
        assert len(rows) == 2, overlay.queue_updates[-1]
        assert "transcript B." in rows[0][1]
        assert "transcript E." in rows[1][1]
        assert all("Not inserted" in label for _token, label in rows)
        assert len(messages) == 2
        assert "2 transcripts are waiting to be inserted" in messages[-1]
        assert "F10" in messages[-1]
        assert "Insert transcript again" in messages[-1]
        # A transcription is still running, so nothing was painted over it.
        assert overlay.states == painted
    finally:
        controller.shutdown()
    _ = app


def test_the_re_paste_inserts_every_waiting_transcript_while_a_job_runs(
    monkeypatch, tmp_path
):
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript A.", "transcript C."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=inserter,
        immediate_background_insert=True,
    )
    busy: list[str] = []
    controller.busy_overlay_error.connect(busy.append)
    try:
        token_a, token_b, token_c, token_d = _record_several(controller, 4)
        for token, name in zip((token_a, token_b, token_c), "ABC", strict=True):
            inserter.now += 10.0
            controller._on_transcription_ready(
                f"transcript {name}.", request_token=token
            )
        assert len(_not_inserted_rows(overlay)) == 2
        painted = list(overlay.states)
        inserter.fail = set()
        inserter.now += 10.0

        controller.repaste_last_transcript()

        assert inserter.calls[-1] == ("transcript A. transcript C.", 321, "auto")
        assert _not_inserted_rows(overlay) == []
        assert [token for token, _ in overlay.queue_updates[-1]] == [token_d]
        assert overlay.states == painted
        assert busy == []
        assert controller._active_request_token == token_d
    finally:
        controller.shutdown()
    _ = app


def test_the_re_paste_of_waiting_transcripts_is_refused_while_a_recording_stops(
    monkeypatch, tmp_path
):
    """The refusal names the waiting transcripts and how to insert them.

    During a stop the recording's target snapshot is being taken; an open
    batch capture alone no longer refuses (owner's decision, 2026-10-01).
    """
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript A."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=inserter,
        immediate_background_insert=True,
        # Configured, but held by another program: never named as the way.
        repaste_hotkey="F10",
    )
    busy: list[str] = []
    controller.busy_overlay_error.connect(busy.append)
    try:
        token_a, _token_b = _record_several(controller, 2)
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        controller._recording_stop_in_progress = True
        inserter.fail = set()
        pasted = list(inserter.calls)

        controller.repaste_last_transcript()

        assert inserter.calls == pasted
        assert len(busy) == 1, busy
        assert "Wait for the recording to start or stop" in busy[0]
        assert "1 transcript is waiting to be inserted" in busy[0]
        assert "F10" not in busy[0]
        assert len(_not_inserted_rows(overlay)) == 1
    finally:
        controller._recording_stop_in_progress = False
        controller.shutdown()
    _ = app


def test_a_paste_that_may_have_landed_is_listed_but_never_re_pasted(
    monkeypatch, tmp_path
):
    """No double paste: the keystroke went out, only the cleanup failed."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.maybe = {"transcript A."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=inserter,
        immediate_background_insert=True,
    )
    try:
        token_x = _record_and_stop(controller)
        controller._on_transcription_ready("transcript X.", request_token=token_x)
        token_a, _token_b = _record_several(controller, 2)
        inserter.now += 10.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        rows = _not_inserted_rows(overlay)
        assert len(rows) == 1
        assert "Possibly inserted" in rows[0][1]
        inserter.maybe = set()
        inserter.now += 10.0

        controller.repaste_last_transcript()

        assert all("transcript A." not in call[0] for call in inserter.calls[2:])
        assert len(_not_inserted_rows(overlay)) == 1
    finally:
        controller.shutdown()
    _ = app


def test_a_not_inserted_row_is_dismissed_by_its_cancel_button_and_clear_queue(
    monkeypatch, tmp_path
):
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript A.", "transcript B."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=inserter,
        immediate_background_insert=True,
    )
    try:
        token_a, token_b, _token_c = _record_several(controller, 3)
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        inserter.now += 10.0
        controller._on_transcription_ready("transcript B.", request_token=token_b)
        rows = _not_inserted_rows(overlay)
        assert len(rows) == 2

        controller.cancel_queued_transcription(rows[0][0])

        remaining = _not_inserted_rows(overlay)
        assert len(remaining) == 1 and "transcript B." in remaining[0][1]

        controller.clear_transcription_queue()

        assert _not_inserted_rows(overlay) == []
        assert overlay.queue_updates[-1] == []
    finally:
        controller.shutdown()
    _ = app


def test_a_not_inserted_row_survives_the_next_recording_and_insert_retires_it(
    monkeypatch, tmp_path
):
    """The overlay's offer is cleared at the next recording start; the row is not.

    A foreground paste that failed shows its Error with Insert, which the
    next recording replaces. The row keeps the text visible after that, and
    the re-paste, once it succeeds, retires the row it inserted. (The Insert
    retires its own row, by identity: `test_the_overlay_insert_retires_only_
    its_own_row`.)
    """

    class FakeClipboard:
        def setText(self, text):
            pass

    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: FakeClipboard())
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript A."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_a = _record_and_stop(controller)
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        assert overlay.states[-1][0] == "Error"
        assert len(_not_inserted_rows(overlay)) == 1

        controller.start_recording()
        assert controller._insert_action_text == ""
        assert len(_not_inserted_rows(overlay)) == 1
        controller.cancel_current_action()

        inserter.fail = set()
        inserter.now += 10.0
        controller.repaste_last_transcript()

        assert inserter.calls[-1] == ("transcript A.", 321, "auto")
        assert _not_inserted_rows(overlay) == []
    finally:
        controller.shutdown()
    _ = app


def test_a_re_paste_of_waiting_transcripts_that_fails_keeps_them_and_says_so(
    monkeypatch, tmp_path
):
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript A."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=inserter,
        immediate_background_insert=True,
    )
    busy: list[str] = []
    controller.busy_overlay_error.connect(busy.append)
    try:
        token_a, _token_b = _record_several(controller, 2)
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        painted = list(overlay.states)
        inserter.now += 10.0

        controller.repaste_last_transcript()

        assert inserter.calls[-1][0] == "transcript A."
        assert len(_not_inserted_rows(overlay)) == 1
        assert overlay.states == painted
        assert len(busy) == 1
        assert "Target window focus could not be restored." in busy[0]
    finally:
        controller.shutdown()
    _ = app


def test_a_re_paste_whose_keystroke_went_out_is_never_offered_again(
    monkeypatch, tmp_path
):
    """The re-paste of waiting transcripts fails after its keystroke.

    The text is most likely in the window now. The row stays listed as
    possibly inserted, and a second press of the re-paste must not paste it a
    second time.
    """
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript A."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=inserter,
        immediate_background_insert=True,
    )
    try:
        token_a, _token_b = _record_several(controller, 2)
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        assert "Not inserted" in _not_inserted_rows(overlay)[0][1]
        inserter.fail = set()
        inserter.maybe = {"transcript A."}
        inserter.now += 10.0

        controller.repaste_last_transcript()

        rows = _not_inserted_rows(overlay)
        assert len(rows) == 1
        assert "Possibly inserted" in rows[0][1]
        attempts = [call for call in inserter.calls if call[0] == "transcript A."]
        inserter.maybe = set()
        inserter.now += 10.0

        controller.repaste_last_transcript()

        assert [
            call for call in inserter.calls if call[0] == "transcript A."
        ] == attempts
    finally:
        controller.shutdown()
    _ = app


def test_a_waiting_insert_row_reaches_the_overlay_as_its_own_kind(
    monkeypatch, tmp_path
):
    """The overlay labels the button Dismiss for a waiting insert; the row's
    text no longer has to explain a Cancel button."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript A."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch,
        tmp_path,
        mode="insert",
        inserter=inserter,
        immediate_background_insert=True,
    )
    try:
        token_a, token_b = _record_several(controller, 2)
        controller._on_transcription_ready("transcript A.", request_token=token_a)

        rows = overlay.queue_updates[-1]
        kinds = overlay.queue_kinds[-1]
        assert rows[0][0] == token_b
        assert kinds == [QUEUE_ROW_KIND_TRANSCRIPTION, QUEUE_ROW_KIND_UNDELIVERED]
        assert "Not inserted" in rows[1][1]
        assert "Cancel" not in rows[1][1]
    finally:
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
    controller, app, _overlay, inserter, focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    # Recorded for another window: a result for the stream's own window would
    # go ahead of the stream at once instead of waiting for it.
    focus.captured = 111
    token_a = _record_and_stop(controller)
    focus.captured = 987
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
    controller, app, overlay, inserter, focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    # Recorded for another window: a result for the stream's own window would
    # go ahead of the stream at once instead of waiting for it.
    focus.captured = 111
    token_a = _record_and_stop(controller)
    focus.captured = 987
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    assert controller._deferred_background_results

    controller._on_stream_runtime_failed(
        controller._stream_session_token, "stream died"
    )

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

    controller._on_stream_runtime_failed(
        controller._stream_session_token, "stream died"
    )

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

    controller._on_stream_runtime_failed(
        controller._stream_session_token, "stream died"
    )

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
    # The foreground result C shares the one target as well, so it joins the
    # deferred paste instead of following it as a second clipboard write.
    assert pastes == ["transcript A transcript B transcript C"], (
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
        pytest.param(lambda c, _token: c.clear_transcription_queue(), id="clear-queue"),
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
        f"the poll overwrote the {finished_state} state with {overlay.states[-1]!r}"
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
    """ "A finalize will deliver this" is not "a finalize did deliver".

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

    controller._on_stream_runtime_failed(
        controller._stream_session_token, "stream died"
    )

    assert job.stashed_partial == "ein ganzer satz den ich diktiert habe", (
        "the partial was dropped instead of handed to the pending finalize"
    )
    assert [e.text for e in history.load()] == [], "it was written twice"

    # The finalize now delivers nothing, which is exactly when the stash is
    # the only remaining copy.
    controller._finish_transcription_job(999)

    assert [e.text for e in history.load()] == ["ein ganzer satz den ich diktiert habe"]
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

    controller._on_stream_runtime_failed(
        controller._stream_session_token, "stream died"
    )

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

    controller._on_stream_runtime_failed(
        controller._stream_session_token, "stream died"
    )

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


# -- Re-paste and the pace; results held only by the pace -------------------


def test_a_re_paste_inside_the_restore_window_waits_for_it(monkeypatch, tmp_path):
    """F10 0.3 s after a paste overwrote the clipboard that paste may still
    be read from, exactly what the pace exists to prevent for every other
    paste. It is held and goes out when the window ends."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript A."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_a = _record_and_stop(controller)
        inserter.now = 990.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        assert len(_not_inserted_rows(overlay)) == 1
        token_b = _record_and_stop(controller)
        inserter.now = 1000.0
        controller._on_transcription_ready("transcript B.", request_token=token_b)
        inserter.fail = set()
        inserter.now = 1000.3

        controller.repaste_last_transcript()

        assert [call[0] for call in inserter.calls] == [
            "transcript A.",
            "transcript B.",
        ]
        assert controller._paste_pace_timer.isActive()
        inserter.now = 1001.5
        controller._on_paste_pace_timeout()

        assert [call[0] for call in inserter.calls][2:] == ["transcript A."]
        assert inserter.paste_times == [1000.0, 1001.5]
        assert _not_inserted_rows(overlay) == []
    finally:
        controller.shutdown()
    _ = app


def test_the_overlay_insert_inside_the_restore_window_waits_for_it(
    monkeypatch, tmp_path
):
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_a = _record_and_stop(controller)
        inserter.now = 1000.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        controller._insert_action_text = "offered text."
        inserter.now = 1000.3

        controller.insert_failed_text()

        assert [call[0] for call in inserter.calls] == ["transcript A."]
        inserter.now = 1001.5
        controller._on_paste_pace_timeout()
        assert [call[0] for call in inserter.calls] == [
            "transcript A.",
            "offered text.",
        ]
    finally:
        controller.shutdown()
    _ = app


def test_f10_on_a_result_the_pace_holds_never_pastes_it_twice(monkeypatch, tmp_path):
    """The shown transcript is already on its way; F10 must not send it ahead
    of the pace and then let the timer send it again."""
    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=PacedTextInserter()
    )
    try:
        token_a = _record_and_stop(controller)
        inserter.now = 1000.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        token_b = _record_and_stop(controller)
        inserter.now = 1000.5
        controller._on_transcription_ready("transcript B.", request_token=token_b)
        inserter.now = 1000.8

        controller.repaste_last_transcript()

        # Every timer that would run, each after its window: a duplicate held
        # behind B's own paste would go out on the second one.
        for now in (1002.5, 1004.5):
            inserter.now = now
            controller._on_paste_pace_timeout()
        assert [call[0] for call in inserter.calls] == [
            "transcript A.",
            "transcript B.",
        ]
        assert controller._pending_repaste is None
    finally:
        controller.shutdown()
    _ = app


def test_a_paced_result_a_recording_then_holds_is_listed_again(monkeypatch, tmp_path):
    """Once a recording defers it, it waits for an unbounded time: the panel
    lists it as "Pending insert" again, as every deferred result."""
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=PacedTextInserter()
    )
    try:
        token_a = _record_and_stop(controller)
        inserter.now = 1000.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        token_b = _record_and_stop(controller)
        inserter.now = 1000.4
        controller._on_transcription_ready("transcript B.", request_token=token_b)
        assert _queue_labels(overlay) == []

        controller.start_recording()
        inserter.now = 1001.5
        controller._on_paste_pace_timeout()

        assert inserter.calls == [("transcript A.", 321, "auto")]
        assert overlay.queue_updates[-1][0][0] == token_b
        assert "Pending insert" in overlay.queue_updates[-1][0][1]
    finally:
        controller.shutdown()
    _ = app


def test_re_paste_skips_a_possibly_inserted_last_transcript(monkeypatch, tmp_path):
    """A paste whose keystroke may have gone out is never pasted again, also
    not through the last-transcript fallback, and its row stays listed."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.maybe = {"transcript A."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_a = _record_and_stop(controller)
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        rows = _not_inserted_rows(overlay)
        assert len(rows) == 1 and "Possibly inserted" in rows[0][1]
        inserter.maybe = set()
        inserter.now += 10.0

        controller.repaste_last_transcript()

        assert [call[0] for call in inserter.calls] == ["transcript A."]
        assert _not_inserted_rows(overlay) == rows
    finally:
        controller.shutdown()
    _ = app


def test_a_paste_of_the_same_text_leaves_a_possibly_inserted_row_listed(
    monkeypatch, tmp_path
):
    """Only the user's Dismiss takes a possibly-landed row away.

    A later dictation with the same text fails before its keystroke; the
    overlay's Insert on it retires its own row, never the possibly-landed one.
    """
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.maybe = {"transcript A."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_a = _record_and_stop(controller)
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        possibly = _not_inserted_rows(overlay)
        assert len(possibly) == 1 and "Possibly inserted" in possibly[0][1]
        inserter.maybe = set()
        inserter.fail = {"transcript A."}
        inserter.now += 10.0
        token_b = _record_and_stop(controller)
        controller._on_transcription_ready("transcript A.", request_token=token_b)
        assert len(_not_inserted_rows(overlay)) == 2
        inserter.fail = set()
        inserter.now += 10.0

        controller.insert_failed_text()

        assert [call[0] for call in inserter.calls][-1] == "transcript A."
        assert _not_inserted_rows(overlay) == possibly
    finally:
        controller.shutdown()
    _ = app


@pytest.mark.parametrize("action", ["clear", "cancel"])
def test_a_result_the_pace_holds_survives_clear_and_cancel(
    monkeypatch, tmp_path, action
):
    """The overlay already shows it as Done; dropping it silently lost a paste
    the user saw finish."""
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=PacedTextInserter()
    )
    try:
        token_a = _record_and_stop(controller)
        inserter.now = 1000.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        token_b = _record_and_stop(controller)
        inserter.now = 1000.4
        controller._on_transcription_ready("transcript B.", request_token=token_b)
        assert overlay.states[-1] == ("Done", "transcript B.")

        if action == "clear":
            controller.clear_transcription_queue()
        else:
            controller.cancel_queued_transcription(token_b)

        inserter.now = 1001.5
        controller._on_paste_pace_timeout()
        assert inserter.calls[1:] == [("transcript B.", 321, "auto")]
        assert overlay.states[-1] == ("Done", "transcript B.")
    finally:
        controller.shutdown()
    _ = app


def test_a_result_the_pace_holds_is_not_listed_in_the_queue(monkeypatch, tmp_path):
    """Held for at most the restore delay, it flashed the panel open and shut
    as a "Pending insert" row under a "Transcribing 1 recording" title."""
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=PacedTextInserter()
    )
    try:
        token_a = _record_and_stop(controller)
        inserter.now = 1000.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        token_b = _record_and_stop(controller)
        inserter.now = 1001.0
        updates_before = len(overlay.queue_updates)

        controller._on_transcription_ready("transcript B.", request_token=token_b)
        inserter.now = 1001.5
        controller._on_paste_pace_timeout()

        assert [call[0] for call in inserter.calls] == [
            "transcript A.",
            "transcript B.",
        ]
        assert overlay.queue_updates[updates_before:], "precondition"
        assert all(update == [] for update in overlay.queue_updates[updates_before:]), (
            overlay.queue_updates[updates_before:]
        )
    finally:
        controller.shutdown()
    _ = app


# -- Re-paste during an open batch capture (owner's decision, 2026-10-01) ----


def test_a_re_paste_during_a_batch_recording_inserts_the_waiting_rows(
    monkeypatch, tmp_path
):
    """The paste goes to the current focus; the recording keeps its own
    target snapshot and its overlay session, and the microphone stays open."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript A."}
    controller, app, overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_a = _record_and_stop(controller)
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        assert len(_not_inserted_rows(overlay)) == 1
        controller.start_recording()
        assert controller._audio_capture is not None, "precondition"
        snapshot = (
            controller._target_window_handle,
            controller._target_focus_signature,
        )
        # The user has clicked into another window since the recording began.
        focus.current, focus.current_focus, focus.current_caret = 555, 556, 557
        states_before = list(overlay.states)
        inserter.fail = set()
        inserter.now += 10.0

        controller.repaste_last_transcript()

        assert inserter.calls[1:] == [("transcript A.", 557, "auto")]
        assert _not_inserted_rows(overlay) == []
        assert (
            controller._target_window_handle,
            controller._target_focus_signature,
        ) == snapshot
        assert overlay.states == states_before
        assert controller._audio_capture is not None
    finally:
        controller.shutdown()
    _ = app


def test_a_re_paste_during_a_batch_recording_waits_for_the_pace(monkeypatch, tmp_path):
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"transcript A."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_a = _record_and_stop(controller)
        inserter.now = 990.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        token_b = _record_and_stop(controller)
        inserter.now = 1000.0
        controller._on_transcription_ready("transcript B.", request_token=token_b)
        controller.start_recording()
        inserter.fail = set()
        inserter.now = 1000.3

        controller.repaste_last_transcript()

        assert [call[0] for call in inserter.calls][2:] == []
        inserter.now = 1001.5
        controller._on_paste_pace_timeout()
        assert [call[0] for call in inserter.calls][2:] == ["transcript A."]
        assert _not_inserted_rows(overlay) == []
    finally:
        controller.shutdown()
    _ = app


def test_a_clipboard_restore_that_failed_for_good_reaches_the_tray(
    monkeypatch, tmp_path
):
    """The inserter reports on its restore's timer thread; the controller
    hands that to a signal, which main.py shows in the tray."""

    class ReportingInserter(PacedTextInserter):
        def __init__(self):
            super().__init__()
            self.restore_failure_handler = None

        def set_restore_failure_handler(self, handler):
            self.restore_failure_handler = handler

    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=ReportingInserter()
    )
    reported: list[tuple[str, bool]] = []
    controller.clipboard_restore_failed.connect(
        lambda message: reported.append(
            (message, QtCore.QThread.currentThread() is app.thread())
        )
    )
    try:
        assert callable(inserter.restore_failure_handler)

        # Called from the restore's timer thread, as the inserter does.
        worker = threading.Thread(
            target=inserter.restore_failure_handler,
            args=("Your clipboard could not be put back.",),
        )
        worker.start()
        worker.join(timeout=5)
        deadline = time.monotonic() + 5
        while not reported and time.monotonic() < deadline:
            app.processEvents()

        # Delivered on the Qt thread, where the tray may be touched.
        assert reported == [("Your clipboard could not be put back.", True)]
    finally:
        controller.shutdown()
    _ = app


# -- Rows and held results are identified by identity, never by text --------


def test_the_overlay_insert_retires_only_its_own_row(monkeypatch, tmp_path):
    """Two dictations "okay." both fail: two rows. Insert pastes the one the
    offer is about, and the other dictation is still not inserted."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"okay."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_1 = _record_and_stop(controller)
        controller._on_transcription_ready("okay.", request_token=token_1)
        token_2 = _record_and_stop(controller)
        controller._on_transcription_ready("okay.", request_token=token_2)
        rows = _not_inserted_rows(overlay)
        assert len(rows) == 2
        inserter.fail = set()
        inserter.now += 10.0

        controller.insert_failed_text()

        assert [call[0] for call in inserter.calls] == ["okay.", "okay.", "okay."]
        # The offer is the newest failure's: its row goes, the older stays.
        assert _not_inserted_rows(overlay) == [rows[0]]
    finally:
        controller.shutdown()
    _ = app


def test_insert_on_an_offer_a_failed_repaste_painted_retires_its_row(
    monkeypatch, tmp_path
):
    """F10 re-pastes the listed row and fails before its keystroke; the offer
    it paints used to carry no row, so its successful Insert left the row
    listed and the next F10 pasted the same text a second time."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"okay."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token = _record_and_stop(controller)
        controller._on_transcription_ready("okay.", request_token=token)
        assert len(_not_inserted_rows(overlay)) == 1
        inserter.now += 10.0
        controller.repaste_last_transcript()
        assert overlay.states[-1][0] == "Error"
        inserter.fail = set()
        inserter.now += 10.0

        controller.insert_failed_text()

        assert _not_inserted_rows(overlay) == []
        inserter.now += 10.0
        token_next = _record_and_stop(controller)
        controller._on_transcription_ready("next.", request_token=token_next)
        inserter.now += 10.0
        controller.repaste_last_transcript()
        assert [call[0] for call in inserter.calls] == [
            "okay.",
            "okay.",
            "okay.",
            "next.",
            "next.",
        ]
    finally:
        controller.shutdown()
    _ = app


def test_insert_on_an_offer_a_failed_insert_painted_retires_its_row(
    monkeypatch, tmp_path
):
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"okay."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token = _record_and_stop(controller)
        controller._on_transcription_ready("okay.", request_token=token)
        inserter.now += 10.0
        controller.insert_failed_text()  # fails before its keystroke again
        assert overlay.states[-1][0] == "Error"
        assert len(_not_inserted_rows(overlay)) == 1
        inserter.fail = set()
        inserter.now += 10.0

        controller.insert_failed_text()

        assert _not_inserted_rows(overlay) == []
    finally:
        controller.shutdown()
    _ = app


def test_insert_on_a_failed_repaste_of_several_rows_retires_all_of_them(
    monkeypatch, tmp_path
):
    """A re-paste of two waiting rows is one joined text; its offer covers
    both rows, and its Insert retires both."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"first.", "second."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_1 = _record_and_stop(controller)
        controller._on_transcription_ready("first.", request_token=token_1)
        inserter.now += 10.0
        token_2 = _record_and_stop(controller)
        controller._on_transcription_ready("second.", request_token=token_2)
        assert len(_not_inserted_rows(overlay)) == 2
        inserter.now += 10.0
        controller.repaste_last_transcript()  # both rows, joined: fails
        assert overlay.states[-1][0] == "Error"
        inserter.fail = set()
        inserter.now += 10.0

        controller.insert_failed_text()

        assert _not_inserted_rows(overlay) == []
        assert "first." in inserter.calls[-1][0]
        assert "second." in inserter.calls[-1][0]
    finally:
        controller.shutdown()
    _ = app


def test_f10_pastes_the_shown_transcript_when_another_dictation_may_have_landed(
    monkeypatch, tmp_path
):
    """An older dictation "okay." may have landed and is listed so. A newer
    "okay." was inserted fine; F10 re-pastes the newer one -- a different
    dictation, which the text match refused."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.maybe = {"okay."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_1 = _record_and_stop(controller)
        controller._on_transcription_ready("okay.", request_token=token_1)
        assert "Possibly inserted" in _not_inserted_rows(overlay)[0][1]
        inserter.maybe = set()
        inserter.now += 10.0
        token_2 = _record_and_stop(controller)
        controller._on_transcription_ready("okay.", request_token=token_2)
        assert overlay.states[-1] == ("Done", "okay.")
        inserter.now += 10.0

        controller.repaste_last_transcript()

        assert [call[0] for call in inserter.calls] == ["okay.", "okay.", "okay."]
    finally:
        controller.shutdown()
    _ = app


def test_f10_refuses_the_shown_transcript_whose_own_paste_may_have_landed(
    monkeypatch, tmp_path
):
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.maybe = {"okay."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token = _record_and_stop(controller)
        controller._on_transcription_ready("okay.", request_token=token)
        inserter.maybe = set()
        inserter.now += 10.0

        controller.repaste_last_transcript()

        assert [call[0] for call in inserter.calls] == ["okay."]
        assert "may already have been inserted" in overlay.states[-1][1]
    finally:
        controller.shutdown()
    _ = app


def test_a_paced_insert_still_retires_its_row(monkeypatch, tmp_path):
    """The overlay's Insert inside the previous paste's restore window is held
    and run by the pace timer; it carries the offer's rows through the wait."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"okay."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token = _record_and_stop(controller)
        controller._on_transcription_ready("okay.", request_token=token)
        assert len(_not_inserted_rows(overlay)) == 1
        inserter.fail = set()
        # Another paste's keystroke just went out: the Insert waits for it.
        inserter.last_keystroke_at = inserter.now

        controller.insert_failed_text()

        assert [call[0] for call in inserter.calls] == ["okay."]
        inserter.now += 10.0
        controller._on_paste_pace_timeout()
        assert [call[0] for call in inserter.calls] == ["okay.", "okay."]
        assert _not_inserted_rows(overlay) == []
    finally:
        controller.shutdown()
    _ = app


def test_f10_pastes_a_row_whose_text_a_held_result_shares(monkeypatch, tmp_path):
    """A listed row "okay." and a pace-held foreground result "okay." are two
    dictations: F10 inserts the row after the held one, both go in."""
    _fake_clipboard(monkeypatch)
    inserter = SelectiveTextInserter()
    inserter.fail = {"okay."}
    controller, app, overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    try:
        token_1 = _record_and_stop(controller)
        inserter.now = 990.0
        controller._on_transcription_ready("okay.", request_token=token_1)
        assert len(_not_inserted_rows(overlay)) == 1
        inserter.fail = set()
        token_2 = _record_and_stop(controller)
        inserter.now = 1000.0
        controller._on_transcription_ready("first.", request_token=token_2)
        token_3 = _record_and_stop(controller)
        inserter.now = 1000.4
        controller._on_transcription_ready("okay.", request_token=token_3)
        inserter.now = 1000.5

        controller.repaste_last_transcript()

        for now in (1001.7, 1003.5):
            inserter.now = now
            controller._on_paste_pace_timeout()
        assert [call[0] for call in inserter.calls] == [
            "okay.",
            "first.",
            "okay.",
            "okay.",
        ]
        assert _not_inserted_rows(overlay) == []
    finally:
        controller.shutdown()
    _ = app


def test_f10_on_a_held_shown_transcript_says_so_in_the_tray(monkeypatch, tmp_path):
    """Skipped for a real reason -- it is about to be pasted -- and told."""
    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=PacedTextInserter()
    )
    busy: list[str] = []
    controller.busy_overlay_error.connect(busy.append)
    try:
        token_a = _record_and_stop(controller)
        inserter.now = 1000.0
        controller._on_transcription_ready("transcript A.", request_token=token_a)
        token_b = _record_and_stop(controller)
        inserter.now = 1000.5
        controller._on_transcription_ready("transcript B.", request_token=token_b)
        inserter.now = 1000.8

        controller.repaste_last_transcript()

        assert len(busy) == 1 and "about to be inserted" in busy[0], busy
        assert controller._pending_repaste is None
    finally:
        controller.shutdown()
    _ = app
