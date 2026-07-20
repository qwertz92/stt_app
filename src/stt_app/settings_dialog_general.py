"""Settings dialog: general mixin (split from settings_dialog.py)."""
from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

from PySide6 import QtCore, QtWidgets

from .config import (
    DEFAULT_ENGINE,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_MODEL_SIZE,
    LANGUAGE_MODE_LABELS,
    LOCAL_BATCH_ONLY_MODELS,
    LOCAL_ENGLISH_ONLY_MODELS,
    LOCAL_EXPLICIT_LANGUAGE_MODELS,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_ONNX_AUTO_CPU_MODELS,
    LOCAL_ONNX_MODEL_PRECISION,
    LOCAL_ONNX_MODEL_RUNTIME_LABELS,
    LOCAL_ONNX_MODEL_SIZES,
    LOCAL_WEBGPU_MODEL_SIZES,
    VALID_DISPLAY_TIMEZONES,
    VALID_ENGINES,
    VALID_LANGUAGE_MODES,
    VALID_MODEL_SIZES,
    VALID_MODES,
    VALID_OVERLAY_CORNERS,
    VALID_INSERT_TARGETS,
    VALID_PASTE_MODES,
    language_modes_for_selection,
    supports_streaming,
)
from .settings_dialog_helpers import (
    _CONCURRENT_MODE_UI_CHOICES,
    _INSERT_TARGET_LABELS,
    _ENGINE_LABELS,
    _HISTORY_TIMEZONE_LABELS,
    _MODE_LABELS,
    _OVERLAY_CORNER_LABELS,
    _PASTE_MODE_LABELS,
    _REMOTE_MODEL_CHOICES,
    _REMOTE_MODEL_DEFAULTS,
    _WheelPassthroughComboBox,
)
from .settings_store import AppSettings


class _GeneralTabMixin:
    _GENERAL_FORM_ROW_SPACING_PX = 10
    _DYNAMIC_HINT_LINE_COUNT = 2

    @classmethod
    def _general_form_box(
        cls,
        title: str,
    ) -> tuple[QtWidgets.QGroupBox, QtWidgets.QFormLayout]:
        """Create a consistently spaced form section for the General tab."""
        box = QtWidgets.QGroupBox(title)
        form = QtWidgets.QFormLayout(box)
        form.setContentsMargins(10, 10, 10, 10)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(cls._GENERAL_FORM_ROW_SPACING_PX)
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        return box, form

    @classmethod
    def _reserve_dynamic_hint_height(cls, label: QtWidgets.QLabel) -> None:
        """Reserve a compact, stable two-line area for changing hint text."""
        # Windows' offscreen/high-DPI font backend can need several pixels more
        # than two nominal line spacings for the same wrapped glyph bounds.
        # Keep that platform padding inside the fixed area so text never clips
        # while all following rows still remain stationary.
        height = label.fontMetrics().lineSpacing() * cls._DYNAMIC_HINT_LINE_COUNT + 10
        label.setFixedHeight(height)
        label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)

    def _build_general_tab(self) -> None:
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

        self.history_timezone_combo = _WheelPassthroughComboBox()
        for value in VALID_DISPLAY_TIMEZONES:
            self.history_timezone_combo.addItem(
                _HISTORY_TIMEZONE_LABELS.get(value, value.upper()),
                value,
            )
        self.history_timezone_combo.setToolTip(
            "How stored UTC history timestamps are displayed in the app."
        )
        self.history_timezone_combo.currentIndexChanged.connect(
            lambda _index: self._refresh_history_list(force=True)
        )
        history_timezone_hint = QtWidgets.QLabel(
            "Transcript history is stored in UTC. This only changes how times "
            "are shown in Settings and the History window."
        )
        history_timezone_hint.setWordWrap(True)
        self._style_field_hint_label(history_timezone_hint)
        display_form.addRow(
            "History Time",
            self._field_with_hint(
                self.history_timezone_combo,
                history_timezone_hint,
            ),
        )

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

        # --- Engine / Mode section ---
        engine_box, engine_form = self._general_form_box("Engine && Mode")

        self.engine_combo = _WheelPassthroughComboBox()
        for value in VALID_ENGINES:
            self.engine_combo.addItem(_ENGINE_LABELS.get(value, value), value)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        engine_hint = QtWidgets.QLabel(
            "Local keeps audio on this computer and uses faster-whisper, "
            "ONNX/WebGPU, or ONNX Runtime GenAI. Remote engines upload audio "
            "to the selected provider."
        )
        engine_hint.setWordWrap(True)
        self._style_field_hint_label(engine_hint)
        engine_form.addRow("Engine", self._field_with_hint(self.engine_combo, engine_hint))

        # --- Unified model selector: one "Model" row, one page per engine kind ---
        # The stack naturally sizes to its largest page (Qt keeps every page's
        # sizeHint contributing to the stack's sizeHint regardless of which
        # page is current), so switching pages never shifts the rows below.
        self.model_selector_stack = QtWidgets.QStackedWidget()
        self.model_selector_stack.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )

        local_model_widget = QtWidgets.QWidget()
        local_model_layout = QtWidgets.QVBoxLayout(local_model_widget)
        local_model_layout.setContentsMargins(0, 0, 0, 0)
        local_model_layout.setSpacing(2)
        self.model_combo = _WheelPassthroughComboBox()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.local_model_runtime_warning_label = QtWidgets.QLabel(" ")
        self.local_model_runtime_warning_label.setWordWrap(True)
        self.local_model_runtime_warning_label.setStyleSheet(
            "color: #b71c1c; font-size: 11px;"
        )
        # Reserve a stable two-line note area so switching between models with
        # and without runtime notes never shifts the widgets below.
        self._reserve_dynamic_hint_height(self.local_model_runtime_warning_label)
        local_model_layout.addWidget(self.model_combo)
        local_model_layout.addWidget(self.local_model_runtime_warning_label)
        self.model_selector_stack.addWidget(local_model_widget)

        remote_model_widget = QtWidgets.QWidget()
        remote_model_layout = QtWidgets.QVBoxLayout(remote_model_widget)
        remote_model_layout.setContentsMargins(0, 0, 0, 0)
        remote_model_layout.setSpacing(3)
        self.remote_model_combo = _WheelPassthroughComboBox()
        self.remote_model_combo.currentIndexChanged.connect(
            self._on_remote_model_changed
        )
        self.remote_model_note_label = QtWidgets.QLabel("")
        self.remote_model_note_label.setWordWrap(True)
        self._style_field_hint_label(self.remote_model_note_label)
        self._reserve_dynamic_hint_height(self.remote_model_note_label)
        remote_model_layout.addWidget(self.remote_model_combo)
        remote_model_layout.addWidget(self.remote_model_note_label)
        self.model_selector_stack.addWidget(remote_model_widget)

        engine_form.addRow("Model", self.model_selector_stack)

        self.language_combo = _WheelPassthroughComboBox()
        for value in VALID_LANGUAGE_MODES:
            self.language_combo.addItem(
                LANGUAGE_MODE_LABELS.get(value, value), value
            )
        self.language_note_label = QtWidgets.QLabel("")
        self.language_note_label.setWordWrap(True)
        self._style_field_hint_label(self.language_note_label)
        self._reserve_dynamic_hint_height(self.language_note_label)
        self.language_note_label.setVisible(True)
        engine_form.addRow(
            "Language",
            self._field_with_hint(self.language_combo, self.language_note_label),
        )

        self.custom_vocabulary_edit = QtWidgets.QPlainTextEdit()
        self.custom_vocabulary_edit.setTabChangesFocus(True)
        self.custom_vocabulary_edit.setFixedHeight(
            self.custom_vocabulary_edit.fontMetrics().height() * 3 + 12
        )
        self.custom_vocabulary_edit.setPlaceholderText(
            "e.g. Kubernetes, Splunk SOAR"
        )
        self.vocabulary_hint_label = QtWidgets.QLabel(
            "Enter up to 100 terms or phrases, separated by commas, semicolons, "
            "or new lines. Spaces inside a phrase are kept (for example, "
            "Splunk SOAR). Supported in both modes by faster-whisper, "
            "AssemblyAI, and Deepgram, and in batch mode by OpenAI and Groq. "
            "Nemotron, Cohere/Granite ONNX, ElevenLabs, Azure, and Fun-ASR "
            "ignore it."
        )
        self.vocabulary_hint_label.setWordWrap(True)
        self._style_field_hint_label(self.vocabulary_hint_label)
        engine_form.addRow(
            "Vocabulary",
            self._field_with_hint(
                self.custom_vocabulary_edit,
                self.vocabulary_hint_label,
            ),
        )

        self.mode_combo = _WheelPassthroughComboBox()
        for value in VALID_MODES:
            self.mode_combo.addItem(_MODE_LABELS.get(value, value), value)
        self.mode_combo.setToolTip(
            "Streaming inserts only stable append-only text while speaking and "
            "auto-aborts on focus change. Batch remains the recommended default."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_hint = QtWidgets.QLabel(
            "Batch inserts text after recording stops. Streaming can append stable "
            "text while you speak, but it never rewrites already inserted text."
        )
        mode_hint.setWordWrap(True)
        self._style_field_hint_label(mode_hint)
        engine_form.addRow("Mode", self._field_with_hint(self.mode_combo, mode_hint))

        self.streaming_full_final_check = QtWidgets.QCheckBox(
            "Re-transcribe full recording after streaming"
        )
        self.streaming_full_final_check.setToolTip(
            "After a local faster-whisper streaming session ends, transcribe "
            "the whole recording once more so the saved history entry uses "
            "the highest-quality pass. Stopping takes noticeably longer on "
            "long dictations. Inserted text is unaffected either way."
        )
        streaming_full_final_hint = QtWidgets.QLabel(
            "Applies to local faster-whisper streaming only. When disabled, "
            "the history entry uses the live streaming text and stopping "
            "finishes faster."
        )
        streaming_full_final_hint.setWordWrap(True)
        self._style_field_hint_label(streaming_full_final_hint)
        engine_form.addRow(
            "",
            self._field_with_hint(
                self.streaming_full_final_check,
                streaming_full_final_hint,
            ),
        )

        self.concurrent_mode_combo = _WheelPassthroughComboBox()
        for value, label in _CONCURRENT_MODE_UI_CHOICES:
            self.concurrent_mode_combo.addItem(label, value)
        self.concurrent_mode_combo.setToolTip(
            "What happens to the previous transcription when you press the "
            "recording hotkey again, and when finished results are inserted. "
            "A finished transcription is never discarded.\n"
            "- Insert when idle: results are inserted once no transcription "
            "is running anymore.\n"
            "- Insert immediately: each result is inserted the moment it is "
            "ready, into the window captured for its recording.\n"
            "- History only: results are saved to history without inserting.\n"
            "- Cancel: stop the older transcription (a result that still "
            "finishes is kept in history)."
        )
        self.concurrent_mode_hint_label = QtWidgets.QLabel(
            "If you press the recording hotkey again before the previous "
            "transcription finishes, this controls the previous job. Jobs run "
            "one at a time and finished results keep their recording order."
        )
        self.concurrent_mode_hint_label.setWordWrap(True)
        self._style_field_hint_label(self.concurrent_mode_hint_label)
        engine_form.addRow(
            "New Recording",
            self._field_with_hint(
                self.concurrent_mode_combo,
                self.concurrent_mode_hint_label,
            ),
        )
        layout.addWidget(engine_box)

        # --- Text Insertion section ---
        paste_box, paste_form = self._general_form_box("Text Insertion")

        self.paste_mode_combo = _WheelPassthroughComboBox()
        for value in VALID_PASTE_MODES:
            self.paste_mode_combo.addItem(
                _PASTE_MODE_LABELS.get(value, value), value
            )
        self.paste_mode_combo.setToolTip(
            "Auto tries SendInput first and falls back to WM_PASTE. "
            "SendInput simulates the real Ctrl+V keyboard shortcut. "
            "WM_PASTE sends a paste message directly to the focused edit control; "
            "some modern apps ignore it."
        )
        self.paste_mode_hint_label = QtWidgets.QLabel(
            "SendInput behaves like pressing Ctrl+V; WM_PASTE bypasses keyboard "
            "simulation, but some modern apps ignore it. Auto tries SendInput "
            "first, then WM_PASTE."
        )
        self.paste_mode_hint_label.setWordWrap(True)
        self._style_field_hint_label(self.paste_mode_hint_label)
        paste_form.addRow(
            "Paste Mode",
            self._field_with_hint(self.paste_mode_combo, self.paste_mode_hint_label),
        )

        self.insert_target_combo = _WheelPassthroughComboBox()
        for value in VALID_INSERT_TARGETS:
            self.insert_target_combo.addItem(
                _INSERT_TARGET_LABELS.get(value, value), value
            )
        self.insert_target_combo.setToolTip(
            "Which window receives the finished transcript.\n"
            "- Window focused when the recording started: a queued result "
            "follows its own recording even after you moved on (default).\n"
            "- Window focused when the transcript is ready: the text goes to "
            "wherever you are working at that moment."
        )
        insert_target_hint = QtWidgets.QLabel(
            "The caret position inside the target is always the position at "
            "insert time; Windows cannot paste at a remembered caret offset."
        )
        insert_target_hint.setWordWrap(True)
        self._style_field_hint_label(insert_target_hint)
        paste_form.addRow(
            "Insert Into",
            self._field_with_hint(self.insert_target_combo, insert_target_hint),
        )

        self.keep_clipboard_checkbox = QtWidgets.QCheckBox(
            "Keep transcript in clipboard after insertion"
        )
        self.keep_clipboard_checkbox.setToolTip(
            "When enabled, the transcript remains in the clipboard after insertion. "
            "When disabled, the previous clipboard contents are restored."
        )
        keep_clipboard_hint = QtWidgets.QLabel(
            "This only controls whether the finished transcript replaces your "
            "previous clipboard contents after insertion."
        )
        keep_clipboard_hint.setWordWrap(True)
        self._style_field_hint_label(keep_clipboard_hint)
        paste_form.addRow(
            "",
            self._field_with_hint(self.keep_clipboard_checkbox, keep_clipboard_hint),
        )
        layout.addWidget(paste_box)

        # The shared label column spanning General and Audio & Recording is
        # applied by _build_audio_tab once both tabs exist.
        self._general_forms = (
            hotkey_form,
            display_form,
            engine_form,
            paste_form,
        )
        layout.addStretch(1)
        self.tabs.addTab(tab, "General")

    _MODEL_LABELS: ClassVar[dict[str, str]] = {
        "tiny": "tiny (~75 MB)",
        "base": "base (~141 MB)",
        "small": "small (~484 MB)",
        "medium": "medium (~1.4 GB)",
        "large-v3": "large-v3 (~3 GB, multilingual)",
        "large-v3-turbo": "large-v3-turbo (~809 MB, multilingual, fast)",
        "distil-large-v3.5": "distil-large-v3.5 (~756 MB, English only, improved)",
        "cohere-transcribe-03-2026": (
            "Cohere Transcribe 03-2026 (~2.13 GB, ONNX/WebGPU)"
        ),
        "granite-4.0-1b-speech": (
            "IBM Granite 4.0 1B Speech (~1.84 GB, ONNX/WebGPU)"
        ),
        "granite-speech-4.1-2b": (
            "IBM Granite Speech 4.1 2B (~1.84 GB, ONNX/WebGPU)"
        ),
        "granite-speech-4.1-2b-plus": (
            "IBM Granite Speech 4.1 2B Plus (~4.1 GB, ONNX)"
        ),
        "granite-speech-4.1-2b-nar": (
            "IBM Granite Speech 4.1 2B NAR (~2.5 GB, ONNX)"
        ),
        "nemotron-3.5-asr-streaming-0.6b-int4": (
            "NVIDIA Nemotron 3.5 ASR 0.6B (~793 MB, true 560 ms streaming)"
        ),
    }

    @staticmethod
    def _precision_label(model_name: str) -> str:
        precision = LOCAL_ONNX_MODEL_PRECISION.get(model_name, "")
        if not precision:
            return ""
        return precision.upper()

    def _model_label(self, model_name: str) -> str:
        label = self._MODEL_LABELS.get(model_name, model_name)
        precision = self._precision_label(model_name)
        if not precision:
            return label
        return f"{label} [{precision}]"

    def _remote_model_value_for_provider(self, provider: str) -> str:
        normalized = str(provider or "").strip().lower()
        fallback = _REMOTE_MODEL_DEFAULTS.get(normalized, "")
        value = str(self._remote_model_values.get(normalized, fallback) or fallback)
        valid_values = {item_value for item_value, _label in _REMOTE_MODEL_CHOICES.get(normalized, ())}
        if value not in valid_values:
            return fallback
        return value

    def _import_model_choices(
        self,
        engine: str,
    ) -> tuple[tuple[str, str], ...]:
        normalized = str(engine or "").strip().lower()
        if normalized == DEFAULT_ENGINE:
            return tuple(
                (value, self._model_label(value))
                for value in VALID_MODEL_SIZES
            )
        return _REMOTE_MODEL_CHOICES.get(normalized, ())

    def _import_model_value_for_engine(self, engine: str) -> str:
        normalized = str(engine or "").strip().lower()
        if normalized == DEFAULT_ENGINE:
            fallback = str(self._loaded_settings.model_size or DEFAULT_MODEL_SIZE)
            value = str(self._import_model_values.get(normalized, fallback) or fallback)
            if value not in VALID_MODEL_SIZES:
                return DEFAULT_MODEL_SIZE
            return value
        fallback = _REMOTE_MODEL_DEFAULTS.get(normalized, "")
        value = str(self._import_model_values.get(normalized, fallback) or fallback)
        valid_values = {
            item_value
            for item_value, _label in self._import_model_choices(normalized)
        }
        if value not in valid_values:
            return fallback
        return value

    def _update_import_model_selector(self) -> None:
        if not hasattr(self, "import_model_combo"):
            return

        engine = str(self.import_engine_combo.currentData() or DEFAULT_ENGINE)
        choices = self._import_model_choices(engine)
        current_value = self._import_model_value_for_engine(engine)

        self.import_model_combo.blockSignals(True)
        self.import_model_combo.clear()
        for value, label in choices:
            self.import_model_combo.addItem(label, value)
        self._select_combo_data(self.import_model_combo, current_value)
        self.import_model_combo.setEnabled(self.import_model_combo.count() > 0)
        self.import_model_combo.blockSignals(False)

        self._update_import_language_selector()

        if engine == DEFAULT_ENGINE:
            self.import_model_note.setText(
                "This import uses the selected local model only for the imported file."
            )
            return
        self.import_model_note.setText(
            f"This import uses the selected {self._provider_label(engine)} model only for the imported file."
        )

    @staticmethod
    def _import_language_key(engine: str, model: str) -> tuple[str, str]:
        return (str(engine or "").strip().lower(), str(model or "").strip())

    def _update_import_language_selector(
        self,
        *,
        preferred_mode: str | None = None,
    ) -> None:
        if not hasattr(self, "import_language_combo"):
            return

        engine = str(self.import_engine_combo.currentData() or DEFAULT_ENGINE)
        model = str(
            self.import_model_combo.currentData()
            or self._import_model_value_for_engine(engine)
        )
        supported_modes = language_modes_for_selection(engine, model, "batch")
        key = self._import_language_key(engine, model)
        selected_mode = (
            preferred_mode
            or self._import_language_values.get(key)
            or str(self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE)
        )
        target_mode = (
            selected_mode
            if selected_mode in supported_modes
            else (
                DEFAULT_LANGUAGE_MODE
                if DEFAULT_LANGUAGE_MODE in supported_modes
                else supported_modes[0]
            )
        )

        self.import_language_combo.blockSignals(True)
        self.import_language_combo.clear()
        for value in supported_modes:
            self.import_language_combo.addItem(
                LANGUAGE_MODE_LABELS.get(value, value), value
            )
        self._select_combo_data(self.import_language_combo, target_mode)
        self.import_language_combo.setEnabled(len(supported_modes) > 1)
        self.import_language_combo.blockSignals(False)
        self._import_language_values[key] = target_mode
        self.import_language_note.setText(
            "Used only for this imported file; it does not change the General tab."
        )
        self.import_language_combo.setToolTip(self.import_language_note.text())

    def _apply_engine_model_selection(
        self,
        settings: AppSettings,
        engine: str,
        model_value: str,
    ) -> AppSettings:
        normalized_engine = str(engine or "").strip().lower()
        selected_model = str(model_value or "").strip()
        if normalized_engine == DEFAULT_ENGINE:
            if selected_model and selected_model in VALID_MODEL_SIZES:
                return replace(settings, model_size=selected_model)
            return settings
        if normalized_engine == "groq" and selected_model:
            return replace(settings, groq_model=selected_model)
        if normalized_engine == "openai" and selected_model:
            return replace(settings, openai_model=selected_model)
        if normalized_engine == "deepgram" and selected_model:
            return replace(settings, deepgram_model=selected_model)
        if normalized_engine == "assemblyai" and selected_model:
            return replace(settings, assemblyai_model=selected_model)
        if normalized_engine == "elevenlabs" and selected_model:
            return replace(settings, elevenlabs_model=selected_model)
        if normalized_engine == "azure" and selected_model:
            return replace(settings, azure_speech_model=selected_model)
        if normalized_engine == "funasr" and selected_model:
            return replace(settings, funasr_model=selected_model)
        return settings

    def _update_model_selector_page(self) -> None:
        """Switch the unified Model row to the local or remote page."""
        if not hasattr(self, "model_selector_stack"):
            return
        provider = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        page = 0 if provider == DEFAULT_ENGINE else 1
        self.model_selector_stack.setCurrentIndex(page)

    def _update_remote_model_selector(self) -> None:
        if not hasattr(self, "remote_model_combo"):
            return

        self._update_model_selector_page()
        provider = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        choices = _REMOTE_MODEL_CHOICES.get(provider, ())

        self.remote_model_combo.blockSignals(True)
        self.remote_model_combo.clear()

        if provider == DEFAULT_ENGINE:
            self.remote_model_combo.addItem("Not applicable for local engine", "")
            self.remote_model_combo.setEnabled(False)
            self.remote_model_note_label.setText(
                "faster-whisper and Nemotron support streaming; Cohere and "
                "Granite ONNX/WebGPU models are batch-only."
            )
            self.remote_model_combo.blockSignals(False)
            return

        for value, label in choices:
            self.remote_model_combo.addItem(label, value)
        self._select_combo_data(
            self.remote_model_combo,
            self._remote_model_value_for_provider(provider),
        )
        self.remote_model_combo.setEnabled(True)

        note = (
            f"The selected {self._provider_label(provider)} model is used for "
            "batch dictation and audio imports; its stored API key is reused."
        )
        if provider == "assemblyai" and self.mode_combo.currentData() == "streaming":
            self.remote_model_combo.setEnabled(False)
            note = (
                "Streaming always uses Universal-3.5 Pro Realtime. The selected "
                "model applies to batch transcription and audio imports."
            )
        elif provider == "deepgram":
            note = "Deepgram uses the selected model for batch and streaming transcription."
        elif provider == "elevenlabs":
            note = (
                "The selected model applies to batch dictation and audio imports. "
                "Realtime Scribe exists, but is not yet wired into this app."
            )
        elif provider == "azure":
            note = (
                "Cloud, batch-only. Configure the endpoint and key on the Remote "
                "tab; MAI-Transcribe 1.5 supports the most languages."
            )
        elif provider == "funasr":
            note = (
                "Cloud, batch-only, with 31 languages focused on Chinese and "
                "East/Southeast Asia, but no German. Use Azure or local for German."
            )

        self.remote_model_note_label.setText(note)
        self.remote_model_combo.blockSignals(False)

    def _language_modes_for_current_selection(self) -> tuple[str, ...]:
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        mode = str(self.mode_combo.currentData() or DEFAULT_MODE)
        if engine == DEFAULT_ENGINE:
            model = str(self.model_combo.currentData() or "")
        else:
            model = self._remote_model_value_for_provider(engine)
        return language_modes_for_selection(engine, model, mode)

    def _language_constraint_note(self) -> str:
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        mode = str(self.mode_combo.currentData() or DEFAULT_MODE)
        if engine == DEFAULT_ENGINE:
            model = str(self.model_combo.currentData() or "")
        else:
            model = self._remote_model_value_for_provider(engine)

        if engine == "assemblyai" and mode == "streaming":
            return (
                "AssemblyAI streaming always uses automatic language detection "
                "(language is fixed to Auto)."
            )

        if engine == "local" and model in LOCAL_ENGLISH_ONLY_MODELS:
            return (
                "distil-large-v3.5 is an English-only model "
                "(only Auto and English are available)."
            )

        if engine == "local" and model == "cohere-transcribe-03-2026":
            return (
                "Cohere supports 14 explicit languages and does not provide "
                "automatic language detection."
            )

        if engine == "local" and model in LOCAL_EXPLICIT_LANGUAGE_MODELS:
            return (
                "Granite supports Auto plus the languages documented for the "
                "selected model."
            )

        if engine == "local" and model in LOCAL_NEMOTRON_MODEL_SIZES:
            return (
                "Nemotron supports automatic language detection plus the "
                "transcription-ready and broad-coverage languages in the "
                "official ORT GenAI language-ID mapping."
            )

        if engine == "groq":
            return (
                "Groq Whisper models are multilingual. 'Auto' lets the model detect "
                "language; selecting a language sends a recognition hint."
            )

        if engine == "elevenlabs":
            return (
                "ElevenLabs Scribe models are multilingual. 'Auto' lets the provider "
                "detect language; selecting a language sends a language hint."
            )

        if engine == "deepgram":
            return "Available languages follow the selected Deepgram Nova model."

        if engine == "assemblyai":
            return (
                "Universal-3.5 Pro supports 18 languages at the highest accuracy. "
                "Universal-2 is the lower-cost choice for broader coverage."
            )

        if engine == "azure":
            return (
                "Azure LLM Speech (MAI-Transcribe) is multilingual. 'Auto' lets "
                "the model detect language; selecting one sends a locale hint. "
                "Available languages follow the selected MAI-Transcribe model."
            )

        if engine == "funasr":
            return (
                "Fun-ASR is multilingual across 31 languages but does NOT support "
                "German. 'Auto' auto-detects; selecting one sends a language hint."
            )

        return ""

    def _update_language_availability(self, preferred_mode: str | None = None) -> None:
        supported_modes = self._language_modes_for_current_selection()
        selected_mode = preferred_mode or str(
            self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE
        )

        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        for value in supported_modes:
            self.language_combo.addItem(
                LANGUAGE_MODE_LABELS.get(value, value),
                value,
            )

        target_mode = (
            selected_mode if selected_mode in supported_modes else supported_modes[0]
        )
        self._select_combo_data(self.language_combo, target_mode)
        self.language_combo.blockSignals(False)

        note = self._language_constraint_note()
        default_note = (
            "Auto lets the selected engine detect the language; choosing one "
            "sends an explicit recognition hint. The choices update with the "
            "selected engine and model."
        )
        self.language_note_label.setText(note or default_note)
        self.language_combo.setEnabled(len(supported_modes) > 1)
        self.language_combo.setToolTip(
            note or default_note
        )

    def _update_local_model_runtime_warning(self) -> None:
        if not hasattr(self, "local_model_runtime_warning_label"):
            return
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        model_name = (
            str(self.model_combo.currentData() or "")
            if hasattr(self, "model_combo")
            else ""
        )
        # The label stays visible with reserved space either way; only its
        # text and color change, so model switches never shift the layout.
        warning_style = "color: #b71c1c; font-size: 11px;"
        note_style = "color: #666666; font-size: 11px;"
        if engine == "local" and model_name in LOCAL_ONNX_AUTO_CPU_MODELS:
            self.local_model_runtime_warning_label.setStyleSheet(warning_style)
            self.local_model_runtime_warning_label.setText(
                "Batch mode only. NAR uses CPU by default because its encoder "
                "is not currently compatible with WebGPU or DirectML."
            )
            return
        if engine == "local" and model_name in LOCAL_WEBGPU_MODEL_SIZES:
            self.local_model_runtime_warning_label.setStyleSheet(warning_style)
            self.local_model_runtime_warning_label.setText(
                "Batch mode only. Auto tries WebGPU, then DirectML, then "
                "falls back to CPU (active device shown in the overlay)."
            )
            return
        if engine == "local" and model_name in LOCAL_NEMOTRON_MODEL_SIZES:
            self.local_model_runtime_warning_label.setStyleSheet(warning_style)
            self.local_model_runtime_warning_label.setText(
                "Streams with a fixed 560 ms ONNX chunk. Auto tries DirectML, "
                "then falls back to CPU."
            )
            return
        self.local_model_runtime_warning_label.setStyleSheet(note_style)
        if engine == "local" and model_name:
            self.local_model_runtime_warning_label.setText(
                "faster-whisper runs via CTranslate2 in batch and streaming. "
                "Vocabulary biasing is available in both modes."
            )
            return
        self.local_model_runtime_warning_label.setText(" ")

    def _update_engine_indicator(self) -> None:
        """Update the always-visible engine indicator bar."""
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        if engine == "local":
            model = (
                str(self.model_combo.currentData() or "")
                if hasattr(self, "model_combo")
                else ""
            )
            runtime = (
                LOCAL_ONNX_MODEL_RUNTIME_LABELS.get(model, "ONNX")
                if model in LOCAL_ONNX_MODEL_SIZES
                else "faster-whisper"
            )
            label = f"Engine: LOCAL ({runtime})"
            self.engine_indicator.setText(label)
            self.engine_indicator.setStyleSheet(
                "font-weight: bold; padding: 4px; border-radius: 4px; "
                "background-color: #e8f5e9; color: #1b5e20;"
            )
        else:
            label = self._provider_label(engine)
            self.engine_indicator.setText(f"Engine: REMOTE ({label})")
            self.engine_indicator.setStyleSheet(
                "font-weight: bold; padding: 4px; border-radius: 4px; "
                "background-color: #e3f2fd; color: #0d47a1;"
            )

    def _update_mode_availability(self) -> None:
        """Enable/disable streaming option based on the selected engine."""
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        model_name = (
            str(self.model_combo.currentData() or "")
            if hasattr(self, "model_combo")
            else ""
        )
        streaming_supported = supports_streaming(engine, model_name)
        streaming_idx = self.mode_combo.findData("streaming")

        if streaming_idx < 0:
            return

        # Disable the streaming item in the combo model (greys it out).
        model = self.mode_combo.model()
        item = model.item(streaming_idx)
        if item is not None:
            if streaming_supported:
                item.setEnabled(True)
                item.setToolTip("")
            else:
                item.setEnabled(False)
                if engine == "local" and model_name in LOCAL_BATCH_ONLY_MODELS:
                    item.setToolTip(
                        "Streaming is not supported by the selected ONNX/WebGPU "
                        "local model. Use batch mode."
                    )
                else:
                    item.setToolTip(
                        f"Streaming is not supported by the {engine} provider. "
                        "Use faster-whisper local models, AssemblyAI, or Deepgram "
                        "for streaming."
                    )

        # If streaming is selected but not supported, switch to batch.
        if not streaming_supported and self.mode_combo.currentData() == "streaming":
            batch_idx = self.mode_combo.findData("batch")
            if batch_idx >= 0:
                self.mode_combo.setCurrentIndex(batch_idx)

    def _on_engine_changed(self, _index: int = 0) -> None:
        self._update_engine_indicator()
        self._update_mode_availability()
        self._update_language_availability()
        self._update_local_model_runtime_warning()
        self._update_remote_model_selector()
        self._update_import_engine_note()

    def _on_mode_changed(self, _index: int = 0) -> None:
        self._update_language_availability()
        self._update_remote_model_selector()

    def _on_model_changed(self, _index: int = 0) -> None:
        self._update_engine_indicator()
        self._update_mode_availability()
        self._update_language_availability()
        self._update_local_model_runtime_warning()

    def _on_model_dir_changed(self, _text: str = "") -> None:
        """React to model directory changes — update cached model info."""
        self._mark_local_model_refresh_stale()
        if not self._prime_local_model_views_from_available_cache():
            status = (
                "Checking the selected model directory in the background."
                if self._inventory_tab_is_visible()
                else "Open Local or Benchmark to verify this model directory in the background."
            )
            self._show_local_model_unverified_state(status)
        if self._inventory_tab_is_visible():
            self._schedule_local_model_auto_refresh(delay_ms=250)

    def _on_remote_model_changed(self, _index: int = 0) -> None:
        provider = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        if provider == DEFAULT_ENGINE:
            return
        value = str(self.remote_model_combo.currentData() or "")
        if not value:
            value = _REMOTE_MODEL_DEFAULTS.get(provider, "")
        self._remote_model_values[provider] = value
        self._update_language_availability()

    def _on_import_engine_changed(self, _index: int = 0) -> None:
        self._update_import_model_selector()
        self._update_import_engine_note()

    def _on_import_model_changed(self, _index: int = 0) -> None:
        if not hasattr(self, "import_model_combo"):
            return
        engine = str(self.import_engine_combo.currentData() or DEFAULT_ENGINE)
        value = str(self.import_model_combo.currentData() or "")
        if not value:
            value = self._import_model_value_for_engine(engine)
        self._import_model_values[engine] = value
        self._update_import_language_selector()
        self._update_import_engine_note()

    def _on_import_language_changed(self, _index: int = 0) -> None:
        if not hasattr(self, "import_language_combo"):
            return
        engine = str(self.import_engine_combo.currentData() or DEFAULT_ENGINE)
        model = str(
            self.import_model_combo.currentData()
            or self._import_model_value_for_engine(engine)
        )
        language = str(
            self.import_language_combo.currentData() or DEFAULT_LANGUAGE_MODE
        )
        self._import_language_values[self._import_language_key(engine, model)] = language
