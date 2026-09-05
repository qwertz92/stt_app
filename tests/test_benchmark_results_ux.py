"""Benchmark results: run-order column, three-state sorting, no layout jump."""
from __future__ import annotations

import math
import threading

from PySide6 import QtCore, QtWidgets
from test_benchmark_transcript_ui import _entry, _run
from test_settings_dialog_connection import (
    _FakeLogger,
    _FakeSecretStore,
    _FakeSettingsStore,
)

from stt_app.benchmark_history import BenchmarkHistoryEntry, BenchmarkHistoryStore
from stt_app.local_benchmark import BenchmarkCase, BenchmarkRun
from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_dialog_benchmark import (
    BenchmarkResultsPanel,
    BenchmarkResultsWindow,
)
from stt_app.settings_store import AppSettings

_RESULT_HEADERS = [
    "#",
    "Model",
    "Resolved Device",
    "Compute",
    "Load",
    "Avg",
    "RTF",
    "Status",
]


def _measured_case(model: str, rtf: float, *, device: str = "cpu") -> BenchmarkCase:
    return BenchmarkCase(
        model=model,
        device=device,
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=rtf * 10,
        runs=[
            BenchmarkRun(
                run_index=1,
                seconds=rtf * 2,
                audio_duration_seconds=2.0,
                real_time_factor=rtf,
                transcript_chars=5,
                transcript_words=1,
                detected_language="en",
                language_probability=0.9,
                transcript="hello",
            )
        ],
    )


def _failed_case(model: str, *, device: str = "cpu") -> BenchmarkCase:
    return BenchmarkCase(
        model=model,
        device=device,
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=math.nan,
        runs=[],
        error="the runtime refused this model",
    )


def _dialog() -> tuple[SettingsDialog, QtWidgets.QApplication]:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    return dialog, app


def _mixed_cases() -> list[BenchmarkCase]:
    """Four cases in run order, one of them an error row with no numbers."""
    return [
        _measured_case("beta", 0.5),
        _measured_case("Alpha", 0.1),
        _failed_case("gamma"),
        _measured_case("Delta", 0.3),
    ]


def _column(table: QtWidgets.QTableWidget, column: int) -> list[str]:
    return [
        table.item(row, column).text() if table.item(row, column) else ""
        for row in range(table.rowCount())
    ]


def test_the_results_table_leads_with_the_run_order_of_every_case():
    dialog, app = _dialog()
    table = dialog.benchmark_results_table

    dialog._populate_benchmark_results(_mixed_cases())

    assert table.columnCount() == len(_RESULT_HEADERS)
    assert [
        table.horizontalHeaderItem(column).text()
        for column in range(table.columnCount())
    ] == _RESULT_HEADERS
    assert _column(table, 0) == ["1", "2", "3", "4"]
    assert _column(table, 1) == ["beta", "Alpha", "gamma", "Delta"]
    header = table.horizontalHeader()
    assert header.sectionResizeMode(0) == QtWidgets.QHeaderView.ResizeToContents
    assert header.stretchLastSection() is True
    _ = app


def test_three_clicks_on_rtf_sort_up_then_down_then_back_to_the_run_order():
    """NaN rows carry no measurement, so they belong last in both directions."""
    dialog, app = _dialog()
    table = dialog.benchmark_results_table
    header = table.horizontalHeader()
    dialog._populate_benchmark_results(_mixed_cases())
    rtf_column = _RESULT_HEADERS.index("RTF")

    header.sectionClicked.emit(rtf_column)

    assert _column(table, rtf_column) == ["0.100", "0.300", "0.500", "-"]
    assert _column(table, 0) == ["2", "4", "1", "3"]
    assert header.isSortIndicatorShown() is True
    assert header.sortIndicatorSection() == rtf_column
    assert header.sortIndicatorOrder() == QtCore.Qt.AscendingOrder

    header.sectionClicked.emit(rtf_column)

    assert _column(table, rtf_column) == ["0.500", "0.300", "0.100", "-"]
    assert _column(table, 0) == ["1", "4", "2", "3"]
    assert header.sortIndicatorSection() == rtf_column
    assert header.sortIndicatorOrder() == QtCore.Qt.DescendingOrder

    header.sectionClicked.emit(rtf_column)

    assert _column(table, 0) == ["1", "2", "3", "4"]
    assert _column(table, 1) == ["beta", "Alpha", "gamma", "Delta"]
    # -1 is Qt's "no section carries the indicator"; the flag itself stays on
    # because turning it off changes every ResizeToContents column's width.
    assert header.sortIndicatorSection() == -1
    assert header.isSortIndicatorShown() is True
    _ = app


def test_sorting_by_model_ignores_case():
    dialog, app = _dialog()
    table = dialog.benchmark_results_table
    header = table.horizontalHeader()
    dialog._populate_benchmark_results(_mixed_cases())
    model_column = _RESULT_HEADERS.index("Model")

    header.sectionClicked.emit(model_column)

    assert _column(table, model_column) == ["Alpha", "beta", "Delta", "gamma"]

    header.sectionClicked.emit(model_column)

    assert _column(table, model_column) == ["gamma", "Delta", "beta", "Alpha"]
    _ = app


def test_ties_keep_the_order_the_cases_were_measured_in():
    dialog, app = _dialog()
    table = dialog.benchmark_results_table
    header = table.horizontalHeader()
    dialog._populate_benchmark_results(
        [
            _measured_case("first", 0.2),
            _measured_case("second", 0.2),
            _measured_case("third", 0.1),
            _measured_case("fourth", 0.2),
        ]
    )
    rtf_column = _RESULT_HEADERS.index("RTF")

    header.sectionClicked.emit(rtf_column)

    assert _column(table, 1) == ["third", "first", "second", "fourth"]

    header.sectionClicked.emit(rtf_column)

    assert _column(table, 1) == ["first", "second", "fourth", "third"]
    _ = app


def test_a_case_finishing_mid_run_keeps_the_order_the_user_chose():
    """`_populate_benchmark_results` runs once per finished case."""
    dialog, app = _dialog()
    table = dialog.benchmark_results_table
    header = table.horizontalHeader()
    live: list[BenchmarkCase] = [_measured_case("beta", 0.5)]
    dialog._populate_benchmark_results(list(live))
    rtf_column = _RESULT_HEADERS.index("RTF")
    header.sectionClicked.emit(rtf_column)
    header.sectionClicked.emit(rtf_column)

    live.append(_measured_case("Alpha", 0.1))
    dialog._populate_benchmark_results(list(live))

    assert _column(table, rtf_column) == ["0.500", "0.100"]

    live.append(_measured_case("Delta", 0.9))
    dialog._populate_benchmark_results(list(live))

    assert _column(table, rtf_column) == ["0.900", "0.500", "0.100"]
    assert _column(table, 0) == ["3", "1", "2"]
    _ = app


def test_clicking_the_results_header_moves_nothing():
    dialog, app = _dialog()
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    dialog.show()
    app.processEvents()
    table = dialog.benchmark_results_table
    header = table.horizontalHeader()
    dialog._populate_benchmark_results(_mixed_cases())
    app.processEvents()

    def _geometry() -> dict[str, object]:
        return {
            "header_height": header.height(),
            "header_hint": header.sizeHint(),
            "sections": [
                header.sectionSize(column) for column in range(table.columnCount())
            ],
            "rows": [table.rowHeight(row) for row in range(table.rowCount())],
            "table": table.geometry(),
            "splitter": dialog.benchmark_results_splitter.sizes(),
        }

    before = _geometry()
    for column in range(table.columnCount()):
        for _click in range(3):
            header.sectionClicked.emit(column)
            app.processEvents()
            assert _geometry() == before, f"column {column} moved the layout"
    _ = app


def test_every_results_column_says_how_sorting_works():
    dialog, app = _dialog()
    table = dialog.benchmark_results_table

    for column in range(table.columnCount()):
        tooltip = table.horizontalHeaderItem(column).toolTip()
        assert "a third click restores the run order" in tooltip, column
    # The Resolved Device explanation is kept, not replaced.
    assert "runtime" in table.horizontalHeaderItem(2).toolTip()
    _ = app


def test_the_results_panel_shows_a_stored_run_on_its_own():
    """The panel is the whole Results view, usable outside the settings tab."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    panel = BenchmarkResultsPanel()
    entry = _entry([_run(1, "hello world"), _run(2, "hello world")])

    panel.show_entry(entry)

    assert panel.results_table.rowCount() == 1
    assert panel.results_table.item(0, 0).text() == "1"
    assert panel.results_table.item(0, 1).text() == "small"
    assert panel.details_view.toPlainText() == entry.summary
    assert panel.details_view.transcripts_table.rowCount() == 2
    assert panel.splitter.widget(0) is panel.results_table
    assert panel.splitter.widget(1) is panel.details_view

    panel.set_status_text("Running benchmark...")

    assert panel.details_view.toPlainText() == "Running benchmark..."
    assert panel.details_view.transcripts_table.rowCount() == 0

    panel.show_live("live summary", list(entry.cases))

    assert panel.results_table.rowCount() == 1
    assert panel.details_view.toPlainText() == "live summary"

    panel.clear()

    assert panel.results_table.rowCount() == 0
    assert panel.details_view.toPlainText() == ""
    _ = app


def test_a_cleared_panel_does_not_bring_the_result_back_on_the_next_sort():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    panel = BenchmarkResultsPanel()
    panel.show_cases(_mixed_cases())

    panel.clear()
    panel.results_table.horizontalHeader().sectionClicked.emit(1)

    assert panel.results_table.rowCount() == 0
    _ = app


def test_the_benchmark_tab_addresses_the_panels_own_widgets():
    dialog, app = _dialog()

    panel = dialog.benchmark_results_panel
    assert isinstance(panel, BenchmarkResultsPanel)
    assert dialog.benchmark_results_table is panel.results_table
    assert dialog.benchmark_summary_text is panel.details_view
    assert dialog.benchmark_results_splitter is panel.splitter
    assert (
        dialog.benchmark_transcripts_table is panel.details_view.transcripts_table
    )
    assert dialog.benchmark_transcript_text is panel.details_view.transcript_text
    _ = app


def _history_dialog(tmp_path, entries: list[BenchmarkHistoryEntry]):
    dialog, app = _dialog()
    dialog._benchmark_history_store = BenchmarkHistoryStore(
        path=tmp_path / "benchmark_history.json"
    )
    for entry in entries:
        dialog._benchmark_history_store.add_entry(entry)
    dialog._refresh_benchmark_history_list()
    return dialog, app


def _stored_entry(transcript: str) -> BenchmarkHistoryEntry:
    return _entry([_run(1, transcript)])


def test_two_stored_runs_get_two_windows_and_one_run_gets_one(tmp_path):
    first = _stored_entry("first run")
    second = _stored_entry("second run")
    # `identity_key` is (created_at, status, summary); both differ here.
    second.summary = "Benchmark summary:\nthe other run"
    dialog, app = _history_dialog(tmp_path, [first, second])

    dialog._open_benchmark_results_window(first)
    dialog._open_benchmark_results_window(second)
    app.processEvents()

    assert len(dialog._benchmark_result_windows) == 2
    first_window = dialog._benchmark_result_windows[first.identity_key()]
    second_window = dialog._benchmark_result_windows[second.identity_key()]
    assert first_window is not second_window
    assert first_window.isVisible() is True
    assert second_window.isVisible() is True

    dialog._open_benchmark_results_window(first)
    app.processEvents()

    assert len(dialog._benchmark_result_windows) == 2
    assert dialog._benchmark_result_windows[first.identity_key()] is first_window
    first_window.close()
    second_window.close()
    app.processEvents()
    _ = app


def test_a_results_window_shows_the_entry_and_exports_exactly_it():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    entry = _stored_entry("exported run")
    exported: list[BenchmarkHistoryEntry] = []
    window = BenchmarkResultsWindow(entry, export_entry=exported.append)

    assert window.isModal() is False
    assert window.windowFlags() & QtCore.Qt.Window
    assert window.minimumSize() == QtCore.QSize(560, 400)
    assert "sample.wav" in window.windowTitle()
    assert window.windowTitle().startswith("Benchmark ")
    assert window.panel.results_table.rowCount() == 1
    assert window.panel.results_table.item(0, 1).text() == "small"
    assert window.panel.details_view.toPlainText() == entry.summary

    window.export_button.click()

    assert exported == [entry]
    window.close()
    _ = app


def test_a_results_window_sorts_on_its_own():
    """Two windows are opened to compare runs; one sort must not move another."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = BenchmarkResultsWindow(_stored_entry("one"), export_entry=lambda _e: None)
    window.panel.show_cases(_mixed_cases())
    other = BenchmarkResultsPanel()
    other.show_cases(_mixed_cases())

    window.panel.results_table.horizontalHeader().sectionClicked.emit(
        _RESULT_HEADERS.index("RTF")
    )

    assert _column(window.panel.results_table, 0) == ["2", "4", "1", "3"]
    assert _column(other.results_table, 0) == ["1", "2", "3", "4"]
    window.close()
    _ = app


def test_deleting_a_history_entry_closes_its_window(monkeypatch, tmp_path):
    entry = _stored_entry("doomed run")
    dialog, app = _history_dialog(tmp_path, [entry])
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *_a, **_k: QtWidgets.QMessageBox.Yes),
    )
    dialog.benchmark_history_list.setCurrentRow(0)
    dialog._open_benchmark_results_window(entry)
    app.processEvents()
    assert dialog._benchmark_result_windows

    dialog._delete_selected_benchmark_history()
    app.processEvents()

    assert dialog._benchmark_result_windows == {}
    _ = app


def test_clearing_the_history_closes_every_results_window(monkeypatch, tmp_path):
    first = _stored_entry("first run")
    second = _stored_entry("second run")
    second.summary = "Benchmark summary:\nthe other run"
    dialog, app = _history_dialog(tmp_path, [first, second])
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *_a, **_k: QtWidgets.QMessageBox.Yes),
    )
    dialog._open_benchmark_results_window(first)
    dialog._open_benchmark_results_window(second)
    app.processEvents()
    assert len(dialog._benchmark_result_windows) == 2

    dialog._clear_benchmark_history()
    app.processEvents()

    assert dialog._benchmark_result_windows == {}
    _ = app


def test_a_stored_run_can_be_opened_in_a_window_while_a_benchmark_runs(tmp_path):
    """A pop-out only reads the entry, so it cannot disturb the running case list."""
    entry = _stored_entry("stored run")
    dialog, app = _history_dialog(tmp_path, [entry])
    dialog.benchmark_history_list.setCurrentRow(0)
    dialog._current_benchmark_entry = entry
    dialog._update_benchmark_actions()

    assert dialog.open_benchmark_history_window_button.isEnabled() is True
    assert dialog.open_benchmark_results_window_button.isEnabled() is True

    dialog._active_benchmark_thread = threading.Thread(target=lambda: None)
    dialog._update_benchmark_actions()

    assert dialog.export_benchmark_history_button.isEnabled() is False
    assert dialog.load_benchmark_history_button.isEnabled() is False
    assert dialog.open_benchmark_history_window_button.isEnabled() is True
    assert dialog.open_benchmark_results_window_button.isEnabled() is True

    dialog._active_benchmark_thread = None
    # `_selected_benchmark_history_entry` reads the current cell, which
    # `clearSelection()` leaves in place.
    dialog.benchmark_history_list.setCurrentCell(-1, -1)
    dialog._current_benchmark_entry = None
    dialog._update_benchmark_actions()

    assert dialog.open_benchmark_history_window_button.isEnabled() is False
    assert dialog.open_benchmark_results_window_button.isEnabled() is False
    _ = app


def _button_row_of(button: QtWidgets.QPushButton) -> list[QtWidgets.QWidget]:
    """The widgets of the layout row that holds *button*, in visual order."""
    pending: list[QtWidgets.QLayout] = [button.parentWidget().layout()]
    while pending:
        layout = pending.pop()
        if layout is None:
            continue
        widgets: list[QtWidgets.QWidget] = []
        for index in range(layout.count()):
            item = layout.itemAt(index)
            nested = item.layout()
            if nested is not None:
                pending.append(nested)
            widget = item.widget()
            if widget is not None:
                widgets.append(widget)
        if button in widgets:
            return widgets
    return []


def test_open_in_window_sits_in_both_action_rows():
    dialog, app = _dialog()

    assert _button_row_of(dialog.clear_benchmark_results_button) == [
        dialog.clear_benchmark_results_button,
        dialog.open_benchmark_results_window_button,
        dialog.export_benchmark_results_button,
    ]
    history_row = _button_row_of(dialog.load_benchmark_history_button)
    assert history_row[:3] == [
        dialog.load_benchmark_history_button,
        dialog.export_benchmark_history_button,
        dialog.open_benchmark_history_window_button,
    ]
    assert dialog.open_benchmark_results_window_button.text() == "Open in Window"
    assert dialog.open_benchmark_history_window_button.text() == "Open in Window"
    _ = app
