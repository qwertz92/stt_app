from __future__ import annotations

from datetime import datetime

from PySide6 import QtCore, QtGui, QtWidgets

from .transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore


class HistoryDialog(QtWidgets.QDialog):
    def __init__(
        self,
        history_store: TranscriptHistoryStore,
        max_items: int,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._history_store = history_store
        self._max_items = max(1, int(max_items or 1))

        self.setWindowTitle("Recent Transcriptions")
        self.resize(760, 420)
        self.setModal(False)

        self._table = QtWidgets.QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Time", "Engine", "Model", "Text"])
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeToContents
        )
        self._table.itemSelectionChanged.connect(self._on_selection_changed)

        self._detail = QtWidgets.QPlainTextEdit()
        self._detail.setReadOnly(True)

        self._copy_button = QtWidgets.QPushButton("Copy selected")
        self._copy_button.setEnabled(False)
        self._copy_button.clicked.connect(self._copy_selected)

        self._refresh_button = QtWidgets.QPushButton("Refresh")
        self._refresh_button.clicked.connect(self.reload)

        self._close_button = QtWidgets.QPushButton("Close")
        self._close_button.clicked.connect(self.close)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self._refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self._copy_button)
        buttons.addWidget(self._close_button)

        root = QtWidgets.QVBoxLayout(self)
        root.addWidget(self._table, 2)
        root.addWidget(self._detail, 1)
        root.addLayout(buttons)

        self._entries: list[TranscriptHistoryEntry] = []
        self.reload()

    def reload(self) -> None:
        self._entries = self._history_store.recent_entries(limit=self._max_items)
        self._table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            self._table.setItem(row, 0, QtWidgets.QTableWidgetItem(_format_time(entry.created_at)))
            self._table.setItem(row, 1, QtWidgets.QTableWidgetItem(entry.engine))
            self._table.setItem(row, 2, QtWidgets.QTableWidgetItem(entry.model))
            self._table.setItem(row, 3, QtWidgets.QTableWidgetItem(entry.text))
        if self._entries:
            self._table.selectRow(0)
        else:
            self._detail.clear()
            self._copy_button.setEnabled(False)

    def _on_selection_changed(self) -> None:
        row = self._selected_row()
        if row is None:
            self._detail.clear()
            self._copy_button.setEnabled(False)
            return
        entry = self._entries[row]
        self._detail.setPlainText(entry.text)
        self._copy_button.setEnabled(True)

    def _selected_row(self) -> int | None:
        selected = self._table.selectionModel().selectedRows()
        if not selected:
            return None
        row = selected[0].row()
        if row < 0 or row >= len(self._entries):
            return None
        return row

    def _copy_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        text = self._entries[row].text
        if not text:
            return
        QtGui.QGuiApplication.clipboard().setText(text)


def _format_time(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value
