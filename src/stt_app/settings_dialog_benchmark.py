"""Settings dialog: benchmark mixin (split from settings_dialog.py)."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .app_icon import load_app_icon
from .benchmark_environment import BenchmarkEnvironment, collect_benchmark_environment
from .benchmark_history import (
    BenchmarkHistoryEntry,
    BenchmarkOptions,
    export_benchmark_entry,
)
from .config import LOCAL_ENGLISH_ONLY_MODELS, LOCAL_WEBGPU_BENCHMARK_DEVICE_GROUPS
from .local_benchmark import (
    BenchmarkCancelled,
    BenchmarkCase,
    _format_number,
    _format_seconds,
    format_benchmark_summary,
    normalize_webgpu_benchmark_devices,
)
from .settings_dialog_helpers import (
    _benchmark_status_text,
    _emit_background_signal,
    _INLINE_FIELD_BUTTON_SPACING_PX,
    _WheelPassthroughComboBox,
    _WheelPassthroughSpinBox,
)
from .ui_feedback import restore_vertical_scrollbar

_BENCHMARK_WINDOW_DEFAULT_SIZE = QtCore.QSize(820, 720)
_BENCHMARK_WINDOW_MINIMUM_SIZE = QtCore.QSize(680, 560)
_BENCHMARK_COMPACT_BUTTON_WIDTH_PX = 110
_BENCHMARK_RESULT_SURFACE_STYLESHEET = """
    border: 1px solid #b8c2d2;
    border-radius: 4px;
    background-color: #ffffff;
"""


def _benchmark_created_label(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value or "-")


class _BenchmarkHistoryTable(QtWidgets.QTableWidget):
    """Column-based benchmark history with small QListWidget compatibility.

    A few existing integration seams refer to the widget as
    ``benchmark_history_list``.  Keeping the attribute and these tiny aliases
    avoids needless churn while the visible control is now a proper table.
    """

    def count(self) -> int:
        return self.rowCount()

    def item(
        self,
        row: int,
        column: int = 0,
    ) -> QtWidgets.QTableWidgetItem | None:
        return super().item(row, column)

    def setCurrentRow(self, row: int) -> None:
        if row < 0:
            self.clearSelection()
            return
        self.selectRow(row)
        first = super().item(row, 0)
        if first is not None:
            self.setCurrentItem(first)


class _BenchmarkDetailsView(QtWidgets.QTabWidget):
    """Rendered benchmark context and clickable per-run transcripts."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._plain_text = ""

        self.overview_table = QtWidgets.QTableWidget(0, 2)
        self.overview_table.setHorizontalHeaderLabels(["Field", "Value"])
        self._configure_table(self.overview_table)
        self.overview_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents
        )
        self.overview_table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch
        )
        self.addTab(self.overview_table, "Details")

        transcript_page = QtWidgets.QWidget()
        transcript_layout = QtWidgets.QVBoxLayout(transcript_page)
        transcript_layout.setContentsMargins(0, 0, 0, 0)
        transcript_layout.setSpacing(6)
        transcript_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        transcript_splitter.setChildrenCollapsible(False)

        self.transcripts_table = QtWidgets.QTableWidget(0, 6)
        self.transcripts_table.setHorizontalHeaderLabels(
            ["Model", "Device", "Run", "Consistency", "Words", "Preview"]
        )
        self._configure_table(self.transcripts_table)
        self.transcripts_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        self.transcripts_table.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        transcript_header = self.transcripts_table.horizontalHeader()
        for column in range(5):
            transcript_header.setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeToContents
            )
        transcript_header.setSectionResizeMode(5, QtWidgets.QHeaderView.Stretch)
        self.transcripts_table.itemSelectionChanged.connect(
            self._show_selected_transcript
        )
        transcript_splitter.addWidget(self.transcripts_table)

        self.transcript_text = QtWidgets.QPlainTextEdit()
        self.transcript_text.setReadOnly(True)
        self.transcript_text.setPlaceholderText(
            "Select a model run above to inspect its complete transcript."
        )
        transcript_splitter.addWidget(self.transcript_text)
        transcript_splitter.setSizes([150, 130])
        transcript_layout.addWidget(transcript_splitter)
        self.addTab(transcript_page, "Transcripts")

    @staticmethod
    def _configure_table(table: QtWidgets.QTableWidget) -> None:
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        table.setStyleSheet(_BENCHMARK_RESULT_SURFACE_STYLESHEET)

    def toPlainText(self) -> str:
        """Compatibility accessor for tests and older dialog integrations."""
        return self._plain_text

    def setPlainText(self, text: str) -> None:
        """Show an interim status before a complete entry is available."""
        self._plain_text = str(text or "")
        self._set_overview_rows(
            [("Status", self._plain_text or "No benchmark loaded.")]
        )
        self._set_transcript_rows([])

    def clear(self) -> None:
        self._plain_text = ""
        self.overview_table.setRowCount(0)
        self._set_transcript_rows([])

    def set_entry(self, entry: BenchmarkHistoryEntry) -> None:
        self._plain_text = entry.summary
        options = entry.options
        rows: list[tuple[str, object]] = [
            ("Status", _benchmark_status_text(entry.status)),
            ("Recorded", entry.created_at),
            ("Audio", options.audio_path or options.audio_name or "-"),
            ("Models", ", ".join(options.model_names) or "-"),
            ("Runs per model/device", options.runs),
            ("Language", options.language),
            ("Warmup", "Enabled" if options.warmup else "Disabled"),
            ("Beam size", options.beam_size),
            ("VAD filter", "Enabled" if options.vad_filter else "Disabled"),
            ("Standard device", options.device),
            ("ONNX targets", ", ".join(options.webgpu_devices) or "-"),
        ]
        rows.extend(
            (name, value)
            for name, value in entry.environment.summary_details().items()
            if value not in (None, "", [], {})
        )
        self._set_overview_rows(rows)
        self._set_transcript_rows(entry.cases)

    def set_live_results(self, summary: str, cases: list[BenchmarkCase]) -> None:
        """Render completed cases while the remaining benchmark is running."""
        self._plain_text = str(summary or "")
        self._set_overview_rows(
            [
                ("Status", "Running"),
                ("Completed cases", len(cases)),
                (
                    "Transcript capture",
                    "Available below as soon as each model/device case finishes.",
                ),
            ]
        )
        self._set_transcript_rows(cases)

    def _set_overview_rows(self, rows: list[tuple[str, object]]) -> None:
        self.overview_table.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            self.overview_table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(name)))
            if isinstance(value, (list, tuple)):
                display = ", ".join(str(item) for item in value)
            else:
                display = str(value)
            item = QtWidgets.QTableWidgetItem(display)
            item.setToolTip(display)
            self.overview_table.setItem(row, 1, item)

    def _set_transcript_rows(self, cases: list[BenchmarkCase]) -> None:
        transcript_rows: list[tuple[BenchmarkCase, object, str]] = []
        for case in cases:
            reference = case.runs[0].transcript if case.runs else ""
            available = [run.transcript for run in case.runs if run.transcript]
            all_identical = bool(available) and len(available) == len(case.runs) and all(
                transcript == reference for transcript in available
            )
            for run in case.runs:
                if not run.transcript:
                    consistency = "Not stored (legacy)"
                elif run.run_index == case.runs[0].run_index:
                    consistency = (
                        "Reference · all identical"
                        if all_identical and len(case.runs) > 1
                        else "Reference"
                    )
                elif run.transcript == reference:
                    consistency = "Identical to run 1"
                else:
                    consistency = "Differs from run 1"
                transcript_rows.append((case, run, consistency))

        self.transcripts_table.setRowCount(len(transcript_rows))
        for row, (case, run, consistency) in enumerate(transcript_rows):
            transcript = run.transcript
            preview = " ".join(transcript.split())
            if len(preview) > 140:
                preview = f"{preview[:137].rstrip()}..."
            values = [
                case.model,
                case.device,
                str(run.run_index),
                consistency,
                str(run.transcript_words),
                preview or "Transcript not available in this older result.",
            ]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(QtCore.Qt.UserRole, transcript)
                    item.setData(
                        QtCore.Qt.UserRole + 1,
                        f"{case.model} · {case.device} · run {run.run_index}",
                    )
                if column == 3:
                    if consistency.startswith("Differs"):
                        item.setBackground(QtGui.QColor("#fff4cc"))
                    elif consistency.startswith("Not stored"):
                        item.setForeground(QtGui.QColor("#6b7280"))
                    else:
                        item.setBackground(QtGui.QColor("#e8f5e9"))
                self.transcripts_table.setItem(row, column, item)

        if transcript_rows:
            self.transcripts_table.selectRow(0)
            self._show_selected_transcript()
        else:
            self.transcript_text.clear()

    def _show_selected_transcript(self) -> None:
        row = self.transcripts_table.currentRow()
        item = self.transcripts_table.item(row, 0) if row >= 0 else None
        if item is None:
            self.transcript_text.clear()
            return
        transcript = str(item.data(QtCore.Qt.UserRole) or "")
        label = str(item.data(QtCore.Qt.UserRole + 1) or "Transcript")
        if transcript:
            self.transcript_text.setPlainText(f"{label}\n\n{transcript}")
        else:
            self.transcript_text.setPlainText(
                f"{label}\n\nThis benchmark predates transcript capture. "
                "Run it again to compare recognition quality."
            )


def _facade():
    """Return the settings_dialog facade module.

    Imported lazily so this mixin module has no import-time dependency on the
    facade (which imports this module), and so the monkeypatched
    ``stt_app.settings_dialog.run_benchmark_cases`` still resolves at call time.
    """
    import stt_app.settings_dialog as facade

    return facade


class _BenchmarkMixin:
    def _build_benchmark_tab(self) -> None:
        """Build the Benchmark tab: history and results, viewed directly.

        Viewing results/history is the frequent action; running a benchmark
        is rare. So the tab shows the viewing parts (history + results)
        directly, filling the tab via a vertical splitter, with a compact
        header row to open the "Run Benchmark" window and to show live
        status while a run is in progress. The run side (model selection,
        run options, run/cancel controls) lives only in that pop-out window,
        built by ``_build_benchmark_window``.
        """
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        header_row = QtWidgets.QHBoxLayout()
        self._configure_button_row(header_row)
        self.open_benchmark_window_button = QtWidgets.QPushButton(
            "Run Benchmark..."
        )
        self.open_benchmark_window_button.clicked.connect(
            self._open_benchmark_window
        )
        header_row.addWidget(self.open_benchmark_window_button)
        self.benchmark_status_label = QtWidgets.QLabel("")
        self.benchmark_status_label.setWordWrap(False)
        self.benchmark_status_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )
        self.benchmark_status_label.setFixedHeight(
            self.open_benchmark_window_button.sizeHint().height()
        )
        header_row.addWidget(self.benchmark_status_label, 1)
        layout.addLayout(header_row)

        self.benchmark_main_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.benchmark_main_splitter.setChildrenCollapsible(False)

        history_box = self._build_benchmark_history_box()
        results_box = self._build_benchmark_results_box()

        self.benchmark_main_splitter.addWidget(history_box)
        self.benchmark_main_splitter.addWidget(results_box)
        self.benchmark_main_splitter.setSizes([220, 420])
        layout.addWidget(self.benchmark_main_splitter, 1)

        self._benchmark_tab_index = self.tabs.addTab(tab, "Benchmark")
        self._build_benchmark_window()

    def _build_benchmark_history_box(self) -> QtWidgets.QGroupBox:
        history_box = QtWidgets.QGroupBox("Benchmark History")
        history_box.setMinimumHeight(210)
        history_layout = QtWidgets.QVBoxLayout(history_box)
        history_layout.setContentsMargins(10, 10, 10, 10)
        history_layout.setSpacing(6)

        self.benchmark_history_note_label = QtWidgets.QLabel(
            "Runs that produce results are saved here automatically, including "
            "partial canceled runs. Export creates a separate file for sharing "
            "or analysis."
        )
        self.benchmark_history_note_label.setWordWrap(True)
        self._style_note_label(self.benchmark_history_note_label)
        history_layout.addWidget(self.benchmark_history_note_label)

        self.benchmark_history_list = _BenchmarkHistoryTable(0, 6)
        self.benchmark_history_list.setHorizontalHeaderLabels(
            ["Recorded", "Audio", "Models", "Runs", "Best RTF", "Status"]
        )
        self.benchmark_history_list.setMinimumHeight(90)
        self.benchmark_history_list.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self.benchmark_history_list.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        self.benchmark_history_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        self.benchmark_history_list.setAlternatingRowColors(True)
        self.benchmark_history_list.verticalHeader().setVisible(False)
        self.benchmark_history_list.setHorizontalScrollMode(
            QtWidgets.QAbstractItemView.ScrollPerPixel
        )
        self.benchmark_history_list.setVerticalScrollMode(
            QtWidgets.QAbstractItemView.ScrollPerPixel
        )
        history_header = self.benchmark_history_list.horizontalHeader()
        history_header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        history_header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        history_header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        for column in (3, 4, 5):
            history_header.setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeToContents
            )
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
        return history_box

    def _build_benchmark_results_box(self) -> QtWidgets.QGroupBox:
        results_box = QtWidgets.QGroupBox("Results")
        results_box.setMinimumHeight(400)
        results_box.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        results_layout = QtWidgets.QVBoxLayout(results_box)
        results_layout.setContentsMargins(10, 10, 10, 10)
        results_layout.setSpacing(6)
        self.benchmark_results_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.benchmark_results_splitter.setChildrenCollapsible(False)
        self.benchmark_results_table = QtWidgets.QTableWidget(0, 7)
        self.benchmark_results_table.setMinimumHeight(110)
        self.benchmark_results_table.setHorizontalHeaderLabels(
            [
                "Model",
                "Resolved Device",
                "Compute",
                "Load",
                "Avg",
                "RTF",
                "Status",
            ]
        )
        device_header = self.benchmark_results_table.horizontalHeaderItem(1)
        if device_header is not None:
            device_header.setToolTip(
                "The device actually used by the runtime. Older stored "
                "faster-whisper results may show the configured value 'auto'."
            )
        self.benchmark_results_table.setStyleSheet(
            _BENCHMARK_RESULT_SURFACE_STYLESHEET
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
        self.benchmark_results_table.setHorizontalScrollMode(
            QtWidgets.QAbstractItemView.ScrollPerPixel
        )
        self.benchmark_results_table.setVerticalScrollMode(
            QtWidgets.QAbstractItemView.ScrollPerPixel
        )
        self.benchmark_results_table.horizontalHeader().setStretchLastSection(True)
        self.benchmark_results_splitter.addWidget(self.benchmark_results_table)

        self.benchmark_summary_text = _BenchmarkDetailsView()
        self.benchmark_transcripts_table = (
            self.benchmark_summary_text.transcripts_table
        )
        self.benchmark_transcript_text = self.benchmark_summary_text.transcript_text
        self.benchmark_summary_text.setMinimumHeight(220)
        self.benchmark_results_splitter.addWidget(self.benchmark_summary_text)
        self.benchmark_results_splitter.setSizes([130, 260])
        results_layout.addWidget(self.benchmark_results_splitter)

        results_actions = QtWidgets.QHBoxLayout()
        self._configure_button_row(results_actions)
        self.clear_benchmark_results_button = QtWidgets.QPushButton("Clear Loaded")
        self.clear_benchmark_results_button.setToolTip(
            "Clear the displayed result without deleting its saved history entry."
        )
        self.clear_benchmark_results_button.clicked.connect(
            self._clear_benchmark_results
        )
        self.export_benchmark_results_button = QtWidgets.QPushButton(
            "Export Loaded..."
        )
        self.export_benchmark_results_button.setEnabled(False)
        self.export_benchmark_results_button.setToolTip(
            "Export the displayed result. Runs are already saved in Benchmark History."
        )
        self.export_benchmark_results_button.clicked.connect(
            self._export_current_benchmark_results
        )
        results_actions.addWidget(self.clear_benchmark_results_button)
        results_actions.addWidget(self.export_benchmark_results_button)
        results_actions.addStretch(1)
        results_layout.addLayout(results_actions)
        return results_box

    def _build_benchmark_window(self) -> None:
        """Build the resizable pop-out window that hosts the "run" UI.

        Owned by the settings dialog (parent=self) so it hides/closes together
        with it; re-opening the "Run Benchmark..." button raises the existing
        window instead of creating a second one (see
        ``_open_benchmark_window``). Only the model selection, audio picker,
        run options, and run/cancel controls live here; history and results
        stay in the Benchmark tab so they remain visible while a benchmark
        runs.
        """
        window = QtWidgets.QDialog(self)
        self.benchmark_window = window
        window.setWindowTitle("Run Benchmark")
        window.setWindowIcon(load_app_icon())
        window.setModal(False)
        window.setWindowFlag(QtCore.Qt.Window, True)
        window.setWindowFlag(QtCore.Qt.WindowSystemMenuHint, True)
        window.setWindowFlag(QtCore.Qt.WindowMinimizeButtonHint, True)
        window.setWindowFlag(QtCore.Qt.WindowMaximizeButtonHint, True)
        window.setWindowFlag(QtCore.Qt.WindowCloseButtonHint, True)
        window.resize(_BENCHMARK_WINDOW_DEFAULT_SIZE)
        window.setMinimumSize(_BENCHMARK_WINDOW_MINIMUM_SIZE)

        outer_layout = QtWidgets.QVBoxLayout(window)
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.setSpacing(6)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.benchmark_setup_scroll = scroll

        setup_box = QtWidgets.QWidget()
        self.benchmark_setup_box = setup_box
        scroll.setWidget(setup_box)
        setup_layout = QtWidgets.QVBoxLayout(setup_box)
        setup_layout.setContentsMargins(10, 10, 10, 10)
        setup_layout.setSpacing(6)

        intro = QtWidgets.QLabel(
            "Benchmark installed local models against one audio file. "
            "Results appear live on the Benchmark tab and are saved to history "
            "automatically. Test Auto, GPU-only, CPU-only, DirectML, or WebGPU "
            "targets on this machine."
        )
        intro.setWordWrap(True)
        self._style_note_label(intro)
        setup_layout.addWidget(intro)

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
        setup_layout.addWidget(audio_box)

        models_box = QtWidgets.QGroupBox("Installed Models")
        models_box.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        models_layout = QtWidgets.QVBoxLayout(models_box)
        self.benchmark_models_list = QtWidgets.QListWidget()
        # Explorer-style selection (Shift for ranges, Ctrl for toggles), like
        # every other multi-select list in the app.
        self.benchmark_models_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection
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

        # Quality-of-life: bulk select/clear/refresh in one compact row, so
        # the user does not have to click each model individually (all
        # models are selected by default) and this area does not sprawl into
        # multiple oversized button rows.
        select_buttons_row = QtWidgets.QHBoxLayout()
        self._configure_button_row(select_buttons_row)
        self.benchmark_select_all_button = QtWidgets.QPushButton("Select all")
        self.benchmark_select_all_button.clicked.connect(
            self.benchmark_models_list.selectAll
        )
        self.benchmark_deselect_all_button = QtWidgets.QPushButton("Deselect all")
        self.benchmark_deselect_all_button.clicked.connect(
            self.benchmark_models_list.clearSelection
        )
        self.refresh_benchmark_models_button = QtWidgets.QPushButton("Refresh")
        self.refresh_benchmark_models_button.clicked.connect(
            self._refresh_local_model_views
        )
        for button in (
            self.benchmark_select_all_button,
            self.benchmark_deselect_all_button,
            self.refresh_benchmark_models_button,
        ):
            button.setFixedWidth(_BENCHMARK_COMPACT_BUTTON_WIDTH_PX)
        self._match_field_button_height(
            self.benchmark_select_all_button,
            self.benchmark_deselect_all_button,
            self.refresh_benchmark_models_button,
        )
        select_buttons_row.addWidget(self.benchmark_select_all_button)
        select_buttons_row.addWidget(self.benchmark_deselect_all_button)
        select_buttons_row.addWidget(self.refresh_benchmark_models_button)
        select_buttons_row.addStretch(1)
        models_layout.addLayout(select_buttons_row)
        models_help = QtWidgets.QLabel(
            "Only locally available models can be benchmarked here. Download missing models on the Local tab first."
        )
        models_help.setWordWrap(True)
        self._style_note_label(models_help)
        models_layout.addWidget(models_help)
        setup_layout.addWidget(models_box)

        self.benchmark_options_toggle = QtWidgets.QToolButton()
        self.benchmark_options_toggle.setCheckable(True)
        self.benchmark_options_toggle.setChecked(False)
        # Use a small text triangle instead of the style-drawn QToolButton arrow,
        # which some styles render oversized and misaligned next to the label.
        self.benchmark_options_toggle.setToolButtonStyle(
            QtCore.Qt.ToolButtonTextOnly
        )
        self.benchmark_options_toggle.setArrowType(QtCore.Qt.NoArrow)
        self.benchmark_options_toggle.toggled.connect(
            self._set_benchmark_options_visible
        )
        setup_layout.addWidget(
            self.benchmark_options_toggle,
            0,
            QtCore.Qt.AlignLeft,
        )

        self.benchmark_options_box = QtWidgets.QGroupBox("Run Options")
        options_form = QtWidgets.QFormLayout(self.benchmark_options_box)
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
            "Runs one complete, unmeasured transcription after model loading to "
            "prime runtime compilation, kernels, and first-run caches. Model "
            "load time is still measured separately."
        )
        warmup_note = QtWidgets.QLabel(
            "Enable for steady-state comparisons; disable to include first-use "
            "inference overhead in the measured runs."
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
        setup_layout.addWidget(self.benchmark_options_box)
        self._set_benchmark_options_visible(False)

        benchmark_actions = QtWidgets.QHBoxLayout()
        self._configure_button_row(benchmark_actions)
        self.run_benchmark_button = QtWidgets.QPushButton("Run Benchmark")
        self.run_benchmark_button.clicked.connect(self._run_local_benchmark)
        self.cancel_benchmark_button = QtWidgets.QPushButton("Cancel Benchmark")
        self.cancel_benchmark_button.setEnabled(False)
        self.cancel_benchmark_button.clicked.connect(self._cancel_local_benchmark)
        benchmark_actions.addWidget(self.run_benchmark_button)
        benchmark_actions.addWidget(self.cancel_benchmark_button)
        benchmark_actions.addStretch(1)
        setup_layout.addLayout(benchmark_actions)

        outer_layout.addWidget(scroll, 1)

        self.benchmark_window_status_label = QtWidgets.QLabel("")
        self.benchmark_window_status_label.setWordWrap(True)
        outer_layout.addWidget(self.benchmark_window_status_label)

    def _open_benchmark_window(self) -> None:
        """Show the benchmark window, raising the existing one if already open."""
        window = self.benchmark_window
        self._refresh_benchmark_model_list()
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
        window.raise_()
        window.activateWindow()

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
        """Update every visible benchmark status label from one place.

        The tab's header status label is always present; the pop-out
        window's own status label only exists while the window has been
        built (always true after ``_build_benchmark_window`` runs, but the
        ``hasattr`` guard keeps this safe to call before that or in tests
        that build a bare mixin instance).
        """
        style = f"color: {color};"
        if hasattr(self, "benchmark_status_label"):
            self.benchmark_status_label.setText(text)
            self.benchmark_status_label.setStyleSheet(style)
        if hasattr(self, "benchmark_window_status_label"):
            self.benchmark_window_status_label.setText(text)
            self.benchmark_window_status_label.setStyleSheet(style)

    def _set_benchmark_options_visible(self, visible: bool) -> None:
        if hasattr(self, "benchmark_options_box"):
            self.benchmark_options_box.setVisible(bool(visible))
        if hasattr(self, "benchmark_options_toggle"):
            self.benchmark_options_toggle.setChecked(bool(visible))
            self.benchmark_options_toggle.setText(
                "▾  Hide Run Options" if visible else "▸  Show Run Options"
            )

    def _expand_benchmark_results_area(self) -> None:
        if not hasattr(self, "benchmark_main_splitter"):
            return
        self.benchmark_main_splitter.setSizes([220, 420])

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
        self.benchmark_select_all_button.setEnabled(not busy)
        self.benchmark_deselect_all_button.setEnabled(not busy)
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
            _emit_background_signal(self, "benchmark_progress", text)

        def _run() -> None:
            completed_cases: list[BenchmarkCase] = []
            environment = collect_benchmark_environment()
            self._current_benchmark_environment = environment

            def _case_finished(case: BenchmarkCase) -> None:
                completed_cases.append(case)
                _emit_background_signal(self, "benchmark_case_finished", case)

            def _is_canceled() -> bool:
                return cancel_event.is_set()

            try:
                # Shutdown may arrive while environment metadata is still being
                # collected. Do not launch a new benchmark child process after
                # cancellation has already been requested.
                if cancel_event.is_set():
                    raise BenchmarkCancelled("Benchmark canceled.")
                cases = _facade().run_benchmark_cases(
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
                _emit_background_signal(
                    self,
                    "benchmark_finished",
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
                _emit_background_signal(
                    self,
                    "benchmark_finished",
                    False,
                    str(exc),
                    [],
                )
                return

            status = "completed_with_errors" if any(case.error for case in cases) else "completed"
            _emit_background_signal(
                self,
                "benchmark_finished",
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
        summary = self._benchmark_summary(
            self._current_benchmark_cases,
            status="running",
        )
        self.benchmark_summary_text.set_live_results(
            summary,
            self._current_benchmark_cases,
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
            self.benchmark_summary_text.set_entry(entry)
            try:
                self._benchmark_history_store.add_entry(entry)
            except Exception as exc:
                history_error = str(exc)
                self._refresh_benchmark_history_list()
            else:
                self._refresh_benchmark_history_list(select_entry=entry)
        else:
            self._current_benchmark_entry = None
            self.benchmark_summary_text.setPlainText(text)
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
                "Benchmark completed with errors and was saved to history. "
                "See the summary for details.",
                "#b26a00",
            )
        else:
            self._set_benchmark_status(
                "Benchmark finished and saved to history.",
                "#1b5e20",
            )
        self._expand_benchmark_results_area()
        self._update_benchmark_actions()

    def _refresh_benchmark_history_list(
        self,
        *,
        select_entry: BenchmarkHistoryEntry | None = None,
    ) -> None:
        if not hasattr(self, "benchmark_history_list"):
            return
        previous_scroll = self.benchmark_history_list.verticalScrollBar().value()
        self.benchmark_history_list.setRowCount(0)
        selected_row = -1
        for row, entry in enumerate(self._benchmark_history_store.recent_entries(20)):
            self.benchmark_history_list.insertRow(row)
            successful = [
                case for case in entry.cases if case.error is None and case.runs
            ]
            best_rtf = min(
                (case.avg_rtf for case in successful),
                default=float("nan"),
            )
            actual_runs = sum(len(case.runs) for case in entry.cases)
            values = [
                _benchmark_created_label(entry.created_at),
                entry.options.audio_name or Path(entry.options.audio_path).name or "-",
                ", ".join(entry.options.model_names) or "-",
                str(actual_runs),
                _format_number(best_rtf),
                _benchmark_status_text(entry.status),
            ]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(QtCore.Qt.UserRole, entry)
                self.benchmark_history_list.setItem(row, column, item)
            if (
                select_entry is not None
                and entry.identity_key() == select_entry.identity_key()
            ):
                selected_row = row
        if selected_row >= 0:
            self.benchmark_history_list.setCurrentRow(selected_row)
        restore_vertical_scrollbar(self.benchmark_history_list, previous_scroll)
        self._update_benchmark_history_actions()

    def _selected_benchmark_history_entry(self) -> BenchmarkHistoryEntry | None:
        if not hasattr(self, "benchmark_history_list"):
            return None
        row = self.benchmark_history_list.currentRow()
        if row < 0:
            return None
        item = self.benchmark_history_list.item(row, 0)
        if item is None:
            return None
        entry = item.data(QtCore.Qt.UserRole)
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

    def _load_benchmark_history_item(
        self,
        item: QtWidgets.QTableWidgetItem,
    ) -> None:
        entry = item.data(QtCore.Qt.UserRole)
        if not isinstance(entry, BenchmarkHistoryEntry):
            first = self.benchmark_history_list.item(item.row(), 0)
            entry = first.data(QtCore.Qt.UserRole) if first is not None else None
        if isinstance(entry, BenchmarkHistoryEntry):
            self._load_benchmark_history_entry(entry)

    def _load_benchmark_history_entry(self, entry: BenchmarkHistoryEntry) -> None:
        self._current_benchmark_entry = entry
        self._current_benchmark_options = entry.options
        self._current_benchmark_environment = entry.environment
        self._current_benchmark_cases = list(entry.cases)
        self._populate_benchmark_results(entry.cases)
        self.benchmark_summary_text.set_entry(entry)
        self._set_benchmark_status("Loaded benchmark history entry.", "#555")
        self._expand_benchmark_results_area()
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
