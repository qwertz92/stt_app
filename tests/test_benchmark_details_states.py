"""What the Results "Details" overview says in each state of a run."""

from __future__ import annotations

from test_benchmark_run_progress import _armed_dialog, _case, _dialog, _options

from stt_app.benchmark_environment import BenchmarkEnvironment
from stt_app.benchmark_history import BenchmarkHistoryEntry
from stt_app.local_benchmark import BenchmarkCancelled
from stt_app.settings_dialog_benchmark import _benchmark_created_label


def _overview(dialog) -> dict[str, str]:
    table = dialog.benchmark_summary_text.overview_table
    return {
        table.item(row, 0).text(): table.item(row, 1).text()
        for row in range(table.rowCount())
    }


def test_the_details_overview_shows_the_recorded_time_as_the_history_list_does(
    tmp_path,
):
    """The History list read "2026-10-03 22:27" (local) and the Details
    overview of the same run "2026-10-03T20:27:07+00:00" (UTC): two hours
    apart for one run, in two formats."""
    dialog, app = _dialog(tmp_path, ["small"])
    entry = BenchmarkHistoryEntry.new(
        status="completed",
        summary="Benchmark summary:\nsmall",
        options=_options(["small"]),
        cases=[_case("small", "cpu")],
        environment=BenchmarkEnvironment(),
    )

    dialog.benchmark_results_panel.show_entry(entry)

    assert _overview(dialog)["Recorded"] == _benchmark_created_label(entry.created_at)
    _ = app


def test_a_starting_run_reads_running_in_the_overview(monkeypatch, tmp_path):
    """The whole multi-line text summary went into the one Status cell, which
    shows its first line: "No benchmark results available." for a run that
    had just started."""
    seen: list[dict[str, str]] = []

    def _fake_run(**kwargs):
        seen.append(_overview(dialog))
        return [_case("small", "cpu")]

    monkeypatch.setattr("stt_app.settings_dialog.run_benchmark_cases", _fake_run)
    dialog, app = _armed_dialog(monkeypatch, tmp_path, ["small"])

    dialog._run_local_benchmark()

    assert seen, "the fake runner was not reached"
    assert seen[0]["Status"] == "Running"
    assert seen[0]["Completed cases"] == "0"
    for value in seen[0].values():
        assert "\n" not in value, value
    _ = app


def test_a_run_canceled_before_any_case_says_so_in_the_overview(monkeypatch, tmp_path):
    def _fake_run(**kwargs):
        raise BenchmarkCancelled("canceled")

    monkeypatch.setattr("stt_app.settings_dialog.run_benchmark_cases", _fake_run)
    dialog, app = _armed_dialog(monkeypatch, tmp_path, ["small"])

    dialog._run_local_benchmark()
    app.processEvents()

    overview = _overview(dialog)
    assert overview["Status"] == "Canceled"
    assert overview["Result"] == "No case finished. Nothing was saved."
    for value in overview.values():
        assert "\n" not in value, value
    # The text summary is still what the view holds as its plain text.
    assert "Benchmark details:" in dialog.benchmark_summary_text.toPlainText()
    _ = app


def test_the_live_overview_points_at_the_transcripts_tab(tmp_path):
    """It said the transcripts were "available below"; below is the
    results table, and they are on the Transcripts tab."""
    dialog, app = _dialog(tmp_path, ["small"])

    dialog.benchmark_results_panel.show_live("summary", [_case("small", "cpu")])

    hint = _overview(dialog)["Transcripts"]
    assert "Transcripts tab" in hint
    assert "below" not in hint
    _ = app
