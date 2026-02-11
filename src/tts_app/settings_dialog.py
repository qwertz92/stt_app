from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from .config import (
    DEFAULT_ENGINE,
    DEFAULT_HOTKEY,
    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_PASTE_MODE,
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

        self.setWindowTitle("Dictation Settings")
        self.setModal(True)
        self.resize(540, 560)

        self._build_ui()
        self._populate(self._loaded_settings)

    def _build_ui(self) -> None:
        self.hotkey_edit = QtWidgets.QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        if hasattr(self.hotkey_edit, "setClearButtonEnabled"):
            self.hotkey_edit.setClearButtonEnabled(True)
        hotkey_hint = QtWidgets.QLabel(
            "Click the hotkey field and press the combination to record it."
        )
        hotkey_hint.setStyleSheet("color: #555;")

        self.model_combo = QtWidgets.QComboBox()
        model_labels = {
            "tiny": "tiny (~75 MB)",
            "base": "base (~141 MB)",
            "small": "small (~484 MB)",
            "medium": "medium (~1.4 GB)",
            "large-v3": "large-v3 (~3 GB, multilingual)",
            "large-v3-turbo": "large-v3-turbo (~809 MB, multilingual, fast)",
            "distil-large-v3.5": "distil-large-v3.5 (~756 MB, English only, improved)",
        }
        for value in VALID_MODEL_SIZES:
            self.model_combo.addItem(model_labels.get(value, value), value)

        self.language_combo = QtWidgets.QComboBox()
        language_labels = {
            "auto": "Auto",
            "de": "German",
            "en": "English",
        }
        for value in VALID_LANGUAGE_MODES:
            self.language_combo.addItem(language_labels.get(value, value), value)

        self.vad_checkbox = QtWidgets.QCheckBox("Enable energy-based auto-stop")
        self.save_wav_checkbox = QtWidgets.QCheckBox("Save last WAV for debugging")
        self.keep_clipboard_checkbox = QtWidgets.QCheckBox(
            "Keep transcript in clipboard after transcription"
        )
        self.offline_mode_checkbox = QtWidgets.QCheckBox(
            "Offline mode (use cached models only, no internet)"
        )
        self.offline_mode_checkbox.setToolTip(
            "When enabled, sets local_files_only=True so faster-whisper never "
            "attempts to download models. The model must already be cached "
            "locally (see README for offline setup instructions)."
        )

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
        self.model_dir_edit.textChanged.connect(
            lambda _text: self._refresh_local_models_label()
        )
        model_dir_layout = QtWidgets.QHBoxLayout()
        model_dir_layout.addWidget(self.model_dir_edit, 1)
        model_dir_layout.addWidget(self.model_dir_browse)

        self.engine_combo = QtWidgets.QComboBox()
        engine_labels = {
            "local": "Local (faster-whisper)",
            "assemblyai": "Remote (AssemblyAI)",
            "openai": "Remote (OpenAI)",
            "azure": "Remote (Azure)",
            "deepgram": "Remote (Deepgram)",
        }
        for value in VALID_ENGINES:
            self.engine_combo.addItem(engine_labels.get(value, value), value)

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
            self.paste_mode_combo.addItem(paste_mode_labels.get(value, value), value)

        self.openai_key_edit = QtWidgets.QLineEdit()
        self.azure_key_edit = QtWidgets.QLineEdit()
        self.deepgram_key_edit = QtWidgets.QLineEdit()
        self.assemblyai_key_edit = QtWidgets.QLineEdit()
        for field in (
            self.openai_key_edit,
            self.azure_key_edit,
            self.deepgram_key_edit,
            self.assemblyai_key_edit,
        ):
            field.setEchoMode(QtWidgets.QLineEdit.Password)
            field.setPlaceholderText("Stored in Windows Credential Manager")

        form = QtWidgets.QFormLayout()
        form.addRow("Hotkey", self.hotkey_edit)
        form.addRow("", hotkey_hint)
        form.addRow("Model Size", self.model_combo)
        form.addRow("Language", self.language_combo)
        form.addRow("Engine", self.engine_combo)
        form.addRow("Mode", self.mode_combo)
        form.addRow("Paste Mode", self.paste_mode_combo)
        form.addRow("", self.vad_checkbox)
        form.addRow("", self.save_wav_checkbox)
        form.addRow("", self.keep_clipboard_checkbox)
        form.addRow("", self.offline_mode_checkbox)
        form.addRow("Model Dir", model_dir_layout)

        provider_box = QtWidgets.QGroupBox("Remote Provider API Keys")
        provider_layout = QtWidgets.QFormLayout(provider_box)
        provider_layout.addRow("AssemblyAI", self.assemblyai_key_edit)
        provider_layout.addRow("OpenAI", self.openai_key_edit)
        provider_layout.addRow("Azure", self.azure_key_edit)
        provider_layout.addRow("Deepgram", self.deepgram_key_edit)
        provider_note = QtWidgets.QLabel(
            "Keys are saved in Windows Credential Manager via keyring."
        )
        provider_note.setStyleSheet("color: #555;")
        provider_layout.addRow(provider_note)

        self.test_conn_button = QtWidgets.QPushButton("Test Connection")
        self.test_conn_button.setToolTip(
            "Test the selected remote provider's API key and network connectivity."
        )
        self.test_conn_button.clicked.connect(self._test_connection)
        self.test_conn_result = QtWidgets.QLabel("")
        self.test_conn_result.setWordWrap(True)
        provider_layout.addRow(self.test_conn_button, self.test_conn_result)

        # Local models info
        local_models_box = QtWidgets.QGroupBox("Local Models")
        local_models_layout = QtWidgets.QVBoxLayout(local_models_box)
        self.local_models_label = QtWidgets.QLabel("Scanning...")
        self.local_models_label.setWordWrap(True)
        local_models_layout.addWidget(self.local_models_label)
        self._refresh_local_models_label()

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
        root.addLayout(form)
        root.addWidget(provider_box)
        root.addWidget(local_models_box)
        root.addStretch(1)
        root.addLayout(buttons)

    def _refresh_local_models_label(self) -> None:
        """Scan for locally cached models and update the label."""
        model_dir = self.model_dir_edit.text().strip()
        try:
            cached = find_cached_models(model_dir)
        except Exception:
            cached = []

        if cached:
            self.local_models_label.setText(f"Available locally: {', '.join(cached)}")
            self.local_models_label.setStyleSheet("color: #1b5e20;")
        else:
            self.local_models_label.setText(
                "No local models found. Models will be downloaded on first use.\n"
                "See docs/offline-usage-guide.md if downloads are blocked."
            )
            self.local_models_label.setStyleSheet("color: #b71c1c;")

    def _test_connection(self) -> None:
        """Test connectivity for the selected remote provider."""
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)

        if engine == DEFAULT_ENGINE:
            self.test_conn_result.setText("Local provider — no connection test needed.")
            self.test_conn_result.setStyleSheet("color: #555;")
            return

        self.test_conn_button.setEnabled(False)
        self.test_conn_result.setText("Testing...")
        self.test_conn_result.setStyleSheet("color: #555;")
        QtWidgets.QApplication.processEvents()

        try:
            if engine == "assemblyai":
                # Get the API key: prefer text field, fall back to stored key.
                api_key = self.assemblyai_key_edit.text().strip()
                if not api_key:
                    api_key = self._secret_store.get_api_key("assemblyai") or ""
                if not api_key:
                    self.test_conn_result.setText(
                        "No API key entered. Enter a key above first."
                    )
                    self.test_conn_result.setStyleSheet("color: #b71c1c;")
                    return

                from .transcriber.assemblyai_provider import AssemblyAITranscriber

                t = AssemblyAITranscriber(api_key=api_key)
                ok, msg = t.test_connection()
                if ok:
                    self.test_conn_result.setText(f"✓ {msg}")
                    self.test_conn_result.setStyleSheet("color: #1b5e20;")
                else:
                    self.test_conn_result.setText(f"✗ {msg}")
                    self.test_conn_result.setStyleSheet("color: #b71c1c;")
            else:
                self.test_conn_result.setText(
                    f"Connection test not yet implemented for {engine}."
                )
                self.test_conn_result.setStyleSheet("color: #555;")
        except Exception as exc:
            self.test_conn_result.setText(f"Test failed: {exc}")
            self.test_conn_result.setStyleSheet("color: #b71c1c;")
        finally:
            self.test_conn_button.setEnabled(True)

    def _populate(self, settings: AppSettings) -> None:
        self.hotkey_edit.setKeySequence(
            QtGui.QKeySequence(_app_hotkey_to_qt_hotkey_text(settings.hotkey))
        )
        self._select_combo_data(self.model_combo, settings.model_size)
        self._select_combo_data(self.language_combo, settings.language_mode)
        self.vad_checkbox.setChecked(settings.vad_enabled)
        self.save_wav_checkbox.setChecked(settings.save_last_wav)
        self.keep_clipboard_checkbox.setChecked(settings.keep_transcript_in_clipboard)
        self.offline_mode_checkbox.setChecked(settings.offline_mode)
        self.model_dir_edit.setText(settings.model_dir or "")
        self._select_combo_data(self.engine_combo, settings.engine)
        self._select_combo_data(self.mode_combo, settings.mode)
        self._select_combo_data(self.paste_mode_combo, settings.paste_mode)

        if settings.has_openai_key:
            self.openai_key_edit.setPlaceholderText("Stored (leave empty to keep)")
        if settings.has_azure_key:
            self.azure_key_edit.setPlaceholderText("Stored (leave empty to keep)")
        if settings.has_deepgram_key:
            self.deepgram_key_edit.setPlaceholderText("Stored (leave empty to keep)")
        if settings.has_assemblyai_key:
            self.assemblyai_key_edit.setPlaceholderText("Stored (leave empty to keep)")

    def _select_combo_data(self, combo: QtWidgets.QComboBox, value: str) -> None:
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

    def _save(self) -> None:
        hotkey = _qt_hotkey_sequence_to_app_hotkey(self.hotkey_edit.keySequence())
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
        has_azure_key = self._loaded_settings.has_azure_key
        has_deepgram_key = self._loaded_settings.has_deepgram_key
        has_assemblyai_key = self._loaded_settings.has_assemblyai_key

        openai_value = self.openai_key_edit.text().strip()
        azure_value = self.azure_key_edit.text().strip()
        deepgram_value = self.deepgram_key_edit.text().strip()
        assemblyai_value = self.assemblyai_key_edit.text().strip()

        if openai_value:
            self._secret_store.set_api_key("openai", openai_value)
            has_openai_key = True
        if azure_value:
            self._secret_store.set_api_key("azure", azure_value)
            has_azure_key = True
        if deepgram_value:
            self._secret_store.set_api_key("deepgram", deepgram_value)
            has_deepgram_key = True
        if assemblyai_value:
            self._secret_store.set_api_key("assemblyai", assemblyai_value)
            has_assemblyai_key = True

        settings = AppSettings(
            hotkey=hotkey,
            model_size=str(self.model_combo.currentData()),
            language_mode=str(
                self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE
            ),
            vad_enabled=self.vad_checkbox.isChecked(),
            save_last_wav=self.save_wav_checkbox.isChecked(),
            keep_transcript_in_clipboard=self.keep_clipboard_checkbox.isChecked(),
            offline_mode=self.offline_mode_checkbox.isChecked(),
            model_dir=self.model_dir_edit.text().strip(),
            engine=str(self.engine_combo.currentData() or DEFAULT_ENGINE),
            mode=str(self.mode_combo.currentData() or DEFAULT_MODE),
            paste_mode=str(self.paste_mode_combo.currentData() or DEFAULT_PASTE_MODE),
            has_openai_key=has_openai_key,
            has_azure_key=has_azure_key,
            has_deepgram_key=has_deepgram_key,
            has_assemblyai_key=has_assemblyai_key,
        )

        self._settings_store.save(settings)
        self.accept()


def _qt_hotkey_sequence_to_app_hotkey(sequence: QtGui.QKeySequence) -> str:
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
