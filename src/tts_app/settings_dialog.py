from __future__ import annotations

import threading

from PySide6 import QtCore, QtGui, QtWidgets

from .config import (
    DEFAULT_ENGINE,
    DEFAULT_GROQ_MODEL,
    DEFAULT_HOTKEY,
    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_PASTE_MODE,
    DOC_MODELS_PATH,
    GROQ_MODELS,
    OPENAI_MODELS,
    STREAMING_ENGINES,
    VALID_ENGINES,
    VALID_LANGUAGE_MODES,
    VALID_MODES,
    VALID_MODEL_SIZES,
    VALID_PASTE_MODES,
)
from .hotkey import parse_hotkey
from .logger import AppLogger
from .secret_store import SecretStore
from .settings_store import AppSettings, SettingsStore
from .transcriber.local_faster_whisper import find_cached_models


class SettingsDialog(QtWidgets.QDialog):
    connection_test_finished = QtCore.Signal(int, bool, str)

    def __init__(
        self,
        settings_store: SettingsStore,
        secret_store: SecretStore,
        app_logger: AppLogger,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_store = settings_store
        self._secret_store = secret_store
        self._app_logger = app_logger
        self._loaded_settings = self._settings_store.load()
        self._connection_test_id = 0
        self._active_connection_test_thread: threading.Thread | None = None

        self.setWindowTitle("Dictation Settings")
        self.setModal(True)
        self.resize(580, 620)

        self.connection_test_finished.connect(self._on_connection_test_finished)
        self._build_ui()
        self._populate(self._loaded_settings)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # --- Engine indicator bar (always visible) ---
        self.engine_indicator = QtWidgets.QLabel()
        self.engine_indicator.setAlignment(QtCore.Qt.AlignCenter)
        self.engine_indicator.setStyleSheet(
            "font-weight: bold; padding: 4px; border-radius: 4px;"
        )

        # --- Tab widget ---
        self.tabs = QtWidgets.QTabWidget()
        self._build_general_tab()
        self._build_local_tab()
        self._build_remote_tab()

        # --- Status bar for save confirmation ---
        self.save_status = QtWidgets.QLabel("")
        self.save_status.setAlignment(QtCore.Qt.AlignCenter)
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(lambda: self.save_status.setText(""))

        # --- Bottom buttons ---
        self.copy_diag_button = QtWidgets.QPushButton("Copy diagnostics")
        self.copy_diag_button.clicked.connect(self._copy_diagnostics)

        save_button = QtWidgets.QPushButton("Save")
        cancel_button = QtWidgets.QPushButton("Cancel")
        save_button.clicked.connect(self._save)
        cancel_button.clicked.connect(self.reject)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.copy_diag_button)
        buttons.addStretch(1)
        buttons.addWidget(save_button)
        buttons.addWidget(cancel_button)

        root = QtWidgets.QVBoxLayout(self)
        root.addWidget(self.engine_indicator)
        root.addWidget(self.tabs)
        root.addWidget(self.save_status)
        root.addLayout(buttons)

    # --- General tab ---

    def _build_general_tab(self) -> None:
        tab = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(tab)

        self.hotkey_edit = QtWidgets.QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        if hasattr(self.hotkey_edit, "setClearButtonEnabled"):
            self.hotkey_edit.setClearButtonEnabled(True)
        hotkey_hint = QtWidgets.QLabel(
            "Click the hotkey field and press the combination to record it."
        )
        hotkey_hint.setStyleSheet("color: #555;")

        self.language_combo = QtWidgets.QComboBox()
        language_labels = {
            "auto": "Auto",
            "de": "German",
            "en": "English",
        }
        for value in VALID_LANGUAGE_MODES:
            self.language_combo.addItem(language_labels.get(value, value), value)

        self.engine_combo = QtWidgets.QComboBox()
        engine_labels = {
            "local": "Local (faster-whisper)",
            "assemblyai": "Remote (AssemblyAI)",
            "groq": "Remote (Groq)",
            "openai": "Remote (OpenAI)",
            "deepgram": "Remote (Deepgram)",
        }
        for value in VALID_ENGINES:
            self.engine_combo.addItem(engine_labels.get(value, value), value)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)

        self.mode_combo = QtWidgets.QComboBox()
        mode_labels = {
            "batch": "Batch",
            "streaming": "Streaming (Experimental)",
        }
        for value in VALID_MODES:
            self.mode_combo.addItem(mode_labels.get(value, value), value)
        self.mode_combo.setToolTip(
            "Streaming is experimental: live insertion while speaking, "
            "auto-abort on focus change. Batch remains the recommended default."
        )

        self.paste_mode_combo = QtWidgets.QComboBox()
        paste_mode_labels = {
            "auto": "Auto (SendInput -> WM_PASTE)",
            "wm_paste": "WM_PASTE only",
            "send_input": "SendInput only",
        }
        for value in VALID_PASTE_MODES:
            self.paste_mode_combo.addItem(
                paste_mode_labels.get(value, value), value
            )

        self.vad_checkbox = QtWidgets.QCheckBox("Enable energy-based auto-stop")
        self.save_wav_checkbox = QtWidgets.QCheckBox("Save last WAV for debugging")
        self.keep_clipboard_checkbox = QtWidgets.QCheckBox(
            "Keep transcript in clipboard after transcription"
        )

        form.addRow("Hotkey", self.hotkey_edit)
        form.addRow("", hotkey_hint)
        form.addRow("Engine", self.engine_combo)
        form.addRow("Language", self.language_combo)
        form.addRow("Mode", self.mode_combo)
        form.addRow("Paste Mode", self.paste_mode_combo)
        form.addRow("", self.vad_checkbox)
        form.addRow("", self.save_wav_checkbox)
        form.addRow("", self.keep_clipboard_checkbox)

        self.tabs.addTab(tab, "General")

    # --- Local tab ---

    def _build_local_tab(self) -> None:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        form = QtWidgets.QFormLayout()

        self.model_combo = QtWidgets.QComboBox()
        form.addRow("Model Size", self.model_combo)

        self.model_dir_edit = QtWidgets.QLineEdit()
        self.model_dir_edit.setPlaceholderText(
            "Leave empty for default HuggingFace cache"
        )
        self.model_dir_edit.setToolTip(
            "Custom directory for model storage (download_root).\n"
            "When set, all models are cached here instead of the default \n"
            "HuggingFace cache (~/.cache/huggingface/hub/).\n"
            "Use the download script: python scripts/download_model.py"
        )
        self.model_dir_browse = QtWidgets.QPushButton("Browse...")
        self.model_dir_browse.setFixedWidth(80)
        self.model_dir_browse.clicked.connect(self._browse_model_dir)
        self.model_dir_edit.textChanged.connect(self._on_model_dir_changed)
        model_dir_layout = QtWidgets.QHBoxLayout()
        model_dir_layout.addWidget(self.model_dir_edit, 1)
        model_dir_layout.addWidget(self.model_dir_browse)
        form.addRow("Model Dir", model_dir_layout)

        self.offline_mode_checkbox = QtWidgets.QCheckBox(
            "Offline mode (use cached models only, no internet)"
        )
        self.offline_mode_checkbox.setToolTip(
            "When enabled, sets local_files_only=True so faster-whisper never "
            "attempts to download models. The model must already be cached "
            "locally (see README for offline setup instructions)."
        )
        form.addRow("", self.offline_mode_checkbox)

        layout.addLayout(form)

        # Local models info
        local_models_box = QtWidgets.QGroupBox("Local Models")
        local_models_layout = QtWidgets.QVBoxLayout(local_models_box)
        self.local_models_label = QtWidgets.QLabel("Scanning...")
        self.local_models_label.setWordWrap(True)
        local_models_layout.addWidget(self.local_models_label)
        self._refresh_local_models_label()

        layout.addWidget(local_models_box)
        layout.addStretch(1)
        self.tabs.addTab(tab, "Local")

    # --- Remote tab ---

    def _build_remote_tab(self) -> None:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        # Groq model selector
        groq_box = QtWidgets.QGroupBox("Groq Settings")
        groq_form = QtWidgets.QFormLayout(groq_box)
        self.groq_model_combo = QtWidgets.QComboBox()
        groq_model_labels = {
            "whisper-large-v3": "whisper-large-v3 (best quality, $0.111/hr)",
            "whisper-large-v3-turbo": "whisper-large-v3-turbo (faster, $0.04/hr)",
        }
        for value in GROQ_MODELS:
            self.groq_model_combo.addItem(
                groq_model_labels.get(value, value), value
            )
        groq_form.addRow("Groq Model", self.groq_model_combo)
        layout.addWidget(groq_box)

        # OpenAI model selector
        openai_box = QtWidgets.QGroupBox("OpenAI Settings")
        openai_form = QtWidgets.QFormLayout(openai_box)
        self.openai_model_combo = QtWidgets.QComboBox()
        openai_model_labels = {
            "gpt-4o-mini-transcribe": "gpt-4o-mini-transcribe (fast, low cost)",
            "gpt-4o-transcribe": "gpt-4o-transcribe (higher quality)",
            "whisper-1": "whisper-1 (legacy whisper model)",
        }
        for value in OPENAI_MODELS:
            self.openai_model_combo.addItem(
                openai_model_labels.get(value, value), value
            )
        openai_form.addRow("OpenAI Model", self.openai_model_combo)
        layout.addWidget(openai_box)

        # API keys
        provider_box = QtWidgets.QGroupBox("Remote Provider API Keys")
        provider_layout = QtWidgets.QFormLayout(provider_box)

        self.assemblyai_key_edit = QtWidgets.QLineEdit()
        self.groq_key_edit = QtWidgets.QLineEdit()
        self.openai_key_edit = QtWidgets.QLineEdit()
        self.deepgram_key_edit = QtWidgets.QLineEdit()
        for field in (
            self.assemblyai_key_edit,
            self.groq_key_edit,
            self.openai_key_edit,
            self.deepgram_key_edit,
        ):
            field.setEchoMode(QtWidgets.QLineEdit.Password)
            field.setPlaceholderText("Stored in Windows Credential Manager")

        provider_layout.addRow("AssemblyAI", self.assemblyai_key_edit)
        provider_layout.addRow("Groq", self.groq_key_edit)
        provider_layout.addRow("OpenAI", self.openai_key_edit)
        provider_layout.addRow("Deepgram", self.deepgram_key_edit)
        provider_note = QtWidgets.QLabel(
            "Keys are saved in Windows Credential Manager via keyring."
        )
        provider_note.setStyleSheet("color: #555;")
        provider_layout.addRow(provider_note)

        # Test connection
        self.test_conn_button = QtWidgets.QPushButton("Test Connection")
        self.test_conn_button.setToolTip(
            "Test the selected remote provider's API key and network connectivity."
        )
        self.test_conn_button.clicked.connect(self._test_connection)
        self.test_conn_result = QtWidgets.QLabel("")
        self.test_conn_result.setWordWrap(True)
        provider_layout.addRow(self.test_conn_button, self.test_conn_result)

        layout.addWidget(provider_box)
        layout.addStretch(1)
        self.tabs.addTab(tab, "Remote")

    # ------------------------------------------------------------------
    # Model combo helpers
    # ------------------------------------------------------------------

    _MODEL_LABELS: dict[str, str] = {
        "tiny": "tiny (~75 MB)",
        "base": "base (~141 MB)",
        "small": "small (~484 MB)",
        "medium": "medium (~1.4 GB)",
        "large-v3": "large-v3 (~3 GB, multilingual)",
        "large-v3-turbo": "large-v3-turbo (~809 MB, multilingual, fast)",
        "distil-large-v3.5": "distil-large-v3.5 (~756 MB, English only, improved)",
    }

    def _refresh_model_combo(self, selected: str | None = None) -> None:
        """Rebuild model combo: downloaded models on top, separator, rest below."""
        model_dir = self.model_dir_edit.text().strip()
        try:
            cached = set(find_cached_models(model_dir))
        except Exception:
            cached = set()

        current_data = selected or str(self.model_combo.currentData() or "")

        self.model_combo.blockSignals(True)
        self.model_combo.clear()

        downloaded = [m for m in VALID_MODEL_SIZES if m in cached]
        not_downloaded = [m for m in VALID_MODEL_SIZES if m not in cached]

        for value in downloaded:
            label = self._MODEL_LABELS.get(value, value)
            self.model_combo.addItem(f"\u2713 {label}", value)

        if downloaded and not_downloaded:
            self.model_combo.insertSeparator(self.model_combo.count())

        for value in not_downloaded:
            label = self._MODEL_LABELS.get(value, value)
            self.model_combo.addItem(f"   {label}", value)

        if current_data:
            idx = self.model_combo.findData(current_data)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)

        self.model_combo.blockSignals(False)

    def _refresh_local_models_label(self) -> None:
        """Scan for locally cached models and update the label."""
        model_dir = self.model_dir_edit.text().strip()
        try:
            cached = find_cached_models(model_dir)
        except Exception:
            cached = []

        if cached:
            self.local_models_label.setText(
                f"Available locally: {', '.join(cached)}"
            )
            self.local_models_label.setStyleSheet("color: #1b5e20;")
        else:
            self.local_models_label.setText(
                "No local models found. Models will be downloaded on first use.\n"
                f"See {DOC_MODELS_PATH} if downloads are blocked."
            )
            self.local_models_label.setStyleSheet("color: #b71c1c;")

    # ------------------------------------------------------------------
    # Engine indicator
    # ------------------------------------------------------------------

    def _update_engine_indicator(self) -> None:
        """Update the always-visible engine indicator bar."""
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        if engine == "local":
            self.engine_indicator.setText("Engine: LOCAL (faster-whisper)")
            self.engine_indicator.setStyleSheet(
                "font-weight: bold; padding: 4px; border-radius: 4px; "
                "background-color: #e8f5e9; color: #1b5e20;"
            )
        else:
            label = engine.capitalize()
            self.engine_indicator.setText(f"Engine: REMOTE ({label})")
            self.engine_indicator.setStyleSheet(
                "font-weight: bold; padding: 4px; border-radius: 4px; "
                "background-color: #e3f2fd; color: #0d47a1;"
            )

    def _update_mode_availability(self) -> None:
        """Enable/disable streaming option based on the selected engine."""
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        supports_streaming = engine in STREAMING_ENGINES
        streaming_idx = self.mode_combo.findData("streaming")

        if streaming_idx < 0:
            return

        # Disable the streaming item in the combo model (greys it out).
        model = self.mode_combo.model()
        item = model.item(streaming_idx)
        if item is not None:
            if supports_streaming:
                item.setEnabled(True)
                item.setToolTip("")
            else:
                item.setEnabled(False)
                item.setToolTip(
                    f"Streaming is not supported by the {engine} provider. "
                    "Use local or AssemblyAI for streaming."
                )

        # If streaming is selected but not supported, switch to batch.
        if not supports_streaming and self.mode_combo.currentData() == "streaming":
            batch_idx = self.mode_combo.findData("batch")
            if batch_idx >= 0:
                self.mode_combo.setCurrentIndex(batch_idx)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_engine_changed(self, _index: int = 0) -> None:
        self._update_engine_indicator()
        self._update_mode_availability()

    def _on_model_dir_changed(self, _text: str = "") -> None:
        """React to model directory changes — update cached model info."""
        self._refresh_local_models_label()
        self._refresh_model_combo()

    def _test_connection(self) -> None:
        """Test connectivity for the selected remote provider."""
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)

        if engine == DEFAULT_ENGINE:
            self._set_test_connection_feedback(
                "Local provider \u2014 no connection test needed.",
                "#555",
            )
            return

        tester, error_text = self._build_connection_tester(engine)
        if tester is None:
            if error_text:
                self._set_test_connection_feedback(error_text, "#b71c1c")
            else:
                self._set_test_connection_feedback(
                    f"Connection test not yet implemented for {engine}.",
                    "#555",
                )
            return

        self._connection_test_id += 1
        test_id = self._connection_test_id
        self.test_conn_button.setEnabled(False)
        self._set_test_connection_feedback("Testing...", "#555")
        worker = threading.Thread(
            target=self._run_connection_test_worker,
            args=(test_id, tester),
            name="tts_app_settings_connection_test",
            daemon=True,
        )
        self._active_connection_test_thread = worker
        worker.start()

    def _build_connection_tester(self, engine: str):
        if engine == "assemblyai":
            api_key = self._resolve_api_key("assemblyai", self.assemblyai_key_edit)
            if not api_key:
                return (
                    None,
                    "No API key entered. Enter a key above first.",
                )

            from .transcriber.assemblyai_provider import AssemblyAITranscriber

            transcriber = AssemblyAITranscriber(api_key=api_key)
            return transcriber.test_connection, None

        if engine == "groq":
            api_key = self._resolve_api_key("groq", self.groq_key_edit)
            if not api_key:
                return (
                    None,
                    "No API key entered. Enter a key above first.",
                )

            from .transcriber.groq_provider import GroqTranscriber

            transcriber = GroqTranscriber(api_key=api_key)
            return transcriber.test_connection, None

        if engine == "openai":
            api_key = self._resolve_api_key("openai", self.openai_key_edit)
            if not api_key:
                return (
                    None,
                    "No API key entered. Enter a key above first.",
                )

            from .transcriber.openai_provider import OpenAITranscriber

            transcriber = OpenAITranscriber(
                api_key=api_key,
                model=str(
                    self.openai_model_combo.currentData() or DEFAULT_OPENAI_MODEL
                ),
            )
            return transcriber.test_connection, None

        if engine == "deepgram":
            api_key = self._resolve_api_key("deepgram", self.deepgram_key_edit)
            if not api_key:
                return (
                    None,
                    "No API key entered. Enter a key above first.",
                )

            from .transcriber.deepgram_provider import DeepgramTranscriber

            transcriber = DeepgramTranscriber(api_key=api_key)
            return transcriber.test_connection, None

        return None, None

    def _resolve_api_key(self, provider: str, key_field: QtWidgets.QLineEdit) -> str:
        api_key = key_field.text().strip()
        if api_key:
            return api_key
        return self._secret_store.get_api_key(provider) or ""

    def _run_connection_test_worker(self, test_id: int, tester) -> None:
        try:
            ok, msg = tester()
        except Exception as exc:
            ok, msg = False, f"Test failed: {exc}"
        self.connection_test_finished.emit(test_id, bool(ok), str(msg))

    @QtCore.Slot(int, bool, str)
    def _on_connection_test_finished(self, test_id: int, ok: bool, msg: str) -> None:
        if test_id != self._connection_test_id:
            return
        self.test_conn_button.setEnabled(True)
        self._active_connection_test_thread = None
        if ok:
            self._set_test_connection_feedback(f"\u2713 {msg}", "#1b5e20")
        else:
            self._set_test_connection_feedback(f"\u2717 {msg}", "#b71c1c")

    def _set_test_connection_feedback(self, text: str, color: str) -> None:
        self.test_conn_result.setText(text)
        self.test_conn_result.setStyleSheet(f"color: {color};")

    # ------------------------------------------------------------------
    # Populate / select helpers
    # ------------------------------------------------------------------

    def _populate(self, settings: AppSettings) -> None:
        self.hotkey_edit.setKeySequence(
            QtGui.QKeySequence(
                _app_hotkey_to_qt_hotkey_text(settings.hotkey)
            )
        )
        # Model Dir must be set before refreshing the model combo so it can
        # scan the correct directory for cached models.
        self.model_dir_edit.setText(settings.model_dir or "")
        self._refresh_model_combo(selected=settings.model_size)
        self._select_combo_data(self.language_combo, settings.language_mode)
        self.vad_checkbox.setChecked(settings.vad_enabled)
        self.save_wav_checkbox.setChecked(settings.save_last_wav)
        self.keep_clipboard_checkbox.setChecked(
            settings.keep_transcript_in_clipboard
        )
        self.offline_mode_checkbox.setChecked(settings.offline_mode)
        self._select_combo_data(self.engine_combo, settings.engine)
        self._select_combo_data(self.mode_combo, settings.mode)
        self._select_combo_data(self.paste_mode_combo, settings.paste_mode)
        self._select_combo_data(self.groq_model_combo, settings.groq_model)
        self._select_combo_data(self.openai_model_combo, settings.openai_model)
        self._update_mode_availability()

        if settings.has_openai_key:
            self.openai_key_edit.setPlaceholderText(
                "Stored (leave empty to keep)"
            )
        if settings.has_deepgram_key:
            self.deepgram_key_edit.setPlaceholderText(
                "Stored (leave empty to keep)"
            )
        if settings.has_assemblyai_key:
            self.assemblyai_key_edit.setPlaceholderText(
                "Stored (leave empty to keep)"
            )
        if settings.has_groq_key:
            self.groq_key_edit.setPlaceholderText(
                "Stored (leave empty to keep)"
            )

        self._update_engine_indicator()

    def _select_combo_data(
        self, combo: QtWidgets.QComboBox, value: str
    ) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _browse_model_dir(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select model directory", self.model_dir_edit.text()
        )
        if path:
            self.model_dir_edit.setText(path)

    def _copy_diagnostics(self) -> None:
        text = self._app_logger.diagnostics_text()
        clipboard = QtGui.QGuiApplication.clipboard()
        clipboard.setText(text)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save(self) -> None:
        hotkey = _qt_hotkey_sequence_to_app_hotkey(
            self.hotkey_edit.keySequence()
        )
        hotkey = hotkey or DEFAULT_HOTKEY
        try:
            parse_hotkey(hotkey)
        except ValueError as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Invalid hotkey",
                f"The hotkey is invalid: {exc}",
            )
            return

        has_openai_key = self._loaded_settings.has_openai_key
        has_deepgram_key = self._loaded_settings.has_deepgram_key
        has_assemblyai_key = self._loaded_settings.has_assemblyai_key
        has_groq_key = self._loaded_settings.has_groq_key

        openai_value = self.openai_key_edit.text().strip()
        deepgram_value = self.deepgram_key_edit.text().strip()
        assemblyai_value = self.assemblyai_key_edit.text().strip()
        groq_value = self.groq_key_edit.text().strip()

        if openai_value:
            self._secret_store.set_api_key("openai", openai_value)
            has_openai_key = True
        if deepgram_value:
            self._secret_store.set_api_key("deepgram", deepgram_value)
            has_deepgram_key = True
        if assemblyai_value:
            self._secret_store.set_api_key("assemblyai", assemblyai_value)
            has_assemblyai_key = True
        if groq_value:
            self._secret_store.set_api_key("groq", groq_value)
            has_groq_key = True

        settings = AppSettings(
            hotkey=hotkey,
            model_size=str(self.model_combo.currentData()),
            language_mode=str(
                self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE
            ),
            vad_enabled=self.vad_checkbox.isChecked(),
            save_last_wav=self.save_wav_checkbox.isChecked(),
            keep_transcript_in_clipboard=(
                self.keep_clipboard_checkbox.isChecked()
            ),
            offline_mode=self.offline_mode_checkbox.isChecked(),
            model_dir=self.model_dir_edit.text().strip(),
            engine=str(
                self.engine_combo.currentData() or DEFAULT_ENGINE
            ),
            mode=str(self.mode_combo.currentData() or DEFAULT_MODE),
            paste_mode=str(
                self.paste_mode_combo.currentData() or DEFAULT_PASTE_MODE
            ),
            has_openai_key=has_openai_key,
            has_deepgram_key=has_deepgram_key,
            has_assemblyai_key=has_assemblyai_key,
            has_groq_key=has_groq_key,
            groq_model=str(
                self.groq_model_combo.currentData() or DEFAULT_GROQ_MODEL
            ),
            openai_model=str(
                self.openai_model_combo.currentData() or DEFAULT_OPENAI_MODEL
            ),
        )

        self._settings_store.save(settings)
        self._loaded_settings = settings

        # Show confirmation feedback instead of closing the dialog.
        self.save_status.setText("\u2713 Settings saved")
        self.save_status.setStyleSheet(
            "color: #1b5e20; font-weight: bold; padding: 2px;"
        )
        self._save_timer.start(3000)


# ======================================================================
# Hotkey conversion helpers
# ======================================================================


def _qt_hotkey_sequence_to_app_hotkey(
    sequence: QtGui.QKeySequence,
) -> str:
    text = sequence.toString(QtGui.QKeySequence.PortableText)
    return _qt_hotkey_text_to_app_hotkey(text)


def _qt_hotkey_text_to_app_hotkey(text: str) -> str:
    if not text:
        return ""

    first = text.split(",")[0].strip()
    if not first:
        return ""

    token_map = {
        "CTRL": "Ctrl",
        "ALT": "Alt",
        "SHIFT": "Shift",
        "META": "Win",
        "ESCAPE": "Esc",
        "RETURN": "Enter",
    }
    tokens = [token.strip() for token in first.split("+") if token.strip()]
    normalized: list[str] = []
    for token in tokens:
        upper = token.upper()
        if upper in token_map:
            normalized.append(token_map[upper])
            continue
        if len(token) == 1:
            normalized.append(token.upper())
            continue
        normalized.append(token)

    return "+".join(normalized)


def _app_hotkey_to_qt_hotkey_text(text: str) -> str:
    if not text:
        return ""

    token_map = {
        "WIN": "Meta",
        "ESC": "Escape",
    }
    tokens = [token.strip() for token in text.split("+") if token.strip()]
    normalized: list[str] = []
    for token in tokens:
        upper = token.upper()
        normalized.append(token_map.get(upper, token))
    return "+".join(normalized)
