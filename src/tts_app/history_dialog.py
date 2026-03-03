from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .config import DEFAULT_HISTORY_MAX_ITEMS, HISTORY_MAX_ITEMS_MAX
from .settings_store import SettingsStore
from .transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore


class HistoryDialog(QtWidgets.QDialog):
    def __init__(
        self,
        history_store: TranscriptHistoryStore,
        settings_store: SettingsStore,
        on_history_limit_changed: Callable[[int], None] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._history_store = history_store
        self._settings_store = settings_store
        self._on_history_limit_changed = on_history_limit_changed
        self._entries: list[TranscriptHistoryEntry] = []
        self._copy_feedback_timer = QtCore.QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(1000)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_feedback)

        settings = self._settings_store.load()
        self._history_limit = _normalize_history_limit(settings.history_max_items)

        self.setWindowTitle("Recent Transcriptions")
        self.resize(820, 500)
        self.setModal(False)

        self._max_items_spin = QtWidgets.QSpinBox()
        self._max_items_spin.setRange(0, HISTORY_MAX_ITEMS_MAX)
        self._max_items_spin.setSpecialValueText("Unlimited (0)")
        self._max_items_spin.setToolTip(
            "Maximum number of entries kept in transcript history (0 = unlimited)."
        )
        self._max_items_spin.setValue(self._history_limit)
        self._max_items_spin.valueChanged.connect(self._on_limit_spin_changed)

        self._history_count_label = QtWidgets.QLabel("")
        self._history_count_label.setStyleSheet("color: #555;")

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Stored history limit"))
        controls.addWidget(self._max_items_spin)
        controls.addStretch(1)
        controls.addWidget(self._history_count_label)

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

        self._refresh_button = QtWidgets.QPushButton("Refresh")
        self._refresh_button.clicked.connect(self.reload)

        self._export_button = QtWidgets.QPushButton("Export...")
        self._export_button.clicked.connect(self._export_history)

        self._import_button = QtWidgets.QPushButton("Import...")
        self._import_button.clicked.connect(self._import_history)

        self._clear_button = QtWidgets.QPushButton("Clear history")
        self._clear_button.clicked.connect(self._clear_history)

        self._copy_button = QtWidgets.QPushButton("Copy selected")
        self._copy_button.setEnabled(False)
        self._copy_button.clicked.connect(self._copy_selected)

        self._close_button = QtWidgets.QPushButton("Close")
        self._close_button.clicked.connect(self.close)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self._refresh_button)
        buttons.addWidget(self._export_button)
        buttons.addWidget(self._import_button)
        buttons.addWidget(self._clear_button)
        buttons.addStretch(1)
        buttons.addWidget(self._copy_button)
        buttons.addWidget(self._close_button)

        root = QtWidgets.QVBoxLayout(self)
        root.addLayout(controls)
        root.addWidget(self._table, 2)
        root.addWidget(self._detail, 1)
        root.addLayout(buttons)

        self.reload()

    def reload(self) -> None:
        self._entries = self._history_store.recent_entries(limit=self._history_limit)
        self._table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            self._table.setItem(
                row, 0, QtWidgets.QTableWidgetItem(_format_time(entry.created_at))
            )
            self._table.setItem(row, 1, QtWidgets.QTableWidgetItem(entry.engine))
            self._table.setItem(row, 2, QtWidgets.QTableWidgetItem(entry.model))
            self._table.setItem(row, 3, QtWidgets.QTableWidgetItem(entry.text))
        total = self._history_store.count()
        if self._history_limit == 0:
            self._history_count_label.setText(f"Stored: {total} entries (showing all)")
        else:
            shown = min(total, self._history_limit)
            self._history_count_label.setText(
                f"Stored: {total} entries (showing latest {shown})"
            )
        if self._entries:
            self._table.selectRow(0)
        else:
            self._detail.clear()
            self._copy_button.setEnabled(False)
            self._reset_copy_feedback()

    def _on_selection_changed(self) -> None:
        row = self._selected_row()
        if row is None:
            self._detail.clear()
            self._copy_button.setEnabled(False)
            self._reset_copy_feedback()
            return
        entry = self._entries[row]
        self._detail.setPlainText(entry.text)
        self._copy_button.setEnabled(True)
        self._reset_copy_feedback()

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
        self._copy_button.setText("Copied")
        self._copy_button.setStyleSheet(
            "background-color: #dff5e0; border: 1px solid #89c88f;"
        )
        self._copy_feedback_timer.start()

    def _reset_copy_feedback(self) -> None:
        self._copy_button.setText("Copy selected")
        self._copy_button.setStyleSheet("")

    def _on_limit_spin_changed(self, value: int) -> None:
        next_limit = _normalize_history_limit(value)
        if next_limit == self._history_limit:
            return

        current_count = self._history_store.count()
        if next_limit > 0 and current_count > next_limit:
            to_delete = current_count - next_limit
            answer = QtWidgets.QMessageBox.question(
                self,
                "Reduce history size",
                (
                    f"Reducing the history limit to {next_limit} will delete "
                    f"{to_delete} oldest entr{'y' if to_delete == 1 else 'ies'}.\n\n"
                    "Do you want to continue?"
                ),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                blocker = QtCore.QSignalBlocker(self._max_items_spin)
                self._max_items_spin.setValue(self._history_limit)
                del blocker
                return

        if not self._persist_limit(next_limit):
            blocker = QtCore.QSignalBlocker(self._max_items_spin)
            self._max_items_spin.setValue(self._history_limit)
            del blocker
            return

        if next_limit > 0:
            self._history_store.apply_max_items(next_limit)
        self.reload()

    def _persist_limit(self, limit: int) -> bool:
        settings = self._settings_store.load()
        updated = replace(settings, history_max_items=limit)
        try:
            self._settings_store.save(updated)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "Save failed",
                f"Failed to persist history setting: {exc}",
            )
            return False
        self._history_limit = limit
        if callable(self._on_history_limit_changed):
            self._on_history_limit_changed(limit)
        return True

    def _export_history(self) -> None:
        suggested = (
            Path.home()
            / "Documents"
            / f"dictation_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export transcript history",
            str(suggested),
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return

        try:
            count = self._history_store.export_to_file(Path(path))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "Export failed",
                f"Failed to export transcript history: {exc}",
            )
            return

        QtWidgets.QMessageBox.information(
            self,
            "Export complete",
            f"Exported {count} entr{'y' if count == 1 else 'ies'} to:\n{path}",
        )

    def _import_history(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Import transcript history",
            "",
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return

        try:
            imported_entries = self._history_store.import_from_file(Path(path))
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Import failed", str(exc))
            return

        if not imported_entries:
            QtWidgets.QMessageBox.information(
                self,
                "No entries found",
                "The selected file does not contain importable transcript entries.",
            )
            return

        active_limit = self._history_limit
        current_count = self._history_store.count()
        entries_to_append = imported_entries

        if active_limit > 0:
            free_slots = max(0, active_limit - current_count)
            if len(imported_entries) > free_slots:
                decision = self._prompt_import_overflow(
                    import_count=len(imported_entries),
                    free_slots=free_slots,
                    max_items=active_limit,
                )
                if decision == "cancel":
                    return
                if decision == "free":
                    if free_slots <= 0:
                        QtWidgets.QMessageBox.information(
                            self,
                            "No free slots",
                            "History is already full. Increase the limit or use unlimited mode.",
                        )
                        return
                    entries_to_append = imported_entries[:free_slots]
                else:
                    if not self._persist_limit(0):
                        return
                    active_limit = 0
                    blocker = QtCore.QSignalBlocker(self._max_items_spin)
                    self._max_items_spin.setValue(0)
                    del blocker

        imported_count = self._history_store.append_entries(
            entries_to_append,
            max_items=active_limit,
        )
        self.reload()
        QtWidgets.QMessageBox.information(
            self,
            "Import complete",
            f"Imported {imported_count} entr{'y' if imported_count == 1 else 'ies'}.",
        )

    def _prompt_import_overflow(
        self,
        *,
        import_count: int,
        free_slots: int,
        max_items: int,
    ) -> str:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Import exceeds history size")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(
            (
                f"Import contains {import_count} entries, but only {free_slots} "
                f"slot{'s' if free_slots != 1 else ''} are free "
                f"(current max: {max_items})."
            )
        )
        box.setInformativeText(
            "Choose whether to import only free slots or switch to unlimited storage."
        )
        free_button = box.addButton(
            f"Import only {free_slots}",
            QtWidgets.QMessageBox.AcceptRole,
        )
        unlimited_button = box.addButton(
            "Import all and set unlimited",
            QtWidgets.QMessageBox.DestructiveRole,
        )
        cancel_button = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(free_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked == free_button:
            return "free"
        if clicked == unlimited_button:
            return "unlimited"
        if clicked == cancel_button:
            return "cancel"
        return "cancel"

    def _clear_history(self) -> None:
        count = self._history_store.count()
        if count <= 0:
            QtWidgets.QMessageBox.information(
                self,
                "History is empty",
                "There are no history entries to clear.",
            )
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Clear history",
            (
                f"This will permanently delete {count} "
                f"entr{'y' if count == 1 else 'ies'}.\n\nContinue?"
            ),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        self._history_store.clear()
        self.reload()


def _normalize_history_limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_MAX_ITEMS
    if limit < 0:
        return 0
    return min(limit, HISTORY_MAX_ITEMS_MAX)


def _format_time(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value
