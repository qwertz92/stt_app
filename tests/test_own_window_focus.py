"""Which of this app's own widgets take a pasted transcript.

The Settings dialog is a valid dictation target for its text fields only: a
transcript pasted while its focus sat on a table was reported as inserted and
reached nothing (the owner's log, 2026-11-24).
"""

from __future__ import annotations

import pytest
from PySide6 import QtWidgets

from stt_app.own_window_focus import own_window_takes_pasted_text, takes_pasted_text


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _read_only(widget):
    widget.setReadOnly(True)
    return widget


def _disabled(widget):
    widget.setEnabled(False)
    return widget


def _editable_combo():
    combo = QtWidgets.QComboBox()
    combo.setEditable(True)
    return combo


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (QtWidgets.QLineEdit, True),
        (QtWidgets.QPlainTextEdit, True),
        (QtWidgets.QTextEdit, True),
        (QtWidgets.QSpinBox, True),
        (_editable_combo, True),
        (lambda: _read_only(QtWidgets.QLineEdit()), False),
        (lambda: _read_only(QtWidgets.QPlainTextEdit()), False),
        (lambda: _disabled(QtWidgets.QLineEdit()), False),
        (QtWidgets.QTextBrowser, False),
        (QtWidgets.QComboBox, False),
        (QtWidgets.QPushButton, False),
        (QtWidgets.QTableWidget, False),
        (QtWidgets.QKeySequenceEdit, False),
    ],
)
def test_only_an_editable_text_widget_takes_pasted_text(app, build, expected):
    widget = build()
    try:
        assert takes_pasted_text(widget) is expected
    finally:
        widget.deleteLater()


def test_the_line_edit_inside_a_hotkey_field_records_keys_and_takes_no_text(app):
    """The focus of a `QKeySequenceEdit` sits on its inner line edit, which
    would record Ctrl+V as the new hotkey."""
    field = QtWidgets.QKeySequenceEdit()
    try:
        inner = field.findChild(QtWidgets.QLineEdit)
        assert inner is not None, "Qt no longer builds the field around a line edit"
        assert takes_pasted_text(inner) is False
    finally:
        field.deleteLater()


def test_no_focus_widget_takes_nothing(app):
    assert takes_pasted_text(None) is False


def _dialog_with(*widgets):
    dialog = QtWidgets.QDialog()
    layout = QtWidgets.QVBoxLayout(dialog)
    for widget in widgets:
        layout.addWidget(widget)
    return dialog


def test_an_own_window_answers_for_the_widget_it_gives_its_focus_to(app):
    line = QtWidgets.QLineEdit()
    table = QtWidgets.QTableWidget(2, 2)
    dialog = _dialog_with(line, table)
    try:
        # The native handle exists without showing the window, and
        # `focusWidget` is the window's own record: neither needs the
        # foreground.
        hwnd = int(dialog.winId())
        table.setFocus()
        assert own_window_takes_pasted_text(hwnd) is False
        line.setFocus()
        assert own_window_takes_pasted_text(hwnd) is True
    finally:
        dialog.deleteLater()


def test_an_own_window_without_any_focus_widget_takes_nothing(app):
    dialog = _dialog_with(QtWidgets.QLabel("nothing to focus"))
    try:
        assert own_window_takes_pasted_text(int(dialog.winId())) is False
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("hwnd", [None, 0, 987, 2**40])
def test_a_window_of_another_program_has_no_answer(app, hwnd):
    assert own_window_takes_pasted_text(hwnd) is None
