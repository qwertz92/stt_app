"""Two owner decisions of 2026-10-09.

An edit reaches a result that was not inserted: the pending Insert offer,
the waiting-insert row and a result still waiting in the paste queue paste
the edited text, and Copy yields it -- but nothing whose paste keystroke may
already have gone out is ever pasted again.

Results for one target window are inserted in recording order: a streaming
dictation never pastes ahead of an earlier result for its own window, while
an earlier result for another window holds nothing back.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime

import pytest
from conftest import (
    FakeOverlay,
    FakeSettingsStore,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)
from PySide6 import QtGui
from test_controller import FakeClipboard
from test_controller_queue import (
    PacedTextInserter,
    _make_queue_controller,
    _record_and_stop,
)

from stt_app.config import (
    CLIPBOARD_RESTORE_DELAY_S,
    FALLBACK_HOTKEY,
    OVERLAY_ERROR_ACTION_INSERT,
    OVERLAY_ERROR_ACTION_NONE,
)
from stt_app.settings_store import AppSettings
from stt_app.streaming_text import tail_prefix
from stt_app.text_inserter import TextMayHaveBeenPastedError
from stt_app.transcript_history import TranscriptHistoryStore, edited_entry


def _patch_edit_dialog(monkeypatch, get_text):
    monkeypatch.setattr(
        "stt_app.transcript_edit_dialog.TranscriptEditDialog.get_text",
        staticmethod(get_text),
    )


def _controller(tmp_path, *, inserter, **settings_overrides):
    history = TranscriptHistoryStore(tmp_path / "history.json")
    overlay = FakeOverlay()
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
        model_size="small",
        silence_gate_enabled=False,
        **settings_overrides,
    )
    controller, app = make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        text_inserter=inserter,
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=history,
        logger=logging.getLogger("test.controller.edit_and_order"),
    )
    return controller, app, overlay, history


class _PostPasteFailingInserter(FakeTextInserter):
    """The keystroke goes out, then the clipboard cleanup fails."""

    def insert_text_with_options(
        self, text, target_hwnd=None, paste_mode="auto", restore_clipboard=True
    ):
        self.calls.append((text, target_hwnd, paste_mode))
        raise TextMayHaveBeenPastedError(
            "The text was pasted but the clipboard could not be restored."
        )


class _SwitchableInserter(FakeTextInserter):
    """Fails before the keystroke (``should_fail``) or after it (``post_paste``)."""

    post_paste = False

    def insert_text_with_options(
        self, text, target_hwnd=None, paste_mode="auto", restore_clipboard=True
    ):
        if self.post_paste:
            self.calls.append((text, target_hwnd, paste_mode))
            raise TextMayHaveBeenPastedError(
                "The text was pasted but the clipboard could not be restored."
            )
        return super().insert_text_with_options(
            text, target_hwnd, paste_mode, restore_clipboard
        )


# -- Edit applies to a result that was not inserted ---------------------------


def _assert_offer(overlay, text, *, editable=True):
    assert overlay.states[-1][0] == "Error"
    kwargs = overlay.state_kwargs[-1]
    assert kwargs["copy_text"] == text
    assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_INSERT
    # A coalesced paste has no single history entry, so the overlay's Edit
    # has nothing to write to; the history editor still reaches its parts.
    assert bool(kwargs.get("editable")) is editable


def test_edit_on_a_failed_paste_is_what_insert_and_f10_paste(monkeypatch, tmp_path):
    """A failed paste paints the Insert offer; Edit is enabled there, and
    after an edit the overlay's Insert, the re-paste hotkey and Copy all act
    on the edited text. Before, Edit stayed disabled on the offer, and an
    edit made anyway left the offer, its row and Copy on the old text."""
    for repaste in ("insert_failed_text", "repaste_last_transcript"):
        inserter = FakeTextInserter(should_fail=True)
        controller, _app, overlay, history = _controller(
            tmp_path / repaste, inserter=inserter
        )
        _patch_edit_dialog(monkeypatch, lambda parent, text: "Transcript A, edited.")
        try:
            controller._on_transcription_ready("transcript A.")
            _assert_offer(overlay, "transcript A.")

            assert controller.edit_last_transcript() is True

            assert [entry.text for entry in history.load()] == ["Transcript A, edited."]
            _assert_offer(overlay, "Transcript A, edited.")
            assert controller._last_transcript == "Transcript A, edited."
            assert [row.text for row in controller._undelivered_inserts] == [
                "Transcript A, edited."
            ]

            inserter.should_fail = False
            getattr(controller, repaste)()

            assert inserter.calls[-1][0] == "Transcript A, edited.", repaste
            assert controller._undelivered_inserts == [], repaste
            assert controller._insert_action_text == "", repaste
        finally:
            controller.shutdown()


def test_an_edited_paste_that_may_have_landed_is_never_pasted_again(
    monkeypatch, tmp_path
):
    """Edit is offered on a "possibly inserted" result too (the history
    entry deserves the correction), but the edit must not turn it into a
    paste: the edit used to forget which row the shown transcript was, and
    the re-paste then pasted the edited text over the one already there."""
    inserter = _PostPasteFailingInserter()
    controller, _app, overlay, _history = _controller(tmp_path, inserter=inserter)
    _patch_edit_dialog(monkeypatch, lambda parent, text: "edited after the paste")
    try:
        controller._on_transcription_ready("possibly pasted.")
        assert overlay.state_kwargs[-1]["error_action"] == OVERLAY_ERROR_ACTION_NONE
        assert overlay.state_kwargs[-1].get("editable") is True
        pastes = len(inserter.calls)

        assert controller.edit_last_transcript() is True
        controller.repaste_last_transcript()

        assert len(inserter.calls) == pastes
        assert "may already have been inserted" in overlay.states[-1][1]
    finally:
        controller.shutdown()


def test_a_history_edit_of_a_coalesced_waiting_row_is_what_f10_pastes(tmp_path):
    """Two queued results for one window failed as one paste. Editing one of
    them in the history editor changes the waiting row, the overlay's offer
    and Copy; before, the row knew only the joined text and kept it."""
    inserter = FakeTextInserter()
    controller, _app, overlay, history = _controller(tmp_path, inserter=inserter)
    try:
        controller._target_window_handle = 987
        job_b = controller._register_transcription_job(77, controller.settings, "batch")
        job_c = controller._register_transcription_job(78, controller.settings, "batch")
        # A transcription in flight holds both results back, so the next
        # flush pastes them together.
        controller._active_request_token = 99
        controller._handle_background_transcription_ready(job_b, "queued B")
        controller._handle_background_transcription_ready(job_c, "queued C")
        controller._active_request_token = None
        inserter.should_fail = True
        controller._flush_deferred_background_results()
        _assert_offer(overlay, "queued B queued C", editable=False)

        entry_b = job_b.history_entry
        assert history.update_entry_text(entry_b, "edited B ") == 1
        controller.on_history_entry_edited(entry_b, edited_entry(entry_b, "edited B "))

        _assert_offer(overlay, "edited B queued C", editable=False)
        inserter.should_fail = False
        controller.repaste_last_transcript()
        assert inserter.calls[-1][0] == "edited B queued C"
        assert controller._undelivered_inserts == []
    finally:
        controller.shutdown()


def _coalesced_failure(controller, inserter):
    """Two queued results for one window fail as one paste; returns B's job."""
    controller._target_window_handle = 987
    job_b = controller._register_transcription_job(77, controller.settings, "batch")
    job_c = controller._register_transcription_job(78, controller.settings, "batch")
    controller._active_request_token = 99
    controller._handle_background_transcription_ready(job_b, "queued B")
    controller._handle_background_transcription_ready(job_c, "queued C")
    controller._active_request_token = None
    controller._flush_deferred_background_results()
    return job_b


def test_copy_follows_a_history_edit_inside_a_coalesced_row(monkeypatch, tmp_path):
    """The failed coalesced paste is the shown transcript, with no single
    entry. An edit of one of its results changes what Copy yields (owner's
    decision 2026-10-09); before, the row and the offer followed and the
    tray's Copy still put the old joined text on the clipboard."""
    clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: clipboard)
    inserter = FakeTextInserter(should_fail=True)
    controller, _app, _overlay, history = _controller(tmp_path, inserter=inserter)
    try:
        job_b = _coalesced_failure(controller, inserter)
        entry_b = job_b.history_entry
        assert history.update_entry_text(entry_b, "edited B") == 1
        controller.on_history_entry_edited(entry_b, edited_entry(entry_b, "edited B"))

        assert controller.copy_last_transcript_to_clipboard() is True
        assert clipboard.text() == "edited B queued C"
    finally:
        controller.shutdown()


def test_copy_follows_an_edit_of_a_possibly_inserted_row_that_stays_unpasted(
    monkeypatch, tmp_path
):
    """The coalesced paste may have landed. Copy yields the edit, but the
    shown transcript keeps its row: writing it through the
    `_last_transcript` setter forgot that row, and the re-paste fallback
    then pasted a text that may already be in the window."""
    clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: clipboard)
    inserter = _SwitchableInserter()
    inserter.post_paste = True
    controller, _app, overlay, history = _controller(tmp_path, inserter=inserter)
    try:
        job_b = _coalesced_failure(controller, inserter)
        inserter.post_paste = False
        assert overlay.state_kwargs[-1]["error_action"] == OVERLAY_ERROR_ACTION_NONE
        pastes = len(inserter.calls)
        entry_b = job_b.history_entry
        assert history.update_entry_text(entry_b, "edited B") == 1
        controller.on_history_entry_edited(entry_b, edited_entry(entry_b, "edited B"))

        assert controller.copy_last_transcript_to_clipboard() is True
        assert clipboard.text() == "edited B queued C"
        controller.repaste_last_transcript()
        assert len(inserter.calls) == pastes
        assert "may already have been inserted" in overlay.states[-1][1]
    finally:
        controller.shutdown()


def test_a_history_edit_reaches_a_result_still_waiting_to_be_pasted(
    monkeypatch, tmp_path
):
    """A queued result waits for the recording in progress; edited in the
    history editor meanwhile, it is pasted as edited."""
    controller, app, _overlay, inserter, _focus, history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    token_a = _record_and_stop(controller)
    controller.start_recording()
    controller._on_transcription_ready("queued A", request_token=token_a)
    assert inserter.calls == []
    [entry_a] = history.load()

    assert history.update_entry_text(entry_a, "queued A, edited") == 1
    controller.on_history_entry_edited(
        entry_a, edited_entry(entry_a, "queued A, edited")
    )
    controller.stop_recording()
    token_b = controller._active_request_token
    controller._on_transcription_ready("dictation B", request_token=token_b)

    assert [call[0] for call in inserter.calls] == ["queued A, edited dictation B"]
    controller.shutdown()
    _ = app


def _streaming_tail_offer(tmp_path, inserter):
    controller, app, overlay, history = _controller(
        tmp_path, inserter=inserter, mode="streaming"
    )
    controller._active_session_mode = "streaming"
    controller._streaming_recording = True
    controller._stream_text_state.committed_text = "erster teil"
    controller._stream_text_state.live_text = "erster teil"
    controller._target_window_handle = 123
    controller._target_focus_signature = None
    inserter.should_fail = True
    controller._on_transcription_ready("erster teil zweiter teil")
    inserter.should_fail = False
    assert controller._insert_action_text == " zweiter teil"
    _assert_offer(overlay, " zweiter teil")
    return controller, app, overlay, history


def test_an_edit_of_a_streaming_tail_offer_moves_the_tail(monkeypatch, tmp_path):
    """The words before the tail are in the document; an edit of the part
    that never arrived is what Insert pastes, still behind those words."""
    inserter = FakeTextInserter()
    controller, _app, overlay, history = _streaming_tail_offer(tmp_path, inserter)
    _patch_edit_dialog(monkeypatch, lambda parent, text: "erster teil dritter teil")
    try:
        assert controller.edit_last_transcript() is True

        assert [entry.text for entry in history.load()] == ["erster teil dritter teil"]
        _assert_offer(overlay, " dritter teil")
        controller.insert_failed_text()
        assert inserter.calls[-1][0] == " dritter teil"
        assert controller._insert_action_text == ""
    finally:
        controller.shutdown()


@pytest.mark.parametrize(
    ("committed", "final_text", "row_text"),
    [
        # A punctuation tail: every transcript ending in "." ends with it.
        ("hallo welt", "hallo welt .", "Ich komme morgen."),
        # A word tail that another dictation happens to end with.
        ("bis", "bis morgen.", "Ich komme morgen."),
    ],
)
def test_f10_on_another_dictations_row_never_carries_a_streaming_tail_offer(
    tmp_path, committed, final_text, row_text
):
    """An earlier batch result failed and waits as a row; then a streaming
    dictation's tail failed and is offered without a row. F10 pastes the
    row. That paste is a different dictation, so it neither retires the
    tail's offer nor, failing after its keystroke, marks it as possibly
    pasted -- before, `tail_prefix(row, tail)` counted the row as carrying
    the tail, and the tail was no longer offered anywhere."""
    inserter = _SwitchableInserter()
    controller, _app, overlay, _history = _controller(
        tmp_path, inserter=inserter, mode="streaming"
    )
    try:
        created = datetime.now().astimezone()
        controller._record_undelivered_insert(
            row_text, may_have_pasted=False, created_at=created, history_entry=None
        )
        controller._active_session_mode = "streaming"
        controller._streaming_recording = True
        controller._stream_text_state.committed_text = committed
        controller._stream_text_state.live_text = committed
        controller._target_window_handle = 123
        controller._target_focus_signature = None
        inserter.should_fail = True
        controller._on_transcription_ready(final_text)
        inserter.should_fail = False
        tail = controller._insert_action_text
        assert tail and tail_prefix(row_text, tail) is not None

        inserter.post_paste = True
        controller.repaste_last_transcript()
        inserter.post_paste = False
        assert inserter.calls[-1][0] == row_text
        assert controller._insert_action_text == tail
        assert controller._insert_offer_may_have_pasted is False

        # The row is "possibly inserted" now; another dictation's row with
        # the same words is pasted cleanly.
        controller._record_undelivered_insert(
            row_text, may_have_pasted=False, created_at=created, history_entry=None
        )
        controller.repaste_last_transcript()
        assert inserter.calls[-1][0] == row_text
        assert controller._insert_action_text == tail
        assert overlay.state_kwargs[-1]["error_action"] == OVERLAY_ERROR_ACTION_INSERT

        controller.insert_failed_text()
        assert inserter.calls[-1][0] == tail
        assert controller._insert_action_text == ""
    finally:
        controller.shutdown()


def test_f10_that_pastes_the_offers_row_inside_a_join_retires_the_offer(tmp_path):
    """The offer is row B's; a later failure listed row C without painting
    over it (its report went to the tray). F10 pastes "B C", B's row inside
    the join. That paste carried the offer, so the offer goes -- matched by
    text, "B C" does not end with B, the offer stayed, and its Insert pasted
    B a second time."""
    inserter = FakeTextInserter(should_fail=True)
    controller, _app, overlay, _history = _controller(tmp_path, inserter=inserter)
    try:
        controller._on_transcription_ready("dictation B.")
        _assert_offer(overlay, "dictation B.")
        controller._record_undelivered_insert(
            "dictation C.",
            may_have_pasted=False,
            created_at=datetime.now().astimezone(),
            history_entry=None,
        )
        inserter.should_fail = False

        controller.repaste_last_transcript()

        assert inserter.calls[-1][0] == "dictation B. dictation C."
        assert controller._undelivered_inserts == []
        assert controller._insert_action_text == ""
        assert overlay.states[-1] == ("Done", "dictation B. dictation C.")
    finally:
        controller.shutdown()


def test_an_edit_of_words_already_in_the_window_keeps_the_tail(monkeypatch, tmp_path):
    """Words that reached the document cannot be taken back, so an edit that
    changes them cannot say what the missing part is now: the offer keeps
    its text and the overlay says why."""
    inserter = FakeTextInserter()
    controller, _app, overlay, history = _streaming_tail_offer(tmp_path, inserter)
    _patch_edit_dialog(monkeypatch, lambda parent, text: "Erster Teil zweiter teil")
    try:
        assert controller.edit_last_transcript() is True

        assert [entry.text for entry in history.load()] == ["Erster Teil zweiter teil"]
        _assert_offer(overlay, " zweiter teil")
        assert "already in the window" in overlay.states[-1][1]
        assert controller._last_transcript == "Erster Teil zweiter teil"
    finally:
        controller.shutdown()


# -- Order per target window --------------------------------------------------


_PARTIALS = (
    "eins zwei drei vier",
    "eins zwei drei vier fuenf sechs",
    "eins zwei drei vier fuenf sechs sieben acht",
    "eins zwei drei vier fuenf sechs sieben acht neun zehn",
)


def _stream(controller, partials=_PARTIALS):
    for partial in partials:
        controller._on_transcription_partial(partial)


def test_a_streaming_dictation_never_pastes_ahead_of_an_earlier_result(
    monkeypatch, tmp_path
):
    """A batch dictation for the same window is still transcribing when a
    streaming one starts. The streaming words wait; the earlier result is
    pasted as soon as it is ready -- nothing of the stream is in the window
    yet -- and the stream then catches up. Before, the live words went in
    first and the earlier result landed after or inside them."""
    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    token_a = _record_and_stop(controller)
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    assert controller._streaming_recording is True

    _stream(controller, _PARTIALS[:2])
    assert inserter.calls == [], "the stream pasted ahead of the earlier result"

    controller._on_transcription_ready("dictation A.", request_token=token_a)
    assert [call[0] for call in inserter.calls] == ["dictation A."]

    _stream(controller, _PARTIALS[2:])
    pasted = [call[0] for call in inserter.calls]
    assert pasted[0] == "dictation A."
    assert len(pasted) > 1 and "eins" in "".join(pasted[1:])
    controller.shutdown()
    _ = app


def test_an_earlier_result_for_another_window_holds_no_stream_back(
    monkeypatch, tmp_path
):
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    focus.captured = 111
    token_a = _record_and_stop(controller)
    focus.captured = 987
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()

    _stream(controller)
    assert inserter.calls, "the stream waited for another window's result"
    controller._on_transcription_ready("dictation A.", request_token=token_a)
    assert "dictation A." not in [call[0] for call in inserter.calls]
    assert controller._deferred_background_results
    controller.shutdown()
    _ = app


def test_the_first_live_insert_waits_for_the_earlier_pastes_restore_window(
    monkeypatch, tmp_path
):
    """The earlier result's paste may still be read late from the clipboard;
    a live insert right behind it would overwrite it."""
    inserter = PacedTextInserter()
    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    token_a = _record_and_stop(controller)
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    controller._on_transcription_ready("dictation A.", request_token=token_a)
    assert [call[0] for call in inserter.calls] == ["dictation A."]

    inserter.now += 0.5
    _stream(controller)
    assert [call[0] for call in inserter.calls] == ["dictation A."]

    inserter.now += CLIPBOARD_RESTORE_DELAY_S
    _stream(controller, (_PARTIALS[-1], f"{_PARTIALS[-1]} elf"))
    assert len(inserter.calls) > 1
    controller.shutdown()
    _ = app


def test_a_queued_stream_result_holds_the_next_streams_live_words(
    monkeypatch, tmp_path
):
    """Stream S1 inserted nothing live, so its result went into the paste
    queue behind the open restore window. Stream S2 for the same window
    starts, and its first partial arrives after the window ended but before
    the pace timer ran. S1's result goes first; before, the order check
    skipped every streaming job, and S2's live words went in ahead of it."""
    inserter = PacedTextInserter()
    controller, app, _overlay, inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    controller.stop_recording()
    token_s1 = controller._active_request_token
    # A paste into some window just went out.
    inserter.last_keystroke_at = inserter.now
    controller._on_transcription_ready("dictation S1.", request_token=token_s1)
    assert inserter.calls == []

    controller.start_recording()
    inserter.now += CLIPBOARD_RESTORE_DELAY_S
    _stream(controller)

    pasted = [call[0] for call in inserter.calls]
    assert pasted and pasted[0] == "dictation S1."
    controller.shutdown()
    _ = app


def test_the_order_check_counts_a_stream_result_waiting_in_the_paste_queue(
    monkeypatch, tmp_path
):
    """The predicate itself: a streaming job whose words went in live never
    waits, one held in the paste queue (`insertion_deferred`) does."""
    controller, app, _overlay, _inserter, _focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert"
    )
    job = controller._register_transcription_job(41, controller.settings, "streaming")
    job.target_handle = 987
    assert controller._earlier_result_waits_for(987) is False
    job.insertion_deferred = True
    assert controller._earlier_result_waits_for(987) is True
    assert controller._earlier_result_waits_for(987, before=41) is False
    controller.shutdown()
    _ = app


def test_a_stream_with_nothing_inserted_queues_behind_a_held_earlier_result(
    monkeypatch, tmp_path
):
    """At the stream's stop the earlier result is held by the paste pace.
    The streaming result -- none of it in the window yet -- used to paste at
    once and so ahead of it; it now joins the paste queue behind it."""
    inserter = PacedTextInserter()
    controller, app, _overlay, inserter, focus, _history = _make_queue_controller(
        monkeypatch, tmp_path, mode="insert", inserter=inserter
    )
    token_a = _record_and_stop(controller)
    controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    # The user is in another window: nothing is pasted during the stream.
    focus.current = 555
    controller._on_transcription_ready("dictation A.", request_token=token_a)
    focus.current = 987
    controller.stop_recording()
    token_s = controller._active_request_token
    # A paste into some window just went out.
    inserter.last_keystroke_at = inserter.now

    controller._on_transcription_ready("dictation S.", request_token=token_s)
    assert inserter.calls == []

    inserter.now += CLIPBOARD_RESTORE_DELAY_S
    controller._on_paste_pace_timeout()
    assert [call[0] for call in inserter.calls] == ["dictation A. dictation S."]
    controller.shutdown()
    _ = app
