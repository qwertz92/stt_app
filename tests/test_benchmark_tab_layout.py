"""Benchmark tab geometry: what fits on a small screen, what is cut off."""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtTest, QtWidgets
from test_benchmark_results_ux import _dialog, _measured_case, _settle_table
from test_settings_dialog_general_ux import _AppFont

from stt_app.benchmark_history import (
    BenchmarkHistoryEntry,
    BenchmarkHistoryStore,
    BenchmarkOptions,
)
from stt_app.settings_dialog_helpers import _DEFAULT_SETTINGS_DIALOG_SIZE

# A 1366x768 screen at 100% with a 40 px taskbar leaves 728 px, and the dialog
# keeps a 48 px screen margin (`_DIALOG_SCREEN_MARGIN`), so it opens at most
# 680 px tall there.
_SMALL_SCREEN_DIALOG_HEIGHT = 680


def _shown_benchmark_tab():
    dialog, app = _dialog()
    dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    dialog.show()
    QtTest.QTest.qWait(50)
    app.processEvents()
    return dialog, app


def _bottom_in(page: QtWidgets.QWidget, widget: QtWidgets.QWidget) -> int:
    return widget.mapTo(page, QtCore.QPoint(0, widget.height())).y()


def test_the_benchmark_tab_fits_a_dialog_on_a_small_screen():
    """The page asked for 675 px, which is 806 px of dialog: on a 680 px
    dialog the Results action row (Clear Loaded, Open in Window, Export
    Loaded) lay entirely below the visible page, and on 700 px the details
    view overlapped it by 9 px."""
    dialog, app = _shown_benchmark_tab()
    page = dialog.tabs.widget(dialog._benchmark_tab_index)
    content_height = page.widget().minimumSizeHint().height()
    chrome = dialog.height() - page.height()
    point_size = app.font().pointSizeF()
    if point_size != 9.0:
        dialog.hide()
        pytest.skip(
            f"the {_SMALL_SCREEN_DIALOG_HEIGHT} px budget is a 9 pt number; this "
            f"session runs at {point_size} pt and the page needs "
            f"{content_height} px plus {chrome} px around it"
        )

    assert content_height + chrome <= _SMALL_SCREEN_DIALOG_HEIGHT

    dialog.resize(dialog.width(), _SMALL_SCREEN_DIALOG_HEIGHT)
    QtTest.QTest.qWait(50)
    app.processEvents()

    # Fits without scrolling: the page needs less than the dialog gives it.
    assert page.verticalScrollBar().isVisible() is False
    for button in _action_buttons(dialog):
        assert _bottom_in(page, button) <= page.height(), button.text()
    details_bottom = _bottom_in(page, dialog.benchmark_summary_text)
    actions_top = dialog.clear_benchmark_results_button.mapTo(page, QtCore.QPoint()).y()
    assert details_bottom <= actions_top
    dialog.hide()


_LONG_MODEL_IDS = (
    "granite-speech-5.0-470m-turboctc",
    "cohere-transcribe-03-2026",
    "parakeet-tdt-0.6b-v3",
)


def _model_column_need(table: QtWidgets.QTableWidget, text: str) -> int:
    # The item's text plus the cell's own margins on both sides.
    margin = table.style().pixelMetric(QtWidgets.QStyle.PM_FocusFrameHMargin) + 1
    return table.fontMetrics().horizontalAdvance(text) + 2 * margin


def test_the_results_table_shows_long_model_names_whole_at_the_default_width():
    """Every column was 100 px wide except the last, Status, which took all
    the room left: "OK" sat in a 190 px cell while
    "granite-speech-5.0-470m-turboctc" (183 px of text) was cut to 100 px in
    the Model column. At the default 860 px the Model column now holds it
    whole (191 px); at the 801 px minimum it gets 185 px and the cell's
    tooltip carries the name."""
    dialog, app = _shown_benchmark_tab()
    if app.font().pointSizeF() != 9.0:
        dialog.hide()
        pytest.skip("the 860 px width was measured against 9 pt text")
    dialog.resize(_DEFAULT_SETTINGS_DIALOG_SIZE.width(), dialog.height())
    table = dialog.benchmark_results_table
    dialog.benchmark_results_panel.show_cases(
        [_measured_case(model, 0.05) for model in _LONG_MODEL_IDS]
    )
    _settle_table(app, table)

    width = table.columnWidth(1)
    for row, model in enumerate(_LONG_MODEL_IDS):
        assert width >= _model_column_need(table, model), (model, width)
        assert table.item(row, 1).toolTip() == model
    dialog.hide()


def test_a_live_case_with_long_numbers_moves_no_results_column():
    """The short columns size to their header, which is wider than any value
    they hold, so a case finishing mid-run cannot shift the columns."""
    dialog, app = _shown_benchmark_tab()
    table = dialog.benchmark_results_table
    header = table.horizontalHeader()
    panel = dialog.benchmark_results_panel
    first = _measured_case("tiny", 0.05)
    panel.show_cases([first])
    _settle_table(app, table)
    before = [header.sectionSize(column) for column in range(table.columnCount())]

    slow = _measured_case("large-v3", 99.0, device="webgpu")
    slow.compute_type = "onnx-int8"
    panel.show_cases([first, slow])
    _settle_table(app, table)

    assert [header.sectionSize(c) for c in range(table.columnCount())] == before
    dialog.hide()


def _action_buttons(dialog) -> list[QtWidgets.QPushButton]:
    return [
        dialog.open_benchmark_results_window_button,
        dialog.export_benchmark_results_button,
        dialog.clear_benchmark_results_button,
        dialog.delete_benchmark_history_button,
        dialog.clear_benchmark_history_button,
    ]


@pytest.mark.pixel_exact
@pytest.mark.parametrize("point_size", [9.0, 11.25, 13.5])
def test_the_action_row_fits_the_minimum_width_and_its_buttons_keep_their_width(
    point_size: float,
):
    """One row holds the five actions that two rows held. At the dialog's
    minimum width no caption may be cut, the row must stay inside the page,
    and a button changing state (enabled, disabled, a run shown or active)
    must not change its width or position."""
    with _AppFont(point_size) as app:
        dialog, _ = _dialog()
        dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
        dialog.show()
        QtTest.QTest.qWait(50)
        app.processEvents()
        dialog.resize(dialog.minimumWidth(), dialog.height())
        QtTest.QTest.qWait(50)
        app.processEvents()
        page = dialog.tabs.widget(dialog._benchmark_tab_index)
        buttons = _action_buttons(dialog)

        def geometry() -> list[tuple[int, int, int]]:
            return [
                (b.mapTo(page, QtCore.QPoint()).x(), b.width(), b.height())
                for b in buttons
            ]

        idle = geometry()
        for button in buttons:
            assert button.width() >= button.sizeHint().width(), button.text()
        assert idle[-1][0] + idle[-1][1] <= page.width()
        assert [x for x, _w, _h in idle] == sorted(x for x, _w, _h in idle)

        dialog.benchmark_results_panel.show_cases([_measured_case("tiny", 0.05)])
        dialog._current_benchmark_cases = [_measured_case("tiny", 0.05)]
        dialog._update_benchmark_action_row()
        shown = geometry()
        dialog._active_benchmark_thread = object()
        dialog._update_benchmark_action_row()
        running = geometry()
        dialog._active_benchmark_thread = None
        dialog.hide()

    assert idle == shown == running


def test_the_action_row_sits_under_the_splitter_not_inside_a_box():
    """Dragging the History/Results splitter must not move the row, and the
    two boxes carry no buttons of their own."""
    dialog, app = _shown_benchmark_tab()
    page = dialog.tabs.widget(dialog._benchmark_tab_index)
    splitter = dialog.benchmark_main_splitter
    before = _action_buttons(dialog)[0].mapTo(page, QtCore.QPoint()).y()
    assert _bottom_in(page, splitter) <= before

    splitter.setSizes([splitter.height() // 3, 2 * splitter.height() // 3])
    QtTest.QTest.qWait(50)
    app.processEvents()

    assert _action_buttons(dialog)[0].mapTo(page, QtCore.QPoint()).y() == before
    for box_widget in (dialog.benchmark_history_list, dialog.benchmark_results_table):
        box = box_widget.parentWidget()
        while not isinstance(box, QtWidgets.QGroupBox):
            box = box.parentWidget()
        assert box.findChildren(QtWidgets.QPushButton) == []
    dialog.hide()


@pytest.mark.pixel_exact
@pytest.mark.parametrize("point_size", [9.0, 11.25, 13.5])
def test_a_short_dialog_scrolls_the_benchmark_tab_instead_of_squeezing_it(
    point_size: float,
):
    """The tab squeezed its tables down to their minimums and then clipped the
    action row for any dialog shorter than the page's minimum height (673 px at
    9 pt). It is a scroll area now: from that height down the page keeps the
    minimums, the vertical bar appears and reaches the row, and there is never
    a horizontal bar, at the narrowest dialog too. Above it nothing scrolls."""
    with _AppFont(point_size) as app:
        dialog, _ = _dialog()
        dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
        dialog.show()
        QtTest.QTest.qWait(50)
        app.processEvents()
        page = dialog.tabs.widget(dialog._benchmark_tab_index)
        assert isinstance(page, QtWidgets.QScrollArea)
        assert page.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarAlwaysOff
        # With every tab a scroll area the minimum width must still be the tab
        # bar's: the pane frame was once read as 61 px (the bar's hint minus
        # the stack's) instead of the 6 it is, and the dialog got 55 px wider.
        tabs = dialog.tabs
        frame = tabs.width() - tabs.currentWidget().parentWidget().width()
        margins = dialog.layout().contentsMargins()
        assert dialog.minimumWidth() == (
            tabs.tabBar().sizeHint().width() + frame + margins.left() + margins.right()
        )
        content = page.widget()
        needed = content.minimumSizeHint().height()
        chrome = dialog.height() - page.viewport().height()
        results = dialog.benchmark_results_table
        row = dialog.open_benchmark_results_window_button

        def settle(height: int) -> None:
            dialog.resize(dialog.minimumWidth(), height)
            QtTest.QTest.qWait(50)
            app.processEvents()

        settle(needed + chrome + 40)
        roomy = page.verticalScrollBar().isVisible()
        settle(needed + chrome - 40)
        short_heights = (
            dialog.benchmark_history_list.height(),
            results.height(),
            dialog.benchmark_summary_text.height(),
        )
        short_bar = page.verticalScrollBar().isVisible()
        settle(dialog.minimumHeight())
        shortest = page.verticalScrollBar().maximum()
        horizontal = (
            page.horizontalScrollBar().isVisible(),
            page.horizontalScrollBar().maximum(),
        )
        page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
        app.processEvents()
        row_bottom = row.mapTo(page.viewport(), QtCore.QPoint(0, row.height())).y()
        row_right = max(
            button.mapTo(page.viewport(), QtCore.QPoint(button.width(), 0)).x()
            for button in _action_buttons(dialog)
        )
        viewport = page.viewport().size()
        dialog.hide()

    assert roomy is False
    assert short_bar is True
    assert short_heights[0] >= dialog.benchmark_history_list.minimumHeight()
    assert short_heights[2] >= 120
    assert shortest > 0
    assert horizontal == (False, 0)
    assert row_bottom <= viewport.height()
    assert row_right <= viewport.width()


def _run_over(models: list[str], *, status: str, day: int, selected=None):
    """A stored run that measured *models*; *selected* is what it started with."""
    entry = BenchmarkHistoryEntry.new(
        status=status,
        summary=f"run on day {day}",
        options=BenchmarkOptions(
            audio_path="C:/sample.wav",
            audio_name="sample.wav",
            model_names=list(selected if selected is not None else models),
            device="auto",
            compute_type="int8",
            webgpu_devices=["auto"],
            runs=1,
            beam_size=5,
            language="auto",
            vad_filter=False,
            warmup=True,
            threads=0,
        ),
        cases=[_measured_case(model, 0.05) for model in models],
    )
    entry.created_at = f"2026-10-{day:02d}T10:00:00+00:00"
    return entry


_TWELVE_MODELS = [
    "tiny",
    "base",
    "small",
    "medium",
    "large-v3",
    "large-v3-turbo",
    "distil-large-v3.5",
    "parakeet-tdt-0.6b-v3",
    "canary-1b-v2",
    "cohere-transcribe-03-2026",
    "granite-speech-5.0-470m-turboctc",
    "nemotron-3.5-asr-0.6b",
]


def test_the_history_models_cell_counts_the_models_the_run_measured(tmp_path):
    """The column listed `options.model_names` whole (1006 px of text for
    twelve models, cut at 189 px with nothing to say how many there were), and
    a canceled run named the models it never reached. The cell now leads with
    the count of models that have a stored case; the tooltip lists them."""
    canceled = _run_over(
        ["tiny", "base"],
        status="canceled",
        day=3,
        selected=["tiny", "base", "small", "medium"],
    )
    one = _run_over(["tiny"], status="completed", day=2)
    twelve = _run_over(_TWELVE_MODELS, status="completed", day=1)
    dialog, app = _dialog()
    dialog._benchmark_history_store = BenchmarkHistoryStore(
        path=tmp_path / "benchmark_history.json"
    )
    for entry in (twelve, one, canceled):
        dialog._benchmark_history_store.add_entry(entry)
    dialog._refresh_benchmark_history_list()
    table = dialog.benchmark_history_list

    assert table.item(0, 2).text() == "2 models: tiny, base"
    assert table.item(1, 2).text() == "1 model: tiny"
    assert table.item(2, 2).text() == "12 models: " + ", ".join(_TWELVE_MODELS)
    assert table.item(2, 2).toolTip() == "12 models:\n" + "\n".join(_TWELVE_MODELS)
    assert table.textElideMode() == QtCore.Qt.ElideRight
    _ = app


@pytest.mark.pixel_exact
@pytest.mark.parametrize("point_size", [9.0, 11.25, 13.5])
def test_no_history_column_changes_width_when_rows_arrive(point_size: float, tmp_path):
    """Recorded, Runs, Best RTF and Status were sized to their contents, so
    the first "Completed with errors" took 59 px from the Audio and Models
    columns (9 pt) and the next date, 20 px more; and the vertical bar that
    came with the row that outgrew the list took 12 px. Measured at the
    dialog's minimum width with five very different runs and then 20 rows."""
    with _AppFont(point_size) as app:
        dialog, _ = _dialog()
        dialog._benchmark_history_store = BenchmarkHistoryStore(
            path=tmp_path / "benchmark_history.json"
        )
        dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
        dialog.show()
        QtTest.QTest.qWait(50)
        app.processEvents()
        dialog.resize(dialog.minimumWidth(), 760)
        QtTest.QTest.qWait(50)
        app.processEvents()
        table = dialog.benchmark_history_list
        header = table.horizontalHeader()

        def widths() -> list[int]:
            QtTest.QTest.qWait(30)
            app.processEvents()
            return [header.sectionSize(c) for c in range(table.columnCount())]

        empty = widths()
        seen = [empty]
        for day, (status, models) in enumerate(
            (
                ("completed", ["tiny"]),
                ("completed", _TWELVE_MODELS[:3]),
                ("completed_with_errors", _TWELVE_MODELS),
                ("canceled", _TWELVE_MODELS[:2]),
                ("failed", _TWELVE_MODELS),
            ),
            start=1,
        ):
            dialog._benchmark_history_store.add_entry(
                _run_over(models, status=status, day=day)
            )
            dialog._refresh_benchmark_history_list()
            seen.append(widths())
        for day in range(6, 26):
            dialog._benchmark_history_store.add_entry(
                _run_over(["tiny"], status="completed", day=day)
            )
        dialog._refresh_benchmark_history_list()
        seen.append(widths())
        metrics = table.fontMetrics()
        margin = 2 * (
            table.style().pixelMetric(QtWidgets.QStyle.PM_FocusFrameHMargin) + 1
        )
        status_need = metrics.horizontalAdvance("Completed with errors") + margin
        recorded_need = metrics.horizontalAdvance("2026-10-30 23:59") + margin
        header_needs = [
            header.sectionSizeFromContents(c).width()
            for c in range(table.columnCount())
        ]
        dialog.hide()

    assert all(sizes == empty for sizes in seen), seen
    assert empty[5] >= status_need
    assert empty[0] >= recorded_need
    for column in (0, 3, 4, 5):
        assert empty[column] >= header_needs[column], column
