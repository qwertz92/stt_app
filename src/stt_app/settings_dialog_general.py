"""Settings dialog: general mixin (split from settings_dialog.py)."""

from __future__ import annotations

import threading
from typing import ClassVar

from PySide6 import QtCore, QtWidgets

from . import local_runtime_support
from .config import (
    ASSEMBLYAI_STREAMING_MODEL_LABEL,
    CANARY_MODEL_SIZE,
    CUSTOM_VOCABULARY_SUPPORTED_SUMMARY,
    DEFAULT_CUSTOM_API_MODE,
    DEFAULT_ENGINE,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_MODEL_SIZE,
    DEVICE_AWARE_LOCAL_MODELS,
    FLUID_SPEECH_PRO_MODEL_SIZE,
    LANGUAGE_MODE_LABELS,
    LOCAL_ENGLISH_ONLY_MODELS,
    LOCAL_EXPLICIT_LANGUAGE_MODELS,
    LOCAL_GRANITE_CTC_MODEL_SIZES,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_ONNX_ASR_MODEL_SIZES,
    LOCAL_ONNX_MODEL_RUNTIME_LABELS,
    LOCAL_ONNX_MODEL_SIZES,
    LOCAL_WEBGPU_MODEL_SIZES,
    PARAKEET_MODEL_SIZES,
    VALID_ENGINES,
    VALID_INSERT_TARGETS,
    VALID_LANGUAGE_MODES,
    VALID_MODEL_SIZES,
    VALID_MODES,
    VALID_PASTE_MODES,
    language_modes_for_selection,
    onnx_auto_device_order,
    supports_custom_vocabulary,
    supports_streaming,
)
from .settings_dialog_helpers import (
    _CONCURRENT_MODE_UI_CHOICES,
    _INLINE_FIELD_BUTTON_SPACING_PX,
    _INSERT_TARGET_LABELS,
    _PASTE_MODE_LABELS,
    _REMOTE_MODEL_CHOICES,
    _REMOTE_MODEL_DEFAULTS,
    _THREAD_START_ERRORS,
    BENCHMARK_GPU_CPU_COMPARISON_LABEL,
    LOCAL_MODEL_LABELS,
    OPENROUTER_AUDIO_NOTE,
    _emit_background_signal,
    _WheelPassthroughComboBox,
    exception_reason,
    fill_engine_combo,
    hint_font,
    local_model_label,
    local_model_precision_label,
    local_model_short_label,
    mode_label,
    model_choices_for_engine,
    onnx_device_label,
    onnx_device_order_text,
    unlabelled_row_label,
)
from .settings_store import (
    _REMOTE_MODEL_FIELDS,
    LISTED_MODEL_ENGINES,
    AppSettings,
    apply_engine_model_selection,
    custom_endpoint_identity,
)

# How each engine that reads the custom vocabulary passes it on, named after
# the request field it ends up in. `{name}` is the model or provider as the
# screen names it. Which engines appear here is not a second list: keys absent
# from `CUSTOM_VOCABULARY_ENGINES` (plus `local`, whose faster-whisper runtime
# is the one that reads the terms) are never asked for, and a supported engine
# missing here would show an empty note, which
# `test_every_engine_that_uses_the_vocabulary_has_a_sentence` refuses.
_VOCABULARY_SUPPORTED_NOTES: dict[str, str] = {
    "local": (
        "{name} uses the custom vocabulary as faster-whisper's initial "
        "prompt, in batch and streaming."
    ),
    "assemblyai": (
        "{name} uses the custom vocabulary as its key-terms prompt, in batch "
        "and streaming."
    ),
    "deepgram": (
        "{name} uses the custom vocabulary as keyterm, in batch and streaming."
    ),
    "openai": "{name} uses the custom vocabulary as keywords, batch only.",
    "groq": "{name} uses the custom vocabulary as the request prompt (batch only).",
    "custom": (
        "{name} uses the custom vocabulary as the request prompt, or as a "
        "spelling instruction in the chat style (batch only)."
    ),
    # Enhanced only; Melia 1 gets the "ignores" sentence.
    "speechmatics": (
        "{name} uses the custom vocabulary as its additional vocabulary (batch only)."
    ),
    "mistral": (
        "{name} uses the custom vocabulary as context bias, tuned for "
        "English (batch only)."
    ),
    "meta": "{name} uses the custom vocabulary as its keywords (batch only).",
    "soniox": ("{name} uses the custom vocabulary as its context terms (batch only)."),
    "openrouter": (
        "{name} uses the custom vocabulary as the request prompt, which only "
        "some of the models behind it honour (batch only)."
    ),
}

# What the editable model field and its Refresh button say for each engine of
# `LISTED_MODEL_ENGINES`.
_MODEL_ID_PLACEHOLDERS: dict[str, str] = {
    "custom": "Model id, e.g. whisper-1",
    "openrouter": "Model id, e.g. openai/gpt-transcribe",
}
_REFRESH_TOOLTIPS: dict[str, str] = {
    "custom": (
        "Ask the custom endpoint which models it offers (GET /models), "
        "using the URL and key entered on the Providers tab. Save keeps "
        "the list for the next start."
    ),
    "openrouter": (
        "Ask OpenRouter which models can transcribe (its public list, "
        "GET /models?output_modalities=transcription; no key needed). Save "
        "keeps the list for the next start."
    ),
}

# Where the two things the Model row does not do are done. The tabs are named
# Models and Providers, so both sentences name a tab the user can see; they are
# appended to the notes already reserved under the Model combo rather than
# given lines of their own, because the Transcription tab has 4 px of its
# height budget left (measured).
_LOCAL_MODEL_DOWNLOAD_POINTER = "Download or remove local models on the Models tab."
# Shorter than "The API key for this provider is set ...": with that wording
# the Fun-ASR note needed 45 px of the 42 reserved at the dialog's minimum
# width, and "this provider" repeats what the row already shows.
_REMOTE_MODEL_KEY_POINTER = "The API key is set on the Providers tab."
# The Mode row's hint, per mode. The streaming one is the "why are the others
# missing" line for the filtered engine and model lists. Both are about as
# long, so a switch does not change the row's height.
_MODE_HINTS: dict[str, str] = {
    "batch": "Batch inserts the text when you stop. Every engine and model can do it.",
    "streaming": (
        "Streaming types text while you speak. Only engines and models that "
        "can stream are listed."
    ),
}


class _GeneralTabMixin:
    _GENERAL_FORM_ROW_SPACING_PX = 10
    _DYNAMIC_HINT_LINE_COUNT = 2

    @classmethod
    def _general_form_box(
        cls,
        title: str,
    ) -> tuple[QtWidgets.QGroupBox, QtWidgets.QFormLayout]:
        """Create a consistently spaced form section for the Transcription tab."""
        box = QtWidgets.QGroupBox(title)
        form = QtWidgets.QFormLayout(box)
        form.setContentsMargins(10, 10, 10, 10)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(cls._GENERAL_FORM_ROW_SPACING_PX)
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        return box, form

    @classmethod
    def _dynamic_hint_height(
        cls, label: QtWidgets.QLabel, lines: int | None = None
    ) -> int:
        """Height of the compact area used for changing hint text."""
        # Windows' offscreen/high-DPI font backend can need several pixels more
        # than two nominal line spacings for the same wrapped glyph bounds.
        # Keep that platform padding inside the reserved area so text never
        # clips while all following rows still remain stationary.
        count = cls._DYNAMIC_HINT_LINE_COUNT if lines is None else lines
        return label.fontMetrics().lineSpacing() * count + 10

    @classmethod
    def _reserve_dynamic_hint_height(
        cls, label: QtWidgets.QLabel, lines: int | None = None
    ) -> None:
        """Reserve a stable area (two lines unless told) for changing text.

        Measured with the label's own font, so call it after the font is set:
        the hints use `hint_font`, which grows with the system text size.
        """
        label.setFixedHeight(cls._dynamic_hint_height(label, lines))
        label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)

    def _build_general_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # --- Engine / Mode section ---
        engine_box, engine_form = self._general_form_box("Engine && Mode")

        # Mode comes first because it decides what the rows below offer: in
        # Streaming the engine and model lists show only what can stream
        # (`_update_mode_availability`). It used to sit below Vocabulary and
        # follow the model instead -- a greyed-out Streaming entry whose
        # reason was only a tooltip, and a silent flip to Batch when the
        # engine or model changed (owner's request 2026-11-24).
        self.mode_combo = _WheelPassthroughComboBox()
        # Built with the longer Streaming label (no selection yet counts as
        # experimental); `_update_mode_labels` shortens it for Nemotron.
        for value in VALID_MODES:
            self.mode_combo.addItem(mode_label(value, ""), value)
        self.mode_combo.setToolTip(
            "Streaming inserts only stable append-only text while speaking and "
            "suspends live insertion on focus change. Batch remains the recommended default."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.mode_hint_label = QtWidgets.QLabel(_MODE_HINTS[DEFAULT_MODE])
        self.mode_hint_label.setWordWrap(True)
        self._style_field_hint_label(self.mode_hint_label)
        engine_form.addRow(
            "Mode", self._field_with_hint(self.mode_combo, self.mode_hint_label)
        )

        self.engine_combo = _WheelPassthroughComboBox()
        fill_engine_combo(self.engine_combo, VALID_ENGINES)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        engine_hint = QtWidgets.QLabel(
            "Local runs on this PC. Cloud engines upload the audio to the "
            "provider you pick."
        )
        engine_hint.setWordWrap(True)
        self._style_field_hint_label(engine_hint)
        engine_form.addRow(
            "Engine", self._field_with_hint(self.engine_combo, engine_hint)
        )

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
        self.local_model_runtime_warning_label.setFont(hint_font())
        self.local_model_runtime_warning_label.setStyleSheet("color: #b71c1c;")
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
        # `activated` also fires when the picked entry is the current one,
        # which Qt answers by rewriting the editable line edit without any
        # other signal (a typed custom model would otherwise outlive the pick).
        self.remote_model_combo.activated.connect(self._on_remote_model_activated)
        # Only the custom endpoint has a button here: its models are whatever
        # the endpoint offers, fetched on request with the Providers tab's
        # typed (unsaved) URL and credentials. It sits beside the combo it
        # fills rather than on the Providers tab, because the result is read
        # and picked here. The list it returns is saved with the settings
        # (`custom_models`), so the combo offers it again after a restart.
        self.fetch_models_button = QtWidgets.QPushButton("Refresh")
        self.fetch_models_button.setToolTip(
            "Ask the custom endpoint which models it offers (GET /models), "
            "using the URL and key entered on the Providers tab. Save keeps "
            "the list for the next start."
        )
        self.fetch_models_button.clicked.connect(self._fetch_models)
        self._match_field_button_height(
            self.remote_model_combo, self.fetch_models_button
        )
        self.fetch_models_button.setVisible(False)
        remote_model_row = QtWidgets.QHBoxLayout()
        remote_model_row.setContentsMargins(0, 0, 0, 0)
        remote_model_row.setSpacing(_INLINE_FIELD_BUTTON_SPACING_PX)
        remote_model_row.addWidget(self.remote_model_combo, 1)
        remote_model_row.addWidget(self.fetch_models_button)
        self.remote_model_note_label = QtWidgets.QLabel("")
        self.remote_model_note_label.setWordWrap(True)
        self._style_field_hint_label(self.remote_model_note_label)
        self._reserve_dynamic_hint_height(self.remote_model_note_label)
        remote_model_layout.addLayout(remote_model_row)
        remote_model_layout.addWidget(self.remote_model_note_label)
        self.model_selector_stack.addWidget(remote_model_widget)

        engine_form.addRow("Model", self.model_selector_stack)

        # The ONNX Device picker lives in the Models tab's "Local runtime"
        # group (`_build_local_tab`): it is set once per machine, and on this
        # tab it cost two rows of height for most users' models, which ignore
        # it.

        self.language_combo = _WheelPassthroughComboBox()
        for value in VALID_LANGUAGE_MODES:
            self.language_combo.addItem(LANGUAGE_MODE_LABELS.get(value, value), value)
        # A saved language the selected engine does not offer is shown as the
        # engine's first choice (Auto) but is not the user's decision:
        # (shown value, saved language). Any change of the combo ends it.
        self._language_fallback: tuple[str, str] | None = None
        self.language_combo.currentIndexChanged.connect(self._forget_language_fallback)
        self.language_combo.activated.connect(self._forget_language_fallback)
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
        # The parsing rules were a static hint of their own under the field --
        # a third block of text for one row. The placeholder says the short
        # version while the field is empty, the tooltip the whole of it.
        self.custom_vocabulary_edit.setPlaceholderText(
            "Names and terms to spell correctly, e.g. Kubernetes, Splunk SOAR "
            "(comma or new line)"
        )
        self.custom_vocabulary_edit.setToolTip(
            "Enter up to 100 terms or phrases, separated by commas, semicolons, "
            "or new lines. Spaces inside a phrase are kept (for example, "
            "Splunk SOAR)."
        )
        # Whether the *selected* model is sent these terms. The field stays
        # editable either way: the terms are stored for whatever model is
        # picked next.
        self.vocabulary_support_label = QtWidgets.QLabel("")
        self.vocabulary_support_label.setWordWrap(True)
        self._style_field_hint_label(self.vocabulary_support_label)
        self._reserve_dynamic_hint_height(self.vocabulary_support_label)
        engine_form.addRow(
            "Vocabulary",
            self._field_with_hint(
                self.custom_vocabulary_edit,
                self.vocabulary_support_label,
            ),
        )

        self.streaming_full_final_check = QtWidgets.QCheckBox(
            "Re-transcribe the whole recording after streaming (faster-whisper)"
        )
        # Enabled only where it does something (`_update_streaming_full_final
        # _availability`); the hint that explained that moved in here.
        self.streaming_full_final_check.setToolTip(
            "After a local faster-whisper streaming session ends, transcribe "
            "the whole recording once more so the saved history entry uses "
            "the highest-quality pass. Stopping takes noticeably longer on "
            "long dictations. Inserted text is unaffected either way.\n"
            "Applies to local faster-whisper streaming only. When off, the "
            "history entry uses the live streaming text and stopping finishes "
            "faster."
        )
        engine_form.addRow(unlabelled_row_label(), self.streaming_full_final_check)

        self.concurrent_mode_combo = _WheelPassthroughComboBox()
        for value, label in _CONCURRENT_MODE_UI_CHOICES:
            self.concurrent_mode_combo.addItem(label, value)
        # The hint that sat under this combo is the tooltip's first paragraph
        # now; the combo's own choices already say "previous" four times.
        self.concurrent_mode_combo.setToolTip(
            "While busy: what happens when you press the recording hotkey "
            "again before the previous transcription finishes, and when "
            "finished results are inserted. Jobs run one at a time, finished "
            "results keep their recording order, and a finished transcription "
            "is never discarded.\n"
            "- Insert when idle: results are inserted once no transcription "
            "is running anymore.\n"
            "- Insert immediately: each result is inserted the moment it is "
            "ready, into the window captured for its recording.\n"
            "- History only: results are saved to history without inserting.\n"
            "- Cancel: stop the older transcription (a result that still "
            "finishes is kept in history)."
        )
        engine_form.addRow("While busy", self.concurrent_mode_combo)
        layout.addWidget(engine_box)

        # --- Text Insertion section ---
        paste_box, paste_form = self._general_form_box("Text Insertion")

        self.paste_mode_combo = _WheelPassthroughComboBox()
        for value in VALID_PASTE_MODES:
            self.paste_mode_combo.addItem(_PASTE_MODE_LABELS.get(value, value), value)
        self.paste_mode_combo.setToolTip(
            "Auto tries SendInput first and falls back to WM_PASTE. "
            "SendInput simulates the real Ctrl+V keyboard shortcut. "
            "WM_PASTE sends a paste message directly to the focused edit control; "
            "some modern apps ignore it."
        )
        self.paste_mode_hint_label = QtWidgets.QLabel(
            "Auto works for almost every app; change it only if nothing is pasted."
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
            "wherever you are working at that moment.\n"
            "The caret position inside the target is always the position at "
            "insert time; Windows cannot paste at a remembered caret offset."
        )
        paste_form.addRow("Insert Into", self.insert_target_combo)

        self.keep_clipboard_checkbox = QtWidgets.QCheckBox(
            "Leave the transcript in the clipboard after inserting it"
        )
        self.keep_clipboard_checkbox.setToolTip(
            "When enabled, the transcript remains in the clipboard after insertion. "
            "When disabled, the previous clipboard contents are restored. This "
            "only decides whether the finished transcript replaces your "
            "previous clipboard contents."
        )
        paste_form.addRow(unlabelled_row_label(), self.keep_clipboard_checkbox)
        layout.addWidget(paste_box)

        # The shared label column spanning Transcription, Hotkeys & Display
        # and Audio is applied by _build_audio_tab once all three exist.
        self._general_forms = (engine_form, paste_form)
        layout.addStretch(1)
        # "Transcription", not "General": this is where the engine, the
        # model, the language and the mode are chosen, and a tab called
        # General says nothing about that while "Local" and "Remote" --
        # now Models and Providers -- read as if they did.
        self.tabs.addTab(tab, "Transcription")

    # Shared with the overlay retranscribe dialog; the table itself lives in
    # settings_dialog_helpers so both callers label a model identically.
    _MODEL_LABELS: ClassVar[dict[str, str]] = LOCAL_MODEL_LABELS

    @staticmethod
    def _precision_label(model_name: str) -> str:
        return local_model_precision_label(model_name)

    def _model_label(self, model_name: str) -> str:
        return local_model_label(model_name)

    def _remote_model_value_for_provider(self, provider: str) -> str:
        normalized = str(provider or "").strip().lower()
        fallback = _REMOTE_MODEL_DEFAULTS.get(normalized, "")
        value = str(self._remote_model_values.get(normalized, fallback) or fallback)
        if normalized in LISTED_MODEL_ENGINES:
            # Free text: the service, not a roster, decides what is valid. A
            # blank is the default (none for the custom endpoint).
            return value.strip() or fallback
        valid_values = {
            item_value
            for item_value, _label in _REMOTE_MODEL_CHOICES.get(normalized, ())
        }
        if value not in valid_values:
            return fallback
        return value

    def _fetched_models_for(self, engine: str) -> tuple[str, ...]:
        """What the last Refresh listed for one of `LISTED_MODEL_ENGINES`."""
        if engine == "custom":
            return self._custom_fetched_models
        if engine == "openrouter":
            return self._openrouter_fetched_models
        return ()

    def _listed_model_candidates(self, engine: str, *extra: str) -> tuple[str, ...]:
        """A listed engine's known models: the given ones, the chosen one and
        whatever the last fetch returned."""
        return (
            *extra,
            self._remote_model_value_for_provider(engine),
            *self._fetched_models_for(engine),
        )

    def _import_model_choices(
        self,
        engine: str,
    ) -> tuple[tuple[str, str], ...]:
        return model_choices_for_engine(
            engine,
            self._listed_model_candidates(
                engine, str(self._import_model_values.get(engine, "") or "")
            ),
        )

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
            item_value for item_value, _label in self._import_model_choices(normalized)
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

    @staticmethod
    def _import_language_key(engine: str, model: str) -> tuple[str, str]:
        return (str(engine or "").strip().lower(), str(model or "").strip())

    def _import_language_scope(self) -> tuple[tuple[str, str], tuple[str, ...]]:
        """The Import tab's (engine, model) key and the languages that pair
        offers for an imported file."""
        engine = str(self.import_engine_combo.currentData() or DEFAULT_ENGINE)
        model = str(
            self.import_model_combo.currentData()
            or self._import_model_value_for_engine(engine)
        )
        return (
            self._import_language_key(engine, model),
            language_modes_for_selection(engine, model, "batch"),
        )

    def _update_import_language_selector(
        self,
        *,
        preferred_mode: str | None = None,
    ) -> None:
        if not hasattr(self, "import_language_combo"):
            return

        key, supported_modes = self._import_language_scope()
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
            "Used only for this imported file; it does not change the Transcription tab."
        )
        self.import_language_combo.setToolTip(self.import_language_note.text())

    def _apply_engine_model_selection(
        self,
        settings: AppSettings,
        engine: str,
        model_value: str,
    ) -> AppSettings:
        return apply_engine_model_selection(settings, engine, model_value)

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
        is_listed = provider in LISTED_MODEL_ENGINES
        choices = (
            model_choices_for_engine(provider, self._listed_model_candidates(provider))
            if is_listed
            else _REMOTE_MODEL_CHOICES.get(provider, ())
        )

        self.remote_model_combo.blockSignals(True)
        self.remote_model_combo.clear()
        self._set_remote_model_combo_editable(is_listed)
        self.fetch_models_button.setVisible(is_listed)
        if is_listed:
            self.remote_model_combo.lineEdit().setPlaceholderText(
                _MODEL_ID_PLACEHOLDERS[provider]
            )
            self.fetch_models_button.setToolTip(_REFRESH_TOOLTIPS[provider])

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
        if is_listed:
            self.remote_model_combo.setEditText(
                self._remote_model_value_for_provider(provider)
            )
        self.remote_model_combo.setEnabled(
            not self._assemblyai_streaming_selected(provider)
        )
        self._update_remote_model_note()
        self.remote_model_combo.blockSignals(False)

    def _assemblyai_streaming_selected(self, provider: str) -> bool:
        """AssemblyAI streams with one fixed model, so its picker is moot."""
        return provider == "assemblyai" and self.mode_combo.currentData() == "streaming"

    def _update_remote_model_note(self) -> None:
        """The two reserved lines under a remote engine's model row.

        A setup gap that makes dictation with the selected engine fail -- no
        key, a key marked for removal, Azure's missing endpoint, the custom
        endpoint's missing base URL -- takes the whole note, in red, because
        the model description does not matter until it is fixed. Called
        whenever the engine, a key or one of those fields changes; it only
        replaces text in a reserved area, so nothing moves.
        """
        if not hasattr(self, "remote_model_note_label"):
            return
        provider = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        if provider == DEFAULT_ENGINE:
            return
        # A batch-only engine in Streaming first: a key does not help it.
        issue = self._streaming_selection_issue() or self._remote_engine_setup_issue(
            provider
        )
        if issue is not None:
            note, error = issue, True
        else:
            note = self._remote_model_description(provider)
            error = (
                provider in LISTED_MODEL_ENGINES
                and self._model_list_notes.get(provider, ("", False))[1]
            )
        self.remote_model_note_label.setText(note)
        self.remote_model_note_label.setToolTip(note)
        self.remote_model_note_label.setStyleSheet(
            "color: #b71c1c; padding: 0;" if error else "color: #555; padding: 0;"
        )

    def _remote_model_description(self, provider: str) -> str:
        if provider in LISTED_MODEL_ENGINES:
            note, error = self._model_list_notes.get(provider, ("", False))
            note = note or self._model_list_default_note(provider)
            if provider == "openrouter" and not error:
                # Where the audio goes is said here, where the engine is
                # chosen, and not only in the Providers row's tooltip.
                note = f"{OPENROUTER_AUDIO_NOTE} {note}"
            return note
        note = (
            f"The selected {self._provider_label(provider)} model is used for "
            "batch dictation and audio imports; its stored API key is reused."
        )
        if self._assemblyai_streaming_selected(provider):
            note = (
                f"Streaming always uses {ASSEMBLYAI_STREAMING_MODEL_LABEL} "
                "Realtime. The selected "
                "model applies to batch transcription and audio imports."
            )
        elif provider == "deepgram":
            note = "Deepgram uses the selected model for batch and streaming transcription."
        elif provider == "elevenlabs":
            note = (
                "The selected model applies to batch dictation and audio imports. "
                "Scribe v3 Live exists, but is not yet wired into this app."
            )
        elif provider == "azure":
            # Only the endpoint is named here; the key is the sentence every
            # remote engine gets below, and saying both twice filled the two
            # reserved lines with one instruction.
            note = (
                "Cloud, batch-only. MAI-Transcribe 2 supports the most "
                "languages; Azure also needs an endpoint beside the key."
            )
        elif provider == "funasr":
            note = (
                "Cloud, batch-only, with 30 languages (German included) focused on "
                "Chinese and East/Southeast Asia."
            )
        # Every remote engine needs a key, and the tab that holds it is no
        # longer called Remote. Inside the two reserved lines: measured, the
        # longest of these notes plus this sentence needs 30 px of the 42.
        return f"{note} {_REMOTE_MODEL_KEY_POINTER}"

    def _model_list_default_note(self, engine: str) -> str:
        """A listed engine's note before this session refreshed its list."""
        count = len(self._fetched_models_for(engine))
        if count:
            return (
                f"Batch-only. {count} model{'s' if count != 1 else ''} listed "
                "at the last Refresh; a model id the list lacks can be typed."
            )
        if engine == "openrouter":
            return (
                "Batch-only. Type the model id, or press Refresh to list "
                "the transcription models (no key needed)."
            )
        return (
            "Batch-only. Type the model id, or press Refresh to list the "
            "endpoint's models; its URL is set on the Providers tab."
        )

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
            fallback = self._language_fallback
            if fallback is not None:
                return (
                    f"AssemblyAI streaming does not offer "
                    f"{LANGUAGE_MODE_LABELS.get(fallback[1], fallback[1])}; it "
                    "streams in Auto. The saved choice is kept."
                )
            return ""

        if engine == "local" and model in LOCAL_ENGLISH_ONLY_MODELS:
            # Named from the selection: there is more than one English-only
            # model, and the hard-coded name answered a Granite CTC selection
            # with a sentence about distil-large-v3.5.
            return (
                f"{local_model_short_label(model)} is an English-only model "
                "(only Auto and English are available)."
            )

        if engine == "local" and model == "cohere-transcribe-03-2026":
            return (
                "Cohere supports 14 explicit languages and does not provide "
                "automatic language detection."
            )

        if engine == "local" and model == CANARY_MODEL_SIZE:
            return (
                "Canary has no automatic detection: pick the language actually "
                "spoken. With the wrong one it translates instead of "
                "transcribing."
            )

        if engine == "local" and model in PARAKEET_MODEL_SIZES:
            return (
                "Parakeet is multilingual and detects the language itself; "
                "there is nothing to choose."
            )

        if engine == "local" and model in LOCAL_EXPLICIT_LANGUAGE_MODELS:
            # Only the Granite models reach here: Cohere and Canary have their
            # own branches above, and both of those really do lack Auto.
            return "This model supports Auto plus the languages documented for it."

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
                "AssemblyAI-STT-3.7 supports 32 languages at the highest accuracy. "
                "Universal-2 is the lower-cost choice for broader coverage."
            )

        if engine == "azure":
            # Trimmed from 186 to 137 characters: at the dialog's 581 px
            # minimum width the longer wording needed 45 px of the 42 reserved
            # here, so its last line was cut off with only the tooltip left to
            # read it in. It was the one note in this dialog that did.
            return (
                "Azure MAI-Transcribe is multilingual. 'Auto' detects the "
                "language; selecting one sends a locale hint. The list follows "
                "the selected model."
            )

        if engine == "custom":
            return (
                "Which languages work depends on the endpoint's model. 'Auto' "
                "sends no language; selecting one sends it as a hint."
            )

        if engine == "funasr":
            return (
                "Fun-ASR is multilingual across 30 languages. 'Auto' auto-detects; "
                "selecting one sends a language hint."
            )

        return ""

    def _forget_language_fallback(self, *_args: object) -> None:
        self._language_fallback = None

    def _update_language_availability(self, preferred_mode: str | None = None) -> None:
        supported_modes = self._language_modes_for_current_selection()
        shown_mode = str(self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE)
        fallback = self._language_fallback
        if preferred_mode:
            selected_mode = preferred_mode
        elif fallback is not None and shown_mode == fallback[0]:
            # Still the engine's stand-in for the saved language: offer the
            # saved one again (another engine or model may know it).
            selected_mode = fallback[1]
        else:
            selected_mode = shown_mode

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
        self._language_fallback = (
            None
            if selected_mode in supported_modes
            else (self._language_mode_shown(), selected_mode)
        )

        note = self._language_constraint_note()
        default_note = (
            "Auto lets the selected engine detect the language; choosing one "
            "sends an explicit recognition hint. The choices update with the "
            "selected engine and model."
        )
        self.language_note_label.setText(note or default_note)
        self.language_combo.setEnabled(len(supported_modes) > 1)
        self.language_combo.setToolTip(note or default_note)

    def _on_local_onnx_device_changed(self, _index: int) -> None:
        self._update_local_onnx_device_row()

    def _set_local_onnx_device_note(self, text: str) -> None:
        # Two reserved lines are not much room at a larger text size, so the
        # whole sentence goes into the tooltip as well -- the same answer
        # every other changing note in this dialog uses.
        self.local_onnx_device_note_label.setText(text)
        self.local_onnx_device_note_label.setToolTip(text)

    def _measured_onnx_device(self, model_name: str) -> str:
        """The device a benchmark measured as fastest for this model, if any.

        Read from the populated baseline rather than from the store: it is what
        the widgets describe, it is updated by the benchmark path in the same
        breath as the file, and `_update_local_onnx_device_row` also runs once
        during construction, before that attribute exists.
        """
        measured = getattr(self, "_populated_settings", None)
        stored = getattr(measured, "onnx_auto_preferred_devices", None) or {}
        device = str(stored.get(model_name, "") or "")
        # A device this model's runtime cannot reach changes nothing, so the
        # note must not announce it: ORT GenAI has no WebGPU provider, and a
        # stored "webgpu" for Nemotron can only come from a hand-edited file.
        return device if device in onnx_auto_device_order(model_name) else ""

    def _update_local_onnx_device_row(self) -> None:
        """Enable the device picker only where it has an effect.

        The row stays present and only changes enabled state and note text, so
        switching models never shifts the fields below it. It sits on the
        Models tab while the model is picked on the Transcription tab, so the
        note names the model it is talking about.
        """
        if not hasattr(self, "local_onnx_device_combo"):
            return
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        model_name = (
            str(self.model_combo.currentData() or "")
            if hasattr(self, "model_combo")
            else ""
        )
        applies = engine == "local" and model_name in DEVICE_AWARE_LOCAL_MODELS
        self.local_onnx_device_combo.setEnabled(applies)

        if engine != "local":
            # Not a sentence about the local model the hidden combo still
            # holds: with a cloud engine selected no local model runs at all.
            self._set_local_onnx_device_note("Only used by local models.")
            return
        name = local_model_short_label(model_name) if model_name else "This model"
        if not applies:
            if model_name in LOCAL_ONNX_ASR_MODEL_SIZES:
                # Not "the fastest local option": `tiny` is quicker, and the
                # claim was never true for Canary at all.
                self._set_local_onnx_device_note(
                    f"{name} always runs on the CPU through onnx-asr and "
                    "ignores this setting."
                )
                return
            if model_name in LOCAL_GRANITE_CTC_MODEL_SIZES:
                # Also CPU-only, but a different runtime: naming onnx-asr here
                # would point at a package this model never loads.
                self._set_local_onnx_device_note(
                    f"{name} always runs on the CPU through ONNX Runtime and "
                    "ignores this setting."
                )
                return
            # Names what decides instead. "faster-whisper uses its own device
            # setting" pointed at a setting this app does not have.
            self._set_local_onnx_device_note(
                f"{name} ignores this setting: only Cohere, Granite and Nemotron "
                "use it. Whisper models pick their device themselves (CUDA if "
                "present, otherwise CPU)."
            )
            return

        device = str(self.local_onnx_device_combo.currentData() or "auto")
        if device == "auto":
            self._set_local_onnx_device_note(
                f"{name}: {self._auto_device_note(model_name)}"
            )
            return
        if device == "cpu":
            self._set_local_onnx_device_note(
                f"{name}: forced to the CPU, the GPU is never tried. Faster for "
                "models whose encoder the GPU cannot run; slower for the rest."
            )
            return
        if model_name in LOCAL_NEMOTRON_MODEL_SIZES:
            self._set_local_onnx_device_note(
                f"{name} runs on ONNX Runtime GenAI, which has DirectML and CPU "
                "only: every GPU choice here means DirectML."
            )
            return
        self._set_local_onnx_device_note(
            f"{name}: forced to this device, failing instead of falling back to "
            "CPU, so a model the GPU cannot run errors rather than transcribes "
            "slowly."
        )

    def _auto_device_note(self, model_name: str) -> str:
        """What `auto` will do for this model, and how to change it."""
        order = onnx_auto_device_order(model_name)
        measured = self._measured_onnx_device(model_name)
        if measured and measured != order[0]:
            # "your benchmark", never "your last benchmark": the map is merged
            # across runs, so this entry can be older than the last run, which
            # may have measured other models only.
            return (
                f"Auto starts with {onnx_device_label(measured)}: the fastest "
                "device for this model in your benchmark. Run it again to "
                "update, or pick a device to override."
            )
        if measured:
            # Not "confirmed as the fastest": the first device also stands when
            # another one was quicker by less than `MEASURED_DEVICE_MIN_GAIN`.
            return (
                f"Tries {onnx_device_order_text(order)}. Your benchmark "
                f"measured nothing clearly faster than {onnx_device_label(measured)} "
                "for this model."
            )
        if model_name in LOCAL_NEMOTRON_MODEL_SIZES:
            # Shorter than it was: the note starts with the model's name now,
            # and with it the sentence needed a third line at the label's
            # 460 px minimum. That every GPU choice means DirectML is said by
            # the note of each GPU choice itself.
            return (
                "DirectML and CPU only. Run a benchmark with "
                f'"{BENCHMARK_GPU_CPU_COMPARISON_LABEL}" and Auto will start '
                "with the faster one."
            )
        return (
            f"Tries {onnx_device_order_text(order)}. Run a benchmark with "
            f'"{BENCHMARK_GPU_CPU_COMPARISON_LABEL}" and Auto will start with '
            "the device that was fastest."
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
        warning_style = "color: #b71c1c;"
        note_style = "color: #666666;"
        unavailable = (
            local_runtime_support.unavailable_reason(model_name)
            if engine == "local"
            else None
        )
        streaming_issue = (
            self._streaming_selection_issue() if engine == "local" else None
        )
        if unavailable:
            style = warning_style
            text = unavailable
        elif streaming_issue:
            style = warning_style
            text = streaming_issue
        elif engine == "local" and model_name in LOCAL_WEBGPU_MODEL_SIZES:
            style = note_style
            # The order Auto tries is no longer fixed -- a benchmark can put
            # CPU first -- and the ONNX Device row on the Models tab owns it
            # either way, so restating it here could only ever contradict it.
            text = (
                "Batch mode only. Runs on the ONNX Device set on the Models "
                "tab (the overlay shows the active one)."
            )
        elif engine == "local" and model_name == CANARY_MODEL_SIZE:
            style = note_style
            text = (
                "Batch mode only, CPU. Pick a language: this model has no "
                "auto-detect and would otherwise translate into English."
            )
        elif engine == "local" and model_name == FLUID_SPEECH_PRO_MODEL_SIZE:
            style = note_style
            # Measured 2026-11-25: German 3.25 against Parakeet v3's 6.50 and
            # English 3.57 against 4.89, at about three times its processing time.
            text = (
                "Batch mode only, CPU. Multilingual; more accurate than "
                "Parakeet in German and English, about three times slower. "
                "FluidVoice Labs, CC-BY-4.0."
            )
        elif engine == "local" and model_name in LOCAL_ONNX_ASR_MODEL_SIZES:
            style = note_style
            # Not "the fastest local model here": a Whisper size once measured
            # 0.033 against Parakeet's 0.043 in the same run.
            text = (
                "Batch mode only, CPU. Multilingual, no language selection "
                "needed; the recommended default."
            )
        elif engine == "local" and model_name in LOCAL_GRANITE_CTC_MODEL_SIZES:
            style = note_style
            # The casing and the missing punctuation are what the CTC head
            # writes, not a setting -- say so here rather than let it look
            # like a defect after the first dictation.
            text = (
                "Batch mode only, CPU. English only; writes lowercase text "
                "without punctuation."
            )
        elif engine == "local" and model_name in LOCAL_NEMOTRON_MODEL_SIZES:
            style = note_style
            text = (
                "Streams with a fixed 560 ms ONNX chunk. Runs on the ONNX "
                "Device set on the Models tab."
            )
        elif engine == "local" and model_name:
            style = note_style
            # The vocabulary half of this sentence moved to the Vocabulary
            # row's own note, which says it for every engine and is the row
            # the user is reading when the question comes up.
            text = "faster-whisper runs via CTranslate2 in batch and streaming."
        else:
            style = note_style
            text = " "

        if engine == "local" and model_name and not (unavailable or streaming_issue):
            # Where the download lives, now that the tab is called Models
            # rather than Local: this row offers models that may not be on
            # disk yet, and nothing beside it said where to get them. It goes
            # inside the two lines already reserved here rather than on a line
            # of its own -- measured, the longest combination needs 30 px of
            # the 42 reserved, at the dialog's 581 px minimum width as well.
            text = f"{text} {_LOCAL_MODEL_DOWNLOAD_POINTER}"
        self.local_model_runtime_warning_label.setStyleSheet(style)
        self.local_model_runtime_warning_label.setText(text)
        self.local_model_runtime_warning_label.setToolTip(text if text.strip() else "")

    def _update_custom_vocabulary_note(self) -> None:
        """Say whether the selected model is sent the custom vocabulary.

        Eight of the twelve selectable engines/runtimes have no biasing input
        at all, so terms typed for one of them do nothing and nothing said so
        -- the static hint listed all eleven names and required reading the
        list to find the one selected model in it. The label keeps a reserved
        two-line area and only its text and colour change, so no selection can
        move the fields below it.
        """
        if not hasattr(self, "vocabulary_support_label"):
            return
        # The remote engine's own model, not the local combo's: Speechmatics'
        # answer differs by model.
        engine, model = self._pending_engine_selection()
        # Named the way the screen names it: `local_model_short_label` for a
        # local model and the provider label for a remote one. A settings id
        # ('parakeet-tdt-0.6b-v3') matches nothing the user can see.
        name = (
            local_model_short_label(model)
            if engine == DEFAULT_ENGINE
            else self._provider_label(engine)
        )

        if not supports_custom_vocabulary(engine, model):
            # Amber, not the #b71c1c of a real failure: nothing is broken and
            # the terms stay stored for the next model.
            self.vocabulary_support_label.setStyleSheet("color: #b26a00; padding: 0;")
            text = (
                f"{name} ignores the custom vocabulary. Models that use it: "
                f"{CUSTOM_VOCABULARY_SUPPORTED_SUMMARY}."
            )
        else:
            self.vocabulary_support_label.setStyleSheet("color: #555; padding: 0;")
            text = _VOCABULARY_SUPPORTED_NOTES.get(engine, "").format(name=name)

        self.vocabulary_support_label.setText(text)
        # Two reserved lines are not much room at the dialog's minimum width,
        # so the whole sentence is the tooltip as well -- what every other
        # changing note in this dialog does.
        self.vocabulary_support_label.setToolTip(text)

    def _engine_selection_text(self, engine: str, model: str) -> str:
        """One engine/model pair the way the engine bar names it."""
        if engine == DEFAULT_ENGINE:
            runtime = (
                LOCAL_ONNX_MODEL_RUNTIME_LABELS.get(model, "ONNX")
                if model in LOCAL_ONNX_MODEL_SIZES
                else "faster-whisper"
            )
            name = local_model_short_label(model) if model else "local model"
            return f"{name} (on this PC, {runtime})"
        model_text = f" {model}" if model else ""
        return f"{self._provider_label(engine)}{model_text} (cloud)"

    def _pending_engine_selection(self) -> tuple[str, str]:
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        if engine == DEFAULT_ENGINE:
            model = (
                str(self.model_combo.currentData() or "")
                if hasattr(self, "model_combo")
                else ""
            )
        else:
            model = self._remote_model_value_for_provider(engine)
        return engine, model

    def _active_engine_selection(self) -> tuple[str, str]:
        """What the running app uses: the settings as last saved or loaded."""
        settings = self._loaded_settings
        engine = str(getattr(settings, "engine", "") or DEFAULT_ENGINE)
        if engine == DEFAULT_ENGINE:
            return engine, str(getattr(settings, "model_size", "") or "")
        field = _REMOTE_MODEL_FIELDS.get(engine, "")
        return engine, str(getattr(settings, field, "") or "") if field else ""

    def _update_engine_indicator(self) -> None:
        """The always-visible bar: what runs now, and what Save changes it to.

        It used to show the combo's selection alone, labelled "Engine:", so
        after picking another model it named something that was not running
        and would not run until Save.
        """
        if not hasattr(self, "engine_indicator"):
            return
        active_engine, active_model = self._active_engine_selection()
        pending_engine, pending_model = self._pending_engine_selection()
        text = f"Active: {self._engine_selection_text(active_engine, active_model)}"
        pending = (pending_engine, pending_model) != (active_engine, active_model)
        if pending:
            text += (
                "  \u2192  after Save: "
                f"{self._engine_selection_text(pending_engine, pending_model)}"
            )
        self.engine_indicator.setText(text)
        if pending:
            colors = "background-color: #fff3e0; color: #8a4b00;"
        elif pending_engine == DEFAULT_ENGINE:
            colors = "background-color: #e8f5e9; color: #1b5e20;"
        else:
            colors = "background-color: #e3f2fd; color: #0d47a1;"
        self.engine_indicator.setStyleSheet(
            "font-weight: bold; padding: 4px; border-radius: 4px; " + colors
        )

    def _streaming_mode_selected(self) -> bool:
        return (
            hasattr(self, "mode_combo") and self.mode_combo.currentData() == "streaming"
        )

    def _local_model_offered(self, model_name: str, current: str) -> bool:
        """Whether the local model list shows ``model_name`` in this mode.

        In Streaming only models that can stream, plus the current selection
        whatever it is: a selected local model is strict (AGENTS.md), so a
        saved batch-only model stays selected and is flagged by
        `_streaming_selection_issue` instead of being swapped for another.
        """
        return (
            not self._streaming_mode_selected()
            or model_name == current
            or supports_streaming(DEFAULT_ENGINE, model_name)
        )

    def _streaming_selection_issue(self) -> str | None:
        """Why the selected engine or model cannot stream, or None.

        Shown in red in the reserved note under the Model row; the
        controller refuses such a dictation with the same reason.
        """
        if not self._streaming_mode_selected():
            return None
        engine, model_name = self._pending_engine_selection()
        if supports_streaming(engine, model_name):
            return None
        if engine == DEFAULT_ENGINE:
            # The name the user recognises, not the raw settings value
            # ('parakeet-tdt-0.6b-v3' matches nothing on screen).
            name = local_model_short_label(model_name)
            return (
                f"{name} cannot stream; it works in Batch mode only. Pick a "
                "model from this list, or set Mode to Batch."
            )
        return (
            f"{self._provider_label(engine)} cannot stream; it works in Batch "
            "mode only. Pick Local, AssemblyAI or Deepgram, or set Mode to Batch."
        )

    def _update_mode_availability(self) -> None:
        """Let the engine and model lists offer what the selected mode can use.

        In Streaming, engines and local models that cannot stream are hidden
        (the Mode row's hint says so), except the current selection, which
        is never switched silently: it stays and is flagged in red.
        """
        self._update_mode_hint()
        self._update_mode_labels()
        streaming = self._streaming_mode_selected()
        current_engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        view = self.engine_combo.view()
        item_model = self.engine_combo.model()
        for index in range(self.engine_combo.count()):
            engine = self.engine_combo.itemData(index)
            if engine not in VALID_ENGINES:
                continue  # the separator and the "Cloud" header
            hidden = (
                streaming
                and engine != current_engine
                and not supports_streaming(str(engine))
            )
            view.setRowHidden(index, hidden)
            item = item_model.item(index) if hasattr(item_model, "item") else None
            if item is not None:
                # Disabled as well as hidden: the arrow keys and the wheel
                # on a closed combo still reach a hidden row, not a
                # disabled one.
                item.setEnabled(not hidden)
        if hasattr(self, "model_combo"):
            current_model = str(self.model_combo.currentData() or "")
            shown = [
                str(self.model_combo.itemData(index))
                for index in range(self.model_combo.count())
                if self.model_combo.itemData(index) is not None
            ]
            wanted = [
                name
                for name in VALID_MODEL_SIZES
                if self._local_model_offered(name, current_model)
            ]
            if sorted(shown) != sorted(wanted):
                self._refresh_model_combo()

    def _update_mode_labels(self) -> None:
        """Say "Experimental" only while the current selection is.

        Only the text changes; the stored value stays "streaming".
        """
        engine, model_name = self._pending_engine_selection()
        for index in range(self.mode_combo.count()):
            value = str(self.mode_combo.itemData(index))
            self.mode_combo.setItemText(index, mode_label(value, engine, model_name))

    def _update_mode_hint(self) -> None:
        if not hasattr(self, "mode_hint_label"):
            return
        mode = str(self.mode_combo.currentData() or DEFAULT_MODE)
        self.mode_hint_label.setText(_MODE_HINTS.get(mode, _MODE_HINTS[DEFAULT_MODE]))

    def _update_streaming_full_final_availability(self) -> None:
        """Enabled only for local faster-whisper streaming, where it acts."""
        if not hasattr(self, "streaming_full_final_check"):
            return
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        model_name = (
            str(self.model_combo.currentData() or "")
            if hasattr(self, "model_combo")
            else ""
        )
        applies = (
            engine == DEFAULT_ENGINE
            and bool(model_name)
            and model_name not in LOCAL_ONNX_MODEL_SIZES
            and self.mode_combo.currentData() == "streaming"
        )
        self.streaming_full_final_check.setEnabled(applies)

    def _on_engine_changed(self, _index: int = 0) -> None:
        self._update_engine_indicator()
        self._update_mode_availability()
        self._update_streaming_full_final_availability()
        self._update_language_availability()
        self._update_local_model_runtime_warning()
        self._update_local_onnx_device_row()
        self._update_custom_vocabulary_note()
        self._update_remote_model_selector()
        self._update_import_engine_note()

    def _on_mode_changed(self, _index: int = 0) -> None:
        self._update_mode_availability()
        self._update_language_availability()
        self._update_remote_model_selector()
        self._update_streaming_full_final_availability()
        self._update_local_model_runtime_warning()

    def _on_model_changed(self, _index: int = 0) -> None:
        self._update_engine_indicator()
        self._update_mode_availability()
        self._update_streaming_full_final_availability()
        self._update_language_availability()
        self._update_local_model_runtime_warning()
        self._update_local_onnx_device_row()
        self._update_custom_vocabulary_note()

    def _on_model_dir_changed(self, _text: str = "") -> None:
        """React to model directory changes — update cached model info."""
        self._mark_local_model_refresh_stale()
        if not self._prime_local_model_views_from_available_cache():
            status = (
                "Checking the selected model directory in the background."
                if self._inventory_tab_is_visible()
                else "Open Models or Benchmark to verify this model directory in the background."
            )
            self._show_local_model_unverified_state(status)
        if self._inventory_tab_is_visible():
            self._schedule_local_model_auto_refresh(delay_ms=250)

    def _set_remote_model_combo_editable(self, editable: bool) -> None:
        """Only the engines of `LISTED_MODEL_ENGINES` take a typed model id."""
        combo = self.remote_model_combo
        if combo.isEditable() == editable:
            return
        combo.setEditable(editable)
        # A fetched model id can be long, and the dialog's minimum width
        # follows every page's minimum and never shrinks back; the other
        # providers' fixed rosters keep sizing to their captions.
        combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
            if editable
            else QtWidgets.QComboBox.AdjustToContentsOnFirstShow
        )
        combo.setMinimumContentsLength(24 if editable else 0)
        if editable:
            combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
            combo.lineEdit().textEdited.connect(self._on_listed_model_text_edited)

    def _on_listed_model_text_edited(self, text: str) -> None:
        provider = str(self.engine_combo.currentData() or "")
        if provider not in LISTED_MODEL_ENGINES:
            return
        self._remote_model_values[provider] = str(text or "").strip()
        self._update_engine_indicator()
        self._schedule_unsaved_changes_refresh()

    def _models_fetch_snapshot(self, engine: str) -> dict[str, str]:
        """What a model fetch needs, read on the GUI thread."""
        key_field = self._provider_key_edits.get(engine)
        api_key = self._resolve_api_key(engine, key_field) if key_field else ""
        if engine == "openrouter":
            # The list is public: the key is sent when there is one, and
            # none is needed.
            return {"api_key": api_key}
        return {
            "api_key": api_key,
            "endpoint": self.custom_endpoint_edit.text().strip(),
            "api_mode": str(
                self.custom_api_mode_combo.currentData() or DEFAULT_CUSTOM_API_MODE
            ),
            "key_command": self.custom_key_command_edit.text().strip(),
        }

    def _fetch_models(self) -> None:
        """List the selected engine's models on a worker thread and fill the
        combo."""
        if self._active_models_fetch_thread is not None:
            return
        engine = str(self.engine_combo.currentData() or "")
        if engine not in LISTED_MODEL_ENGINES:
            return
        snapshot = self._models_fetch_snapshot(engine)
        self._models_fetch_engine = engine
        self._models_fetch_endpoint = snapshot.get("endpoint", "")
        self._models_fetch_id += 1
        fetch_id = self._models_fetch_id
        self.fetch_models_button.setEnabled(False)
        self._set_model_list_note(
            engine,
            "Fetching OpenRouter's models..."
            if engine == "openrouter"
            else "Fetching the endpoint's models...",
        )
        worker = threading.Thread(
            target=self._run_models_fetch,
            args=(fetch_id, engine, snapshot),
            name="stt_app_settings_models_fetch",
            daemon=True,
        )
        self._active_models_fetch_thread = worker
        # The busy marker is set; a start that raises would leave it set for
        # the life of the dialog (see the connection test's guard).
        try:
            worker.start()
        except _THREAD_START_ERRORS as exc:
            self._on_models_fetched(
                fetch_id,
                False,
                f"Could not start the model fetch: {exception_reason(exc)}",
            )

    def _run_models_fetch(
        self, fetch_id: int, engine: str, snapshot: dict[str, str]
    ) -> None:
        try:
            if engine == "openrouter":
                from .transcriber.openrouter_provider import OpenRouterTranscriber

                transcriber = OpenRouterTranscriber(listing_only=True, **snapshot)
            else:
                from .transcriber.custom_endpoint_provider import (
                    CustomEndpointTranscriber,
                )

                transcriber = CustomEndpointTranscriber(**snapshot)
            result: object = tuple(model.id for model in transcriber.list_models())
            ok = True
        except Exception as exc:
            ok, result = False, str(exc)
        _emit_background_signal(self, "models_fetch_finished", fetch_id, ok, result)

    @QtCore.Slot(int, bool, object)
    def _on_models_fetched(self, fetch_id: int, ok: bool, result: object) -> None:
        if fetch_id != self._models_fetch_id:
            return
        engine = self._models_fetch_engine
        subject = "OpenRouter" if engine == "openrouter" else "The endpoint"
        self._active_models_fetch_thread = None
        self.fetch_models_button.setEnabled(True)
        if not ok:
            self._set_model_list_note(
                engine, f"Could not fetch models: {result}", error=True
            )
            return
        models = tuple(result) if isinstance(result, tuple) else ()
        if not models:
            # An outage or a key without rights answers like this too; the
            # list from the last good answer stays rather than going with
            # the next Save unannounced.
            kept = len(self._fetched_models_for(engine))
            text = f"{subject} listed no models this key can use." + (
                f" The {kept} model{'s' if kept != 1 else ''} listed before are kept."
                if kept
                else ""
            )
            self._set_model_list_note(engine, text, error=True)
            return
        if engine == "openrouter":
            self._openrouter_fetched_models = models
        else:
            self._custom_models_by_endpoint[
                custom_endpoint_identity(self._models_fetch_endpoint)
            ] = models
            self._custom_fetched_models = self._custom_models_for_shown_endpoint()
        chosen = self._remote_model_value_for_provider(engine)
        if chosen and chosen not in models:
            text = (
                f"{subject} offers {len(models)} models, but not "
                f"'{chosen}'. Pick one from the list."
            )
        else:
            text = f"{subject} offers {len(models)} models. Pick one from the list."
        self._set_model_list_note(engine, text)
        if not chosen:
            self._remote_model_values[engine] = models[0]
        self._update_remote_model_selector()
        self._update_import_model_selector()
        self._update_engine_indicator()
        self._schedule_unsaved_changes_refresh()

    def _custom_models_for_shown_endpoint(self) -> tuple[str, ...]:
        return self._custom_models_by_endpoint.get(
            custom_endpoint_identity(self.custom_endpoint_edit.text()), ()
        )

    def _on_custom_endpoint_changed(self, _text: str = "") -> None:
        """Offer the list the Base URL field's endpoint gave, and only that.

        Changed from A to B and saved, the reopened dialog offered A's
        models as B's ("2 models listed at the last Refresh"). Lists are
        kept per URL for the session, so correcting a typo back to A brings
        A's list back without a new Refresh.
        """
        models = self._custom_models_for_shown_endpoint()
        if models != self._custom_fetched_models:
            self._custom_fetched_models = models
            # A Refresh note described the previous endpoint.
            self._model_list_notes.pop("custom", None)
            self._update_remote_model_selector()
            self._update_import_model_selector()
        else:
            self._update_remote_model_note()

    def _set_model_list_note(
        self, engine: str, text: str, *, error: bool = False
    ) -> None:
        """The note under the model row reports the fetch while it is shown."""
        self._model_list_notes[engine] = (text, error)
        self._update_remote_model_note()

    def _on_remote_model_changed(self, _index: int = 0) -> None:
        provider = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        if provider == DEFAULT_ENGINE:
            return
        value = str(self.remote_model_combo.currentData() or "")
        if not value:
            value = _REMOTE_MODEL_DEFAULTS.get(provider, "")
        self._remote_model_values[provider] = value
        self._update_language_availability()
        self._update_engine_indicator()
        self._update_custom_vocabulary_note()
        # A model can be unavailable in the chosen region (Melia 1 in au1).
        self._update_remote_model_note()

    def _on_remote_model_activated(self, _index: int = 0) -> None:
        self._on_remote_model_changed()
        self._schedule_unsaved_changes_refresh()

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
        # Signals are blocked while the selector rebuilds the combo, so this
        # runs for the user's own pick only: it ends the recorded language's
        # preselection for this visit (`_preselect_recorded_import_language`).
        self._import_language_chosen = True
        key, _supported_modes = self._import_language_scope()
        self._import_language_values[key] = str(
            self.import_language_combo.currentData() or DEFAULT_LANGUAGE_MODE
        )
