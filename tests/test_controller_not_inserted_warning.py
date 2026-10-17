"""A transcript that exists but was not (surely) inserted is a warning.

Owner's decision 2026-10-17: when the transcription worked and only the paste
failed or is doubtful, the overlay is not the red Error -- the text is safe and
Insert, Copy and Edit work -- it is painted as a warning under a word that says
what happened. Red stays for failures that left no transcript. Every writer of
that state goes through `DictationController._paint_not_inserted`, which picks
the word and the action from one flag; the `FakeOverlay` in `conftest.py`
refuses an Insert offer painted without the warning, so any other writer fails
every test that reaches it.
"""

from __future__ import annotations

import pytest
from conftest import FakeOverlay, FakeTextInserter, make_controller
from PySide6 import QtGui

import stt_app.controller as controller_module
from stt_app.config import (
    OVERLAY_ERROR_ACTION_INSERT,
    OVERLAY_ERROR_ACTION_NONE,
    OVERLAY_WARNING_CHECK_WINDOW,
    OVERLAY_WARNING_NOT_INSERTED,
)
from stt_app.text_inserter import TextMayHaveBeenPastedError


class _Clipboard:
    """The failed-paste path copies the transcript; keep it off the real one."""

    def setText(self, _text):
        pass


class _InserterThatPastesThenFails(FakeTextInserter):
    """The paste keystroke went out and only the cleanup failed."""

    def insert_text_with_options(self, text, target_hwnd=None, **_kwargs):
        self.calls.append((text, target_hwnd))
        raise TextMayHaveBeenPastedError(
            "The text was pasted but the clipboard could not be restored."
        )


def _controller(monkeypatch, inserter):
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: _Clipboard())
    overlay = FakeOverlay()
    controller, app = make_controller(overlay=overlay, text_inserter=inserter)
    controller._target_window_handle = 555
    return controller, app, overlay


def _background_job(controller):
    return controller_module._TranscriptionJob(
        token=7,
        engine="local",
        model="small",
        mode="batch",
        settings=controller.settings,
        target_handle=123,
        target_signature=None,
    )


def _painted(overlay):
    return overlay.states[-1], overlay.state_kwargs[-1]


def test_a_paste_that_failed_is_painted_as_not_inserted_with_insert(monkeypatch):
    controller, app, overlay = _controller(
        monkeypatch, FakeTextInserter(should_fail=True)
    )

    controller._on_transcription_ready("the transcribed sentence")

    (state, _detail), kwargs = _painted(overlay)
    assert state == "Error", "the state stays Error for every control and check"
    assert kwargs["warning"] == OVERLAY_WARNING_NOT_INSERTED
    assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_INSERT
    assert kwargs["copy_text"] == "the transcribed sentence"
    controller.shutdown()
    _ = app


def test_a_paste_whose_keystroke_went_out_says_to_check_the_window(monkeypatch):
    """The text is most likely in the window, so it is neither "Not inserted"
    nor offered again -- but it is no error either: nothing needs retrying."""
    controller, app, overlay = _controller(monkeypatch, _InserterThatPastesThenFails())

    controller._on_transcription_ready("the transcribed sentence")

    (state, detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert "check the target window" in detail
    assert kwargs["warning"] == OVERLAY_WARNING_CHECK_WINDOW
    assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_NONE
    controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    ("may_have_pasted", "warning", "action"),
    [
        (False, OVERLAY_WARNING_NOT_INSERTED, OVERLAY_ERROR_ACTION_INSERT),
        (True, OVERLAY_WARNING_CHECK_WINDOW, OVERLAY_ERROR_ACTION_NONE),
    ],
)
def test_a_queued_transcript_that_failed_to_paste_is_a_warning_too(
    monkeypatch, may_have_pasted, warning, action
):
    controller, app, overlay = _controller(monkeypatch, FakeTextInserter())
    controller._last_insert_may_have_pasted = may_have_pasted

    assert controller._report_background_insertion_failure(
        _background_job(controller), "transcript A"
    )

    (state, _detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert kwargs["warning"] == warning
    assert kwargs["error_action"] == action
    controller.shutdown()
    _ = app


def test_a_status_painted_over_a_pending_offer_keeps_it_a_warning(monkeypatch):
    """A refusal, a notice or a settings save repaints the pending offer
    ("Still not inserted:"); the repaint is the same warning, not red."""
    controller, app, overlay = _controller(
        monkeypatch, FakeTextInserter(should_fail=True)
    )
    controller._on_transcription_ready("the transcribed sentence")

    controller._paint_status_keeping_offer("Error", "No window to insert into.")

    (state, detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert "Still not inserted" in detail
    assert kwargs["warning"] == OVERLAY_WARNING_NOT_INSERTED
    assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_INSERT

    # The offer's own keystroke may have gone out: same warning, no Insert.
    controller._insert_offer_may_have_pasted = True
    controller._paint_status_keeping_offer("Error", "No window to insert into.")

    (state, detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert "check the target window" in detail
    assert kwargs["warning"] == OVERLAY_WARNING_CHECK_WINDOW
    assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_NONE
    controller.shutdown()
    _ = app


def test_failures_that_left_no_transcript_stay_the_red_error(monkeypatch):
    """Red is for what Retry or the user must act on without a transcript."""
    controller, app, overlay = _controller(monkeypatch, FakeTextInserter())

    controller._on_transcription_failed("The provider refused the request.")
    (state, _detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert not kwargs.get("warning")

    controller._report_background_failure(None, "boom", retry_available=False)
    (state, _detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert not kwargs.get("warning")

    # No offer pending: a refusal is a plain Error, as before.
    controller._paint_status_keeping_offer("Error", "No window to insert into.")
    (state, _detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert not kwargs.get("warning")
    controller.shutdown()
    _ = app
