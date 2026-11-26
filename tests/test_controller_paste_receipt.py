"""What the user is told once a paste's read receipt is in.

A paste used to be "Done" the moment its keystroke went out. The owner's
report of 2026-11-24: the overlay says inserted, nothing arrived; and with
several results queued, the one that missed was no longer shown once the
next one had landed. The receipt (`clipboard_owner`) says whether any
program fetched the transcript; these tests settle it by hand and pin what
the user gets.
"""

from __future__ import annotations

import itertools
import logging

import pytest
from conftest import FakeTextInserter
from test_controller_paste_target_check import (
    FakePasteTargetCheck,
    _dictate,
    _make,
    _rows,
)

from stt_app.clipboard_owner import RECEIPT_READ, RECEIPT_UNKNOWN, RECEIPT_UNREAD
from stt_app.config import (
    GRANITE_CTC_MODEL_SIZE,
    NEMOTRON_MODEL_SIZE,
    OVERLAY_ERROR_ACTION_INSERT,
    OVERLAY_WARNING_NOT_INSERTED,
    PARAKEET_MODEL_SIZE,
)
from stt_app.paste_target_check import VERDICT_NOT_TEXT_FIELD, VERDICT_TEXT_FIELD
from stt_app.settings_store import AppSettings
from stt_app.text_inserter import TextInsertionError

EVIDENCE = "reason=test reader=- read_ms=- refused=0"


class ReceiptInserter(FakeTextInserter):
    """A fake inserter whose pastes carry a read receipt the test settles."""

    def __init__(self, fail_texts=()):
        super().__init__()
        self._ids = itertools.count(101)
        self._fail_texts = set(fail_texts)
        self.handler = None
        # The last paste's id while its receipt is to come.
        self.pending = None

    def set_paste_receipt_handler(self, handler):
        self.handler = handler

    def insert_text_with_options(
        self, text, target_hwnd=None, paste_mode="auto", restore_clipboard=True
    ):
        if text in self._fail_texts:
            self.calls.append((text, target_hwnd, paste_mode))
            raise TextInsertionError("failed insert")
        result = super().insert_text_with_options(
            text,
            target_hwnd=target_hwnd,
            paste_mode=paste_mode,
            restore_clipboard=restore_clipboard,
        )
        self.pending = next(self._ids)
        return result

    def pending_receipt_id(self):
        return self.pending

    def settle(self, app, transaction_id, verdict):
        """The inserter reports the paste's final receipt."""
        if self.pending == transaction_id:
            self.pending = None
        self.handler(transaction_id, verdict, EVIDENCE)
        # The controller takes it through a queued connection.
        app.processEvents()


def _make_with_receipts(monkeypatch, tmp_path, check=None, **kwargs):
    inserter = ReceiptInserter(fail_texts=kwargs.pop("fail_texts", ()))
    controller, app, overlay, _inserter, beeps = _make(
        monkeypatch, tmp_path, check, inserter=inserter, **kwargs
    )
    tray: list[str] = []
    controller.background_insertion_failed.connect(tray.append)
    return controller, app, overlay, inserter, beeps, tray


def test_a_paste_no_window_fetched_is_reported_as_not_inserted(monkeypatch, tmp_path):
    controller, app, overlay, inserter, beeps, tray = _make_with_receipts(
        monkeypatch, tmp_path
    )
    _dictate(controller, "hello world")
    assert overlay.states[-1] == ("Done", "hello world")
    assert _rows(overlay) == []

    inserter.settle(app, 101, RECEIPT_UNREAD)

    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "no window took the text from the clipboard" in detail
    assert "press Insert" in detail and "hello world" in detail
    kwargs = overlay.state_kwargs[-1]
    assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_INSERT
    assert kwargs["warning"] == OVERLAY_WARNING_NOT_INSERTED
    rows = _rows(overlay)
    assert len(rows) == 1 and rows[0].startswith("Not inserted")
    # The overlay carries a foreground paste's report; the tray stays quiet.
    assert tray == []
    assert beeps == [1], "no check ran, so the tone played with the paste"

    controller.insert_failed_text()

    assert [call[0] for call in inserter.calls] == ["hello world", "hello world"]
    assert _rows(overlay) == [], "the row of the inserted text stayed listed"
    controller.shutdown()


def test_a_fetched_paste_and_one_without_evidence_stay_done(monkeypatch, tmp_path):
    controller, app, overlay, inserter, _beeps, tray = _make_with_receipts(
        monkeypatch, tmp_path
    )
    for transaction_id, text, verdict in (
        (101, "first words", RECEIPT_READ),
        (102, "second words", RECEIPT_UNKNOWN),
    ):
        _dictate(controller, text)
        inserter.settle(app, transaction_id, verdict)
        assert overlay.states[-1] == ("Done", text)

    assert _rows(overlay) == [] and tray == []
    assert controller._paste_checks == {}
    controller.shutdown()


def test_a_fetched_transcript_never_overrules_the_caret(monkeypatch, tmp_path):
    """Edge and Chrome fetch the clipboard text on every Ctrl+V, with the
    focus on a button as on a text field (measured 2026-11-25): "read" says
    the keystroke reached the browser, nothing about the element."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, beeps, _tray = _make_with_receipts(
        monkeypatch, tmp_path, check
    )
    _dictate(controller, "hello world")
    check.answer(VERDICT_NOT_TEXT_FIELD)
    rows = _rows(overlay)
    shown = overlay.states[-1]

    inserter.settle(app, 101, RECEIPT_READ)

    assert len(rows) == 1 and _rows(overlay) == rows
    assert rows[0].startswith("Not in a text field")
    assert overlay.states[-1] == shown and shown[0] == "Error"
    assert beeps == []
    assert controller._paste_checks == {}
    controller.shutdown()


def test_a_doubtful_paste_no_window_fetched_becomes_a_failed_paste(
    monkeypatch, tmp_path
):
    """One row and one report: the caret's guess, then the evidence for it."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, beeps, tray = _make_with_receipts(
        monkeypatch, tmp_path, check
    )
    _dictate(controller, "hello world")
    check.answer(VERDICT_NOT_TEXT_FIELD)
    rows = _rows(overlay)
    assert len(rows) == 1 and rows[0].startswith("Not in a text field")
    shown = overlay.states[-1]

    inserter.settle(app, 101, RECEIPT_UNREAD)

    rows = _rows(overlay)
    assert len(rows) == 1 and rows[0].startswith("Not inserted")
    assert overlay.states[-1] == shown, "the report was painted a second time"
    assert tray == [] and beeps == []
    controller.shutdown()


def test_a_text_field_verdict_does_not_outweigh_an_unread_receipt(
    monkeypatch, tmp_path
):
    """The caret of 2026-10-26 and 2026-11-23 appeared; the text did not."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, beeps, _tray = _make_with_receipts(
        monkeypatch, tmp_path, check
    )
    _dictate(controller, "hello world")
    check.answer(VERDICT_TEXT_FIELD)
    assert overlay.states[-1] == ("Done", "hello world") and beeps == [1]

    inserter.settle(app, 101, RECEIPT_UNREAD)

    assert overlay.states[-1][0] == "Error"
    assert len(_rows(overlay)) == 1
    controller.shutdown()


def _queue_two(controller, first, second):
    """Two results pasted in the background, each during the next recording."""
    controller.start_recording()
    controller.stop_recording()
    token_a = controller._active_request_token
    controller.start_recording()
    controller._on_transcription_ready(first, request_token=token_a)
    controller.stop_recording()
    token_b = controller._active_request_token
    controller.start_recording()
    controller._on_transcription_ready(second, request_token=token_b)


def test_a_queued_paste_that_missed_stays_listed_after_the_next_one_landed(
    monkeypatch, tmp_path
):
    """The owner's second symptom. In 0.13.0 the next paste dropped the
    doubtful row of the one that missed, and nothing showed it any more. B
    was being recorded when A's paste went out, so its paste supersedes
    nothing, and the receipt turns the doubtful row into the failed one."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, _beeps, tray = _make_with_receipts(
        monkeypatch, tmp_path, check, immediate_insert=True
    )
    _queue_two(controller, "transcript A", "transcript B")
    check.answer(VERDICT_NOT_TEXT_FIELD)
    assert [call[0] for call in inserter.calls] == ["transcript A", "transcript B"]
    rows = _rows(overlay)
    assert len(rows) == 1 and rows[0].startswith("Not in a text field")
    assert len(tray) == 1 and "does not look like a text field" in tray[0]
    tray.clear()

    # The second paste replaced A's transcript on the clipboard, unfetched.
    inserter.settle(app, 101, RECEIPT_UNREAD)

    rows = _rows(overlay)
    assert len(rows) == 1 and rows[0].startswith("Not inserted")
    assert '"transcript A"' in rows[0]
    # The user has this paste's report already.
    assert tray == []
    # The live recording keeps the overlay.
    assert overlay.states[-1][0] == "Listening"

    check.answer(VERDICT_TEXT_FIELD)
    inserter.settle(app, 102, RECEIPT_READ)
    controller.stop_recording()
    controller._on_transcription_ready(
        "transcript C", request_token=controller._active_request_token
    )

    assert len(_rows(overlay)) == 1, "a later paste dropped the failed row"
    controller.shutdown()


def test_a_queued_paste_no_window_fetched_is_reported_although_its_caret_looked_right(
    monkeypatch, tmp_path
):
    """The receipt alone: the target check saw a text field, the next paste
    went out, and A's transcript left the clipboard unfetched."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, _beeps, tray = _make_with_receipts(
        monkeypatch, tmp_path, check, immediate_insert=True
    )
    _queue_two(controller, "transcript A", "transcript B")
    check.answer(VERDICT_TEXT_FIELD)
    assert _rows(overlay) == []
    tray.clear()

    inserter.settle(app, 101, RECEIPT_UNREAD)

    rows = _rows(overlay)
    assert len(rows) == 1 and rows[0].startswith("Not inserted")
    assert '"transcript A"' in rows[0]
    assert len(tray) == 1
    assert "no window took the text from the clipboard" in tray[0]
    assert "1 transcript is waiting to be inserted" in tray[0]
    controller.shutdown()


def test_a_queued_paste_that_failed_stays_listed_after_the_next_one_landed(
    monkeypatch, tmp_path
):
    """The half of the second symptom 0.13.0 already had right: a paste that
    raised is a row until it is inserted or dismissed."""
    controller, app, overlay, inserter, _beeps, tray = _make_with_receipts(
        monkeypatch, tmp_path, immediate_insert=True, fail_texts={"transcript A"}
    )
    _queue_two(controller, "transcript A", "transcript B")

    rows = _rows(overlay)
    assert len(rows) == 1 and rows[0].startswith("Not inserted")
    assert '"transcript A"' in rows[0]
    assert len(tray) == 1 and "could not be inserted" in tray[0]
    assert inserter.calls[-1][0] == "transcript B"

    inserter.settle(app, 101, RECEIPT_READ)
    assert len(_rows(overlay)) == 1
    controller.shutdown()


def test_a_re_paste_of_the_same_text_makes_the_earlier_receipt_moot(
    monkeypatch, tmp_path, caplog
):
    """Nothing arrived, the user pressed the re-paste hotkey before the
    receipt was in: the first paste's "unread" must not list as missing a
    text they have just pasted again."""
    caplog.set_level(logging.INFO, logger="test.controller.paste_target_check")
    controller, app, overlay, inserter, _beeps, tray = _make_with_receipts(
        monkeypatch, tmp_path
    )
    _dictate(controller, "hello world")

    controller.repaste_last_transcript()
    inserter.settle(app, 101, RECEIPT_UNREAD)

    assert [call[0] for call in inserter.calls] == ["hello world", "hello world"]
    assert _rows(overlay) == [] and tray == []
    assert "dropped=repeated" in caplog.text

    inserter.settle(app, 102, RECEIPT_UNREAD)

    assert len(_rows(overlay)) == 1, "the re-paste's own receipt was dropped too"
    controller.shutdown()


def test_a_receipt_nobody_registered_changes_nothing(monkeypatch, tmp_path):
    """Streaming inserts report theirs as well."""
    controller, app, overlay, inserter, _beeps, tray = _make_with_receipts(
        monkeypatch, tmp_path
    )
    _dictate(controller, "hello world")

    inserter.settle(app, 999, RECEIPT_UNREAD)

    assert overlay.states[-1] == ("Done", "hello world")
    assert _rows(overlay) == [] and tray == []
    controller.shutdown()


def test_an_unread_receipt_never_paints_over_a_newer_screen(monkeypatch, tmp_path):
    controller, app, overlay, inserter, _beeps, tray = _make_with_receipts(
        monkeypatch, tmp_path
    )
    _dictate(controller, "hello world")
    controller.start_recording()

    inserter.settle(app, 101, RECEIPT_UNREAD)

    assert overlay.states[-1][0] == "Listening"
    assert len(_rows(overlay)) == 1
    assert len(tray) == 1 and "saved in history" in tray[0]
    controller.shutdown()


def test_a_receipt_after_shutdown_is_ignored(monkeypatch, tmp_path):
    controller, app, overlay, inserter, _beeps, tray = _make_with_receipts(
        monkeypatch, tmp_path
    )
    _dictate(controller, "hello world")
    controller.shutdown()
    states = list(overlay.states)

    inserter.settle(app, 101, RECEIPT_UNREAD)

    assert overlay.states == states and tray == []


def test_a_paste_waiting_only_for_its_receipt_does_not_hold_a_quit(
    monkeypatch, tmp_path
):
    """The pace covers the wait; a quit must not hang on a receipt that a
    starved timer never reports."""
    check = FakePasteTargetCheck()
    controller, _app, _overlay, _inserter, _beeps, _tray = _make_with_receipts(
        monkeypatch, tmp_path, check
    )
    _dictate(controller, "hello world")
    assert controller.quit_pending_work().paste_settling is True

    check.answer(VERDICT_TEXT_FIELD)

    assert len(controller._paste_checks) == 1
    assert controller.quit_pending_work().paste_settling is False
    controller.shutdown()


def test_the_same_words_dictated_twice_keep_the_first_ones_receipt(
    monkeypatch, tmp_path
):
    """Two dictations of "okay." are two pastes. The second is no re-paste of
    the first, whose miss must still be reported."""
    check = FakePasteTargetCheck()
    controller, app, overlay, inserter, _beeps, _tray = _make_with_receipts(
        monkeypatch, tmp_path, check
    )
    _dictate(controller, "okay.")
    check.answer(VERDICT_TEXT_FIELD)
    _dictate(controller, "okay.")

    inserter.settle(app, 101, RECEIPT_UNREAD)

    rows = _rows(overlay)
    assert len(rows) == 1 and rows[0].startswith("Not inserted")
    controller.shutdown()


class _GatedInserter(ReceiptInserter):
    def __init__(self):
        super().__init__()
        self.offer_gate = None

    def set_offer_gate(self, gate):
        self.offer_gate = gate


@pytest.mark.parametrize(
    ("engine", "model_size", "announced_while_leased"),
    [
        ("local", PARAKEET_MODEL_SIZE, False),
        ("local", GRANITE_CTC_MODEL_SIZE, False),
        ("local", NEMOTRON_MODEL_SIZE, False),
        # faster-whisper kept the interpreter for 62 ms at most, the Node
        # runner is another process and a remote engine waits on a socket.
        ("local", "tiny", True),
        ("local", "cohere-transcribe-03-2026", True),
        ("groq", "tiny", True),
    ],
)
def test_no_paste_is_announced_while_an_in_process_onnx_runtime_is_in_use(
    monkeypatch, tmp_path, engine, model_size, announced_while_leased
):
    """Creating an ONNX session in this process kept the interpreter lock for
    1.0-1.9 s (measured 2026-11-25): a program fetching an announced
    transcript would wait that long for the owner window's Python callback.
    The controller's leases say when a preload, a job or a stream may do it."""
    inserter = _GatedInserter()
    controller, app, _overlay, _inserter, _beeps = _make(
        monkeypatch, tmp_path, FakePasteTargetCheck(), inserter=inserter
    )
    assert inserter.offer_gate() is True
    settings = AppSettings(engine=engine, model_size=model_size)

    lease = controller._acquire_transcriber_runtime(settings)

    assert inserter.offer_gate() is announced_while_leased
    lease.release()
    lease.release()
    assert inserter.offer_gate() is True
    controller.shutdown()
    _ = app


def test_a_loaded_onnx_model_no_longer_blocks_the_announcement(monkeypatch, tmp_path):
    """Parakeet and Granite CTC run their graphs without the lock (10 ms and
    4 ms measured): once the model is loaded, a job that decodes while an
    earlier result is pasted costs that paste nothing. Nemotron's decoding
    kept the lock for up to 0.8 s."""
    inserter = _GatedInserter()
    controller, app, _overlay, _inserter, _beeps = _make(
        monkeypatch, tmp_path, FakePasteTargetCheck(), inserter=inserter
    )
    parakeet = AppSettings(engine="local", model_size=PARAKEET_MODEL_SIZE)
    lease = controller._acquire_transcriber_runtime(parakeet)
    assert inserter.offer_gate() is False

    lease.transcriber.is_model_loaded = lambda: True

    assert inserter.offer_gate() is True
    lease.release()
    nemotron = AppSettings(engine="local", model_size=NEMOTRON_MODEL_SIZE)
    lease = controller._acquire_transcriber_runtime(nemotron)
    lease.transcriber.is_model_loaded = lambda: True
    assert inserter.offer_gate() is False
    lease.release()
    controller.shutdown()
    _ = app


def test_two_leases_of_an_onnx_runtime_both_have_to_end(monkeypatch, tmp_path):
    """A stream and an overlapping batch job: the second gets a runtime of
    its own, and the paste stays plain until both are released."""
    inserter = _GatedInserter()
    controller, app, _overlay, _inserter, _beeps = _make(
        monkeypatch, tmp_path, FakePasteTargetCheck(), inserter=inserter
    )
    settings = AppSettings(engine="local", model_size=NEMOTRON_MODEL_SIZE)

    shared = controller._acquire_transcriber_runtime(settings)
    isolated = controller._acquire_transcriber_runtime(settings)
    shared.release()

    assert inserter.offer_gate() is False
    isolated.release()
    assert inserter.offer_gate() is True
    controller.shutdown()
    _ = app
