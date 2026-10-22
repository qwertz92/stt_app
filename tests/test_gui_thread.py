"""GuiThreadReporter: a worker's reports run on the GUI thread or not at all."""

from __future__ import annotations

import threading

from PySide6 import QtCore, QtWidgets

from stt_app.gui_thread import GuiThreadReporter


class _Receiver(QtCore.QObject):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def on_report(self, text: str) -> None:
        self.calls.append((text, threading.current_thread().name))


def _post_from_worker(reporter: GuiThreadReporter, text: str) -> None:
    worker = threading.Thread(
        target=lambda: reporter.post("on_report", text), name="report_worker"
    )
    worker.start()
    worker.join(5)


def test_a_report_runs_on_the_gui_thread():
    """A widget may only be touched on the GUI thread; the worker posts."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    receiver = _Receiver()
    reporter = GuiThreadReporter(receiver)

    _post_from_worker(reporter, "progress")
    assert receiver.calls == [], "a report ran inside the worker's own call"
    app.processEvents()

    assert receiver.calls == [("progress", threading.main_thread().name)]


def test_a_report_to_a_deleted_receiver_is_dropped():
    """A dialog closed during the run gets nothing, and nothing raises."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    parent = QtCore.QObject()
    receiver = _Receiver()
    receiver.setParent(parent)
    calls = receiver.calls
    reporter = GuiThreadReporter(receiver)
    parent.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    app.processEvents()

    _post_from_worker(reporter, "finished")
    app.processEvents()

    assert calls == []
