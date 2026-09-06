"""Benchmark results: run-order column, three-state sorting, no layout jump."""
from __future__ import annotations

import json
import math
import threading

import pytest
from PySide6 import QtCore, QtTest, QtWidgets
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
    _BENCHMARK_RESULT_STATUS_COLUMN,
    BenchmarkResultsPanel,
    BenchmarkResultsWindow,
    _benchmark_created_label,
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

    dialog.benchmark_results_panel.show_cases(_mixed_cases())

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
    dialog.benchmark_results_panel.show_cases(_mixed_cases())
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
    dialog.benchmark_results_panel.show_cases(_mixed_cases())
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
    dialog.benchmark_results_panel.show_cases(
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
    """A finished case re-renders the table through `show_cases`."""
    dialog, app = _dialog()
    table = dialog.benchmark_results_table
    header = table.horizontalHeader()
    live: list[BenchmarkCase] = [_measured_case("beta", 0.5)]
    dialog.benchmark_results_panel.show_cases(list(live))
    rtf_column = _RESULT_HEADERS.index("RTF")
    header.sectionClicked.emit(rtf_column)
    header.sectionClicked.emit(rtf_column)

    live.append(_measured_case("Alpha", 0.1))
    dialog.benchmark_results_panel.show_cases(list(live))

    assert _column(table, rtf_column) == ["0.500", "0.100"]

    live.append(_measured_case("Delta", 0.9))
    dialog.benchmark_results_panel.show_cases(list(live))

    assert _column(table, rtf_column) == ["0.900", "0.500", "0.100"]
    assert _column(table, 0) == ["3", "1", "2"]
    _ = app


def _settle_table(app: QtWidgets.QApplication, table: QtWidgets.QTableWidget) -> None:
    """Pump until the table's scrollbars and section widths stop changing.

    When the rows do not fit, Qt shows the vertical scrollbar on a later
    event-loop pass than the one that added the rows, and the stretch column
    gives up the scrollbar's width at that moment. A geometry snapshot taken
    before it blames whatever happens next.
    """
    header = table.horizontalHeader()
    QtTest.QTest.qWait(100)
    previous: tuple[object, ...] | None = None
    for _ in range(50):
        app.processEvents()
        state = (
            table.verticalScrollBar().isVisible(),
            table.horizontalScrollBar().isVisible(),
            tuple(header.sectionSize(column) for column in range(table.columnCount())),
        )
        if state == previous:
            return
        previous = state


def test_clicking_the_results_header_moves_nothing():
    dialog, app = _dialog()
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    dialog.show()
    app.processEvents()
    # A dialog the 1024x768 CI runner can grant, and one that leaves the
    # results table 110 px: four 20 px rows under a 33 px header do not fit,
    # so the vertical scrollbar is part of the geometry under test. Measured
    # at this size: with one event-loop pass before the snapshot the first
    # click read as the stretch column losing 12 px (132 -> 120); settled
    # first, every click leaves all of it unchanged.
    dialog.resize(860, 700)
    app.processEvents()
    table = dialog.benchmark_results_table
    header = table.horizontalHeader()
    dialog.benchmark_results_panel.show_cases(_mixed_cases())
    _settle_table(app, table)
    assert table.verticalScrollBar().isVisible(), (
        "the rows fit, so the scrollbar case this test exists for is not measured"
    )

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
    # `_selected_benchmark_history_entry` reads the selection; an invalid
    # current cell clears it along with the current row.
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


def test_minimising_the_settings_dialog_keeps_the_benchmark_windows(tmp_path):
    """Qt sends a hideEvent for a minimise too, and a minimise is no dismissal.

    The Run Benchmark window and every pop-out were hidden there exactly as
    on a Close, and nothing re-showed them on the restore: minimising
    Settings once took them off screen for the rest of the session.
    """
    entry = _stored_entry("minimised run")
    dialog, app = _history_dialog(tmp_path, [entry])
    dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    dialog.show()
    dialog._open_benchmark_window()
    dialog._open_benchmark_results_window(entry)
    app.processEvents()
    popout = dialog._benchmark_result_windows[entry.identity_key()]
    assert dialog.benchmark_window.isVisible() and popout.isVisible()

    dialog.showMinimized()
    for _ in range(10):
        app.processEvents()
    if not dialog.isMinimized():
        pytest.skip("the window manager did not minimise the dialog")

    assert dialog.benchmark_window.isVisible() is True
    assert popout.isVisible() is True

    dialog.showNormal()
    for _ in range(10):
        app.processEvents()

    assert dialog.benchmark_window.isVisible() is True
    assert popout.isVisible() is True

    # A dismissal still takes both with it.
    dialog.reject()
    app.processEvents()

    assert dialog.benchmark_window.isVisible() is False
    assert popout.isVisible() is False
    _ = app


def test_deleting_an_entry_whose_window_is_hidden_drops_it_from_the_registry(
    monkeypatch, tmp_path
):
    """`QDialog.close()` rejects -- and so emits `finished` -- only while visible.

    A pop-out hidden with the settings dialog closed silently, so the key
    and the window stayed in the registry for the life of the app.
    """
    entry = _stored_entry("hidden run")
    dialog, app = _history_dialog(tmp_path, [entry])
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *_a, **_k: QtWidgets.QMessageBox.Yes),
    )
    dialog.benchmark_history_list.setCurrentRow(0)
    dialog._open_benchmark_results_window(entry)
    app.processEvents()
    dialog._hide_benchmark_window()
    window = dialog._benchmark_result_windows[entry.identity_key()]
    assert window.isVisible() is False

    dialog._delete_selected_benchmark_history()
    app.processEvents()

    assert dialog._benchmark_result_windows == {}
    _ = app


def test_clearing_the_history_drops_hidden_windows_too(monkeypatch, tmp_path):
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
    dialog._hide_benchmark_window()
    assert len(dialog._benchmark_result_windows) == 2

    dialog._clear_benchmark_history()
    app.processEvents()

    assert dialog._benchmark_result_windows == {}
    _ = app


def test_a_deselected_history_row_offers_no_actions(tmp_path):
    """A Ctrl+click on the selected row deselects it and leaves it current.

    The action row read `currentRow()`, so Open in Window and Delete
    Selected stayed enabled and acted on a row nothing showed as selected.
    """
    entry = _stored_entry("deselected run")
    dialog, app = _history_dialog(tmp_path, [entry])
    table = dialog.benchmark_history_list
    table.setCurrentRow(0)
    dialog._update_benchmark_history_actions()
    assert dialog.open_benchmark_history_window_button.isEnabled() is True
    assert dialog.delete_benchmark_history_button.isEnabled() is True

    table.selectionModel().clearSelection()
    dialog._update_benchmark_history_actions()

    assert table.currentRow() == 0
    assert dialog._selected_benchmark_history_entry() is None
    assert dialog.open_benchmark_history_window_button.isEnabled() is False
    assert dialog.delete_benchmark_history_button.isEnabled() is False
    _ = app


def test_a_created_stamp_the_local_clock_cannot_place_is_shown_as_is():
    """`astimezone` raises OSError at the ends of the datetime range, and a
    hand-edited stamp there kept the dialog from being built."""
    stamp = "0001-01-01T00:00:00+00:00"

    assert _benchmark_created_label(stamp) == stamp


def test_the_dialog_cannot_be_dragged_narrower_than_its_widest_tab():
    """The explicit 520 px minimum predates the third History action button.

    Between 520 px and the 611 px the Benchmark tab needs, every caption in
    that row was clipped -- and `minimumSizeHint` reports that width only
    with the tab current and painted, so the pin follows the tab switch. The
    budget keeps a later widget from raising the minimum unnoticed: one
    label once took the layout's minimum to 1109 px.
    """
    dialog, app = _dialog()
    dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    dialog.show()
    # Let the pin the show schedules fire on the first tab, so the one the
    # tab switch schedules is what the assertions below depend on.
    QtTest.QTest.qWait(50)
    app.processEvents()
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    QtTest.QTest.qWait(50)
    app.processEvents()
    needed = dialog.minimumSizeHint().width()

    point_size = app.font().pointSizeF()
    if point_size != 9.0:
        # Windows' "Text size" raises the application font without the DPI:
        # measured 720 px at 11.25 pt, 813 at 13.5 and 1025 at 18. The
        # budget is a 9 pt number, so on such a machine the test says what
        # it saw instead of failing on a healthy dialog.
        dialog.hide()
        pytest.skip(
            f"the 640 px budget was measured at 9 pt; this session runs at "
            f"{point_size} pt and the dialog needs {needed} px"
        )
    assert 520 < needed <= 640, needed
    assert dialog.minimumWidth() >= needed

    dialog.tabs.setCurrentIndex(0)
    dialog.resize(520, dialog.height())
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    QtTest.QTest.qWait(50)
    app.processEvents()

    assert dialog.width() == dialog.minimumWidth()
    row = _button_row_of(dialog.open_benchmark_history_window_button)
    assert len(row) >= 3
    for widget in row:
        assert widget.width() >= widget.sizeHint().width(), widget
    dialog.hide()
    _ = app


def test_a_dialog_opened_on_the_benchmark_tab_is_pinned_by_the_show():
    """A tab made current while the dialog is hidden pins nothing usable:
    the page reports its full width only once painted. The show has to
    measure again, or the minimum stays the tab bar's until the next tab
    switch."""
    dialog, app = _dialog()
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    QtTest.QTest.qWait(50)
    app.processEvents()
    hidden_minimum = dialog.minimumWidth()

    dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    dialog.show()
    QtTest.QTest.qWait(50)
    app.processEvents()
    needed = dialog.minimumSizeHint().width()

    assert needed > hidden_minimum
    assert dialog.minimumWidth() >= needed
    dialog.hide()
    _ = app


def test_the_benchmark_header_row_widgets_share_the_buttons_rendered_height():
    """The row's label and bar were pinned to the button's unpolished height.

    Measured before the button was a child of the styled dialog it reported
    26 px; polished, its QSS box makes it 34, so the label and the bar sat
    8 px short beside it.
    """
    dialog, app = _dialog()
    dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    dialog.show()
    app.processEvents()
    rendered = dialog.open_benchmark_window_button.sizeHint().height()

    assert rendered > 0
    assert dialog.benchmark_status_label.minimumHeight() == rendered
    assert dialog.benchmark_status_label.maximumHeight() == rendered
    assert dialog.benchmark_progress_bar.minimumHeight() == rendered
    assert dialog.benchmark_progress_bar.maximumHeight() == rendered
    dialog.hide()
    _ = app


def _let_the_pin_fire(app: QtWidgets.QApplication) -> None:
    QtTest.QTest.qWait(50)
    app.processEvents()


def test_a_transient_bottom_status_does_not_pin_the_dialogs_width():
    """The pin read the dialog's own hint, and the dialog's root layout holds
    the bottom status line, whose text after a failed save is the whole
    exception message -- 172 to 468 characters. A tab switch or a reopen
    inside the three seconds it showed pinned that width for the life of
    the app: measured, 3077 px on a 2560 px screen, with the message long
    gone and no way to drag the dialog back. The pin measures the tab
    widget, and nothing outside it can raise the minimum -- the engine line
    is the other root-level label.
    """
    dialog, app = _dialog()
    dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    dialog.show()
    _let_the_pin_fire(app)
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    _let_the_pin_fire(app)
    pinned = dialog.minimumWidth()
    assert pinned > 520

    dialog._set_bottom_status("Failed to save settings: " + "x" * 440, "#b71c1c")
    dialog.engine_indicator.setText("Engine: " + "y" * 400)
    dialog.tabs.setCurrentIndex(0)
    _let_the_pin_fire(app)
    dialog.hide()
    dialog.show()
    _let_the_pin_fire(app)
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    _let_the_pin_fire(app)

    assert dialog.minimumWidth() == pinned
    dialog._set_bottom_status("")
    dialog._update_engine_indicator()
    dialog.hide()
    _ = app


def test_the_pin_stops_at_the_screen(monkeypatch):
    """A minimum the screen cannot host puts Save and Close past its edge
    with no way back short of restarting the app: `setMinimumWidth` holds
    whatever `_apply_initial_dialog_size` fitted to the screen before it."""
    dialog, app = _dialog()
    monkeypatch.setattr(
        dialog, "_available_dialog_size", lambda: QtCore.QSize(580, 700)
    )
    dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    dialog.show()
    _let_the_pin_fire(app)
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    _let_the_pin_fire(app)

    assert dialog.minimumSizeHint().width() > 580
    assert dialog.minimumWidth() == 580
    dialog.hide()
    _ = app


def test_a_stored_case_whose_error_is_a_number_still_renders(tmp_path):
    """The results table hands a case's `error` to a tooltip, which takes a
    string only; a hand-edited number raised TypeError inside `show_entry`,
    which is Load Selected and Open in Window alike. The reader keeps such
    a value as its text."""
    dialog, app = _history_dialog(tmp_path, [_stored_entry("first run")])
    path = tmp_path / "benchmark_history.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[0]["cases"][0]["error"] = 42
    path.write_text(json.dumps(payload), encoding="utf-8")
    entry = BenchmarkHistoryStore(path=path).recent_entries(1)[0]

    dialog.benchmark_results_panel.show_entry(entry)

    status = dialog.benchmark_results_table.item(0, _BENCHMARK_RESULT_STATUS_COLUMN)
    assert status.toolTip() == "42"
    _ = app


def test_the_history_list_rates_a_run_by_its_measured_cases(tmp_path):
    """`min` over a NaN depends on the order the cases come in: a stored run
    whose first case had no numbers read "-" in the Best RTF column while
    its second case had measured 0.500."""
    template = _stored_entry("template")
    entry = BenchmarkHistoryEntry.new(
        status="completed",
        summary="two cases",
        options=template.options,
        cases=[_measured_case("alpha", math.nan), _measured_case("beta", 0.5)],
    )
    dialog, app = _history_dialog(tmp_path, [entry])

    assert dialog.benchmark_history_list.item(0, 4).text() == "0.500"
    _ = app
