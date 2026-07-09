"""Tests for the concurrent-transcription modes and cooperative cancel.

These exercise the controller's per-job delivery and abort handling without
real worker threads by swapping in a deferred executor and driving the result
signals directly.
"""

import logging
from dataclasses import replace

from PySide6 import QtCore, QtGui

from stt_app.settings_store import AppSettings
from stt_app.text_inserter import TextInsertionError
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


def test_background_insert_failure_does_not_overwrite_clipboard(
    monkeypatch,
    tmp_path,
):
    controller, app, overlay, inserter, _focus, history = _make_queue_controller(
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


def test_immediate_insert_during_batch_recording_into_foreground_window(
    monkeypatch,
    tmp_path,
):
    """A finished result pastes mid-recording when its target is already the
    foreground window: the paste lands where the user is dictating anyway and
    no focus steal happens."""
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(
        controller._settings, immediate_background_insert=True
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    assert controller._audio_capture is not None

    controller._on_transcription_ready("msg A", request_token=token_a)

    assert inserter.calls == [("msg A", 321, "auto")]
    assert controller._deferred_background_results == []
    # No focus restore mid-recording: the target was already foreground.
    assert focus.restore_calls == []
    controller.shutdown()
    _ = app


def test_immediate_insert_defers_when_foreground_differs_mid_recording(
    monkeypatch,
    tmp_path,
):
    """Mid-recording insert is skipped when it would require a focus steal."""
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(
        controller._settings, immediate_background_insert=True
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()
    focus.current = 555  # the user switched windows mid-recording

    controller._on_transcription_ready("msg A", request_token=token_a)
    assert inserter.calls == []
    assert controller._deferred_background_results

    controller.stop_recording()
    # The capture is gone; the deferred result is delivered on the stop flush.
    assert inserter.calls == [("msg A", 321, "auto")]
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


def test_silence_gate_skips_transcription_of_silent_recording(
    monkeypatch,
    tmp_path,
):
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    controller._settings = replace(
        controller._settings, silence_gate_enabled=True
    )

    controller.start_recording()
    controller.stop_recording()

    # FakeCapture returns unparsable audio -> measured level 0.0 -> gated.
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


def test_background_failure_keeps_live_recording_session(monkeypatch, tmp_path):
    controller, app, overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )

    token_a = _record_and_stop(controller)
    controller.start_recording()

    controller._on_transcription_failed("provider down", request_token=token_a)

    assert overlay.states[-1][0] == "Listening"
    assert controller._audio_capture is not None
    assert token_a not in controller._jobs
    # The failed job's audio stays available for a manual retry.
    assert controller._last_failed_wav_bytes == b"RIFF"
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

    # Simulate an in-flight transcription still holding the cached transcriber.
    _record_and_stop(controller)
    sentinel = object()
    closed: list[object] = []
    monkeypatch.setattr(controller, "_close_cached_transcriber", closed.append)
    controller._transcriber_cache = sentinel
    controller._transcriber_cache_key = ("local", "small")

    # Saving settings while the job is active must not close the runtime now.
    controller.reload_settings(re_register_hotkey=False)

    assert controller._pending_transcriber_cache_reset is True
    assert controller._transcriber_cache is sentinel
    assert closed == []

    # The deferred reset is applied before the next transcriber is built, once
    # the active job is gone, so new settings/keys still take effect.
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _s, **kw: FakeStreamingTranscriber(),
    )
    controller._active_request_token = None
    built = controller._get_or_create_transcriber(controller.settings)

    # The stale runtime is closed exactly once; the real close no-ops on None.
    assert [c for c in closed if c is not None] == [sentinel]
    assert controller._pending_transcriber_cache_reset is False
    assert built is controller._transcriber_cache
    controller.shutdown()
    _ = app
