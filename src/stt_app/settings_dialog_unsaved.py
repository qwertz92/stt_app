"""Settings dialog: unsaved-changes tracking and the close prompt.

Before this the dialog gave no sign that an edit had not been saved: Save was
enabled with nothing to save, and Close, Esc and the title bar's X threw
unsaved edits away without a word.

What counts as an edit is a fingerprint of the setting widgets taken when
they were last settled (populated, saved, or moved by the dialog itself),
compared with their values now. It is deliberately not "would Save write
anything": answering that builds `AppSettings` from the widgets, which parses
the hotkeys (a half-typed one is invalid) and reads the settings file, and
the answer is wanted on every keystroke. Undoing an edit makes the widget
equal its fingerprint again, so the dialog is clean again.
"""
from __future__ import annotations

from collections.abc import Iterable

from PySide6 import QtCore, QtGui, QtWidgets


class _UnsavedChangesMixin:
    _UNSAVED_CHANGES_DEBOUNCE_MS = 150
    _UNSAVED_CHANGES_TEXT = "Unsaved changes"
    # Amber, the colour this dialog uses for "nothing is broken, but look".
    _UNSAVED_CHANGES_COLOR = "#b26a00"

    def _install_unsaved_changes_tracking(self) -> None:
        """Connect every setting widget; must run after `_build_ui`."""
        self._unsaved_baseline: dict[int, object] = {}
        self._unsaved_widgets = self._watched_setting_widgets()
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(self._UNSAVED_CHANGES_DEBOUNCE_MS)
        timer.timeout.connect(self._refresh_unsaved_changes_ui)
        self._unsaved_changes_timer = timer
        for widget in self._unsaved_widgets:
            signal = self._change_signal(widget)
            if signal is not None:
                signal.connect(self._schedule_unsaved_changes_refresh)

    def _watched_setting_widgets(self) -> list[QtWidgets.QWidget]:
        """Every input on the pages before History, plus History's two.

        The settings pages are the ones built before the History tab
        (Transcription, Hotkeys & Display, Audio, Models, API Keys); walking
        them rather than listing attribute names means a setting added to one
        of them is tracked without anyone remembering to add it here. What is
        on those pages and is not a setting is excluded by name.
        """
        excluded = {id(getattr(self, "test_conn_target_combo", None))}
        pages = [
            self.tabs.widget(index)
            for index in range(getattr(self, "_history_tab_index", 0))
        ]
        widgets: list[QtWidgets.QWidget] = []
        for page in pages:
            for widget in page.findChildren(QtWidgets.QWidget):
                if id(widget) in excluded or not self._is_setting_input(widget):
                    continue
                widgets.append(widget)
        for name in ("history_max_spin", "history_timezone_combo"):
            widget = getattr(self, name, None)
            if widget is not None:
                widgets.append(widget)
        return widgets

    @staticmethod
    def _is_setting_input(widget: QtWidgets.QWidget) -> bool:
        if isinstance(widget, QtWidgets.QLineEdit):
            # The line edits inside a spin box, a key-sequence edit or a combo
            # mirror their owner, which is tracked itself.
            return not isinstance(
                widget.parentWidget(),
                (
                    QtWidgets.QAbstractSpinBox,
                    QtWidgets.QKeySequenceEdit,
                    QtWidgets.QComboBox,
                ),
            )
        if isinstance(widget, QtWidgets.QCheckBox):
            return True
        # No slider: none of these pages has one as a setting, and every
        # scroll bar is one -- the pages' own and the popup list of every
        # combo box -- so scrolling read as an edit.
        return isinstance(
            widget,
            (
                QtWidgets.QComboBox,
                QtWidgets.QPlainTextEdit,
                QtWidgets.QAbstractSpinBox,
                QtWidgets.QKeySequenceEdit,
            ),
        )

    @staticmethod
    def _change_signal(widget: QtWidgets.QWidget):
        if isinstance(widget, QtWidgets.QComboBox):
            return widget.currentIndexChanged
        if isinstance(widget, QtWidgets.QCheckBox):
            return widget.toggled
        if isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QPlainTextEdit)):
            return widget.textChanged
        if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            return widget.valueChanged
        if isinstance(widget, QtWidgets.QKeySequenceEdit):
            return widget.keySequenceChanged
        return None

    @staticmethod
    def _setting_value(widget: QtWidgets.QWidget) -> object:
        if isinstance(widget, QtWidgets.QComboBox):
            data = widget.currentData()
            return widget.currentText() if data is None else data
        if isinstance(widget, QtWidgets.QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QtWidgets.QLineEdit):
            return widget.text()
        if isinstance(widget, QtWidgets.QPlainTextEdit):
            return widget.toPlainText()
        if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            return widget.value()
        if isinstance(widget, QtWidgets.QKeySequenceEdit):
            return widget.keySequence().toString(QtGui.QKeySequence.PortableText)
        return None

    def _unsaved_state_values(self) -> dict[int, object]:
        """The fingerprint: every watched widget, plus two pending states
        that no widget shows as a value."""
        values: dict[int, object] = {
            id(widget): self._setting_value(widget) for widget in self._unsaved_widgets
        }
        # A remote model chosen for a provider that is not selected now keeps
        # its choice in this map only; Save writes every provider's entry.
        values[id(self._remote_model_values)] = tuple(
            sorted(
                (provider, self._remote_model_value_for_provider(provider))
                for provider in self._remote_model_values
            )
        )
        values[id(self._provider_pending_clear)] = tuple(
            sorted(self._provider_pending_clear)
        )
        return values

    def has_unsaved_changes(self) -> bool:
        if not hasattr(self, "_unsaved_widgets"):
            return False
        return self._unsaved_state_values() != self._unsaved_baseline

    def _mark_unsaved_changes_clean(self) -> None:
        """Record what the widgets show now as settled."""
        if not hasattr(self, "_unsaved_widgets"):
            return
        self._unsaved_baseline = self._unsaved_state_values()
        self._refresh_unsaved_changes_ui()

    def _mark_widgets_clean(self, widgets: Iterable[object]) -> None:
        """Settle only these entries, e.g. after a key-only save."""
        if not hasattr(self, "_unsaved_widgets"):
            return
        current = self._unsaved_state_values()
        for widget in widgets:
            key = id(widget)
            if key in current:
                self._unsaved_baseline[key] = current[key]
        self._refresh_unsaved_changes_ui()

    def _schedule_unsaved_changes_refresh(self, *_args: object) -> None:
        timer = getattr(self, "_unsaved_changes_timer", None)
        if timer is not None:
            timer.start()

    def _refresh_unsaved_changes_ui(self) -> None:
        """Save's enabled state and the idle bottom line follow the state."""
        dirty = self.has_unsaved_changes()
        save_button = getattr(self, "_save_button", None)
        if save_button is not None:
            save_button.setEnabled(dirty)
        # A transient message (a save's confirmation, a failure) owns the line
        # until its timer runs out; `_on_save_status_timeout` re-evaluates.
        timer = getattr(self, "_save_status_timer", None)
        if timer is not None and timer.isActive():
            return
        self._show_idle_bottom_status()

    def _show_idle_bottom_status(self) -> None:
        label = getattr(self, "_save_status_label", None)
        if label is None:
            return
        if self.has_unsaved_changes():
            self._set_bottom_status(
                self._UNSAVED_CHANGES_TEXT, self._UNSAVED_CHANGES_COLOR
            )
        elif label.text() == self._UNSAVED_CHANGES_TEXT:
            self._set_bottom_status("")

    def _on_save_status_timeout(self) -> None:
        self._set_bottom_status("")
        self._show_idle_bottom_status()

    # --- Closing -----------------------------------------------------------

    def _confirm_close_with_unsaved_changes(self) -> bool:
        """Ask what to do with unsaved edits; True when the dialog may close.

        Only user entry points ask (the Close button, Esc, the title bar's X).
        A programmatic close never does: Qt 6 cancels an application quit
        when a window refuses its close event, and tests close dialogs in
        code by the hundred.
        """
        if self._shutdown_started or not self.isVisible():
            return True
        if not self.has_unsaved_changes():
            return True
        answer = QtWidgets.QMessageBox.question(
            self,
            "Unsaved changes",
            "Save your changes before closing?",
            QtWidgets.QMessageBox.Save
            | QtWidgets.QMessageBox.Discard
            | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Save,
        )
        if answer == QtWidgets.QMessageBox.Save:
            self._save()
            # A save that failed validation or could not write leaves the
            # edits unsaved, and closing would then lose them after all.
            return not self.has_unsaved_changes()
        if answer == QtWidgets.QMessageBox.Discard:
            if self._background_work_active():
                self._discard_unsaved_edits_while_busy()
            else:
                self.reload_from_store()
            return True
        return False

    def _discard_unsaved_edits_while_busy(self) -> None:
        """Put the setting widgets back without touching the running job.

        `reload_from_store` waits while dialog-owned work runs, because the
        views that work owns (a download's rows, the connection test's labels,
        the Import tab's pickers) must stay as it left them. Calling it here
        left the edits in the widgets behind a dialog told to discard them,
        and the next Save wrote them. The widgets go back to the values they
        were last settled from instead -- what the user could restore by hand
        -- and the reload at the next open, once nothing runs, catches up with
        anything written elsewhere meanwhile.
        """
        self._discard_unsaved_provider_key_edits()
        self._populate_setting_widgets(self._populated_settings)
        self._mark_unsaved_changes_clean()

    def _request_close(self) -> None:
        if self._confirm_close_with_unsaved_changes():
            self.reject()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        # QDialog maps Esc to reject() directly, which would bypass the prompt.
        if (
            event.key() == QtCore.Qt.Key_Escape
            and event.modifiers() == QtCore.Qt.NoModifier
        ):
            event.accept()
            self._request_close()
            return
        super().keyPressEvent(event)
