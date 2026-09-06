"""The Run Benchmark case list and the Benchmark tab's run progress bar."""
from __future__ import annotations

import threading
import time

from PySide6 import QtWidgets
from test_settings_dialog_connection import (
    _FakeLogger,
    _FakeSecretStore,
    _FakeSettingsStore,
)

from stt_app.benchmark_environment import BenchmarkEnvironment
from stt_app.benchmark_history import BenchmarkHistoryStore, BenchmarkOptions
from stt_app.local_benchmark import BenchmarkCancelled, BenchmarkCase, BenchmarkRun
from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_store import AppSettings

_PLAN_HEADERS = ["#", "Model", "Device", "Compute", "Status"]


def _dialog(tmp_path, models: list[str]):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._benchmark_history_store = BenchmarkHistoryStore(
        path=tmp_path / "benchmark_history.json"
    )
    dialog._refresh_benchmark_model_list(cached=models)
    return dialog, app


def _plan(dialog) -> list[tuple[str, ...]]:
    table = dialog.benchmark_plan_table
    return [
        tuple(
            table.item(row, column).text() if table.item(row, column) else ""
            for column in range(table.columnCount())
        )
        for row in range(table.rowCount())
    ]


def _statuses(dialog) -> list[str]:
    return [row[-1] for row in _plan(dialog)]


def _case(model: str, device: str, *, error: str | None = None) -> BenchmarkCase:
    return BenchmarkCase(
        model=model,
        device=device,
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.4,
        runs=[]
        if error
        else [
            BenchmarkRun(
                run_index=1,
                seconds=1.0,
                audio_duration_seconds=2.0,
                real_time_factor=0.043,
                transcript_chars=5,
                transcript_words=1,
                detected_language="en",
                language_probability=0.9,
                transcript="hello",
            )
        ],
        error=error,
    )


def _options(models: list[str]) -> BenchmarkOptions:
    return BenchmarkOptions(
        audio_path="C:/sample.wav",
        audio_name="sample.wav",
        model_names=models,
        device="auto",
        compute_type="int8",
        webgpu_devices=["auto"],
        runs=1,
        beam_size=5,
        language="auto",
        vad_filter=False,
        warmup=False,
        threads=0,
    )


def test_the_case_list_shows_what_the_current_selection_will_run(tmp_path):
    dialog, app = _dialog(tmp_path, ["small", "cohere-transcribe-03-2026"])
    table = dialog.benchmark_plan_table

    assert [
        table.horizontalHeaderItem(column).text()
        for column in range(table.columnCount())
    ] == _PLAN_HEADERS
    assert _plan(dialog) == [
        ("1", "small", "auto", "int8", "Pending"),
        ("2", "cohere-transcribe-03-2026", "auto", "onnx-q4", "Pending"),
    ]
    assert dialog.benchmark_plan_caption_label.text() == "2 cases will run"

    dialog.benchmark_webgpu_device_combo.setCurrentIndex(
        dialog.benchmark_webgpu_device_combo.findData("all")
    )
    app.processEvents()

    # "All explicit targets" is webgpu/dml/cpu, and only the ONNX model runs
    # once per target; faster-whisper keeps the standard device.
    assert [row[1:3] for row in _plan(dialog)] == [
        ("small", "auto"),
        ("cohere-transcribe-03-2026", "webgpu"),
        ("cohere-transcribe-03-2026", "dml"),
        ("cohere-transcribe-03-2026", "cpu"),
    ]
    assert dialog.benchmark_plan_caption_label.text() == "4 cases will run"

    dialog.benchmark_compute_type_combo.setCurrentIndex(
        dialog.benchmark_compute_type_combo.findData("float16")
    )
    app.processEvents()

    assert [row[3] for row in _plan(dialog)] == [
        "float16",
        "onnx-q4",
        "onnx-q4",
        "onnx-q4",
    ]

    dialog.benchmark_models_list.clearSelection()
    app.processEvents()

    assert _plan(dialog) == []
    assert dialog.benchmark_plan_caption_label.text() == "0 cases will run"
    _ = app


def test_a_selection_of_one_model_is_announced_in_the_singular(tmp_path):
    dialog, app = _dialog(tmp_path, ["small"])

    assert dialog.benchmark_plan_caption_label.text() == "1 case will run"
    _ = app


def test_a_run_marks_each_case_running_then_done_or_error(tmp_path):
    dialog, app = _dialog(tmp_path, ["small", "tiny"])
    dialog._set_benchmark_plan_rows(
        dialog._planned_benchmark_cases_from_widgets()
    )
    dialog._set_benchmark_progress(0, 2)

    assert _statuses(dialog) == ["Pending", "Pending"]
    assert dialog.benchmark_progress_bar.value() == 0
    assert dialog.benchmark_progress_bar.maximum() == 2

    dialog._on_benchmark_progress("[Case 1/2] small (auto/int8)")

    assert _statuses(dialog) == ["Running...", "Pending"]

    dialog._current_benchmark_cases = []
    dialog._on_benchmark_case_finished(_case("small", "cpu"))

    assert _statuses(dialog) == ["Done (RTF 0.043)", "Pending"]
    assert dialog.benchmark_progress_bar.value() == 1

    dialog._on_benchmark_progress("[Case 2/2] tiny (auto/int8)")

    assert _statuses(dialog) == ["Done (RTF 0.043)", "Running..."]

    dialog._on_benchmark_case_finished(_case("tiny", "cpu", error="it broke"))

    assert _statuses(dialog) == ["Done (RTF 0.043)", "Error"]
    assert dialog.benchmark_progress_bar.value() == 2
    _ = app


def test_a_canceled_run_marks_every_unfinished_case_skipped(tmp_path):
    dialog, app = _dialog(tmp_path, ["small", "tiny"])
    dialog._current_benchmark_options = _options(["small", "tiny"])
    dialog._set_benchmark_plan_rows(
        dialog._planned_benchmark_cases_from_widgets()
    )
    dialog._set_benchmark_progress(0, 2)
    dialog._current_benchmark_cases = []
    dialog._on_benchmark_progress("[Case 1/2] small (auto/int8)")
    dialog._on_benchmark_case_finished(_case("small", "cpu"))
    dialog._on_benchmark_progress("[Case 2/2] tiny (auto/int8)")

    assert _statuses(dialog) == ["Done (RTF 0.043)", "Running..."]

    dialog._on_benchmark_finished(
        True,
        "Benchmark summary:\ncanceled",
        {
            "cases": [_case("small", "cpu")],
            "options": _options(["small", "tiny"]),
            "status": "canceled",
        },
    )
    app.processEvents()

    # The row that was running never delivered a case, so it did not finish.
    assert _statuses(dialog) == ["Done (RTF 0.043)", "Skipped"]
    assert dialog.benchmark_progress_bar.isVisible() is False
    _ = app


def test_a_finished_run_leaves_the_case_list_alone_until_the_selection_changes(
    tmp_path,
):
    dialog, app = _dialog(tmp_path, ["small"])
    dialog._set_benchmark_plan_rows(
        dialog._planned_benchmark_cases_from_widgets()
    )
    dialog._current_benchmark_cases = []
    dialog._on_benchmark_progress("[Case 1/1] small (auto/int8)")
    dialog._on_benchmark_case_finished(_case("small", "cpu"))
    dialog._on_benchmark_finished(
        True,
        "Benchmark summary:\nsmall",
        {
            "cases": [_case("small", "cpu")],
            "options": _options(["small"]),
            "status": "completed",
        },
    )
    app.processEvents()

    assert _statuses(dialog) == ["Done (RTF 0.043)"]

    # Clearing the loaded result is about the result, not the run setup.
    dialog._clear_benchmark_results()

    assert _statuses(dialog) == ["Done (RTF 0.043)"]

    dialog.benchmark_models_list.clearSelection()
    app.processEvents()

    assert _statuses(dialog) == []
    _ = app


def test_a_model_list_refresh_during_a_run_does_not_redraw_the_plan(tmp_path):
    """The plan comes from the options snapshotted at the run's start."""
    dialog, app = _dialog(tmp_path, ["small", "tiny"])
    dialog._set_benchmark_plan_rows(
        dialog._planned_benchmark_cases_from_widgets()
    )
    dialog._current_benchmark_cases = []
    dialog._on_benchmark_progress("[Case 1/2] small (auto/int8)")
    dialog._on_benchmark_case_finished(_case("small", "cpu"))
    dialog._active_benchmark_thread = threading.Thread(target=lambda: None)

    dialog._refresh_benchmark_model_list(cached=["small", "tiny", "base"])
    app.processEvents()

    assert _statuses(dialog) == ["Done (RTF 0.043)", "Pending"]

    # Once the run is over the same refresh still leaves it alone: the
    # rebuild keeps the previous selection, so the plan it describes has not
    # changed, and the newly available model is not in it until selected.
    dialog._active_benchmark_thread = None
    dialog._refresh_benchmark_model_list(cached=["small", "tiny", "base"])
    app.processEvents()

    assert _statuses(dialog) == ["Done (RTF 0.043)", "Pending"]
    assert [row[1] for row in _plan(dialog)] == ["small", "tiny"]

    dialog.benchmark_models_list.selectAll()
    app.processEvents()

    assert [row[1] for row in _plan(dialog)] == ["small", "tiny", "base"]
    _ = app


def test_the_progress_bar_keeps_its_space_while_it_is_hidden(tmp_path):
    dialog, app = _dialog(tmp_path, ["small", "tiny"])
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    dialog.show()
    app.processEvents()
    bar = dialog.benchmark_progress_bar
    label = dialog.benchmark_status_label

    assert bar.isVisible() is False
    assert bar.sizePolicy().retainSizeWhenHidden() is True
    assert bar.format() == "%v / %m cases"
    assert "Show Progress" in bar.toolTip()
    # The two changing items of the header row are the same height, both fixed
    # from the run button's build-time sizeHint.
    assert bar.height() == label.height() > 0
    idle = (label.geometry(), label.sizeHint(), bar.geometry())

    dialog._set_benchmark_progress(0, 2)
    app.processEvents()

    assert bar.isVisible() is True
    assert bar.value() == 0
    assert bar.maximum() == 2
    assert (label.geometry(), label.sizeHint(), bar.geometry()) == idle

    dialog._set_benchmark_progress(1, 2)
    app.processEvents()

    assert bar.value() == 1
    assert (label.geometry(), label.sizeHint(), bar.geometry()) == idle

    dialog._set_benchmark_progress(0, 0)
    app.processEvents()

    assert bar.isVisible() is False
    assert (label.geometry(), label.sizeHint(), bar.geometry()) == idle
    _ = app


def test_the_header_button_caption_swap_moves_nothing(tmp_path):
    dialog, app = _dialog(tmp_path, ["small"])
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    dialog.show()
    app.processEvents()
    button = dialog.open_benchmark_window_button
    label = dialog.benchmark_status_label

    assert button.text() == "Run Benchmark..."
    # `sizeHint()` is the raw hint and does follow the caption; what the layout
    # uses is that hint expanded to the reserved minimum, so the rendered
    # geometry is what must not move.
    idle = (button.geometry(), button.minimumWidth(), label.geometry())

    dialog._active_benchmark_thread = threading.Thread(target=lambda: None)
    dialog._update_benchmark_actions()
    app.processEvents()

    assert button.text() == "Show Progress..."
    assert (button.geometry(), button.minimumWidth(), label.geometry()) == idle

    dialog._active_benchmark_thread = None
    dialog._update_benchmark_actions()
    app.processEvents()

    assert button.text() == "Run Benchmark..."
    assert (button.geometry(), button.minimumWidth(), label.geometry()) == idle
    _ = app


def test_both_captions_open_the_same_run_window(tmp_path):
    dialog, app = _dialog(tmp_path, ["small"])

    dialog.open_benchmark_window_button.click()
    app.processEvents()

    assert dialog.benchmark_window.isVisible() is True
    dialog.benchmark_window.hide()
    app.processEvents()

    dialog._active_benchmark_thread = threading.Thread(target=lambda: None)
    dialog._update_benchmark_actions()

    assert dialog.open_benchmark_window_button.text() == "Show Progress..."

    dialog.open_benchmark_window_button.click()
    app.processEvents()

    assert dialog.benchmark_window.isVisible() is True
    dialog._active_benchmark_thread = None
    dialog.benchmark_window.hide()
    _ = app


class _ImmediateThread:
    def __init__(self, target, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def _armed_dialog(monkeypatch, tmp_path, models: list[str]):
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )
    dialog, app = _dialog(tmp_path, models)
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    dialog._set_benchmark_audio_path(str(audio_path))
    return dialog, app


def test_starting_a_run_arms_the_case_list_and_the_progress_bar(
    monkeypatch, tmp_path
):
    """The real start path, not the writers called by hand."""
    seen: list[tuple[str, list[str]]] = []

    def _fake_run(**kwargs):
        kwargs["progress_callback"]("[Case 1/2] small (auto/int8)")
        seen.append(("running", _statuses(dialog)))
        kwargs["case_callback"](_case("small", "cpu"))
        seen.append(("first done", _statuses(dialog)))
        kwargs["progress_callback"]("[Case 2/2] tiny (auto/int8)")
        kwargs["case_callback"](_case("tiny", "cpu"))
        seen.append(("second done", _statuses(dialog)))
        return [_case("small", "cpu"), _case("tiny", "cpu")]

    monkeypatch.setattr("stt_app.settings_dialog.run_benchmark_cases", _fake_run)
    dialog, app = _armed_dialog(monkeypatch, tmp_path, ["small", "tiny"])

    assert _statuses(dialog) == ["Pending", "Pending"]
    assert dialog.benchmark_progress_bar.isVisible() is False

    dialog._run_local_benchmark()
    app.processEvents()

    assert seen == [
        ("running", ["Running...", "Pending"]),
        ("first done", ["Done (RTF 0.043)", "Pending"]),
        ("second done", ["Done (RTF 0.043)", "Done (RTF 0.043)"]),
    ]
    # The run is over, so the bar is hidden again and the plan keeps its
    # final states.
    assert dialog.benchmark_progress_bar.isVisible() is False
    assert _statuses(dialog) == ["Done (RTF 0.043)", "Done (RTF 0.043)"]
    _ = app


def test_a_run_that_cannot_start_leaves_nothing_counting(monkeypatch, tmp_path):
    dialog, app = _armed_dialog(monkeypatch, tmp_path, ["small", "tiny"])

    class _RefusingThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _RefusingThread,
    )

    dialog._run_local_benchmark()
    app.processEvents()

    assert dialog.benchmark_progress_bar.isVisible() is False
    assert _statuses(dialog) == ["Pending", "Pending"]
    assert "Could not start the benchmark" in dialog.benchmark_status_label.text()
    _ = app


def test_reopening_the_run_window_keeps_a_finished_runs_case_states(tmp_path):
    """The header button rebuilds the model list, which used to redraw the plan.

    So did an inventory scan landing after the run. Both wiped the Done and
    Skipped states of a run that had just ended, although the plan they
    redrew was the one already on screen; only a changed plan redraws.
    """
    dialog, app = _dialog(tmp_path, ["small", "tiny"])
    dialog._current_benchmark_options = _options(["small", "tiny"])
    dialog._set_benchmark_plan_rows(
        dialog._planned_benchmark_cases_from_widgets()
    )
    dialog._set_benchmark_progress(0, 2)
    dialog._current_benchmark_cases = []
    dialog._on_benchmark_progress("[Case 1/2] small (auto/int8)")
    dialog._on_benchmark_case_finished(_case("small", "cpu"))
    dialog._on_benchmark_finished(
        True,
        "Benchmark summary:\ncanceled",
        {
            "cases": [_case("small", "cpu")],
            "options": _options(["small", "tiny"]),
            "status": "canceled",
        },
    )
    app.processEvents()
    assert _statuses(dialog) == ["Done (RTF 0.043)", "Skipped"]

    # The inventory the reopen's list rebuild reads, as the app has it.
    dialog._cached_local_models = ["small", "tiny"]
    dialog._cached_local_models_dir = dialog.model_dir_edit.text().strip()
    dialog._cached_local_models_available = True
    dialog._open_benchmark_window()
    app.processEvents()

    assert _statuses(dialog) == ["Done (RTF 0.043)", "Skipped"]

    dialog._refresh_benchmark_model_list(cached=["small", "tiny", "base"])
    app.processEvents()

    assert _statuses(dialog) == ["Done (RTF 0.043)", "Skipped"]

    # A changed selection is a new plan and does redraw.
    dialog.benchmark_models_list.selectAll()
    app.processEvents()

    assert _statuses(dialog) == ["Pending", "Pending", "Pending"]
    dialog.benchmark_window.hide()
    _ = app


def test_quitting_during_a_run_still_saves_what_it_measured(monkeypatch, tmp_path):
    """`shutdown()` runs from `aboutToQuit`, after `exec()` has returned. The
    worker it cancels hands its cases and its outcome to queued signals that
    no loop is left to deliver, so the partial run was never saved (measured:
    0 history entries) and the dialog still believed the run was active."""
    reached = threading.Event()

    def _fake_run(**kwargs):
        kwargs["case_callback"](_case("small", "cpu"))
        reached.set()
        while not kwargs["cancel_check"]():
            time.sleep(0.005)
        raise BenchmarkCancelled("Benchmark canceled.")

    monkeypatch.setattr("stt_app.settings_dialog.run_benchmark_cases", _fake_run)
    monkeypatch.setattr(
        "stt_app.settings_dialog_benchmark.collect_benchmark_environment",
        lambda: BenchmarkEnvironment.from_dict(None),
    )
    dialog, app = _dialog(tmp_path, ["small"])
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    dialog._set_benchmark_audio_path(str(audio_path))

    dialog._run_local_benchmark()
    assert reached.wait(5.0), "the run never started"

    # No event-loop pass after this, exactly as at `aboutToQuit`.
    dialog.shutdown()

    assert dialog._active_benchmark_thread is None
    entries = dialog._benchmark_history_store.recent_entries(5)
    assert [
        (entry.status, [case.model for case in entry.cases]) for entry in entries
    ] == [("canceled", ["small"])]
    assert _statuses(dialog) == ["Done (RTF 0.043)"]
    _ = app


def test_a_run_that_measured_nothing_does_not_claim_a_save(tmp_path):
    """The history arm skips an empty case list, and the completion line
    still said "finished and saved to history" over a store that had never
    been written (measured: the line painted, 0 entries, no file)."""
    dialog, app = _dialog(tmp_path, ["small"])
    dialog._current_benchmark_cases = []

    dialog._on_benchmark_finished(
        True,
        "Benchmark summary:\n",
        {"cases": [], "options": _options(["small"]), "status": "completed"},
    )
    app.processEvents()

    assert (
        dialog.benchmark_status_label.text()
        == "Benchmark finished with no cases. Nothing was saved."
    )
    assert dialog._benchmark_history_store.recent_entries(20) == []
    _ = app
