"""A queued transcript that was produced but not pasted must be reported.

Transcribing successfully and then silently failing to insert is
indistinguishable from a successful insert, which is exactly how a transcript
goes missing without the user noticing. These tests pin the reporting.
"""

import logging

from stt_app.config import FALLBACK_HOTKEY, OVERLAY_ERROR_ACTION_INSERT
from stt_app.settings_store import AppSettings
from stt_app.text_inserter import TextInsertionError
from stt_app.transcript_history import TranscriptHistoryStore

from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)
from test_controller_queue import DeferredExecutor


def _make_controller(monkeypatch, tmp_path, *, immediate_insert=False):
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
        concurrent_transcription_mode="insert",
        immediate_background_insert=immediate_insert,
        silence_gate_enabled=False,
    )
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    controller, app = make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        text_inserter=inserter,
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=history_store,
        logger=logging.getLogger("test.controller.background_insert"),
    )
    controller._executor = DeferredExecutor()
    return controller, app, overlay, inserter, history_store


def _fail_inserting(inserter, *failing_texts):
    failing = set(failing_texts)

    def insert_text_with_options(
        text,
        target_hwnd=None,
        paste_mode="auto",
        restore_clipboard=True,
    ):
        inserter.calls.append((text, target_hwnd, paste_mode))
        if not failing or text in failing:
            raise TextInsertionError("failed insert")
        return True

    inserter.insert_text_with_options = insert_text_with_options


def _record_and_stop(controller):
    controller.start_recording()
    controller.stop_recording()
    return controller._active_request_token


def test_failed_background_insert_emits_a_notification(monkeypatch, tmp_path):
    """The queued transcript A is produced but its paste fails."""
    controller, app, _overlay, inserter, history = _make_controller(
        monkeypatch, tmp_path
    )
    _fail_inserting(inserter, "transcript A")
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)

    token_a = _record_and_stop(controller)
    controller.start_recording()
    # A finishes while B records, so its insert is deferred.
    controller._on_transcription_ready("transcript A", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token
    # B's delivery unblocks the deferred insert of A, which then fails.
    controller._on_transcription_ready("transcript B", request_token=token_b)

    assert {entry.text for entry in history.load()} == {
        "transcript A",
        "transcript B",
    }
    assert len(messages) == 1
    assert "could not be inserted" in messages[0]
    assert "saved in history" in messages[0]
    controller.shutdown()
    _ = app


def test_failed_background_insert_offers_insert_on_a_free_overlay(
    monkeypatch,
    tmp_path,
):
    """Nothing newer claims the overlay, so the failure stays on screen."""
    controller, app, overlay, inserter, _history = _make_controller(
        monkeypatch, tmp_path
    )
    _fail_inserting(inserter, "transcript A")

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    # Canceling the new recording flushes the deferred insert of A, and
    # produces no result of its own that could overwrite the report.
    controller.cancel_current_action()

    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "could not be inserted" in detail
    # The transcript itself is shown; it is otherwise invisible.
    assert "transcript A" in detail
    # Retry re-transcribes and is wrong here: the transcript already exists.
    assert overlay.state_kwargs[-1].get("error_action") == OVERLAY_ERROR_ACTION_INSERT
    assert overlay.state_kwargs[-1].get("copy_text") == "transcript A"
    # Insert/Copy must act on exactly what the overlay shows.
    assert controller._last_transcript == "transcript A"
    controller.shutdown()
    _ = app


def test_failed_background_insert_does_not_hijack_a_live_session(
    monkeypatch,
    tmp_path,
):
    """A live recording owns the overlay, so only the notification reports it."""
    controller, app, overlay, inserter, _history = _make_controller(
        monkeypatch, tmp_path, immediate_insert=True
    )
    _fail_inserting(inserter, "transcript A")
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    controller._last_transcript = "newer transcript"

    token_a = _record_and_stop(controller)
    controller.start_recording()
    # Immediate mode inserts the finished job straight away, mid-recording.
    controller._on_transcription_ready("transcript A", request_token=token_a)

    assert len(messages) == 1
    assert overlay.states[-1][0] == "Listening"
    assert controller._last_transcript == "newer transcript"
    controller.shutdown()
    _ = app


def test_successful_background_insert_reports_nothing(monkeypatch, tmp_path):
    controller, app, _overlay, _inserter, history = _make_controller(
        monkeypatch, tmp_path
    )
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token
    controller._on_transcription_ready("transcript B", request_token=token_b)

    assert {entry.text for entry in history.load()} == {
        "transcript A",
        "transcript B",
    }
    assert messages == []
    controller.shutdown()
    _ = app


def test_failed_coalesced_flush_counts_every_lost_transcript(
    monkeypatch,
    tmp_path,
):
    """Two deferred results coalesce into one paste; one failure loses both."""
    controller, app, _overlay, inserter, _history = _make_controller(
        monkeypatch, tmp_path
    )
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)

    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token
    controller.start_recording()
    controller._on_transcription_ready("transcript B", request_token=token_b)
    controller.stop_recording()
    token_c = controller._active_request_token
    # Only the coalesced paste of A and B fails; C inserts normally.
    _fail_inserting(inserter, "transcript A transcript B")
    controller._on_transcription_ready("transcript C", request_token=token_c)

    assert len(messages) == 1
    assert "2 queued transcriptions were" in messages[0]
    controller.shutdown()
    _ = app
