"""Benchmark tab geometry: what fits on a small screen, what is cut off."""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtTest, QtWidgets
from test_benchmark_results_ux import _dialog

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
    ):
        assert _bottom_in(page, button) <= page.height(), button.text()
    details_bottom = _bottom_in(page, dialog.benchmark_summary_text)
    actions_top = dialog.clear_benchmark_results_button.mapTo(page, QtCore.QPoint()).y()
    assert details_bottom <= actions_top
    dialog.hide()
