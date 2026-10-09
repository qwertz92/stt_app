"""The tray's Quit with work pending: the question, the wait and its end."""

from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6 import QtWidgets

from stt_app.controller import PendingQuitWork
from stt_app.quit_dialog import QuitCoordinator, QuitDialog


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _FakeController:
    def __init__(self, work: PendingQuitWork):
        self.work = work
        self.holds = 0
        self.releases = 0

    def quit_pending_work(self):
        return self.work

    def hold_for_quit(self):
        self.holds += 1

    def release_quit_hold(self):
        self.releases += 1


_BUSY = PendingQuitWork(recording=True, transcribing=2, waiting_to_insert=1)


def _coordinator(work):
    controller = _FakeController(work)
    quits = []
    coordinator = QuitCoordinator(controller, lambda: quits.append(True))
    return coordinator, controller, quits


def test_nothing_pending_quits_at_once_without_a_window(app):
    coordinator, _controller, quits = _coordinator(PendingQuitWork())

    coordinator.request()

    assert quits == [True]
    assert coordinator.dialog is None


def test_pending_work_asks_instead_of_quitting(app):
    coordinator, controller, quits = _coordinator(_BUSY)

    coordinator.request()

    assert quits == []
    dialog = coordinator.dialog
    assert dialog is not None and dialog.isVisible()
    assert dialog.transcribing_value.text() == "2"
    assert dialog.recording_value.text() == "Yes"
    assert controller.holds == 0, "asking must not stop the recording yet"
    dialog.close_for_quit()


def test_waiting_quits_only_once_the_work_is_delivered(app):
    """The quit -- and the watchdog `main` arms on aboutToQuit -- must not
    start while the user deliberately waits."""
    coordinator, controller, quits = _coordinator(_BUSY)
    coordinator.request()

    coordinator.dialog.wait_button.click()
    controller.work = PendingQuitWork(transcribing=1)
    coordinator._poll()

    assert quits == []
    assert controller.holds >= 2, "every poll holds the quit"
    assert coordinator.dialog.wait_button.isEnabled() is False

    controller.work = PendingQuitWork()
    coordinator._poll()

    assert quits == [True]
    assert coordinator.dialog is None


def test_a_paste_that_fails_during_the_wait_stops_the_quit_to_say_so(app):
    coordinator, controller, quits = _coordinator(_BUSY)
    coordinator.request()
    coordinator.dialog.wait_button.click()
    dialog = coordinator.dialog

    controller.work = PendingQuitWork(not_inserted=1, failed=1)
    coordinator._poll()

    assert quits == []
    assert "could not be inserted" in dialog.status_label.text()
    assert "offered at the next start" in dialog.status_label.text()
    dialog.wait_button.click()
    assert quits == [True]


def test_dont_quit_calls_the_quit_off_and_lets_dictation_resume(app):
    coordinator, controller, quits = _coordinator(_BUSY)
    coordinator.request()
    coordinator.dialog.wait_button.click()

    coordinator.dialog.dont_quit_button.click()

    assert quits == []
    assert controller.releases == 1
    assert coordinator.dialog is None
    coordinator.request()
    assert coordinator.dialog is not None, "the next Quit must ask again"
    coordinator.dialog.close_for_quit()


def test_closing_the_window_is_dont_quit(app):
    coordinator, controller, quits = _coordinator(_BUSY)
    coordinator.request()

    coordinator.dialog.close()

    assert quits == []
    assert controller.releases == 1


def test_quit_now_quits_without_waiting(app):
    coordinator, controller, quits = _coordinator(_BUSY)
    coordinator.request()

    coordinator.dialog.quit_now_button.click()

    assert quits == [True]
    assert controller.holds == 0


def test_only_rows_that_were_not_inserted_offer_no_wait(app):
    coordinator, _controller, _quits = _coordinator(PendingQuitWork(not_inserted=2))

    coordinator.request()

    dialog = coordinator.dialog
    assert dialog.wait_button.isEnabled() is False
    assert dialog.quit_now_button.text() == "Quit now"
    dialog.close_for_quit()


def test_the_window_keeps_one_size_and_its_buttons_stay_put(app):
    """Nothing may jump: the counts, the status line and the button labels
    change with the state, the window and its buttons must not."""
    dialog = QuitDialog()
    dialog.show_question(_BUSY)
    dialog.show()
    app.processEvents()
    size = dialog.size()
    buttons = (dialog.wait_button, dialog.quit_now_button, dialog.dont_quit_button)
    geometry = [button.geometry() for button in buttons]
    long_problem = (
        "Done waiting. 12 transcripts could not be inserted; they are in History. "
        "3 transcriptions failed; their recordings are offered at the next start."
    )

    states = [
        lambda: dialog.show_waiting(replace(_BUSY, transcribing=12), 1234),
        lambda: dialog.show_problems(PendingQuitWork(not_inserted=12), long_problem),
        lambda: dialog.show_question(PendingQuitWork(not_inserted=3)),
        lambda: dialog.show_question(_BUSY),
    ]
    for show in states:
        show()
        app.processEvents()
        assert dialog.size() == size
        assert [button.geometry() for button in buttons] == geometry
        label = dialog.status_label
        assert label.heightForWidth(label.width()) <= label.height(), (
            f"the status line is clipped: {label.text()!r}"
        )
    dialog.close_for_quit()
