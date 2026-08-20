"""Settings dialog: local mixin (split from settings_dialog.py)."""
from __future__ import annotations

import logging
import threading
import time

from PySide6 import QtCore, QtGui, QtWidgets

from .config import (
    DOC_MODELS_PATH,
    LOCAL_ENGLISH_ONLY_MODELS,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_ONNX_MODEL_RUNTIME_LABELS,
    LOCAL_WEBGPU_MODEL_SIZES,
    VALID_MODEL_SIZES,
)
from .local_model_download import (
    model_download_process_error,
    terminate_model_download_process,
)
from .model_download_coordinator import (
    ACQUIRE_JOINED,
    ModelDownloadCanceled,
    model_download_coordinator,
)
from .model_download_progress import format_model_download_progress
from .settings_dialog_helpers import (
    _INLINE_FIELD_BUTTON_SPACING_PX,
    _LOCAL_MODEL_SCAN_SESSION_CACHE,
    _LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS,
    _emit_background_signal,
)
from .dialog_style import make_label_selectable
from .ui_feedback import restore_vertical_scrollbar

_logger = logging.getLogger(__name__)


def _facade():
    """Return the settings_dialog facade module.

    Imported lazily so this mixin module has no import-time dependency on the
    facade (which imports this module), and so the monkeypatched
    ``stt_app.settings_dialog.<name>`` functions still resolve at call time.
    """
    import stt_app.settings_dialog as facade

    return facade


class _LocalModelsMixin:
    def _build_local_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        active_model_note = QtWidgets.QLabel(
            "The active local model is selected on the General tab (Engine && Mode)."
        )
        active_model_note.setWordWrap(True)
        self._style_note_label(active_model_note)
        layout.addWidget(active_model_note)

        form = QtWidgets.QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)

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
            "Keeps the last ONNX runtime process alive so every following "
            "dictation skips the model load. Turn it off if RAM or GPU memory "
            "pressure matters more than the delay before each transcription."
        )
        keep_onnx_note = QtWidgets.QLabel(
            "On by default. Without it every dictation reloads the model; "
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
        make_label_selectable(self.local_models_scan_status_label)
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
        # Explorer-style selection (Shift for ranges, Ctrl for toggles), like
        # every other multi-select list in the app.
        self.local_models_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection
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
        make_label_selectable(self.local_models_action_label)
        self.local_models_action_label.setWordWrap(True)
        # Reserve the space: this label sits below the stretching model list,
        # so a status message that wraps to a second line would otherwise
        # shorten the list mid-scan/mid-download.
        self._reserve_dynamic_hint_height(self.local_models_action_label)
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

        # The controller downloads models on its own (preload after a save, or
        # lazily on first use) without going through this tab's queue, so the
        # list has to be told to look. Polling is tied to the tab actually being
        # in front: a timer that runs for the dialog's whole lifetime keeps
        # firing on every dialog anything ever creates, which is both wasted
        # work and a way to have background refreshes outlive their usefulness.
        self._preload_download_seen: str | None = self._preload_downloading_model()
        self._preload_download_watch_timer = QtCore.QTimer(self)
        self._preload_download_watch_timer.setInterval(1000)
        self._preload_download_watch_timer.timeout.connect(
            self._poll_preload_download_state
        )
        self.tabs.currentChanged.connect(self._sync_preload_download_watch)
        self._sync_preload_download_watch(self.tabs.currentIndex())

    def _sync_preload_download_watch(self, index: int) -> None:
        timer = getattr(self, "_preload_download_watch_timer", None)
        if timer is None:
            return
        if index == self._local_tab_index:
            self._preload_download_seen = self._preload_downloading_model()
            timer.start()
        else:
            timer.stop()

    def _poll_preload_download_state(self) -> None:
        current = self._preload_downloading_model()
        changed = current != self._preload_download_seen
        self._preload_download_seen = current
        if not hasattr(self, "local_models_list"):
            return
        if changed:
            self._refresh_local_models_list()
            self._update_local_model_actions()
        # The bar has to be driven from here as well, not only from this tab's
        # own queue: a download the controller started is the same download to
        # the user, and without this its progress branch is unreachable.
        if self._local_model_download_is_running():
            return
        if current:
            self._refresh_local_model_download_progress()
        else:
            # Not gated on `changed`: leaving and re-entering the tab re-seeds
            # the seen value, so a gated hide could never fire and the bar stayed
            # frozen at the last percentage forever.
            self._hide_local_model_download_progress()

    def _hide_local_model_download_progress(self) -> None:
        # Track the state rather than asking the widget: Qt reports
        # isVisible() False for any child of a hidden dialog, and this dialog
        # persists hidden for the app lifetime, so a download that ends while
        # Settings is closed would never get its bar hidden.
        if not getattr(self, "_local_model_download_bar_shown", False):
            return
        self._local_model_download_bar_shown = False
        if hasattr(self, "local_model_download_progress_bar"):
            self.local_model_download_progress_bar.setVisible(False)
        self._local_model_download_speed_tracker.reset()
        # Clear the stale "Downloading ... 27% ... measuring speed" line too;
        # hiding only the bar left it on screen indefinitely.
        if hasattr(self, "local_models_action_label"):
            self.local_models_action_label.setStyleSheet("color: #555;")
            self.local_models_action_label.setText("")

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
                cached = _facade()._scan_cached_models(model_dir)
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

    def _discard_queued_downloads_locked(self) -> None:
        """Drop every still-queued entry and the interest it registered.

        Interest is claimed at enqueue so the preload path leaves a queued
        model's partial files alone. Every path that abandons the queue has to
        give that claim back, or `has_explicit_interest` stays true for the rest
        of the process and those partials are never cleaned up again. Callers
        hold `_local_model_download_lock`.
        """
        coordinator = model_download_coordinator()
        for queued_name, queued_dir in self._local_model_download_queue:
            coordinator.drop_explicit_interest(queued_name, queued_dir)
        self._local_model_download_queue.clear()

    def _local_model_download_snapshot(
        self,
    ) -> tuple[tuple[str, str] | None, list[tuple[str, str]], bool]:
        with self._local_model_download_lock:
            # `claimed` (popped, waiting for the slot) is deliberately folded
            # in for the list, the pending set and the duplicate check, which
            # all need to see it. The progress bar must NOT use this: it
            # measures directory growth, and a merely-claimed model is not
            # being written to. It reads `_local_model_download_active`.
            return (
                self._local_model_download_active
                # getattr: this mixin's attributes live on the composed dialog,
                # and the visibility tests drive it with a light stub.
                or getattr(self, "_local_model_download_claimed", None),
                list(self._local_model_download_queue),
                self._local_model_download_worker_running,
            )

    def _local_model_download_is_running(self) -> bool:
        _active, _queued, running = self._local_model_download_snapshot()
        return running

    def _preload_downloading_model(self) -> str | None:
        """Model the controller's preload is downloading into *this* Model Dir.

        A download filling a different directory does not satisfy the one this
        tab is configured for, so it must not suppress it either.
        """
        getter = getattr(self._controller, "preload_downloading_model", None)
        if not callable(getter):
            return None
        try:
            answer = getter()
        except Exception:
            return None
        if not isinstance(answer, tuple) or len(answer) != 2:
            return None
        name, model_dir = answer
        # Test doubles hand back stand-ins rather than a name; only a real
        # string can match a model in the list.
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(model_dir, str):
            return None
        if self._local_model_cache_key(model_dir) != self._local_model_cache_key(
            self.model_dir_edit.text() if hasattr(self, "model_dir_edit") else ""
        ):
            return None
        return name

    def _local_model_download_state(self, model_name: str) -> str:
        active, queued, _running = self._local_model_download_snapshot()
        if active is not None and active[0] == model_name:
            return "active"
        if any(name == model_name for name, _model_dir in queued):
            return "queued"
        # A download the controller started for the selected model is just as
        # real as one from this tab; listing it as "Not downloaded" while bytes
        # arrive is what made two downloads race for the same link.
        if self._preload_downloading_model() == model_name:
            return "active"
        return ""

    def _local_model_download_pending_names(self) -> set[str]:
        active, queued, _running = self._local_model_download_snapshot()
        pending = {name for name, _model_dir in queued}
        if active is not None:
            pending.add(active[0])
        # Treat a controller-side preload download as pending too, so this tab
        # neither offers to start a second copy of it nor reports it missing.
        preloading = self._preload_downloading_model()
        if preloading:
            pending.add(preloading)
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
            claimed = getattr(self, "_local_model_download_claimed", None)
            if claimed is not None:
                pending.add(claimed[0])
            pending.update(self._local_model_download_completed_names)
            for model_name in model_names:
                if model_name in pending:
                    continue
                self._local_model_download_queue.append((model_name, model_dir))
                # Claim interest now, not when this entry reaches the slot: the
                # preload path deletes partial files on cancel, and a queued
                # user request is going to resume from them.
                model_download_coordinator().register_explicit_interest(
                    model_name, model_dir
                )
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
            self._discard_queued_downloads_locked()
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
        # Claim the one process-wide download slot first. The preload path may
        # already be fetching this model because the user selected it and
        # pressed Save; starting a second worker against the same cache
        # directory is what made this queue sit at 0% forever.
        coordinator = model_download_coordinator()
        # One finally for every exit. Returning early from the cancel branch
        # used to leave `_claimed` set, which made the tab show the model as
        # downloading forever and refuse to queue it again for the rest of the
        # session.
        acquired = False
        result: tuple[str, str, int, int] = ("failed", "", 0, 0)
        try:
            try:
                outcome = coordinator.acquire(
                    model_name,
                    model_dir,
                    explicit=True,
                    cancel_check=self._local_model_download_cancel_event.is_set,
                    # The enqueue already claimed it. Releasing and re-taking
                    # would leave a window at zero interest in which the preload
                    # cancel path could delete the partials this resumes from.
                    interest_already_registered=True,
                )
            except ModelDownloadCanceled:
                result = ("canceled", "", 0, 0)
                return result
            if outcome == ACQUIRE_JOINED:
                # The preload path finished this exact model while we waited.
                result = ("success", "", 0, 0)
                return result

            acquired = True
            with self._local_model_download_lock:
                self._local_model_download_active = (model_name, model_dir)
            result = self._run_download_worker(model_name, model_dir)
            return result
        finally:
            with self._local_model_download_lock:
                if self._local_model_download_active == (model_name, model_dir):
                    self._local_model_download_active = None
                if self._local_model_download_claimed == (model_name, model_dir):
                    self._local_model_download_claimed = None
            if acquired:
                coordinator.release(
                    model_name, model_dir, succeeded=result[0] == "success"
                )
            else:
                # Never held the slot, but the enqueue-time interest is ours.
                coordinator.drop_explicit_interest(model_name, model_dir)

    def _run_download_worker(
        self,
        model_name: str,
        model_dir: str,
    ) -> tuple[str, str, int, int]:
        try:
            process = _facade().start_model_download_process(model_name, model_dir)
        except Exception as exc:
            return "failed", str(exc), 0, 0

        with self._local_model_download_lock:
            self._local_model_download_process = process
        try:
            while process.poll() is None:
                if self._local_model_download_cancel_event.wait(timeout=0.1):
                    terminate_model_download_process(process)
                    model_download_process_error(process)
                    removed_files, removed_bytes = _facade().cleanup_incomplete_model_download(
                        model_name,
                        model_dir,
                    )
                    return "canceled", "", removed_files, removed_bytes

            detail = model_download_process_error(process)
            if process.returncode == 0:
                return "success", "", 0, 0
            if self._local_model_download_cancel_event.is_set():
                removed_files, removed_bytes = _facade().cleanup_incomplete_model_download(
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
        try:
            self._drive_local_model_download_queue(worker_token)
        except BaseException:
            # Without this the thread dies holding the queue: interest stays
            # registered (so the preload path never cleans up those partials
            # again), `_worker_running` stays True forever, and every later
            # Download click appends to a queue nothing will ever run while the
            # Refresh/Delete/Model-Dir controls stay disabled.
            _logger.exception("Local model download queue crashed")
            with self._local_model_download_lock:
                self._discard_queued_downloads_locked()
                self._local_model_download_active = None
                self._local_model_download_claimed = None
                self._local_model_download_worker_running = False
            _emit_background_signal(
                self,
                "local_model_download_finished",
                worker_token,
                False,
                "Download failed: the download queue stopped unexpectedly.",
            )
            raise

    def _drive_local_model_download_queue(self, worker_token: int) -> None:
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
                    self._discard_queued_downloads_locked()
                    self._local_model_download_active = None
                    self._local_model_download_claimed = None
                    self._local_model_download_worker_running = False
                    break
                model_name, model_dir = self._local_model_download_queue.pop(0)
                # Claimed but not yet downloading: the list, the pending set and
                # the duplicate check must still see it, or pressing Download
                # again queues the same model twice.
                self._local_model_download_claimed = (model_name, model_dir)
                queued_count = len(self._local_model_download_queue)

            # Only claim to be downloading this model once the slot is ours.
            # Publishing it while still queued behind another download made the
            # progress bar measure a directory nothing was writing to, which
            # looked exactly like the 0%-forever bug this queue was fixed for.
            if model_download_coordinator().active() is not None:
                _emit_background_signal(
                    self,
                    "local_model_download_progress",
                    worker_token,
                    f"Waiting for the current download to finish, then "
                    f"'{model_name}'. {queued_count} queued.",
                )
            else:
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
                    self._discard_queued_downloads_locked()
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
        # Only let the progress refresh speak when this tab's worker actually
        # holds the slot. While it is queued, the message just set above is the
        # accurate one and the refresh would replace it with another download's
        # percentage.
        if self._local_model_download_active is not None:
            self._refresh_local_model_download_progress()
        self._local_model_download_progress_timer.start()
        self._update_local_model_actions()

    def _refresh_local_model_download_progress(self) -> None:
        if not hasattr(self, "local_model_download_progress_bar"):
            return
        _snapshot_active, queued, running = self._local_model_download_snapshot()
        # Deliberately not the snapshot's first element: that folds in a merely
        # *claimed* entry (popped, still waiting for the slot). Measuring one of
        # those reports another model's directory growth as its progress and
        # invents a percentage for a download that has not started.
        downloading = self._local_model_download_active
        if not running or downloading is None:
            # A download the *controller* started (the user selected an
            # uncached model and pressed Save) is the same single download as
            # far as the user is concerned, so show its progress here too
            # instead of leaving this tab looking idle.
            preload_active = self._preload_downloading_model()
            if not preload_active:
                return
            model_name, model_dir = preload_active, self.model_dir_edit.text().strip()
            queued = []
        else:
            model_name, model_dir = downloading
        downloaded_bytes = _facade().estimate_cached_model_bytes(model_name, model_dir)
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
        self._local_model_download_bar_shown = True

    def _set_local_models_action_text(
        self,
        text: str,
        color: str,
        *,
        allow_growth: bool = False,
    ) -> None:
        """Show a status line, without silently swallowing long messages.

        The label is deliberately fixed at two lines so a status update during a
        scan cannot resize the model list underneath the pointer. A download
        error is longer than that and used to be clipped mid-sentence, which is
        the one case where the text matters most, so terminal messages are
        allowed to grow and the full string is always available as a tooltip.
        """
        label = self.local_models_action_label
        if allow_growth:
            label.setMinimumHeight(self._dynamic_hint_height(label))
            label.setMaximumHeight(16777215)
        else:
            label.setFixedHeight(self._dynamic_hint_height(label))
        label.setStyleSheet(f"color: {color};")
        label.setText(text)
        label.setToolTip(text)

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
        # Clear the shown-flag with it. Leaving it set let the Local tab's 1 Hz
        # watchdog run the full hide body a second later, which blanks the
        # "Downloaded X" / "Download failed: <reason>" line the user needs.
        self._local_model_download_bar_shown = False
        if success:
            color = "#1b5e20"
        elif text.startswith("Completed with errors"):
            color = "#b26a00"
        elif text.startswith("Download canceled"):
            color = "#b26a00"
        else:
            color = "#b71c1c"
        # A finished download is a terminal message: nothing is about to move
        # underneath the pointer, so let a long failure explain itself.
        self._set_local_models_action_text(text, color, allow_growth=not success)
        self._refresh_local_model_views(force=True)

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
                removed = _facade().delete_cached_model(
                    model_name,
                    self.model_dir_edit.text().strip(),
                )
                total_removed += removed
                # Forget the "downloaded this session" marker with it. It is
                # name-keyed and otherwise only ever shrunk against the scan, so
                # a delete left the row reading "Downloaded" and made every
                # re-download attempt answer "already downloaded or queued"
                # until the app restarted.
                with self._local_model_download_lock:
                    self._local_model_download_completed_names.discard(model_name)
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

    def _browse_model_dir(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select model directory", self.model_dir_edit.text()
        )
        if path:
            self.model_dir_edit.setText(path)
