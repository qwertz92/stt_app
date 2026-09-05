"""Benchmark results: run-order column, three-state sorting, no layout jump."""
from __future__ import annotations

import math

from PySide6 import QtCore, QtWidgets
from test_settings_dialog_connection import (
    _FakeLogger,
    _FakeSecretStore,
    _FakeSettingsStore,
)

from stt_app.local_benchmark import BenchmarkCase, BenchmarkRun
from stt_app.settings_dialog import SettingsDialog
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
