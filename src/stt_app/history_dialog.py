from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .config import DEFAULT_HISTORY_MAX_ITEMS, HISTORY_MAX_ITEMS_MAX
from .settings_store import SettingsStore
from .transcript_edit_dialog import TranscriptEditDialog
from .transcript_history import (
    TranscriptHistoryEntry,
    TranscriptHistoryStore,
    join_recent_entries_for_clipboard,
)
from .ui_feedback import (
    BUTTON_FEEDBACK_STYLESHEET,
    reserve_button_width_for_texts,
    restore_vertical_scrollbar,
    set_button_feedback_state,
)

_COMPACT_TABLE_ROW_EXTRA_PX = 4
_TABLE_TEXT_PREVIEW_CHARS = 180


class HistoryDialog(QtWidgets.QDialog):
    def __init__(
        self,
        history_store: TranscriptHistoryStore,
        settings_store: SettingsStore,
        on_history_limit_changed: Callable[[int], None] | None = None,
        parent: QtWidgets.QWidget | None = None,
        autoload: bool = True,
    ) -> None:
        super().__init__(parent)
        self._history_store = history_store
        self._settings_store = settings_store
        self._on_history_limit_changed = on_history_limit_changed
        self._entries: list[TranscriptHistoryEntry] = []
        self._last_total_entries = 0
        self._copy_feedback_timer = QtCore.QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(1000)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_feedback)

        settings = self._settings_store.load()
        self._history_limit = _normalize_history_limit(settings.history_max_items)

        self.setWindowTitle("Recent Transcriptions")
        self.resize(1040, 660)
        self.setMinimumSize(700, 460)
        self.setModal(False)
        self.setWindowFlag(QtCore.Qt.Window, True)
        self.setWindowFlag(QtCore.Qt.WindowSystemMenuHint, True)
        self.setWindowFlag(QtCore.Qt.WindowMinimizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowMaximizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowCloseButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)

        self.setStyleSheet(self._scrollbar_stylesheet())

        self._max_items_spin = QtWidgets.QSpinBox()
        self._max_items_spin.setRange(0, HISTORY_MAX_ITEMS_MAX)
        self._max_items_spin.setSpecialValueText("Unlimited (0)")
        self._max_items_spin.setKeyboardTracking(False)
        self._max_items_spin.setToolTip(
            "Maximum number of entries kept in transcript history (0 = unlimited)."
        )
        self._max_items_spin.setValue(self._history_limit)
        self._max_items_spin.valueChanged.connect(self._on_limit_spin_changed)

        self._history_count_label = QtWidgets.QLabel("")
        self._history_count_label.setStyleSheet("color: #555;")

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(6)
        controls.addWidget(QtWidgets.QLabel("Stored history limit"))
        controls.addWidget(self._max_items_spin)
        controls.addStretch(1)
        controls.addWidget(self._history_count_label)

        self._table = QtWidgets.QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Time", "Engine", "Model", "Text"])
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        compact_row_height = max(
            self.fontMetrics().height() + _COMPACT_TABLE_ROW_EXTRA_PX,
            18,
        )
        self._table.verticalHeader().setMinimumSectionSize(compact_row_height)
        self._table.verticalHeader().setDefaultSectionSize(
            compact_row_height
        )
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
        self._detail.setFont(self._table.font())
        self._detail.setMinimumHeight(self.fontMetrics().height() * 4)
        self._detail.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )

        self._splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(self._table)
        self._splitter.addWidget(self._detail)
        self._splitter.setStretchFactor(0, 2)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([460, 220])

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

        self._edit_button = QtWidgets.QPushButton("Edit selected")
        self._edit_button.setEnabled(False)
        self._edit_button.clicked.connect(self._edit_selected)

        self._delete_button = QtWidgets.QPushButton("Delete selected")
        self._delete_button.setEnabled(False)
        self._delete_button.clicked.connect(self._delete_selected)

        self._close_button = QtWidgets.QPushButton("Close")
        self._close_button.clicked.connect(self.close)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addWidget(self._refresh_button)
        buttons.addWidget(self._export_button)
        buttons.addWidget(self._import_button)
        buttons.addWidget(self._clear_button)
        buttons.addStretch(1)
        buttons.addWidget(self._copy_button)
        buttons.addWidget(self._edit_button)
        buttons.addWidget(self._delete_button)
        buttons.addWidget(self._close_button)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addLayout(controls)
        root.addWidget(self._splitter, 1)
        root.addLayout(buttons)
        reserve_button_width_for_texts(
            self._copy_button,
            ("Copy selected", "Copied"),
        )

        if autoload:
            self.reload()

    def reload(self) -> None:
        previous_selected_rows = self._selected_rows()
        previous_selected_entries = self._selected_entries()
        previous_scroll_value = self._table.verticalScrollBar().value()
        self._entries, total = self._history_store.recent_entries_with_count(
            limit=self._history_limit
        )
        restored_selected_rows: list[int] = []
        self._table.setUpdatesEnabled(False)
        self._table.blockSignals(True)
        try:
            self._table.clearContents()
            self._table.setRowCount(len(self._entries))
            for row, entry in enumerate(self._entries):
                self._table.setItem(
                    row, 0, QtWidgets.QTableWidgetItem(_format_time(entry.created_at))
                )
                self._table.setItem(row, 1, QtWidgets.QTableWidgetItem(entry.engine))
                self._table.setItem(row, 2, QtWidgets.QTableWidgetItem(entry.model))
                self._table.setItem(
                    row,
                    3,
                    QtWidgets.QTableWidgetItem(_preview_text(entry.text)),
                )
                if any(entry == selected for selected in previous_selected_entries):
                    restored_selected_rows.append(row)
        finally:
            self._table.blockSignals(False)
            self._table.setUpdatesEnabled(True)

        self._last_total_entries = total
        self._update_history_count_label(total)
        if self._entries:
            fallback_row = previous_selected_rows[0] if previous_selected_rows else 0
            rows_to_select = (
                restored_selected_rows
                if restored_selected_rows
                else [min(fallback_row, len(self._entries) - 1)]
            )
            self._select_rows(rows_to_select)
            restore_vertical_scrollbar(self._table, previous_scroll_value)
            self._on_selection_changed()
        else:
            self._detail.clear()
            self._copy_button.setEnabled(False)
            self._edit_button.setEnabled(False)
            self._delete_button.setEnabled(False)
            self._reset_copy_feedback()

    def _on_selection_changed(self) -> None:
        entries = self._selected_entries()
        if not entries:
            self._detail.clear()
            self._copy_button.setEnabled(False)
            self._edit_button.setEnabled(False)
            self._delete_button.setEnabled(False)
            self._reset_copy_feedback()
            return
        if len(entries) == 1:
            self._detail.setPlainText(entries[0].text)
            self._copy_button.setEnabled(bool(entries[0].text))
            self._edit_button.setEnabled(bool(entries[0].text))
        else:
            self._detail.setPlainText(f"{len(entries)} entries selected.")
            self._copy_button.setEnabled(
                any(bool(entry.text) for entry in entries)
            )
            self._edit_button.setEnabled(False)
        self._delete_button.setEnabled(True)
        self._reset_copy_feedback()

    def _selected_rows(self) -> list[int]:
        selected = self._table.selectionModel().selectedRows()
        rows = sorted({index.row() for index in selected})
        return [row for row in rows if 0 <= row < len(self._entries)]

    def _selected_entries(self) -> list[TranscriptHistoryEntry]:
        return [self._entries[row] for row in self._selected_rows()]

    def _selected_row(self) -> int | None:
        rows = self._selected_rows()
        if len(rows) != 1:
            return None
        return rows[0]

    def _select_rows(self, rows: list[int]) -> None:
        selection_model = self._table.selectionModel()
        if selection_model is None:
            return
        selection_model.clearSelection()
        model = self._table.model()
        for row in rows:
            if row < 0 or row >= self._table.rowCount():
                continue
            top_left = model.index(row, 0)
            bottom_right = model.index(row, self._table.columnCount() - 1)
            selection = QtCore.QItemSelection(top_left, bottom_right)
            selection_model.select(
                selection,
                QtCore.QItemSelectionModel.Select
                | QtCore.QItemSelectionModel.Rows,
            )
        if rows:
            current_row = min(max(rows[0], 0), self._table.rowCount() - 1)
            selection_model.setCurrentIndex(
                model.index(current_row, 0),
                QtCore.QItemSelectionModel.NoUpdate,
            )

    def _copy_selected(self) -> None:
        text = join_recent_entries_for_clipboard(self._selected_entries())
        if not text:
            return
        QtGui.QGuiApplication.clipboard().setText(text)
        self._copy_button.setText("Copied")
        set_button_feedback_state(self._copy_button, "success")
        self._copy_feedback_timer.start()

    def _edit_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        entry = self._entries[row]
        next_text = TranscriptEditDialog.get_text(self, entry.text)
        if next_text is None or next_text == entry.text:
            return
        updated = self._history_store.update_entry_text(entry, next_text)
        if updated <= 0:
            QtWidgets.QMessageBox.information(
                self,
                "Entry not found",
                "The selected history entry could not be updated.",
            )
            return
        self.reload()
        if row < self._table.rowCount():
            self._table.selectRow(row)

    def _reset_copy_feedback(self) -> None:
        self._copy_button.setText("Copy selected")
        set_button_feedback_state(self._copy_button, None)

    @staticmethod
    def _scrollbar_stylesheet() -> str:
        return (
            BUTTON_FEEDBACK_STYLESHEET
            + """
        QScrollBar:vertical {
            width: 12px;
            background: transparent;
            margin: 0;
        }
        QScrollBar::handle:vertical {
            min-height: 36px;
            background: #c4cdd8;
            border-radius: 6px;
        }
        QScrollBar::handle:vertical:hover {
            background: #b0bac6;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0;
        }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
            background: transparent;
        }
        QScrollBar:horizontal {
            height: 12px;
            background: transparent;
            margin: 0;
        }
        QScrollBar::handle:horizontal {
            min-width: 36px;
            background: #c4cdd8;
            border-radius: 6px;
        }
        QScrollBar::handle:horizontal:hover {
            background: #b0bac6;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
            width: 0;
        }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
            background: transparent;
        }
        """
        )

    def _delete_selected(self) -> None:
        entries = self._selected_entries()
        if not entries:
            return
        count = len(entries)
        prompt = (
            "Delete the selected transcription from history?"
            if count == 1
            else f"Delete {count} selected transcriptions from history?"
        )
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete history entry" if count == 1 else "Delete history entries",
            prompt,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        removed = self._history_store.delete_entries(entries)
        if removed <= 0:
            QtWidgets.QMessageBox.information(
                self,
                "Entry not found",
                "The selected history entries could not be removed.",
            )
            return
        self.reload()

    def _on_limit_spin_changed(self, value: int) -> None:
        next_limit = _normalize_history_limit(value)
        if next_limit == self._history_limit:
            return

        current_count = self._history_store.count()
        current_visible = _visible_history_count(current_count, self._history_limit)
        next_visible = _visible_history_count(current_count, next_limit)
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
            current_count = min(current_count, next_limit)
            next_visible = _visible_history_count(current_count, next_limit)

        self._last_total_entries = current_count
        self._update_history_count_label(current_count)
        if next_visible != current_visible:
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
            self._import_dialog_dir(),
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

    def _import_dialog_dir(self) -> str:
        path = self._history_store.path.parent
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            return str(Path.home() / "Documents")
        return str(path)

    def _update_history_count_label(self, total: int | None = None) -> None:
        count = self._last_total_entries if total is None else int(total)
        if self._history_limit == 0:
            self._history_count_label.setText(
                f"Stored: {count} entries (unlimited; showing all)"
            )
            return

        shown = min(count, self._history_limit)
        if count <= self._history_limit:
            self._history_count_label.setText(
                f"Stored: {count} entries (limit {self._history_limit}; "
                "showing all stored entries)"
            )
            return

        self._history_count_label.setText(
            f"Stored: {count} entries (limit {self._history_limit}; "
            f"showing latest {shown})"
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


def _visible_history_count(total: int, limit: int) -> int:
    if limit == 0:
        return max(0, int(total))
    return min(max(0, int(total)), max(0, int(limit)))


def _preview_text(value: str) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= _TABLE_TEXT_PREVIEW_CHARS:
        return text
    return f"{text[:_TABLE_TEXT_PREVIEW_CHARS]}..."


def _format_time(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value
