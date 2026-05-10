from __future__ import annotations

from PySide6 import QtWidgets

from stt_app.transcript_edit_dialog import TranscriptEditDialog


def test_transcript_edit_dialog_hides_error_space_until_needed():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = TranscriptEditDialog("original text")

    assert dialog._error_label.isHidden()

    dialog._editor.setPlainText("   ")
    dialog._accept_if_valid()

    assert not dialog._error_label.isHidden()
    assert dialog._error_label.text() == "Transcript text cannot be empty."

    dialog._editor.setPlainText("corrected text")

    assert dialog._error_label.isHidden()
    assert dialog._error_label.text() == ""
