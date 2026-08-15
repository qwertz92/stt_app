"""The benchmark surface stylesheets must stay scoped to widget types.

An unscoped property block (``border: 1px; border-radius: 4px``) is inherited
by every child widget, so each header section and the corner button drew its
own rounded box. That is what the tab looked like before these rules gained
selectors, and it is easy to reintroduce by editing the string.
"""

from __future__ import annotations

import pytest
from PySide6 import QtWidgets

from stt_app.settings_dialog_benchmark import (
    _BENCHMARK_DETAILS_STYLESHEET,
    _BENCHMARK_RESULT_SURFACE_STYLESHEET,
    _BenchmarkDetailsView,
)


@pytest.mark.parametrize(
    "stylesheet",
    [_BENCHMARK_RESULT_SURFACE_STYLESHEET, _BENCHMARK_DETAILS_STYLESHEET],
    ids=["results-surface", "details-surface"],
)
def test_surface_rules_are_scoped_to_a_selector(stylesheet: str) -> None:
    # Every declaration must live inside a `Selector { ... }` block, so the
    # text before the first brace is a selector. A selector never contains a
    # semicolon, while a property declaration always does — unlike `:`, which
    # a pseudo-element selector such as `QTabWidget::pane` uses legitimately.
    head = stylesheet.split("{", 1)[0]

    assert ";" not in head, f"unscoped property block in: {head!r}"
    assert "Q" in head, "the first rule has no widget-type selector"


def test_header_sections_are_styled_flat_without_their_own_box() -> None:
    for stylesheet in (
        _BENCHMARK_RESULT_SURFACE_STYLESHEET,
        _BENCHMARK_DETAILS_STYLESHEET,
    ):
        assert "QHeaderView::section" in stylesheet
        assert "QTableCornerButton::section" in stylesheet


def test_details_tables_do_not_draw_a_second_frame_inside_the_pane() -> None:
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = _BenchmarkDetailsView()

    # The tab pane draws the frame; the views inside must not add one.
    assert view.overview_table.frameShape() == QtWidgets.QFrame.NoFrame
    assert view.transcripts_table.frameShape() == QtWidgets.QFrame.NoFrame
    # Content is wrapped so it does not sit flush against the pane border.
    assert view.widget(0) is not view.overview_table
    assert view.overview_table.parent() is not view
