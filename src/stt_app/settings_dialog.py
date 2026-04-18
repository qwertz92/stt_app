from __future__ import annotations

import threading
from dataclasses import replace
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
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_GROQ_MODEL,
    DEFAULT_HISTORY_MAX_ITEMS,
    DEFAULT_HOTKEY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_MODEL_SIZE,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OVERLAY_CORNER,
    DEFAULT_PASTE_MODE,
    DEFAULT_RECORDINGS_MAX_COUNT,
    DEFAULT_START_BEEP_TONE,
    DEFAULT_VAD_ENERGY_THRESHOLD,
    DOC_MODELS_PATH,
    DEEPGRAM_MODELS,
    ELEVENLABS_MODELS,
    ENGINE_LANGUAGE_MODES,
    GROQ_MODELS,
    HISTORY_MAX_ITEMS_MAX,
    LANGUAGE_MODE_LABELS,
    LOCAL_BATCH_ONLY_MODELS,
    LOCAL_ENGLISH_ONLY_MODELS,
    LOCAL_EXPLICIT_LANGUAGE_MODELS,
    LOCAL_WEBGPU_MODEL_SIZES,
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
from .local_model_inventory_store import LocalModelInventoryStore
from .local_benchmark import (
    BenchmarkCase,
    _format_number,
    _format_seconds,
    format_benchmark_summary,
    run_benchmark_cases,
)
from .logger import AppLogger
from .secret_store import SecretStore
from .settings_store import AppSettings, SettingsStore
from .transcript_history import TranscriptHistoryStore
from .transcriber.local_faster_whisper import (
    delete_cached_model,
    download_model_snapshot,
    find_cached_models,
)

if TYPE_CHECKING:
    from .controller import DictationController


class _WheelPassthroughComboBox(QtWidgets.QComboBox):
    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        view = self.view()
        if view is not None and view.isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


class _WheelPassthroughSpinBox(QtWidgets.QSpinBox):
    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        event.ignore()


class _WheelPassthroughDoubleSpinBox(QtWidgets.QDoubleSpinBox):
    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        event.ignore()


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
    "scribe_v2": "scribe_v2 (current default, highest published accuracy)",
    "scribe_v1": "scribe_v1 (legacy batch model)",
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
    "elevenlabs": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value))
        for value in ELEVENLABS_MODELS
    ),
}

_DEFAULT_SETTINGS_DIALOG_SIZE = QtCore.QSize(680, 720)
_DIALOG_SCREEN_MARGIN = 48
_COMPACT_LIST_ITEM_STYLESHEET = "QListWidget::item { padding: 1px 4px; }"

_REMOTE_MODEL_DEFAULTS: dict[str, str] = {
    "groq": DEFAULT_GROQ_MODEL,
    "openai": DEFAULT_OPENAI_MODEL,
    "deepgram": DEFAULT_DEEPGRAM_MODEL,
    "assemblyai": DEFAULT_ASSEMBLYAI_MODEL,
    "elevenlabs": DEFAULT_ELEVENLABS_MODEL,
}

_LOCAL_MODEL_SCAN_SESSION_CACHE: dict[str, list[str]] = {}


class SettingsDialog(QtWidgets.QDialog):
    connection_test_finished = QtCore.Signal(int, bool, str)
    import_transcription_finished = QtCore.Signal(bool, str)
    local_model_scan_finished = QtCore.Signal(int, str, object)
    local_model_download_progress = QtCore.Signal(str)
    local_model_download_finished = QtCore.Signal(bool, str)
    benchmark_progress = QtCore.Signal(str)
    benchmark_finished = QtCore.Signal(bool, str, object)
    settings_changed = QtCore.Signal()

    def __init__(
        self,
        settings_store: SettingsStore,
        secret_store: SecretStore,
        app_logger: AppLogger,
        controller: DictationController | None = None,
        last_recording_store: LastRecordingStore | None = None,
        local_model_inventory_store: LocalModelInventoryStore | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_store = settings_store
        self._secret_store = secret_store
        self._app_logger = app_logger
        self._controller = controller
        self._history_store = TranscriptHistoryStore()
        self._last_recording_store = last_recording_store or LastRecordingStore()
        self._local_model_inventory_store = local_model_inventory_store
        self._loaded_settings = self._settings_store.load()
        self._connection_test_id = 0
        self._connection_test_details: dict[int, dict[str, tuple[bool, str]]] = {}
        self._provider_key_edits: dict[str, QtWidgets.QLineEdit] = {}
        self._provider_status_labels: dict[str, QtWidgets.QLabel] = {}
        self._provider_last_test_labels: dict[str, QtWidgets.QLabel] = {}
        self._provider_pending_clear: set[str] = set()
        self._provider_test_history: dict[str, tuple[bool, str, str]] = {}
        self._active_local_model_scan_thread: threading.Thread | None = None
        self._local_model_scan_token = 0
        self._local_model_scan_pending = False
        self._cached_local_models: list[str] = []
        self._cached_local_models_dir = ""
        self._cached_local_models_available = False
        self._local_model_auto_refresh_requested_dirs: set[str] = set()
        self._local_model_auto_refreshed_dirs: set[str] = set()
        self._local_tab_index: int | None = None
        self._benchmark_tab_index: int | None = None
        self._active_local_model_download_thread: threading.Thread | None = None
        self._active_benchmark_thread: threading.Thread | None = None
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
            "elevenlabs": getattr(
                self._loaded_settings,
                "elevenlabs_model",
                DEFAULT_ELEVENLABS_MODEL,
            ),
        }
        self._import_model_values: dict[str, str] = {
            "local": self._loaded_settings.model_size,
            "groq": self._remote_model_values["groq"],
            "openai": self._remote_model_values["openai"],
            "deepgram": self._remote_model_values["deepgram"],
            "assemblyai": self._remote_model_values["assemblyai"],
            "elevenlabs": self._remote_model_values["elevenlabs"],
        }
        self._active_connection_test_thread: threading.Thread | None = None
        self._history_copy_feedback_timer = QtCore.QTimer(self)
        self._history_copy_feedback_timer.setSingleShot(True)
        self._history_copy_feedback_timer.setInterval(900)
        self._history_copy_feedback_timer.timeout.connect(
            self._reset_history_copy_feedback
        )
        self._deferred_local_model_refresh_timer = QtCore.QTimer(self)
        self._deferred_local_model_refresh_timer.setSingleShot(True)
        self._deferred_local_model_refresh_timer.timeout.connect(
            self._run_deferred_local_model_refresh
        )
        self._deferred_local_model_refresh_pending = False
        self._deferred_local_model_refresh_force = False
        self._initial_dialog_size_applied = False

        self.setWindowTitle("Dictation Settings")
        self.setModal(False)
        self.setWindowFlag(QtCore.Qt.Window, True)
        self.setWindowFlag(QtCore.Qt.WindowSystemMenuHint, True)
        self.setWindowFlag(QtCore.Qt.WindowMinimizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowMaximizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowCloseButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, False)
        self.setMinimumSize(520, 400)
        self._default_dialog_size = QtCore.QSize(_DEFAULT_SETTINGS_DIALOG_SIZE)
        self.resize(self._default_dialog_size)

        self.connection_test_finished.connect(self._on_connection_test_finished)
        self.import_transcription_finished.connect(self._finish_import_transcription)
        self.local_model_scan_finished.connect(self._on_local_model_scan_finished)
        self.local_model_download_progress.connect(
            self._on_local_model_download_progress
        )
        self.local_model_download_finished.connect(
            self._on_local_model_download_finished
        )
        self.benchmark_progress.connect(self._on_benchmark_progress)
        self.benchmark_finished.connect(self._on_benchmark_finished)
        self._build_ui()
        self.tabs.currentChanged.connect(self._on_settings_tab_changed)
        self._populate(self._loaded_settings)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(self._dialog_scrollbar_stylesheet())
        # --- Engine indicator bar (always visible) ---
        self.engine_indicator = QtWidgets.QLabel()
        self.engine_indicator.setAlignment(QtCore.Qt.AlignCenter)
        self.engine_indicator.setStyleSheet(
            "font-weight: bold; padding: 4px; border-radius: 4px;"
        )

        # --- Tab widget ---
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.tabs.tabBar().setElideMode(QtCore.Qt.ElideRight)
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
        self._build_import_tab()
        self._build_benchmark_tab()
        self._configure_combo_popups()

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
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(self.engine_indicator)
        root.addWidget(self.tabs, 1)
        root.addLayout(buttons)

    def _restore_default_dialog_size(self) -> None:
        target_size = self._refresh_default_dialog_size()
        self.resize(target_size)

    def _refresh_default_dialog_size(self) -> QtCore.QSize:
        target_size = self._preferred_dialog_size()
        available_size = self._available_dialog_size()
        if available_size.isValid():
            target_size = target_size.boundedTo(available_size)
        self._default_dialog_size = QtCore.QSize(target_size)
        return QtCore.QSize(target_size)

    def _preferred_dialog_size(self) -> QtCore.QSize:
        preferred = QtCore.QSize(_DEFAULT_SETTINGS_DIALOG_SIZE)
        if not hasattr(self, "tabs"):
            return preferred

        self.ensurePolished()
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.activate()

        current_index = self.tabs.currentIndex()
        updates_enabled = self.updatesEnabled()
        blocker = QtCore.QSignalBlocker(self.tabs)
        self.setUpdatesEnabled(False)
        try:
            for index in range(self.tabs.count()):
                self.tabs.setCurrentIndex(index)
                if root_layout is not None:
                    root_layout.activate()
                preferred = preferred.expandedTo(self.sizeHint())
        finally:
            self.tabs.setCurrentIndex(current_index)
            del blocker
            self.setUpdatesEnabled(updates_enabled)
            if root_layout is not None:
                root_layout.activate()

        return preferred.expandedTo(self.minimumSize())

    def _available_dialog_size(self) -> QtCore.QSize:
        screen = (
            self.screen()
            or QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
            or QtGui.QGuiApplication.primaryScreen()
        )
        if screen is None:
            return QtCore.QSize()
        geometry = screen.availableGeometry()
        return QtCore.QSize(
            max(0, geometry.width() - _DIALOG_SCREEN_MARGIN),
            max(0, geometry.height() - _DIALOG_SCREEN_MARGIN),
        )

    def _style_note_label(self, label: QtWidgets.QLabel, *, bold: bool = False) -> None:
        style = "color: #555; font-size: 11px; padding: 0 0 6px 0;"
        if bold:
            style += " font-weight: bold;"
        label.setStyleSheet(style)

    @staticmethod
    def _field_with_hint(
        control: QtWidgets.QWidget,
        hint: QtWidgets.QLabel,
    ) -> QtWidgets.QWidget:
        """Wrap *control* and its *hint* label in a tight vertical group."""
        wrapper = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(control)
        layout.addWidget(hint)
        return wrapper

    @staticmethod
    def _configure_compact_list_widget(
        widget: QtWidgets.QListWidget,
        *,
        expand: bool = False,
        adjust_to_contents: bool = False,
    ) -> None:
        widget.setUniformItemSizes(True)
        widget.setSpacing(0)
        widget.setStyleSheet(_COMPACT_LIST_ITEM_STYLESHEET)
        widget.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        if expand:
            widget.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,
                QtWidgets.QSizePolicy.Expanding,
            )
        if adjust_to_contents:
            widget.setSizeAdjustPolicy(
                QtWidgets.QAbstractScrollArea.AdjustToContents
            )

    @staticmethod
    def _minimum_list_height_for_rows(
        widget: QtWidgets.QListWidget,
        row_count: int,
    ) -> int:
        effective_rows = max(1, int(row_count))
        row_height = widget.sizeHintForRow(0)
        if row_height <= 0:
            row_height = widget.fontMetrics().height() + 10
        frame = widget.frameWidth() * 2
        return frame + (row_height * effective_rows) + 2

    def _dialog_scrollbar_stylesheet(self) -> str:
        return """
        QScrollBar:vertical {
            width: 12px;
            background: transparent;
            margin: 0;
        }
        QScrollBar::handle:vertical {
            min-height: 36px;
            background: #c4cdd8;
            border-radius: 6px;
        }
        QScrollBar::handle:vertical:hover {
            background: #b0bac6;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0;
        }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
            background: transparent;
        }
        QScrollBar:horizontal {
            height: 12px;
            background: transparent;
            margin: 0;
        }
        QScrollBar::handle:horizontal {
            min-width: 36px;
            background: #c4cdd8;
            border-radius: 6px;
        }
        QScrollBar::handle:horizontal:hover {
            background: #b0bac6;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
            width: 0;
        }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
            background: transparent;
        }
        """

    def _configure_combo_popups(self) -> None:
        for combo in self.findChildren(QtWidgets.QComboBox):
            view = QtWidgets.QListView(combo)
            view.setUniformItemSizes(True)
            view.setLayoutMode(QtWidgets.QListView.SinglePass)
            view.setSpacing(0)
            view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerItem)
            combo.setView(view)
            combo.setMaxVisibleItems(12)

    def _create_scroll_tab(self) -> tuple[QtWidgets.QScrollArea, QtWidgets.QWidget]:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        content = QtWidgets.QWidget()
        content.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred,
            QtWidgets.QSizePolicy.Expanding,
        )
        scroll.setWidget(content)
        return scroll, content

    # --- General tab ---

    def _build_general_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # --- Hotkeys section ---
        hotkey_box = QtWidgets.QGroupBox("Hotkeys")
        hotkey_form = QtWidgets.QFormLayout(hotkey_box)
        hotkey_form.setContentsMargins(10, 10, 10, 10)
        hotkey_form.setHorizontalSpacing(10)
        hotkey_form.setVerticalSpacing(6)
        hotkey_form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        self.hotkey_edit = QtWidgets.QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        if hasattr(self.hotkey_edit, "setClearButtonEnabled"):
            self.hotkey_edit.setClearButtonEnabled(True)
        hotkey_hint = QtWidgets.QLabel(
            "Click the hotkey field and press the combination to record it."
        )
        self._style_note_label(hotkey_hint)
        hotkey_form.addRow("Hotkey", self._field_with_hint(self.hotkey_edit, hotkey_hint))

        self.cancel_hotkey_edit = QtWidgets.QKeySequenceEdit()
        self.cancel_hotkey_edit.setMaximumSequenceLength(1)
        if hasattr(self.cancel_hotkey_edit, "setClearButtonEnabled"):
            self.cancel_hotkey_edit.setClearButtonEnabled(True)
        cancel_hotkey_hint = QtWidgets.QLabel(
            "Cancel hotkey stops current recording/transcription (must differ from main hotkey)."
        )
        self._style_note_label(cancel_hotkey_hint)
        hotkey_form.addRow(
            "Cancel Hotkey",
            self._field_with_hint(self.cancel_hotkey_edit, cancel_hotkey_hint),
        )
        layout.addWidget(hotkey_box)

        # --- Engine / Mode section ---
        engine_box = QtWidgets.QGroupBox("Engine && Mode")
        engine_form = QtWidgets.QFormLayout(engine_box)
        engine_form.setContentsMargins(10, 10, 10, 10)
        engine_form.setHorizontalSpacing(10)
        engine_form.setVerticalSpacing(6)
        engine_form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        self.engine_combo = _WheelPassthroughComboBox()
        engine_labels = {
            "local": "Local (faster-whisper)",
            "assemblyai": "Remote (AssemblyAI)",
            "groq": "Remote (Groq)",
            "openai": "Remote (OpenAI)",
            "deepgram": "Remote (Deepgram)",
            "elevenlabs": "Remote (ElevenLabs)",
        }
        for value in VALID_ENGINES:
            self.engine_combo.addItem(engine_labels.get(value, value), value)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        engine_hint = QtWidgets.QLabel(
            "Local keeps audio on your machine. Remote providers need internet and an API key."
        )
        engine_hint.setWordWrap(True)
        self._style_note_label(engine_hint)
        engine_form.addRow("Engine", self._field_with_hint(self.engine_combo, engine_hint))

        remote_model_widget = QtWidgets.QWidget()
        remote_model_layout = QtWidgets.QVBoxLayout(remote_model_widget)
        remote_model_layout.setContentsMargins(0, 0, 0, 0)
        remote_model_layout.setSpacing(3)
        self.remote_model_provider_label = QtWidgets.QLabel("Local engine selected")
        self._style_note_label(self.remote_model_provider_label, bold=True)
        self.remote_model_combo = _WheelPassthroughComboBox()
        self.remote_model_combo.currentIndexChanged.connect(
            self._on_remote_model_changed
        )
        self.remote_model_note_label = QtWidgets.QLabel("")
        self.remote_model_note_label.setWordWrap(True)
        self._style_note_label(self.remote_model_note_label)
        self.remote_model_note_label.setMinimumHeight(
            self.fontMetrics().height() + 8
        )
        remote_model_layout.addWidget(self.remote_model_provider_label)
        remote_model_layout.addWidget(self.remote_model_combo)
        remote_model_layout.addWidget(self.remote_model_note_label)
        engine_form.addRow("Remote Model", remote_model_widget)

        self.language_combo = _WheelPassthroughComboBox()
        for value in VALID_LANGUAGE_MODES:
            self.language_combo.addItem(
                LANGUAGE_MODE_LABELS.get(value, value), value
            )
        self.language_note_label = QtWidgets.QLabel("")
        self.language_note_label.setWordWrap(True)
        self._style_note_label(self.language_note_label)
        self.language_note_label.setVisible(True)
        engine_form.addRow(
            "Language",
            self._field_with_hint(self.language_combo, self.language_note_label),
        )

        self.mode_combo = _WheelPassthroughComboBox()
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
        mode_hint = QtWidgets.QLabel(
            "Batch inserts text after recording stops. Streaming writes while you speak, "
            "but is more sensitive to focus changes and provider differences."
        )
        mode_hint.setWordWrap(True)
        self._style_note_label(mode_hint)
        engine_form.addRow("Mode", self._field_with_hint(self.mode_combo, mode_hint))
        layout.addWidget(engine_box)

        # --- Text Insertion section ---
        paste_box = QtWidgets.QGroupBox("Text Insertion")
        paste_form = QtWidgets.QFormLayout(paste_box)
        paste_form.setContentsMargins(10, 10, 10, 10)
        paste_form.setHorizontalSpacing(10)
        paste_form.setVerticalSpacing(6)
        paste_form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        self.paste_mode_combo = _WheelPassthroughComboBox()
        paste_mode_labels = {
            "auto": "Auto (SendInput -> WM_PASTE)",
            "wm_paste": "WM_PASTE only",
            "send_input": "SendInput only",
        }
        for value in VALID_PASTE_MODES:
            self.paste_mode_combo.addItem(
                paste_mode_labels.get(value, value), value
            )
        self.paste_mode_combo.setToolTip(
            "Auto tries SendInput first and falls back to WM_PASTE. "
            "WM_PASTE only sends the window message directly. "
            "SendInput only simulates Ctrl+V."
        )
        paste_mode_hint = QtWidgets.QLabel(
            "Paste Mode controls how the paste command is delivered to the target app. "
            "It does not decide what stays in your clipboard afterwards."
        )
        paste_mode_hint.setWordWrap(True)
        self._style_note_label(paste_mode_hint)
        paste_form.addRow(
            "Paste Mode",
            self._field_with_hint(self.paste_mode_combo, paste_mode_hint),
        )

        self.keep_clipboard_checkbox = QtWidgets.QCheckBox(
            "Keep transcript in clipboard after insertion"
        )
        self.keep_clipboard_checkbox.setToolTip(
            "When enabled, the transcript remains in the clipboard after insertion. "
            "When disabled, the previous clipboard contents are restored."
        )
        keep_clipboard_hint = QtWidgets.QLabel(
            "Clipboard retention is separate from Paste Mode: this only controls whether "
            "the final transcript stays in the clipboard after insertion completes."
        )
        keep_clipboard_hint.setWordWrap(True)
        self._style_note_label(keep_clipboard_hint)
        paste_form.addRow(
            "",
            self._field_with_hint(self.keep_clipboard_checkbox, keep_clipboard_hint),
        )
        layout.addWidget(paste_box)

        # --- Audio / VAD section ---
        audio_box = QtWidgets.QGroupBox("Audio && Voice Detection")
        audio_form = QtWidgets.QFormLayout(audio_box)
        audio_form.setContentsMargins(10, 10, 10, 10)
        audio_form.setHorizontalSpacing(10)
        audio_form.setVerticalSpacing(6)
        audio_form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        self.vad_checkbox = QtWidgets.QCheckBox("Enable energy-based auto-stop")
        audio_form.addRow("", self.vad_checkbox)

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
        audio_form.addRow("VAD Threshold", self.vad_threshold_spin)

        self.start_beep_checkbox = QtWidgets.QCheckBox("Play start tone on recording")
        audio_form.addRow("", self.start_beep_checkbox)

        self.start_beep_tone_combo = _WheelPassthroughComboBox()
        tone_labels = {
            "soft": "Soft beep",
            "high": "High beep",
            "chime": "Two-tone chime",
            "system": "System notification",
        }
        for value in VALID_START_BEEP_TONES:
            self.start_beep_tone_combo.addItem(tone_labels.get(value, value), value)
        audio_form.addRow("Start Tone", self.start_beep_tone_combo)
        layout.addWidget(audio_box)

        # --- Recordings section ---
        recordings_box = QtWidgets.QGroupBox("Recordings")
        recordings_form = QtWidgets.QFormLayout(recordings_box)
        recordings_form.setContentsMargins(10, 10, 10, 10)
        recordings_form.setHorizontalSpacing(10)
        recordings_form.setVerticalSpacing(6)
        recordings_form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        self.save_wav_checkbox = QtWidgets.QCheckBox(
            "Keep last recording after successful transcription"
        )
        self.save_wav_path_label = QtWidgets.QLabel(
            "The current recording is always preserved until transcription "
            f"finishes. When enabled, the latest recording remains at: {debug_audio_path()}"
        )
        self.save_wav_path_label.setWordWrap(True)
        self._style_note_label(self.save_wav_path_label)
        recordings_form.addRow(
            "",
            self._field_with_hint(self.save_wav_checkbox, self.save_wav_path_label),
        )

        self.save_all_recordings_checkbox = QtWidgets.QCheckBox(
            "Archive every recording to folder"
        )
        recordings_form.addRow("", self.save_all_recordings_checkbox)

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
        recordings_form.addRow("Recordings Folder", recordings_dir_layout)

        self.recordings_max_spin = _WheelPassthroughSpinBox()
        self.recordings_max_spin.setRange(1, 500)
        self.recordings_max_spin.setValue(DEFAULT_RECORDINGS_MAX_COUNT)
        self.recordings_max_spin.setToolTip(
            "Keep only the newest N archived recordings."
        )
        recordings_hint = QtWidgets.QLabel(
            "Archiving stores the original WAV files so you can retry or inspect recordings later."
        )
        recordings_hint.setWordWrap(True)
        self._style_note_label(recordings_hint)
        recordings_form.addRow(
            "Keep Recordings",
            self._field_with_hint(self.recordings_max_spin, recordings_hint),
        )
        layout.addWidget(recordings_box)

        # --- Appearance section ---
        appearance_box = QtWidgets.QGroupBox("Appearance")
        appearance_form = QtWidgets.QFormLayout(appearance_box)
        appearance_form.setContentsMargins(10, 10, 10, 10)
        appearance_form.setHorizontalSpacing(10)
        appearance_form.setVerticalSpacing(6)
        appearance_form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        self.overlay_corner_combo = _WheelPassthroughComboBox()
        corner_labels = {
            "top-right": "Top Right",
            "top-left": "Top Left",
            "bottom-right": "Bottom Right",
            "bottom-left": "Bottom Left",
        }
        for value in VALID_OVERLAY_CORNERS:
            self.overlay_corner_combo.addItem(corner_labels.get(value, value), value)
        appearance_form.addRow("Overlay Corner", self.overlay_corner_combo)
        layout.addWidget(appearance_box)

        layout.addStretch(1)
        self.tabs.addTab(tab, "General")

    # --- Local tab ---

    def _build_local_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        form = QtWidgets.QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)

        self.model_combo = _WheelPassthroughComboBox()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        form.addRow("Model Size", self.model_combo)
        self.local_model_runtime_warning_label = QtWidgets.QLabel("")
        self.local_model_runtime_warning_label.setWordWrap(True)
        self.local_model_runtime_warning_label.setStyleSheet(
            "color: #b71c1c; font-size: 11px;"
        )
        form.addRow("", self.local_model_runtime_warning_label)

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

        # Unified local models section
        self.local_models_box = QtWidgets.QGroupBox("Local Models")
        self.local_models_box.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        local_models_layout = QtWidgets.QVBoxLayout(self.local_models_box)
        local_models_layout.setSpacing(4)
        self.local_models_label = QtWidgets.QLabel("Scanning...")
        self.local_models_label.setWordWrap(True)
        local_models_layout.addWidget(self.local_models_label)

        self.local_models_scan_status_label = QtWidgets.QLabel("")
        self.local_models_scan_status_label.setWordWrap(True)
        self._style_note_label(self.local_models_scan_status_label)
        local_models_layout.addWidget(self.local_models_scan_status_label)

        download_hint = QtWidgets.QLabel(
            "Select models to download or delete. Green entries are already cached locally. "
            "Cohere and Granite use the experimental ONNX/WebGPU runtime."
        )
        download_hint.setWordWrap(True)
        self._style_note_label(download_hint)
        local_models_layout.addWidget(download_hint)

        self.local_models_list = QtWidgets.QListWidget()
        self.local_models_list.setSelectionMode(
            QtWidgets.QAbstractItemView.MultiSelection
        )
        self._configure_compact_list_widget(
            self.local_models_list,
            expand=True,
            adjust_to_contents=True,
        )
        self.local_models_list.itemSelectionChanged.connect(
            self._update_local_model_actions
        )
        local_models_layout.addWidget(self.local_models_list, 1)

        manage_buttons = QtWidgets.QHBoxLayout()
        self.refresh_local_models_button = QtWidgets.QPushButton("Refresh")
        self.refresh_local_models_button.clicked.connect(
            self._refresh_local_model_views
        )
        self.download_selected_models_button = QtWidgets.QPushButton(
            "Download Selected"
        )
        self.download_selected_models_button.clicked.connect(
            self._download_selected_local_models
        )
        self.download_all_missing_models_button = QtWidgets.QPushButton(
            "Download All Missing"
        )
        self.download_all_missing_models_button.clicked.connect(
            self._download_all_missing_local_models
        )
        self.delete_selected_model_button = QtWidgets.QPushButton("Delete Selected")
        self.delete_selected_model_button.setEnabled(False)
        self.delete_selected_model_button.clicked.connect(
            self._delete_selected_cached_model
        )
        manage_buttons.addWidget(self.refresh_local_models_button)
        manage_buttons.addWidget(self.download_selected_models_button)
        manage_buttons.addWidget(self.download_all_missing_models_button)
        manage_buttons.addStretch(1)
        manage_buttons.addWidget(self.delete_selected_model_button)
        local_models_layout.addLayout(manage_buttons)

        self.local_models_action_label = QtWidgets.QLabel("")
        self.local_models_action_label.setWordWrap(True)
        local_models_layout.addWidget(self.local_models_action_label)
        self._show_local_model_unverified_state(
            "Open this tab to verify local model availability in the background."
        )

        layout.addWidget(self.local_models_box, 1)
        self._local_tab_index = self.tabs.addTab(tab, "Local")

    def _build_benchmark_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        intro = QtWidgets.QLabel(
            "Benchmark installed local faster-whisper models against one audio file. "
            "If you want to compare more models, download them first on the Local tab."
        )
        intro.setWordWrap(True)
        self._style_note_label(intro)
        layout.addWidget(intro)

        audio_box = QtWidgets.QGroupBox("Audio Sample")
        audio_layout = QtWidgets.QVBoxLayout(audio_box)
        audio_row = QtWidgets.QHBoxLayout()
        self.benchmark_audio_edit = QtWidgets.QLineEdit()
        self.benchmark_audio_edit.setPlaceholderText(
            "Choose an audio file or use the last recording"
        )
        self.benchmark_audio_edit.textChanged.connect(
            lambda _text: self._update_benchmark_actions()
        )
        self.benchmark_audio_browse_button = QtWidgets.QPushButton("Choose file...")
        self.benchmark_audio_browse_button.clicked.connect(
            self._choose_benchmark_audio_file
        )
        self.benchmark_audio_last_button = QtWidgets.QPushButton(
            "Use last recording"
        )
        self.benchmark_audio_last_button.clicked.connect(
            self._use_last_recording_for_benchmark
        )
        audio_row.addWidget(self.benchmark_audio_edit, 1)
        audio_row.addWidget(self.benchmark_audio_browse_button)
        audio_row.addWidget(self.benchmark_audio_last_button)
        audio_layout.addLayout(audio_row)

        self.benchmark_audio_status_label = QtWidgets.QLabel("No audio sample selected.")
        self.benchmark_audio_status_label.setWordWrap(True)
        self._style_note_label(self.benchmark_audio_status_label)
        audio_layout.addWidget(self.benchmark_audio_status_label)
        audio_help = QtWidgets.QLabel(
            "Use a representative sample. The benchmark measures model speed and runtime factor on this file."
        )
        audio_help.setWordWrap(True)
        self._style_note_label(audio_help)
        audio_layout.addWidget(audio_help)
        layout.addWidget(audio_box)

        models_box = QtWidgets.QGroupBox("Installed Models")
        models_box.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        models_layout = QtWidgets.QVBoxLayout(models_box)
        self.benchmark_models_list = QtWidgets.QListWidget()
        self.benchmark_models_list.setSelectionMode(
            QtWidgets.QAbstractItemView.MultiSelection
        )
        self._configure_compact_list_widget(
            self.benchmark_models_list,
            expand=True,
            adjust_to_contents=True,
        )
        self.benchmark_models_list.itemSelectionChanged.connect(
            self._update_benchmark_actions
        )
        models_layout.addWidget(self.benchmark_models_list, 1)
        self.refresh_benchmark_models_button = QtWidgets.QPushButton(
            "Refresh Installed Models"
        )
        self.refresh_benchmark_models_button.clicked.connect(
            self._refresh_local_model_views
        )
        models_layout.addWidget(self.refresh_benchmark_models_button)
        models_help = QtWidgets.QLabel(
            "Only locally available models can be benchmarked here. Download missing models on the Local tab first."
        )
        models_help.setWordWrap(True)
        self._style_note_label(models_help)
        models_layout.addWidget(models_help)
        layout.addWidget(models_box)

        options_box = QtWidgets.QGroupBox("Run Options")
        options_form = QtWidgets.QFormLayout(options_box)
        options_form.setContentsMargins(10, 10, 10, 10)
        options_form.setHorizontalSpacing(10)
        options_form.setVerticalSpacing(6)

        self.benchmark_compute_type_combo = _WheelPassthroughComboBox()
        for value in ("int8", "float16", "float32"):
            self.benchmark_compute_type_combo.addItem(value, value)
        self.benchmark_compute_type_combo.setToolTip(
            "int8 is usually fastest and smallest. float16 is useful on capable GPUs. float32 is the most compatible but slowest."
        )
        compute_type_note = QtWidgets.QLabel(
            "Controls precision: int8 is usually fastest, float32 is slowest but safest."
        )
        compute_type_note.setWordWrap(True)
        self._style_note_label(compute_type_note)
        options_form.addRow(
            "Compute Type",
            self._field_with_hint(self.benchmark_compute_type_combo, compute_type_note),
        )

        self.benchmark_runs_spin = _WheelPassthroughSpinBox()
        self.benchmark_runs_spin.setRange(1, 10)
        self.benchmark_runs_spin.setValue(1)
        self.benchmark_runs_spin.setToolTip(
            "Run the same benchmark multiple times. More runs reduce noise but take longer."
        )
        runs_note = QtWidgets.QLabel(
            "Repeat count for the same audio sample. Higher values give more stable averages."
        )
        runs_note.setWordWrap(True)
        self._style_note_label(runs_note)
        options_form.addRow(
            "Runs",
            self._field_with_hint(self.benchmark_runs_spin, runs_note),
        )

        self.benchmark_beam_size_spin = _WheelPassthroughSpinBox()
        self.benchmark_beam_size_spin.setRange(1, 10)
        self.benchmark_beam_size_spin.setValue(5)
        self.benchmark_beam_size_spin.setToolTip(
            "Beam size controls decoding breadth. Higher values can improve quality but slow the run down."
        )
        beam_note = QtWidgets.QLabel(
            "Decoder search width. Larger beams may improve recognition, but increase latency."
        )
        beam_note.setWordWrap(True)
        self._style_note_label(beam_note)
        options_form.addRow(
            "Beam Size",
            self._field_with_hint(self.benchmark_beam_size_spin, beam_note),
        )

        self.benchmark_language_combo = _WheelPassthroughComboBox()
        self.benchmark_language_combo.addItem("Auto", "auto")
        self.benchmark_language_combo.addItem("German", "de")
        self.benchmark_language_combo.addItem("English", "en")
        self.benchmark_language_combo.setToolTip(
            "Use Auto for unknown or mixed audio. A fixed language removes one source of model guesswork."
        )
        language_note = QtWidgets.QLabel(
            "Language hint for decoding. Auto detects language; fixed values can be more consistent on known input."
        )
        language_note.setWordWrap(True)
        self._style_note_label(language_note)
        options_form.addRow(
            "Language",
            self._field_with_hint(self.benchmark_language_combo, language_note),
        )

        self.benchmark_warmup_checkbox = QtWidgets.QCheckBox(
            "Run one warmup pass before measurements"
        )
        self.benchmark_warmup_checkbox.setToolTip(
            "Runs one unmeasured pass first so model loading and first-run caches affect the final numbers less."
        )
        warmup_note = QtWidgets.QLabel(
            "Useful when you want cleaner timings after the first cold run."
        )
        warmup_note.setWordWrap(True)
        self._style_note_label(warmup_note)
        options_form.addRow(
            "",
            self._field_with_hint(self.benchmark_warmup_checkbox, warmup_note),
        )

        self.benchmark_vad_checkbox = QtWidgets.QCheckBox(
            "Enable faster-whisper VAD filter"
        )
        self.benchmark_vad_checkbox.setToolTip(
            "Filters silence before transcription. This can improve speed on pause-heavy audio, but also changes the workload."
        )
        vad_note = QtWidgets.QLabel(
            "Silence filtering. Can speed up long recordings with pauses, but changes the benchmark scenario."
        )
        vad_note.setWordWrap(True)
        self._style_note_label(vad_note)
        options_form.addRow(
            "",
            self._field_with_hint(self.benchmark_vad_checkbox, vad_note),
        )
        layout.addWidget(options_box)

        benchmark_actions = QtWidgets.QHBoxLayout()
        self.run_benchmark_button = QtWidgets.QPushButton("Run Benchmark")
        self.run_benchmark_button.clicked.connect(self._run_local_benchmark)
        self.clear_benchmark_results_button = QtWidgets.QPushButton("Clear Results")
        self.clear_benchmark_results_button.clicked.connect(
            self._clear_benchmark_results
        )
        benchmark_actions.addWidget(self.run_benchmark_button)
        benchmark_actions.addWidget(self.clear_benchmark_results_button)
        benchmark_actions.addStretch(1)
        layout.addLayout(benchmark_actions)

        self.benchmark_status_label = QtWidgets.QLabel("")
        self.benchmark_status_label.setWordWrap(True)
        layout.addWidget(self.benchmark_status_label)

        results_box = QtWidgets.QGroupBox("Results")
        results_layout = QtWidgets.QVBoxLayout(results_box)
        self.benchmark_results_table = QtWidgets.QTableWidget(0, 6)
        self.benchmark_results_table.setHorizontalHeaderLabels(
            ["Model", "Compute", "Load", "Avg", "RTF", "Status"]
        )
        self.benchmark_results_table.verticalHeader().setVisible(False)
        self.benchmark_results_table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self.benchmark_results_table.setSelectionMode(
            QtWidgets.QAbstractItemView.NoSelection
        )
        self.benchmark_results_table.horizontalHeader().setStretchLastSection(True)
        results_layout.addWidget(self.benchmark_results_table)

        self.benchmark_summary_text = QtWidgets.QPlainTextEdit()
        self.benchmark_summary_text.setReadOnly(True)
        results_layout.addWidget(self.benchmark_summary_text)
        layout.addWidget(results_box, 1)

        self._benchmark_tab_index = self.tabs.addTab(tab, "Benchmark")

    # --- Remote tab ---

    def _build_remote_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # API keys
        provider_box = QtWidgets.QGroupBox("Remote Provider API Keys")
        provider_layout = QtWidgets.QFormLayout(provider_box)
        provider_layout.setContentsMargins(10, 10, 10, 10)
        provider_layout.setHorizontalSpacing(10)
        provider_layout.setVerticalSpacing(6)
        provider_layout.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        provider_layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.AllNonFixedFieldsGrow
        )
        provider_rows = (
            ("assemblyai", "AssemblyAI"),
            ("groq", "Groq"),
            ("openai", "OpenAI"),
            ("deepgram", "Deepgram"),
            ("elevenlabs", "ElevenLabs"),
        )
        provider_intro = QtWidgets.QLabel(
            "Enter a key only when you want to replace the stored one. The status badge shows whether the app already has a usable key."
        )
        provider_intro.setWordWrap(True)
        self._style_note_label(provider_intro)
        provider_layout.addRow("", provider_intro)
        for provider, title in provider_rows:
            key_field = QtWidgets.QLineEdit()
            key_field.setEchoMode(QtWidgets.QLineEdit.Password)
            key_field.setPlaceholderText(
                "Enter new key to update; use Clear saved to remove the stored key."
            )
            key_field.setMinimumWidth(180)
            key_field.textChanged.connect(
                lambda _text, p=provider: self._on_provider_key_changed(p)
            )
            clear_button = QtWidgets.QPushButton("Clear saved")
            clear_button.setToolTip("Delete the stored key for this provider on Save.")
            clear_button.setMinimumWidth(78)
            clear_button.setMaximumWidth(88)
            clear_button.clicked.connect(
                lambda _checked=False, p=provider: self._mark_provider_key_for_clear(p)
            )

            status_badge = QtWidgets.QLabel("Not configured")
            status_badge.setAlignment(
                QtCore.Qt.AlignCenter | QtCore.Qt.AlignVCenter
            )
            status_badge.setMinimumWidth(148)
            status_badge.setMaximumWidth(170)
            status_badge.setSizePolicy(
                QtWidgets.QSizePolicy.Fixed,
                QtWidgets.QSizePolicy.Fixed,
            )
            status_badge.setStyleSheet(
                "padding: 2px 8px; border: 1px solid #bbb; border-radius: 9px;"
                " color: #555; background: #f2f2f2;"
            )

            title_label = QtWidgets.QLabel(title)
            title_label.setMinimumWidth(54)

            field_row_widget = QtWidgets.QWidget()
            field_row = QtWidgets.QHBoxLayout(field_row_widget)
            field_row.setContentsMargins(0, 0, 0, 0)
            field_row.setSpacing(6)
            field_row.addWidget(key_field, 1)
            field_row.addWidget(clear_button, 0)
            field_row.addWidget(status_badge, 0)

            last_test_label = QtWidgets.QLabel("Last test: never.")
            last_test_label.setWordWrap(True)
            self._style_note_label(last_test_label)
            provider_layout.addRow(
                title_label,
                self._field_with_hint(field_row_widget, last_test_label),
            )

            self._provider_key_edits[provider] = key_field
            self._provider_status_labels[provider] = status_badge
            self._provider_last_test_labels[provider] = last_test_label

        self.assemblyai_key_edit = self._provider_key_edits["assemblyai"]
        self.groq_key_edit = self._provider_key_edits["groq"]
        self.openai_key_edit = self._provider_key_edits["openai"]
        self.deepgram_key_edit = self._provider_key_edits["deepgram"]
        self.elevenlabs_key_edit = self._provider_key_edits["elevenlabs"]
        provider_note = QtWidgets.QLabel(
            "Status badges show where each key is currently sourced from."
        )
        self._style_note_label(provider_note)
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
        self._style_note_label(self.key_storage_status_label)
        provider_layout.addRow(self.key_storage_status_label)

        self.test_conn_target_combo = _WheelPassthroughComboBox()
        self.test_conn_target_combo.addItem(
            "All configured providers (Recommended)",
            "all-configured",
        )
        self.test_conn_target_combo.addItem("AssemblyAI only", "assemblyai")
        self.test_conn_target_combo.addItem("Groq only", "groq")
        self.test_conn_target_combo.addItem("OpenAI only", "openai")
        self.test_conn_target_combo.addItem("Deepgram only", "deepgram")
        self.test_conn_target_combo.addItem("ElevenLabs only", "elevenlabs")
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
        self.history_max_spin.setValue(DEFAULT_HISTORY_MAX_ITEMS)
        self.history_max_spin.setToolTip(
            "Maximum transcript history items stored (0 = unlimited)."
        )
        self.history_max_spin.valueChanged.connect(
            lambda _value: self._refresh_history_list()
        )
        history_controls = QtWidgets.QHBoxLayout()
        history_controls.addWidget(QtWidgets.QLabel("History Size"))
        history_controls.addWidget(self.history_max_spin)
        history_controls.addStretch(1)
        layout.addLayout(history_controls)

        history_box = QtWidgets.QGroupBox("Transcript History")
        history_layout = QtWidgets.QVBoxLayout(history_box)
        history_layout.setContentsMargins(10, 10, 10, 10)
        history_layout.setSpacing(6)

        self.history_list = QtWidgets.QListWidget()
        history_font = QtGui.QFont(self.font())
        self.history_list.setFont(history_font)
        self._configure_compact_list_widget(self.history_list)
        self.history_list.itemSelectionChanged.connect(self._on_history_item_selected)
        history_layout.addWidget(self.history_list)

        self.history_detail = QtWidgets.QPlainTextEdit()
        self.history_detail.setReadOnly(True)
        self.history_detail.setFont(history_font)
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
        layout.addWidget(history_box)
        layout.addStretch(1)
        self.tabs.addTab(tab, "History")

    def _build_import_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        self._import_tab = tab
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        import_box = QtWidgets.QGroupBox("Import Audio File")
        import_layout = QtWidgets.QVBoxLayout(import_box)
        import_hint = QtWidgets.QLabel(
            "Transcribe an existing audio file and select the transcription service "
            "and model directly here (useful after failures or for external recordings)."
        )
        import_hint.setWordWrap(True)
        self._style_note_label(import_hint)
        import_layout.addWidget(import_hint)

        self.import_engine_combo = _WheelPassthroughComboBox()
        import_engine_labels = {
            "local": "Local (faster-whisper)",
            "assemblyai": "Remote (AssemblyAI)",
            "groq": "Remote (Groq)",
            "openai": "Remote (OpenAI)",
            "deepgram": "Remote (Deepgram)",
            "elevenlabs": "Remote (ElevenLabs)",
        }
        for value in VALID_ENGINES:
            self.import_engine_combo.addItem(
                import_engine_labels.get(value, value),
                value,
            )
        self.import_engine_note = QtWidgets.QLabel("")
        self.import_engine_note.setWordWrap(True)
        self._style_note_label(self.import_engine_note)
        self.import_engine_combo.currentIndexChanged.connect(
            self._on_import_engine_changed
        )
        import_layout.addWidget(QtWidgets.QLabel("Import Service"))
        import_layout.addWidget(self.import_engine_combo)
        import_layout.addWidget(self.import_engine_note)

        self.import_model_combo = _WheelPassthroughComboBox()
        self.import_model_note = QtWidgets.QLabel("")
        self.import_model_note.setWordWrap(True)
        self._style_note_label(self.import_model_note)
        self.import_model_combo.currentIndexChanged.connect(
            self._on_import_model_changed
        )
        import_layout.addWidget(QtWidgets.QLabel("Import Model"))
        import_layout.addWidget(self.import_model_combo)
        import_layout.addWidget(self.import_model_note)

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

        layout.addWidget(import_box)
        layout.addStretch(1)
        self.tabs.addTab(tab, "Import Audio")

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if self._initial_dialog_size_applied:
            return
        self._initial_dialog_size_applied = True
        self._restore_default_dialog_size()

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
        "cohere-transcribe-03-2026": (
            "Cohere Transcribe 03-2026 (~2.13 GB q4, ONNX/WebGPU)"
        ),
        "granite-4.0-1b-speech": (
            "IBM Granite 4.0 1B Speech (~1.84 GB q4, ONNX/WebGPU)"
        ),
    }

    def _model_label(self, model_name: str) -> str:
        return self._MODEL_LABELS.get(model_name, model_name)

    def _local_model_cache_key(self, model_dir: str | None = None) -> str:
        return str(model_dir or "").strip()

    def _prime_local_model_views_from_session_cache(self) -> bool:
        cache_key = self._local_model_cache_key(self.model_dir_edit.text())
        if cache_key not in _LOCAL_MODEL_SCAN_SESSION_CACHE:
            return False
        cached = list(_LOCAL_MODEL_SCAN_SESSION_CACHE.get(cache_key, []))
        self._cached_local_models = cached
        self._cached_local_models_dir = cache_key
        self._cached_local_models_available = True
        self._apply_local_model_scan_result(cached)
        return True

    def _prime_local_model_views_from_persistent_cache(self) -> bool:
        if self._local_model_inventory_store is None:
            return False
        cache_key = self._local_model_cache_key(self.model_dir_edit.text())
        cached = self._local_model_inventory_store.load_cached_models(cache_key)
        if cached is None:
            return False
        _LOCAL_MODEL_SCAN_SESSION_CACHE[cache_key] = list(cached)
        self._cached_local_models = list(cached)
        self._cached_local_models_dir = cache_key
        self._cached_local_models_available = True
        self._apply_local_model_scan_result(cached)
        return True

    def _prime_local_model_views_from_available_cache(self) -> bool:
        if self._prime_local_model_views_from_session_cache():
            return True
        return self._prime_local_model_views_from_persistent_cache()

    def _schedule_deferred_local_model_refresh(
        self,
        *,
        delay_ms: int = 0,
        force: bool = True,
    ) -> None:
        self._deferred_local_model_refresh_pending = True
        self._deferred_local_model_refresh_force = (
            self._deferred_local_model_refresh_force or force
        )
        self._deferred_local_model_refresh_timer.start(max(0, int(delay_ms)))

    def _run_deferred_local_model_refresh(self) -> None:
        if not self._deferred_local_model_refresh_pending:
            return
        self._deferred_local_model_refresh_pending = False
        force = self._deferred_local_model_refresh_force
        self._deferred_local_model_refresh_force = False
        model_dir = self._local_model_cache_key(self.model_dir_edit.text())
        if force and model_dir in self._local_model_auto_refresh_requested_dirs:
            return
        if force:
            self._local_model_auto_refresh_requested_dirs.add(model_dir)
        self._request_local_model_scan(force=force)

    def _refresh_model_combo(
        self,
        selected: str | None = None,
        cached: list[str] | None = None,
    ) -> None:
        """Rebuild model combo: downloaded models on top, separator, rest below."""
        cached_set = set(self._known_cached_models(cached))

        current_data = selected or str(self.model_combo.currentData() or "")

        self.model_combo.blockSignals(True)
        self.model_combo.clear()

        downloaded = [m for m in VALID_MODEL_SIZES if m in cached_set]
        not_downloaded = [m for m in VALID_MODEL_SIZES if m not in cached_set]

        for value in downloaded:
            label = self._model_label(value)
            self.model_combo.addItem(f"\u2713 {label}", value)

        if downloaded and not_downloaded:
            self.model_combo.insertSeparator(self.model_combo.count())

        for value in not_downloaded:
            label = self._model_label(value)
            self.model_combo.addItem(f"   {label}", value)

        if current_data:
            idx = self.model_combo.findData(current_data)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)

        self.model_combo.blockSignals(False)

    def _refresh_local_models_label(self, cached: list[str] | None = None) -> None:
        """Update the label for locally cached models with tag-style badges."""
        cached = self._known_cached_models(cached)

        if cached:
            tags = "".join(
                f'<span style="background-color: #f5f5f5; color: #333;'
                f" border: 1px solid #d0d0d0; border-radius: 10px;"
                f' padding: 2px 10px; margin-right: 4px;">{name}</span>&nbsp;'
                for name in cached
            )
            self.local_models_label.setTextFormat(QtCore.Qt.RichText)
            self.local_models_label.setText(
                f'<span style="color: #1b5e20;">Available locally:</span><br>{tags}'
            )
            self.local_models_label.setStyleSheet("")
        else:
            self.local_models_label.setTextFormat(QtCore.Qt.PlainText)
            self.local_models_label.setText(
                "No local models found. Download models below or let the app fetch one on first use.\n"
                f"See {DOC_MODELS_PATH} if downloads are blocked."
            )
            self.local_models_label.setStyleSheet("color: #b71c1c;")

    def _refresh_local_models_list(self, cached: list[str] | None = None) -> None:
        if not hasattr(self, "local_models_list"):
            return
        cached = self._known_cached_models(cached)

        selected = {
            str(item.data(QtCore.Qt.UserRole) or "")
            for item in self.local_models_list.selectedItems()
        }
        cached_set = set(cached)

        self.local_models_list.clear()
        for model_name in VALID_MODEL_SIZES:
            status = "Downloaded" if model_name in cached_set else "Not downloaded"
            if model_name in LOCAL_ENGLISH_ONLY_MODELS:
                status = f"{status}, English only"
            if model_name in LOCAL_WEBGPU_MODEL_SIZES:
                status = f"{status}, ONNX/WebGPU, batch only"
            item = QtWidgets.QListWidgetItem(
                f"{self._model_label(model_name)} - {status}"
            )
            item.setData(QtCore.Qt.UserRole, model_name)
            item.setData(QtCore.Qt.UserRole + 1, model_name in cached_set)
            if model_name in cached_set:
                item.setBackground(QtGui.QColor("#e8f5e9"))
                item.setForeground(QtGui.QColor("#1b5e20"))
            self.local_models_list.addItem(item)
            if model_name in selected:
                item.setSelected(True)

        visible_rows = min(max(self.local_models_list.count(), 1), 5)
        self.local_models_list.setMinimumHeight(
            self._minimum_list_height_for_rows(
                self.local_models_list,
                visible_rows,
            )
        )
        self._update_local_model_actions()

    def _refresh_benchmark_model_list(
        self,
        cached: list[str] | None = None,
    ) -> None:
        if not hasattr(self, "benchmark_models_list"):
            return
        cached = self._known_cached_models(cached)

        selected = {
            str(item.data(QtCore.Qt.UserRole) or "")
            for item in self.benchmark_models_list.selectedItems()
        }
        self.benchmark_models_list.clear()

        for model_name in cached:
            suffix = " (English only)" if model_name in LOCAL_ENGLISH_ONLY_MODELS else ""
            item = QtWidgets.QListWidgetItem(
                f"{self._model_label(model_name)}{suffix}"
            )
            item.setData(QtCore.Qt.UserRole, model_name)
            self.benchmark_models_list.addItem(item)
            if selected:
                item.setSelected(model_name in selected)
            else:
                item.setSelected(True)

        visible_rows = min(max(self.benchmark_models_list.count(), 1), 4)
        self.benchmark_models_list.setMinimumHeight(
            self._minimum_list_height_for_rows(
                self.benchmark_models_list,
                visible_rows,
            )
        )
        self._update_benchmark_actions()

    def _refresh_local_model_views(self, *, force: bool = True) -> None:
        if force:
            self._mark_local_model_refresh_stale()
        self._request_local_model_scan(force=force)

    def _known_cached_models(self, cached: list[str] | None = None) -> list[str]:
        if cached is not None:
            return list(cached)
        current_dir = self.model_dir_edit.text().strip() if hasattr(self, "model_dir_edit") else ""
        if self._cached_local_models_available and current_dir == self._cached_local_models_dir:
            return list(self._cached_local_models)
        return []

    def _set_local_model_scan_status(self, text: str, color: str = "#555") -> None:
        if not hasattr(self, "local_models_scan_status_label"):
            return
        self.local_models_scan_status_label.setText(text)
        self.local_models_scan_status_label.setStyleSheet(
            f"color: {color}; font-size: 11px; padding: 0 0 4px 0;"
        )

    def _show_local_model_unverified_state(self, status_text: str) -> None:
        if hasattr(self, "local_models_label"):
            self.local_models_label.setTextFormat(QtCore.Qt.PlainText)
            self.local_models_label.setText(
                "Local model inventory has not been verified yet.\n"
                "Models are shown as unavailable until the background check finishes."
            )
            self.local_models_label.setStyleSheet("color: #555;")
        if hasattr(self, "local_models_list"):
            self._refresh_local_models_list([])
            self.local_models_list.setEnabled(True)
        if hasattr(self, "benchmark_models_list"):
            self._refresh_benchmark_model_list([])
            self.benchmark_models_list.setEnabled(True)
        if hasattr(self, "model_combo"):
            self._refresh_model_combo(cached=[])
        if hasattr(self, "refresh_local_models_button"):
            self.refresh_local_models_button.setEnabled(
                self._active_local_model_download_thread is None
            )
        self._set_local_model_scan_status(status_text)
        self._update_language_availability()
        self._update_local_model_actions()
        self._update_benchmark_actions()

    def _set_local_model_scan_loading(self, *, preserve_current: bool = False) -> None:
        if hasattr(self, "local_models_label"):
            if preserve_current:
                self._set_local_model_scan_status(
                    "Showing the last known local models while the cache is verified in the background."
                )
            else:
                self._show_local_model_unverified_state(
                    "Checking local model availability in the background."
                )

    def _apply_local_model_scan_result(self, cached: list[str]) -> None:
        self._refresh_local_models_label(cached)
        self._refresh_local_models_list(cached)
        self._refresh_model_combo(cached=cached)
        self._refresh_benchmark_model_list(cached)
        self._set_local_model_scan_status("")
        self.local_models_list.setEnabled(True)
        self.benchmark_models_list.setEnabled(True)
        self.refresh_local_models_button.setEnabled(
            self._active_local_model_download_thread is None
        )
        self._update_language_availability()
        self._update_local_model_actions()
        self._update_benchmark_actions()

    def _inventory_tab_is_visible(self) -> bool:
        current_index = self.tabs.currentIndex() if hasattr(self, "tabs") else -1
        return current_index in {
            index
            for index in (self._local_tab_index, self._benchmark_tab_index)
            if index is not None
        }

    def _mark_local_model_refresh_stale(self, model_dir: str | None = None) -> None:
        cache_key = self._local_model_cache_key(
            self.model_dir_edit.text() if model_dir is None else model_dir
        )
        self._local_model_auto_refresh_requested_dirs.discard(cache_key)
        self._local_model_auto_refreshed_dirs.discard(cache_key)

    def _schedule_local_model_auto_refresh(
        self,
        *,
        delay_ms: int,
    ) -> None:
        if not self._inventory_tab_is_visible():
            return
        cache_key = self._local_model_cache_key(self.model_dir_edit.text())
        if (
            cache_key in self._local_model_auto_refreshed_dirs
            or cache_key in self._local_model_auto_refresh_requested_dirs
        ):
            return
        preserve_current = (
            self._cached_local_models_available
            and cache_key == self._cached_local_models_dir
        )
        self._set_local_model_scan_loading(preserve_current=preserve_current)
        self._schedule_deferred_local_model_refresh(delay_ms=delay_ms, force=True)

    def _request_local_model_scan(self, *, force: bool = False) -> None:
        model_dir = self.model_dir_edit.text().strip() if hasattr(self, "model_dir_edit") else ""
        if (
            not force
            and self._active_local_model_scan_thread is None
            and self._cached_local_models_available
            and model_dir == self._cached_local_models_dir
        ):
            self._apply_local_model_scan_result(self._cached_local_models)
            return

        preserve_current = (
            self._cached_local_models_available
            and model_dir == self._cached_local_models_dir
        )
        self._set_local_model_scan_loading(preserve_current=preserve_current)
        if self._active_local_model_scan_thread is not None:
            self._local_model_scan_pending = True
            return

        self._local_model_scan_token += 1
        token = self._local_model_scan_token

        def _run() -> None:
            try:
                cached = find_cached_models(model_dir)
            except Exception:
                cached = []
            self.local_model_scan_finished.emit(token, model_dir, list(cached))

        self._active_local_model_scan_thread = threading.Thread(
            target=_run,
            name="stt_app_local_model_scan",
            daemon=True,
        )
        self._active_local_model_scan_thread.start()

    @QtCore.Slot(int, str, object)
    def _on_local_model_scan_finished(
        self,
        token: int,
        model_dir: str,
        payload: object,
    ) -> None:
        if token != self._local_model_scan_token:
            return

        self._active_local_model_scan_thread = None
        self._local_model_auto_refresh_requested_dirs.discard(model_dir)
        self._local_model_auto_refreshed_dirs.add(model_dir)
        cached = [value for value in payload if isinstance(value, str)]
        _LOCAL_MODEL_SCAN_SESSION_CACHE[model_dir] = list(cached)
        self._cached_local_models = cached
        self._cached_local_models_dir = model_dir
        self._cached_local_models_available = True
        if self._local_model_inventory_store is not None:
            try:
                self._local_model_inventory_store.save_cached_models(model_dir, cached)
            except Exception:
                pass

        current_dir = self.model_dir_edit.text().strip() if hasattr(self, "model_dir_edit") else ""
        if current_dir == model_dir:
            self._apply_local_model_scan_result(cached)

        if self._local_model_scan_pending:
            self._local_model_scan_pending = False
            self._request_local_model_scan(force=True)

    def _selected_downloadable_model_names(self) -> list[str]:
        if not hasattr(self, "local_models_list"):
            return []
        return [
            str(item.data(QtCore.Qt.UserRole) or "").strip()
            for item in self.local_models_list.selectedItems()
            if str(item.data(QtCore.Qt.UserRole) or "").strip()
        ]

    def _update_local_model_actions(self) -> None:
        if not hasattr(self, "download_selected_models_button"):
            return

        busy = self._active_local_model_download_thread is not None
        selected = self._selected_downloadable_model_names()

        # Determine missing and downloaded from selection
        missing: list[str] = []
        selected_downloaded: list[str] = []
        if hasattr(self, "local_models_list"):
            for item in self.local_models_list.selectedItems():
                name = str(item.data(QtCore.Qt.UserRole) or "")
                if bool(item.data(QtCore.Qt.UserRole + 1)):
                    selected_downloaded.append(name)
                else:
                    missing.append(name)

        # Any missing models at all (for "Download All Missing")?
        any_missing = False
        if hasattr(self, "local_models_list"):
            for index in range(self.local_models_list.count()):
                item = self.local_models_list.item(index)
                if not bool(item.data(QtCore.Qt.UserRole + 1)):
                    any_missing = True
                    break

        self.local_models_list.setEnabled(not busy)
        self.refresh_local_models_button.setEnabled(not busy)
        self.delete_selected_model_button.setEnabled(
            (not busy) and bool(selected_downloaded)
        )
        self.download_selected_models_button.setEnabled(
            (not busy) and bool(selected)
        )
        self.download_all_missing_models_button.setEnabled(
            (not busy) and any_missing
        )

    def _download_selected_local_models(self) -> None:
        selected = self._selected_downloadable_model_names()
        if not selected:
            return
        missing = self._missing_downloadable_models(selected)
        if not missing:
            self.local_models_action_label.setStyleSheet("color: #555;")
            self.local_models_action_label.setText(
                "All selected models are already downloaded."
            )
            return
        self._start_local_model_download(missing)

    def _download_all_missing_local_models(self) -> None:
        missing = self._missing_downloadable_models()
        if not missing:
            self.local_models_action_label.setStyleSheet("color: #555;")
            self.local_models_action_label.setText(
                "All available local models are already downloaded."
            )
            return
        self._start_local_model_download(missing)

    def _missing_downloadable_models(
        self,
        names: list[str] | None = None,
    ) -> list[str]:
        wanted = set(names or [
            str(self.local_models_list.item(index).data(QtCore.Qt.UserRole) or "")
            for index in range(self.local_models_list.count())
        ])
        missing: list[str] = []
        for index in range(self.local_models_list.count()):
            item = self.local_models_list.item(index)
            model_name = str(item.data(QtCore.Qt.UserRole) or "")
            if model_name not in wanted:
                continue
            if not bool(item.data(QtCore.Qt.UserRole + 1)):
                missing.append(model_name)
        return missing

    def _start_local_model_download(self, model_names: list[str]) -> None:
        if not model_names or self._active_local_model_download_thread is not None:
            return

        self.local_models_action_label.setStyleSheet("color: #555;")
        self.local_models_action_label.setText(
            f"Preparing download for: {', '.join(model_names)}"
        )
        self._update_local_model_actions()

        model_dir = self.model_dir_edit.text().strip()

        def _run() -> None:
            successes: list[str] = []
            failures: list[str] = []
            total = len(model_names)
            for index, model_name in enumerate(model_names, start=1):
                self.local_model_download_progress.emit(
                    f"Downloading {index}/{total}: {model_name}..."
                )
                try:
                    download_model_snapshot(model_name, model_dir)
                    successes.append(model_name)
                except Exception as exc:
                    failures.append(f"{model_name}: {exc}")

            if failures and successes:
                message = (
                    f"Completed with errors. Downloaded: {', '.join(successes)}. "
                    f"Failed: {' | '.join(failures)}"
                )
                self.local_model_download_finished.emit(False, message)
                return
            if failures:
                self.local_model_download_finished.emit(
                    False,
                    f"Download failed: {' | '.join(failures)}",
                )
                return
            self.local_model_download_finished.emit(
                True,
                f"Downloaded: {', '.join(successes)}",
            )

        self._active_local_model_download_thread = threading.Thread(
            target=_run,
            name="stt_app_local_model_download",
            daemon=True,
        )
        self._active_local_model_download_thread.start()
        self._update_local_model_actions()

    def _on_local_model_download_progress(self, text: str) -> None:
        self.local_models_action_label.setStyleSheet("color: #555;")
        self.local_models_action_label.setText(text)

    def _on_local_model_download_finished(self, success: bool, text: str) -> None:
        self._active_local_model_download_thread = None
        if success:
            self.local_models_action_label.setStyleSheet("color: #1b5e20;")
        elif text.startswith("Completed with errors"):
            self.local_models_action_label.setStyleSheet("color: #b26a00;")
        else:
            self.local_models_action_label.setStyleSheet("color: #b71c1c;")
        self.local_models_action_label.setText(text)
        self._refresh_local_model_views(force=True)

    def _selected_benchmark_model_names(self) -> list[str]:
        if not hasattr(self, "benchmark_models_list"):
            return []
        return [
            str(item.data(QtCore.Qt.UserRole) or "").strip()
            for item in self.benchmark_models_list.selectedItems()
            if str(item.data(QtCore.Qt.UserRole) or "").strip()
        ]

    def _set_benchmark_audio_path(self, path: str) -> None:
        selected = str(path or "").strip()
        self.benchmark_audio_edit.setText(selected)
        if selected:
            self.benchmark_audio_status_label.setText(f"Selected: {selected}")
            self.benchmark_audio_status_label.setStyleSheet("color: #1b5e20;")
        else:
            self.benchmark_audio_status_label.setText("No audio sample selected.")
            self.benchmark_audio_status_label.setStyleSheet("color: #555;")
        self._update_benchmark_actions()

    def _choose_benchmark_audio_file(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select benchmark audio file",
            "",
            "Audio files (*.wav *.mp3 *.m4a *.flac *.ogg *.opus *.webm);;All files (*)",
        )
        if path:
            self._set_benchmark_audio_path(path)

    def _use_last_recording_for_benchmark(self) -> None:
        path = self._last_recording_store.selectable_path()
        if path is None:
            self._set_benchmark_status(
                "No last recording is currently available.",
                "#b71c1c",
            )
            return
        self._set_benchmark_audio_path(str(path))
        self._set_benchmark_status(
            "Last recording loaded for benchmarking.",
            "#555",
        )

    def _set_benchmark_status(self, text: str, color: str) -> None:
        self.benchmark_status_label.setText(text)
        self.benchmark_status_label.setStyleSheet(f"color: {color};")

    def _update_benchmark_actions(self) -> None:
        if not hasattr(self, "run_benchmark_button"):
            return

        busy = self._active_benchmark_thread is not None
        audio_path = self.benchmark_audio_edit.text().strip()
        has_audio = bool(audio_path) and Path(audio_path).is_file()
        has_models = bool(self._selected_benchmark_model_names())

        self.benchmark_audio_edit.setEnabled(not busy)
        self.benchmark_audio_browse_button.setEnabled(not busy)
        self.benchmark_audio_last_button.setEnabled(not busy)
        self.benchmark_models_list.setEnabled(not busy)
        self.refresh_benchmark_models_button.setEnabled(not busy)
        self.benchmark_compute_type_combo.setEnabled(not busy)
        self.benchmark_runs_spin.setEnabled(not busy)
        self.benchmark_beam_size_spin.setEnabled(not busy)
        self.benchmark_language_combo.setEnabled(not busy)
        self.benchmark_warmup_checkbox.setEnabled(not busy)
        self.benchmark_vad_checkbox.setEnabled(not busy)
        self.run_benchmark_button.setEnabled((not busy) and has_audio and has_models)
        self.clear_benchmark_results_button.setEnabled(not busy)

    def _clear_benchmark_results(self) -> None:
        self.benchmark_results_table.setRowCount(0)
        self.benchmark_summary_text.clear()
        self._set_benchmark_status("", "#555")
        self._update_benchmark_actions()
        self._restore_default_dialog_size()

    def _populate_benchmark_results(self, cases: list[BenchmarkCase]) -> None:
        self.benchmark_results_table.setRowCount(len(cases))
        for row, case in enumerate(cases):
            status = "OK" if case.error is None else "Error"
            values = [
                case.model,
                case.compute_type,
                _format_seconds(case.load_seconds),
                _format_seconds(case.avg_seconds),
                _format_number(case.avg_rtf),
                status,
            ]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if case.error and column == len(values) - 1:
                    item.setToolTip(case.error)
                self.benchmark_results_table.setItem(row, column, item)

    def _run_local_benchmark(self) -> None:
        if self._active_benchmark_thread is not None:
            return

        audio_path = self.benchmark_audio_edit.text().strip()
        if not audio_path or not Path(audio_path).is_file():
            self._set_benchmark_status(
                "Choose a valid audio file before starting the benchmark.",
                "#b71c1c",
            )
            return

        model_names = self._selected_benchmark_model_names()
        if not model_names:
            self._set_benchmark_status(
                "Select at least one installed model for the benchmark.",
                "#b71c1c",
            )
            return

        language_value = str(self.benchmark_language_combo.currentData() or "auto")
        if language_value == "de" and any(
            model_name in LOCAL_ENGLISH_ONLY_MODELS for model_name in model_names
        ):
            self._set_benchmark_status(
                "German cannot be benchmarked with the selected English-only model. "
                "Use Auto or English, or deselect distil-large-v3.5.",
                "#b71c1c",
            )
            return

        self._set_benchmark_status("Running benchmark...", "#555")
        compute_type = str(self.benchmark_compute_type_combo.currentData() or "int8")
        run_count = int(self.benchmark_runs_spin.value())
        beam_size = int(self.benchmark_beam_size_spin.value())
        use_vad = self.benchmark_vad_checkbox.isChecked()
        warmup = self.benchmark_warmup_checkbox.isChecked()
        model_dir = self.model_dir_edit.text().strip()
        self._update_benchmark_actions()

        def _progress(text: str) -> None:
            self.benchmark_progress.emit(text)

        def _run() -> None:
            try:
                cases = run_benchmark_cases(
                    audio_path=audio_path,
                    model_names=model_names,
                    device="auto",
                    compute_type=compute_type,
                    runs=run_count,
                    beam_size=beam_size,
                    language=None if language_value == "auto" else language_value,
                    vad_filter=use_vad,
                    warmup=warmup,
                    threads=0,
                    model_dir=model_dir,
                    progress_callback=_progress,
                )
            except Exception as exc:
                self.benchmark_finished.emit(False, str(exc), [])
                return

            self.benchmark_finished.emit(
                True,
                format_benchmark_summary(cases),
                cases,
            )

        self._active_benchmark_thread = threading.Thread(
            target=_run,
            name="stt_app_local_benchmark",
            daemon=True,
        )
        self._active_benchmark_thread.start()
        self._update_benchmark_actions()

    def _on_benchmark_progress(self, text: str) -> None:
        self._set_benchmark_status(text, "#555")

    def _on_benchmark_finished(
        self,
        success: bool,
        text: str,
        payload: object,
    ) -> None:
        self._active_benchmark_thread = None
        self._update_benchmark_actions()
        if not success:
            self._set_benchmark_status(text, "#b71c1c")
            return

        cases = [case for case in payload if isinstance(case, BenchmarkCase)]
        self._populate_benchmark_results(cases)
        self.benchmark_summary_text.setPlainText(text)
        if any(case.error for case in cases):
            self._set_benchmark_status(
                "Benchmark completed with errors. See the summary for details.",
                "#b26a00",
            )
        else:
            self._set_benchmark_status("Benchmark finished.", "#1b5e20")

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

        if engine == DEFAULT_ENGINE:
            self.import_model_note.setText(
                "This import uses the selected local model only for the imported file."
            )
            return
        self.import_model_note.setText(
            f"This import uses the selected {self._provider_label(engine)} model only for the imported file."
        )

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
        return settings

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
        elif provider == "elevenlabs":
            note = (
                "ElevenLabs currently uses the selected model for batch transcription "
                "and imports. Realtime Scribe is documented, but not yet wired into "
                "this app's streaming mode."
            )

        self.remote_model_note_label.setText(note)
        self.remote_model_combo.blockSignals(False)

    def _on_cached_model_selection_changed(self) -> None:
        self._update_local_model_actions()

    def _delete_selected_cached_model(self) -> None:
        selected_items = [
            item
            for item in self.local_models_list.selectedItems()
            if bool(item.data(QtCore.Qt.UserRole + 1))
        ]
        if not selected_items:
            self.delete_selected_model_button.setEnabled(False)
            return
        names = [
            str(item.data(QtCore.Qt.UserRole) or "").strip()
            for item in selected_items
        ]
        names = [n for n in names if n]
        if not names:
            self.delete_selected_model_button.setEnabled(False)
            return

        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete local model",
            (
                f"Delete local cache for: {', '.join(names)}?\n\n"
                "This removes downloaded files from disk."
            ),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        total_removed = 0
        errors: list[str] = []
        for model_name in names:
            try:
                removed = delete_cached_model(
                    model_name,
                    self.model_dir_edit.text().strip(),
                )
                total_removed += removed
            except Exception as exc:
                errors.append(f"'{model_name}': {exc}")

        if errors:
            self.local_models_action_label.setStyleSheet("color: #b71c1c;")
            self.local_models_action_label.setText(
                f"Failed to delete: {'; '.join(errors)}"
            )
        elif total_removed <= 0:
            self.local_models_action_label.setStyleSheet("color: #555;")
            self.local_models_action_label.setText(
                f"No cache directories found for: {', '.join(names)}."
            )
        else:
            self.local_models_action_label.setStyleSheet("color: #1b5e20;")
            self.local_models_action_label.setText(
                f"Deleted {', '.join(names)} ({total_removed} folder(s) removed)."
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

        if engine == "local" and model in LOCAL_EXPLICIT_LANGUAGE_MODELS:
            return ("de", "en")

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

        if engine == "local" and model in LOCAL_EXPLICIT_LANGUAGE_MODELS:
            return (
                "Cohere and Granite require an explicit language in this app; "
                "Auto is disabled for these experimental local models."
            )

        if engine == "groq":
            return (
                "Groq Whisper models are multilingual. 'Auto' lets the model detect "
                "language; selecting German/English sends a language hint."
            )

        if engine == "elevenlabs":
            return (
                "ElevenLabs Scribe models are multilingual. 'Auto' lets the provider "
                "detect language; selecting German/English sends a language hint."
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

    def _update_local_model_runtime_warning(self) -> None:
        if not hasattr(self, "local_model_runtime_warning_label"):
            return
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        model_name = (
            str(self.model_combo.currentData() or "")
            if hasattr(self, "model_combo")
            else ""
        )
        if engine == "local" and model_name in LOCAL_WEBGPU_MODEL_SIZES:
            self.local_model_runtime_warning_label.setText(
                "Experimental ONNX model: the app tries WebGPU, then DirectML "
                "on Windows, and falls back to CPU if no compatible "
                "Intel/AMD/NVIDIA GPU runtime loads. CPU fallback can be much "
                "slower than large-v3-turbo. Batch mode only. Requires Node.js "
                "and `npm install` for the local runtime."
            )
            self.local_model_runtime_warning_label.setVisible(True)
            return
        self.local_model_runtime_warning_label.setText(" ")
        self.local_model_runtime_warning_label.setVisible(False)

    # ------------------------------------------------------------------
    # Engine indicator
    # ------------------------------------------------------------------

    def _update_engine_indicator(self) -> None:
        """Update the always-visible engine indicator bar."""
        engine = str(self.engine_combo.currentData() or DEFAULT_ENGINE)
        if engine == "local":
            model = (
                str(self.model_combo.currentData() or "")
                if hasattr(self, "model_combo")
                else ""
            )
            runtime = "ONNX/WebGPU" if model in LOCAL_WEBGPU_MODEL_SIZES else "faster-whisper"
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
        supports_streaming = (
            engine in STREAMING_ENGINES and model_name not in LOCAL_BATCH_ONLY_MODELS
        )
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
                if engine == "local" and model_name in LOCAL_BATCH_ONLY_MODELS:
                    item.setToolTip(
                        "Streaming is not supported by the experimental "
                        "ONNX/WebGPU local models. Use batch mode."
                    )
                else:
                    item.setToolTip(
                        f"Streaming is not supported by the {engine} provider. "
                        "Use faster-whisper local models, AssemblyAI, or Deepgram "
                        "for streaming."
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

    def _on_import_engine_changed(self, _index: int = 0) -> None:
        self._update_import_model_selector()
        self._update_import_engine_note()

    def _on_settings_tab_changed(self, _index: int) -> None:
        self._schedule_local_model_auto_refresh(delay_ms=0)

    def _on_import_model_changed(self, _index: int = 0) -> None:
        if not hasattr(self, "import_model_combo"):
            return
        engine = str(self.import_engine_combo.currentData() or DEFAULT_ENGINE)
        value = str(self.import_model_combo.currentData() or "")
        if not value:
            value = self._import_model_value_for_engine(engine)
        self._import_model_values[engine] = value
        self._update_import_engine_note()

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
            "elevenlabs": "ElevenLabs",
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
        selected_model = (
            str(self.import_model_combo.currentData() or "")
            if hasattr(self, "import_model_combo")
            else ""
        )
        if engine == DEFAULT_ENGINE:
            self.import_engine_note.setStyleSheet("color: #555;")
            self.import_engine_note.setText(
                "Local import transcription stays independent from the main Local tab selection."
            )
            return
        if self._import_engine_has_api_key(engine):
            self.import_engine_note.setStyleSheet("color: #555;")
            model_text = (
                f" using model '{selected_model}'."
                if selected_model
                else "."
            )
            self.import_engine_note.setText(
                f"Import transcription will use {self._provider_label(engine)}{model_text}"
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
        remote_providers = ("assemblyai", "groq", "openai", "deepgram", "elevenlabs")
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

        if engine == "elevenlabs":
            api_key = self._resolve_api_key("elevenlabs", self.elevenlabs_key_edit)
            if not api_key:
                return (
                    None,
                    "No API key entered. Enter a key above first.",
                )

            from .transcriber.elevenlabs_provider import ElevenLabsTranscriber

            transcriber = ElevenLabsTranscriber(
                api_key=api_key,
                language_mode=str(self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE),
                model=self._remote_model_value_for_provider("elevenlabs"),
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
            for provider in ("assemblyai", "groq", "openai", "deepgram", "elevenlabs"):
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
        blocker = QtCore.QSignalBlocker(self.model_dir_edit)
        self.model_dir_edit.setText(settings.model_dir or "")
        del blocker
        self._refresh_model_combo(selected=settings.model_size, cached=[])
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
        self._update_local_model_runtime_warning()
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
                "elevenlabs": getattr(
                    settings,
                    "elevenlabs_model",
                    DEFAULT_ELEVENLABS_MODEL,
                ),
            }
        )
        self._import_model_values.update(
            {
                "local": settings.model_size,
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
                "elevenlabs": getattr(
                    settings,
                    "elevenlabs_model",
                    DEFAULT_ELEVENLABS_MODEL,
                ),
            }
        )
        self._update_remote_model_selector()
        self._select_combo_data(self.test_conn_target_combo, "all-configured")
        if hasattr(self, "import_engine_combo"):
            self._select_combo_data(self.import_engine_combo, settings.engine)
            self._update_import_model_selector()
            self._update_import_engine_note()

        if not self._prime_local_model_views_from_available_cache():
            self._show_local_model_unverified_state(
                "Open Local or Benchmark to verify local model availability in the background."
            )
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
        import_index = self.tabs.indexOf(self._import_tab)
        if import_index >= 0:
            self.tabs.setCurrentIndex(import_index)
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
        self.import_model_combo.setEnabled(False)

        # Build settings on the GUI thread — widgets must not be accessed
        # from background threads.
        import_engine = str(
            self.import_engine_combo.currentData() or DEFAULT_ENGINE
        )
        import_model = str(self.import_model_combo.currentData() or "")
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
            self.import_model_combo.setEnabled(True)
            return
        settings = self._build_current_settings(
            engine_override=import_engine,
            model_override=import_model,
        )

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
        try:
            text = transcriber.transcribe_batch(path)
        finally:
            if hasattr(transcriber, "close"):
                transcriber.close()
        return True, str(text or "").strip()

    def _finish_import_transcription(self, ok: bool, text: str) -> None:
        self.import_file_button.setEnabled(True)
        self.import_last_recording_button.setEnabled(True)
        self.import_start_button.setEnabled(bool(self._selected_import_file_path))
        self.import_engine_combo.setEnabled(True)
        self.import_model_combo.setEnabled(True)
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
        model_override: str | None = None,
    ) -> AppSettings:
        """Construct an ``AppSettings`` from current widget state.

        Must be called on the GUI thread.
        """
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
            has_elevenlabs_key=getattr(self._loaded_settings, "has_elevenlabs_key", False),
            groq_model=self._remote_model_value_for_provider("groq"),
            openai_model=self._remote_model_value_for_provider("openai"),
            deepgram_model=self._remote_model_value_for_provider("deepgram"),
            assemblyai_model=self._remote_model_value_for_provider("assemblyai"),
            elevenlabs_model=self._remote_model_value_for_provider("elevenlabs"),
        )
        effective_engine = str(
            engine_override or self.engine_combo.currentData() or DEFAULT_ENGINE
        )
        return self._apply_engine_model_selection(
            settings,
            effective_engine,
            str(model_override or ""),
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
        has_elevenlabs_key = bool(
            self._resolve_api_key("elevenlabs", self.elevenlabs_key_edit)
        )

        self._apply_secret_store_options()
        key_storage_errors: list[str] = []
        pending_clear = set(self._provider_pending_clear)

        openai_value = self.openai_key_edit.text().strip()
        deepgram_value = self.deepgram_key_edit.text().strip()
        assemblyai_value = self.assemblyai_key_edit.text().strip()
        groq_value = self.groq_key_edit.text().strip()
        elevenlabs_value = self.elevenlabs_key_edit.text().strip()

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
        if elevenlabs_value:
            try:
                self._secret_store.set_api_key("elevenlabs", elevenlabs_value)
                self.elevenlabs_key_edit.clear()
                has_elevenlabs_key = bool(
                    self._resolve_api_key("elevenlabs", self.elevenlabs_key_edit)
                )
            except Exception as exc:
                key_storage_errors.append(f"ElevenLabs: {exc}")
        elif "elevenlabs" in pending_clear:
            try:
                self._secret_store.delete_api_key("elevenlabs")
                has_elevenlabs_key = False
            except Exception as exc:
                key_storage_errors.append(f"ElevenLabs delete: {exc}")

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
            if any(
                (
                    openai_value,
                    deepgram_value,
                    assemblyai_value,
                    groq_value,
                    elevenlabs_value,
                )
            ) or pending_clear:
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
            has_elevenlabs_key=has_elevenlabs_key,
            groq_model=self._remote_model_value_for_provider("groq"),
            openai_model=self._remote_model_value_for_provider("openai"),
            deepgram_model=self._remote_model_value_for_provider("deepgram"),
            assemblyai_model=self._remote_model_value_for_provider("assemblyai"),
            elevenlabs_model=self._remote_model_value_for_provider("elevenlabs"),
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
