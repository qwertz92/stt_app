from __future__ import annotations

from PySide6 import QtWidgets

from stt_app.transcript_edit_dialog import TranscriptEditDialog


def test_transcript_edit_dialog_reserves_error_space_instead_of_shifting_buttons():
    """The validation error must not push the Save/Cancel row down a line.

    The label used to be hidden until needed, which reflowed the buttons the
    moment an empty transcript was rejected.
    """
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = TranscriptEditDialog("original text")

    reserved_height = dialog._error_label.minimumHeight()
    assert reserved_height > 0
    assert dialog._error_label.maximumHeight() == reserved_height
    assert dialog._error_label.text() == ""

    dialog._editor.setPlainText("   ")
    dialog._accept_if_valid()

    assert dialog._error_label.text() == "Transcript text cannot be empty."
    assert dialog._error_label.minimumHeight() == reserved_height
    assert dialog._error_label.maximumHeight() == reserved_height

    dialog._editor.setPlainText("corrected text")

    assert dialog._error_label.text() == ""
    assert dialog._error_label.minimumHeight() == reserved_height
