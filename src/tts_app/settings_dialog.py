from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from .app_paths import debug_audio_path, recordings_dir
from .config import (
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_ENGINE,
    DEFAULT_GROQ_MODEL,
    DEFAULT_HISTORY_MAX_ITEMS,
    DEFAULT_HOTKEY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OVERLAY_CORNER,
    DEFAULT_PASTE_MODE,
    DEFAULT_RECORDINGS_MAX_COUNT,
    DEFAULT_START_BEEP_TONE,
    DEFAULT_VAD_ENERGY_THRESHOLD,
    DOC_MODELS_PATH,
    ENGINE_LANGUAGE_MODES,
    GROQ_MODELS,
    HISTORY_MAX_ITEMS_MAX,
    LANGUAGE_MODE_LABELS,
    LOCAL_ENGLISH_ONLY_MODELS,
    OPENAI_MODELS,
    STREAMING_ENGINES,
    VAD_ENERGY_THRESHOLD_MAX,
    VAD_ENERGY_THRESHOLD_MIN,
    VALID_ENGINES,
    VALID_LANGUAGE_MODES,
    VALID_MODES,
    VALID_MODEL_SIZES,
    VALID_OVERLAY_CORNERS,
    VALID_PASTE_MODES,
    VALID_START_BEEP_TONES,
)
from .hotkey import parse_hotkey
from .logger import AppLogger
from .secret_store import SecretStore
from .settings_store import AppSettings, SettingsStore
from .transcript_history import TranscriptHistoryStore
from .transcriber.local_faster_whisper import (
    delete_cached_model,
    find_cached_models,
)

if TYPE_CHECKING:
    from .controller import DictationController


class SettingsDialog(QtWidgets.QDialog):
    connection_test_finished = QtCore.Signal(int, bool, str)
    settings_changed = QtCore.Signal()

    def __init__(
        self,
        settings_store: SettingsStore,
        secret_store: SecretStore,
        app_logger: AppLogger,
        controller: DictationController | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_store = settings_store
        self._secret_store = secret_store
        self._app_logger = app_logger
        self._controller = controller
        self._history_store = TranscriptHistoryStore()
        self._loaded_settings = self._settings_store.load()
        self._connection_test_id = 0
        self._active_connection_test_thread: threading.Thread | None = None
        self._history_copy_feedback_timer = QtCore.QTimer(self)
        self._history_copy_feedback_timer.setSingleShot(True)
        self._history_copy_feedback_timer.setInterval(900)
        self._history_copy_feedback_timer.timeout.connect(
            self._reset_history_copy_feedback
        )

        self.setWindowTitle("Dictation Settings")
        self.setModal(False)
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, False)
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
        self.tabs.setStyleSheet(
            """
            QTabBar::tab {
                padding: 6px 18px;
                margin-right: 2px;
                border: 1px solid #bbb;
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                background: #e8e8e8;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                border-bottom: 2px solid #1a73e8;
                font-weight: bold;
            }
            QTabBar::tab:hover:!selected {
                background: #d6e4f0;
            }
            QTabBar::tab:selected:hover {
                background: #ffffff;
            }
            """
        )
        self._build_general_tab()
        self._build_local_tab()
        self._build_remote_tab()
        self._build_history_tab()

        # --- Bottom buttons ---
        self.copy_diag_button = QtWidgets.QPushButton("Copy diagnostics")
        self.copy_diag_button.clicked.connect(self._copy_diagnostics)

        save_button = QtWidgets.QPushButton("Save")
        close_button = QtWidgets.QPushButton("Close")
        save_button.clicked.connect(self._save)
        close_button.clicked.connect(self.reject)

        self._save_status_label = QtWidgets.QLabel()
        self._save_status_label.setStyleSheet("color: #2e7d32; font-weight: bold;")
        self._save_status_timer = QtCore.QTimer(self)
        self._save_status_timer.setSingleShot(True)
        self._save_status_timer.setInterval(3000)
        self._save_status_timer.timeout.connect(
            lambda: self._save_status_label.setText("")
        )

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.copy_diag_button)
        buttons.addStretch(1)
        buttons.addWidget(self._save_status_label)
        buttons.addWidget(save_button)
        buttons.addWidget(close_button)

        root = QtWidgets.QVBoxLayout(self)
        root.addWidget(self.engine_indicator)
        root.addWidget(self.tabs)
        root.addLayout(buttons)

    # --- General tab ---

    def _build_general_tab(self) -> None:
        tab = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(tab)

        self.hotkey_edit = QtWidgets.QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        if hasattr(self.hotkey_edit, "setClearButtonEnabled"):
            self.hotkey_edit.setClearButtonEnabled(True)
        self.cancel_hotkey_edit = QtWidgets.QKeySequenceEdit()
        self.cancel_hotkey_edit.setMaximumSequenceLength(1)
        if hasattr(self.cancel_hotkey_edit, "setClearButtonEnabled"):
            self.cancel_hotkey_edit.setClearButtonEnabled(True)
        hotkey_hint = QtWidgets.QLabel(
            "Click the hotkey field and press the combination to record it."
        )
        hotkey_hint.setStyleSheet("color: #555;")
        cancel_hotkey_hint = QtWidgets.QLabel(
            "Cancel hotkey stops current recording/transcription (must differ from main hotkey)."
        )
        cancel_hotkey_hint.setStyleSheet("color: #555;")

        self.language_combo = QtWidgets.QComboBox()
        self.language_note_label = QtWidgets.QLabel("")
        self.language_note_label.setWordWrap(True)
        self.language_note_label.setStyleSheet("color: #555;")
        self.language_note_label.setVisible(False)

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
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

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
        self.vad_threshold_spin = QtWidgets.QDoubleSpinBox()
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

        self.start_beep_checkbox = QtWidgets.QCheckBox("Play start tone on recording")
        self.start_beep_tone_combo = QtWidgets.QComboBox()
        tone_labels = {
            "soft": "Soft beep",
            "high": "High beep",
            "chime": "Two-tone chime",
            "system": "System notification",
        }
        for value in VALID_START_BEEP_TONES:
            self.start_beep_tone_combo.addItem(tone_labels.get(value, value), value)

        self.save_wav_checkbox = QtWidgets.QCheckBox("Save last WAV for debugging")
        self.save_wav_path_label = QtWidgets.QLabel(
            f"Saved to: {debug_audio_path()} (overwritten on each recording)"
        )
        self.save_wav_path_label.setWordWrap(True)
        self.save_wav_path_label.setStyleSheet("color: #555;")

        self.save_all_recordings_checkbox = QtWidgets.QCheckBox(
            "Archive every recording to folder"
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
        recordings_dir_layout = QtWidgets.QHBoxLayout()
        recordings_dir_layout.addWidget(self.recordings_dir_edit, 1)
        recordings_dir_layout.addWidget(self.recordings_dir_browse)
        recordings_dir_layout.addWidget(self.recordings_open_button)
        self.recordings_max_spin = QtWidgets.QSpinBox()
        self.recordings_max_spin.setRange(1, 500)
        self.recordings_max_spin.setValue(DEFAULT_RECORDINGS_MAX_COUNT)
        self.recordings_max_spin.setToolTip(
            "Keep only the newest N archived recordings."
        )

        self.history_max_spin = QtWidgets.QSpinBox()
        self.history_max_spin.setRange(0, HISTORY_MAX_ITEMS_MAX)
        self.history_max_spin.setSpecialValueText("Unlimited (0)")
        self.history_max_spin.setValue(DEFAULT_HISTORY_MAX_ITEMS)
        self.history_max_spin.setToolTip(
            "Maximum transcript history items stored (0 = unlimited)."
        )
        self.history_max_spin.valueChanged.connect(
            lambda _value: self._refresh_history_list()
        )

        self.overlay_corner_combo = QtWidgets.QComboBox()
        corner_labels = {
            "top-right": "Top Right",
            "top-left": "Top Left",
            "bottom-right": "Bottom Right",
            "bottom-left": "Bottom Left",
        }
        for value in VALID_OVERLAY_CORNERS:
            self.overlay_corner_combo.addItem(corner_labels.get(value, value), value)

        self.keep_clipboard_checkbox = QtWidgets.QCheckBox(
            "Keep transcript in clipboard after transcription"
        )

        form.addRow("Hotkey", self.hotkey_edit)
        form.addRow("", hotkey_hint)
        form.addRow("Cancel Hotkey", self.cancel_hotkey_edit)
        form.addRow("", cancel_hotkey_hint)
        form.addRow("Engine", self.engine_combo)
        form.addRow("Language", self.language_combo)
        form.addRow("", self.language_note_label)
        form.addRow("Mode", self.mode_combo)
        form.addRow("Paste Mode", self.paste_mode_combo)
        form.addRow("", self.vad_checkbox)
        form.addRow("VAD Threshold", self.vad_threshold_spin)
        form.addRow("", self.start_beep_checkbox)
        form.addRow("Start Tone", self.start_beep_tone_combo)
        form.addRow("Overlay Corner", self.overlay_corner_combo)
        form.addRow("", self.save_wav_checkbox)
        form.addRow("", self.save_wav_path_label)
        form.addRow("", self.save_all_recordings_checkbox)
        form.addRow("Recordings Folder", recordings_dir_layout)
        form.addRow("Keep Recordings", self.recordings_max_spin)
        form.addRow("History Size", self.history_max_spin)
        form.addRow("", self.keep_clipboard_checkbox)

        self.tabs.addTab(tab, "General")

    # --- Local tab ---

    def _build_local_tab(self) -> None:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        form = QtWidgets.QFormLayout()

        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
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

        self.cached_models_list = QtWidgets.QListWidget()
        self.cached_models_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        self.cached_models_list.itemSelectionChanged.connect(
            self._on_cached_model_selection_changed
        )
        local_models_layout.addWidget(self.cached_models_list)

        manage_buttons = QtWidgets.QHBoxLayout()
        self.refresh_local_models_button = QtWidgets.QPushButton("Refresh")
        self.refresh_local_models_button.clicked.connect(
            self._refresh_local_model_views
        )
        self.delete_cached_model_button = QtWidgets.QPushButton("Delete Selected")
        self.delete_cached_model_button.setEnabled(False)
        self.delete_cached_model_button.clicked.connect(
            self._delete_selected_cached_model
        )
        manage_buttons.addWidget(self.refresh_local_models_button)
        manage_buttons.addStretch(1)
        manage_buttons.addWidget(self.delete_cached_model_button)
        local_models_layout.addLayout(manage_buttons)

        self.local_models_action_label = QtWidgets.QLabel("")
        self.local_models_action_label.setWordWrap(True)
        local_models_layout.addWidget(self.local_models_action_label)
        self._refresh_local_models_label()
        self._refresh_cached_models_list()

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

    # --- History tab ---

    def _build_history_tab(self) -> None:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)

        history_box = QtWidgets.QGroupBox("Transcript History")
        history_layout = QtWidgets.QVBoxLayout(history_box)

        self.history_list = QtWidgets.QListWidget()
        self.history_list.itemSelectionChanged.connect(self._on_history_item_selected)
        history_layout.addWidget(self.history_list)

        self.history_detail = QtWidgets.QPlainTextEdit()
        self.history_detail.setReadOnly(True)
        history_layout.addWidget(self.history_detail)

        history_buttons = QtWidgets.QHBoxLayout()
        self.history_refresh_button = QtWidgets.QPushButton("Refresh")
        self.history_refresh_button.clicked.connect(self._refresh_history_list)
        self.history_copy_button = QtWidgets.QPushButton("Copy selected")
        self.history_copy_button.clicked.connect(self._copy_selected_history)
        self.history_copy_button.setEnabled(False)
        history_buttons.addWidget(self.history_refresh_button)
        history_buttons.addStretch(1)
        history_buttons.addWidget(self.history_copy_button)
        history_layout.addLayout(history_buttons)
        layout.addWidget(history_box, 2)

        import_box = QtWidgets.QGroupBox("Import Audio File")
        import_layout = QtWidgets.QVBoxLayout(import_box)
        import_hint = QtWidgets.QLabel(
            "Transcribe an existing audio file using current settings "
            "(useful after failures or for external recordings)."
        )
        import_hint.setWordWrap(True)
        import_hint.setStyleSheet("color: #555;")
        import_layout.addWidget(import_hint)

        import_buttons = QtWidgets.QHBoxLayout()
        self.import_file_button = QtWidgets.QPushButton("Choose file and transcribe")
        self.import_file_button.clicked.connect(self._import_and_transcribe_file)
        import_buttons.addWidget(self.import_file_button)
        import_buttons.addStretch(1)
        import_layout.addLayout(import_buttons)

        self.import_result_label = QtWidgets.QLabel("")
        self.import_result_label.setWordWrap(True)
        import_layout.addWidget(self.import_result_label)

        self.import_result_text = QtWidgets.QPlainTextEdit()
        self.import_result_text.setReadOnly(True)
        import_layout.addWidget(self.import_result_text)

        layout.addWidget(import_box, 2)
        self.tabs.addTab(tab, "History")

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

    def _refresh_cached_models_list(self) -> None:
        model_dir = self.model_dir_edit.text().strip()
        try:
            cached = find_cached_models(model_dir)
        except Exception:
            cached = []
        self.cached_models_list.clear()
        for model_name in cached:
            item = QtWidgets.QListWidgetItem(model_name)
            item.setData(QtCore.Qt.UserRole, model_name)
            self.cached_models_list.addItem(item)
        self.delete_cached_model_button.setEnabled(False)

    def _refresh_local_model_views(self) -> None:
        self._refresh_local_models_label()
        self._refresh_cached_models_list()
        self._refresh_model_combo()
        self._update_language_availability()

    def _on_cached_model_selection_changed(self) -> None:
        self.delete_cached_model_button.setEnabled(
            bool(self.cached_models_list.selectedItems())
        )

    def _delete_selected_cached_model(self) -> None:
        selected_items = self.cached_models_list.selectedItems()
        if not selected_items:
            self.delete_cached_model_button.setEnabled(False)
            return
        model_name = str(selected_items[0].data(QtCore.Qt.UserRole) or "").strip()
        if not model_name:
            self.delete_cached_model_button.setEnabled(False)
            return

        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete local model",
            (
                f"Delete local cache for model '{model_name}'?\n\n"
                "This removes downloaded files from disk."
            ),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        try:
            removed = delete_cached_model(
                model_name,
                self.model_dir_edit.text().strip(),
            )
        except Exception as exc:
            self.local_models_action_label.setStyleSheet("color: #b71c1c;")
            self.local_models_action_label.setText(
                f"Failed to delete '{model_name}': {exc}"
            )
            return

        if removed <= 0:
            self.local_models_action_label.setStyleSheet("color: #555;")
            self.local_models_action_label.setText(
                f"No cache directories found for '{model_name}'."
            )
        else:
            self.local_models_action_label.setStyleSheet("color: #1b5e20;")
            self.local_models_action_label.setText(
                f"Deleted '{model_name}' ({removed} folder(s) removed)."
            )
        self._refresh_local_model_views()

    # ------------------------------------------------------------------
    # Engine-dependent option availability
    # ------------------------------------------------------------------

    def _language_modes_for_current_selection(self) -> tuple[str, ...]:
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        mode = str(self.mode_combo.currentData() or DEFAULT_MODE)
        model = (
            str(self.model_combo.currentData() or "")
            if hasattr(self, "model_combo")
            else ""
        )

        if engine == "assemblyai" and mode == "streaming":
            # AssemblyAI realtime streaming path does language detection automatically.
            return ("auto",)

        if engine == "local" and model in LOCAL_ENGLISH_ONLY_MODELS:
            return ("auto", "en")

        return ENGINE_LANGUAGE_MODES.get(engine, VALID_LANGUAGE_MODES)

    def _language_constraint_note(self) -> str:
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        mode = str(self.mode_combo.currentData() or DEFAULT_MODE)
        model = (
            str(self.model_combo.currentData() or "")
            if hasattr(self, "model_combo")
            else ""
        )

        if engine == "assemblyai" and mode == "streaming":
            return (
                "AssemblyAI streaming always uses automatic language detection "
                "(language is fixed to Auto)."
            )

        if engine == "local" and model in LOCAL_ENGLISH_ONLY_MODELS:
            return (
                "distil-large-v3.5 is an English-only model "
                "(German is disabled for this model)."
            )

        if engine == "groq":
            return (
                "Groq Whisper models are multilingual. 'Auto' lets the model detect "
                "language; selecting German/English sends a language hint."
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
            self.language_combo.addItem(LANGUAGE_MODE_LABELS.get(value, value), value)

        target_mode = (
            selected_mode if selected_mode in supported_modes else supported_modes[0]
        )
        self._select_combo_data(self.language_combo, target_mode)
        self.language_combo.blockSignals(False)

        note = self._language_constraint_note()
        self.language_note_label.setText(note)
        self.language_note_label.setVisible(bool(note))
        self.language_combo.setEnabled(len(supported_modes) > 1)
        self.language_combo.setToolTip(
            note
            or "Choose the recognition language for the selected engine."
        )

    # ------------------------------------------------------------------
    # Engine indicator
    # ------------------------------------------------------------------

    def _update_engine_indicator(self) -> None:
        """Update the always-visible engine indicator bar."""
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        if engine == "local":
            label = "Engine: LOCAL (faster-whisper)"
            self.engine_indicator.setText(label)
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
                    "Use local, AssemblyAI, or Deepgram for streaming."
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
        self._update_language_availability()

    def _on_mode_changed(self, _index: int = 0) -> None:
        self._update_language_availability()

    def _on_model_changed(self, _index: int = 0) -> None:
        self._update_language_availability()

    def _on_model_dir_changed(self, _text: str = "") -> None:
        """React to model directory changes — update cached model info."""
        self._refresh_local_model_views()

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
        self.cancel_hotkey_edit.setKeySequence(
            QtGui.QKeySequence(
                _app_hotkey_to_qt_hotkey_text(settings.cancel_hotkey)
            )
        )
        # Model Dir must be set before refreshing the model combo so it can
        # scan the correct directory for cached models.
        self.model_dir_edit.setText(settings.model_dir or "")
        self._refresh_model_combo(selected=settings.model_size)
        self.vad_checkbox.setChecked(settings.vad_enabled)
        self.vad_threshold_spin.setValue(float(settings.vad_energy_threshold))
        self.start_beep_checkbox.setChecked(settings.start_beep_enabled)
        self._select_combo_data(self.start_beep_tone_combo, settings.start_beep_tone)
        self.save_wav_checkbox.setChecked(settings.save_last_wav)
        self.save_all_recordings_checkbox.setChecked(settings.save_all_recordings)
        self.recordings_dir_edit.setText(settings.recordings_dir or "")
        self.recordings_max_spin.setValue(int(settings.recordings_max_count))
        self.history_max_spin.setValue(int(settings.history_max_items))
        self._select_combo_data(self.overlay_corner_combo, settings.overlay_corner)
        self.keep_clipboard_checkbox.setChecked(
            settings.keep_transcript_in_clipboard
        )
        self.offline_mode_checkbox.setChecked(settings.offline_mode)
        self._select_combo_data(self.engine_combo, settings.engine)
        self._select_combo_data(self.mode_combo, settings.mode)
        self._update_mode_availability()
        self._update_language_availability(preferred_mode=settings.language_mode)
        self._select_combo_data(self.paste_mode_combo, settings.paste_mode)
        self._select_combo_data(self.groq_model_combo, settings.groq_model)
        self._select_combo_data(self.openai_model_combo, settings.openai_model)

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
        self._refresh_history_list()

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

    def _browse_recordings_dir(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select recordings directory",
            self.recordings_dir_edit.text() or str(recordings_dir()),
        )
        if path:
            self.recordings_dir_edit.setText(path)

    def _open_recordings_dir(self) -> None:
        target = self._effective_recordings_dir()
        Path(target).mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(target))

    def _effective_recordings_dir(self) -> str:
        text = self.recordings_dir_edit.text().strip()
        if text:
            return text
        return str(recordings_dir())

    def _refresh_history_list(self) -> None:
        self.history_list.clear()
        self.history_detail.clear()
        self.history_copy_button.setEnabled(False)
        entries = self._history_store.recent_entries(self.history_max_spin.value())
        for entry in entries:
            text = entry.text.strip().replace("\n", " ")
            preview = text[:70] + ("..." if len(text) > 70 else "")
            label = f"{entry.created_at} | {entry.engine}/{entry.model} | {preview}"
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, entry.text)
            self.history_list.addItem(item)

    def _on_history_item_selected(self) -> None:
        items = self.history_list.selectedItems()
        if not items:
            self.history_copy_button.setEnabled(False)
            self.history_detail.clear()
            self._reset_history_copy_feedback()
            return
        text = str(items[0].data(QtCore.Qt.UserRole) or "")
        self.history_copy_button.setEnabled(bool(text))
        self.history_detail.setPlainText(text)
        self._reset_history_copy_feedback()

    def _copy_selected_history(self) -> None:
        items = self.history_list.selectedItems()
        if not items:
            return
        text = str(items[0].data(QtCore.Qt.UserRole) or "")
        if not text:
            return
        QtGui.QGuiApplication.clipboard().setText(text)
        self.history_copy_button.setText("Copied")
        self.history_copy_button.setStyleSheet(
            "background-color: #dff5e0; border: 1px solid #89c88f;"
        )
        self._history_copy_feedback_timer.start()

    def _reset_history_copy_feedback(self) -> None:
        self.history_copy_button.setText("Copy selected")
        self.history_copy_button.setStyleSheet("")

    def _import_and_transcribe_file(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select audio file",
            "",
            "Audio files (*.wav *.mp3 *.m4a *.flac *.ogg *.opus *.webm);;All files (*)",
        )
        if not path:
            return
        self.import_result_label.setText("Transcribing...")
        self.import_result_label.setStyleSheet("color: #555;")
        self.import_result_text.clear()
        self.import_file_button.setEnabled(False)

        def _run() -> None:
            try:
                ok, text = self._transcribe_import_file(path)
            except Exception as exc:
                ok, text = False, str(exc)
            QtCore.QTimer.singleShot(
                0,
                lambda: self._finish_import_transcription(bool(ok), str(text)),
            )

        threading.Thread(
            target=_run,
            name="tts_app_import_file_transcription",
            daemon=True,
        ).start()

    def _transcribe_import_file(self, path: str) -> tuple[bool, str]:
        from .transcriber import create_transcriber

        latest_overlay_opacity = int(
            self._settings_store.load().overlay_opacity_percent
        )
        settings = AppSettings(
            hotkey=self._loaded_settings.hotkey,
            cancel_hotkey=self._loaded_settings.cancel_hotkey,
            model_size=str(
                self.model_combo.currentData()
                or self._loaded_settings.model_size
            ),
            language_mode=str(
                self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE
            ),
            vad_enabled=self.vad_checkbox.isChecked(),
            vad_energy_threshold=float(self.vad_threshold_spin.value()),
            save_last_wav=self.save_wav_checkbox.isChecked(),
            save_all_recordings=self.save_all_recordings_checkbox.isChecked(),
            recordings_dir=self._effective_recordings_dir(),
            recordings_max_count=int(self.recordings_max_spin.value()),
            history_max_items=int(self.history_max_spin.value()),
            overlay_opacity_percent=latest_overlay_opacity,
            keep_transcript_in_clipboard=self.keep_clipboard_checkbox.isChecked(),
            offline_mode=self.offline_mode_checkbox.isChecked(),
            start_beep_enabled=self.start_beep_checkbox.isChecked(),
            start_beep_tone=str(
                self.start_beep_tone_combo.currentData() or DEFAULT_START_BEEP_TONE
            ),
            overlay_corner=str(
                self.overlay_corner_combo.currentData() or DEFAULT_OVERLAY_CORNER
            ),
            model_dir=self.model_dir_edit.text().strip(),
            engine=str(self.engine_combo.currentData() or DEFAULT_ENGINE),
            mode="batch",
            paste_mode=str(
                self.paste_mode_combo.currentData() or DEFAULT_PASTE_MODE
            ),
            has_openai_key=self._loaded_settings.has_openai_key,
            has_deepgram_key=self._loaded_settings.has_deepgram_key,
            has_assemblyai_key=self._loaded_settings.has_assemblyai_key,
            has_groq_key=self._loaded_settings.has_groq_key,
            groq_model=str(
                self.groq_model_combo.currentData() or DEFAULT_GROQ_MODEL
            ),
            openai_model=str(
                self.openai_model_combo.currentData() or DEFAULT_OPENAI_MODEL
            ),
        )
        if self._controller is not None:
            return self._controller.transcribe_audio_file(path)

        transcriber = create_transcriber(settings, secret_store=self._secret_store)
        text = transcriber.transcribe_batch(path)
        return True, str(text or "").strip()

    def _finish_import_transcription(self, ok: bool, text: str) -> None:
        self.import_file_button.setEnabled(True)
        if ok:
            self.import_result_label.setText("Transcription finished.")
            self.import_result_label.setStyleSheet("color: #1b5e20;")
            self.import_result_text.setPlainText(text)
            self._refresh_history_list()
            return
        self.import_result_label.setText(f"Failed: {text}")
        self.import_result_label.setStyleSheet("color: #b71c1c;")
        self.import_result_text.clear()

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
        cancel_hotkey = _qt_hotkey_sequence_to_app_hotkey(
            self.cancel_hotkey_edit.keySequence()
        )
        cancel_hotkey = cancel_hotkey or DEFAULT_CANCEL_HOTKEY
        try:
            parse_hotkey(hotkey)
        except ValueError as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Invalid hotkey",
                f"The hotkey is invalid: {exc}",
            )
            return
        try:
            parse_hotkey(cancel_hotkey)
        except ValueError as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Invalid cancel hotkey",
                f"The cancel hotkey is invalid: {exc}",
            )
            return
        if _hotkeys_conflict(hotkey, cancel_hotkey):
            QtWidgets.QMessageBox.critical(
                self,
                "Hotkey conflict",
                "Cancel hotkey must not be identical to, subset of, or superset "
                "of the main recording hotkey.",
            )
            return

        requested_history_limit = int(self.history_max_spin.value())
        current_history_count = self._history_store.count()
        history_limit_changed = (
            requested_history_limit != int(self._loaded_settings.history_max_items)
        )
        if (
            history_limit_changed
            and
            requested_history_limit > 0
            and current_history_count > requested_history_limit
        ):
            to_delete = current_history_count - requested_history_limit
            answer = QtWidgets.QMessageBox.question(
                self,
                "Reduce history size",
                (
                    f"Reducing the history limit to {requested_history_limit} will "
                    f"delete {to_delete} oldest entr{'y' if to_delete == 1 else 'ies'}.\n\n"
                    "Do you want to continue?"
                ),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
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

        latest_overlay_opacity = int(
            self._settings_store.load().overlay_opacity_percent
        )
        settings = AppSettings(
            hotkey=hotkey,
            cancel_hotkey=cancel_hotkey,
            model_size=str(self.model_combo.currentData()),
            language_mode=str(
                self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE
            ),
            vad_enabled=self.vad_checkbox.isChecked(),
            vad_energy_threshold=float(self.vad_threshold_spin.value()),
            save_last_wav=self.save_wav_checkbox.isChecked(),
            save_all_recordings=self.save_all_recordings_checkbox.isChecked(),
            recordings_dir=self._effective_recordings_dir(),
            recordings_max_count=int(self.recordings_max_spin.value()),
            history_max_items=requested_history_limit,
            overlay_opacity_percent=latest_overlay_opacity,
            keep_transcript_in_clipboard=(
                self.keep_clipboard_checkbox.isChecked()
            ),
            offline_mode=self.offline_mode_checkbox.isChecked(),
            start_beep_enabled=self.start_beep_checkbox.isChecked(),
            start_beep_tone=str(
                self.start_beep_tone_combo.currentData() or DEFAULT_START_BEEP_TONE
            ),
            overlay_corner=str(
                self.overlay_corner_combo.currentData() or DEFAULT_OVERLAY_CORNER
            ),
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

        if history_limit_changed and requested_history_limit > 0:
            self._history_store.apply_max_items(requested_history_limit)
        self._settings_store.save(settings)
        self._loaded_settings = settings
        self._save_status_label.setText("\u2713 Settings saved")
        self._save_status_timer.start()
        self.settings_changed.emit()


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


def _hotkeys_conflict(first: str, second: str) -> bool:
    left = _hotkey_token_set(first)
    right = _hotkey_token_set(second)
    if not left or not right:
        return False
    if left == right:
        return True
    return left.issubset(right) or right.issubset(left)


def _hotkey_token_set(value: str) -> set[str]:
    return {
        token.strip().upper()
        for token in str(value or "").split("+")
        if token.strip()
    }
