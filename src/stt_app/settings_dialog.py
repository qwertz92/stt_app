from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, ClassVar

from PySide6 import QtCore, QtGui, QtWidgets

from .app_paths import debug_audio_path, recordings_dir
from .benchmark_environment import BenchmarkEnvironment, collect_benchmark_environment
from .benchmark_history import (
    BenchmarkHistoryEntry,
    BenchmarkHistoryStore,
    BenchmarkOptions,
    export_benchmark_entry,
)
from .config import (
    APP_LOGGER_NAME,
    ASSEMBLYAI_MODELS,
    AZURE_SPEECH_MODELS,
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_AZURE_ENDPOINT,
    DEFAULT_AZURE_SPEECH_MODEL,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_ENGINE,
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_FUNASR_MODEL,
    DEFAULT_GROQ_MODEL,
    DEFAULT_HISTORY_MAX_ITEMS,
    DEFAULT_HOTKEY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_MODEL_SIZE,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OVERLAY_CORNER,
    DEFAULT_CONCURRENT_TRANSCRIPTION_MODE,
    VALID_CONCURRENT_TRANSCRIPTION_MODES,
    DEFAULT_PASTE_MODE,
    DEFAULT_RECORDINGS_MAX_COUNT,
    DEFAULT_START_BEEP_TONE,
    DEFAULT_VAD_ENERGY_THRESHOLD,
    DOC_MODELS_PATH,
    DEEPGRAM_MODELS,
    ELEVENLABS_MODELS,
    FUNASR_MODELS,
    GROQ_MODELS,
    HISTORY_MAX_ITEMS_MAX,
    LANGUAGE_MODE_LABELS,
    LOCAL_BATCH_ONLY_MODELS,
    LOCAL_ENGLISH_ONLY_MODELS,
    LOCAL_EXPLICIT_LANGUAGE_MODELS,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_ONNX_MODEL_PRECISION,
    LOCAL_ONNX_MODEL_RUNTIME_LABELS,
    LOCAL_ONNX_MODEL_SIZES,
    LOCAL_WEBGPU_BENCHMARK_DEVICE_GROUPS,
    LOCAL_WEBGPU_MODEL_SIZES,
    OPENAI_MODELS,
    VAD_ENERGY_THRESHOLD_MAX,
    VAD_ENERGY_THRESHOLD_MIN,
    VALID_ENGINES,
    VALID_LANGUAGE_MODES,
    VALID_MODES,
    VALID_MODEL_SIZES,
    VALID_OVERLAY_CORNERS,
    VALID_PASTE_MODES,
    VALID_START_BEEP_TONES,
    language_modes_for_selection,
    supports_streaming,
)
from .hotkey import parse_hotkey
from .last_recording_store import LastRecordingStore
from .local_model_download import (
    model_download_process_error,
    start_model_download_process,
    terminate_model_download_process,
)
from .local_model_inventory_store import LocalModelInventoryStore
from .local_model_scan import scan_cached_models_out_of_process as _scan_cached_models
from .local_benchmark import (
    BenchmarkCase,
    BenchmarkCancelled,
    _format_number,
    _format_seconds,
    format_benchmark_summary,
    normalize_webgpu_benchmark_devices,
    run_benchmark_cases,
)
from .logger import AppLogger
from .model_download_progress import (
    ModelDownloadSpeedTracker,
    format_model_download_progress,
)
from .secret_store import SecretStore
from .settings_store import AppSettings, SettingsStore
from .transcript_edit_dialog import TranscriptEditDialog
from .transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore
from .ui_feedback import (
    BUTTON_FEEDBACK_STYLESHEET,
    reserve_button_width_for_texts,
    restore_vertical_scrollbar,
    set_button_feedback_state,
)
from .transcriber.local_faster_whisper import (
    cleanup_incomplete_model_download,
    delete_cached_model,
    estimate_cached_model_bytes,
)

if TYPE_CHECKING:
    from .controller import DictationController


def _emit_background_signal(
    owner: QtCore.QObject,
    signal_name: str,
    *args: object,
) -> bool:
    try:
        getattr(owner, signal_name).emit(*args)
    except RuntimeError:
        return False
    return True


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
    "universal-3-pro": "universal-3-pro (highest accuracy, falls back to universal-2)",
    "universal-2": "universal-2 (fast, broad language coverage)",
    "scribe_v2": "scribe_v2 (current default, highest published accuracy)",
    "scribe_v1": "scribe_v1 (legacy batch model)",
    "mai-transcribe-1.5": "mai-transcribe-1.5 (current default, 42 languages)",
    "mai-transcribe-1": "mai-transcribe-1 (first generation, fewer languages)",
    "fun-asr-realtime": "fun-asr-realtime (31 languages; no German)",
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
    "azure": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value))
        for value in AZURE_SPEECH_MODELS
    ),
    "funasr": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value))
        for value in FUNASR_MODELS
    ),
}

_DEFAULT_SETTINGS_DIALOG_SIZE = QtCore.QSize(780, 960)
_DIALOG_SCREEN_MARGIN = 48
_COMPACT_LIST_ITEM_STYLESHEET = "QListWidget::item { padding: 0px 4px; }"
_COMPACT_LIST_ROW_EXTRA_PX = 4
_COMPACT_TABLE_ROW_EXTRA_PX = 4
_LOCAL_MODEL_AUTO_REFRESH_DELAY_MS = 150
_PROVIDER_STATUS_BADGE_TEXTS = (
    "Not configured",
    "Unsaved input",
    "Will clear on Save",
    "Stored securely",
    "Secure (legacy)",
    "Stored insecurely",
    "Insecure disabled",
)
_PROVIDER_STATUS_BADGE_HORIZONTAL_PADDING_PX = 16
_REMOTE_PROVIDER_LABEL_EXTRA_PX = 18
_REMOTE_PROVIDER_GRID_SPACING_PX = 12
_GENERAL_FORM_LABEL_EXTRA_PX = 12
_ACTION_ROW_SPACING_PX = 8
_INLINE_FIELD_BUTTON_SPACING_PX = 6

_REMOTE_MODEL_DEFAULTS: dict[str, str] = {
    "groq": DEFAULT_GROQ_MODEL,
    "openai": DEFAULT_OPENAI_MODEL,
    "deepgram": DEFAULT_DEEPGRAM_MODEL,
    "assemblyai": DEFAULT_ASSEMBLYAI_MODEL,
    "elevenlabs": DEFAULT_ELEVENLABS_MODEL,
    "azure": DEFAULT_AZURE_SPEECH_MODEL,
    "funasr": DEFAULT_FUNASR_MODEL,
}

_REMOTE_API_KEY_PROVIDERS = (
    "openai",
    "deepgram",
    "assemblyai",
    "groq",
    "elevenlabs",
    "azure",
    "funasr",
)

_LOCAL_MODEL_SCAN_SESSION_CACHE: dict[str, list[str]] = {}
_LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS: set[str] = set()


def _set_transcriber_progress_callback(
    transcriber: object,
    callback: Callable[[str], None],
) -> None:
    setter = getattr(transcriber, "set_progress_callback", None)
    if callable(setter):
        setter(callback)


class SettingsDialog(QtWidgets.QDialog):
    connection_test_finished = QtCore.Signal(int, bool, str)
    import_transcription_finished = QtCore.Signal(bool, str)
    import_transcription_progress = QtCore.Signal(str)
    local_model_scan_finished = QtCore.Signal(int, str, object)
    local_model_download_progress = QtCore.Signal(int, str)
    local_model_download_finished = QtCore.Signal(int, bool, str)
    benchmark_progress = QtCore.Signal(str)
    benchmark_case_finished = QtCore.Signal(object)
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
        self._benchmark_history_store = BenchmarkHistoryStore()
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
        self._local_model_inventory_loaded_from_cache_dirs: set[str] = set()
        self._local_tab_index: int | None = None
        self._benchmark_tab_index: int | None = None
        self._settings_perf_logger = logging.getLogger(APP_LOGGER_NAME)
        self._settings_perf_started_at = time.perf_counter()
        self._settings_perf_logged_first_show = False
        self._settings_perf_prewarmed_tab_indexes: set[int] = set()
        self._settings_perf_painted_tabs: set[int] = set()
        self._local_model_scan_started_at_by_token: dict[int, float] = {}
        self._active_local_model_download_thread: threading.Thread | None = None
        self._local_model_download_lock = threading.Lock()
        self._local_model_download_queue: list[tuple[str, str]] = []
        self._local_model_download_active: tuple[str, str] | None = None
        self._local_model_download_completed_names: set[str] = set()
        self._local_model_download_worker_running = False
        self._local_model_download_worker_token = 0
        self._local_model_download_cancel_event = threading.Event()
        self._local_model_download_process = None
        self._local_model_download_speed_tracker = ModelDownloadSpeedTracker()
        self._local_model_download_progress_timer = QtCore.QTimer(self)
        self._local_model_download_progress_timer.setInterval(500)
        self._local_model_download_progress_timer.timeout.connect(
            self._refresh_local_model_download_progress
        )
        self._active_benchmark_thread: threading.Thread | None = None
        self._benchmark_cancel_event: threading.Event | None = None
        self._current_benchmark_cases: list[BenchmarkCase] = []
        self._current_benchmark_entry: BenchmarkHistoryEntry | None = None
        self._current_benchmark_options: BenchmarkOptions | None = None
        self._current_benchmark_environment: BenchmarkEnvironment | None = None
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
            "azure": getattr(
                self._loaded_settings,
                "azure_speech_model",
                DEFAULT_AZURE_SPEECH_MODEL,
            ),
            "funasr": getattr(
                self._loaded_settings,
                "funasr_model",
                DEFAULT_FUNASR_MODEL,
            ),
        }
        self._import_model_values: dict[str, str] = {
            "local": self._loaded_settings.model_size,
            "groq": self._remote_model_values["groq"],
            "openai": self._remote_model_values["openai"],
            "deepgram": self._remote_model_values["deepgram"],
            "assemblyai": self._remote_model_values["assemblyai"],
            "elevenlabs": self._remote_model_values["elevenlabs"],
            "azure": self._remote_model_values["azure"],
            "funasr": self._remote_model_values["funasr"],
        }
        self._active_connection_test_thread: threading.Thread | None = None
        self._import_progress_message = ""
        self._import_progress_started_at: datetime | None = None
        self._import_progress_timer = QtCore.QTimer(self)
        self._import_progress_timer.setInterval(1000)
        self._import_progress_timer.timeout.connect(
            self._refresh_import_progress_label
        )
        self._history_copy_feedback_timer = QtCore.QTimer(self)
        self._history_copy_feedback_timer.setSingleShot(True)
        self._history_copy_feedback_timer.setInterval(900)
        self._history_copy_feedback_timer.timeout.connect(
            self._reset_history_copy_feedback
        )
        self._import_copy_feedback_timer = QtCore.QTimer(self)
        self._import_copy_feedback_timer.setSingleShot(True)
        self._import_copy_feedback_timer.setInterval(900)
        self._import_copy_feedback_timer.timeout.connect(
            self._reset_import_copy_feedback
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
        self.import_transcription_progress.connect(
            self._on_import_transcription_progress
        )
        self.import_transcription_finished.connect(self._finish_import_transcription)
        self.local_model_scan_finished.connect(self._on_local_model_scan_finished)
        self.local_model_download_progress.connect(
            self._on_local_model_download_progress
        )
        self.local_model_download_finished.connect(
            self._on_local_model_download_finished
        )
        self.benchmark_progress.connect(self._on_benchmark_progress)
        self.benchmark_case_finished.connect(self._on_benchmark_case_finished)
        self.benchmark_finished.connect(self._on_benchmark_finished)
        phase_started_at = time.perf_counter()
        self._build_ui()
        self._log_settings_timing("build_ui", phase_started_at)
        self.tabs.currentChanged.connect(self._on_settings_tab_changed)
        phase_started_at = time.perf_counter()
        self._populate(self._loaded_settings)
        self._log_settings_timing("populate", phase_started_at)
        phase_started_at = time.perf_counter()
        self._apply_initial_dialog_size()
        self._log_settings_timing("initial_size", phase_started_at)
        self._log_settings_timing("dialog_init", self._settings_perf_started_at)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(self._dialog_scrollbar_stylesheet())
        self._disable_combo_popup_effects()
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
                border-bottom: 2px solid #bbb;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                background: #e8e8e8;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                border-bottom-color: #1a73e8;
                color: #0d47a1;
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
        self._configure_button_row(buttons)
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
        self._reserve_feedback_button_widths()

    def _reserve_feedback_button_widths(self) -> None:
        for button, texts in (
            (getattr(self, "history_copy_button", None), ("Copy selected", "Copied")),
            (getattr(self, "import_copy_button", None), ("Copy result", "Copied")),
        ):
            if isinstance(button, QtWidgets.QPushButton):
                reserve_button_width_for_texts(button, texts)

    def _restore_default_dialog_size(self) -> None:
        target_size = self._refresh_default_dialog_size()
        self.resize(target_size)

    def _apply_initial_dialog_size(self) -> None:
        if self._initial_dialog_size_applied:
            return
        self._initial_dialog_size_applied = True
        self._restore_default_dialog_size()

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

    def _provider_status_badge_width(self) -> int:
        metrics = self.fontMetrics()
        text_width = max(
            metrics.horizontalAdvance(text)
            for text in _PROVIDER_STATUS_BADGE_TEXTS
        )
        return text_width + _PROVIDER_STATUS_BADGE_HORIZONTAL_PADDING_PX

    def _remote_provider_label_width(
        self,
        provider_rows: tuple[tuple[str, str], ...],
    ) -> int:
        candidates = [title for _provider, title in provider_rows]
        candidates.extend(
            (
                "Azure Endpoint",
                "Connection Target",
            )
        )
        text_width = max(self.fontMetrics().horizontalAdvance(text) for text in candidates)
        return text_width + _REMOTE_PROVIDER_LABEL_EXTRA_PX

    def _apply_shared_form_label_width(
        self,
        forms: tuple[QtWidgets.QFormLayout, ...],
    ) -> None:
        label_widgets: list[QtWidgets.QLabel] = []
        for form in forms:
            for row in range(form.rowCount()):
                item = form.itemAt(row, QtWidgets.QFormLayout.LabelRole)
                widget = item.widget() if item is not None else None
                if isinstance(widget, QtWidgets.QLabel):
                    label_widgets.append(widget)
        measured_labels = [label for label in label_widgets if label.text().strip()]
        if not measured_labels:
            return
        width = (
            max(label.sizeHint().width() for label in measured_labels)
            + _GENERAL_FORM_LABEL_EXTRA_PX
        )
        for label in label_widgets:
            label.setMinimumWidth(width)
            label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

    def _style_provider_last_test_label(
        self,
        label: QtWidgets.QLabel,
        *,
        color: str = "#555",
    ) -> None:
        label.setStyleSheet(f"color: {color}; font-size: 11px; padding: 0 0 6px 0;")

    def _style_note_label(self, label: QtWidgets.QLabel, *, bold: bool = False) -> None:
        style = "color: #555; font-size: 11px; padding: 0 0 6px 0;"
        if bold:
            style += " font-weight: bold;"
        label.setStyleSheet(style)

    @staticmethod
    def _configure_button_row(
        layout: QtWidgets.QHBoxLayout,
        *,
        spacing: int = _ACTION_ROW_SPACING_PX,
    ) -> None:
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(spacing)

    @staticmethod
    def _match_field_button_height(
        field: QtWidgets.QWidget,
        *buttons: QtWidgets.QAbstractButton,
    ) -> None:
        height = max(
            1,
            field.sizeHint().height(),
            *(button.sizeHint().height() for button in buttons),
        )
        field.setFixedHeight(height)
        for button in buttons:
            button.setFixedHeight(height)

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
    def _compact_list_item_size(widget: QtWidgets.QListWidget) -> QtCore.QSize:
        height = widget.fontMetrics().height() + _COMPACT_LIST_ROW_EXTRA_PX
        return QtCore.QSize(0, max(height, 18))

    @classmethod
    def _apply_compact_list_item_size(
        cls,
        widget: QtWidgets.QListWidget,
        item: QtWidgets.QListWidgetItem,
    ) -> None:
        item.setSizeHint(cls._compact_list_item_size(widget))

    @staticmethod
    def _compact_table_row_height(widget: QtWidgets.QTableWidget) -> int:
        return max(widget.fontMetrics().height() + _COMPACT_TABLE_ROW_EXTRA_PX, 18)

    @staticmethod
    def _minimum_list_height_for_rows(
        widget: QtWidgets.QListWidget,
        row_count: int,
    ) -> int:
        effective_rows = max(1, int(row_count))
        row_height = SettingsDialog._compact_list_item_size(widget).height()
        frame = widget.frameWidth() * 2
        return frame + (row_height * effective_rows) + 2

    def _dialog_scrollbar_stylesheet(self) -> str:
        return (
            BUTTON_FEEDBACK_STYLESHEET
            + """
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
        )

    def _configure_combo_popups(self) -> None:
        for combo in self.findChildren(QtWidgets.QComboBox):
            view = QtWidgets.QListView(combo)
            view.setUniformItemSizes(True)
            view.setLayoutMode(QtWidgets.QListView.SinglePass)
            view.setSpacing(0)
            view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerItem)
            combo.setView(view)
            combo.setMaxVisibleItems(12)

    @staticmethod
    def _disable_combo_popup_effects() -> None:
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        ui_effect_enum = getattr(QtCore.Qt, "UIEffect", None)
        for name in ("UI_AnimateCombo", "UI_AnimateMenu", "UI_FadeMenu"):
            effect = getattr(QtCore.Qt, name, None)
            if effect is None and ui_effect_enum is not None:
                effect = getattr(ui_effect_enum, name, None)
            if effect is None:
                continue
            try:
                QtWidgets.QApplication.setEffectEnabled(effect, False)
            except Exception:
                pass

    def _log_settings_timing(
        self,
        event: str,
        started_at: float,
        **fields: object,
    ) -> None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        parts = [f"event={event}", f"elapsed_ms={elapsed_ms:.1f}"]
        for key, value in fields.items():
            parts.append(f"{key}={self._settings_timing_value(value)}")
        self._settings_perf_logger.info("settings_timing %s", " ".join(parts))

    @staticmethod
    def _settings_timing_value(value: object) -> str:
        if isinstance(value, float):
            return f"{value:.1f}"
        return str(value).strip().replace(" ", "_") or "-"

    def _schedule_settings_tab_prewarm(self) -> None:
        if self._local_tab_index not in self._settings_perf_prewarmed_tab_indexes:
            QtCore.QTimer.singleShot(
                25,
                lambda: self._prewarm_settings_tabs((self._local_tab_index,)),
            )
        if self._benchmark_tab_index not in self._settings_perf_prewarmed_tab_indexes:
            QtCore.QTimer.singleShot(
                800,
                lambda: self._prewarm_settings_tabs((self._benchmark_tab_index,)),
            )

    def prepare_for_first_show(self) -> None:
        self._prewarm_settings_tabs(
            (self._local_tab_index,),
            require_visible=False,
        )
        QtCore.QTimer.singleShot(
            800,
            lambda: self._prewarm_settings_tabs(
                (self._benchmark_tab_index,),
                require_visible=False,
            ),
        )

    def reload_from_store(self) -> None:
        started_at = time.perf_counter()
        self._loaded_settings = self._settings_store.load()
        self._populate(self._loaded_settings)
        self._log_settings_timing("reload_from_store", started_at)

    def _prewarm_settings_tabs(
        self,
        indexes: tuple[int | None, ...],
        *,
        require_visible: bool = True,
    ) -> None:
        if require_visible and not self.isVisible():
            return
        started_at = time.perf_counter()
        warmed_tabs: list[str] = []
        for index in indexes:
            if index is None or index in self._settings_perf_prewarmed_tab_indexes:
                continue
            tab_started_at = time.perf_counter()
            self._prewarm_tab_widget(index)
            self._settings_perf_prewarmed_tab_indexes.add(index)
            tab_name = self.tabs.tabText(index)
            warmed_tabs.append(self.tabs.tabText(index))
            self._log_settings_timing(
                "tab_prewarm",
                tab_started_at,
                tab=tab_name,
            )
        if not warmed_tabs:
            return
        self._log_settings_timing(
            "tabs_prewarm",
            started_at,
            tabs=",".join(warmed_tabs),
        )

    def _prewarm_tab_widget(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is None:
            return
        self._activate_widget_layout(widget)
        if isinstance(widget, QtWidgets.QScrollArea):
            inner = widget.widget()
            if inner is not None:
                self._activate_widget_layout(inner)
        for list_widget in widget.findChildren(QtWidgets.QListWidget):
            list_widget.doItemsLayout()
            list_widget.updateGeometry()

    @staticmethod
    def _activate_widget_layout(widget: QtWidgets.QWidget) -> None:
        widget.ensurePolished()
        layout = widget.layout()
        if layout is not None:
            layout.activate()

    def _create_scroll_tab(self) -> tuple[QtWidgets.QScrollArea, QtWidgets.QWidget]:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.AdjustIgnored)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        content = QtWidgets.QWidget()
        content.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
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
            "local": "Local (faster-whisper / ONNX)",
            "assemblyai": "Remote (AssemblyAI)",
            "groq": "Remote (Groq)",
            "openai": "Remote (OpenAI)",
            "deepgram": "Remote (Deepgram)",
            "elevenlabs": "Remote (ElevenLabs)",
            "azure": "Remote (Azure LLM Speech)",
            "funasr": "Remote (Fun-ASR / Alibaba)",
        }
        for value in VALID_ENGINES:
            self.engine_combo.addItem(engine_labels.get(value, value), value)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        engine_hint = QtWidgets.QLabel(
            "Local keeps audio on your machine. Local models can use either "
            "faster-whisper, ONNX/WebGPU, or ORT GenAI."
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
            "Streaming inserts only stable append-only text while speaking and "
            "auto-aborts on focus change. Batch remains the recommended default."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_hint = QtWidgets.QLabel(
            "Batch inserts text after recording stops. Streaming can append stable "
            "text while you speak, but it never rewrites already inserted text."
        )
        mode_hint.setWordWrap(True)
        self._style_note_label(mode_hint)
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
        self._style_note_label(streaming_full_final_hint)
        engine_form.addRow(
            "",
            self._field_with_hint(
                self.streaming_full_final_check,
                streaming_full_final_hint,
            ),
        )

        self.concurrent_mode_combo = _WheelPassthroughComboBox()
        concurrent_mode_labels = {
            "insert": "Queue & insert into its window",
            "history": "Queue & save to history only",
            "cancel": "Cancel the running transcription",
        }
        for value in VALID_CONCURRENT_TRANSCRIPTION_MODES:
            self.concurrent_mode_combo.addItem(
                concurrent_mode_labels.get(value, value), value
            )
        self.concurrent_mode_combo.setToolTip(
            "What happens to a transcription that is still running when you start "
            "a new recording. A finished transcription is never discarded.\n"
            "- Insert: keep it running, insert its result into the window that "
            "was focused when it was recorded, and save it to history.\n"
            "- History only: keep it running, save its result to history without "
            "inserting it.\n"
            "- Cancel: request a real stop (local compute is aborted; a remote "
            "upload that has not started yet never starts). If it still finishes, "
            "it is saved to history."
        )
        concurrent_mode_hint = QtWidgets.QLabel(
            "Local and remote engines share one transcription worker, so jobs run "
            "one at a time. Use the overlay queue (with per-item cancel) to stop a "
            "specific transcription; canceling a local transcription stops its "
            "compute between segments, a not-yet-started one never starts, and a "
            "result that still completes is kept in history."
        )
        concurrent_mode_hint.setWordWrap(True)
        self._style_note_label(concurrent_mode_hint)
        engine_form.addRow(
            "While transcribing",
            self._field_with_hint(self.concurrent_mode_combo, concurrent_mode_hint),
        )
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
            "SendInput simulates the real Ctrl+V keyboard shortcut. "
            "WM_PASTE sends a paste message directly to the focused edit control; "
            "some modern apps ignore it."
        )
        self.paste_mode_hint_label = QtWidgets.QLabel(
            "Paste Mode controls how the paste command reaches the target app. "
            "SendInput behaves like pressing Ctrl+V and works in most apps; "
            "WM_PASTE bypasses keyboard simulation and can help when simulated "
            "keys are blocked, but some modern apps ignore that message. "
            "Auto tries SendInput first, then WM_PASTE."
        )
        self.paste_mode_hint_label.setWordWrap(True)
        self._style_note_label(self.paste_mode_hint_label)
        paste_form.addRow(
            "Paste Mode",
            self._field_with_hint(self.paste_mode_combo, self.paste_mode_hint_label),
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

        self._apply_shared_form_label_width(
            (
                hotkey_form,
                engine_form,
                paste_form,
                audio_form,
                recordings_form,
                appearance_form,
            )
        )
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
        self.local_model_runtime_warning_label = QtWidgets.QLabel("")
        self.local_model_runtime_warning_label.setWordWrap(True)
        self.local_model_runtime_warning_label.setStyleSheet(
            "color: #b71c1c; font-size: 11px;"
        )
        self.local_model_runtime_warning_label.setVisible(False)
        form.addRow(
            "Model Size",
            self._field_with_hint(
                self.model_combo,
                self.local_model_runtime_warning_label,
            ),
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
        self.model_dir_edit.textChanged.connect(self._on_model_dir_changed)
        self._match_field_button_height(self.model_dir_edit, self.model_dir_browse)
        model_dir_layout = QtWidgets.QHBoxLayout()
        self._configure_button_row(
            model_dir_layout,
            spacing=_INLINE_FIELD_BUTTON_SPACING_PX,
        )
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

        self.keep_onnx_model_loaded_checkbox = QtWidgets.QCheckBox(
            "Keep Cohere/Granite ONNX model loaded after dictation"
        )
        self.keep_onnx_model_loaded_checkbox.setToolTip(
            "Expert option for Cohere and Granite. Keeps the last ONNX runtime "
            "process alive so short follow-up dictations skip model load time. "
            "Disable it if RAM or GPU memory pressure matters more."
        )
        keep_onnx_note = QtWidgets.QLabel(
            "Cohere and Granite can use several GB of RAM/VRAM while loaded. "
            "Nemotron stays warm like faster-whisper so streaming starts promptly. "
            "Benchmarks always close each case after measuring it."
        )
        keep_onnx_note.setWordWrap(True)
        self._style_note_label(keep_onnx_note)
        form.addRow(
            "",
            self._field_with_hint(
                self.keep_onnx_model_loaded_checkbox,
                keep_onnx_note,
            ),
        )

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
            "Select models to download or delete. Downloads run one at a time; "
            "you can add more models to the queue while one is active. Green "
            "entries are already cached locally. ONNX models use a Node.js "
            "local runtime."
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
        self._configure_button_row(manage_buttons)
        self.refresh_local_models_button = QtWidgets.QPushButton("Refresh")
        self.refresh_local_models_button.clicked.connect(
            self._refresh_local_model_views
        )
        self.download_selected_models_button = QtWidgets.QPushButton(
            "Download / Queue Selected"
        )
        self.download_selected_models_button.clicked.connect(
            self._download_selected_local_models
        )
        self.download_all_missing_models_button = QtWidgets.QPushButton(
            "Download / Queue All Missing"
        )
        self.download_all_missing_models_button.clicked.connect(
            self._download_all_missing_local_models
        )
        self.cancel_model_downloads_button = QtWidgets.QPushButton("Cancel Downloads")
        self.cancel_model_downloads_button.setEnabled(False)
        self.cancel_model_downloads_button.clicked.connect(
            self._cancel_local_model_downloads
        )
        self.delete_selected_model_button = QtWidgets.QPushButton("Delete Selected")
        self.delete_selected_model_button.setEnabled(False)
        self.delete_selected_model_button.clicked.connect(
            self._delete_selected_cached_model
        )
        manage_buttons.addWidget(self.refresh_local_models_button)
        manage_buttons.addWidget(self.download_selected_models_button)
        manage_buttons.addWidget(self.download_all_missing_models_button)
        manage_buttons.addWidget(self.cancel_model_downloads_button)
        manage_buttons.addStretch(1)
        manage_buttons.addWidget(self.delete_selected_model_button)
        local_models_layout.addLayout(manage_buttons)

        self.local_models_action_label = QtWidgets.QLabel("")
        self.local_models_action_label.setWordWrap(True)
        local_models_layout.addWidget(self.local_models_action_label)

        self.local_model_download_progress_bar = QtWidgets.QProgressBar()
        self.local_model_download_progress_bar.setRange(0, 100)
        self.local_model_download_progress_bar.setTextVisible(True)
        self.local_model_download_progress_bar.setVisible(False)
        local_models_layout.addWidget(self.local_model_download_progress_bar)
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
            "Benchmark installed local models against one audio file. "
            "Cohere and Granite 4.0 use q4 ONNX weights; Granite 4.1 uses "
            "INT8 ONNX weights. Test Auto, GPU-only, CPU-only, DirectML, or "
            "WebGPU targets on this machine."
        )
        intro.setWordWrap(True)
        self._style_note_label(intro)
        layout.addWidget(intro)

        audio_box = QtWidgets.QGroupBox("Audio Sample")
        audio_layout = QtWidgets.QVBoxLayout(audio_box)
        audio_row = QtWidgets.QHBoxLayout()
        self._configure_button_row(
            audio_row,
            spacing=_INLINE_FIELD_BUTTON_SPACING_PX,
        )
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
        self._match_field_button_height(
            self.benchmark_audio_edit,
            self.benchmark_audio_browse_button,
            self.benchmark_audio_last_button,
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
            "Use a representative sample. The benchmark measures model speed and runtime factor on this file. "
            "Cohere and Granite require WAV input in the Node runtime."
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

        self.benchmark_webgpu_device_combo = _WheelPassthroughComboBox()
        webgpu_device_choices = (
            ("Auto (WebGPU -> DirectML -> CPU)", "auto"),
            ("GPU only (WebGPU -> DirectML)", "gpu"),
            ("CPU only", "cpu"),
            ("GPU + CPU comparison", "gpu,cpu"),
            ("DirectML only", "dml"),
            ("WebGPU only", "webgpu"),
            ("All explicit targets", "all"),
        )
        for label, value in webgpu_device_choices:
            if value in LOCAL_WEBGPU_BENCHMARK_DEVICE_GROUPS:
                self.benchmark_webgpu_device_combo.addItem(label, value)
        self.benchmark_webgpu_device_combo.setToolTip(
            "Controls only Cohere and Granite ONNX benchmarks. Auto tries GPU first "
            "and falls back to CPU; GPU-only fails instead of using CPU."
        )
        webgpu_device_note = QtWidgets.QLabel(
            "ONNX target selection. Faster-whisper models ignore this and use the standard Device setting."
        )
        webgpu_device_note.setWordWrap(True)
        self._style_note_label(webgpu_device_note)
        options_form.addRow(
            "ONNX Device",
            self._field_with_hint(
                self.benchmark_webgpu_device_combo,
                webgpu_device_note,
            ),
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
            "Decoder search width for faster-whisper. Cohere and Granite ignore this setting."
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
        self._configure_button_row(benchmark_actions)
        self.run_benchmark_button = QtWidgets.QPushButton("Run Benchmark")
        self.run_benchmark_button.clicked.connect(self._run_local_benchmark)
        self.cancel_benchmark_button = QtWidgets.QPushButton("Cancel Benchmark")
        self.cancel_benchmark_button.setEnabled(False)
        self.cancel_benchmark_button.clicked.connect(self._cancel_local_benchmark)
        self.clear_benchmark_results_button = QtWidgets.QPushButton("Clear Results")
        self.clear_benchmark_results_button.clicked.connect(
            self._clear_benchmark_results
        )
        self.export_benchmark_results_button = QtWidgets.QPushButton("Export Results...")
        self.export_benchmark_results_button.setEnabled(False)
        self.export_benchmark_results_button.clicked.connect(
            self._export_current_benchmark_results
        )
        benchmark_actions.addWidget(self.run_benchmark_button)
        benchmark_actions.addWidget(self.cancel_benchmark_button)
        benchmark_actions.addWidget(self.clear_benchmark_results_button)
        benchmark_actions.addWidget(self.export_benchmark_results_button)
        benchmark_actions.addStretch(1)
        layout.addLayout(benchmark_actions)

        self.benchmark_status_label = QtWidgets.QLabel("")
        self.benchmark_status_label.setWordWrap(True)
        layout.addWidget(self.benchmark_status_label)

        results_box = QtWidgets.QGroupBox("Results")
        results_layout = QtWidgets.QVBoxLayout(results_box)
        self.benchmark_results_table = QtWidgets.QTableWidget(0, 7)
        self.benchmark_results_table.setHorizontalHeaderLabels(
            ["Model", "Device", "Compute", "Load", "Avg", "RTF", "Status"]
        )
        self.benchmark_results_table.verticalHeader().setVisible(False)
        benchmark_row_height = self._compact_table_row_height(
            self.benchmark_results_table
        )
        self.benchmark_results_table.verticalHeader().setMinimumSectionSize(
            benchmark_row_height
        )
        self.benchmark_results_table.verticalHeader().setDefaultSectionSize(
            benchmark_row_height
        )
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

        history_box = QtWidgets.QGroupBox("Benchmark History")
        history_layout = QtWidgets.QVBoxLayout(history_box)
        history_layout.setContentsMargins(10, 10, 10, 10)
        history_layout.setSpacing(6)
        self.benchmark_history_list = QtWidgets.QListWidget()
        self._configure_compact_list_widget(self.benchmark_history_list, expand=True)
        self.benchmark_history_list.itemSelectionChanged.connect(
            self._update_benchmark_history_actions
        )
        self.benchmark_history_list.itemDoubleClicked.connect(
            self._load_benchmark_history_item
        )
        history_layout.addWidget(self.benchmark_history_list, 1)

        benchmark_history_actions = QtWidgets.QHBoxLayout()
        self._configure_button_row(benchmark_history_actions)
        self.load_benchmark_history_button = QtWidgets.QPushButton("Load Selected")
        self.load_benchmark_history_button.setEnabled(False)
        self.load_benchmark_history_button.clicked.connect(
            self._load_selected_benchmark_history
        )
        self.export_benchmark_history_button = QtWidgets.QPushButton("Export Selected...")
        self.export_benchmark_history_button.setEnabled(False)
        self.export_benchmark_history_button.clicked.connect(
            self._export_selected_benchmark_history
        )
        self.delete_benchmark_history_button = QtWidgets.QPushButton("Delete Selected")
        self.delete_benchmark_history_button.setEnabled(False)
        self.delete_benchmark_history_button.clicked.connect(
            self._delete_selected_benchmark_history
        )
        self.clear_benchmark_history_button = QtWidgets.QPushButton("Clear History")
        self.clear_benchmark_history_button.clicked.connect(
            self._clear_benchmark_history
        )
        benchmark_history_actions.addWidget(self.load_benchmark_history_button)
        benchmark_history_actions.addWidget(self.export_benchmark_history_button)
        benchmark_history_actions.addStretch(1)
        benchmark_history_actions.addWidget(self.delete_benchmark_history_button)
        benchmark_history_actions.addWidget(self.clear_benchmark_history_button)
        history_layout.addLayout(benchmark_history_actions)
        layout.addWidget(history_box)

        self._benchmark_tab_index = self.tabs.addTab(tab, "Benchmark")

    # --- Remote tab ---

    def _build_remote_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # API keys
        provider_box = QtWidgets.QGroupBox("Remote Provider API Keys")
        provider_layout = QtWidgets.QVBoxLayout(provider_box)
        provider_layout.setContentsMargins(10, 10, 10, 10)
        provider_layout.setSpacing(6)
        provider_rows = (
            ("assemblyai", "AssemblyAI"),
            ("groq", "Groq"),
            ("openai", "OpenAI"),
            ("deepgram", "Deepgram"),
            ("elevenlabs", "ElevenLabs"),
            ("azure", "Azure"),
            ("funasr", "Fun-ASR"),
        )
        provider_intro = QtWidgets.QLabel(
            "Enter a key only when you want to replace the stored one. The status badge shows whether the app already has a usable key."
        )
        provider_intro.setWordWrap(True)
        self._style_note_label(provider_intro)
        provider_layout.addWidget(provider_intro)

        provider_label_width = self._remote_provider_label_width(provider_rows)
        status_badge_width = self._provider_status_badge_width()
        provider_grid = QtWidgets.QGridLayout()
        provider_grid.setContentsMargins(0, 0, 0, 0)
        provider_grid.setHorizontalSpacing(_REMOTE_PROVIDER_GRID_SPACING_PX)
        provider_grid.setVerticalSpacing(3)
        provider_grid.setColumnMinimumWidth(0, provider_label_width)
        provider_grid.setColumnStretch(1, 1)
        provider_grid.setColumnStretch(2, 0)
        provider_grid.setColumnStretch(3, 0)

        grid_row = 0
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
            self._match_field_button_height(key_field, clear_button)
            clear_button.clicked.connect(
                lambda _checked=False, p=provider: self._mark_provider_key_for_clear(p)
            )

            status_badge = QtWidgets.QLabel("Not configured")
            status_badge.setAlignment(
                QtCore.Qt.AlignCenter | QtCore.Qt.AlignVCenter
            )
            status_badge.setFixedWidth(status_badge_width)
            status_badge.setSizePolicy(
                QtWidgets.QSizePolicy.Fixed,
                QtWidgets.QSizePolicy.Fixed,
            )
            status_badge.setStyleSheet(
                "padding: 2px 8px; border: 1px solid #bbb; border-radius: 9px;"
                " color: #555; background: #f2f2f2;"
            )

            title_label = QtWidgets.QLabel(title)
            title_label.setFixedWidth(provider_label_width)
            title_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)

            last_test_label = QtWidgets.QLabel("Last test: never.")
            last_test_label.setWordWrap(True)
            self._style_provider_last_test_label(last_test_label)
            provider_grid.addWidget(
                title_label,
                grid_row,
                0,
                QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            )
            provider_grid.addWidget(key_field, grid_row, 1)
            provider_grid.addWidget(clear_button, grid_row, 2)
            provider_grid.addWidget(status_badge, grid_row, 3)
            provider_grid.addWidget(last_test_label, grid_row + 1, 1, 1, 3)
            provider_grid.setRowMinimumHeight(
                grid_row + 1,
                max(1, self.fontMetrics().height()),
            )
            grid_row += 2

            self._provider_key_edits[provider] = key_field
            self._provider_status_labels[provider] = status_badge
            self._provider_last_test_labels[provider] = last_test_label

        self.assemblyai_key_edit = self._provider_key_edits["assemblyai"]
        self.groq_key_edit = self._provider_key_edits["groq"]
        self.openai_key_edit = self._provider_key_edits["openai"]
        self.deepgram_key_edit = self._provider_key_edits["deepgram"]
        self.elevenlabs_key_edit = self._provider_key_edits["elevenlabs"]
        self.azure_key_edit = self._provider_key_edits["azure"]
        self.funasr_key_edit = self._provider_key_edits["funasr"]

        # Azure additionally needs a per-resource endpoint (no other provider
        # does), so it gets a dedicated, non-secret text field here.
        self.azure_endpoint_edit = QtWidgets.QLineEdit()
        self.azure_endpoint_edit.setPlaceholderText(
            "https://<resource>.cognitiveservices.azure.com"
        )
        self.azure_endpoint_edit.setMinimumWidth(180)
        azure_endpoint_hint = QtWidgets.QLabel(
            "Required for Azure LLM Speech. Copy the endpoint from your Azure "
            "Speech / Foundry resource (Keys and Endpoint). The region must "
            "support LLM Speech."
        )
        azure_endpoint_hint.setWordWrap(True)
        self._style_note_label(azure_endpoint_hint)
        azure_endpoint_label = QtWidgets.QLabel("Azure Endpoint")
        azure_endpoint_label.setFixedWidth(provider_label_width)
        azure_endpoint_label.setAlignment(
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
        )
        provider_grid.addWidget(
            azure_endpoint_label,
            grid_row,
            0,
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
        )
        provider_grid.addWidget(self.azure_endpoint_edit, grid_row, 1, 1, 3)
        provider_grid.addWidget(azure_endpoint_hint, grid_row + 1, 1, 1, 3)
        grid_row += 2

        provider_note = QtWidgets.QLabel(
            "Status badges show where each key is currently sourced from."
        )
        self._style_note_label(provider_note)
        provider_grid.addWidget(provider_note, grid_row, 1, 1, 3)
        grid_row += 1

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
        provider_grid.addWidget(self.insecure_key_storage_checkbox, grid_row, 1, 1, 3)
        grid_row += 1

        self.key_storage_status_label = QtWidgets.QLabel("")
        self.key_storage_status_label.setWordWrap(True)
        self._style_note_label(self.key_storage_status_label)
        self.save_api_keys_button = QtWidgets.QPushButton("Save API Keys")
        self.save_api_keys_button.setToolTip(
            "Store entered API keys without applying all settings or refreshing the app."
        )
        self.save_api_keys_button.clicked.connect(self._save_api_keys_only)
        provider_grid.addWidget(
            self.save_api_keys_button,
            grid_row,
            0,
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
        )
        provider_grid.addWidget(self.key_storage_status_label, grid_row, 1, 1, 3)
        grid_row += 1

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
        self.test_conn_target_combo.addItem("Azure only", "azure")
        self.test_conn_target_combo.addItem("Fun-ASR only", "funasr")
        self.test_conn_target_combo.setToolTip(
            "Choose which provider to test. "
            "This is independent from the transcription engine selection."
        )
        connection_target_label = QtWidgets.QLabel("Connection Target")
        connection_target_label.setFixedWidth(provider_label_width)
        connection_target_label.setAlignment(
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
        )
        provider_grid.addWidget(
            connection_target_label,
            grid_row,
            0,
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
        )
        provider_grid.addWidget(self.test_conn_target_combo, grid_row, 1, 1, 3)
        grid_row += 1

        # Test connection
        self.test_conn_button = QtWidgets.QPushButton("Run Connection Test")
        self.test_conn_button.setToolTip(
            "Test one provider or all configured providers. "
            "Typed key input is preferred over stored key."
        )
        self.test_conn_button.clicked.connect(self._test_connection)
        self.test_conn_result = QtWidgets.QLabel("")
        self.test_conn_result.setWordWrap(True)
        provider_grid.addWidget(
            self.test_conn_button,
            grid_row,
            0,
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
        )
        provider_grid.addWidget(self.test_conn_result, grid_row, 1, 1, 3)
        provider_layout.addLayout(provider_grid)

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
        self.history_max_spin.setKeyboardTracking(False)
        self.history_max_spin.setValue(DEFAULT_HISTORY_MAX_ITEMS)
        self.history_max_spin.setToolTip(
            "Maximum transcript history items stored (0 = unlimited)."
        )
        self.history_max_spin.valueChanged.connect(
            lambda _value: self._refresh_history_list()
        )
        history_controls = QtWidgets.QHBoxLayout()
        self._configure_button_row(history_controls)
        history_controls.addWidget(QtWidgets.QLabel("History Size"))
        history_controls.addWidget(self.history_max_spin)
        history_controls.addStretch(1)
        layout.addLayout(history_controls)

        history_box = QtWidgets.QGroupBox("Transcript History")
        history_box.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        history_layout = QtWidgets.QVBoxLayout(history_box)
        history_layout.setContentsMargins(10, 10, 10, 10)
        history_layout.setSpacing(6)

        self.history_list = QtWidgets.QListWidget()
        history_font = QtGui.QFont(self.font())
        self.history_list.setFont(history_font)
        self.history_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection
        )
        self._configure_compact_list_widget(self.history_list, expand=True)
        self.history_list.itemSelectionChanged.connect(self._on_history_item_selected)

        self.history_detail = QtWidgets.QPlainTextEdit()
        self.history_detail.setReadOnly(True)
        self.history_detail.setFont(history_font)
        self.history_detail.setMinimumHeight(
            self.fontMetrics().height() * 4
        )
        self.history_detail.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )

        self.history_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.history_splitter.setChildrenCollapsible(False)
        self.history_splitter.addWidget(self.history_list)
        self.history_splitter.addWidget(self.history_detail)
        self.history_splitter.setStretchFactor(0, 2)
        self.history_splitter.setStretchFactor(1, 1)
        self.history_splitter.setSizes([400, 200])
        history_layout.addWidget(self.history_splitter, 1)

        history_buttons = QtWidgets.QHBoxLayout()
        self._configure_button_row(history_buttons)
        self.history_refresh_button = QtWidgets.QPushButton("Refresh")
        self.history_refresh_button.clicked.connect(self._refresh_history_list)
        self.history_copy_button = QtWidgets.QPushButton("Copy selected")
        self.history_copy_button.clicked.connect(self._copy_selected_history)
        self.history_copy_button.setEnabled(False)
        self.history_edit_button = QtWidgets.QPushButton("Edit selected")
        self.history_edit_button.clicked.connect(self._edit_selected_history)
        self.history_edit_button.setEnabled(False)
        self.history_delete_button = QtWidgets.QPushButton("Delete selected")
        self.history_delete_button.clicked.connect(self._delete_selected_history)
        self.history_delete_button.setEnabled(False)
        history_buttons.addWidget(self.history_refresh_button)
        history_buttons.addStretch(1)
        history_buttons.addWidget(self.history_copy_button)
        history_buttons.addWidget(self.history_edit_button)
        history_buttons.addWidget(self.history_delete_button)
        history_layout.addLayout(history_buttons)
        layout.addWidget(history_box, 1)
        self.tabs.addTab(tab, "History")

    def _build_import_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        self._import_tab = tab
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        import_box = QtWidgets.QGroupBox("Import Audio File")
        import_box.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        import_layout = QtWidgets.QVBoxLayout(import_box)

        import_controls = QtWidgets.QWidget()
        import_controls_layout = QtWidgets.QVBoxLayout(import_controls)
        import_controls_layout.setContentsMargins(0, 0, 0, 0)
        import_controls_layout.setSpacing(6)

        import_hint = QtWidgets.QLabel(
            "Transcribe an existing audio file and select the transcription service "
            "and model directly here (useful after failures or for external recordings)."
        )
        import_hint.setWordWrap(True)
        self._style_note_label(import_hint)
        import_controls_layout.addWidget(import_hint)

        self.import_engine_combo = _WheelPassthroughComboBox()
        import_engine_labels = {
            "local": "Local (faster-whisper / ONNX)",
            "assemblyai": "Remote (AssemblyAI)",
            "groq": "Remote (Groq)",
            "openai": "Remote (OpenAI)",
            "deepgram": "Remote (Deepgram)",
            "elevenlabs": "Remote (ElevenLabs)",
            "azure": "Remote (Azure LLM Speech)",
            "funasr": "Remote (Fun-ASR / Alibaba)",
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
        import_controls_layout.addWidget(QtWidgets.QLabel("Import Service"))
        import_controls_layout.addWidget(self.import_engine_combo)
        import_controls_layout.addWidget(self.import_engine_note)

        self.import_model_combo = _WheelPassthroughComboBox()
        self.import_model_note = QtWidgets.QLabel("")
        self.import_model_note.setWordWrap(True)
        self._style_note_label(self.import_model_note)
        self.import_model_combo.currentIndexChanged.connect(
            self._on_import_model_changed
        )
        import_controls_layout.addWidget(QtWidgets.QLabel("Import Model"))
        import_controls_layout.addWidget(self.import_model_combo)
        import_controls_layout.addWidget(self.import_model_note)

        import_buttons = QtWidgets.QHBoxLayout()
        self._configure_button_row(import_buttons)
        self.import_file_button = QtWidgets.QPushButton("Choose file...")
        self.import_file_button.clicked.connect(self._choose_import_file)
        self.import_last_recording_button = QtWidgets.QPushButton(
            "Use last recording"
        )
        self.import_last_recording_button.clicked.connect(
            self._select_last_recording_file
        )
        self.import_start_button = QtWidgets.QPushButton("Start transcription")
        self.import_start_button.setEnabled(False)
        self.import_start_button.clicked.connect(
            self._transcribe_selected_import_file
        )
        import_buttons.addWidget(self.import_file_button)
        import_buttons.addWidget(self.import_last_recording_button)
        import_buttons.addWidget(self.import_start_button)
        import_buttons.addStretch(1)
        import_controls_layout.addLayout(import_buttons)

        self.import_selected_file_label = QtWidgets.QLabel("No file selected.")
        self.import_selected_file_label.setWordWrap(True)
        self.import_selected_file_label.setStyleSheet("color: #555;")
        import_controls_layout.addWidget(self.import_selected_file_label)

        import_result = QtWidgets.QWidget()
        import_result_layout = QtWidgets.QVBoxLayout(import_result)
        import_result_layout.setContentsMargins(0, 0, 0, 0)
        import_result_layout.setSpacing(6)

        import_result_header = QtWidgets.QHBoxLayout()
        self._configure_button_row(import_result_header)
        import_result_header.addWidget(QtWidgets.QLabel("Result"))
        import_result_header.addStretch(1)
        self.import_copy_button = QtWidgets.QPushButton("Copy result")
        self.import_copy_button.setEnabled(False)
        self.import_copy_button.clicked.connect(self._copy_import_result)
        import_result_header.addWidget(self.import_copy_button)
        import_result_layout.addLayout(import_result_header)

        self.import_result_label = QtWidgets.QLabel("")
        self.import_result_label.setWordWrap(True)
        self.import_result_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse | QtCore.Qt.TextSelectableByKeyboard
        )
        import_result_layout.addWidget(self.import_result_label)

        self.import_result_text = QtWidgets.QPlainTextEdit()
        self.import_result_text.setReadOnly(True)
        self.import_result_text.setMinimumHeight(self.fontMetrics().height() * 12)
        self.import_result_text.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        import_result_layout.addWidget(self.import_result_text, 1)

        self.import_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.import_splitter.setChildrenCollapsible(False)
        self.import_splitter.addWidget(import_controls)
        self.import_splitter.addWidget(import_result)
        self.import_splitter.setStretchFactor(0, 0)
        self.import_splitter.setStretchFactor(1, 1)
        self.import_splitter.setSizes([320, 420])
        import_layout.addWidget(self.import_splitter, 1)

        self._selected_import_file_path = ""

        layout.addWidget(import_box, 1)
        self.tabs.addTab(tab, "Import Audio")

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        started_at = time.perf_counter()
        super().showEvent(event)
        self._apply_initial_dialog_size()
        self._log_settings_timing("show_event", started_at)
        if not self._settings_perf_logged_first_show:
            self._settings_perf_logged_first_show = True
            QtCore.QTimer.singleShot(
                0,
                lambda started_at=started_at: self._log_settings_timing(
                    "first_show_paint",
                    started_at,
                ),
            )
        self._schedule_settings_tab_prewarm()

    # ------------------------------------------------------------------
    # Model combo helpers
    # ------------------------------------------------------------------

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

    def _local_model_cache_key(self, model_dir: str | None = None) -> str:
        return str(model_dir or "").strip()

    def _prime_local_model_views_from_session_cache(self) -> bool:
        started_at = time.perf_counter()
        cache_key = self._local_model_cache_key(self.model_dir_edit.text())
        if cache_key not in _LOCAL_MODEL_SCAN_SESSION_CACHE:
            return False
        cached = list(_LOCAL_MODEL_SCAN_SESSION_CACHE.get(cache_key, []))
        self._cached_local_models = cached
        self._cached_local_models_dir = cache_key
        self._cached_local_models_available = True
        self._apply_local_model_scan_result(cached)
        if cache_key in _LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS:
            self._local_model_auto_refreshed_dirs.add(cache_key)
        else:
            self._local_model_inventory_loaded_from_cache_dirs.add(cache_key)
            self._set_local_model_scan_status(
                "Showing the last known local models while disk state is verified in the background."
            )
        self._log_settings_timing(
            "local_inventory_session_cache",
            started_at,
            model_dir=cache_key or "default",
            model_count=len(cached),
        )
        return True

    def _prime_local_model_views_from_persistent_cache(self) -> bool:
        started_at = time.perf_counter()
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
        self._local_model_inventory_loaded_from_cache_dirs.add(cache_key)
        self._set_local_model_scan_status(
            "Showing the last known local models while disk state is verified in the background."
        )
        self._log_settings_timing(
            "local_inventory_persistent_cache",
            started_at,
            model_dir=cache_key or "default",
            model_count=len(cached),
        )
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
        started_at = time.perf_counter()
        if not self._deferred_local_model_refresh_pending:
            return
        self._deferred_local_model_refresh_pending = False
        force = self._deferred_local_model_refresh_force
        self._deferred_local_model_refresh_force = False
        if not self._inventory_tab_is_visible():
            return
        model_dir = self._local_model_cache_key(self.model_dir_edit.text())
        if force and model_dir in self._local_model_auto_refresh_requested_dirs:
            return
        if force:
            self._local_model_auto_refresh_requested_dirs.add(model_dir)
        self._request_local_model_scan(force=force)
        self._log_settings_timing(
            "local_inventory_refresh_deferred",
            started_at,
            model_dir=model_dir or "default",
            force=force,
        )

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
        current_item = self.local_models_list.currentItem()
        current_model = (
            str(current_item.data(QtCore.Qt.UserRole) or "")
            if current_item is not None
            else ""
        )
        scroll_value = self.local_models_list.verticalScrollBar().value()
        cached_set = set(cached)
        with self._local_model_download_lock:
            cached_set.update(self._local_model_download_completed_names)

        restored_current_item: QtWidgets.QListWidgetItem | None = None
        self.local_models_list.setUpdatesEnabled(False)
        self.local_models_list.blockSignals(True)
        try:
            self.local_models_list.clear()
            for model_name in VALID_MODEL_SIZES:
                download_state = self._local_model_download_state(model_name)
                if download_state == "active":
                    status = "Downloading"
                elif download_state == "queued":
                    status = "Queued"
                else:
                    status = (
                        "Downloaded"
                        if model_name in cached_set
                        else "Not downloaded"
                    )
                if model_name in LOCAL_ENGLISH_ONLY_MODELS:
                    status = f"{status}, English only"
                if model_name in LOCAL_WEBGPU_MODEL_SIZES:
                    runtime = LOCAL_ONNX_MODEL_RUNTIME_LABELS.get(
                        model_name,
                        "ONNX/WebGPU",
                    )
                    status = f"{status}, {runtime}, batch only"
                elif model_name in LOCAL_NEMOTRON_MODEL_SIZES:
                    runtime = LOCAL_ONNX_MODEL_RUNTIME_LABELS.get(
                        model_name,
                        "ORT GenAI INT4",
                    )
                    status = f"{status}, {runtime}, batch and true streaming"
                item = QtWidgets.QListWidgetItem(
                    f"{self._model_label(model_name)} - {status}"
                )
                item.setData(QtCore.Qt.UserRole, model_name)
                item.setData(QtCore.Qt.UserRole + 1, model_name in cached_set)
                self._apply_compact_list_item_size(self.local_models_list, item)
                if model_name in cached_set:
                    item.setBackground(QtGui.QColor("#e8f5e9"))
                    item.setForeground(QtGui.QColor("#1b5e20"))
                elif download_state == "active":
                    item.setBackground(QtGui.QColor("#e3f2fd"))
                    item.setForeground(QtGui.QColor("#0d47a1"))
                elif download_state == "queued":
                    item.setBackground(QtGui.QColor("#fff8e1"))
                    item.setForeground(QtGui.QColor("#8d6e00"))
                self.local_models_list.addItem(item)
                if model_name in selected:
                    item.setSelected(True)
                if model_name == current_model:
                    restored_current_item = item
        finally:
            self.local_models_list.blockSignals(False)
            self.local_models_list.setUpdatesEnabled(True)

        if restored_current_item is not None:
            self.local_models_list.setCurrentItem(
                restored_current_item,
                QtCore.QItemSelectionModel.NoUpdate,
            )
        restore_vertical_scrollbar(self.local_models_list, scroll_value)

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
        current_item = self.benchmark_models_list.currentItem()
        current_model = (
            str(current_item.data(QtCore.Qt.UserRole) or "")
            if current_item is not None
            else ""
        )
        scroll_value = self.benchmark_models_list.verticalScrollBar().value()

        restored_current_item: QtWidgets.QListWidgetItem | None = None
        self.benchmark_models_list.setUpdatesEnabled(False)
        self.benchmark_models_list.blockSignals(True)
        try:
            self.benchmark_models_list.clear()
            for model_name in cached:
                suffix = (
                    " (English only)"
                    if model_name in LOCAL_ENGLISH_ONLY_MODELS
                    else ""
                )
                item = QtWidgets.QListWidgetItem(
                    f"{self._model_label(model_name)}{suffix}"
                )
                item.setData(QtCore.Qt.UserRole, model_name)
                self._apply_compact_list_item_size(self.benchmark_models_list, item)
                self.benchmark_models_list.addItem(item)
                if selected:
                    item.setSelected(model_name in selected)
                else:
                    item.setSelected(True)
                if model_name == current_model:
                    restored_current_item = item
        finally:
            self.benchmark_models_list.blockSignals(False)
            self.benchmark_models_list.setUpdatesEnabled(True)

        if restored_current_item is not None:
            self.benchmark_models_list.setCurrentItem(
                restored_current_item,
                QtCore.QItemSelectionModel.NoUpdate,
            )
        restore_vertical_scrollbar(self.benchmark_models_list, scroll_value)

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
        started_at = time.perf_counter()
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
                not self._local_model_download_is_running()
            )
        self._set_local_model_scan_status(status_text)
        self._update_language_availability()
        self._update_local_model_actions()
        self._update_benchmark_actions()
        self._log_settings_timing("local_inventory_render_unverified", started_at)

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
        started_at = time.perf_counter()
        with self._local_model_download_lock:
            self._local_model_download_completed_names.difference_update(cached)
        self._refresh_local_models_label(cached)
        self._refresh_local_models_list(cached)
        self._refresh_model_combo(cached=cached)
        self._refresh_benchmark_model_list(cached)
        self._set_local_model_scan_status("")
        self.local_models_list.setEnabled(True)
        self.benchmark_models_list.setEnabled(True)
        self.refresh_local_models_button.setEnabled(
            not self._local_model_download_is_running()
        )
        self._update_language_availability()
        self._update_local_model_actions()
        self._update_benchmark_actions()
        self._log_settings_timing(
            "local_inventory_render",
            started_at,
            model_count=len(cached),
        )

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
        self._local_model_inventory_loaded_from_cache_dirs.discard(cache_key)
        _LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS.discard(cache_key)

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
        if delay_ms <= 0:
            self._set_local_model_scan_loading(preserve_current=preserve_current)
        elif preserve_current:
            self._set_local_model_scan_status(
                "Showing the last known local models while the cache is verified in the background."
            )
        self._schedule_deferred_local_model_refresh(delay_ms=delay_ms, force=True)

    def _request_local_model_scan(self, *, force: bool = False) -> None:
        request_started_at = time.perf_counter()
        model_dir = self.model_dir_edit.text().strip() if hasattr(self, "model_dir_edit") else ""
        if (
            not force
            and self._active_local_model_scan_thread is None
            and self._cached_local_models_available
            and model_dir == self._cached_local_models_dir
        ):
            self._apply_local_model_scan_result(self._cached_local_models)
            self._log_settings_timing(
                "local_inventory_scan_skipped_cached",
                request_started_at,
                model_dir=model_dir or "default",
            )
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
        self._local_model_scan_started_at_by_token[token] = time.perf_counter()
        self._log_settings_timing(
            "local_inventory_scan_start",
            request_started_at,
            model_dir=model_dir or "default",
            force=force,
            preserve_current=preserve_current,
        )

        def _run() -> None:
            try:
                cached = _scan_cached_models(model_dir)
            except Exception:
                cached = None
            _emit_background_signal(
                self,
                "local_model_scan_finished",
                token,
                model_dir,
                cached,
            )

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

        scan_started_at = self._local_model_scan_started_at_by_token.pop(token, None)
        if scan_started_at is not None:
            model_count = len(payload) if isinstance(payload, list) else 0
            self._log_settings_timing(
                "local_inventory_scan_finish",
                scan_started_at,
                model_dir=model_dir or "default",
                success=isinstance(payload, list),
                model_count=model_count,
            )

        self._active_local_model_scan_thread = None
        self._local_model_auto_refresh_requested_dirs.discard(model_dir)
        if not isinstance(payload, list):
            self._set_local_model_scan_status(
                "Local model verification did not finish. Showing cached inventory.",
                "#b26a00",
            )
            if self._local_model_scan_pending:
                self._local_model_scan_pending = False
                self._request_local_model_scan(force=True)
            return

        self._local_model_auto_refreshed_dirs.add(model_dir)
        cached = [value for value in payload if isinstance(value, str)]
        _LOCAL_MODEL_SCAN_SESSION_CACHE[model_dir] = list(cached)
        _LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS.add(model_dir)
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

    def _local_model_download_snapshot(
        self,
    ) -> tuple[tuple[str, str] | None, list[tuple[str, str]], bool]:
        with self._local_model_download_lock:
            return (
                self._local_model_download_active,
                list(self._local_model_download_queue),
                self._local_model_download_worker_running,
            )

    def _local_model_download_is_running(self) -> bool:
        _active, _queued, running = self._local_model_download_snapshot()
        return running

    def _local_model_download_state(self, model_name: str) -> str:
        active, queued, _running = self._local_model_download_snapshot()
        if active is not None and active[0] == model_name:
            return "active"
        if any(name == model_name for name, _model_dir in queued):
            return "queued"
        return ""

    def _local_model_download_pending_names(self) -> set[str]:
        active, queued, _running = self._local_model_download_snapshot()
        pending = {name for name, _model_dir in queued}
        if active is not None:
            pending.add(active[0])
        return pending

    def _update_local_model_actions(self) -> None:
        if not hasattr(self, "download_selected_models_button"):
            return

        busy = self._local_model_download_is_running()
        pending = self._local_model_download_pending_names()
        with self._local_model_download_lock:
            completed = set(self._local_model_download_completed_names)
        pending.update(completed)

        # Determine missing and downloaded from selection
        missing: list[str] = []
        selected_downloaded: list[str] = []
        if hasattr(self, "local_models_list"):
            for item in self.local_models_list.selectedItems():
                name = str(item.data(QtCore.Qt.UserRole) or "")
                if bool(item.data(QtCore.Qt.UserRole + 1)):
                    selected_downloaded.append(name)
                elif name not in pending:
                    missing.append(name)

        # Any missing models at all (for "Download All Missing")?
        any_missing = False
        if hasattr(self, "local_models_list"):
            for index in range(self.local_models_list.count()):
                item = self.local_models_list.item(index)
                name = str(item.data(QtCore.Qt.UserRole) or "")
                if not bool(item.data(QtCore.Qt.UserRole + 1)) and name not in pending:
                    any_missing = True
                    break

        self.local_models_list.setEnabled(True)
        self.refresh_local_models_button.setEnabled(not busy)
        self.delete_selected_model_button.setEnabled(
            (not busy) and bool(selected_downloaded)
        )
        self.download_selected_models_button.setEnabled(
            bool(missing)
        )
        self.download_all_missing_models_button.setEnabled(
            any_missing
        )
        self.cancel_model_downloads_button.setEnabled(busy)
        self.model_dir_edit.setEnabled(not busy)
        self.model_dir_browse.setEnabled(not busy)

    def _download_selected_local_models(self) -> None:
        selected = self._selected_downloadable_model_names()
        if not selected:
            return
        missing = self._missing_downloadable_models(selected)
        if not missing:
            self.local_models_action_label.setStyleSheet("color: #555;")
            self.local_models_action_label.setText(
                "All selected models are already downloaded or queued."
            )
            return
        self._start_local_model_download(missing)

    def _download_all_missing_local_models(self) -> None:
        missing = self._missing_downloadable_models()
        if not missing:
            self.local_models_action_label.setStyleSheet("color: #555;")
            self.local_models_action_label.setText(
                "All available local models are already downloaded or queued."
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
        pending = self._local_model_download_pending_names()
        with self._local_model_download_lock:
            pending.update(self._local_model_download_completed_names)
        missing: list[str] = []
        for index in range(self.local_models_list.count()):
            item = self.local_models_list.item(index)
            model_name = str(item.data(QtCore.Qt.UserRole) or "")
            if model_name not in wanted:
                continue
            if (
                not bool(item.data(QtCore.Qt.UserRole + 1))
                and model_name not in pending
            ):
                missing.append(model_name)
        return missing

    def _start_local_model_download(self, model_names: list[str]) -> None:
        if not model_names:
            return

        model_dir = self.model_dir_edit.text().strip()
        start_worker = False
        added: list[str] = []
        with self._local_model_download_lock:
            pending = {name for name, _model_dir in self._local_model_download_queue}
            if self._local_model_download_active is not None:
                pending.add(self._local_model_download_active[0])
            pending.update(self._local_model_download_completed_names)
            for model_name in model_names:
                if model_name in pending:
                    continue
                self._local_model_download_queue.append((model_name, model_dir))
                pending.add(model_name)
                added.append(model_name)
            if added and not self._local_model_download_worker_running:
                self._local_model_download_worker_running = True
                self._local_model_download_worker_token += 1
                worker_token = self._local_model_download_worker_token
                self._local_model_download_cancel_event.clear()
                start_worker = True

        if not added:
            self.local_models_action_label.setStyleSheet("color: #555;")
            self.local_models_action_label.setText(
                "The selected models are already downloaded or queued."
            )
            self._update_local_model_actions()
            return

        self.local_models_action_label.setStyleSheet("color: #555;")
        self.local_models_action_label.setText(
            f"Queued for download: {', '.join(added)}"
        )
        self._refresh_local_models_list()
        self._update_local_model_actions()
        self._local_model_download_progress_timer.start()

        if not start_worker:
            self._refresh_local_model_download_progress()
            return

        thread = threading.Thread(
            target=lambda: self._run_local_model_download_queue(worker_token),
            name="stt_app_local_model_download",
            daemon=True,
        )
        self._active_local_model_download_thread = thread
        thread.start()
        self._update_local_model_actions()

    def _cancel_local_model_downloads(self) -> None:
        with self._local_model_download_lock:
            if not self._local_model_download_worker_running:
                return
            self._local_model_download_cancel_event.set()
            queued_count = len(self._local_model_download_queue)
            self._local_model_download_queue.clear()
            process = self._local_model_download_process

        terminate_model_download_process(process)
        self.local_models_action_label.setStyleSheet("color: #b26a00;")
        suffix = (
            f" Removed {queued_count} queued model"
            f"{'s' if queued_count != 1 else ''}."
            if queued_count
            else ""
        )
        self.local_models_action_label.setText(
            f"Canceling active model download.{suffix}"
        )
        self._update_local_model_actions()

    def _download_local_model_in_subprocess(
        self,
        model_name: str,
        model_dir: str,
    ) -> tuple[str, str, int, int]:
        try:
            process = start_model_download_process(model_name, model_dir)
        except Exception as exc:
            return "failed", str(exc), 0, 0

        with self._local_model_download_lock:
            self._local_model_download_process = process
        try:
            while process.poll() is None:
                if self._local_model_download_cancel_event.wait(timeout=0.1):
                    terminate_model_download_process(process)
                    model_download_process_error(process)
                    removed_files, removed_bytes = cleanup_incomplete_model_download(
                        model_name,
                        model_dir,
                    )
                    return "canceled", "", removed_files, removed_bytes

            detail = model_download_process_error(process)
            if process.returncode == 0:
                return "success", "", 0, 0
            if self._local_model_download_cancel_event.is_set():
                removed_files, removed_bytes = cleanup_incomplete_model_download(
                    model_name,
                    model_dir,
                )
                return "canceled", "", removed_files, removed_bytes
            return "failed", detail or "Download worker failed.", 0, 0
        finally:
            with self._local_model_download_lock:
                if self._local_model_download_process is process:
                    self._local_model_download_process = None

    def _run_local_model_download_queue(self, worker_token: int) -> None:
        successes: list[str] = []
        failures: list[str] = []
        canceled = False
        cleaned_files = 0
        cleaned_bytes = 0
        while True:
            with self._local_model_download_lock:
                if (
                    self._local_model_download_cancel_event.is_set()
                    or not self._local_model_download_queue
                ):
                    canceled = self._local_model_download_cancel_event.is_set()
                    self._local_model_download_active = None
                    self._local_model_download_worker_running = False
                    break
                model_name, model_dir = self._local_model_download_queue.pop(0)
                self._local_model_download_active = (model_name, model_dir)
                queued_count = len(self._local_model_download_queue)

            _emit_background_signal(
                self,
                "local_model_download_progress",
                worker_token,
                f"Starting '{model_name}'. {queued_count} queued.",
            )
            status, detail, removed_files, removed_bytes = (
                self._download_local_model_in_subprocess(model_name, model_dir)
            )
            cleaned_files += removed_files
            cleaned_bytes += removed_bytes
            if status == "success":
                successes.append(model_name)
                with self._local_model_download_lock:
                    self._local_model_download_completed_names.add(model_name)
            elif status == "canceled":
                canceled = True
                with self._local_model_download_lock:
                    self._local_model_download_queue.clear()
                    self._local_model_download_active = None
                    self._local_model_download_worker_running = False
                break
            else:
                failures.append(f"{model_name}: {detail}")

        if canceled:
            cleanup_mb = cleaned_bytes / 1_000_000.0
            cleanup_detail = (
                f" Removed {cleaned_files} incomplete file"
                f"{'s' if cleaned_files != 1 else ''} ({cleanup_mb:.1f} MB)."
                if cleaned_files
                else " No incomplete files remained."
            )
            success_detail = (
                f" Completed before cancellation: {', '.join(successes)}."
                if successes
                else ""
            )
            _emit_background_signal(
                self,
                "local_model_download_finished",
                worker_token,
                False,
                f"Download canceled.{cleanup_detail}{success_detail}",
            )
            return

        if failures and successes:
            message = (
                f"Completed with errors. Downloaded: {', '.join(successes)}. "
                f"Failed: {' | '.join(failures)}"
            )
            _emit_background_signal(
                self,
                "local_model_download_finished",
                worker_token,
                False,
                message,
            )
            return
        if failures:
            _emit_background_signal(
                self,
                "local_model_download_finished",
                worker_token,
                False,
                f"Download failed: {' | '.join(failures)}",
            )
            return
        _emit_background_signal(
            self,
            "local_model_download_finished",
            worker_token,
            True,
            f"Downloaded: {', '.join(successes)}",
        )

    def _on_local_model_download_progress(self, worker_token: int, text: str) -> None:
        if worker_token != self._local_model_download_worker_token:
            return
        self.local_models_action_label.setStyleSheet("color: #555;")
        self.local_models_action_label.setText(text)
        self._refresh_local_models_list()
        self._refresh_local_model_download_progress()
        self._local_model_download_progress_timer.start()
        self._update_local_model_actions()

    def _refresh_local_model_download_progress(self) -> None:
        if not hasattr(self, "local_model_download_progress_bar"):
            return
        active, queued, running = self._local_model_download_snapshot()
        if not running or active is None:
            return

        model_name, model_dir = active
        downloaded_bytes = estimate_cached_model_bytes(model_name, model_dir)
        progress = self._local_model_download_speed_tracker.measure(
            model_name,
            downloaded_bytes,
        )

        self.local_models_action_label.setStyleSheet("color: #0d47a1;")
        self.local_models_action_label.setText(
            format_model_download_progress(progress, queued_count=len(queued))
        )
        if progress.percent is None:
            self.local_model_download_progress_bar.setRange(0, 0)
        else:
            self.local_model_download_progress_bar.setRange(0, 100)
            self.local_model_download_progress_bar.setValue(progress.percent)
            self.local_model_download_progress_bar.setFormat(
                f"{model_name}: approx. %p%"
            )
        self.local_model_download_progress_bar.setVisible(True)

    def _on_local_model_download_finished(
        self,
        worker_token: int,
        success: bool,
        text: str,
    ) -> None:
        if (
            worker_token != self._local_model_download_worker_token
            or self._local_model_download_is_running()
        ):
            return
        self._active_local_model_download_thread = None
        self._local_model_download_progress_timer.stop()
        self._local_model_download_speed_tracker.reset()
        self.local_model_download_progress_bar.setVisible(False)
        if success:
            self.local_models_action_label.setStyleSheet("color: #1b5e20;")
        elif text.startswith("Completed with errors"):
            self.local_models_action_label.setStyleSheet("color: #b26a00;")
        elif text.startswith("Download canceled"):
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
            self._recordings_file_dialog_dir(),
            "Audio files (*.wav *.mp3 *.m4a *.flac *.ogg *.opus *.webm);;All files (*)",
        )
        if path:
            self._set_benchmark_audio_path(path)

    def _use_last_recording_for_benchmark(self) -> None:
        path = self._last_recording_store.selectable_path(
            self._archived_recordings_dir_for_selection()
        )
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
        self.benchmark_webgpu_device_combo.setEnabled(not busy)
        self.benchmark_runs_spin.setEnabled(not busy)
        self.benchmark_beam_size_spin.setEnabled(not busy)
        self.benchmark_language_combo.setEnabled(not busy)
        self.benchmark_warmup_checkbox.setEnabled(not busy)
        self.benchmark_vad_checkbox.setEnabled(not busy)
        self.run_benchmark_button.setEnabled((not busy) and has_audio and has_models)
        self.cancel_benchmark_button.setEnabled(
            busy
            and self._benchmark_cancel_event is not None
            and not self._benchmark_cancel_event.is_set()
        )
        self.clear_benchmark_results_button.setEnabled(not busy)
        self.export_benchmark_results_button.setEnabled(
            (not busy) and self._current_benchmark_entry is not None
        )
        self._update_benchmark_history_actions()

    def _clear_benchmark_results(self) -> None:
        self._current_benchmark_cases = []
        self._current_benchmark_entry = None
        self._current_benchmark_options = None
        self._current_benchmark_environment = None
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
                case.device,
                case.compute_type,
                _format_seconds(case.load_seconds),
                _format_seconds(case.avg_seconds),
                _format_number(case.avg_rtf),
                status,
            ]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if column == len(values) - 1:
                    detail = case.error or case.runtime_details
                    if detail:
                        item.setToolTip(detail)
                self.benchmark_results_table.setItem(row, column, item)

    def _benchmark_summary(
        self,
        cases: list[BenchmarkCase],
        *,
        status: str,
        options: BenchmarkOptions | None = None,
        environment: BenchmarkEnvironment | None = None,
    ) -> str:
        selected_options = options or self._current_benchmark_options
        selected_environment = environment or self._current_benchmark_environment
        details = (
            selected_options.summary_details(status=_benchmark_status_text(status))
            if selected_options is not None
            else {"Status": _benchmark_status_text(status)}
        )
        return format_benchmark_summary(
            cases,
            details=details,
            environment=selected_environment,
        )

    def _benchmark_options_from_widgets(
        self,
        *,
        audio_path: str,
        model_names: list[str],
        compute_type: str,
        webgpu_devices: list[str],
        run_count: int,
        beam_size: int,
        language_value: str,
        use_vad: bool,
        warmup: bool,
        model_dir: str,
    ) -> BenchmarkOptions:
        audio = Path(audio_path)
        return BenchmarkOptions(
            audio_path=str(audio),
            audio_name=audio.name,
            model_names=model_names,
            device="auto",
            compute_type=compute_type,
            webgpu_devices=webgpu_devices,
            runs=run_count,
            beam_size=beam_size,
            language=language_value,
            vad_filter=use_vad,
            warmup=warmup,
            threads=0,
            model_dir=model_dir,
        )

    def _cancel_local_benchmark(self) -> None:
        if self._benchmark_cancel_event is None:
            return
        self._benchmark_cancel_event.set()
        self._set_benchmark_status(
            "Canceling benchmark after the current step...",
            "#b26a00",
        )
        self._update_benchmark_actions()

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
        webgpu_devices = normalize_webgpu_benchmark_devices(
            str(self.benchmark_webgpu_device_combo.currentData() or "auto")
        )
        run_count = int(self.benchmark_runs_spin.value())
        beam_size = int(self.benchmark_beam_size_spin.value())
        use_vad = self.benchmark_vad_checkbox.isChecked()
        warmup = self.benchmark_warmup_checkbox.isChecked()
        model_dir = self.model_dir_edit.text().strip()
        options = self._benchmark_options_from_widgets(
            audio_path=audio_path,
            model_names=model_names,
            compute_type=compute_type,
            webgpu_devices=webgpu_devices,
            run_count=run_count,
            beam_size=beam_size,
            language_value=language_value,
            use_vad=use_vad,
            warmup=warmup,
            model_dir=model_dir,
        )
        self._current_benchmark_cases = []
        self._current_benchmark_entry = None
        self._current_benchmark_options = options
        self._current_benchmark_environment = None
        cancel_event = threading.Event()
        self._benchmark_cancel_event = cancel_event
        self.benchmark_results_table.setRowCount(0)
        self.benchmark_summary_text.setPlainText(
            self._benchmark_summary([], status="running", options=options)
        )
        self._update_benchmark_actions()

        def _progress(text: str) -> None:
            self.benchmark_progress.emit(text)

        def _run() -> None:
            completed_cases: list[BenchmarkCase] = []
            environment = collect_benchmark_environment()
            self._current_benchmark_environment = environment

            def _case_finished(case: BenchmarkCase) -> None:
                completed_cases.append(case)
                self.benchmark_case_finished.emit(case)

            def _is_canceled() -> bool:
                return cancel_event.is_set()

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
                    webgpu_devices=webgpu_devices,
                    progress_callback=_progress,
                    case_callback=_case_finished,
                    cancel_check=_is_canceled,
                )
            except BenchmarkCancelled:
                summary = self._benchmark_summary(
                    completed_cases,
                    status="canceled",
                    options=options,
                    environment=environment,
                )
                self.benchmark_finished.emit(
                    True,
                    summary,
                    {
                        "cases": completed_cases,
                        "options": options,
                        "status": "canceled",
                        "environment": environment,
                    },
                )
                return
            except Exception as exc:
                self.benchmark_finished.emit(False, str(exc), [])
                return

            status = "completed_with_errors" if any(case.error for case in cases) else "completed"
            self.benchmark_finished.emit(
                True,
                self._benchmark_summary(
                    cases,
                    status=status,
                    options=options,
                    environment=environment,
                ),
                {
                    "cases": cases,
                    "options": options,
                    "status": status,
                    "environment": environment,
                },
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

    def _on_benchmark_case_finished(self, payload: object) -> None:
        if not isinstance(payload, BenchmarkCase):
            return
        self._current_benchmark_cases.append(payload)
        self._populate_benchmark_results(self._current_benchmark_cases)
        self.benchmark_summary_text.setPlainText(
            self._benchmark_summary(
                self._current_benchmark_cases,
                status="running",
            )
        )

    def _on_benchmark_finished(
        self,
        success: bool,
        text: str,
        payload: object,
    ) -> None:
        self._active_benchmark_thread = None
        self._benchmark_cancel_event = None
        self._update_benchmark_actions()
        if not success:
            self._set_benchmark_status(text, "#b71c1c")
            return

        status = "completed"
        options = self._current_benchmark_options
        raw_cases: object = payload
        if isinstance(payload, dict):
            raw_cases = payload.get("cases", [])
            raw_options = payload.get("options", None)
            if isinstance(raw_options, BenchmarkOptions):
                options = raw_options
            raw_environment = payload.get("environment", None)
            if isinstance(raw_environment, BenchmarkEnvironment):
                self._current_benchmark_environment = raw_environment
            status = str(payload.get("status", status))

        if not isinstance(raw_cases, (list, tuple)):
            raw_cases = []
        cases = [case for case in raw_cases if isinstance(case, BenchmarkCase)]
        if status == "completed" and any(case.error for case in cases):
            status = "completed_with_errors"
        self._current_benchmark_cases = cases
        self._current_benchmark_options = options
        self._populate_benchmark_results(cases)
        self.benchmark_summary_text.setPlainText(text)
        history_error = ""

        if cases and options is not None:
            entry = BenchmarkHistoryEntry.new(
                status=status,
                summary=text,
                options=options,
                cases=cases,
                environment=self._current_benchmark_environment,
            )
            self._current_benchmark_entry = entry
            try:
                self._benchmark_history_store.add_entry(entry)
            except Exception as exc:
                history_error = str(exc)
                self._refresh_benchmark_history_list()
            else:
                self._refresh_benchmark_history_list(select_entry=entry)
        else:
            self._current_benchmark_entry = None
            self._refresh_benchmark_history_list()

        if history_error:
            self._set_benchmark_status(
                f"Benchmark finished, but history could not be saved: {history_error}",
                "#b26a00",
            )
        elif status == "canceled":
            if cases:
                self._set_benchmark_status(
                    "Benchmark canceled. Partial results were saved.",
                    "#b26a00",
                )
            else:
                self._set_benchmark_status(
                    "Benchmark canceled before producing results.",
                    "#b26a00",
                )
        elif any(case.error for case in cases):
            self._set_benchmark_status(
                "Benchmark completed with errors. See the summary for details.",
                "#b26a00",
            )
        else:
            self._set_benchmark_status("Benchmark finished.", "#1b5e20")
        self._update_benchmark_actions()

    def _refresh_benchmark_history_list(
        self,
        *,
        select_entry: BenchmarkHistoryEntry | None = None,
    ) -> None:
        if not hasattr(self, "benchmark_history_list"):
            return
        self.benchmark_history_list.clear()
        selected_row = -1
        for row, entry in enumerate(self._benchmark_history_store.recent_entries(20)):
            item = QtWidgets.QListWidgetItem(_benchmark_history_label(entry))
            item.setData(QtCore.Qt.UserRole, entry)
            self._apply_compact_list_item_size(self.benchmark_history_list, item)
            self.benchmark_history_list.addItem(item)
            if (
                select_entry is not None
                and entry.identity_key() == select_entry.identity_key()
            ):
                selected_row = row
        if selected_row >= 0:
            self.benchmark_history_list.setCurrentRow(selected_row)
        self._update_benchmark_history_actions()

    def _selected_benchmark_history_entry(self) -> BenchmarkHistoryEntry | None:
        if not hasattr(self, "benchmark_history_list"):
            return None
        items = self.benchmark_history_list.selectedItems()
        if not items:
            return None
        entry = items[0].data(QtCore.Qt.UserRole)
        return entry if isinstance(entry, BenchmarkHistoryEntry) else None

    def _update_benchmark_history_actions(self) -> None:
        if not hasattr(self, "load_benchmark_history_button"):
            return
        busy = self._active_benchmark_thread is not None
        has_selection = self._selected_benchmark_history_entry() is not None
        self.load_benchmark_history_button.setEnabled((not busy) and has_selection)
        self.export_benchmark_history_button.setEnabled((not busy) and has_selection)
        self.delete_benchmark_history_button.setEnabled((not busy) and has_selection)
        self.clear_benchmark_history_button.setEnabled(
            (not busy) and self.benchmark_history_list.count() > 0
        )

    def _load_selected_benchmark_history(self) -> None:
        entry = self._selected_benchmark_history_entry()
        if entry is None:
            return
        self._load_benchmark_history_entry(entry)

    def _load_benchmark_history_item(self, item: QtWidgets.QListWidgetItem) -> None:
        entry = item.data(QtCore.Qt.UserRole)
        if isinstance(entry, BenchmarkHistoryEntry):
            self._load_benchmark_history_entry(entry)

    def _load_benchmark_history_entry(self, entry: BenchmarkHistoryEntry) -> None:
        self._current_benchmark_entry = entry
        self._current_benchmark_options = entry.options
        self._current_benchmark_environment = entry.environment
        self._current_benchmark_cases = list(entry.cases)
        self._populate_benchmark_results(entry.cases)
        self.benchmark_summary_text.setPlainText(entry.summary)
        self._set_benchmark_status("Loaded benchmark history entry.", "#555")
        self._update_benchmark_actions()

    def _export_current_benchmark_results(self) -> None:
        if self._current_benchmark_entry is None:
            return
        self._export_benchmark_entry(self._current_benchmark_entry)

    def _export_selected_benchmark_history(self) -> None:
        entry = self._selected_benchmark_history_entry()
        if entry is None:
            return
        self._export_benchmark_entry(entry)

    def _export_benchmark_entry(self, entry: BenchmarkHistoryEntry) -> None:
        suggested = (
            Path.home()
            / "Documents"
            / f"benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        path, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export benchmark results",
            str(suggested),
            "CSV files (*.csv);;Excel workbooks (*.xlsx);;Markdown files (*.md)",
        )
        if not path:
            return
        output_path = Path(path)
        if output_path.suffix.lower() not in {".csv", ".xlsx", ".md", ".markdown"}:
            if "xlsx" in selected_filter.lower():
                suffix = ".xlsx"
            elif "markdown" in selected_filter.lower() or "*.md" in selected_filter.lower():
                suffix = ".md"
            else:
                suffix = ".csv"
            output_path = output_path.with_suffix(suffix)
        try:
            export_benchmark_entry(output_path, entry)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "Export failed",
                f"Failed to export benchmark results: {exc}",
            )
            return
        self._set_benchmark_status(
            f"Benchmark exported to {output_path}.",
            "#1b5e20",
        )

    def _delete_selected_benchmark_history(self) -> None:
        entry = self._selected_benchmark_history_entry()
        if entry is None:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete benchmark entry",
            "Delete the selected benchmark result from history?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        removed = self._benchmark_history_store.delete_entry(entry)
        if removed <= 0:
            self._set_benchmark_status("Selected benchmark entry was not found.", "#b71c1c")
            return
        if (
            self._current_benchmark_entry is not None
            and self._current_benchmark_entry.identity_key() == entry.identity_key()
        ):
            self._current_benchmark_entry = None
            self._update_benchmark_actions()
        self._refresh_benchmark_history_list()

    def _clear_benchmark_history(self) -> None:
        if self._benchmark_history_store.count() <= 0:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Clear benchmark history",
            "Delete all stored benchmark results?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self._benchmark_history_store.clear()
        self._current_benchmark_entry = None
        self._refresh_benchmark_history_list()
        self._update_benchmark_actions()

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
        if normalized_engine == "azure" and selected_model:
            return replace(settings, azure_speech_model=selected_model)
        if normalized_engine == "funasr" and selected_model:
            return replace(settings, funasr_model=selected_model)
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
                "Local transcription uses the model selected on the Local tab. "
                "faster-whisper and Nemotron support streaming; Cohere and Granite "
                "ONNX/WebGPU models are batch-only."
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
                "AssemblyAI streaming uses the Universal-Streaming multilingual "
                "model. The selected model applies to batch transcription and "
                "imports."
            )
        elif provider == "deepgram":
            note = "Deepgram uses the selected model for batch and streaming transcription."
        elif provider == "elevenlabs":
            note = (
                "ElevenLabs currently uses the selected model for batch transcription "
                "and imports. Realtime Scribe is documented, but not yet wired into "
                "this app's streaming mode."
            )
        elif provider == "azure":
            note = (
                "Azure LLM Speech (MAI-Transcribe) is a cloud, batch-only service. "
                "Set the Azure Endpoint and key under Remote Provider API Keys. "
                "mai-transcribe-1.5 covers the most languages."
            )
        elif provider == "funasr":
            note = (
                "Fun-ASR (Alibaba/DashScope) is a cloud, batch-only service used "
                "here for its 31-language coverage (Chinese + East/SE-Asian). "
                "It does NOT support German; use Azure or local for German."
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
                "Batch requests use Universal-3 Pro with Universal-2 fallback, "
                "providing the broad Universal-2 language list."
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
                "ONNX model: Batch mode only. Auto tries WebGPU, then DirectML, "
                "then falls back to CPU. The active device appears in the "
                "overlay/import status."
            )
            self.local_model_runtime_warning_label.setVisible(True)
            return
        if engine == "local" and model_name in LOCAL_NEMOTRON_MODEL_SIZES:
            self.local_model_runtime_warning_label.setText(
                "Nemotron streams with a fixed 560 ms ONNX chunk. Auto tries "
                "DirectML, then falls back to CPU. Other latency profiles are "
                "not exposed by the published graph."
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
        self._update_language_availability()

    def _on_import_engine_changed(self, _index: int = 0) -> None:
        self._update_import_model_selector()
        self._update_import_engine_note()

    def _on_settings_tab_changed(self, _index: int) -> None:
        started_at = time.perf_counter()
        tab_name = self.tabs.tabText(_index) if 0 <= _index < self.tabs.count() else "-"
        first_visit = _index not in self._settings_perf_painted_tabs
        self._log_settings_timing(
            "tab_change",
            started_at,
            tab=tab_name,
            first_visit=first_visit,
        )
        QtCore.QTimer.singleShot(
            0,
            lambda index=_index, started_at=started_at: self._log_tab_paint_timing(
                index,
                started_at,
            ),
        )
        self._schedule_local_model_auto_refresh(
            delay_ms=_LOCAL_MODEL_AUTO_REFRESH_DELAY_MS
        )

    def _log_tab_paint_timing(self, index: int, started_at: float) -> None:
        if not hasattr(self, "tabs") or index != self.tabs.currentIndex():
            return
        tab_name = self.tabs.tabText(index) if 0 <= index < self.tabs.count() else "-"
        first_visit = index not in self._settings_perf_painted_tabs
        self._settings_perf_painted_tabs.add(index)
        self._log_settings_timing(
            "tab_paint",
            started_at,
            tab=tab_name,
            first_visit=first_visit,
        )

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
            "azure": "Azure LLM Speech",
            "funasr": "Fun-ASR (Alibaba)",
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
        tooltip: str = "",
    ) -> None:
        badge = self._provider_status_labels.get(provider)
        if badge is None:
            return
        badge.setText(text)
        badge.setToolTip(tooltip)
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
                tooltip="A new key is typed here and will be stored on Save.",
            )
            return

        if provider in self._provider_pending_clear:
            self._set_provider_status_badge(
                provider,
                "Will clear on Save",
                text_color="#b26a00",
                background="#fff3e0",
                border="#ffcc80",
                tooltip="The stored key will be deleted when settings are saved.",
            )
            return

        source = self._stored_key_source(provider)
        if source in {"keyring", "legacy-keyring"}:
            label = "Stored securely"
            tooltip = "Stored securely in Windows Credential Manager."
            if source == "legacy-keyring":
                label = "Secure (legacy)"
                tooltip = "Stored securely under the legacy keyring entry."
            self._set_provider_status_badge(
                provider,
                label,
                text_color="#1b5e20",
                background="#e8f5e9",
                border="#a5d6a7",
                tooltip=tooltip,
            )
            return

        if source == "insecure":
            self._set_provider_status_badge(
                provider,
                "Stored insecurely",
                text_color="#7a4a00",
                background="#fff3e0",
                border="#ffcc80",
                tooltip="Stored in the plain-text fallback file.",
            )
            return

        if source == "insecure-disabled":
            self._set_provider_status_badge(
                provider,
                "Insecure disabled",
                text_color="#7a4a00",
                background="#fff8e1",
                border="#ffe082",
                tooltip=(
                    "A plain-text fallback key exists, but insecure fallback "
                    "storage is currently disabled."
                ),
            )
            return

        self._set_provider_status_badge(
            provider,
            "Not configured",
            text_color="#555",
            background="#f2f2f2",
            border="#bbb",
            tooltip="No stored key is configured for this provider.",
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
        remote_providers = (
            "assemblyai",
            "groq",
            "openai",
            "deepgram",
            "elevenlabs",
            "azure",
            "funasr",
        )
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

        if engine == "azure":
            api_key = self._resolve_api_key("azure", self.azure_key_edit)
            if not api_key:
                return (
                    None,
                    "No API key entered. Enter a key above first.",
                )
            endpoint = self._resolve_azure_endpoint()
            if not endpoint:
                return (
                    None,
                    "No Azure endpoint entered. "
                    "Enter the resource endpoint above first.",
                )

            from .transcriber.azure_provider import AzureLlmSpeechTranscriber

            try:
                transcriber = AzureLlmSpeechTranscriber(
                    api_key=api_key,
                    endpoint=endpoint,
                    language_mode=str(
                        self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE
                    ),
                    model=self._remote_model_value_for_provider("azure"),
                )
            except Exception as exc:
                return None, str(exc)
            return transcriber.test_connection, None

        if engine == "funasr":
            api_key = self._resolve_api_key("funasr", self.funasr_key_edit)
            if not api_key:
                return (
                    None,
                    "No API key entered. Enter a key above first.",
                )

            from .transcriber.funasr_provider import FunAsrTranscriber

            transcriber = FunAsrTranscriber(
                api_key=api_key,
                language_mode=str(
                    self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE
                ),
                model=self._remote_model_value_for_provider("funasr"),
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

    def _resolve_azure_endpoint(self) -> str:
        """Return the typed Azure endpoint, or the stored one as fallback."""
        typed = self.azure_endpoint_edit.text().strip()
        if typed:
            return typed
        return str(getattr(self._loaded_settings, "azure_endpoint", "") or "").strip()

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
            self._style_provider_last_test_label(last_label, color=color)
            last_label.setText(
                f"Last test ({timestamp}): {marker} {provider_msg}"
            )

        if len(details) > 1:
            parts = []
            for provider in (
                "assemblyai",
                "groq",
                "openai",
                "deepgram",
                "elevenlabs",
                "azure",
                "funasr",
            ):
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
        self.keep_onnx_model_loaded_checkbox.setChecked(
            bool(getattr(settings, "keep_onnx_model_loaded", False))
        )
        self._select_combo_data(self.engine_combo, settings.engine)
        self._select_combo_data(self.mode_combo, settings.mode)
        self.streaming_full_final_check.setChecked(
            bool(getattr(settings, "streaming_full_final_transcript", False))
        )
        self._select_combo_data(
            self.concurrent_mode_combo,
            str(
                getattr(
                    settings,
                    "concurrent_transcription_mode",
                    DEFAULT_CONCURRENT_TRANSCRIPTION_MODE,
                )
            ),
        )
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
                "azure": getattr(
                    settings,
                    "azure_speech_model",
                    DEFAULT_AZURE_SPEECH_MODEL,
                ),
                "funasr": getattr(
                    settings,
                    "funasr_model",
                    DEFAULT_FUNASR_MODEL,
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
                "azure": getattr(
                    settings,
                    "azure_speech_model",
                    DEFAULT_AZURE_SPEECH_MODEL,
                ),
                "funasr": getattr(
                    settings,
                    "funasr_model",
                    DEFAULT_FUNASR_MODEL,
                ),
            }
        )
        if hasattr(self, "azure_endpoint_edit"):
            blocker = QtCore.QSignalBlocker(self.azure_endpoint_edit)
            self.azure_endpoint_edit.setText(
                getattr(settings, "azure_endpoint", DEFAULT_AZURE_ENDPOINT) or ""
            )
            del blocker
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
        self._refresh_benchmark_history_list()
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
        refresher = getattr(self._controller, "refresh_hotkey_registration", None)
        if callable(refresher):
            QtCore.QTimer.singleShot(500, refresher)

    def _effective_recordings_dir(self) -> str:
        text = self.recordings_dir_edit.text().strip()
        if text:
            return text
        return str(recordings_dir())

    def _recordings_file_dialog_dir(self) -> str:
        target = self._effective_recordings_dir()
        try:
            Path(target).mkdir(parents=True, exist_ok=True)
        except OSError:
            return str(recordings_dir())
        return target

    def _import_file_dialog_dir(self) -> str:
        selected = str(self._selected_import_file_path or "").strip()
        if selected:
            parent = Path(selected).parent
            if parent.is_dir():
                return str(parent)
        return self._recordings_file_dialog_dir()

    def _archived_recordings_dir_for_selection(self) -> str | None:
        if not self.save_all_recordings_checkbox.isChecked():
            return None
        return self._effective_recordings_dir()

    def _refresh_history_list(self) -> None:
        selected_entries = [
            item.data(QtCore.Qt.UserRole)
            for item in self.history_list.selectedItems()
        ]
        current_item = self.history_list.currentItem()
        current_entry = (
            current_item.data(QtCore.Qt.UserRole)
            if current_item is not None
            else None
        )
        scroll_value = self.history_list.verticalScrollBar().value()
        entries = self._history_store.recent_entries(self.history_max_spin.value())
        restored_selection = False
        restored_current_item: QtWidgets.QListWidgetItem | None = None

        self.history_list.setUpdatesEnabled(False)
        self.history_list.blockSignals(True)
        try:
            self.history_list.clear()
            for entry in entries:
                text = entry.text.strip().replace("\n", " ")
                preview = text[:70] + ("..." if len(text) > 70 else "")
                label = f"{entry.created_at} | {entry.engine}/{entry.model} | {preview}"
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.UserRole, entry)
                self._apply_compact_list_item_size(self.history_list, item)
                self.history_list.addItem(item)
                if any(entry == selected for selected in selected_entries):
                    item.setSelected(True)
                    restored_selection = True
                if current_entry is not None and entry == current_entry:
                    restored_current_item = item
        finally:
            self.history_list.blockSignals(False)
            self.history_list.setUpdatesEnabled(True)

        if restored_current_item is not None:
            self.history_list.setCurrentItem(
                restored_current_item,
                QtCore.QItemSelectionModel.NoUpdate,
            )
        restore_vertical_scrollbar(self.history_list, scroll_value)
        if restored_selection:
            self._on_history_item_selected()
            return

        self.history_detail.clear()
        self.history_copy_button.setEnabled(False)
        self.history_edit_button.setEnabled(False)
        self.history_delete_button.setEnabled(False)
        self._reset_history_copy_feedback()

    def _selected_history_items(self) -> list[QtWidgets.QListWidgetItem]:
        """Selected history items sorted by their row order in the list."""
        items = self.history_list.selectedItems()
        return sorted(items, key=self.history_list.row)

    def _selected_history_entries(self) -> list[TranscriptHistoryEntry]:
        entries: list[TranscriptHistoryEntry] = []
        for item in self._selected_history_items():
            entry = item.data(QtCore.Qt.UserRole)
            if entry is not None:
                entries.append(entry)
        return entries

    def _on_history_item_selected(self) -> None:
        entries = self._selected_history_entries()
        if not entries:
            self.history_copy_button.setEnabled(False)
            self.history_edit_button.setEnabled(False)
            self.history_delete_button.setEnabled(False)
            self.history_detail.clear()
            self._reset_history_copy_feedback()
            return
        if len(entries) == 1:
            text = str(getattr(entries[0], "text", "") or "")
            self.history_copy_button.setEnabled(bool(text))
            self.history_edit_button.setEnabled(bool(text))
            self.history_detail.setPlainText(text)
        else:
            has_text = any(str(getattr(e, "text", "") or "") for e in entries)
            self.history_copy_button.setEnabled(has_text)
            # Editing is only meaningful for a single entry.
            self.history_edit_button.setEnabled(False)
            self.history_detail.setPlainText(f"{len(entries)} entries selected.")
        self.history_delete_button.setEnabled(True)
        self._reset_history_copy_feedback()

    def _copy_selected_history(self) -> None:
        texts = [
            str(getattr(entry, "text", "") or "")
            for entry in self._selected_history_entries()
        ]
        texts = [text for text in texts if text]
        if not texts:
            return
        QtGui.QGuiApplication.clipboard().setText("\n\n".join(texts))
        self.history_copy_button.setText("Copied")
        set_button_feedback_state(self.history_copy_button, "success")
        self._history_copy_feedback_timer.start()

    def _edit_selected_history(self) -> None:
        entries = self._selected_history_entries()
        if len(entries) != 1:
            return
        entry = entries[0]
        if entry is None:
            return
        current_text = str(getattr(entry, "text", "") or "")
        next_text = TranscriptEditDialog.get_text(self, current_text)
        if next_text is None or next_text == current_text:
            return
        updated = self._history_store.update_entry_text(entry, next_text)
        if updated <= 0:
            self.import_result_label.setText("Selected history entry was not found.")
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            return
        self._refresh_history_list()

    def _delete_selected_history(self) -> None:
        entries = self._selected_history_entries()
        if not entries:
            return
        count = len(entries)
        prompt = (
            "Delete the selected transcription from history?"
            if count == 1
            else f"Delete {count} selected transcriptions from history?"
        )
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete history entries" if count > 1 else "Delete history entry",
            prompt,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        removed = self._history_store.delete_entries(entries)
        if removed <= 0:
            self.import_result_label.setText(
                "Selected history entries were not found."
            )
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            return
        self._refresh_history_list()

    def _reset_history_copy_feedback(self) -> None:
        self.history_copy_button.setText("Copy selected")
        set_button_feedback_state(self.history_copy_button, None)

    def _copy_import_result(self) -> None:
        text = self.import_result_text.toPlainText()
        if not text:
            return
        QtGui.QGuiApplication.clipboard().setText(text)
        self.import_copy_button.setText("Copied")
        set_button_feedback_state(self.import_copy_button, "success")
        self._import_copy_feedback_timer.start()

    def _reset_import_copy_feedback(self) -> None:
        self.import_copy_button.setText("Copy result")
        set_button_feedback_state(self.import_copy_button, None)

    def _set_selected_import_file(self, path: str) -> None:
        selected = str(path or "").strip()
        self._selected_import_file_path = selected
        if selected and Path(selected).is_file():
            self.import_selected_file_label.setText(f"Selected: {selected}")
            self.import_selected_file_label.setStyleSheet("color: #1b5e20;")
            self.import_start_button.setEnabled(True)
        elif selected:
            self.import_selected_file_label.setText(
                f"Selected file does not exist: {selected}"
            )
            self.import_selected_file_label.setStyleSheet("color: #b71c1c;")
            self.import_start_button.setEnabled(False)
        else:
            self.import_selected_file_label.setText("No file selected.")
            self.import_selected_file_label.setStyleSheet("color: #555;")
            self.import_start_button.setEnabled(False)

    def _choose_import_file(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select audio file",
            self._import_file_dialog_dir(),
            "Audio files (*.wav *.mp3 *.m4a *.flac *.ogg *.opus *.webm);;All files (*)",
        )
        if not path:
            return
        self._set_selected_import_file(path)

    def _select_last_recording_file(self) -> bool:
        path = self._last_recording_store.selectable_path(
            self._archived_recordings_dir_for_selection()
        )
        if path is None:
            self.import_result_label.setText(
                "No last recording is currently available."
            )
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            self.import_result_text.clear()
            self.import_copy_button.setEnabled(False)
            self._reset_import_copy_feedback()
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

    def _transcribe_selected_import_file(self) -> None:
        path = self._selected_import_file_path
        if not path:
            self.import_result_label.setText("Select a file first.")
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            return
        if not Path(path).is_file():
            self.import_result_label.setText(
                f"Selected file no longer exists: {path}"
            )
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            self.import_start_button.setEnabled(False)
            return
        self._start_import_transcription(path)

    def _start_import_transcription(self, path: str) -> None:
        self._import_progress_started_at = datetime.now()
        self._set_import_progress("Preparing transcription...")
        self.import_result_label.setStyleSheet("color: #555;")
        self.import_result_text.clear()
        self.import_copy_button.setEnabled(False)
        self._reset_import_copy_feedback()
        self.import_file_button.setEnabled(False)
        self.import_last_recording_button.setEnabled(False)
        self.import_start_button.setEnabled(False)
        self.import_engine_combo.setEnabled(False)
        self.import_model_combo.setEnabled(False)
        self._import_progress_timer.start()

        # Build settings on the GUI thread — widgets must not be accessed
        # from background threads.
        import_engine = str(
            self.import_engine_combo.currentData() or DEFAULT_ENGINE
        )
        import_model = str(self.import_model_combo.currentData() or "")
        if not self._import_engine_has_api_key(import_engine):
            self._import_progress_timer.stop()
            self._import_progress_message = ""
            self._import_progress_started_at = None
            detail = (
                "Failed: no API key configured for "
                f"{self._provider_label(import_engine)}."
            )
            if self._last_recording_store.is_managed_audio_path(path):
                detail = (
                    f"{detail} The last recording stays available. "
                    "Fix the provider settings and try again."
                )
            self.import_result_label.setText(detail)
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            self.import_result_text.setPlainText(detail)
            self.import_copy_button.setEnabled(bool(detail))
            self.import_file_button.setEnabled(True)
            self.import_last_recording_button.setEnabled(True)
            self.import_start_button.setEnabled(
                bool(self._selected_import_file_path)
            )
            self.import_engine_combo.setEnabled(True)
            self.import_model_combo.setEnabled(True)
            return
        settings = self._build_current_settings(
            engine_override=import_engine,
            model_override=import_model,
        )

        def _run() -> None:
            def _progress(text: str) -> None:
                self.import_transcription_progress.emit(str(text))

            try:
                _progress(f"Sending audio to {self._provider_label(import_engine)}...")
                ok, text = self._transcribe_import_file(
                    path,
                    settings,
                    progress_callback=_progress,
                )
            except Exception as exc:
                ok, text = False, str(exc)
            self.import_transcription_finished.emit(bool(ok), str(text))

        threading.Thread(
            target=_run,
            name="stt_app_import_file_transcription",
            daemon=True,
        ).start()

    def _set_import_progress(self, text: str) -> None:
        self._import_progress_message = str(text or "").strip()
        self._refresh_import_progress_label()

    def _refresh_import_progress_label(self) -> None:
        message = self._import_progress_message.strip()
        if not message:
            return
        if self._import_progress_started_at is None:
            self.import_result_label.setText(message)
            return
        elapsed = int(
            (datetime.now() - self._import_progress_started_at).total_seconds()
        )
        self.import_result_label.setText(f"{message} ({elapsed}s)")

    def _on_import_transcription_progress(self, text: str) -> None:
        self._set_import_progress(text)

    def _transcribe_import_file(
        self,
        path: str,
        settings: AppSettings,
        progress_callback: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        from .transcriber import create_transcriber

        if self._controller is not None:
            if progress_callback is not None:
                return self._controller.transcribe_audio_file(
                    path,
                    settings_override=settings,
                    progress_callback=progress_callback,
                )
            return self._controller.transcribe_audio_file(
                path,
                settings_override=settings,
            )

        transcriber = create_transcriber(settings, secret_store=self._secret_store)
        try:
            if progress_callback is not None:
                _set_transcriber_progress_callback(transcriber, progress_callback)
            text = transcriber.transcribe_batch(path)
        finally:
            if hasattr(transcriber, "close"):
                transcriber.close()
        return True, str(text or "").strip()

    def _finish_import_transcription(self, ok: bool, text: str) -> None:
        self._import_progress_timer.stop()
        self._import_progress_message = ""
        self._import_progress_started_at = None
        self.import_file_button.setEnabled(True)
        self.import_last_recording_button.setEnabled(True)
        self.import_start_button.setEnabled(bool(self._selected_import_file_path))
        self.import_engine_combo.setEnabled(True)
        self.import_model_combo.setEnabled(True)
        if ok:
            self.import_result_label.setText("Transcription finished.")
            self.import_result_label.setStyleSheet("color: #1b5e20;")
            self.import_result_text.setPlainText(text)
            self.import_copy_button.setEnabled(bool(text))
            self._reset_import_copy_feedback()
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
        self.import_result_text.setPlainText(detail)
        self.import_copy_button.setEnabled(bool(detail))
        self._reset_import_copy_feedback()

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

    def _stored_provider_key_states(self) -> dict[str, bool]:
        states: dict[str, bool] = {}
        key_getter = getattr(self._secret_store, "get_api_key", None)
        for provider in _REMOTE_API_KEY_PROVIDERS:
            if not callable(key_getter):
                states[provider] = False
                continue
            try:
                states[provider] = bool(key_getter(provider))
            except Exception:
                states[provider] = False
        return states

    def _persist_provider_key_changes(self) -> tuple[dict[str, bool], list[str], bool]:
        self._apply_secret_store_options()
        errors: list[str] = []
        changed = False
        pending_clear = set(self._provider_pending_clear)

        for provider in _REMOTE_API_KEY_PROVIDERS:
            key_field = self._provider_key_edits.get(provider)
            if key_field is None:
                continue
            label = self._provider_label(provider)
            value = key_field.text().strip()
            if value:
                changed = True
                try:
                    self._secret_store.set_api_key(provider, value)
                    key_field.clear()
                    self._provider_pending_clear.discard(provider)
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
            elif provider in pending_clear:
                changed = True
                try:
                    self._secret_store.delete_api_key(provider)
                    self._provider_pending_clear.discard(provider)
                except Exception as exc:
                    errors.append(f"{label} delete: {exc}")

        states = self._stored_provider_key_states()
        self._refresh_provider_key_statuses()
        self._update_import_engine_note()
        return states, errors, changed

    def _show_key_storage_result(self, errors: list[str], changed: bool) -> None:
        if errors:
            self.key_storage_status_label.setStyleSheet("color: #b71c1c;")
            self.key_storage_status_label.setText(
                "Could not store some API keys in Credential Manager. "
                "Enable insecure fallback storage or retry. "
                + " | ".join(errors)
            )
            return
        if changed:
            self.key_storage_status_label.setStyleSheet("color: #1b5e20;")
            self.key_storage_status_label.setText("API key storage updated.")
        else:
            self.key_storage_status_label.setStyleSheet("color: #555;")
            self.key_storage_status_label.setText("No API key changes to save.")

    def _save_api_keys_only(self) -> None:
        key_states, key_storage_errors, changed = self._persist_provider_key_changes()
        metadata_changed = (
            self.insecure_key_storage_checkbox.isChecked()
            != bool(getattr(self._loaded_settings, "allow_insecure_key_storage", False))
        )
        self._show_key_storage_result(key_storage_errors, changed or metadata_changed)
        if key_storage_errors:
            return

        updated = replace(
            self._loaded_settings,
            allow_insecure_key_storage=self.insecure_key_storage_checkbox.isChecked(),
            has_openai_key=key_states["openai"],
            has_deepgram_key=key_states["deepgram"],
            has_assemblyai_key=key_states["assemblyai"],
            has_groq_key=key_states["groq"],
            has_elevenlabs_key=key_states["elevenlabs"],
            has_azure_key=key_states["azure"],
            has_funasr_key=key_states["funasr"],
            azure_endpoint=self.azure_endpoint_edit.text().strip(),
        )
        try:
            self._settings_store.save(updated)
        except Exception as exc:
            self.key_storage_status_label.setStyleSheet("color: #b71c1c;")
            self.key_storage_status_label.setText(
                f"API keys were saved, but key metadata could not be persisted: {exc}"
            )
            return
        self._loaded_settings = updated

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
            keep_onnx_model_loaded=self.keep_onnx_model_loaded_checkbox.isChecked(),
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
            streaming_full_final_transcript=(
                self.streaming_full_final_check.isChecked()
            ),
            concurrent_transcription_mode=str(
                self.concurrent_mode_combo.currentData()
                or DEFAULT_CONCURRENT_TRANSCRIPTION_MODE
            ),
            paste_mode=str(
                self.paste_mode_combo.currentData() or DEFAULT_PASTE_MODE
            ),
            has_openai_key=self._loaded_settings.has_openai_key,
            has_deepgram_key=self._loaded_settings.has_deepgram_key,
            has_assemblyai_key=self._loaded_settings.has_assemblyai_key,
            has_groq_key=self._loaded_settings.has_groq_key,
            has_elevenlabs_key=getattr(self._loaded_settings, "has_elevenlabs_key", False),
            has_azure_key=getattr(self._loaded_settings, "has_azure_key", False),
            has_funasr_key=getattr(self._loaded_settings, "has_funasr_key", False),
            groq_model=self._remote_model_value_for_provider("groq"),
            openai_model=self._remote_model_value_for_provider("openai"),
            deepgram_model=self._remote_model_value_for_provider("deepgram"),
            assemblyai_model=self._remote_model_value_for_provider("assemblyai"),
            elevenlabs_model=self._remote_model_value_for_provider("elevenlabs"),
            azure_speech_model=self._remote_model_value_for_provider("azure"),
            azure_endpoint=self.azure_endpoint_edit.text().strip(),
            funasr_model=self._remote_model_value_for_provider("funasr"),
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

        key_states, key_storage_errors, key_storage_changed = (
            self._persist_provider_key_changes()
        )
        if key_storage_errors or key_storage_changed:
            self._show_key_storage_result(key_storage_errors, key_storage_changed)

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
            keep_onnx_model_loaded=self.keep_onnx_model_loaded_checkbox.isChecked(),
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
            streaming_full_final_transcript=(
                self.streaming_full_final_check.isChecked()
            ),
            concurrent_transcription_mode=str(
                self.concurrent_mode_combo.currentData()
                or DEFAULT_CONCURRENT_TRANSCRIPTION_MODE
            ),
            paste_mode=str(
                self.paste_mode_combo.currentData() or DEFAULT_PASTE_MODE
            ),
            has_openai_key=key_states["openai"],
            has_deepgram_key=key_states["deepgram"],
            has_assemblyai_key=key_states["assemblyai"],
            has_groq_key=key_states["groq"],
            has_elevenlabs_key=key_states["elevenlabs"],
            has_azure_key=key_states["azure"],
            has_funasr_key=key_states["funasr"],
            groq_model=self._remote_model_value_for_provider("groq"),
            openai_model=self._remote_model_value_for_provider("openai"),
            deepgram_model=self._remote_model_value_for_provider("deepgram"),
            assemblyai_model=self._remote_model_value_for_provider("assemblyai"),
            elevenlabs_model=self._remote_model_value_for_provider("elevenlabs"),
            azure_speech_model=self._remote_model_value_for_provider("azure"),
            azure_endpoint=self.azure_endpoint_edit.text().strip(),
            funasr_model=self._remote_model_value_for_provider("funasr"),
        )

        if history_limit_changed and requested_history_limit > 0:
            self._history_store.apply_max_items(requested_history_limit)
        self._settings_store.save(settings)
        self._loaded_settings = settings
        self._save_status_label.setText("\u2713 Settings saved")
        self._save_status_timer.start()
        self.settings_changed.emit()


def _benchmark_status_text(status: str) -> str:
    labels = {
        "running": "Running",
        "completed": "Completed",
        "completed_with_errors": "Completed with errors",
        "canceled": "Canceled",
        "failed": "Failed",
    }
    return labels.get(str(status or "").strip().lower(), str(status or ""))


def _benchmark_history_label(entry: BenchmarkHistoryEntry) -> str:
    models = ", ".join(entry.options.model_names[:3])
    if len(entry.options.model_names) > 3:
        models = f"{models}, ..."
    fastest = min(
        (case for case in entry.cases if case.error is None and case.runs),
        key=lambda case: case.avg_seconds,
        default=None,
    )
    speed = ""
    if fastest is not None:
        speed = f" | fastest {fastest.model} {_format_seconds(fastest.avg_seconds)}"
    status = _benchmark_status_text(entry.status)
    return f"{entry.created_at} | {status} | {models or 'no models'}{speed}"


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
