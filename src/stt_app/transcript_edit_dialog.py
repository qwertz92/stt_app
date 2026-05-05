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

        self._editor = QtWidgets.QPlainTextEdit()
        self._editor.setPlainText(text)

        self._error_label = QtWidgets.QLabel("")
        self._error_label.setStyleSheet("color: #b71c1c;")

        self._save_button = QtWidgets.QPushButton("Save Transcript")
        self._save_button.clicked.connect(self._accept_if_valid)
        cancel_button = QtWidgets.QPushButton("Cancel")
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
