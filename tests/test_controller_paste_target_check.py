"""A paste into an element that is no text field is reported, not claimed.

A SendInput Ctrl+V into a focused button, list item or page body reports
success and inserts nothing. After the paste the controller asks
`PasteTargetCheck` whether the focus shows a caret; these tests drive that
answer by hand and pin what the user gets for each one.
"""

from __future__ import annotations

import logging

from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)
from PySide6.QtTest import QTest
from test_controller_queue import DeferredExecutor

from stt_app.config import (
    FALLBACK_HOTKEY,
    OVERLAY_ERROR_ACTION_INSERT,
    QUEUE_ROW_KIND_UNDELIVERED,
)
from stt_app.paste_target_check import (
    VERDICT_NOT_TEXT_FIELD,
    VERDICT_TEXT_FIELD,
    VERDICT_UNKNOWN,
    CaretReading,
)
from stt_app.settings_store import AppSettings
from stt_app.transcript_history import TranscriptHistoryStore


class FakePasteTargetCheck:
    """Holds each request until the test answers it."""

    def __init__(self, *, accept=True, raises=False):
        self.accept = accept
        self.raises = raises
        self.callbacks = []
        self.expected_foregrounds = []
        self.closed = False

    def request(self, callback, *, expected_foreground=None):
        if self.raises:
            raise RuntimeError("no thread")
        if not self.accept:
            return False
        self.callbacks.append(callback)
        self.expected_foregrounds.append(expected_foreground)
        return True

    def answer(self, verdict):
        callback = self.callbacks.pop(0)
        callback(CaretReading(verdict, 987, "fake"))

    def close(self):
        self.closed = True


def _make(monkeypatch, tmp_path, check, *, immediate_insert=False):
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _s, **kw: FakeStreamingTranscriber(),
    )
    FakeCapture.instances = []
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
        concurrent_transcription_mode="insert",
        immediate_background_insert=immediate_insert,
        silence_gate_enabled=False,
        repaste_hotkey="Ctrl+Alt+F10",
    )
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    controller, app = make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        text_inserter=inserter,
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=TranscriptHistoryStore(tmp_path / "history.json"),
        logger=logging.getLogger("test.controller.paste_target_check"),
        paste_target_check=check,
    )
    controller._executor = DeferredExecutor()
    beeps = []
    monkeypatch.setattr(controller, "_play_completion_beep", lambda: beeps.append(1))
    return controller, app, overlay, inserter, beeps


def _dictate(controller, text):
    controller.start_recording()
    controller.stop_recording()
    controller._on_transcription_ready(
        text, request_token=controller._active_request_token
    )


def _rows(overlay):
    """The undelivered rows the overlay was last given, as labels."""
    if not overlay.queue_updates:
        return []
    return [
        label
        for (_token, label), kind in zip(
            overlay.queue_updates[-1], overlay.queue_kinds[-1], strict=True
        )
        if kind == QUEUE_ROW_KIND_UNDELIVERED
    ]


def test_a_paste_into_no_text_field_is_reported_with_insert_and_no_tone(
    monkeypatch, tmp_path
):
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, beeps = _make(monkeypatch, tmp_path, check)

    _dictate(controller, "hello world")
    # The paste itself went out at once; only the report waits for the check.
    assert [call[0] for call in inserter.calls] == ["hello world"]
    assert overlay.states[-1] == ("Done", "hello world")
    assert beeps == []
    # The check is told which window the paste went to.
    assert check.expected_foregrounds == [987]

    check.answer(VERDICT_NOT_TEXT_FIELD)

    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "does not look like a text field" in detail
    assert "press Insert" in detail
    assert "hello world" in detail
    assert overlay.state_kwargs[-1]["error_action"] == OVERLAY_ERROR_ACTION_INSERT
    assert overlay.state_kwargs[-1]["copy_text"] == "hello world"
    assert beeps == []
    rows = _rows(overlay)
    assert len(rows) == 1 and rows[0].startswith("Not in a text field")
    controller.shutdown()
    _ = app


def test_f10_after_the_doubtful_report_pastes_it_again_and_retires_the_row(
    monkeypatch, tmp_path
):
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, beeps = _make(monkeypatch, tmp_path, check)
    _dictate(controller, "hello world")
    check.answer(VERDICT_NOT_TEXT_FIELD)
    assert len(_rows(overlay)) == 1

    # The user clicked into the field and pressed the re-paste hotkey.
    controller.repaste_last_transcript()

    assert [call[0] for call in inserter.calls] == ["hello world", "hello world"]
    assert _rows(overlay) == []
    check.answer(VERDICT_TEXT_FIELD)
    assert beeps == [1]
    assert overlay.states[-1] == ("Done", "hello world")
    # Nothing is left for a second press to paste a third time.
    assert controller._insert_action_text == ""
    controller.shutdown()
    _ = app


def test_a_repaste_into_no_text_field_again_stays_listed(monkeypatch, tmp_path):
    check = FakePasteTargetCheck()
    controller, app, overlay, _inserter, beeps = _make(monkeypatch, tmp_path, check)
    _dictate(controller, "hello world")
    check.answer(VERDICT_NOT_TEXT_FIELD)

    controller.insert_failed_text()
    check.answer(VERDICT_NOT_TEXT_FIELD)

    rows = _rows(overlay)
    assert len(rows) == 1 and rows[0].startswith("Not in a text field")
    assert overlay.states[-1][0] == "Error"
    assert overlay.state_kwargs[-1]["error_action"] == OVERLAY_ERROR_ACTION_INSERT
    assert beeps == []
    controller.shutdown()
    _ = app


def test_an_unknown_verdict_keeps_todays_behaviour(monkeypatch, tmp_path):
    check = FakePasteTargetCheck()
    controller, app, overlay, _inserter, beeps = _make(monkeypatch, tmp_path, check)
    _dictate(controller, "hello world")

    check.answer(VERDICT_UNKNOWN)

    assert overlay.states[-1] == ("Done", "hello world")
    assert beeps == [1]
    assert _rows(overlay) == []
    controller.shutdown()
    _ = app


def test_a_check_that_cannot_start_keeps_todays_behaviour(monkeypatch, tmp_path):
    for check in (
        FakePasteTargetCheck(raises=True),
        FakePasteTargetCheck(accept=False),
    ):
        controller, app, overlay, _inserter, beeps = _make(monkeypatch, tmp_path, check)
        _dictate(controller, "hello world")

        assert overlay.states[-1] == ("Done", "hello world")
        assert beeps == [1]
        assert _rows(overlay) == []
        controller.shutdown()
        _ = app


def test_a_check_that_never_answers_times_out_as_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr("stt_app.controller.PASTE_TARGET_CHECK_TIMEOUT_MS", 20)
    check = FakePasteTargetCheck()
    controller, app, overlay, _inserter, beeps = _make(monkeypatch, tmp_path, check)
    _dictate(controller, "hello world")
    assert beeps == []

    QTest.qWait(200)

    assert beeps == [1]
    assert overlay.states[-1] == ("Done", "hello world")
    # A late answer after the timeout changes nothing any more.
    check.answer(VERDICT_NOT_TEXT_FIELD)
    assert overlay.states[-1] == ("Done", "hello world")
    assert _rows(overlay) == []
    controller.shutdown()
    _ = app


def test_a_queued_paste_into_no_text_field_reaches_the_tray(monkeypatch, tmp_path):
    check = FakePasteTargetCheck()
    controller, app, overlay, _inserter, beeps = _make(
        monkeypatch, tmp_path, check, immediate_insert=True
    )
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    controller.start_recording()
    controller.stop_recording()
    token_a = controller._active_request_token
    controller.start_recording()
    # Immediate mode pastes the finished job at once, mid-recording.
    controller._on_transcription_ready("transcript A", request_token=token_a)

    check.answer(VERDICT_NOT_TEXT_FIELD)

    assert len(messages) == 1
    assert "does not look like a text field" in messages[0]
    assert "saved in history" in messages[0]
    # The live recording keeps the overlay.
    assert overlay.states[-1][0] == "Listening"
    assert beeps == []
    assert len(_rows(overlay)) == 1
    controller.shutdown()
    _ = app


def test_a_report_never_paints_over_a_newer_screen(monkeypatch, tmp_path):
    """The user started the next recording before the answer came."""
    check = FakePasteTargetCheck()
    controller, app, overlay, _inserter, _beeps = _make(monkeypatch, tmp_path, check)
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    _dictate(controller, "hello world")
    controller.start_recording()

    check.answer(VERDICT_NOT_TEXT_FIELD)

    assert overlay.states[-1][0] == "Listening"
    assert len(messages) == 1 and "does not look like a text field" in messages[0]
    assert len(_rows(overlay)) == 1
    controller.shutdown()
    _ = app


def test_shutdown_closes_the_check(monkeypatch, tmp_path):
    check = FakePasteTargetCheck()
    controller, app, _overlay, _inserter, _beeps = _make(monkeypatch, tmp_path, check)
    controller.shutdown()
    assert check.closed
    _ = app


def test_a_report_never_paints_over_a_newer_result(monkeypatch, tmp_path):
    """No session, but the overlay already shows something newer than the
    paste's own Done: that stays, and the tray carries the report."""
    check = FakePasteTargetCheck()
    controller, app, overlay, _inserter, _beeps = _make(monkeypatch, tmp_path, check)
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    _dictate(controller, "hello world")
    controller.show_overlay_notice("Transcript copied to the clipboard.")
    newer = overlay.states[-1]

    check.answer(VERDICT_NOT_TEXT_FIELD)

    assert overlay.states[-1] == newer
    assert len(messages) == 1 and "does not look like a text field" in messages[0]
    assert len(_rows(overlay)) == 1
    controller.shutdown()
    _ = app
