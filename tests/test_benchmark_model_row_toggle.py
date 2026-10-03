"""A click anywhere on a Run Benchmark model row toggles its checkbox once.

The list is checkable with no selection, so a click beside the checkbox did
nothing; whole-row toggling must not also fire for a click on the checkbox
itself, which the item delegate already toggles (twice would be no change).
"""

from __future__ import annotations

from PySide6 import QtCore, QtTest, QtWidgets
from test_benchmark_run_progress import _dialog


def _indicator_rect(models: QtWidgets.QListWidget, row: int) -> QtCore.QRect:
    """Where the style draws the row's check indicator, in viewport pixels."""
    option = QtWidgets.QStyleOptionViewItem()
    option.initFrom(models.viewport())
    option.rect = models.visualRect(models.model().index(row, 0))
    option.features |= QtWidgets.QStyleOptionViewItem.HasCheckIndicator
    return models.style().subElementRect(
        QtWidgets.QStyle.SE_ItemViewItemCheckIndicator, option, models
    )


def _shown_list(tmp_path, models_names):
    dialog, app = _dialog(tmp_path, models_names)
    dialog.benchmark_window.show()
    app.processEvents()
    models = dialog.benchmark_models_list
    changes: list[str] = []
    models.itemChanged.connect(lambda item: changes.append(item.text()))
    return dialog, app, models, changes


def _click(models, point: QtCore.QPoint) -> None:
    QtTest.QTest.mouseClick(models.viewport(), QtCore.Qt.LeftButton, pos=point)


def test_a_click_on_the_checkbox_toggles_the_row_once(tmp_path):
    dialog, app, models, changes = _shown_list(tmp_path, ["small", "base"])
    try:
        indicator = _indicator_rect(models, 0)
        assert models.visualRect(models.model().index(0, 0)).contains(indicator)

        _click(models, indicator.center())

        assert models.item(0).checkState() == QtCore.Qt.Unchecked
        assert len(changes) == 1
        assert models.item(1).checkState() == QtCore.Qt.Checked
    finally:
        dialog.benchmark_window.close()
        _ = app


def test_a_click_on_the_label_or_the_row_end_toggles_the_row_once(tmp_path):
    dialog, app, models, changes = _shown_list(tmp_path, ["small", "base"])
    try:
        row_rect = models.visualRect(models.model().index(1, 0))
        indicator = _indicator_rect(models, 1)
        label_point = QtCore.QPoint(indicator.right() + 12, row_rect.center().y())
        end_point = QtCore.QPoint(row_rect.right() - 3, row_rect.center().y())

        _click(models, label_point)
        assert models.item(1).checkState() == QtCore.Qt.Unchecked
        assert len(changes) == 1

        _click(models, end_point)
        assert models.item(1).checkState() == QtCore.Qt.Checked
        assert len(changes) == 2
        assert dialog._selected_benchmark_model_names() == ["small", "base"]
    finally:
        dialog.benchmark_window.close()
        _ = app


def test_a_click_below_the_last_row_changes_nothing(tmp_path):
    dialog, app, models, changes = _shown_list(tmp_path, ["small"])
    try:
        last = models.visualRect(models.model().index(0, 0))
        below = QtCore.QPoint(last.center().x(), last.bottom() + 6)
        assert models.itemAt(below) is None

        _click(models, below)

        assert models.item(0).checkState() == QtCore.Qt.Checked
        assert changes == []
    finally:
        dialog.benchmark_window.close()
        _ = app
