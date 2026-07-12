from __future__ import annotations

from PySide6 import QtWidgets

from stt_app.benchmark_history import BenchmarkHistoryEntry, BenchmarkOptions
from stt_app.local_benchmark import BenchmarkCase, BenchmarkRun
from stt_app.settings_dialog_benchmark import (
    _BenchmarkDetailsView,
    _BenchmarkHistoryTable,
)


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
