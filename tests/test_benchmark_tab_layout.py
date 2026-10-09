"""Benchmark tab geometry: what fits on a small screen, what is cut off."""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtTest, QtWidgets
from test_benchmark_results_ux import _dialog, _measured_case, _settle_table
from test_settings_dialog_general_ux import _AppFont

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
    chrome = dialog.height() - page.height()
    point_size = app.font().pointSizeF()
    if point_size != 9.0:
        dialog.hide()
        pytest.skip(
            f"the {_SMALL_SCREEN_DIALOG_HEIGHT} px budget is a 9 pt number; this "
            f"session runs at {point_size} pt and the page needs "
            f"{page.minimumSizeHint().height()} px plus {chrome} px around it"
        )

    assert page.minimumSizeHint().height() + chrome <= _SMALL_SCREEN_DIALOG_HEIGHT

    dialog.resize(dialog.width(), _SMALL_SCREEN_DIALOG_HEIGHT)
    QtTest.QTest.qWait(50)
    app.processEvents()

    for button in (
        dialog.clear_benchmark_results_button,
        dialog.open_benchmark_results_window_button,
        dialog.export_benchmark_results_button,
        dialog.delete_benchmark_history_button,
        dialog.clear_benchmark_history_button,
    ):
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
