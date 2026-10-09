"""Quit with work pending: ask, or wait for it, before the app goes away.

The tray's Quit used to end the process at once, dropping a running
transcription, the queue, held pastes and the rows of transcripts that were
not inserted. `QuitCoordinator` asks first whenever
`DictationController.quit_pending_work` reports anything, and quits as before
when nothing is pending.

"Wait and insert" keeps the app running until the pending transcriptions are
delivered as usual, then quits. The quit itself -- and with it the quit
watchdog armed on `aboutToQuit` in `main` -- only starts once the wait is over,
so a long wait is never cut short by the watchdog. A recording started during
the wait calls the quit off and closes the window. "Quit now" quits; the
controller's shutdown keeps the unfinished recordings for the next start.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .app_icon import load_app_icon
from .config import APP_DISPLAY_NAME
from .controller import PendingQuitWork
from .dialog_style import apply_dialog_style, make_label_selectable
from .ui_feedback import reserve_button_width_for_texts

QUIT_WAIT_POLL_MS = 250

_WAIT_TEXT = "Wait and insert"
_WAITING_TEXT = "Waiting..."
_QUIT_TEXT = "Quit"
_QUIT_NOW_KEEP_TEXT = "Quit now, keep the recordings"
_QUIT_NOW_TEXT = "Quit now"
_DONT_QUIT_TEXT = "Don't quit"

_EXPLANATION = (
    "Wait and insert finishes the transcriptions, inserts them as usual and "
    "then quits. Quit now quits at once: unfinished recordings are kept, and "
    "the next start offers to transcribe them. Every finished transcript is "
    "in History either way."
)
# The longest status line, so its reserved height holds every one of them.
_LONGEST_STATUS = (
    "Done waiting. 99 transcripts could not be inserted; they are in History. "
    "99 transcriptions failed; their recordings are offered at the next start."
)
_STATUS_LINES = 3


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


class QuitDialog(QtWidgets.QDialog):
    """The quit question and, after "Wait and insert", the wait itself.

    One fixed size for every state: the four counts stand in rows that are
    always there, the explanation never changes, the status line has its
    height reserved for the longest text it can show, and each button keeps
    the width of its widest label.
    """

    wait_requested = QtCore.Signal()
    quit_requested = QtCore.Signal()
    dismissed = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Quit {APP_DISPLAY_NAME}")
        self.setWindowIcon(load_app_icon())
        # A tool window: `window_focus` never takes one of ours for a paste
        # target, so a paste delivered during the wait into "the current
        # window" goes to the user's window, not here. On top, so the wait
        # stays visible while those pastes move the focus.
        self.setWindowFlags(
            QtCore.Qt.Tool
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.WindowTitleHint
            | QtCore.Qt.WindowCloseButtonHint
        )
        apply_dialog_style(self)
        self._closing_by_choice = False

        heading = QtWidgets.QLabel("Work is still pending")
        heading_font = heading.font()
        heading_font.setBold(True)
        heading.setFont(heading_font)

        self.recording_value = QtWidgets.QLabel()
        self.transcribing_value = QtWidgets.QLabel()
        self.waiting_value = QtWidgets.QLabel()
        self.not_inserted_value = QtWidgets.QLabel()
        counts = QtWidgets.QFormLayout()
        counts.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        counts.addRow("Recording in progress:", self.recording_value)
        counts.addRow("Transcribing or queued:", self.transcribing_value)
        counts.addRow("Finished, waiting to be inserted:", self.waiting_value)
        counts.addRow("Not inserted (in History):", self.not_inserted_value)

        explanation = QtWidgets.QLabel(_EXPLANATION)
        explanation.setWordWrap(True)
        explanation.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)

        self.status_label = QtWidgets.QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        make_label_selectable(self.status_label)

        # Parented at once: the width reservation below measures through the
        # dialog's stylesheet only then. Unparented, it measured the plain
        # style's narrower padding, and "Waiting..." shrank the button 10 px.
        self.wait_button = QtWidgets.QPushButton(_WAIT_TEXT, self)
        self.wait_button.setProperty("primary", True)
        self.quit_now_button = QtWidgets.QPushButton(_QUIT_NOW_KEEP_TEXT, self)
        self.dont_quit_button = QtWidgets.QPushButton(_DONT_QUIT_TEXT, self)
        reserve_button_width_for_texts(
            self.wait_button, (_WAIT_TEXT, _WAITING_TEXT, _QUIT_TEXT)
        )
        reserve_button_width_for_texts(
            self.quit_now_button, (_QUIT_NOW_KEEP_TEXT, _QUIT_NOW_TEXT)
        )
        self.wait_button.clicked.connect(self._on_wait_clicked)
        self.quit_now_button.clicked.connect(self._choose_quit)
        self.dont_quit_button.clicked.connect(self.reject)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.wait_button)
        buttons.addWidget(self.quit_now_button)
        buttons.addStretch(1)
        buttons.addWidget(self.dont_quit_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addLayout(counts)
        layout.addWidget(explanation)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self._final = False
        self._fix_size(layout, explanation)

    def _fix_size(
        self, layout: QtWidgets.QVBoxLayout, explanation: QtWidgets.QLabel
    ) -> None:
        self.ensurePolished()
        margins = layout.contentsMargins()
        width = max(480, layout.sizeHint().width())
        text_width = width - margins.left() - margins.right()
        explanation.setFixedHeight(explanation.heightForWidth(text_width))
        self.status_label.setText(_LONGEST_STATUS)
        status_height = max(
            self.status_label.heightForWidth(text_width),
            self.status_label.fontMetrics().lineSpacing() * _STATUS_LINES,
        )
        self.status_label.setFixedHeight(status_height)
        self.status_label.setText("")
        layout.activate()
        self.setFixedSize(width, layout.sizeHint().height())

    # -- what the coordinator shows -------------------------------------------

    def show_counts(self, work: PendingQuitWork) -> None:
        self.recording_value.setText("Yes" if work.recording else "No")
        self.transcribing_value.setText(str(work.transcribing))
        self.waiting_value.setText(str(work.waiting_to_insert))
        self.not_inserted_value.setText(str(work.not_inserted))
        keeps_recordings = work.recording or work.transcribing > 0
        self.quit_now_button.setText(
            _QUIT_NOW_KEEP_TEXT if keeps_recordings else _QUIT_NOW_TEXT
        )

    def show_question(self, work: PendingQuitWork) -> None:
        self._final = False
        self.show_counts(work)
        self.wait_button.setText(_WAIT_TEXT)
        self.wait_button.setEnabled(work.can_wait)
        self.quit_now_button.setEnabled(True)
        self.status_label.setText(
            ""
            if work.can_wait
            else "Nothing is left to wait for; the transcripts that were not "
            "inserted are in History."
        )

    def show_waiting(self, work: PendingQuitWork, elapsed_s: int) -> None:
        self._final = False
        self.show_counts(work)
        self.wait_button.setText(_WAITING_TEXT)
        self.wait_button.setEnabled(False)
        self.quit_now_button.setEnabled(True)
        self.status_label.setText(
            f"Waiting... {elapsed_s} s. The app quits once everything is inserted."
        )

    def show_problems(self, work: PendingQuitWork, message: str) -> None:
        """The wait is over, and something did not arrive: say what, then
        let the user quit or stay."""
        self._final = True
        self.show_counts(work)
        self.wait_button.setText(_QUIT_TEXT)
        self.wait_button.setEnabled(True)
        self.quit_now_button.setEnabled(False)
        self.status_label.setText(message)

    # -- choices ---------------------------------------------------------------

    def _on_wait_clicked(self) -> None:
        if self._final:
            self._choose_quit()
        else:
            self.wait_requested.emit()

    def _choose_quit(self) -> None:
        self.quit_requested.emit()

    def close_for_quit(self) -> None:
        """Close without reporting a dismissal: the app is quitting."""
        self._closing_by_choice = True
        self.close()

    def _report_dismissal_once(self) -> None:
        if not self._closing_by_choice:
            self._closing_by_choice = True
            self.dismissed.emit()

    # "Don't quit", Esc and the title bar's close all call the quit off.
    def reject(self) -> None:
        self._report_dismissal_once()
        super().reject()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._report_dismissal_once()
        super().closeEvent(event)


class QuitCoordinator(QtCore.QObject):
    """The tray's Quit: quit at once, or ask and possibly wait first."""

    def __init__(
        self,
        controller,
        quit_app: Callable[[], None],
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._quit_app = quit_app
        self._dialog: QuitDialog | None = None
        self._waiting = False
        self._wait_started = 0.0
        self._baseline = PendingQuitWork()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(QUIT_WAIT_POLL_MS)
        self._timer.timeout.connect(self._poll)
        # A recording started during the wait calls the quit off (owner
        # decision 2026-10-09); the controller has released its hold already
        # and told the tray. `getattr`: the controller is injected.
        canceled = getattr(controller, "quit_canceled_by_recording", None)
        if canceled is not None:
            canceled.connect(self._on_quit_canceled_by_recording)

    @property
    def dialog(self) -> QuitDialog | None:
        return self._dialog

    def request(self) -> None:
        if self._dialog is not None:
            self._present(self._dialog)
            return
        work = self._controller.quit_pending_work()
        if not work.asks_before_quit:
            self._quit()
            return
        dialog = QuitDialog()
        dialog.wait_requested.connect(self._start_waiting)
        dialog.quit_requested.connect(self._quit)
        dialog.dismissed.connect(self._dismiss)
        dialog.show_question(work)
        self._dialog = dialog
        self._timer.start()
        self._present(dialog)

    @staticmethod
    def _present(dialog: QuitDialog) -> None:
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _start_waiting(self) -> None:
        self._baseline = self._controller.quit_pending_work()
        self._waiting = True
        self._wait_started = time.monotonic()
        self._poll()

    def _poll(self) -> None:
        dialog = self._dialog
        if dialog is None:
            self._timer.stop()
            return
        if not self._waiting:
            dialog.show_question(self._controller.quit_pending_work())
            return
        # Every poll: a capture that finished opening after the last one is
        # stopped too, and a new recording stays refused.
        self._controller.hold_for_quit()
        work = self._controller.quit_pending_work()
        if work.can_wait:
            dialog.show_waiting(work, int(time.monotonic() - self._wait_started))
            return
        message = self._problems_since_wait_started(work)
        if not message:
            self._quit()
            return
        self._waiting = False
        self._timer.stop()
        dialog.show_problems(work, message)

    def _problems_since_wait_started(self, work: PendingQuitWork) -> str:
        not_inserted = work.not_inserted - self._baseline.not_inserted
        failed = work.failed - self._baseline.failed
        parts = []
        if not_inserted > 0:
            parts.append(
                _plural(not_inserted, "transcript", "transcripts")
                + " could not be inserted; "
                + ("it is" if not_inserted == 1 else "they are")
                + " in History."
            )
        if failed > 0:
            parts.append(
                _plural(failed, "transcription", "transcriptions")
                + " failed; "
                + ("its recording is" if failed == 1 else "their recordings are")
                + " offered at the next start."
            )
        return " ".join(["Done waiting.", *parts]) if parts else ""

    def _on_quit_canceled_by_recording(self) -> None:
        """Close the window like "Don't quit": the user is dictating again."""
        dialog = self._dialog
        if dialog is not None:
            # `close` reports the dismissal once, which runs `_dismiss`.
            dialog.close()

    def _dismiss(self) -> None:
        self._timer.stop()
        self._waiting = False
        self._controller.release_quit_hold()
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def _quit(self) -> None:
        self._timer.stop()
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.close_for_quit()
            dialog.deleteLater()
        self._quit_app()
