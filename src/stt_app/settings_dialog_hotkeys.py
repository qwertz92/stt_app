"""Settings dialog: Hotkeys & Display tab mixin (split from the General tab).

Hosts the set-once controls -- the four global hotkeys, the overlay corner and
the tray middle-click toggle -- so the General tab holds only what changes
during daily dictation (engine, model, language, mode, insertion) and fits
without scrolling.
"""
from __future__ import annotations

from PySide6 import QtWidgets

from .config import VALID_OVERLAY_CORNERS
from .settings_dialog_helpers import (
    _OVERLAY_CORNER_LABELS,
    _WheelPassthroughComboBox,
)


class _HotkeysTabMixin:
    def _build_hotkeys_tab(self) -> None:
        """Build the Hotkeys & Display tab.

        Must run before ``_build_audio_tab``, which applies the shared form
        label column across General, this tab and its own.
        """
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # --- Hotkeys section ---
        hotkey_box, hotkey_form = self._general_form_box("Hotkeys")

        self.hotkey_edit = QtWidgets.QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        if hasattr(self.hotkey_edit, "setClearButtonEnabled"):
            self.hotkey_edit.setClearButtonEnabled(True)
        hotkey_hint = QtWidgets.QLabel(
            "Click the hotkey field and press the combination to record it."
        )
        self._style_field_hint_label(hotkey_hint)
        hotkey_form.addRow("Hotkey", self._field_with_hint(self.hotkey_edit, hotkey_hint))

        self.cancel_hotkey_edit = QtWidgets.QKeySequenceEdit()
        self.cancel_hotkey_edit.setMaximumSequenceLength(1)
        if hasattr(self.cancel_hotkey_edit, "setClearButtonEnabled"):
            self.cancel_hotkey_edit.setClearButtonEnabled(True)
        cancel_hotkey_hint = QtWidgets.QLabel(
            "Cancel hotkey stops current recording/transcription (must differ from main hotkey)."
        )
        self._style_field_hint_label(cancel_hotkey_hint)
        hotkey_form.addRow(
            "Cancel Hotkey",
            self._field_with_hint(self.cancel_hotkey_edit, cancel_hotkey_hint),
        )

        self.show_overlay_hotkey_edit = QtWidgets.QKeySequenceEdit()
        self.show_overlay_hotkey_edit.setMaximumSequenceLength(1)
        if hasattr(self.show_overlay_hotkey_edit, "setClearButtonEnabled"):
            self.show_overlay_hotkey_edit.setClearButtonEnabled(True)
        show_overlay_hotkey_hint = QtWidgets.QLabel(
            "Brings the overlay to the front to check the last transcript, "
            "like the tray's Show overlay. Clear the field to disable."
        )
        show_overlay_hotkey_hint.setWordWrap(True)
        self._style_field_hint_label(show_overlay_hotkey_hint)
        hotkey_form.addRow(
            "Overlay Hotkey",
            self._field_with_hint(
                self.show_overlay_hotkey_edit,
                show_overlay_hotkey_hint,
            ),
        )

        self.repaste_hotkey_edit = QtWidgets.QKeySequenceEdit()
        self.repaste_hotkey_edit.setMaximumSequenceLength(1)
        if hasattr(self.repaste_hotkey_edit, "setClearButtonEnabled"):
            self.repaste_hotkey_edit.setClearButtonEnabled(True)
        repaste_hotkey_hint = QtWidgets.QLabel(
            "Optional: pastes the last transcript again into the currently "
            "focused window (also in the tray menu). Leave empty to disable."
        )
        repaste_hotkey_hint.setWordWrap(True)
        self._style_field_hint_label(repaste_hotkey_hint)
        hotkey_form.addRow(
            "Re-paste Hotkey",
            self._field_with_hint(self.repaste_hotkey_edit, repaste_hotkey_hint),
        )
        layout.addWidget(hotkey_box)

        # --- Display section ---
        display_box, display_form = self._general_form_box("Display")

        self.overlay_corner_combo = _WheelPassthroughComboBox()
        for value in VALID_OVERLAY_CORNERS:
            self.overlay_corner_combo.addItem(
                _OVERLAY_CORNER_LABELS.get(value, value), value
            )
        overlay_corner_hint = QtWidgets.QLabel(
            "Choose where the always-on-top recording overlay appears."
        )
        self._style_field_hint_label(overlay_corner_hint)
        display_form.addRow(
            "Overlay Corner",
            self._field_with_hint(self.overlay_corner_combo, overlay_corner_hint),
        )

        self.tray_middle_click_checkbox = QtWidgets.QCheckBox(
            "Middle-click the tray icon to start/stop dictation"
        )
        tray_middle_click_hint = QtWidgets.QLabel(
            "Works like the recording hotkey. Double-click still opens Settings."
        )
        tray_middle_click_hint.setWordWrap(True)
        self._style_field_hint_label(tray_middle_click_hint)
        display_form.addRow(
            "",
            self._field_with_hint(
                self.tray_middle_click_checkbox,
                tray_middle_click_hint,
            ),
        )
        layout.addWidget(display_box)

        # The shared label column spanning General, this tab and Audio &
        # Recording is applied by _build_audio_tab once all three exist.
        self._hotkeys_forms = (hotkey_form, display_form)
        layout.addStretch(1)
        self.tabs.addTab(tab, "Hotkeys && Display")
