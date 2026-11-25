from __future__ import annotations

from stt_app.dialog_style import DIALOG_STYLESHEET


def test_a_disabled_primary_button_is_styled_after_the_primary_rule() -> None:
    """Qt applies the later of two equally specific rules.

    `QPushButton[primary="true"]` has a higher specificity than
    `QPushButton:disabled`, so a disabled primary button stayed blue until it
    got a rule of its own, placed after the primary one.
    """
    primary = DIALOG_STYLESHEET.index('QPushButton[primary="true"] {')
    disabled = DIALOG_STYLESHEET.index('QPushButton[primary="true"]:disabled {')
    assert disabled > primary
    block = DIALOG_STYLESHEET[disabled:].split("}", 1)[0]
    assert "color: #6b7280" in block
    assert "background-color: #e5e7eb" in block


def test_a_disabled_primary_button_is_painted_grey() -> None:
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = QtWidgets.QDialog()
    dialog.setStyleSheet(DIALOG_STYLESHEET)
    button = QtWidgets.QPushButton("Wait and insert", dialog)
    button.setProperty("primary", True)
    button.setEnabled(False)
    dialog.resize(220, 60)
    button.setGeometry(10, 10, 200, 40)
    dialog.show()
    app.processEvents()
    image = button.grab().toImage()
    colour = image.pixelColor(image.width() // 2, 6).name()
    dialog.close()
    assert colour == "#e5e7eb"
