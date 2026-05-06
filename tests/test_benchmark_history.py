from __future__ import annotations

import csv
import math
import zipfile

from stt_app.benchmark_history import (
    BenchmarkHistoryEntry,
    BenchmarkHistoryStore,
    BenchmarkOptions,
    export_benchmark_entry,
)
from stt_app.local_benchmark import BenchmarkCase, BenchmarkRun


def _entry() -> BenchmarkHistoryEntry:
    case = BenchmarkCase(
        model="small",
        device="auto",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.25,
        runs=[
            BenchmarkRun(
                run_index=1,
                seconds=1.2,
                audio_duration_seconds=2.0,
                real_time_factor=0.6,
                transcript_chars=12,
                transcript_words=2,
                detected_language="en",
                language_probability=0.98,
            )
        ],
    )
    options = BenchmarkOptions(
        audio_path="C:/sample.wav",
        audio_name="sample.wav",
        model_names=["small"],
        device="auto",
        compute_type="int8",
        webgpu_devices=["auto"],
        runs=1,
        beam_size=5,
        language="auto",
        vad_filter=False,
        warmup=True,
        threads=0,
        model_dir="",
    )
    return BenchmarkHistoryEntry.new(
        status="completed",
        summary="Benchmark summary:\nsmall",
        options=options,
        cases=[case],
    )


def test_benchmark_history_roundtrip(tmp_path):
    store = BenchmarkHistoryStore(path=tmp_path / "benchmark_history.json")
    entry = _entry()

    store.add_entry(entry)
    loaded = store.recent_entries()

    assert len(loaded) == 1
    assert loaded[0].status == "completed"
    assert loaded[0].options.model_names == ["small"]
    assert loaded[0].cases[0].avg_rtf == 0.6


def test_benchmark_history_delete_handles_nan_case_values(tmp_path):
    store = BenchmarkHistoryStore(path=tmp_path / "benchmark_history.json")
    entry = _entry()
    entry.cases[0].load_seconds = math.nan
    store.add_entry(entry)

    removed = store.delete_entry(entry)

    assert removed == 1
    assert store.load() == []


def test_benchmark_export_writes_matching_csv_xlsx_and_markdown(tmp_path):
    entry = _entry()
    csv_path = tmp_path / "benchmark.csv"
    xlsx_path = tmp_path / "benchmark.xlsx"
    markdown_path = tmp_path / "benchmark.md"

    export_benchmark_entry(csv_path, entry)
    export_benchmark_entry(xlsx_path, entry)
    export_benchmark_entry(markdown_path, entry)

    rows = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0] == [
        "created_at",
        "benchmark_status",
        "audio_path",
        "audio_name",
        "selected_models",
        "standard_device",
        "benchmark_compute_type",
        "onnx_device_targets",
        "configured_runs",
        "beam_size",
        "language",
        "vad_filter",
        "warmup",
        "threads",
        "model_dir",
        "row_type",
        "model",
        "device",
        "compute_type",
        "run_index",
        "seconds",
        "audio_duration_seconds",
        "real_time_factor",
        "transcript_chars",
        "transcript_words",
        "detected_language",
        "language_probability",
        "download_seconds",
        "load_seconds",
        "case_run_count",
        "avg_seconds",
        "stdev_seconds",
        "avg_rtf",
        "case_status",
        "error",
    ]
    assert rows[1][1:4] == ["completed", "C:/sample.wav", "sample.wav"]
    assert rows[1][15:20] == ["run", "small", "auto", "int8", "1"]

    with zipfile.ZipFile(xlsx_path) as archive:
        names = set(archive.namelist())
        assert "xl/worksheets/sheet1.xml" in names
        assert "xl/worksheets/sheet2.xml" not in names
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "created_at" in sheet
        assert "small" in sheet

    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# Benchmark Results")
    assert "| created_at | benchmark_status | audio_path |" in markdown
    assert "| " + rows[1][0] in markdown
