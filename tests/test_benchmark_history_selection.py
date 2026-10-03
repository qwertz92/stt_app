"""Benchmark History is a master list: the selected run is the one shown.

Before, a row had to be selected and then loaded (Load Selected or a
double-click), so the History selection and the Results view could name two
different runs, each with its own Export and Open in Window buttons; and a
deleted or cleared run stayed on screen with every action disabled.
"""

from __future__ import annotations

import threading

from PySide6 import QtCore, QtTest, QtWidgets
from test_benchmark_results_ux import _history_dialog, _stored_entry


def _two_runs(tmp_path):
    first = _stored_entry("first run")
    second = _stored_entry("second run")
    # `identity_key` is (created_at, status, summary); this makes them differ.
    second.summary = "Benchmark summary:\nthe other run"
    dialog, app = _history_dialog(tmp_path, [first, second])
    return dialog, app, first, second


def _overview_status(dialog) -> str:
    table = dialog.benchmark_summary_text.overview_table
    for row in range(table.rowCount()):
        if table.item(row, 0).text() == "Status":
            return table.item(row, 1).text()
    return ""


def _yes(monkeypatch) -> None:
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *_a, **_k: QtWidgets.QMessageBox.Yes),
    )


def test_selecting_a_history_row_shows_that_run(tmp_path):
    dialog, app, first, second = _two_runs(tmp_path)
    table = dialog.benchmark_history_list

    # Newest first: row 0 is the second run.
    table.setCurrentRow(1)

    assert dialog._current_benchmark_entry.identity_key() == first.identity_key()
    assert dialog.benchmark_summary_text.toPlainText() == first.summary
    assert dialog.export_benchmark_results_button.isEnabled() is True

    table.setCurrentRow(0)

    assert dialog._current_benchmark_entry.identity_key() == second.identity_key()
    assert dialog.benchmark_summary_text.toPlainText() == second.summary
    assert not hasattr(dialog, "load_benchmark_history_button")
    _ = app


def test_a_selection_never_moves_the_history_results_splitter(tmp_path):
    """Every load reset the splitter to [220, 420], and a selection is now a
    load: a splitter dragged to [324, 280] snapped to [208, 396] on one
    Down press."""
    dialog, app, _first, _second = _two_runs(tmp_path)
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    dialog.resize(860, 900)
    dialog.show()
    app.processEvents()
    splitter = dialog.benchmark_main_splitter
    splitter.setSizes([400, 300])
    app.processEvents()
    dragged = splitter.sizes()

    dialog.benchmark_history_list.setCurrentRow(0)
    app.processEvents()
    dialog.benchmark_history_list.setCurrentRow(1)
    app.processEvents()

    assert dialog._current_benchmark_entry is not None
    assert splitter.sizes() == dragged
    dialog.hide()


def test_a_selection_during_a_run_leaves_the_live_results_alone(tmp_path):
    dialog, app, _first, _second = _two_runs(tmp_path)
    live = list(_stored_entry("live run").cases)
    dialog._current_benchmark_cases = live
    dialog._active_benchmark_thread = threading.Thread(target=lambda: None)

    dialog.benchmark_history_list.setCurrentRow(0)

    assert dialog._current_benchmark_cases is live
    assert dialog._current_benchmark_entry is None
    # Open in Window still reads the selected row without touching the view.
    assert dialog.open_benchmark_history_window_button.isEnabled() is True
    dialog._active_benchmark_thread = None
    _ = app


def test_a_finished_run_keeps_its_status_line_when_its_row_is_selected(tmp_path):
    """The finish selects the new history row, and that selection must not
    replace "Benchmark finished and saved to history." with a load message."""
    dialog, app, _first, _second = _two_runs(tmp_path)
    entry = _stored_entry("fresh run")
    entry.summary = "Benchmark summary:\nfresh"

    dialog._on_benchmark_finished(
        True,
        entry.summary,
        {"cases": entry.cases, "options": entry.options, "status": "completed"},
    )

    assert dialog.benchmark_status_label.text().startswith(
        "Benchmark finished and saved to history."
    )
    selected = dialog._selected_benchmark_history_entry()
    assert selected is not None
    assert selected.summary == entry.summary
    _ = app


def _finish_with_a_failed_save(monkeypatch, dialog):
    """A run whose history write is refused: Results holds the only copy."""
    fresh = _stored_entry("unsaved run")
    fresh.summary = "Benchmark summary:\nunsaved"

    def _refuse(_entry):
        raise OSError("disk full")

    monkeypatch.setattr(dialog._benchmark_history_store, "add_entry", _refuse)
    dialog._on_benchmark_finished(
        True,
        fresh.summary,
        {"cases": fresh.cases, "options": fresh.options, "status": "completed"},
    )
    assert "history could not be saved" in dialog.benchmark_status_label.text()
    return dialog._current_benchmark_entry


def _answer(monkeypatch, answer) -> list[str]:
    asked: list[str] = []

    def _question(_parent, _title, text, *_args, **_kwargs):
        asked.append(text)
        return answer

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(_question))
    return asked


def _click_row(dialog, row: int) -> None:
    """One user click on a History row, as the mouse delivers it."""
    table = dialog.benchmark_history_list
    rect = table.visualRect(table.model().index(row, 0))
    QtTest.QTest.mouseClick(table.viewport(), QtCore.Qt.LeftButton, pos=rect.center())


def test_a_selection_asks_before_replacing_a_result_that_was_not_saved(
    monkeypatch, tmp_path
):
    """When the history write failed at the finish, Results holds the run's
    only copy (Export Loaded can still save it); one click on any row
    replaced it silently."""
    dialog, app, _first, second = _two_runs(tmp_path)
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    dialog.show()
    app.processEvents()
    unsaved = _finish_with_a_failed_save(monkeypatch, dialog)
    asked = _answer(monkeypatch, QtWidgets.QMessageBox.No)

    _click_row(dialog, 0)

    assert len(asked) == 1
    assert "not saved" in asked[0]
    assert dialog._current_benchmark_entry is unsaved
    # Keep restores the selection as it was: nothing, the run is not a row.
    assert dialog._selected_benchmark_history_entry() is None

    asked = _answer(monkeypatch, QtWidgets.QMessageBox.Yes)
    _click_row(dialog, 0)

    assert len(asked) == 1
    assert dialog._current_benchmark_entry.identity_key() == second.identity_key()

    # A saved run is replaced without asking.
    asked = _answer(monkeypatch, QtWidgets.QMessageBox.No)
    _click_row(dialog, 1)

    assert asked == []
    dialog.hide()
    _ = app


def test_with_history_and_nothing_shown_the_results_say_how_to_show_a_run(
    tmp_path,
):
    """After Clear Loaded the Results area was two empty tables, and Clear
    Loaded stayed enabled with nothing to clear."""
    dialog, app, _first, _second = _two_runs(tmp_path)

    assert dialog.clear_benchmark_results_button.isEnabled() is False
    assert _overview_status(dialog).startswith("Select a run in Benchmark History")

    dialog.benchmark_history_list.setCurrentRow(0)
    assert dialog.clear_benchmark_results_button.isEnabled() is True

    dialog.clear_benchmark_results_button.click()

    assert dialog._selected_benchmark_history_entry() is None
    assert dialog.benchmark_results_table.rowCount() == 0
    assert _overview_status(dialog).startswith("Select a run in Benchmark History")
    assert dialog.clear_benchmark_results_button.isEnabled() is False
    _ = app


def test_deleting_the_shown_run_takes_it_off_the_results(monkeypatch, tmp_path):
    """The deleted run stayed in Results, Export and Open in Window disabled,
    as if it still existed but could not be used."""
    dialog, app, _first, second = _two_runs(tmp_path)
    _yes(monkeypatch)
    dialog.benchmark_history_list.setCurrentRow(0)
    assert dialog._current_benchmark_entry.identity_key() == second.identity_key()

    dialog._delete_selected_benchmark_history()

    assert dialog._current_benchmark_entry is None
    assert dialog.benchmark_results_table.rowCount() == 0
    assert dialog.benchmark_summary_text.toPlainText() == ""
    assert _overview_status(dialog).startswith("Select a run in Benchmark History")
    _ = app


def test_clearing_the_history_takes_the_shown_run_off_the_results(
    monkeypatch, tmp_path
):
    dialog, app, _first, _second = _two_runs(tmp_path)
    _yes(monkeypatch)
    dialog.benchmark_history_list.setCurrentRow(0)

    dialog._clear_benchmark_history()

    assert dialog.benchmark_results_table.rowCount() == 0
    assert _overview_status(dialog).startswith("No benchmark yet.")
    _ = app
