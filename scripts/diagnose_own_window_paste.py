"""Paste a transcript into each kind of text field the app's own windows have.

WHAT IT SHOWS
A dictation pasted into one of the app's own windows is read by Qt on the Qt
thread. Had the transcript been announced for a read receipt
(`clipboard_owner`), the owner window would answer that read from a Python
callback on another thread, and a field whose key handling runs through a
Python override keeps the interpreter lock at that moment. The backend
therefore writes such a paste plainly (`Win32ClipboardBackend`), and this
script shows per field that the paste lands and how long it took:

  call     the paste call itself, until the keystroke is out
  landed   from the start of the call until the field shows the text
  gap      the longest time the Qt thread did not come round to a 5 ms timer
  receipt  the read receipt's verdict ("unknown" for a plain write)

The fields are a QLineEdit, a QPlainTextEdit, the two spin boxes of the
Settings dialog, and a QLineEdit whose `keyPressEvent` is a Python override
(the app has none today; it stands for any future one). A field that does
not show the text within eight seconds is reported as not landed.

WHAT IT TOUCHES
The clipboard (captured first, put back at the end; the last line says
whether it came back format for format; its content is never printed) and
the foreground: a small window takes the focus for about ten seconds and
receives real Ctrl+V keystrokes. The run waits for 12 s without user input
first (at most 60 s, then it exits with code 75).

COMMAND LINE
    .venv\\Scripts\\python.exe scripts\\diagnose_own_window_paste.py

Needs Windows, pywin32 and PySide6.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The release check owns the sandbox redirect, the idle clock, the foreground
# helper and the content-free clipboard description.
import release_check_clipboard_paste as desktop
from PySide6 import QtCore, QtWidgets

from stt_app.clipboard_owner import ClipboardOwner
from stt_app.settings_dialog_helpers import (
    _WheelPassthroughDoubleSpinBox,
    _WheelPassthroughSpinBox,
)
from stt_app.text_inserter import (
    CLIPBOARD_HISTORY_EXCLUSION_FORMAT_NAMES,
    TextInserter,
    Win32ClipboardBackend,
)

EXIT_USER_ACTIVE = 75
IDLE_WAIT_LIMIT_S = 60.0
LANDING_LIMIT_S = 8.0


class _OverriddenLineEdit(QtWidgets.QLineEdit):
    """A field whose key handling passes through Python."""

    def keyPressEvent(self, event) -> None:
        super().keyPressEvent(event)


def _formats(state) -> list[tuple[object, object]]:
    return [
        (item["id"], item["bytes"])
        for item in desktop.describe_state(state)
        if item["name"] not in CLIPBOARD_HISTORY_EXCLUSION_FORMAT_NAMES
    ]


class Probe(QtCore.QObject):
    def __init__(self, app: QtWidgets.QApplication) -> None:
        super().__init__()
        self._app = app
        self.window = QtWidgets.QWidget()
        self.window.setWindowTitle("stt_app own-window paste probe (closes by itself)")
        layout = QtWidgets.QFormLayout(self.window)
        spin = _WheelPassthroughSpinBox()
        spin.setRange(0, 100_000)
        double_spin = _WheelPassthroughDoubleSpinBox()
        double_spin.setRange(0.0, 100_000.0)
        self._fields = [
            ("QLineEdit", QtWidgets.QLineEdit(), "own window line"),
            ("QPlainTextEdit", QtWidgets.QPlainTextEdit(), "own window plain"),
            ("spin box", spin, "4711"),
            ("double spin box", double_spin, "4712"),
            ("QLineEdit, Python keyPressEvent", _OverriddenLineEdit(), "override"),
        ]
        for label, widget, _text in self._fields:
            layout.addRow(label, widget)
        self.window.resize(520, 320)
        self.inserter = TextInserter(
            backend=Win32ClipboardBackend(owner=ClipboardOwner())
        )
        self._receipts: list[str] = []
        self.inserter.set_paste_receipt_handler(
            lambda _id, verdict, _evidence: self._receipts.append(verdict)
        )
        self.lines: list[str] = []
        self._index = -1
        self._started = 0.0
        self._call_ms = 0.0
        self._gap_ms = 0.0
        self._last_beat = 0.0
        self._beat = QtCore.QTimer(self)
        self._beat.timeout.connect(self._on_beat)
        self._beat.start(5)

    def _on_beat(self) -> None:
        now = time.perf_counter()
        if self._last_beat:
            self._gap_ms = max(self._gap_ms, (now - self._last_beat) * 1000.0)
        self._last_beat = now
        if self._index < 0 or not self._started:
            return
        _label, widget, text = self._fields[self._index]
        waited = now - self._started
        if self._shown(widget) == text or waited > LANDING_LIMIT_S:
            self._finish(waited)

    @staticmethod
    def _shown(widget) -> str:
        if isinstance(widget, QtWidgets.QPlainTextEdit):
            return widget.toPlainText()
        if isinstance(widget, QtWidgets.QAbstractSpinBox):
            return widget.lineEdit().text().split(",")[0].split(".")[0]
        return widget.text()

    def next_field(self) -> None:
        self._index += 1
        if self._index >= len(self._fields):
            self._app.quit()
            return
        _label, widget, text = self._fields[self._index]
        hwnd = int(self.window.winId())
        if not desktop.take_foreground(hwnd):
            self.lines.append("Windows refused the foreground; nothing was pasted")
            self._app.quit()
            return
        widget.setFocus()
        if isinstance(widget, QtWidgets.QAbstractSpinBox):
            widget.selectAll()
        self._app.processEvents()
        self._receipts.clear()
        self._gap_ms = 0.0
        self._last_beat = 0.0
        started = time.perf_counter()
        try:
            self.inserter.insert_text_with_options(text, target_hwnd=hwnd)
        except Exception as exc:
            self.lines.append(
                f"{_label:<32} refused: {type(exc).__name__}: "
                f"{desktop.common.ascii_safe(exc)}"
            )
            QtCore.QTimer.singleShot(100, self.next_field)
            return
        self._call_ms = (time.perf_counter() - started) * 1000.0
        self._started = started

    def _finish(self, waited: float) -> None:
        label, widget, text = self._fields[self._index]
        landed = self._shown(widget) == text
        self._started = 0.0
        self.inserter.flush_pending_restore()
        receipt = self._receipts[-1] if self._receipts else "none"
        self.lines.append(
            f"{label:<32} landed={landed!s:<5} call {self._call_ms:6.1f} ms  "
            f"landed {waited * 1000.0:7.1f} ms  gap {self._gap_ms:7.1f} ms  "
            f"receipt {receipt}"
        )
        QtCore.QTimer.singleShot(150, self.next_field)


def main() -> int:
    waited = 0.0
    while desktop.idle_seconds() < desktop.IDLE_REQUIRED_S:
        if waited >= IDLE_WAIT_LIMIT_S:
            sys.stdout.write("the user never paused long enough; nothing was touched\n")
            return EXIT_USER_ACTIVE
        time.sleep(1.0)
        waited += 1.0

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    saver = Win32ClipboardBackend()
    user_state = saver.capture_clipboard_state()
    previous_foreground = int(desktop.user32.GetForegroundWindow() or 0)
    probe = Probe(app)
    try:
        probe.window.show()
        QtCore.QTimer.singleShot(500, probe.next_field)
        QtCore.QTimer.singleShot(60_000, app.quit)
        app.exec()
    finally:
        probe.inserter.close()
        probe.window.close()
        saver.restore_clipboard_state(user_state)
        back = _formats(saver.capture_clipboard_state()) == _formats(user_state)
        if previous_foreground and desktop.user32.IsWindow(previous_foreground):
            desktop.take_foreground(previous_foreground)
    for line in probe.lines:
        sys.stdout.write(line + "\n")
    sys.stdout.write(f"user clipboard back as it was: {back}\n")
    return 0 if back else 1


if __name__ == "__main__":
    raise SystemExit(main())
