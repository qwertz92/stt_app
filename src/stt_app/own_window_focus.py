"""Whether a window of this app would take a pasted transcript.

A paste keystroke goes to whatever has the keyboard focus, and no Windows
call says whether that element took the text. For this app's own windows no
guess is needed: Qt knows which widget the window gives its focus to. The
Settings dialog is a valid dictation target for its text fields, and only
for them -- a transcript "pasted" while its focus sat on a table, a button
or a read-only view reached nothing and was reported as inserted (the
owner's log, 2026-11-24: a recording started with Settings in front, target
class `Qt6120QWindowIcon`, the same text pasted into another window by hand
twelve seconds later). A focused hotkey field is worse than nothing: it
would record Ctrl+V as the new hotkey.
"""

from __future__ import annotations

from PySide6 import QtWidgets

_TEXT_WIDGETS = (
    QtWidgets.QLineEdit,
    QtWidgets.QTextEdit,
    QtWidgets.QPlainTextEdit,
    QtWidgets.QAbstractSpinBox,
)


def takes_pasted_text(widget: QtWidgets.QWidget | None) -> bool:
    """Whether Ctrl+V with the focus on `widget` inserts clipboard text."""
    if widget is None or not widget.isEnabled():
        return False
    if isinstance(widget, QtWidgets.QKeySequenceEdit) or isinstance(
        widget.parentWidget(), QtWidgets.QKeySequenceEdit
    ):
        # Its inner line edit holds the focus and takes keystrokes as the
        # shortcut being recorded, never as text.
        return False
    if isinstance(widget, QtWidgets.QComboBox):
        return widget.isEditable()
    if isinstance(widget, _TEXT_WIDGETS):
        return not widget.isReadOnly()
    return False


def own_window_takes_pasted_text(hwnd: int | None) -> bool | None:
    """The answer for a window of this app; None for anyone else's window.

    Asked of the window's own focus widget (`QWidget.focusWidget`: the
    widget that has, or will get, the focus when the window is active), so
    the answer does not depend on Qt having processed an activation that
    Windows has already made.
    """
    if not hwnd:
        return None
    try:
        widget = QtWidgets.QWidget.find(int(hwnd))
    except (OverflowError, RuntimeError, TypeError, ValueError):
        return None
    if widget is None:
        return None
    return takes_pasted_text(widget.window().focusWidget())
