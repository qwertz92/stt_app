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
from test_controller_queue import DeferredExecutor, PacedTextInserter

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
from stt_app.text_inserter import TextInsertionError
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
        callback(
            CaretReading(
                verdict,
                987,
                window_class="Chrome_WidgetWin_1",
                focus_class="Chrome_RenderWidgetHostHWND",
                gui_caret="none",
                msaa_caret="invisible",
                width=0,
                rechecks=2,
            )
        )

    def close(self):
        self.closed = True


def _make(monkeypatch, tmp_path, check, *, immediate_insert=False, inserter=None):
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
    inserter = inserter or FakeTextInserter()
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


def _check_lines(caplog):
    return [
        record
        for record in caplog.records
        if record.getMessage().startswith("paste_target_check ")
    ]


def test_every_check_logs_one_info_line_with_its_evidence_and_no_text(
    monkeypatch, tmp_path, caplog
):
    """Real use must show which applications the check misjudges."""
    caplog.set_level(logging.INFO, logger="test.controller.paste_target_check")
    check = FakePasteTargetCheck()
    controller, app, _overlay, _inserter, _beeps = _make(monkeypatch, tmp_path, check)

    _dictate(controller, "hello world")
    check.answer(VERDICT_NOT_TEXT_FIELD)

    lines = _check_lines(caplog)
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    message = lines[0].getMessage()
    assert (
        "verdict=not_text_field window_class=Chrome_WidgetWin_1 "
        "focus_class=Chrome_RenderWidgetHostHWND gui_caret=none "
        "msaa_caret=invisible width=0 rechecks=2"
    ) in message
    assert "hello" not in message and "world" not in message

    # A check that could not start logs the same line, with its reason.
    check.accept = False
    caplog.clear()
    _dictate(controller, "second take")
    lines = _check_lines(caplog)
    assert len(lines) == 1
    assert "verdict=unknown" in lines[0].getMessage()
    assert "note=not_started" in lines[0].getMessage()
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


def test_f10_never_joins_a_doubtful_row_to_a_failed_one(monkeypatch, tmp_path):
    """The review's repro: a FALSE "not a text field" verdict (the text did
    land) left a row; the next paste failed cleanly, and F10 joined both, so
    the stale text was pasted a second time."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, _beeps = _make(monkeypatch, tmp_path, check)
    _dictate(controller, "stale words")
    check.answer(VERDICT_NOT_TEXT_FIELD)
    inserter.should_fail = True
    _dictate(controller, "missing words")
    assert len(_rows(overlay)) == 2
    inserter.should_fail = False

    controller.repaste_last_transcript()

    assert inserter.calls[-1][0] == "missing words"
    check.answer(VERDICT_TEXT_FIELD)
    # A later paste went out, so nothing can paste the doubtful row any
    # more: it is dropped rather than left to pile up.
    assert _rows(overlay) == []
    controller.shutdown()
    _ = app


def test_f10_after_a_later_paste_never_pastes_an_older_doubtful_row(
    monkeypatch, tmp_path
):
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, _beeps = _make(monkeypatch, tmp_path, check)
    _dictate(controller, "stale words")
    check.answer(VERDICT_NOT_TEXT_FIELD)
    _dictate(controller, "new words")
    check.answer(VERDICT_TEXT_FIELD)
    assert _rows(overlay) == []

    controller.repaste_last_transcript()

    assert inserter.calls[-1][0] == "new words"
    controller.shutdown()
    _ = app


def test_a_window_that_always_reads_doubtful_keeps_only_the_latest_row(
    monkeypatch, tmp_path
):
    """An Electron prompt that systematically reads "not a text field" must
    not collect a row per dictation for F10 to paste all at once."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, _beeps = _make(monkeypatch, tmp_path, check)
    for text in ("first words", "second words"):
        _dictate(controller, text)
        check.answer(VERDICT_NOT_TEXT_FIELD)
    rows = _rows(overlay)
    assert len(rows) == 1 and "second words" in rows[0]

    controller.repaste_last_transcript()

    assert inserter.calls[-1][0] == "second words"
    controller.shutdown()
    _ = app


def test_a_verdict_that_arrives_after_a_later_paste_lists_no_row(monkeypatch, tmp_path):
    """The next dictation was pasted before the first check answered: its
    row could never be pasted by F10 any more, so only the tray reports it."""
    check = FakePasteTargetCheck()
    controller, app, overlay, _inserter, _beeps = _make(monkeypatch, tmp_path, check)
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    _dictate(controller, "first words")
    _dictate(controller, "second words")

    check.answer(VERDICT_NOT_TEXT_FIELD)

    assert len(messages) == 1 and "saved in history" in messages[0]
    assert "waiting to be inserted" not in messages[0]
    assert _rows(overlay) == []
    assert overlay.states[-1] == ("Done", "second words")
    controller.shutdown()
    _ = app


def test_a_refused_repaste_is_not_confirmed_and_keeps_the_earlier_verdict(
    monkeypatch, tmp_path
):
    """The first check still holds the worker, so the re-paste's own check
    is refused. That played the tone as if confirmed; and dropping the first
    check for the repeat as well left neither paste with a verdict. The
    first one now decides: a row is not listed (the re-paste superseded
    it), but the tray says the focus did not look like a text field."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, beeps = _make(monkeypatch, tmp_path, check)
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    _dictate(controller, "hello world")
    check.accept = False

    controller.repaste_last_transcript()
    assert [call[0] for call in inserter.calls] == ["hello world", "hello world"]
    assert beeps == []

    check.answer(VERDICT_NOT_TEXT_FIELD)

    assert beeps == []
    assert len(messages) == 1 and "does not look like a text field" in messages[0]
    assert _rows(overlay) == []
    assert overlay.states[-1] == ("Done", "hello world")
    controller.shutdown()
    _ = app


def test_the_same_text_into_another_window_keeps_the_earlier_check(
    monkeypatch, tmp_path
):
    """Two dictations of "okay." into two windows are two pastes; the second
    must not cancel the first one's verdict."""
    check = FakePasteTargetCheck()
    controller, app, _overlay, _inserter, _beeps = _make(monkeypatch, tmp_path, check)
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    _dictate(controller, "okay.")
    focus = controller._window_focus_helper
    focus.captured = focus.current = 111
    _dictate(controller, "okay.")
    assert check.expected_foregrounds == [987, 111]

    check.answer(VERDICT_NOT_TEXT_FIELD)

    assert len(messages) == 1 and "does not look like a text field" in messages[0]
    controller.shutdown()
    _ = app


def test_a_repaste_of_the_same_text_leaves_the_verdict_to_its_own_check(
    monkeypatch, tmp_path
):
    check = FakePasteTargetCheck()
    controller, app, overlay, _inserter, beeps = _make(monkeypatch, tmp_path, check)
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    _dictate(controller, "hello world")
    controller.repaste_last_transcript()

    check.answer(VERDICT_NOT_TEXT_FIELD)  # the first paste's, now stale
    assert _rows(overlay) == [] and beeps == [] and messages == []
    check.answer(VERDICT_TEXT_FIELD)  # the re-paste's own

    assert beeps == [1]
    assert overlay.states[-1] == ("Done", "hello world")
    controller.shutdown()
    _ = app


def _f10_inside_the_pace(monkeypatch, tmp_path, *, dismiss_before_pace_ends):
    """A doubtful row A, then F10 inside the paste pace while queued C waits."""
    check = FakePasteTargetCheck()
    inserter = PacedTextInserter()
    controller, app, overlay, _inserter, _beeps = _make(
        monkeypatch, tmp_path, check, inserter=inserter
    )
    messages: list[str] = []
    controller.busy_overlay_error.connect(messages.append)
    controller.start_recording()
    controller.stop_recording()
    token_c = controller._active_request_token
    _dictate(controller, "doubtful words")
    check.answer(VERDICT_NOT_TEXT_FIELD)
    # C finishes inside the doubtful paste's restore window: the pace holds it.
    controller._on_transcription_ready("queued words", request_token=token_c)
    controller.repaste_last_transcript()
    assert [call[0] for call in inserter.calls] == ["doubtful words"]
    if dismiss_before_pace_ends:
        controller._dismiss_undelivered()
    # C goes first; its own keystroke paces the re-paste once more.
    for _round in range(2):
        inserter.now += 5.0
        controller._on_paste_pace_timeout()
    return controller, app, overlay, inserter, messages


def test_f10_held_by_the_pace_still_pastes_the_row_it_named(monkeypatch, tmp_path):
    """C's paste dropped the doubtful row as superseded, and the paced F10
    then found its row gone and ended without pasting or saying anything."""
    controller, app, _overlay, inserter, _messages = _f10_inside_the_pace(
        monkeypatch, tmp_path, dismiss_before_pace_ends=False
    )

    assert [call[0] for call in inserter.calls] == [
        "doubtful words",
        "queued words",
        "doubtful words",
    ]
    controller.shutdown()
    _ = app


def test_a_paced_f10_whose_rows_are_gone_says_so(monkeypatch, tmp_path):
    controller, app, overlay, inserter, messages = _f10_inside_the_pace(
        monkeypatch, tmp_path, dismiss_before_pace_ends=True
    )

    assert [call[0] for call in inserter.calls] == ["doubtful words", "queued words"]
    shown = [detail for state, detail in overlay.states if state == "Error"]
    notices = [*messages, *shown]
    assert any("nothing was inserted" in notice for notice in notices), notices
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


def test_the_waiting_tone_is_skipped_once_a_new_recording_started(
    monkeypatch, tmp_path
):
    """The tone used to play right after the paste. Waiting for the check, it
    could land in the microphone of the recording the user started meanwhile."""
    check = FakePasteTargetCheck()
    controller, app, _overlay, _inserter, beeps = _make(monkeypatch, tmp_path, check)
    _dictate(controller, "hello world")
    controller.start_recording()

    check.answer(VERDICT_TEXT_FIELD)

    assert beeps == []
    controller.shutdown()
    _ = app


def test_a_queued_paste_during_the_same_recording_keeps_its_tone(monkeypatch, tmp_path):
    """Immediate mode pasted mid-recording and played the tone before the
    check existed; only a recording started after the paste skips it."""
    check = FakePasteTargetCheck()
    controller, app, _overlay, _inserter, beeps = _make(
        monkeypatch, tmp_path, check, immediate_insert=True
    )
    controller.start_recording()
    controller.stop_recording()
    token_a = controller._active_request_token
    controller.start_recording()
    controller._on_transcription_ready("transcript A", request_token=token_a)

    check.answer(VERDICT_TEXT_FIELD)

    assert beeps == [1]
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


def test_a_queued_report_never_paints_over_another_jobs_result(monkeypatch, tmp_path):
    """A queued paste paints nothing, so what the overlay showed at its
    paste was another job's result: here B's failed paste with its Insert.
    The doubtful report for A replaced it, and B's Insert was gone."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, beeps = _make(monkeypatch, tmp_path, check)
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    real_insert = inserter.insert_text_with_options

    def _b_fails(text, target_hwnd=None, paste_mode="auto", restore_clipboard=True):
        if text == "transcript B":
            inserter.calls.append((text, target_hwnd, paste_mode))
            raise TextInsertionError("failed insert")
        return real_insert(text, target_hwnd, paste_mode, restore_clipboard)

    inserter.insert_text_with_options = _b_fails
    controller.start_recording()
    controller.stop_recording()
    token_a = controller._active_request_token
    # B goes to another window, so A's paste is not joined to B's.
    focus = controller._window_focus_helper
    focus.captured, focus.captured_focus, focus.captured_caret = 111, 222, 333
    controller.start_recording()
    controller.stop_recording()
    # B finishes first and its paste fails: its Error with Insert is shown.
    controller._on_transcription_ready(
        "transcript B", request_token=controller._active_request_token
    )
    b_screen = overlay.states[-1]
    assert b_screen[0] == "Error" and "transcript B" in b_screen[1]
    # A, still transcribing in the background, is pasted when it finishes.
    controller._on_transcription_ready("transcript A", request_token=token_a)
    assert [call[0] for call in inserter.calls] == ["transcript B", "transcript A"]
    assert overlay.states[-1] == b_screen

    check.answer(VERDICT_NOT_TEXT_FIELD)

    assert overlay.states[-1] == b_screen
    assert controller._insert_action_text == "transcript B"
    assert len(messages) == 1 and "does not look like a text field" in messages[0]
    assert beeps == []
    controller.shutdown()
    _ = app


def test_a_repaste_report_never_paints_over_an_offer_it_kept(monkeypatch, tmp_path):
    """The tray re-paste pasted a text the shown offer is no part of (a
    streaming tail's offer, in the review): the offer stayed on screen, and
    the doubtful report must not take it away."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, _beeps = _make(monkeypatch, tmp_path, check)
    messages: list[str] = []
    controller.background_insertion_failed.connect(messages.append)
    inserter.should_fail = True
    _dictate(controller, "tail words")
    inserter.should_fail = False
    offer_screen = overlay.states[-1]
    assert offer_screen[0] == "Error"
    # `_last_transcript` moved on without the offer, as a failed queued
    # streaming job's rescued partial does; no row is waiting.
    controller._dismiss_undelivered()
    controller._set_last_transcript("other words", None)

    controller.repaste_last_transcript()
    assert inserter.calls[-1][0] == "other words"
    assert controller._insert_action_text == "tail words"
    kept = overlay.states[-1]

    check.answer(VERDICT_NOT_TEXT_FIELD)

    assert overlay.states[-1] == kept
    assert controller._insert_action_text == "tail words"
    assert len(messages) == 1 and "does not look like a text field" in messages[0]
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
