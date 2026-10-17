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

import ast
from pathlib import Path

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


def _register_every_hotkey(controller):
    """A started app has its hotkeys registered; a bare controller reports each
    as failed, which `show_idle_status` would paint as the failure it is."""
    controller._hotkey_registration_ok = True
    controller._cancel_hotkey_registration_ok = True
    controller._show_overlay_hotkey_registration_ok = True
    controller._repaste_hotkey_registration_ok = True


def _controller(monkeypatch, inserter):
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: _Clipboard())
    overlay = FakeOverlay()
    controller, app = make_controller(overlay=overlay, text_inserter=inserter)
    controller._target_window_handle = 555
    _register_every_hotkey(controller)
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


def _status_over_the_offer(controller, how):
    """One way a status reaches the overlay while an Insert offer is pending."""
    if how == "idle line":
        controller.show_idle_status()
    elif how == "re-paste refusal":
        # The overlay's own Insert (F10) answered "No window to insert into".
        controller._window_focus_helper.current = None
        controller._window_focus_helper.current_focus = None
        controller._window_focus_helper.current_caret = None
        controller.repaste_last_transcript()
    elif how == "preload progress":
        controller._paint_status_keeping_offer("Processing", "Loading model...")
    elif how == "hotkey registration failed":
        controller._hotkey_registration_ok = False
        controller.show_idle_status()
    elif how == "preload failed":
        controller._on_model_preload_done(0, False, "Download failed: offline.")
    elif how == "settings save refused":
        controller.show_overlay_error("The opacity was not saved. Disk full.")
    elif how == "recording start refused":
        controller._refuse_recording_start("Model is still loading.")
    else:
        raise AssertionError(how)


_ABOUT_THE_OFFER = ["idle line", "re-paste refusal", "preload progress"]
_REAL_FAILURES = [
    "hotkey registration failed",
    "preload failed",
    "settings save refused",
    "recording start refused",
]


@pytest.mark.parametrize("how", _ABOUT_THE_OFFER)
def test_a_status_about_the_offer_keeps_it_a_warning(monkeypatch, how):
    """An informational line or a refusal of the insert itself repaints the
    pending offer ("Still not inserted:"); the repaint is the same warning."""
    controller, app, overlay = _controller(
        monkeypatch, FakeTextInserter(should_fail=True)
    )
    controller._on_transcription_ready("the transcribed sentence")

    _status_over_the_offer(controller, how)

    (state, detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert "Still not inserted:\nthe transcribed sentence" in detail
    assert kwargs["warning"] == OVERLAY_WARNING_NOT_INSERTED
    assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_INSERT

    # The offer's own keystroke may have gone out: same warning, no Insert.
    controller._insert_offer_may_have_pasted = True
    _status_over_the_offer(controller, how)

    (state, detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert "check the target window" in detail
    assert kwargs["warning"] == OVERLAY_WARNING_CHECK_WINDOW
    assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_NONE
    controller.shutdown()
    _ = app


@pytest.mark.parametrize("how", _REAL_FAILURES)
@pytest.mark.parametrize("may_have_pasted", [False, True])
def test_a_real_failure_over_a_pending_offer_is_red_and_keeps_the_offer(
    monkeypatch, how, may_have_pasted
):
    """Severity wins (lead's decision 2026-10-17): a failure that has nothing
    to do with the insert -- a hotkey that did not register, a model that did
    not load, a setting that was not saved, a refused recording start -- is
    the red Error, not the orange "Not inserted" that hid it. The transcript
    is never lost to it: the offer, its detail, Copy and Insert stay."""
    controller, app, overlay = _controller(
        monkeypatch, FakeTextInserter(should_fail=True)
    )
    controller._on_transcription_ready("the transcribed sentence")
    controller._insert_offer_may_have_pasted = may_have_pasted

    _status_over_the_offer(controller, how)

    (state, detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert "warning" in kwargs, "decided by the one writer, not skipped"
    assert kwargs["warning"] is None, "painted as the red Error"
    assert kwargs["copy_text"] == "the transcribed sentence"
    assert kwargs["error_action"] == (
        OVERLAY_ERROR_ACTION_NONE if may_have_pasted else OVERLAY_ERROR_ACTION_INSERT
    )
    # The failure comes first; the transcript follows, as with the warning.
    assert detail.endswith(
        "check the target window before inserting it again:\nthe transcribed sentence"
        if may_have_pasted
        else "Still not inserted:\nthe transcribed sentence"
    )
    assert controller._insert_action_text == "the transcribed sentence"
    # Once the failure is gone, the next status about the offer is the
    # warning again.
    _register_every_hotkey(controller)
    _status_over_the_offer(controller, "idle line")
    assert _painted(overlay)[1]["warning"] is not None
    controller.shutdown()
    _ = app


def test_the_overlay_shows_red_for_a_failure_and_orange_for_the_offer(monkeypatch):
    """End to end on the real overlay: the same pending Insert offer reads
    "Not inserted" in orange, turns "Error" in red while a hotkey failure is
    shown, and Insert, Copy and Edit work in both."""
    from PySide6 import QtCore, QtWidgets

    from stt_app.config import OVERLAY_STATE_COLORS
    from stt_app.overlay_ui import OverlayUI

    overlay = OverlayUI()
    overlay.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    overlay.show()
    QtWidgets.QApplication.processEvents()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: _Clipboard())
    controller, app = make_controller(
        overlay=overlay, text_inserter=FakeTextInserter(should_fail=True)
    )
    controller._target_window_handle = 555
    _register_every_hotkey(controller)
    try:
        controller._on_transcription_ready("the transcribed sentence")
        QtWidgets.QApplication.processEvents()
        orange = (
            overlay._state_label.text(),
            overlay._state_background,
            overlay._insert_button.isVisible(),
            overlay._copy_button.isEnabled(),
        )
        assert orange == (
            OVERLAY_WARNING_NOT_INSERTED,
            OVERLAY_STATE_COLORS["Warning"],
            True,
            True,
        )

        controller._hotkey_registration_ok = False
        controller.show_idle_status()
        QtWidgets.QApplication.processEvents()
        assert overlay._state_label.text() == "Error"
        assert overlay._state_background == OVERLAY_STATE_COLORS["Error"]
        assert overlay._insert_button.isVisible(), "the offer must survive"
        assert overlay._copy_button.isEnabled()
        assert overlay.copy_text == "the transcribed sentence"

        controller._hotkey_registration_ok = True
        controller.show_idle_status()
        QtWidgets.QApplication.processEvents()
        assert overlay._state_label.text() == OVERLAY_WARNING_NOT_INSERTED
        assert overlay._state_background == OVERLAY_STATE_COLORS["Warning"]
    finally:
        controller.shutdown()
        overlay.close()
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


class _RunningPreload:
    """A model download in flight: the progress poll has work to do."""

    @staticmethod
    def done() -> bool:
        return False


_FAILURES_DURING_A_PRELOAD = [
    ("recording start refused", "Model is still loading."),
    ("settings save refused", "The opacity was not saved. Disk full."),
    ("hotkey registration failed", "Hotkey registration failed."),
]


@pytest.mark.parametrize(("how", "failure_text"), _FAILURES_DURING_A_PRELOAD)
def test_the_preload_poll_leaves_a_red_failure_over_an_offer_alone(
    monkeypatch, how, failure_text
):
    """The poll repaints every 600 ms, and it repaints the painter's own offer
    so the progress line does not freeze. A red failure painted over a pending
    offer is the painter's output too, so the poll took it for an offer to
    refresh and turned it back to the orange "Not inserted" within a poll
    interval: the refusal ("Model is still loading.") vanished before it
    could be read. The failure stays until something else replaces it."""
    controller, app, overlay = _controller(
        monkeypatch, FakeTextInserter(should_fail=True)
    )
    controller._on_transcription_ready("the transcribed sentence")
    controller._preload_future = _RunningPreload()
    try:
        _status_over_the_offer(controller, how)
        painted = len(overlay.states)
        assert _painted(overlay)[1]["warning"] is None

        for _ in range(3):
            controller._on_preload_progress_poll()

        assert len(overlay.states) == painted, (
            f"the poll painted over the failure: {overlay.states[painted:]!r}"
        )
        (state, detail), kwargs = _painted(overlay)
        assert (state, kwargs["warning"]) == ("Error", None)
        assert failure_text in detail
        assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_INSERT
    finally:
        controller._preload_future = None
        controller.shutdown()
    _ = app


def test_the_preload_poll_still_refreshes_the_progress_line_over_an_offer(
    monkeypatch,
):
    """What the poll must keep doing: the preload's start line and every
    progress line are informational, painted over the offer as the warning,
    and refreshed by the poll. A failure in between ends that until the next
    informational status -- a preload that restarts -- paints again."""
    controller, app, overlay = _controller(
        monkeypatch, FakeTextInserter(should_fail=True)
    )
    controller._on_transcription_ready("the transcribed sentence")
    controller._preload_future = _RunningPreload()
    try:
        controller._paint_status_keeping_offer(
            "Processing", "Loading selected model..."
        )
        painted = len(overlay.states)
        controller._on_preload_progress_poll()
        assert len(overlay.states) == painted + 1, "the progress line is refreshed"
        (state, _detail), kwargs = _painted(overlay)
        assert (state, kwargs["warning"]) == (
            "Error",
            OVERLAY_WARNING_NOT_INSERTED,
        )

        controller._refuse_recording_start("Model is still downloading.")
        painted = len(overlay.states)
        controller._on_preload_progress_poll()
        assert len(overlay.states) == painted

        controller._paint_status_keeping_offer(
            "Processing", "Loading selected model..."
        )
        painted = len(overlay.states)
        controller._on_preload_progress_poll()
        assert len(overlay.states) == painted + 1, "refreshed again"
        assert _painted(overlay)[1]["warning"] == OVERLAY_WARNING_NOT_INSERTED
    finally:
        controller._preload_future = None
        controller.shutdown()
    _ = app


def test_only_the_one_writer_names_the_insert_action():
    """`FakeOverlay` can only check that a writer passed the `warning` key, so
    a second writer that passed `warning=None` for an Insert offer would go
    unnoticed. The offer is created by naming `OVERLAY_ERROR_ACTION_INSERT`;
    this pins that the controller does so in `_paint_not_inserted` alone (the
    import is not a use), so a second writer fails here, not in review."""
    tree = ast.parse(Path(controller_module.__file__).read_text(encoding="utf-8"))

    def uses(root):
        return {
            id(node)
            for node in ast.walk(root)
            if (isinstance(node, ast.Name) and node.id == "OVERLAY_ERROR_ACTION_INSERT")
            or (
                isinstance(node, ast.Attribute)
                and node.attr == "OVERLAY_ERROR_ACTION_INSERT"
            )
        }

    writer = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_paint_not_inserted"
    ]
    assert len(writer) == 1
    in_the_writer = uses(writer[0])
    assert in_the_writer, "the one writer must offer Insert; the guard is stale"
    assert uses(tree) == in_the_writer, (
        "OVERLAY_ERROR_ACTION_INSERT is named outside `_paint_not_inserted`: "
        "route the new offer through it"
    )


@pytest.mark.parametrize("retry_available", [True, False], ids=["kept", "not kept"])
def test_a_background_failure_over_a_pending_offer_keeps_the_offer(
    monkeypatch, retry_available
):
    """Another queued job failing while an Insert offer is pending painted a
    plain Error over it: the transcript, Copy and the Insert button left the
    screen while the text stayed pending, and came back only with the next
    status. A real failure is red and keeps the offer, as every other failure
    does. The overlay has one action slot, so Insert takes the place of
    Retry; the text says where Retry is (the tray) instead of pointing at a
    button that is not there."""
    controller, app, overlay = _controller(
        monkeypatch, FakeTextInserter(should_fail=True)
    )
    controller._on_transcription_ready("the transcribed sentence")
    job = _background_job(controller)

    controller._report_background_failure(job, "boom", retry_available)

    (state, detail), kwargs = _painted(overlay)
    assert state == "Error"
    assert kwargs["warning"] is None, "a failure is the red Error"
    assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_INSERT
    assert kwargs["copy_text"] == "the transcribed sentence"
    assert detail.startswith(f"{controller._job_identity(job)} failed: boom")
    assert detail.endswith("Still not inserted:\nthe transcribed sentence")
    assert ("Retry transcription in the tray menu" in detail) is retry_available
    assert "use Retry to" not in detail
    controller.shutdown()
    _ = app
