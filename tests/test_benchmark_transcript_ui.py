from __future__ import annotations

import threading

from PySide6 import QtCore, QtWidgets
from test_settings_dialog_connection import (
    _FakeLogger,
    _FakeSecretStore,
    _FakeSettingsStore,
)

from stt_app.benchmark_history import (
    BenchmarkHistoryEntry,
    BenchmarkHistoryStore,
    BenchmarkOptions,
)
from stt_app.local_benchmark import BenchmarkCase, BenchmarkRun
from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_dialog_benchmark import (
    _BenchmarkDetailsView,
    _BenchmarkHistoryTable,
)
from stt_app.settings_store import AppSettings


def _run(index: int, transcript: str) -> BenchmarkRun:
    return BenchmarkRun(
        run_index=index,
        seconds=1.0,
        audio_duration_seconds=2.0,
        real_time_factor=0.5,
        transcript_chars=len(transcript),
        transcript_words=len(transcript.split()),
        detected_language="en",
        language_probability=0.9,
        transcript=transcript,
    )


def _entry(runs: list[BenchmarkRun]) -> BenchmarkHistoryEntry:
    return BenchmarkHistoryEntry.new(
        status="completed",
        summary="Benchmark summary:\nraw legacy text",
        options=BenchmarkOptions(
            audio_path="C:/sample.wav",
            audio_name="sample.wav",
            model_names=["small"],
            device="auto",
            compute_type="int8",
            webgpu_devices=["auto"],
            runs=len(runs),
            beam_size=5,
            language="auto",
            vad_filter=False,
            warmup=True,
            threads=0,
        ),
        cases=[
            BenchmarkCase(
                model="small",
                device="cpu",
                compute_type="int8",
                download_seconds=0.0,
                load_seconds=0.2,
                runs=runs,
            )
        ],
    )


def test_benchmark_details_renders_all_runs_and_marks_variation():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = _BenchmarkDetailsView()

    view.set_entry(
        _entry(
            [
                _run(1, "hello world"),
                _run(2, "hello world"),
                _run(3, "hello worlds"),
            ]
        )
    )

    assert view.toPlainText().startswith("Benchmark summary:")
    assert view.transcripts_table.rowCount() == 3
    assert view.transcripts_table.item(0, 3).text() == "Reference"
    assert view.transcripts_table.item(1, 3).text() == "Identical to run 1"
    assert view.transcripts_table.item(2, 3).text() == "Differs from run 1"
    view.transcripts_table.selectRow(2)
    assert "hello worlds" in view.transcript_text.toPlainText()


def test_benchmark_details_explains_missing_legacy_transcript():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = _BenchmarkDetailsView()

    view.set_entry(_entry([_run(1, "")]))

    assert view.transcripts_table.item(0, 3).text() == "Not stored (legacy)"
    assert "predates transcript capture" in view.transcript_text.toPlainText()


def test_benchmark_history_table_keeps_existing_list_compatibility():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    table = _BenchmarkHistoryTable(1, 2)
    table.setItem(0, 0, QtWidgets.QTableWidgetItem("entry"))

    table.setCurrentRow(0)

    assert table.count() == 1
    assert table.item(0).text() == "entry"
    assert table.currentRow() == 0


def test_a_history_row_cannot_be_loaded_into_a_running_benchmark(tmp_path):
    """Every other way in is disabled while a run is in flight; this one was not.

    `Load Selected` is gated on `_active_benchmark_thread`, but the table's
    double-click went straight to `_load_benchmark_history_entry`, which
    replaces `_current_benchmark_cases` -- the very list the next finished case
    appends to. The results table and the live summary then showed the stored
    run's cases and the running one's mixed together, and the stored entry's
    environment could reach the saved history entry of the live run whenever
    the worker had none of its own.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    stored = _entry([_run(1, "stored transcript")])
    dialog._benchmark_history_store = BenchmarkHistoryStore(
        path=tmp_path / "benchmark_history.json"
    )
    dialog._benchmark_history_store.add_entry(stored)
    dialog._refresh_benchmark_history_list()
    assert dialog.benchmark_history_list.rowCount() == 1

    live_case = _entry([_run(1, "live transcript")]).cases[0]
    live_options = _entry([_run(1, "live")]).options
    dialog._current_benchmark_cases = [live_case]
    dialog._current_benchmark_options = live_options
    dialog._current_benchmark_environment = None
    dialog._active_benchmark_thread = threading.Thread(target=lambda: None)

    item = dialog.benchmark_history_list.item(0, 0)
    assert item is not None
    dialog._load_benchmark_history_item(item)
    app.processEvents()

    assert dialog._current_benchmark_cases == [live_case]
    assert dialog._current_benchmark_options is live_options
    assert dialog._current_benchmark_environment is None
    assert "while a benchmark is running" in dialog.benchmark_status_label.text()

    # And it is only refused while the run owns the view.
    dialog._active_benchmark_thread = None
    dialog._load_benchmark_history_item(item)
    app.processEvents()

    assert dialog._current_benchmark_cases == list(stored.cases)


def _case(model: str, transcripts: list[str]) -> BenchmarkCase:
    return BenchmarkCase(
        model=model,
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.2,
        runs=[_run(index + 1, text) for index, text in enumerate(transcripts)],
    )


def test_a_finished_case_leaves_the_transcript_the_reader_opened():
    """`set_live_results` runs once per finished case, for the whole run.

    Rebuilding the table used to end in `selectRow(0)`, so anyone reading run 3
    of the first model was thrown back to run 1 the moment the next model
    finished -- repeatedly, and with no way to keep a transcript on screen
    while a multi-model benchmark was still going.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = _BenchmarkDetailsView()
    first = _case("tiny", ["one", "two", "three"])
    view.set_live_results("running", [first])
    app.processEvents()

    view.transcripts_table.selectRow(2)
    app.processEvents()
    opened = view.transcript_text.toPlainText()
    assert "run 3" in opened

    view.set_live_results("running", [_case("small", ["alpha"]), first])
    app.processEvents()

    assert view.transcripts_table.rowCount() == 4
    assert view.transcript_text.toPlainText() == opened
    row = view.transcripts_table.currentRow()
    item = view.transcripts_table.item(row, 0)
    assert item is not None
    assert item.data(QtCore.Qt.UserRole + 1).endswith("run 3")


def test_the_first_transcript_is_opened_when_the_selected_one_is_gone():
    """Falling back to row 0 is still right when the row cannot be restored.

    Loading a different history entry replaces every row, and an empty pane
    with a populated table would be worse than starting at the top.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = _BenchmarkDetailsView()
    view.set_live_results("running", [_case("tiny", ["one", "two"])])
    app.processEvents()
    view.transcripts_table.selectRow(1)
    app.processEvents()

    view.set_live_results("running", [_case("large-v3", ["different"])])
    app.processEvents()

    assert view.transcripts_table.currentRow() == 0
    assert "different" in view.transcript_text.toPlainText()


def test_two_cases_that_fell_back_to_the_same_device_are_told_apart():
    """`case.device` is the device the runtime *resolved*, not the one asked for.

    Benchmarking one ONNX model against "All explicit targets" on a machine
    with no usable GPU brings both the webgpu and the dml case back as `cpu`,
    so their rows are identical in every visible column. Matching only on the
    visible label would restore the reader onto the first of the two whichever
    one they had opened.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = _BenchmarkDetailsView()
    webgpu_attempt = _case("granite-4.0-1b-speech", ["from the webgpu attempt"])
    dml_attempt = _case("granite-4.0-1b-speech", ["from the dml attempt"])
    view.set_live_results("running", [webgpu_attempt, dml_attempt])
    app.processEvents()

    assert view.transcripts_table.rowCount() == 2
    first = view.transcripts_table.item(0, 0)
    second = view.transcripts_table.item(1, 0)
    assert first is not None and second is not None
    # The visible identity really is ambiguous; the selection key is not.
    assert first.data(QtCore.Qt.UserRole + 1) == second.data(QtCore.Qt.UserRole + 1)
    assert first.data(QtCore.Qt.UserRole + 2) != second.data(QtCore.Qt.UserRole + 2)

    view.transcripts_table.selectRow(1)
    app.processEvents()
    assert "from the dml attempt" in view.transcript_text.toPlainText()

    # A third case finishes; the reader stays on the second, not the first.
    view.set_live_results(
        "running", [webgpu_attempt, dml_attempt, _case("tiny", ["later"])]
    )
    app.processEvents()

    assert view.transcripts_table.currentRow() == 1
    assert "from the dml attempt" in view.transcript_text.toPlainText()
