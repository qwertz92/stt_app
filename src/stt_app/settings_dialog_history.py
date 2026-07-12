"""Settings dialog: history mixin (split from settings_dialog.py)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .config import (
    DEFAULT_DISPLAY_TIMEZONE,
    DEFAULT_HISTORY_MAX_ITEMS,
    HISTORY_MAX_ITEMS_MAX,
)
from .history_ui_actions import (
    format_history_count_label,
    history_import_dialog_dir,
    prompt_import_overflow,
    run_history_clear,
    run_history_export,
    run_history_import,
)
from .settings_dialog_helpers import _WheelPassthroughSpinBox
from .transcript_edit_dialog import TranscriptEditDialog
from .transcript_history import (
    HistoryStorageSignature,
    TranscriptHistoryEntry,
    format_history_timestamp,
    join_recent_entries_for_clipboard,
    map_recent_entry_rows,
    recent_entries_change_plan,
)
from .ui_feedback import restore_vertical_scrollbar, set_button_feedback_state


class _HistoryTabMixin:
    def _build_history_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        self._history_tab = tab
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        history_intro = QtWidgets.QLabel(
            "Browse recent transcripts here. Audio-file imports live on their own tab so "
            "this view stays compact on smaller screens."
        )
        history_intro.setWordWrap(True)
        self._style_note_label(history_intro)
        layout.addWidget(history_intro)

        self.history_max_spin = _WheelPassthroughSpinBox()
        self.history_max_spin.setRange(0, HISTORY_MAX_ITEMS_MAX)
        self.history_max_spin.setSpecialValueText("Unlimited (0)")
        self.history_max_spin.setKeyboardTracking(False)
        self.history_max_spin.setValue(DEFAULT_HISTORY_MAX_ITEMS)
        self.history_max_spin.setToolTip(
            "Maximum transcript history items stored (0 = unlimited)."
        )
        self.history_max_spin.valueChanged.connect(
            lambda _value: self._refresh_history_list()
        )
        self.history_count_label = QtWidgets.QLabel("")
        self.history_count_label.setStyleSheet("color: #555;")

        history_controls = QtWidgets.QHBoxLayout()
        self._configure_button_row(history_controls)
        history_controls.addWidget(QtWidgets.QLabel("History Size"))
        history_controls.addWidget(self.history_max_spin)
        history_controls.addStretch(1)
        history_controls.addWidget(self.history_count_label)
        layout.addLayout(history_controls)

        history_box = QtWidgets.QGroupBox("Transcript History")
        history_box.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        history_layout = QtWidgets.QVBoxLayout(history_box)
        history_layout.setContentsMargins(10, 10, 10, 10)
        history_layout.setSpacing(6)

        self.history_list = QtWidgets.QListWidget()
        history_font = QtGui.QFont(self.font())
        self.history_list.setFont(history_font)
        self.history_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection
        )
        self._configure_compact_list_widget(self.history_list, expand=True)
        self.history_list.itemSelectionChanged.connect(self._on_history_item_selected)
        self.history_list.itemDoubleClicked.connect(
            self._on_history_item_double_clicked
        )

        self.history_detail = QtWidgets.QPlainTextEdit()
        self.history_detail.setReadOnly(True)
        self.history_detail.setFont(history_font)
        self.history_detail.setMinimumHeight(self.fontMetrics().height() * 4)
        self.history_detail.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )

        self.history_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.history_splitter.setChildrenCollapsible(False)
        self.history_splitter.addWidget(self.history_list)
        self.history_splitter.addWidget(self.history_detail)
        self.history_splitter.setStretchFactor(0, 2)
        self.history_splitter.setStretchFactor(1, 1)
        self.history_splitter.setSizes([400, 200])
        history_layout.addWidget(self.history_splitter, 1)

        history_management_buttons = QtWidgets.QHBoxLayout()
        self._configure_button_row(history_management_buttons)
        self.history_refresh_button = QtWidgets.QPushButton("Refresh")
        self.history_refresh_button.clicked.connect(self._refresh_history_list)
        self.history_export_button = QtWidgets.QPushButton("Export...")
        self.history_export_button.clicked.connect(self._export_history)
        self.history_import_button = QtWidgets.QPushButton("Import...")
        self.history_import_button.clicked.connect(self._import_history)
        self.history_clear_button = QtWidgets.QPushButton("Clear history")
        self.history_clear_button.clicked.connect(self._clear_history)
        self.history_copy_button = QtWidgets.QPushButton("Copy selected")
        self.history_copy_button.clicked.connect(self._copy_selected_history)
        self.history_copy_button.setEnabled(False)
        self.history_edit_button = QtWidgets.QPushButton("Edit selected")
        self.history_edit_button.clicked.connect(self._edit_selected_history)
        self.history_edit_button.setEnabled(False)
        self.history_retranscribe_button = QtWidgets.QPushButton("Retranscribe...")
        self.history_retranscribe_button.setToolTip(
            "Open this entry's retained audio on Import Audio, where you can "
            "choose a different engine or model before transcribing it again."
        )
        self.history_retranscribe_button.clicked.connect(
            self._prepare_selected_history_retranscription
        )
        self.history_retranscribe_button.setEnabled(False)
        self.history_show_audio_button = QtWidgets.QPushButton("Show audio file")
        self.history_show_audio_button.setToolTip(
            "Open File Explorer and select the retained audio for this entry."
        )
        self.history_show_audio_button.clicked.connect(
            self._show_selected_history_audio_file
        )
        self.history_show_audio_button.setEnabled(False)
        self.history_delete_button = QtWidgets.QPushButton("Delete selected")
        self.history_delete_button.clicked.connect(self._delete_selected_history)
        self.history_delete_button.setEnabled(False)
        history_management_buttons.addWidget(self.history_refresh_button)
        history_management_buttons.addWidget(self.history_export_button)
        history_management_buttons.addWidget(self.history_import_button)
        history_management_buttons.addWidget(self.history_clear_button)
        history_management_buttons.addStretch(1)
        history_layout.addLayout(history_management_buttons)

        history_entry_buttons = QtWidgets.QHBoxLayout()
        self._configure_button_row(history_entry_buttons)
        history_entry_buttons.addWidget(self.history_copy_button)
        history_entry_buttons.addWidget(self.history_edit_button)
        history_entry_buttons.addWidget(self.history_retranscribe_button)
        history_entry_buttons.addWidget(self.history_show_audio_button)
        history_entry_buttons.addWidget(self.history_delete_button)
        history_entry_buttons.addStretch(1)
        history_layout.addLayout(history_entry_buttons)

        self.history_status_label = QtWidgets.QLabel("")
        self.history_status_label.setWordWrap(True)
        self.history_status_label.setStyleSheet("color: #555;")
        self.history_status_label.setVisible(False)
        history_layout.addWidget(self.history_status_label)

        layout.addWidget(history_box, 1)
        self._history_tab_index = self.tabs.addTab(tab, "History")

    def _set_history_status(self, message: str, *, error: bool = False) -> None:
        text = str(message or "").strip()
        self.history_status_label.setText(text)
        self.history_status_label.setStyleSheet(
            "color: #b71c1c;" if error else "color: #555;"
        )
        self.history_status_label.setVisible(bool(text))

    def _update_history_count_label(self, total: int) -> None:
        self.history_count_label.setText(
            format_history_count_label(total, self.history_max_spin.value())
        )

    def _export_history(self) -> None:
        def _on_exported(count: int, path: str) -> None:
            self._set_history_status(
                f"Exported {count} entr{'y' if count == 1 else 'ies'} to {path}"
            )

        run_history_export(self, self._history_store, on_exported=_on_exported)

    def _import_history(self) -> None:
        run_history_import(
            self,
            self._history_store,
            dialog_dir=history_import_dialog_dir(self._history_store),
            current_limit=int(self.history_max_spin.value()),
            prompt_overflow=self._prompt_history_import_overflow,
            persist_limit=self._persist_history_limit_now,
            set_limit_widget=self._set_history_max_spin_value,
            on_imported=self._on_history_imported,
        )

    def _prompt_history_import_overflow(
        self,
        *,
        import_count: int,
        free_slots: int,
        max_items: int,
    ) -> str:
        return prompt_import_overflow(
            self,
            import_count=import_count,
            free_slots=free_slots,
            max_items=max_items,
        )

    def _persist_history_limit_now(self, limit: int) -> bool:
        updated = replace(self._loaded_settings, history_max_items=limit)
        try:
            self._settings_store.save(updated)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "Save failed",
                f"Failed to persist history setting: {exc}",
            )
            return False
        self._loaded_settings = updated
        return True

    def _set_history_max_spin_value(self, value: int) -> None:
        blocker = QtCore.QSignalBlocker(self.history_max_spin)
        self.history_max_spin.setValue(value)
        del blocker

    def _on_history_imported(self, imported_count: int, _active_limit: int) -> None:
        self._set_history_status(
            f"Imported {imported_count} entr{'y' if imported_count == 1 else 'ies'}."
        )
        self._refresh_history_list(force=True)

    def _clear_history(self) -> None:
        def _on_cleared() -> None:
            self._set_history_status("History cleared.")
            self._refresh_history_list(force=True)

        run_history_clear(self, self._history_store, on_cleared=_on_cleared)

    def _refresh_history_list(self, force: bool = False) -> None:
        signature = self._current_history_reload_signature()
        if (
            not force
            and signature is not None
            and signature == self._history_reload_signature
        ):
            return
        selected_rows = [
            row
            for row in (
                self.history_list.row(item)
                for item in self.history_list.selectedItems()
            )
            if row >= 0
        ]
        current_item = self.history_list.currentItem()
        current_row = (
            self.history_list.row(current_item) if current_item is not None else None
        )
        scroll_value = self.history_list.verticalScrollBar().value()
        entries, total = self._history_store.recent_entries_with_count(
            self.history_max_spin.value()
        )
        if self._apply_reconciled_history_refresh(
            entries=entries,
            total=total,
            selected_rows=selected_rows,
            current_row=current_row,
            scroll_value=scroll_value,
        ):
            self._history_reload_signature = signature
            return

        self.history_list.setUpdatesEnabled(False)
        self.history_list.blockSignals(True)
        try:
            self.history_list.clear()
            for entry in entries:
                self.history_list.addItem(self._history_list_item(entry))
        finally:
            self.history_list.blockSignals(False)
            self.history_list.setUpdatesEnabled(True)

        self._history_entries = entries
        self._history_reload_signature = signature
        self._finish_history_refresh(
            entries=entries,
            total=total,
            selected_rows=selected_rows,
            current_row=current_row,
            scroll_value=scroll_value,
        )

    def _current_history_reload_signature(
        self,
    ) -> tuple[HistoryStorageSignature, int, str] | None:
        getter = getattr(self._history_store, "storage_signature", None)
        if not callable(getter):
            return None
        return (
            getter(),
            self.history_max_spin.value(),
            str(self.history_timezone_combo.currentData() or DEFAULT_DISPLAY_TIMEZONE),
        )

    def _history_list_item(
        self,
        entry: TranscriptHistoryEntry,
    ) -> QtWidgets.QListWidgetItem:
        text = entry.text.strip().replace("\n", " ")
        preview = text[:70] + ("..." if len(text) > 70 else "")
        display_timezone = str(
            self.history_timezone_combo.currentData() or DEFAULT_DISPLAY_TIMEZONE
        )
        timestamp = format_history_timestamp(entry.created_at, display_timezone)
        label = f"{timestamp} | {entry.engine}/{entry.model} | {preview}"
        item = QtWidgets.QListWidgetItem(label)
        item.setData(QtCore.Qt.UserRole, entry)
        self._apply_compact_list_item_size(self.history_list, item)
        return item

    def _apply_reconciled_history_refresh(
        self,
        *,
        entries: list[TranscriptHistoryEntry],
        total: int,
        selected_rows: list[int],
        current_row: int | None,
        scroll_value: int,
    ) -> bool:
        changes = recent_entries_change_plan(self._history_entries, entries)
        if not changes:
            self._history_entries = entries
            self._finish_history_refresh(
                entries=entries,
                total=total,
                selected_rows=selected_rows,
                current_row=current_row,
                scroll_value=scroll_value,
            )
            return True
        if not self._history_entries and entries:
            return False

        self.history_list.setUpdatesEnabled(False)
        self.history_list.blockSignals(True)
        try:
            for change in reversed(changes):
                if change.kind == "delete":
                    self._remove_history_items(
                        change.previous_start,
                        change.previous_stop,
                    )
                elif change.kind == "insert":
                    self._insert_history_items(
                        change.previous_start,
                        entries[change.current_start : change.current_stop],
                    )
                elif change.kind == "update":
                    for row, entry in zip(
                        range(change.previous_start, change.previous_stop),
                        entries[change.current_start : change.current_stop],
                    ):
                        self._update_history_item(row, entry)
                elif change.kind == "replace":
                    self._remove_history_items(
                        change.previous_start,
                        change.previous_stop,
                    )
                    self._insert_history_items(
                        change.previous_start,
                        entries[change.current_start : change.current_stop],
                    )
                else:
                    return False
        finally:
            self.history_list.blockSignals(False)
            self.history_list.setUpdatesEnabled(True)

        self._history_entries = entries
        mapped_current_rows = (
            map_recent_entry_rows(changes, [current_row])
            if current_row is not None and current_row >= 0
            else []
        )
        self._finish_history_refresh(
            entries=entries,
            total=total,
            selected_rows=map_recent_entry_rows(changes, selected_rows),
            current_row=mapped_current_rows[0] if mapped_current_rows else None,
            scroll_value=scroll_value,
        )
        return True

    def _remove_history_items(self, start: int, stop: int) -> None:
        for row in range(stop - 1, start - 1, -1):
            if 0 <= row < self.history_list.count():
                self.history_list.takeItem(row)

    def _insert_history_items(
        self,
        start: int,
        entries: list[TranscriptHistoryEntry],
    ) -> None:
        for entry in reversed(entries):
            self.history_list.insertItem(start, self._history_list_item(entry))

    def _update_history_item(self, row: int, entry: TranscriptHistoryEntry) -> None:
        item = self.history_list.item(row)
        if item is None:
            return
        replacement = self._history_list_item(entry)
        item.setText(replacement.text())
        item.setData(QtCore.Qt.UserRole, entry)
        item.setSizeHint(replacement.sizeHint())

    def _finish_history_refresh(
        self,
        *,
        entries: list[TranscriptHistoryEntry],
        total: int,
        selected_rows: list[int],
        current_row: int | None,
        scroll_value: int,
    ) -> None:
        self._update_history_count_label(total)
        selected_row_set = {row for row in selected_rows if 0 <= row < len(entries)}
        restored_selection = False
        for row, entry in enumerate(entries):
            item = self.history_list.item(row)
            if item is None:
                continue
            item.setData(QtCore.Qt.UserRole, entry)
            item.setSelected(row in selected_row_set)
            if item.isSelected():
                restored_selection = True
        if current_row is not None and 0 <= current_row < self.history_list.count():
            self.history_list.setCurrentItem(
                self.history_list.item(current_row),
                QtCore.QItemSelectionModel.NoUpdate,
            )
        restore_vertical_scrollbar(self.history_list, scroll_value)
        if restored_selection:
            self._on_history_item_selected()
            return

        self.history_detail.clear()
        self.history_copy_button.setEnabled(False)
        self.history_edit_button.setEnabled(False)
        self.history_retranscribe_button.setEnabled(False)
        self.history_show_audio_button.setEnabled(False)
        self.history_delete_button.setEnabled(False)
        self._reset_history_copy_feedback()

    def _selected_history_items(self) -> list[QtWidgets.QListWidgetItem]:
        """Selected history items sorted by their row order in the list."""
        items = self.history_list.selectedItems()
        return sorted(items, key=self.history_list.row)

    def _selected_history_entries(self) -> list[TranscriptHistoryEntry]:
        entries: list[TranscriptHistoryEntry] = []
        for item in self._selected_history_items():
            entry = item.data(QtCore.Qt.UserRole)
            if entry is not None:
                entries.append(entry)
        return entries

    def _on_history_item_selected(self) -> None:
        entries = self._selected_history_entries()
        if not entries:
            self.history_copy_button.setEnabled(False)
            self.history_edit_button.setEnabled(False)
            self.history_retranscribe_button.setEnabled(False)
            self.history_show_audio_button.setEnabled(False)
            self.history_delete_button.setEnabled(False)
            self.history_detail.clear()
            self._reset_history_copy_feedback()
            return
        if len(entries) == 1:
            text = str(getattr(entries[0], "text", "") or "")
            has_audio = self._history_audio_path(entries[0]) is not None
            self.history_copy_button.setEnabled(bool(text))
            self.history_edit_button.setEnabled(bool(text))
            self.history_retranscribe_button.setEnabled(has_audio)
            self.history_show_audio_button.setEnabled(has_audio)
            self.history_detail.setPlainText(text)
        else:
            has_text = any(str(getattr(e, "text", "") or "") for e in entries)
            self.history_copy_button.setEnabled(has_text)
            # Editing is only meaningful for a single entry.
            self.history_edit_button.setEnabled(False)
            self.history_retranscribe_button.setEnabled(False)
            self.history_show_audio_button.setEnabled(False)
            self.history_detail.setPlainText(f"{len(entries)} entries selected.")
        self.history_delete_button.setEnabled(True)
        self._reset_history_copy_feedback()

    def _history_audio_path(self, entry: TranscriptHistoryEntry) -> Path | None:
        stored_path = str(getattr(entry, "source_audio_path", "") or "").strip()
        if stored_path:
            path = Path(stored_path)
            if path.is_file():
                return path

        source_id = str(getattr(entry, "source_recording_id", "") or "").strip()
        if not source_id:
            return None
        try:
            state = self._last_recording_store.load()
        except Exception:
            return None
        if state is None or str(getattr(state, "recording_id", "")) != source_id:
            return None
        path = Path(str(getattr(state, "audio_path", "") or ""))
        return path if path.is_file() else None

    def _prepare_selected_history_retranscription(self) -> None:
        entries = self._selected_history_entries()
        if len(entries) != 1:
            return
        entry = entries[0]
        audio_path = self._history_audio_path(entry)
        if audio_path is None:
            self._set_history_status(
                "The audio for this history entry is no longer available.",
                error=True,
            )
            self._on_history_item_selected()
            return

        engine_index = self.import_engine_combo.findData(entry.engine)
        if engine_index >= 0:
            self.import_engine_combo.setCurrentIndex(engine_index)
        model_index = self.import_model_combo.findData(entry.model)
        if model_index >= 0:
            self.import_model_combo.setCurrentIndex(model_index)
        self._set_selected_import_file(str(audio_path))
        self.import_result_label.setText(
            "History audio loaded. Choose the engine and model, then start "
            "transcription. The new result will be saved as a separate entry."
        )
        self.import_result_label.setStyleSheet("color: #555;")
        import_index = self.tabs.indexOf(self._import_tab)
        if import_index >= 0:
            self.tabs.setCurrentIndex(import_index)

    def _show_selected_history_audio_file(self) -> None:
        entries = self._selected_history_entries()
        if len(entries) != 1:
            return
        audio_path = self._history_audio_path(entries[0])
        if audio_path is None:
            self._set_history_status(
                "The audio for this history entry is no longer available.",
                error=True,
            )
            self._on_history_item_selected()
            return
        native_path = QtCore.QDir.toNativeSeparators(str(audio_path.resolve()))
        started = QtCore.QProcess.startDetached(
            "explorer.exe",
            [f"/select,{native_path}"],
        )
        if isinstance(started, tuple):
            started = started[0]
        if not started:
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(audio_path.parent))
            )

    def _copy_selected_history(self) -> None:
        text = join_recent_entries_for_clipboard(self._selected_history_entries())
        if not text:
            return
        QtGui.QGuiApplication.clipboard().setText(text)
        self._flash_history_copy_feedback()

    def _on_history_item_double_clicked(
        self,
        item: QtWidgets.QListWidgetItem,
    ) -> None:
        """Copy the double-clicked entry's transcript to the clipboard."""
        entry = item.data(QtCore.Qt.UserRole)
        text = str(getattr(entry, "text", "") or "")
        if not text:
            return
        QtGui.QGuiApplication.clipboard().setText(text)
        self._flash_history_copy_feedback()

    def _flash_history_copy_feedback(self) -> None:
        self.history_copy_button.setText("Copied")
        set_button_feedback_state(self.history_copy_button, "success")
        self._history_copy_feedback_timer.start()

    def _edit_selected_history(self) -> None:
        entries = self._selected_history_entries()
        if len(entries) != 1:
            return
        entry = entries[0]
        if entry is None:
            return
        current_text = str(getattr(entry, "text", "") or "")
        next_text = TranscriptEditDialog.get_text(self, current_text)
        if next_text is None or next_text == current_text:
            return
        updated = self._history_store.update_entry_text(entry, next_text)
        if updated <= 0:
            self._set_history_status(
                "Selected history entry was not found.", error=True
            )
            return
        self._set_history_status("")
        self._refresh_history_list(force=True)

    def _delete_selected_history(self) -> None:
        entries = self._selected_history_entries()
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
            "Delete history entries" if count > 1 else "Delete history entry",
            prompt,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        removed = self._history_store.delete_entries(entries)
        if removed <= 0:
            self._set_history_status(
                "Selected history entries were not found.", error=True
            )
            return
        self._set_history_status("")
        self._refresh_history_list(force=True)

    def _reset_history_copy_feedback(self) -> None:
        self.history_copy_button.setText("Copy selected")
        set_button_feedback_state(self.history_copy_button, None)
