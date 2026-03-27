from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from .app_paths import debug_audio_path, recordings_dir
from .config import (
    ASSEMBLYAI_MODELS,
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_DEEPGRAM_MODEL,
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
    DEEPGRAM_MODELS,
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
from .last_recording_store import LastRecordingStore
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


_REMOTE_MODEL_LABELS: dict[str, str] = {
    "whisper-large-v3": "whisper-large-v3 (best quality, $0.111/hr)",
    "whisper-large-v3-turbo": "whisper-large-v3-turbo (faster, $0.04/hr)",
    "gpt-4o-mini-transcribe": "gpt-4o-mini-transcribe (fast, low cost)",
    "gpt-4o-transcribe": "gpt-4o-transcribe (higher quality)",
    "whisper-1": "whisper-1 (legacy whisper model)",
    "nova-3": "nova-3 (current default)",
    "nova-2": "nova-2 (older generation)",
    "best": "best (provider-managed default routing)",
    "nano": "nano (lower latency, lower cost)",
    "universal-3-pro": "universal-3-pro (latest premium batch model)",
    "universal": "universal (broad language coverage)",
    "slam-1": "slam-1 (speech understanding model)",
}

_REMOTE_MODEL_CHOICES: dict[str, tuple[tuple[str, str], ...]] = {
    "groq": tuple((value, _REMOTE_MODEL_LABELS.get(value, value)) for value in GROQ_MODELS),
    "openai": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in OPENAI_MODELS
    ),
    "deepgram": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in DEEPGRAM_MODELS
    ),
    "assemblyai": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value))
        for value in ASSEMBLYAI_MODELS
    ),
}

_REMOTE_MODEL_DEFAULTS: dict[str, str] = {
    "groq": DEFAULT_GROQ_MODEL,
    "openai": DEFAULT_OPENAI_MODEL,
    "deepgram": DEFAULT_DEEPGRAM_MODEL,
    "assemblyai": DEFAULT_ASSEMBLYAI_MODEL,
}


class SettingsDialog(QtWidgets.QDialog):
    connection_test_finished = QtCore.Signal(int, bool, str)
    import_transcription_finished = QtCore.Signal(bool, str)
    settings_changed = QtCore.Signal()

    def __init__(
        self,
        settings_store: SettingsStore,
        secret_store: SecretStore,
        app_logger: AppLogger,
        controller: DictationController | None = None,
        last_recording_store: LastRecordingStore | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_store = settings_store
        self._secret_store = secret_store
        self._app_logger = app_logger
        self._controller = controller
        self._history_store = TranscriptHistoryStore()
        self._last_recording_store = last_recording_store or LastRecordingStore()
        self._loaded_settings = self._settings_store.load()
        self._connection_test_id = 0
        self._connection_test_details: dict[int, dict[str, tuple[bool, str]]] = {}
        self._provider_key_edits: dict[str, QtWidgets.QLineEdit] = {}
        self._provider_status_labels: dict[str, QtWidgets.QLabel] = {}
        self._provider_last_test_labels: dict[str, QtWidgets.QLabel] = {}
        self._provider_pending_clear: set[str] = set()
        self._provider_test_history: dict[str, tuple[bool, str, str]] = {}
        self._remote_model_values: dict[str, str] = {
            "groq": self._loaded_settings.groq_model,
            "openai": self._loaded_settings.openai_model,
            "deepgram": getattr(
                self._loaded_settings,
                "deepgram_model",
                DEFAULT_DEEPGRAM_MODEL,
            ),
            "assemblyai": getattr(
                self._loaded_settings,
                "assemblyai_model",
                DEFAULT_ASSEMBLYAI_MODEL,
            ),
        }
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
        self.import_transcription_finished.connect(self._finish_import_transcription)
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
        for value in VALID_LANGUAGE_MODES:
            self.language_combo.addItem(
                LANGUAGE_MODE_LABELS.get(value, value), value
            )
        self.language_note_label = QtWidgets.QLabel("")
        self.language_note_label.setWordWrap(True)
        self.language_note_label.setStyleSheet("color: #555;")
        self.language_note_label.setVisible(True)
        self.language_note_label.setMinimumHeight(34)
        self.language_note_label.setMaximumHeight(34)

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

        self.save_wav_checkbox = QtWidgets.QCheckBox(
            "Keep last recording after successful transcription"
        )
        self.save_wav_path_label = QtWidgets.QLabel(
            "The current recording is always preserved until transcription "
            f"finishes. When enabled, the latest recording remains at: {debug_audio_path()}"
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
            "Keep transcript in clipboard after insertion"
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

        remote_model_box = QtWidgets.QGroupBox("Remote Speech Model")
        remote_model_form = QtWidgets.QFormLayout(remote_model_box)
        self.remote_model_provider_label = QtWidgets.QLabel("Local engine selected")
        self.remote_model_combo = QtWidgets.QComboBox()
        self.remote_model_combo.currentIndexChanged.connect(
            self._on_remote_model_changed
        )
        self.remote_model_note_label = QtWidgets.QLabel("")
        self.remote_model_note_label.setWordWrap(True)
        self.remote_model_note_label.setStyleSheet("color: #555;")
        remote_model_form.addRow("Active Provider", self.remote_model_provider_label)
        remote_model_form.addRow("Model", self.remote_model_combo)
        remote_model_form.addRow("", self.remote_model_note_label)
        layout.addWidget(remote_model_box)

        # API keys
        provider_box = QtWidgets.QGroupBox("Remote Provider API Keys")
        provider_layout = QtWidgets.QFormLayout(provider_box)
        provider_rows = (
            ("assemblyai", "AssemblyAI"),
            ("groq", "Groq"),
            ("openai", "OpenAI"),
            ("deepgram", "Deepgram"),
        )
        for provider, title in provider_rows:
            key_field = QtWidgets.QLineEdit()
            key_field.setEchoMode(QtWidgets.QLineEdit.Password)
            key_field.setPlaceholderText(
                "Enter new key to update; use Clear saved to remove the stored key."
            )
            key_field.textChanged.connect(
                lambda _text, p=provider: self._on_provider_key_changed(p)
            )
            clear_button = QtWidgets.QPushButton("Clear saved")
            clear_button.setToolTip("Delete the stored key for this provider on Save.")
            clear_button.clicked.connect(
                lambda _checked=False, p=provider: self._mark_provider_key_for_clear(p)
            )

            status_badge = QtWidgets.QLabel("Not configured")
            status_badge.setAlignment(
                QtCore.Qt.AlignCenter | QtCore.Qt.AlignVCenter
            )
            status_badge.setMinimumWidth(190)
            status_badge.setStyleSheet(
                "padding: 2px 8px; border: 1px solid #bbb; border-radius: 9px;"
                " color: #555; background: #f2f2f2;"
            )

            field_row_widget = QtWidgets.QWidget()
            field_row = QtWidgets.QHBoxLayout(field_row_widget)
            field_row.setContentsMargins(0, 0, 0, 0)
            field_row.setSpacing(8)
            field_row.addWidget(key_field, 1)
            field_row.addWidget(clear_button, 0)
            field_row.addWidget(status_badge, 0)
            provider_layout.addRow(title, field_row_widget)

            last_test_label = QtWidgets.QLabel("Last test: never.")
            last_test_label.setWordWrap(True)
            last_test_label.setStyleSheet("color: #666;")
            provider_layout.addRow("", last_test_label)

            self._provider_key_edits[provider] = key_field
            self._provider_status_labels[provider] = status_badge
            self._provider_last_test_labels[provider] = last_test_label

        self.assemblyai_key_edit = self._provider_key_edits["assemblyai"]
        self.groq_key_edit = self._provider_key_edits["groq"]
        self.openai_key_edit = self._provider_key_edits["openai"]
        self.deepgram_key_edit = self._provider_key_edits["deepgram"]
        provider_note = QtWidgets.QLabel(
            "Status badges show where each key is currently sourced from."
        )
        provider_note.setStyleSheet("color: #555;")
        provider_layout.addRow("", provider_note)

        self.insecure_key_storage_checkbox = QtWidgets.QCheckBox(
            "Allow insecure local API key fallback (plain text)"
        )
        self.insecure_key_storage_checkbox.setToolTip(
            "Use only if Credential Manager/keyring is blocked. "
            "Keys are then stored unencrypted in the app-data folder."
        )
        self.insecure_key_storage_checkbox.toggled.connect(
            lambda _checked: self._apply_secret_store_options()
        )
        provider_layout.addRow("", self.insecure_key_storage_checkbox)

        self.key_storage_status_label = QtWidgets.QLabel("")
        self.key_storage_status_label.setWordWrap(True)
        self.key_storage_status_label.setStyleSheet("color: #555;")
        provider_layout.addRow(self.key_storage_status_label)

        self.test_conn_target_combo = QtWidgets.QComboBox()
        self.test_conn_target_combo.addItem(
            "All configured providers (Recommended)",
            "all-configured",
        )
        self.test_conn_target_combo.addItem("AssemblyAI only", "assemblyai")
        self.test_conn_target_combo.addItem("Groq only", "groq")
        self.test_conn_target_combo.addItem("OpenAI only", "openai")
        self.test_conn_target_combo.addItem("Deepgram only", "deepgram")
        self.test_conn_target_combo.setToolTip(
            "Choose which provider to test. "
            "This is independent from the transcription engine selection."
        )
        provider_layout.addRow("Connection Target", self.test_conn_target_combo)

        # Test connection
        self.test_conn_button = QtWidgets.QPushButton("Run Connection Test")
        self.test_conn_button.setToolTip(
            "Test one provider or all configured providers. "
            "Typed key input is preferred over stored key."
        )
        self.test_conn_button.clicked.connect(self._test_connection)
        self.test_conn_result = QtWidgets.QLabel("")
        self.test_conn_result.setWordWrap(True)
        provider_layout.addRow(self.test_conn_button, self.test_conn_result)

        self._refresh_provider_key_statuses()

        layout.addWidget(provider_box)
        layout.addStretch(1)
        self.tabs.addTab(tab, "Remote")

    # --- History tab ---

    def _build_history_tab(self) -> None:
        tab = QtWidgets.QWidget()
        self._history_tab = tab
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
        self.history_delete_button = QtWidgets.QPushButton("Delete selected")
        self.history_delete_button.clicked.connect(self._delete_selected_history)
        self.history_delete_button.setEnabled(False)
        history_buttons.addWidget(self.history_refresh_button)
        history_buttons.addStretch(1)
        history_buttons.addWidget(self.history_copy_button)
        history_buttons.addWidget(self.history_delete_button)
        history_layout.addLayout(history_buttons)
        layout.addWidget(history_box, 2)

        import_box = QtWidgets.QGroupBox("Import Audio File")
        import_layout = QtWidgets.QVBoxLayout(import_box)
        import_hint = QtWidgets.QLabel(
            "Transcribe an existing audio file and select the transcription service "
            "directly here (useful after failures or for external recordings)."
        )
        import_hint.setWordWrap(True)
        import_hint.setStyleSheet("color: #555;")
        import_layout.addWidget(import_hint)

        self.import_engine_combo = QtWidgets.QComboBox()
        import_engine_labels = {
            "local": "Local (faster-whisper)",
            "assemblyai": "Remote (AssemblyAI)",
            "groq": "Remote (Groq)",
            "openai": "Remote (OpenAI)",
            "deepgram": "Remote (Deepgram)",
        }
        for value in VALID_ENGINES:
            self.import_engine_combo.addItem(
                import_engine_labels.get(value, value),
                value,
            )
        self.import_engine_note = QtWidgets.QLabel("")
        self.import_engine_note.setWordWrap(True)
        self.import_engine_note.setStyleSheet("color: #555;")
        self.import_engine_combo.currentIndexChanged.connect(
            self._update_import_engine_note
        )
        import_layout.addWidget(QtWidgets.QLabel("Import Service"))
        import_layout.addWidget(self.import_engine_combo)
        import_layout.addWidget(self.import_engine_note)

        import_buttons = QtWidgets.QHBoxLayout()
        self.import_file_button = QtWidgets.QPushButton("Choose file...")
        self.import_file_button.clicked.connect(self._choose_import_file)
        self.import_last_recording_button = QtWidgets.QPushButton("Use last recording")
        self.import_last_recording_button.clicked.connect(
            self._select_last_recording_file
        )
        self.import_start_button = QtWidgets.QPushButton("Start transcription")
        self.import_start_button.setEnabled(False)
        self.import_start_button.clicked.connect(self._confirm_and_transcribe_selected_file)
        import_buttons.addWidget(self.import_file_button)
        import_buttons.addWidget(self.import_last_recording_button)
        import_buttons.addWidget(self.import_start_button)
        import_buttons.addStretch(1)
        import_layout.addLayout(import_buttons)

        self.import_selected_file_label = QtWidgets.QLabel("No file selected.")
        self.import_selected_file_label.setWordWrap(True)
        self.import_selected_file_label.setStyleSheet("color: #555;")
        import_layout.addWidget(self.import_selected_file_label)

        self.import_result_label = QtWidgets.QLabel("")
        self.import_result_label.setWordWrap(True)
        import_layout.addWidget(self.import_result_label)

        self.import_result_text = QtWidgets.QPlainTextEdit()
        self.import_result_text.setReadOnly(True)
        import_layout.addWidget(self.import_result_text)

        self._selected_import_file_path = ""

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

    def _refresh_local_models_label(self, cached: list[str] | None = None) -> None:
        """Scan for locally cached models and update the label."""
        if cached is None:
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

    def _refresh_cached_models_list(self, cached: list[str] | None = None) -> None:
        if cached is None:
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
        model_dir = self.model_dir_edit.text().strip()
        try:
            cached = find_cached_models(model_dir)
        except Exception:
            cached = []
        self._refresh_local_models_label(cached)
        self._refresh_cached_models_list(cached)
        self._refresh_model_combo()
        self._update_language_availability()

    def _remote_model_value_for_provider(self, provider: str) -> str:
        normalized = str(provider or "").strip().lower()
        fallback = _REMOTE_MODEL_DEFAULTS.get(normalized, "")
        value = str(self._remote_model_values.get(normalized, fallback) or fallback)
        valid_values = {item_value for item_value, _label in _REMOTE_MODEL_CHOICES.get(normalized, ())}
        if value not in valid_values:
            return fallback
        return value

    def _update_remote_model_selector(self) -> None:
        if not hasattr(self, "remote_model_combo"):
            return

        provider = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        choices = _REMOTE_MODEL_CHOICES.get(provider, ())

        self.remote_model_combo.blockSignals(True)
        self.remote_model_combo.clear()

        if provider == DEFAULT_ENGINE:
            self.remote_model_provider_label.setText("Local engine selected")
            self.remote_model_combo.addItem("Not applicable for local engine", "")
            self.remote_model_combo.setEnabled(False)
            self.remote_model_note_label.setText(
                "Local transcription uses the faster-whisper model selected on the Local tab."
            )
            self.remote_model_combo.blockSignals(False)
            return

        for value, label in choices:
            self.remote_model_combo.addItem(label, value)
        self._select_combo_data(
            self.remote_model_combo,
            self._remote_model_value_for_provider(provider),
        )
        self.remote_model_provider_label.setText(self._provider_label(provider))
        self.remote_model_combo.setEnabled(True)

        note = (
            f"The selected API key is reused across {self._provider_label(provider)} models."
        )
        if provider == "assemblyai" and self.mode_combo.currentData() == "streaming":
            self.remote_model_combo.setEnabled(False)
            note = (
                "AssemblyAI streaming currently uses the SDK realtime default. "
                "The selected model applies to batch transcription and imports."
            )
        elif provider == "deepgram":
            note = "Deepgram uses the selected model for batch and streaming transcription."

        self.remote_model_note_label.setText(note)
        self.remote_model_combo.blockSignals(False)

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
        combo_model = self.language_combo.model()
        for idx in range(self.language_combo.count()):
            value = str(self.language_combo.itemData(idx) or "")
            item = combo_model.item(idx)
            if item is None:
                continue
            is_supported = value in supported_modes
            item.setEnabled(is_supported)
            if is_supported:
                item.setToolTip("")
            else:
                item.setToolTip("Not available for the current engine/mode/model.")

        target_mode = (
            selected_mode if selected_mode in supported_modes else supported_modes[0]
        )
        self._select_combo_data(self.language_combo, target_mode)
        self.language_combo.blockSignals(False)

        note = self._language_constraint_note()
        self.language_note_label.setText(note or " ")
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
        self._update_remote_model_selector()
        self._update_import_engine_note()

    def _on_mode_changed(self, _index: int = 0) -> None:
        self._update_language_availability()
        self._update_remote_model_selector()

    def _on_model_changed(self, _index: int = 0) -> None:
        self._update_language_availability()

    def _on_model_dir_changed(self, _text: str = "") -> None:
        """React to model directory changes — update cached model info."""
        self._refresh_local_model_views()

    def _on_remote_model_changed(self, _index: int = 0) -> None:
        provider = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        if provider == DEFAULT_ENGINE:
            return
        value = str(self.remote_model_combo.currentData() or "")
        if not value:
            value = _REMOTE_MODEL_DEFAULTS.get(provider, "")
        self._remote_model_values[provider] = value

    def _on_provider_key_changed(self, provider: str) -> None:
        key_field = self._provider_key_edits.get(provider)
        if key_field is not None and key_field.text().strip():
            self._provider_pending_clear.discard(provider)
        self._refresh_provider_key_status(provider)
        self._update_import_engine_note()

    def _provider_label(self, provider: str) -> str:
        labels = {
            "assemblyai": "AssemblyAI",
            "groq": "Groq",
            "openai": "OpenAI",
            "deepgram": "Deepgram",
        }
        return labels.get(provider, provider)

    def _stored_key_source(self, provider: str) -> str:
        source_getter = getattr(self._secret_store, "get_api_key_source", None)
        if callable(source_getter):
            try:
                value = str(source_getter(provider) or "none").strip().lower()
                return value or "none"
            except Exception:
                pass

        key_getter = getattr(self._secret_store, "get_api_key", None)
        if not callable(key_getter):
            return "none"
        try:
            return "keyring" if key_getter(provider) else "none"
        except Exception:
            return "none"

    def _set_provider_status_badge(
        self,
        provider: str,
        text: str,
        *,
        text_color: str,
        background: str,
        border: str,
    ) -> None:
        badge = self._provider_status_labels.get(provider)
        if badge is None:
            return
        badge.setText(text)
        badge.setStyleSheet(
            "padding: 2px 8px; border-radius: 9px; "
            f"border: 1px solid {border}; "
            f"color: {text_color}; "
            f"background: {background};"
        )

    def _refresh_provider_key_status(self, provider: str) -> None:
        key_field = self._provider_key_edits.get(provider)
        if key_field is None:
            return

        typed_value = key_field.text().strip()
        if typed_value:
            self._set_provider_status_badge(
                provider,
                "Unsaved input",
                text_color="#0d47a1",
                background="#e3f2fd",
                border="#90caf9",
            )
            return

        if provider in self._provider_pending_clear:
            self._set_provider_status_badge(
                provider,
                "Will clear on Save",
                text_color="#b26a00",
                background="#fff3e0",
                border="#ffcc80",
            )
            return

        source = self._stored_key_source(provider)
        if source in {"keyring", "legacy-keyring"}:
            label = "Stored securely"
            if source == "legacy-keyring":
                label = "Stored securely (legacy)"
            self._set_provider_status_badge(
                provider,
                label,
                text_color="#1b5e20",
                background="#e8f5e9",
                border="#a5d6a7",
            )
            return

        if source == "insecure":
            self._set_provider_status_badge(
                provider,
                "Stored insecure fallback",
                text_color="#7a4a00",
                background="#fff3e0",
                border="#ffcc80",
            )
            return

        if source == "insecure-disabled":
            self._set_provider_status_badge(
                provider,
                "Stored insecure (disabled)",
                text_color="#7a4a00",
                background="#fff8e1",
                border="#ffe082",
            )
            return

        self._set_provider_status_badge(
            provider,
            "Not configured",
            text_color="#555",
            background="#f2f2f2",
            border="#bbb",
        )

    def _refresh_provider_key_statuses(self) -> None:
        for provider in self._provider_key_edits:
            self._refresh_provider_key_status(provider)

    def _mark_provider_key_for_clear(self, provider: str) -> None:
        key_field = self._provider_key_edits.get(provider)
        if key_field is None:
            return
        key_field.clear()
        self._provider_pending_clear.add(provider)
        self._refresh_provider_key_status(provider)
        self._update_import_engine_note()

    def _import_engine_has_api_key(self, engine: str) -> bool:
        engine_name = str(engine or "").strip().lower()
        if engine_name == DEFAULT_ENGINE:
            return True
        key_field = self._provider_key_edits.get(engine_name)
        if key_field is None:
            return False
        return bool(self._resolve_api_key(engine_name, key_field))

    def _update_import_engine_note(self) -> None:
        if not hasattr(self, "import_engine_combo"):
            return
        engine = str(self.import_engine_combo.currentData() or DEFAULT_ENGINE)
        if engine == DEFAULT_ENGINE:
            self.import_engine_note.setStyleSheet("color: #555;")
            self.import_engine_note.setText(
                "Local import transcription uses the currently selected local model."
            )
            return
        if self._import_engine_has_api_key(engine):
            self.import_engine_note.setStyleSheet("color: #555;")
            self.import_engine_note.setText(
                f"Import transcription will use {self._provider_label(engine)}."
            )
            return
        self.import_engine_note.setStyleSheet("color: #b71c1c;")
        self.import_engine_note.setText(
            f"No API key configured for {self._provider_label(engine)}."
        )

    def _test_connection(self) -> None:
        """Test connectivity for one provider or all configured providers."""
        target = str(
            self.test_conn_target_combo.currentData() or "all-configured"
        )
        providers = self._providers_for_connection_target(target)
        if not providers:
            self._set_test_connection_feedback(
                "No configured provider keys found. Enter a key first.",
                "#b71c1c",
            )
            return

        for provider in providers:
            key_field = self._provider_key_edits.get(provider)
            if key_field is None:
                self._set_test_connection_feedback(
                    f"Unsupported provider: {provider}",
                    "#b71c1c",
                )
                return
            if not self._resolve_api_key(provider, key_field):
                self._set_test_connection_feedback(
                    f"No API key entered for {self._provider_label(provider)}.",
                    "#b71c1c",
                )
                return

        self._connection_test_id += 1
        test_id = self._connection_test_id
        self.test_conn_button.setEnabled(False)
        self.test_conn_target_combo.setEnabled(False)
        if len(providers) == 1:
            provider_label = self._provider_label(providers[0])
            self._set_test_connection_feedback(
                f"Testing {provider_label}...",
                "#555",
            )
        else:
            self._set_test_connection_feedback(
                "Testing all configured providers...",
                "#555",
            )
        worker = threading.Thread(
            target=self._run_connection_test_worker,
            args=(test_id, tuple(providers)),
            name="stt_app_settings_connection_test",
            daemon=True,
        )
        self._active_connection_test_thread = worker
        worker.start()

    def _providers_for_connection_target(self, target: str) -> list[str]:
        normalized = str(target or "").strip().lower()
        remote_providers = ("assemblyai", "groq", "openai", "deepgram")
        if normalized == "all-configured":
            configured: list[str] = []
            for provider in remote_providers:
                key_field = self._provider_key_edits.get(provider)
                if key_field is None:
                    continue
                if self._resolve_api_key(provider, key_field):
                    configured.append(provider)
            return configured
        if normalized in remote_providers:
            return [normalized]
        return []

    def _build_connection_tester(self, engine: str):
        if engine == "assemblyai":
            api_key = self._resolve_api_key("assemblyai", self.assemblyai_key_edit)
            if not api_key:
                return (
                    None,
                    "No API key entered. Enter a key above first.",
                )

            from .transcriber.assemblyai_provider import AssemblyAITranscriber

            transcriber = AssemblyAITranscriber(
                api_key=api_key,
                model=self._remote_model_value_for_provider("assemblyai"),
            )
            return transcriber.test_connection, None

        if engine == "groq":
            api_key = self._resolve_api_key("groq", self.groq_key_edit)
            if not api_key:
                return (
                    None,
                    "No API key entered. Enter a key above first.",
                )

            from .transcriber.groq_provider import GroqTranscriber

            transcriber = GroqTranscriber(
                api_key=api_key,
                model=self._remote_model_value_for_provider("groq"),
            )
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
                    self._remote_model_value_for_provider("openai")
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

            transcriber = DeepgramTranscriber(
                api_key=api_key,
                model=self._remote_model_value_for_provider("deepgram"),
            )
            return transcriber.test_connection, None

        return None, None

    def _resolve_api_key(self, provider: str, key_field: QtWidgets.QLineEdit) -> str:
        api_key = key_field.text().strip()
        if api_key:
            return api_key
        key_getter = getattr(self._secret_store, "get_api_key", None)
        if not callable(key_getter):
            return ""
        try:
            return str(key_getter(provider) or "")
        except Exception:
            return ""

    def _run_connection_test_worker(
        self,
        test_id: int,
        providers: tuple[str, ...],
    ) -> None:
        results: dict[str, tuple[bool, str]] = {}
        for provider in providers:
            tester, error_text = self._build_connection_tester(provider)
            if tester is None:
                if error_text:
                    results[provider] = (False, error_text)
                else:
                    results[provider] = (
                        False,
                        f"Connection test not implemented for {provider}.",
                    )
                continue
            try:
                ok, msg = tester()
            except Exception as exc:
                ok, msg = False, f"Test failed: {exc}"
            results[provider] = (bool(ok), str(msg))

        self._connection_test_details[test_id] = results
        success_count = sum(1 for provider_ok, _ in results.values() if provider_ok)
        total_count = len(results)
        all_ok = total_count > 0 and success_count == total_count
        if total_count <= 1:
            if total_count == 1:
                only_provider = next(iter(results))
                summary = results[only_provider][1]
            else:
                summary = "No providers tested."
        else:
            summary = f"{success_count}/{total_count} provider tests passed."
        self.connection_test_finished.emit(test_id, all_ok, summary)

    @QtCore.Slot(int, bool, str)
    def _on_connection_test_finished(self, test_id: int, ok: bool, msg: str) -> None:
        details = self._connection_test_details.pop(test_id, {})
        if test_id != self._connection_test_id:
            return
        self.test_conn_button.setEnabled(True)
        self.test_conn_target_combo.setEnabled(True)
        self._active_connection_test_thread = None
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for provider, (provider_ok, provider_msg) in details.items():
            self._provider_test_history[provider] = (
                bool(provider_ok),
                str(provider_msg),
                timestamp,
            )
            last_label = self._provider_last_test_labels.get(provider)
            if last_label is None:
                continue
            marker = "\u2713" if provider_ok else "\u2717"
            color = "#1b5e20" if provider_ok else "#b71c1c"
            last_label.setStyleSheet(f"color: {color};")
            last_label.setText(
                f"Last test ({timestamp}): {marker} {provider_msg}"
            )

        if len(details) > 1:
            parts = []
            for provider in ("assemblyai", "groq", "openai", "deepgram"):
                if provider not in details:
                    continue
                provider_ok, _provider_msg = details[provider]
                marker = "OK" if provider_ok else "Fail"
                parts.append(f"{self._provider_label(provider)}: {marker}")
            color = "#1b5e20" if ok else "#b26a00"
            joined = " | ".join(parts)
            self._set_test_connection_feedback(f"{msg} {joined}", color)
            return

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
        self.insecure_key_storage_checkbox.setChecked(
            bool(getattr(settings, "allow_insecure_key_storage", False))
        )
        self.offline_mode_checkbox.setChecked(settings.offline_mode)
        self._select_combo_data(self.engine_combo, settings.engine)
        self._select_combo_data(self.mode_combo, settings.mode)
        self._update_mode_availability()
        self._update_language_availability(preferred_mode=settings.language_mode)
        self._select_combo_data(self.paste_mode_combo, settings.paste_mode)
        self._remote_model_values.update(
            {
                "groq": settings.groq_model,
                "openai": settings.openai_model,
                "deepgram": getattr(
                    settings,
                    "deepgram_model",
                    DEFAULT_DEEPGRAM_MODEL,
                ),
                "assemblyai": getattr(
                    settings,
                    "assemblyai_model",
                    DEFAULT_ASSEMBLYAI_MODEL,
                ),
            }
        )
        self._update_remote_model_selector()
        self._select_combo_data(self.test_conn_target_combo, "all-configured")
        if hasattr(self, "import_engine_combo"):
            self._select_combo_data(self.import_engine_combo, settings.engine)
            self._update_import_engine_note()

        self._update_engine_indicator()
        self._refresh_history_list()
        self._apply_secret_store_options()
        self._refresh_provider_key_statuses()

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
        self.history_delete_button.setEnabled(False)
        entries = self._history_store.recent_entries(self.history_max_spin.value())
        for entry in entries:
            text = entry.text.strip().replace("\n", " ")
            preview = text[:70] + ("..." if len(text) > 70 else "")
            label = f"{entry.created_at} | {entry.engine}/{entry.model} | {preview}"
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, entry)
            self.history_list.addItem(item)

    def _on_history_item_selected(self) -> None:
        items = self.history_list.selectedItems()
        if not items:
            self.history_copy_button.setEnabled(False)
            self.history_delete_button.setEnabled(False)
            self.history_detail.clear()
            self._reset_history_copy_feedback()
            return
        entry = items[0].data(QtCore.Qt.UserRole)
        text = str(getattr(entry, "text", "") or "")
        self.history_copy_button.setEnabled(bool(text))
        self.history_delete_button.setEnabled(True)
        self.history_detail.setPlainText(text)
        self._reset_history_copy_feedback()

    def _copy_selected_history(self) -> None:
        items = self.history_list.selectedItems()
        if not items:
            return
        entry = items[0].data(QtCore.Qt.UserRole)
        text = str(getattr(entry, "text", "") or "")
        if not text:
            return
        QtGui.QGuiApplication.clipboard().setText(text)
        self.history_copy_button.setText("Copied")
        self.history_copy_button.setStyleSheet(
            "background-color: #dff5e0; border: 1px solid #89c88f;"
        )
        self._history_copy_feedback_timer.start()

    def _delete_selected_history(self) -> None:
        items = self.history_list.selectedItems()
        if not items:
            return
        entry = items[0].data(QtCore.Qt.UserRole)
        if entry is None:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete history entry",
            "Delete the selected transcription from history?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        removed = self._history_store.delete_entry(entry)
        if removed <= 0:
            self.import_result_label.setText("Selected history entry was not found.")
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            return
        self._refresh_history_list()

    def _reset_history_copy_feedback(self) -> None:
        self.history_copy_button.setText("Copy selected")
        self.history_copy_button.setStyleSheet("")

    def _set_selected_import_file(self, path: str) -> None:
        selected = str(path or "").strip()
        self._selected_import_file_path = selected
        if selected:
            self.import_selected_file_label.setText(f"Selected: {selected}")
            self.import_selected_file_label.setStyleSheet("color: #1b5e20;")
            self.import_start_button.setEnabled(True)
        else:
            self.import_selected_file_label.setText("No file selected.")
            self.import_selected_file_label.setStyleSheet("color: #555;")
            self.import_start_button.setEnabled(False)

    def _choose_import_file(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select audio file",
            "",
            "Audio files (*.wav *.mp3 *.m4a *.flac *.ogg *.opus *.webm);;All files (*)",
        )
        if not path:
            return
        self._set_selected_import_file(path)

    def _select_last_recording_file(self) -> bool:
        path = self._last_recording_store.selectable_path()
        if path is None:
            self.import_result_label.setText(
                "No last recording is currently available."
            )
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            self.import_result_text.clear()
            return False
        self._set_selected_import_file(str(path))
        self.import_result_label.setText(
            "Last recording loaded. Choose a provider and start transcription."
        )
        self.import_result_label.setStyleSheet("color: #555;")
        return True

    def prepare_last_recording_import(self) -> bool:
        history_index = self.tabs.indexOf(self._history_tab)
        if history_index >= 0:
            self.tabs.setCurrentIndex(history_index)
        return self._select_last_recording_file()

    def _confirm_and_transcribe_selected_file(self) -> None:
        path = self._selected_import_file_path
        if not path:
            self.import_result_label.setText("Select a file first.")
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Start transcription",
            f"Transcribe selected file?\n\n{path}",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self._start_import_transcription(path)

    def _start_import_transcription(self, path: str) -> None:
        self.import_result_label.setText("Transcribing...")
        self.import_result_label.setStyleSheet("color: #555;")
        self.import_result_text.clear()
        self.import_file_button.setEnabled(False)
        self.import_last_recording_button.setEnabled(False)
        self.import_start_button.setEnabled(False)
        self.import_engine_combo.setEnabled(False)

        # Build settings on the GUI thread — widgets must not be accessed
        # from background threads.
        import_engine = str(
            self.import_engine_combo.currentData() or DEFAULT_ENGINE
        )
        if not self._import_engine_has_api_key(import_engine):
            detail = (
                "Failed: no API key configured for "
                f"{self._provider_label(import_engine)}."
            )
            if self._last_recording_store.is_managed_audio_path(path):
                detail = (
                    f"{detail} The last recording stays available. "
                    "Fix the provider settings and try again."
                )
            self.import_result_label.setText(
                detail
            )
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            self.import_file_button.setEnabled(True)
            self.import_last_recording_button.setEnabled(True)
            self.import_start_button.setEnabled(bool(self._selected_import_file_path))
            self.import_engine_combo.setEnabled(True)
            return
        settings = self._build_current_settings(engine_override=import_engine)

        def _run() -> None:
            try:
                ok, text = self._transcribe_import_file(path, settings)
            except Exception as exc:
                ok, text = False, str(exc)
            self.import_transcription_finished.emit(bool(ok), str(text))

        threading.Thread(
            target=_run,
            name="stt_app_import_file_transcription",
            daemon=True,
        ).start()

    def _transcribe_import_file(
        self, path: str, settings: AppSettings
    ) -> tuple[bool, str]:
        from .transcriber import create_transcriber

        if self._controller is not None:
            return self._controller.transcribe_audio_file(
                path,
                settings_override=settings,
            )

        transcriber = create_transcriber(settings, secret_store=self._secret_store)
        text = transcriber.transcribe_batch(path)
        return True, str(text or "").strip()

    def _finish_import_transcription(self, ok: bool, text: str) -> None:
        self.import_file_button.setEnabled(True)
        self.import_last_recording_button.setEnabled(True)
        self.import_start_button.setEnabled(bool(self._selected_import_file_path))
        self.import_engine_combo.setEnabled(True)
        if ok:
            self.import_result_label.setText("Transcription finished.")
            self.import_result_label.setStyleSheet("color: #1b5e20;")
            self.import_result_text.setPlainText(text)
            self._refresh_history_list()
            return
        detail = f"Failed: {text}"
        if self._last_recording_store.is_managed_audio_path(
            self._selected_import_file_path
        ):
            detail = (
                f"{detail} The last recording remains available. "
                "Fix the settings and try again."
            )
        self.import_result_label.setText(detail)
        self.import_result_label.setStyleSheet("color: #b71c1c;")
        self.import_result_text.clear()

    def _copy_diagnostics(self) -> None:
        text = self._app_logger.diagnostics_text()
        clipboard = QtGui.QGuiApplication.clipboard()
        clipboard.setText(text)

    def _apply_secret_store_options(self) -> None:
        enabled = self.insecure_key_storage_checkbox.isChecked()
        setter = getattr(self._secret_store, "set_insecure_fallback_enabled", None)
        if callable(setter):
            try:
                setter(enabled)
            except Exception:
                pass
        if enabled:
            self.key_storage_status_label.setStyleSheet("color: #b26a00;")
            self.key_storage_status_label.setText(
                "Insecure key fallback is enabled. "
                "If secure storage fails, keys are saved in plain text."
            )
        elif not self.key_storage_status_label.text().startswith("Could not store"):
            self.key_storage_status_label.setStyleSheet("color: #555;")
            self.key_storage_status_label.setText(
                "Credential Manager only (recommended)."
            )
        self._refresh_provider_key_statuses()

    def _build_current_settings(
        self,
        *,
        engine_override: str | None = None,
    ) -> AppSettings:
        """Construct an ``AppSettings`` from current widget state.

        Must be called on the GUI thread.
        """
        latest_overlay_opacity = int(
            self._settings_store.load().overlay_opacity_percent
        )
        return AppSettings(
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
            allow_insecure_key_storage=self.insecure_key_storage_checkbox.isChecked(),
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
                engine_override or self.engine_combo.currentData() or DEFAULT_ENGINE
            ),
            mode=str(self.mode_combo.currentData() or DEFAULT_MODE),
            paste_mode=str(
                self.paste_mode_combo.currentData() or DEFAULT_PASTE_MODE
            ),
            has_openai_key=self._loaded_settings.has_openai_key,
            has_deepgram_key=self._loaded_settings.has_deepgram_key,
            has_assemblyai_key=self._loaded_settings.has_assemblyai_key,
            has_groq_key=self._loaded_settings.has_groq_key,
            groq_model=self._remote_model_value_for_provider("groq"),
            openai_model=self._remote_model_value_for_provider("openai"),
            deepgram_model=self._remote_model_value_for_provider("deepgram"),
            assemblyai_model=self._remote_model_value_for_provider("assemblyai"),
        )

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

        has_openai_key = bool(self._resolve_api_key("openai", self.openai_key_edit))
        has_deepgram_key = bool(
            self._resolve_api_key("deepgram", self.deepgram_key_edit)
        )
        has_assemblyai_key = bool(
            self._resolve_api_key("assemblyai", self.assemblyai_key_edit)
        )
        has_groq_key = bool(self._resolve_api_key("groq", self.groq_key_edit))

        self._apply_secret_store_options()
        key_storage_errors: list[str] = []
        pending_clear = set(self._provider_pending_clear)

        openai_value = self.openai_key_edit.text().strip()
        deepgram_value = self.deepgram_key_edit.text().strip()
        assemblyai_value = self.assemblyai_key_edit.text().strip()
        groq_value = self.groq_key_edit.text().strip()

        if openai_value:
            try:
                self._secret_store.set_api_key("openai", openai_value)
                self.openai_key_edit.clear()
                has_openai_key = bool(
                    self._resolve_api_key("openai", self.openai_key_edit)
                )
            except Exception as exc:
                key_storage_errors.append(f"OpenAI: {exc}")
        elif "openai" in pending_clear:
            try:
                self._secret_store.delete_api_key("openai")
                has_openai_key = False
            except Exception as exc:
                key_storage_errors.append(f"OpenAI delete: {exc}")
        if deepgram_value:
            try:
                self._secret_store.set_api_key("deepgram", deepgram_value)
                self.deepgram_key_edit.clear()
                has_deepgram_key = bool(
                    self._resolve_api_key("deepgram", self.deepgram_key_edit)
                )
            except Exception as exc:
                key_storage_errors.append(f"Deepgram: {exc}")
        elif "deepgram" in pending_clear:
            try:
                self._secret_store.delete_api_key("deepgram")
                has_deepgram_key = False
            except Exception as exc:
                key_storage_errors.append(f"Deepgram delete: {exc}")
        if assemblyai_value:
            try:
                self._secret_store.set_api_key("assemblyai", assemblyai_value)
                self.assemblyai_key_edit.clear()
                has_assemblyai_key = bool(
                    self._resolve_api_key("assemblyai", self.assemblyai_key_edit)
                )
            except Exception as exc:
                key_storage_errors.append(f"AssemblyAI: {exc}")
        elif "assemblyai" in pending_clear:
            try:
                self._secret_store.delete_api_key("assemblyai")
                has_assemblyai_key = False
            except Exception as exc:
                key_storage_errors.append(f"AssemblyAI delete: {exc}")
        if groq_value:
            try:
                self._secret_store.set_api_key("groq", groq_value)
                self.groq_key_edit.clear()
                has_groq_key = bool(
                    self._resolve_api_key("groq", self.groq_key_edit)
                )
            except Exception as exc:
                key_storage_errors.append(f"Groq: {exc}")
        elif "groq" in pending_clear:
            try:
                self._secret_store.delete_api_key("groq")
                has_groq_key = False
            except Exception as exc:
                key_storage_errors.append(f"Groq delete: {exc}")

        self._provider_pending_clear.clear()

        if key_storage_errors:
            self.key_storage_status_label.setStyleSheet("color: #b71c1c;")
            self.key_storage_status_label.setText(
                "Could not store some API keys in Credential Manager. "
                "Enable insecure fallback storage or retry. "
                + " | ".join(key_storage_errors)
            )
        else:
            self.key_storage_status_label.setStyleSheet("color: #1b5e20;")
            if any((openai_value, deepgram_value, assemblyai_value, groq_value)) or pending_clear:
                self.key_storage_status_label.setText("API key storage updated.")
        self._refresh_provider_key_statuses()
        self._update_import_engine_note()

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
            allow_insecure_key_storage=self.insecure_key_storage_checkbox.isChecked(),
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
            groq_model=self._remote_model_value_for_provider("groq"),
            openai_model=self._remote_model_value_for_provider("openai"),
            deepgram_model=self._remote_model_value_for_provider("deepgram"),
            assemblyai_model=self._remote_model_value_for_provider("assemblyai"),
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
