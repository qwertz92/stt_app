from __future__ import annotations

import csv
import json
import math
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from stt_app.benchmark_environment import BenchmarkEnvironment
from stt_app.benchmark_history import (
    BenchmarkHistoryEntry,
    BenchmarkHistoryStore,
    BenchmarkOptions,
    export_benchmark_entry,
)
from stt_app.local_benchmark import BenchmarkCase, BenchmarkRun
from stt_app.persistence import backup_path


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
                transcript="hello world",
            )
        ],
        runtime_details="Fallback attempts: webgpu: unsupported",
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
        environment=BenchmarkEnvironment(
            os="Windows 11",
            python="CPython 3.12 64bit",
            cpu="AMD Ryzen",
            logical_cpus=12,
            memory="32.0 GB",
            gpus=["Intel Arc A750"],
            frameworks={"faster-whisper": "1.2.1", "CTranslate2": "4.6.0"},
            node="v22.0.0",
        ),
    )


def test_benchmark_history_roundtrip(tmp_path):
    store = BenchmarkHistoryStore(path=tmp_path / "benchmark_history.json")
    entry = _entry()

    store.add_entry(entry)
    loaded = store.recent_entries()

    assert len(loaded) == 1
    assert loaded[0].status == "completed"
    assert loaded[0].options.model_names == ["small"]
    assert loaded[0].environment.cpu == "AMD Ryzen"
    assert loaded[0].environment.gpus == ["Intel Arc A750"]
    assert loaded[0].cases[0].avg_rtf == 0.6
    assert loaded[0].cases[0].runs[0].transcript == "hello world"
    assert loaded[0].cases[0].runtime_details == "Fallback attempts: webgpu: unsupported"


def test_benchmark_options_parse_explicit_string_booleans():
    options = BenchmarkOptions.from_dict(
        {
            "model_names": ["small"],
            "vad_filter": "false",
            "warmup": "true",
        }
    )

    assert options.vad_filter is False
    assert options.warmup is True


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
        "environment_os",
        "environment_python",
        "environment_cpu",
        "environment_logical_cpus",
        "environment_memory",
        "environment_gpus",
        "environment_frameworks",
        "environment_node",
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
        "transcript",
        "detected_language",
        "language_probability",
        "download_seconds",
        "load_seconds",
        "case_run_count",
        "avg_seconds",
        "stdev_seconds",
        "avg_rtf",
        "case_status",
        "runtime_details",
        "error",
    ]
    assert rows[1][1:4] == ["completed", "C:/sample.wav", "sample.wav"]
    assert rows[1][15:23] == [
        "Windows 11",
        "CPython 3.12 64bit",
        "AMD Ryzen",
        "12",
        "32.0 GB",
        "Intel Arc A750",
        "faster-whisper 1.2.1, CTranslate2 4.6.0",
        "v22.0.0",
    ]
    assert rows[1][23:28] == ["run", "small", "auto", "int8", "1"]
    assert rows[1][33] == "hello world"

    with zipfile.ZipFile(xlsx_path) as archive:
        names = set(archive.namelist())
        assert "xl/worksheets/sheet1.xml" in names
        assert "xl/worksheets/sheet2.xml" not in names
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "created_at" in sheet
        assert "small" in sheet

    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# Benchmark Results")
    assert "## Benchmark Context" in markdown
    assert "| CPU | AMD Ryzen |" in markdown
    assert "## Result Rows" in markdown
    assert "| created_at | benchmark_status | audio_path |" in markdown
    assert "hello world" in markdown


def test_benchmark_history_loads_legacy_runs_without_transcripts(tmp_path):
    path = tmp_path / "benchmark_history.json"
    entry = _entry().to_dict()
    entry["cases"][0]["runs"][0].pop("transcript")
    path.write_text(json.dumps([entry]), encoding="utf-8")

    loaded = BenchmarkHistoryStore(path=path).load()

    assert loaded[0].cases[0].runs[0].transcript == ""


def test_benchmark_csv_export_neutralizes_spreadsheet_formulas(tmp_path):
    entry = _entry()
    entry.options.audio_path = '=HYPERLINK("https://example.invalid")'
    entry.cases[0].error = "+cmd|' /C calc'!A0"
    csv_path = tmp_path / "benchmark.csv"

    export_benchmark_entry(csv_path, entry)

    rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0]["audio_path"].startswith("'=HYPERLINK")
    assert rows[0]["error"].startswith("'+cmd")


@pytest.mark.parametrize(
    ("label", "cell_text", "expected"),
    [
        ("a NUL byte", "before\x00after", "before\ufffdafter"),
        ("a BEL", "before\x07after", "before\ufffdafter"),
        ("a vertical tab", "before\x0bafter", "before\ufffdafter"),
        ("a lone surrogate", "before\ud800after", "before\ufffdafter"),
        ("a tab", "before\tafter", "before\tafter"),
        ("an emoji", "before \U0001f600 after", "before \U0001f600 after"),
        ("an umlaut", "Gr\u00fc\u00dfe", "Gr\u00fc\u00dfe"),
        ("markup", "a <b> & c", "a <b> & c"),
    ],
)
def test_the_spreadsheet_export_stays_openable_whatever_a_cell_holds(
    tmp_path, label, cell_text, expected
):
    """A worksheet that will not parse is an `.xlsx` Excel refuses to open.

    XML 1.0 permits #x9, #xA, #xD, #x20-#xD7FF, #xE000-#xFFFD and
    #x10000-#x10FFFF and nothing else -- not even escaped -- while
    `saxutils.escape` only rewrites `&`, `<` and `>`. One stray control byte
    anywhere in a benchmark row therefore produced a file that was written
    without error and could not be opened. The plausible route is the text
    nobody typed: `runtime_details` built from a runtime's own error output,
    the environment strings read off the system, and a transcript returned by
    a remote provider.

    Only the forbidden characters may be touched. A German transcript, an
    emoji and a literal angle bracket are ordinary content here, and dropping
    them to make the file parse would trade one silent corruption for
    another -- so this compares the round-tripped cell text exactly rather
    than only asserting that the file parses.
    """
    entry = _entry()
    entry.cases[0].runs[0].transcript = cell_text
    export_path = tmp_path / "benchmark.xlsx"

    export_benchmark_entry(export_path, entry)

    with zipfile.ZipFile(export_path) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

    # Parsing is half the assertion: it raises on anything XML cannot carry.
    root = ET.fromstring(sheet)
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    cells = [node.text or "" for node in root.iter(f"{namespace}t")]

    assert expected in cells, (
        f"{label}: the cell round-tripped as something other than "
        f"{expected!r}; sheet holds {cells!r}"
    )


def _one_entry_payload(extra_run_field: dict | None = None) -> list:
    run = {
        "run_index": 1,
        "seconds": 1.0,
        "audio_duration_seconds": 10.0,
        "real_time_factor": 0.1,
        "transcript_chars": 5,
        "transcript_words": 1,
        "detected_language": "de",
        "language_probability": 0.9,
        "transcript": "hallo",
    }
    run.update(extra_run_field or {})
    return [
        {
            "created_at": "2026-08-30T10:00:00+00:00",
            "status": "completed",
            "summary": "one case",
            "options": {"audio_name": "sample.wav", "model_names": ["tiny"]},
            "environment": {},
            "cases": [
                {
                    "model": "tiny",
                    "device": "cpu",
                    "compute_type": "int8",
                    "download_seconds": 0.0,
                    "load_seconds": 0.5,
                    "runs": [run],
                }
            ],
        }
    ]


def test_a_run_field_this_build_does_not_know_is_dropped_not_fatal(tmp_path):
    """`%APPDATA%` is shared between builds, and `transcript` was added late.

    `BenchmarkRun(**entry)` raised `TypeError` for one unexpected key, which
    escaped the store's `except ValueError`, so the backup recovery never ran
    and `SettingsDialog.__init__` -- which calls `recent_entries` with no
    guard -- could not build at all.
    """
    path = tmp_path / "benchmark_history.json"
    path.write_text(
        json.dumps(_one_entry_payload({"peak_memory_mb": 812})), encoding="utf-8"
    )

    entries = BenchmarkHistoryStore(path=path).recent_entries(20)

    assert len(entries) == 1
    assert entries[0].cases[0].runs[0].transcript == "hallo"
    assert not list(tmp_path.glob("*.corrupt.*")), (
        "a readable file was quarantined instead of being read"
    )


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda p: p[0]["cases"][0].__setitem__("runs", 7), id="runs-int"),
        pytest.param(lambda p: p[0].__setitem__("cases", 7), id="cases-int"),
        pytest.param(
            lambda p: p[0]["options"].__setitem__("model_names", 7), id="models-int"
        ),
    ],
)
def test_a_payload_of_the_wrong_shape_recovers_instead_of_raising(tmp_path, mangle):
    """The store must survive it; the caller must not have to."""
    payload = _one_entry_payload()
    mangle(payload)
    path = tmp_path / "benchmark_history.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    # Must not raise -- `SettingsDialog.__init__` has no guard around this.
    BenchmarkHistoryStore(path=path).recent_entries(20)


def test_the_backup_recovery_is_reachable_for_entries_that_have_cases(tmp_path):
    """The parametrized backup test built entries with `cases=[]`.

    `_entries_from_payload` drops those (`if entry.cases`), so all four of its
    assertions compared an empty list against an empty list and the benchmark
    store's recovery was, in effect, untested.
    """
    path = tmp_path / "benchmark_history.json"
    store = BenchmarkHistoryStore(path=path)
    store.save(store._entries_from_payload(_one_entry_payload()))
    assert backup_path(path).exists()
    assert store.count() == 1, "the fixture entry is dropped again"

    path.unlink()

    recovered = BenchmarkHistoryStore(path=path)
    assert recovered.count() == 1, "the backup was not read"
    assert path.exists(), "the primary was not republished from the backup"


_LONE_SURROGATE_TRANSCRIPT = "Guten Tag \ud800 und tschuess"


def _entry_with_transcript(text: str) -> BenchmarkHistoryEntry:
    case = BenchmarkCase(
        model="small",
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=1.0,
        runs=[
            BenchmarkRun(
                run_index=1,
                seconds=2.0,
                audio_duration_seconds=20.0,
                real_time_factor=0.1,
                transcript_chars=len(text),
                transcript_words=len(text.split()),
                detected_language="de",
                language_probability=0.99,
                transcript=text,
            )
        ],
    )
    return BenchmarkHistoryEntry.new(
        status="completed",
        summary="one case",
        options=BenchmarkOptions(
            audio_path="C:/sample.wav",
            audio_name="sample.wav",
            model_names=["small"],
            device="cpu",
            compute_type="int8",
            webgpu_devices=["auto"],
            runs=1,
            beam_size=5,
            language="de",
            vad_filter=False,
            warmup=True,
            threads=0,
        ),
        cases=[case],
    )


@pytest.mark.parametrize("suffix", [".csv", ".md", ".xlsx"])
def test_an_export_never_destroys_the_file_it_replaces(suffix, tmp_path, monkeypatch):
    """The export target is a path the user picked in a Save dialog, so it is
    routinely a file that already exists. Two of the three writers opened it
    directly, which truncates it before a single row has been produced -- and a
    transcript carrying one lone surrogate then raised `UnicodeEncodeError`
    part-way through. Measured before the fix: the user's own file replaced by
    a 638-byte CSV fragment, and by an empty Markdown file.
    """
    target = tmp_path / f"important{suffix}"
    target.write_bytes(b"THE USER'S EXISTING FILE")
    before = target.read_bytes()

    def explode(_entry):
        raise RuntimeError("the row builder failed")

    monkeypatch.setattr("stt_app.benchmark_history._export_rows", explode)
    with pytest.raises(RuntimeError):
        export_benchmark_entry(target, _entry_with_transcript("ok"))

    assert target.read_bytes() == before, (
        "a failed export overwrote the file it was told to replace"
    )


@pytest.mark.parametrize("suffix", [".csv", ".md", ".xlsx"])
def test_every_export_format_survives_a_lone_surrogate(suffix, tmp_path):
    """A lone surrogate reaches here through the store unchanged --
    `json.dumps(ensure_ascii=True)` escapes it and `json.loads` decodes it back
    -- and cannot be encoded as UTF-8 at all. The XLSX writer already replaced
    it because XML cannot carry it either; the other two raised. The character
    set is the same set, so it is now one sanitiser for all three.
    """
    entry = _entry_with_transcript(_LONE_SURROGATE_TRANSCRIPT)
    target = tmp_path / f"export{suffix}"

    export_benchmark_entry(target, entry)

    data = target.read_bytes()
    assert data, "nothing was written"
    if suffix != ".xlsx":
        text = data.decode("utf-8")
        assert "\ufffd" in text, "the unencodable character was not replaced"
        assert "und tschuess" in text, "the rest of the transcript was lost"


def test_the_csv_still_neutralises_a_formula(tmp_path):
    """Sanitising runs after the formula guard, and must not undo it."""
    entry = _entry_with_transcript("=cmd|'/c calc'!A1")
    target = tmp_path / "export.csv"

    export_benchmark_entry(target, entry)

    text = target.read_text(encoding="utf-8")
    assert "'=cmd" in text, text


@pytest.mark.parametrize("suffix", [".csv", ".md", ".xlsx"])
def test_only_the_atomic_writer_touches_the_export_target(suffix, tmp_path, monkeypatch):
    """Building the bytes first is half of it; the write itself must also not
    truncate. `path.write_bytes` and `zipfile.ZipFile(path, "w")` empty the file
    before the first byte goes in, so a disk that fills up or a permission that
    changes mid-write still destroys what was there. `atomic_write_bytes` writes
    a sibling temp file and replaces, so the target only ever changes in one
    step.
    """
    target = tmp_path / f"important{suffix}"
    target.write_bytes(b"THE USER'S EXISTING FILE")
    before = target.read_bytes()

    written: list[tuple[Path, int]] = []

    def spy(path, data):
        written.append((path, len(data)))

    monkeypatch.setattr("stt_app.benchmark_history.atomic_write_bytes", spy)
    export_benchmark_entry(target, _entry_with_transcript("hallo welt"))

    assert [path for path, _size in written] == [target], (
        "the export did not go through the atomic writer"
    )
    assert written[0][1] > 0, "it handed the writer nothing"
    assert target.read_bytes() == before, (
        "something wrote to the target directly, around the atomic writer"
    )
def test_a_carriage_return_never_splits_a_markdown_table_row(tmp_path):
    """Only `\n` was folded into `<br>`, and the sanitiser permits `\r`.

    XML 1.0 allows #xD, so a carriage return survives `export_safe_text` and
    reached the Markdown table raw. The routine source is `runtime_details`,
    built from a child process's own output -- CRLF on Windows. Measured on
    "WebGPU rejected\r\nfell back to cpu": the row carried a bare CR, so
    anything treating a lone CR as a line terminator saw two fragments where a
    table row should be, and the table broke from that row on.
    """
    target = tmp_path / "report.md"

    export_benchmark_entry(
        target,
        _entry_with_transcript("erste zeile\r\nzweite | zeile\rdritte"),
    )

    # Bytes, not `read_text`: universal newlines would strip the very
    # character under test.
    body = target.read_bytes().decode("utf-8")
    assert "\r" not in body, "a carriage return reached the table"
    result_table = body.split("## Result Rows", 1)[1]
    rows = [line for line in result_table.split("\n") if line.startswith("|")]
    # Count separators, not pipe characters: an escaped pipe is content.
    widths = {row.replace("\\|", "").count("|") for row in rows}
    assert len(widths) == 1, f"result rows disagree on their column count: {widths}"
    assert "erste zeile<br>zweite \\| zeile<br>dritte" in body, (
        "a pipe in the transcript was not escaped, so it opened a column"
    )
