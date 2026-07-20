"""Settings dialog: Audio & Recording tab mixin (split from the General tab).

Hosts the set-and-forget capture setup — microphone selection, warm stream,
VAD, silence gate, start/completion tones, and recording retention — so the
General tab stays focused on what changes during daily dictation (engine,
model, language, mode, insertion).
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from . import audio_devices
from .app_paths import debug_audio_path, recordings_dir
from .config import (
    DEFAULT_RECORDINGS_MAX_COUNT,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    DEFAULT_VAD_ENERGY_THRESHOLD,
    SILENCE_GATE_THRESHOLD_MAX,
    SILENCE_GATE_THRESHOLD_MIN,
    VAD_ENERGY_THRESHOLD_MAX,
    VAD_ENERGY_THRESHOLD_MIN,
    VALID_START_BEEP_TONES,
)
from .settings_dialog_helpers import (
    _INLINE_FIELD_BUTTON_SPACING_PX,
    _START_BEEP_TONE_LABELS,
    _WheelPassthroughComboBox,
    _WheelPassthroughDoubleSpinBox,
    _WheelPassthroughSpinBox,
)


class _AudioTabMixin:
    def _build_audio_tab(self) -> None:
        """Build the Audio & Recording tab.

        Must run after ``_build_general_tab``: it applies the shared form
        label column across both tabs so fields stay aligned when switching.
        """
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # --- Audio / VAD section ---
        audio_box, audio_form = self._general_form_box("Audio && Voice Detection")

        self.microphone_combo = _WheelPassthroughComboBox()
        self.microphone_combo.setToolTip(
            "Which microphone recordings use. 'System default' follows the "
            "Windows default input device at every recording start."
        )
        self.microphone_refresh_button = QtWidgets.QPushButton("Refresh")
        self.microphone_refresh_button.setToolTip(
            "Re-scan connected microphones. The list also updates "
            "automatically when devices are connected or removed."
        )
        self.microphone_refresh_button.clicked.connect(
            self._on_microphone_refresh_clicked
        )
        self._match_field_button_height(
            self.microphone_combo,
            self.microphone_refresh_button,
        )
        microphone_row = QtWidgets.QWidget()
        microphone_row_layout = QtWidgets.QHBoxLayout(microphone_row)
        microphone_row_layout.setContentsMargins(0, 0, 0, 0)
        microphone_row_layout.setSpacing(_INLINE_FIELD_BUTTON_SPACING_PX)
        microphone_row_layout.addWidget(self.microphone_combo, 1)
        microphone_row_layout.addWidget(self.microphone_refresh_button, 0)
        microphone_hint = QtWidgets.QLabel(
            "System default follows the Windows default microphone, also for "
            "the warm stream. A selected microphone that is not connected "
            "fails the recording instead of silently using another device."
        )
        microphone_hint.setWordWrap(True)
        self._style_field_hint_label(microphone_hint)
        audio_form.addRow(
            "Microphone",
            self._field_with_hint(microphone_row, microphone_hint),
        )
        # Dialog-owned so the delayed repopulate dies with the dialog instead
        # of firing into deleted Qt objects.
        self._microphone_repopulate_timer = QtCore.QTimer(self)
        self._microphone_repopulate_timer.setSingleShot(True)
        self._microphone_repopulate_timer.setInterval(1200)
        self._microphone_repopulate_timer.timeout.connect(
            self._on_microphone_repopulate_timeout
        )

        self.keep_microphone_warm_checkbox = QtWidgets.QCheckBox(
            "Keep microphone warm for instant recording start"
        )
        self.keep_microphone_warm_checkbox.setToolTip(
            "Keeps one microphone stream open in the background so pressing "
            "the hotkey starts capturing immediately. Useful on machines "
            "where opening the microphone takes seconds and the first words "
            "get cut off."
        )
        self.keep_microphone_warm_hint_label = QtWidgets.QLabel(
            "The microphone stays open while the app runs, so Windows shows "
            "the microphone-in-use indicator permanently. Audio is discarded "
            "unless a recording is active."
        )
        self.keep_microphone_warm_hint_label.setWordWrap(True)
        self._style_field_hint_label(self.keep_microphone_warm_hint_label)
        audio_form.addRow(
            "",
            self._field_with_hint(
                self.keep_microphone_warm_checkbox,
                self.keep_microphone_warm_hint_label,
            ),
        )

        self.vad_checkbox = QtWidgets.QCheckBox("Enable energy-based auto-stop")
        vad_hint = QtWidgets.QLabel(
            "After speech starts, recording stops automatically when the "
            "configured silence period is reached."
        )
        vad_hint.setWordWrap(True)
        self._style_field_hint_label(vad_hint)
        audio_form.addRow("", self._field_with_hint(self.vad_checkbox, vad_hint))

        self.vad_threshold_spin = _WheelPassthroughDoubleSpinBox()
        self.vad_threshold_spin.setDecimals(3)
        self.vad_threshold_spin.setSingleStep(0.002)
        self.vad_threshold_spin.setRange(
            VAD_ENERGY_THRESHOLD_MIN,
            VAD_ENERGY_THRESHOLD_MAX,
        )
        self.vad_threshold_spin.setValue(DEFAULT_VAD_ENERGY_THRESHOLD)
        self.vad_threshold_spin.setToolTip(
            "Lower value = more sensitive for quiet speech/whispering."
        )
        vad_threshold_hint = QtWidgets.QLabel(
            "Lower values detect quieter speech; higher values require louder audio."
        )
        vad_threshold_hint.setWordWrap(True)
        self._style_field_hint_label(vad_threshold_hint)
        audio_form.addRow(
            "VAD Threshold",
            self._field_with_hint(self.vad_threshold_spin, vad_threshold_hint),
        )

        self.silence_gate_checkbox = QtWidgets.QCheckBox(
            "Skip transcription when the recording is silent"
        )
        self.silence_gate_checkbox.setToolTip(
            "Speech models can hallucinate words from pure silence. When "
            "enabled, a recording whose loudest 100 ms window stays below "
            "the threshold is not transcribed at all; it is kept as the last "
            "recording for a manual retry."
        )
        silence_gate_hint = QtWidgets.QLabel(
            "Keep the threshold low so whispering still passes. The measured "
            "peak level of every recording is written to the log "
            "(recording_peak_level) to make tuning easy."
        )
        silence_gate_hint.setWordWrap(True)
        self._style_field_hint_label(silence_gate_hint)
        audio_form.addRow(
            "",
            self._field_with_hint(self.silence_gate_checkbox, silence_gate_hint),
        )

        self.silence_gate_threshold_spin = _WheelPassthroughDoubleSpinBox()
        self.silence_gate_threshold_spin.setDecimals(4)
        self.silence_gate_threshold_spin.setSingleStep(0.0005)
        self.silence_gate_threshold_spin.setRange(
            SILENCE_GATE_THRESHOLD_MIN,
            SILENCE_GATE_THRESHOLD_MAX,
        )
        self.silence_gate_threshold_spin.setValue(DEFAULT_SILENCE_GATE_THRESHOLD)
        self.silence_gate_threshold_spin.setToolTip(
            "Loudest-window RMS level below which a recording counts as "
            "silent. Lower value = more sensitive (whispers pass more easily)."
        )
        silence_threshold_hint = QtWidgets.QLabel(
            "Recordings below this loudest-window RMS level are treated as silent."
        )
        silence_threshold_hint.setWordWrap(True)
        self._style_field_hint_label(silence_threshold_hint)
        audio_form.addRow(
            "Silence Gate",
            self._field_with_hint(
                self.silence_gate_threshold_spin,
                silence_threshold_hint,
            ),
        )

        self.start_beep_checkbox = QtWidgets.QCheckBox("Play start tone on recording")
        audio_form.addRow("", self.start_beep_checkbox)

        self.start_beep_tone_combo = _WheelPassthroughComboBox()
        for value in VALID_START_BEEP_TONES:
            self.start_beep_tone_combo.addItem(
                _START_BEEP_TONE_LABELS.get(value, value), value
            )
        audio_form.addRow("Start Tone", self.start_beep_tone_combo)

        self.completion_beep_checkbox = QtWidgets.QCheckBox(
            "Play completion tone after text insertion"
        )
        self.completion_beep_checkbox.setToolTip(
            "Plays a tone when a finished transcript was inserted into its "
            "target window (batch, queued background, and re-paste inserts)."
        )
        completion_beep_hint = QtWidgets.QLabel(
            "Useful with queued or background inserts, where text can arrive "
            "while you are working elsewhere. Live streaming inserts stay "
            "silent."
        )
        completion_beep_hint.setWordWrap(True)
        self._style_field_hint_label(completion_beep_hint)
        audio_form.addRow(
            "",
            self._field_with_hint(
                self.completion_beep_checkbox,
                completion_beep_hint,
            ),
        )

        self.completion_beep_tone_combo = _WheelPassthroughComboBox()
        for value in VALID_START_BEEP_TONES:
            self.completion_beep_tone_combo.addItem(
                _START_BEEP_TONE_LABELS.get(value, value), value
            )
        audio_form.addRow("Completion Tone", self.completion_beep_tone_combo)
        layout.addWidget(audio_box)

        # --- Recordings section ---
        recordings_box, recordings_form = self._general_form_box("Recordings")

        self.save_wav_checkbox = QtWidgets.QCheckBox(
            "Keep last recording after successful transcription"
        )
        self.save_wav_path_label = QtWidgets.QLabel(
            "The current recording is always preserved until transcription "
            f"finishes. When enabled, the latest recording remains at: {debug_audio_path()}"
        )
        self.save_wav_path_label.setWordWrap(True)
        self._style_field_hint_label(self.save_wav_path_label)
        recordings_form.addRow(
            "",
            self._field_with_hint(self.save_wav_checkbox, self.save_wav_path_label),
        )

        self.save_all_recordings_checkbox = QtWidgets.QCheckBox(
            "Archive every recording to folder"
        )
        archive_recordings_hint = QtWidgets.QLabel(
            "Writes every original WAV file to the recordings folder for "
            "later retry or inspection."
        )
        archive_recordings_hint.setWordWrap(True)
        self._style_field_hint_label(archive_recordings_hint)
        recordings_form.addRow(
            "",
            self._field_with_hint(
                self.save_all_recordings_checkbox,
                archive_recordings_hint,
            ),
        )

        self.recordings_dir_edit = QtWidgets.QLineEdit()
        self.recordings_dir_edit.setPlaceholderText(
            f"Leave empty for default ({recordings_dir()})"
        )
        self.recordings_dir_browse = QtWidgets.QPushButton("Browse...")
        self.recordings_dir_browse.setFixedWidth(80)
        self.recordings_dir_browse.clicked.connect(self._browse_recordings_dir)
        self.recordings_open_button = QtWidgets.QPushButton("Open Folder")
        self.recordings_open_button.clicked.connect(self._open_recordings_dir)
        self._match_field_button_height(
            self.recordings_dir_edit,
            self.recordings_dir_browse,
            self.recordings_open_button,
        )
        recordings_dir_layout = QtWidgets.QHBoxLayout()
        self._configure_button_row(
            recordings_dir_layout,
            spacing=_INLINE_FIELD_BUTTON_SPACING_PX,
        )
        recordings_dir_layout.addWidget(self.recordings_dir_edit, 1)
        recordings_dir_layout.addWidget(self.recordings_dir_browse)
        recordings_dir_layout.addWidget(self.recordings_open_button)
        recordings_form.addRow("Recordings Folder", recordings_dir_layout)

        self.recordings_max_spin = _WheelPassthroughSpinBox()
        self.recordings_max_spin.setRange(1, 500)
        self.recordings_max_spin.setValue(DEFAULT_RECORDINGS_MAX_COUNT)
        self.recordings_max_spin.setToolTip(
            "Keep only the newest N archived recordings."
        )
        recordings_hint = QtWidgets.QLabel(
            "Maximum number of archived WAV files; the oldest files are removed first."
        )
        recordings_hint.setWordWrap(True)
        self._style_field_hint_label(recordings_hint)
        recordings_form.addRow(
            "Keep Recordings",
            self._field_with_hint(self.recordings_max_spin, recordings_hint),
        )
        layout.addWidget(recordings_box)

        layout.addStretch(1)
        self.tabs.addTab(tab, "Audio && Recording")

        # One measured label column across General and this tab keeps fields
        # aligned when switching between the two related tabs.
        self._apply_shared_form_label_width(
            (*self._general_forms, audio_form, recordings_form)
        )

    def _populate_microphone_combo(self, selected_name: str) -> None:
        """Fill the picker: system default, connected devices, stored-but-
        missing selection marked "(not connected)" so saving cannot silently
        drop it."""
        combo = self.microphone_combo
        blocker = QtCore.QSignalBlocker(combo)
        combo.clear()
        combo.addItem("System default (follow Windows)", "")
        try:
            names = [info.name for info in audio_devices.list_input_devices()]
        except Exception:
            names = []
        for name in names:
            combo.addItem(name, name)
        if selected_name and selected_name not in names:
            combo.addItem(f"{selected_name} (not connected)", selected_name)
        self._select_combo_data(combo, selected_name)
        del blocker

    def _on_microphone_refresh_clicked(self) -> None:
        current = str(self.microphone_combo.currentData() or "")
        # Ask the controller to re-enumerate PortAudio (it owns the streams
        # that must be idle for that); show the current list immediately and
        # again after the off-thread re-enumeration had time to finish.
        self.audio_device_refresh_requested.emit()
        self._populate_microphone_combo(current)
        self._microphone_repopulate_timer.start()

    def _on_microphone_repopulate_timeout(self) -> None:
        self._populate_microphone_combo(
            str(self.microphone_combo.currentData() or "")
        )
