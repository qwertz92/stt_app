"""Settings dialog facade.

The dialog was split from one ~6.4k-line module into cohesive mixin
siblings (settings_dialog_*.py). This module composes them into the public
SettingsDialog and keeps the dialog lifecycle/shared-UI code. Names re-
exported here stay importable/patchable as stt_app.settings_dialog.<name>.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .app_icon import load_app_icon
from .benchmark_environment import BenchmarkEnvironment
from .benchmark_history import (
    BenchmarkHistoryEntry,
    BenchmarkHistoryStore,
    BenchmarkOptions,
)
from .config import (
    APP_LOGGER_NAME,
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_AZURE_SPEECH_MODEL,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_FUNASR_MODEL,
)
from .last_recording_store import LastRecordingStore
from .local_benchmark import BenchmarkCase
# Runs the benchmark out-of-process so heavy model loading/inference never
# freezes the Qt UI; same signature/return as the pure function, and the name
# is kept so ``stt_app.settings_dialog.run_benchmark_cases`` stays the seam
# tests patch.
from .benchmark_process import run_benchmark_cases
from .local_model_download import start_model_download_process
from .local_model_inventory_store import LocalModelInventoryStore
from .local_model_scan import scan_cached_models_out_of_process as _scan_cached_models
from .logger import AppLogger
from .model_download_progress import ModelDownloadSpeedTracker
from .provider_connection_test_store import ProviderConnectionTestStore
from .secret_store import SecretStore
from .settings_dialog_benchmark import _BenchmarkMixin
from .settings_dialog_general import _GeneralTabMixin
from .settings_dialog_helpers import (
    _ACTION_ROW_SPACING_PX,
    _COMPACT_LIST_ITEM_STYLESHEET,
    _COMPACT_LIST_ROW_EXTRA_PX,
    _COMPACT_TABLE_ROW_EXTRA_PX,
    _DEFAULT_SETTINGS_DIALOG_SIZE,
    _DIALOG_SCREEN_MARGIN,
    _emit_background_signal,
    _FIELD_HINT_MIN_WIDTH_PX,
    _GENERAL_FORM_LABEL_EXTRA_PX,
    _LOCAL_MODEL_AUTO_REFRESH_DELAY_MS,
    _LOCAL_MODEL_SCAN_SESSION_CACHE,
    _LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS,
    _PROVIDER_STATUS_BADGE_HORIZONTAL_PADDING_PX,
    _PROVIDER_STATUS_BADGE_TEXTS,
    _REMOTE_PROVIDER_LABEL_EXTRA_PX,
    _app_hotkey_to_qt_hotkey_text,
    _hotkey_token_set,
    _hotkeys_conflict,
    _qt_hotkey_sequence_to_app_hotkey,
    _qt_hotkey_text_to_app_hotkey,
)
from .settings_dialog_history import _HistoryTabMixin
from .settings_dialog_import import _ImportTabMixin
from .settings_dialog_local import _LocalModelsMixin
from .settings_dialog_persistence import _PersistenceMixin
from .settings_dialog_remote import _RemoteProvidersMixin
from .settings_store import SettingsStore
from .transcriber.local_faster_whisper import (
    cleanup_incomplete_model_download,
    delete_cached_model,
    estimate_cached_model_bytes,
)
from .transcript_edit_dialog import TranscriptEditDialog
from .transcript_history import (
    HistoryStorageSignature,
    TranscriptHistoryEntry,
    TranscriptHistoryStore,
)
from .ui_feedback import BUTTON_FEEDBACK_STYLESHEET, reserve_button_width_for_texts
from .update_checker import UpdateCheckResult, check_for_updates
from .update_ui import show_update_available_dialog, show_update_status_dialog

if TYPE_CHECKING:
    from .controller import DictationController

__all__ = [
    'SettingsDialog',
    'TranscriptEditDialog',
    '_scan_cached_models',
    'start_model_download_process',
    'cleanup_incomplete_model_download',
    'delete_cached_model',
    'estimate_cached_model_bytes',
    'run_benchmark_cases',
    '_DEFAULT_SETTINGS_DIALOG_SIZE',
    '_LOCAL_MODEL_SCAN_SESSION_CACHE',
    '_LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS',
    '_PROVIDER_STATUS_BADGE_TEXTS',
    '_app_hotkey_to_qt_hotkey_text',
    '_hotkeys_conflict',
    '_qt_hotkey_text_to_app_hotkey',
    '_qt_hotkey_sequence_to_app_hotkey',
    '_hotkey_token_set',
    'threading',
    'time',
]


class SettingsDialog(
    _GeneralTabMixin,
    _LocalModelsMixin,
    _BenchmarkMixin,
    _RemoteProvidersMixin,
    _HistoryTabMixin,
    _ImportTabMixin,
    _PersistenceMixin,
    QtWidgets.QDialog,
):
    connection_test_finished = QtCore.Signal(int, bool, str)
    import_transcription_finished = QtCore.Signal(bool, str)
    import_transcription_progress = QtCore.Signal(str)
    local_model_scan_finished = QtCore.Signal(int, str, object)
    local_model_download_progress = QtCore.Signal(int, str)
    local_model_download_finished = QtCore.Signal(int, bool, str)
    update_check_finished = QtCore.Signal(object)
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
        provider_connection_test_store: ProviderConnectionTestStore | None = None,
        update_check_runner: Callable[[], UpdateCheckResult] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_store = settings_store
        self._secret_store = secret_store
        self._app_logger = app_logger
        self._controller = controller
        self._history_store = TranscriptHistoryStore()
        self._history_entries: list[TranscriptHistoryEntry] = []
        self._history_reload_signature: tuple[
            HistoryStorageSignature, int, str
        ] | None = None
        self._benchmark_history_store = BenchmarkHistoryStore()
        self._last_recording_store = last_recording_store or LastRecordingStore()
        self._local_model_inventory_store = local_model_inventory_store
        self._provider_connection_test_store = (
            provider_connection_test_store or ProviderConnectionTestStore()
        )
        self._update_check_runner = update_check_runner or check_for_updates
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
        self._active_update_check_thread: threading.Thread | None = None
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
        self._shutdown_started = False

        self.setWindowTitle("Dictation Settings")
        self.setWindowIcon(load_app_icon())
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
        self.update_check_finished.connect(self._on_update_check_finished)
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
        self.check_updates_button = QtWidgets.QPushButton("Check for updates")
        self.check_updates_button.clicked.connect(self._check_for_updates)

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
        buttons.addWidget(self.check_updates_button)
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

    def _set_bottom_status(self, text: str, color: str = "#2e7d32") -> None:
        self._save_status_label.setStyleSheet(
            f"color: {color}; font-weight: bold;"
        )
        self._save_status_label.setText(text)

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
    def _style_field_hint_label(label: QtWidgets.QLabel) -> None:
        """Style a note that belongs directly to the control above it.

        Field hints intentionally have no internal bottom padding. Their
        wrapper owns the small control-to-hint gap, while the form layout owns
        the larger gap before the next field. This makes the association
        visually unambiguous without relying on platform stylesheet defaults.
        """
        label.setProperty("fieldHint", True)
        # QLabel's default word-wrapped size hint is intentionally narrow and
        # tall. Giving form hints a realistic readable width prevents the form
        # from reserving phantom lines that appear as unrelated blank space.
        label.setMinimumWidth(_FIELD_HINT_MIN_WIDTH_PX)
        label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        label.setStyleSheet("color: #555; font-size: 11px; padding: 0;")

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
        wrapper.setProperty("fieldWithHint", True)
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
            self._schedule_owned_callback(
                25,
                lambda: self._prewarm_settings_tabs((self._local_tab_index,)),
            )
        if self._benchmark_tab_index not in self._settings_perf_prewarmed_tab_indexes:
            self._schedule_owned_callback(
                800,
                lambda: self._prewarm_settings_tabs((self._benchmark_tab_index,)),
            )

    def _schedule_owned_callback(
        self,
        delay_ms: int,
        callback: Callable[[], None],
    ) -> None:
        """Run a delayed callback only while this dialog still exists."""
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)

        def invoke() -> None:
            try:
                callback()
            finally:
                timer.deleteLater()

        timer.timeout.connect(invoke)
        timer.start(max(0, int(delay_ms)))

    def prepare_for_first_show(self) -> None:
        self._prewarm_settings_tabs(
            (self._local_tab_index,),
            require_visible=False,
        )
        self._schedule_owned_callback(
            800,
            lambda: self._prewarm_settings_tabs(
                (self._benchmark_tab_index,),
                require_visible=False,
            ),
        )

    def reload_from_store(self) -> None:
        started_at = time.perf_counter()
        if self._background_work_active():
            self._log_settings_timing(
                "reload_from_store_deferred_busy",
                started_at,
            )
            return
        self._loaded_settings = self._settings_store.load()
        self._discard_unsaved_provider_key_edits()
        self._populate(self._loaded_settings)
        self._log_settings_timing("reload_from_store", started_at)

    def _background_work_active(self) -> bool:
        return bool(
            self._active_local_model_scan_thread is not None
            or self._local_model_download_is_running()
            or self._active_benchmark_thread is not None
            or self._active_connection_test_thread is not None
            or self._active_update_check_thread is not None
            or self._import_progress_started_at is not None
        )

    def _discard_unsaved_provider_key_edits(self) -> None:
        self._provider_pending_clear.clear()
        for field in self._provider_key_edits.values():
            blocker = QtCore.QSignalBlocker(field)
            field.clear()
            del blocker

    def shutdown(self) -> None:
        """Stop dialog-owned child-process work before the application exits."""
        if self._shutdown_started:
            return
        self._shutdown_started = True

        for timer in self.findChildren(QtCore.QTimer):
            timer.stop()

        if self._local_model_download_is_running():
            self._cancel_local_model_downloads()
        if self._benchmark_cancel_event is not None:
            self._benchmark_cancel_event.set()

        self._hide_benchmark_window()

        # The cancellation paths above terminate the model-download process
        # directly and make the benchmark runner terminate its process tree on
        # its next short poll. Give those daemon threads a bounded opportunity
        # to finish their cleanup before Python tears the process down.
        current_thread = threading.current_thread()
        for thread in (
            self._active_local_model_download_thread,
            self._active_benchmark_thread,
        ):
            if thread is None or thread is current_thread:
                continue
            join = getattr(thread, "join", None)
            if callable(join):
                join(timeout=2.5)

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

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        started_at = time.perf_counter()
        super().showEvent(event)
        self._apply_initial_dialog_size()
        self._log_settings_timing("show_event", started_at)
        if not self._settings_perf_logged_first_show:
            self._settings_perf_logged_first_show = True
            self._schedule_owned_callback(
                0,
                lambda started_at=started_at: self._log_settings_timing(
                    "first_show_paint",
                    started_at,
                ),
            )
        self._schedule_settings_tab_prewarm()

    def _hide_benchmark_window(self) -> None:
        window = getattr(self, "benchmark_window", None)
        if window is not None:
            window.hide()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        # QDialog.reject()/done() hide the dialog without sending closeEvent.
        # The benchmark window has Qt.Window and therefore must be hidden
        # explicitly for every dismissal path, including the Close button.
        self._hide_benchmark_window()
        super().hideEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._hide_benchmark_window()
        super().closeEvent(event)

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
        self._schedule_owned_callback(
            0,
            lambda index=_index, started_at=started_at: self._log_tab_paint_timing(
                index,
                started_at,
            ),
        )
        self._schedule_local_model_auto_refresh(
            delay_ms=_LOCAL_MODEL_AUTO_REFRESH_DELAY_MS
        )
        if _index == getattr(self, "_history_tab_index", None):
            self._refresh_history_list(force=True)

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

    def _copy_diagnostics(self) -> None:
        text = self._app_logger.diagnostics_text()
        clipboard = QtGui.QGuiApplication.clipboard()
        clipboard.setText(text)

    def _check_for_updates(self) -> None:
        if self._active_update_check_thread is not None:
            return
        self.check_updates_button.setEnabled(False)
        self._set_bottom_status("Checking for updates...", "#0d47a1")

        def _run() -> None:
            try:
                result = self._update_check_runner()
            except Exception as exc:
                result = UpdateCheckResult(
                    current_version="",
                    error=f"Update check failed: {exc}",
                )
            _emit_background_signal(self, "update_check_finished", result)

        thread = threading.Thread(
            target=_run,
            name="stt_app_settings_update_check",
            daemon=True,
        )
        self._active_update_check_thread = thread
        thread.start()

    @QtCore.Slot(object)
    def _on_update_check_finished(self, result: object) -> None:
        self._active_update_check_thread = None
        self.check_updates_button.setEnabled(True)
        if not isinstance(result, UpdateCheckResult):
            result = UpdateCheckResult(
                current_version="",
                error="Update check returned an unexpected result.",
            )

        if result.update_available:
            self._set_bottom_status(f"Update {result.latest_tag} available", "#0d47a1")
            show_update_available_dialog(result, parent=self)
            return

        if result.error:
            self._set_bottom_status("Update check failed", "#b71c1c")
            show_update_status_dialog(
                parent=self,
                title="Update check failed",
                text=result.error,
                icon=QtWidgets.QMessageBox.Warning,
            )
            return
        self._set_bottom_status("Already up to date")
        self._save_status_timer.start()
