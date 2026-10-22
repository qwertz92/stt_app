"""Hand a worker thread's reports to a widget without the worker touching it.

A worker that captures a dialog and emits the dialog's own signals has two
ways to crash the process, both measured on 2026-10-23 on History >
Retranscribe (`docs/agents/persistence-and-history.md`):

* PySide6's `QDialog.exec()` hands the dialog back to Python ownership, so it
  dies with its last Python reference even though it has a parent. Once the
  user closed the dialog, the worker that captured it held that last
  reference, and dropping it at the end of the run destroyed a QWidget on the
  worker thread: Qt warned "Timers cannot be stopped from another thread" and
  the process aborted or faulted.
* Emitting on a QObject while the GUI thread deletes it (its window closed
  with `WA_DeleteOnClose`) is a data race: the emit raised a TypeError instead
  of the RuntimeError a guard expected, and the process aborted.

A `GuiThreadReporter` holds only a weak reference to its receiver and posts
each call through one dispatcher object that the application owns and that
lives as long as it does. The call runs on the GUI thread, and only while the
receiver still exists; otherwise it is dropped.
"""

from __future__ import annotations

import functools
import weakref

import shiboken6
from PySide6 import QtCore


class _Dispatcher(QtCore.QObject):
    """Runs posted callables on the thread it lives in (the GUI thread)."""

    _posted = QtCore.Signal(object)

    def __init__(self, parent: QtCore.QObject) -> None:
        super().__init__(parent)
        # Queued even when posted from the GUI thread itself, so a report
        # never runs inside the caller's own stack.
        self._posted.connect(self._run, QtCore.Qt.QueuedConnection)

    @QtCore.Slot(object)
    def _run(self, call: object) -> None:
        call()

    def post(self, call: object) -> bool:
        try:
            self._posted.emit(call)
        except RuntimeError:
            # The application, and with it this object, is gone (process
            # exit): there is no GUI left to report to.
            return False
        return True


_dispatcher: _Dispatcher | None = None


def _gui_dispatcher() -> _Dispatcher:
    global _dispatcher
    app = QtCore.QCoreApplication.instance()
    if app is None:
        raise RuntimeError("GuiThreadReporter needs a running QApplication.")
    if QtCore.QThread.currentThread() != app.thread():
        raise RuntimeError("GuiThreadReporter must be created on the GUI thread.")
    if _dispatcher is None or not shiboken6.isValid(_dispatcher):
        # A child of the application: owned by C++, so no Python reference a
        # worker drops can ever destroy it.
        _dispatcher = _Dispatcher(app)
    return _dispatcher


class GuiThreadReporter:
    """Calls methods of one receiver on the GUI thread, from any thread.

    Create it on the GUI thread and hand it -- never the receiver -- to the
    worker. `post` is safe from any thread at any time.
    """

    def __init__(self, receiver: QtCore.QObject) -> None:
        self._dispatcher = _gui_dispatcher()
        self._receiver = weakref.ref(receiver)

    def post(self, method_name: str, *args: object) -> bool:
        """Queue ``receiver.<method_name>(*args)``; False once the app is gone."""
        return self._dispatcher.post(
            functools.partial(self._deliver, method_name, args)
        )

    def _deliver(self, method_name: str, args: tuple[object, ...]) -> None:
        receiver = self._receiver()
        if receiver is None or not shiboken6.isValid(receiver):
            return
        getattr(receiver, method_name)(*args)
