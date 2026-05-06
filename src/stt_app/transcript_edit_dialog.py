from __future__ import annotations

from PySide6 import QtCore, QtWidgets


class TranscriptEditDialog(QtWidgets.QDialog):
    def __init__(
        self,
        text: str,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Transcript")
        self.resize(720, 420)
        self.setMinimumSize(520, 320)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)
        self.setStyleSheet(_DIALOG_STYLESHEET)

        self._editor = QtWidgets.QPlainTextEdit()
        self._editor.setPlainText(text)

        self._error_label = QtWidgets.QLabel("")
        self._error_label.setStyleSheet("color: #b71c1c;")

        self._save_button = QtWidgets.QPushButton("Save Transcript")
        self._save_button.setObjectName("primaryButton")
        self._save_button.setDefault(True)
        self._save_button.setMinimumWidth(132)
        self._save_button.clicked.connect(self._accept_if_valid)
        cancel_button = QtWidgets.QPushButton("Cancel")
        cancel_button.setObjectName("secondaryButton")
        cancel_button.setMinimumWidth(92)
        cancel_button.clicked.connect(self.reject)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self._save_button)
        buttons.addWidget(cancel_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addWidget(self._editor, 1)
        layout.addWidget(self._error_label)
        layout.addLayout(buttons)

    @property
    def text(self) -> str:
        return self._editor.toPlainText().strip()

    def _accept_if_valid(self) -> None:
        if not self.text:
            self._error_label.setText("Transcript text cannot be empty.")
            return
        self.accept()

    @staticmethod
    def get_text(
        parent: QtWidgets.QWidget | None,
        text: str,
    ) -> str | None:
        dialog = TranscriptEditDialog(text, parent)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.text


_DIALOG_STYLESHEET = """
QDialog {
    background: #f6f8fb;
}
QPlainTextEdit {
    background: #ffffff;
    color: #172033;
    border: 1px solid #b8c2d2;
    border-radius: 6px;
    padding: 8px;
    selection-background-color: #1a73e8;
    selection-color: #ffffff;
}
QPlainTextEdit:focus {
    border: 1px solid #1a73e8;
}
QLabel {
    color: #b71c1c;
}
QPushButton {
    min-height: 30px;
    padding: 0 14px;
    border-radius: 6px;
    font-weight: 600;
}
QPushButton#primaryButton {
    color: #ffffff;
    background: #1a73e8;
    border: 1px solid #1558b0;
}
QPushButton#primaryButton:hover {
    color: #ffffff;
    background: #1558b0;
    border-color: #124a94;
}
QPushButton#primaryButton:pressed {
    color: #ffffff;
    background: #124a94;
}
QPushButton#secondaryButton {
    color: #1f2937;
    background: #ffffff;
    border: 1px solid #b8c2d2;
}
QPushButton#secondaryButton:hover {
    color: #111827;
    background: #e8edf5;
    border-color: #9aa8ba;
}
QPushButton#secondaryButton:pressed {
    color: #111827;
    background: #dce3ed;
}
"""
