from __future__ import annotations

import csv
import math
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .app_paths import benchmark_history_path
from .benchmark_environment import BenchmarkEnvironment
from .csv_safety import spreadsheet_safe_cell
from .local_benchmark import BenchmarkCase, _case_from_dict
from .persistence import (
    atomic_write_json,
    load_json_with_backup,
    parse_json_bool,
    quarantine_corrupt_file,
)

MAX_BENCHMARK_HISTORY_ITEMS = 100


@dataclass(slots=True)
class BenchmarkOptions:
    audio_path: str
    audio_name: str
    model_names: list[str]
    device: str
    compute_type: str
    webgpu_devices: list[str]
    runs: int
    beam_size: int
    language: str
    vad_filter: bool
    warmup: bool
    threads: int
    model_dir: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BenchmarkOptions":
        model_names = raw.get("model_names", [])
        webgpu_devices = raw.get("webgpu_devices", [])
        return cls(
            audio_path=str(raw.get("audio_path", "")),
            audio_name=str(raw.get("audio_name", "")),
            model_names=[str(item) for item in model_names if str(item).strip()],
            device=str(raw.get("device", "auto") or "auto"),
            compute_type=str(raw.get("compute_type", "int8") or "int8"),
            webgpu_devices=[
                str(item) for item in webgpu_devices if str(item).strip()
            ],
            runs=_safe_int(raw.get("runs"), default=1),
            beam_size=_safe_int(raw.get("beam_size"), default=5),
            language=str(raw.get("language", "auto") or "auto"),
            vad_filter=parse_json_bool(raw.get("vad_filter")),
            warmup=parse_json_bool(raw.get("warmup")),
            threads=_safe_int(raw.get("threads"), default=0),
            model_dir=str(raw.get("model_dir", "")),
        )

    def summary_details(self, *, status: str = "") -> dict[str, Any]:
        details: dict[str, Any] = {}
        if status:
            details["Status"] = status
        details.update(
            {
                "Audio file": self.audio_path or self.audio_name,
                "Models": self.model_names,
                "Standard device": self.device,
                "Compute type": self.compute_type,
                "ONNX device targets": self.webgpu_devices,
                "Runs per case": self.runs,
                "Beam size": self.beam_size,
                "Language": self.language,
                "VAD filter": self.vad_filter,
                "Warmup": self.warmup,
                "Threads": self.threads,
                "Model directory": self.model_dir,
            }
        )
        return details


@dataclass(slots=True)
class BenchmarkHistoryEntry:
    created_at: str
    status: str
    summary: str
    options: BenchmarkOptions
    cases: list[BenchmarkCase]
    environment: BenchmarkEnvironment = field(default_factory=BenchmarkEnvironment)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BenchmarkHistoryEntry":
        cases_payload = raw.get("cases", [])
        cases = [
            _case_from_dict(item)
            for item in cases_payload
            if isinstance(item, dict)
        ]
        options_payload = raw.get("options", {})
        options = BenchmarkOptions.from_dict(
            options_payload if isinstance(options_payload, dict) else {}
        )
        environment_payload = raw.get("environment", {})
        return cls(
            created_at=str(raw.get("created_at", "")),
            status=str(raw.get("status", "")),
            summary=str(raw.get("summary", "")),
            options=options,
            cases=cases,
            environment=BenchmarkEnvironment.from_dict(
                environment_payload if isinstance(environment_payload, dict) else {}
            ),
        )

    @classmethod
    def new(
        cls,
        *,
        status: str,
        summary: str,
        options: BenchmarkOptions,
        cases: list[BenchmarkCase],
        environment: BenchmarkEnvironment | None = None,
    ) -> "BenchmarkHistoryEntry":
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return cls(
            created_at=timestamp,
            status=str(status or "completed"),
            summary=str(summary or ""),
            options=options,
            cases=list(cases),
            environment=environment or BenchmarkEnvironment(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "status": self.status,
            "summary": self.summary,
            "options": asdict(self.options),
            "cases": [asdict(case) for case in self.cases],
            "environment": asdict(self.environment),
        }

    def identity_key(self) -> tuple[str, str, str]:
        return (self.created_at, self.status, self.summary)


class BenchmarkHistoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or benchmark_history_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[BenchmarkHistoryEntry]:
        return self._load_from_path(self._path)

    def count(self) -> int:
        return len(self.load())

    def save(self, entries: list[BenchmarkHistoryEntry]) -> None:
        payload = [entry.to_dict() for entry in entries]
        atomic_write_json(self._path, payload, ensure_ascii=True, keep_backup=True)

    def add_entry(
        self,
        entry: BenchmarkHistoryEntry,
        *,
        max_items: int = MAX_BENCHMARK_HISTORY_ITEMS,
    ) -> None:
        entries = self.load()
        entries.append(entry)
        keep = _normalize_limit(max_items)
        if keep > 0 and len(entries) > keep:
            entries = entries[-keep:]
        self.save(entries)

    def recent_entries(self, limit: int = 20) -> list[BenchmarkHistoryEntry]:
        entries = self.load()
        keep = _normalize_limit(limit)
        selected = entries if keep == 0 else entries[-keep:]
        return list(reversed(selected))

    def delete_entry(self, entry: BenchmarkHistoryEntry) -> int:
        entries = self.load()
        target_key = entry.identity_key()
        index = next(
            (
                row
                for row, candidate in enumerate(entries)
                if candidate.identity_key() == target_key
            ),
            -1,
        )
        if index < 0:
            return 0
        entries.pop(index)
        self.save(entries)
        return 1

    def clear(self) -> int:
        removed = self.count()
        if removed:
            self.save([])
        return removed

    @staticmethod
    def _entries_from_payload(payload: Any) -> list[BenchmarkHistoryEntry]:
        if isinstance(payload, dict):
            payload = payload.get("entries", None)
        if not isinstance(payload, list):
            raise ValueError("Expected a JSON array of benchmark entries.")

        entries: list[BenchmarkHistoryEntry] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            entry = BenchmarkHistoryEntry.from_dict(item)
            if entry.cases:
                entries.append(entry)
        return entries

    @classmethod
    def _load_from_path(cls, path: Path) -> list[BenchmarkHistoryEntry]:
        if not path.exists():
            return []
        payload, source = load_json_with_backup(path, expected_type=list)
        if payload is None:
            quarantine_corrupt_file(path, include_backup=True)
            return []
        try:
            entries = cls._entries_from_payload(payload)
        except ValueError:
            quarantine_corrupt_file(path)
            return []
        if source == "backup":
            cls(path=path).save(entries)
        return entries


def export_benchmark_entry(path: Path, entry: BenchmarkHistoryEntry) -> None:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        _write_xlsx(path, entry)
        return
    if suffix == ".csv":
        _write_csv(path, entry)
        return
    if suffix in {".md", ".markdown"}:
        _write_markdown(path, entry)
        return
    raise ValueError("Benchmark export path must end in .csv, .xlsx, or .md.")


def _write_csv(path: Path, entry: BenchmarkHistoryEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_export_headers())
        writer.writerows(
            [spreadsheet_safe_cell(value) for value in row]
            for row in _export_rows(entry)
        )


def _write_xlsx(path: Path, entry: BenchmarkHistoryEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_export_headers()]
    rows.extend(_export_rows(entry))

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml())
        archive.writestr("_rels/.rels", _root_rels_xml())
        archive.writestr("xl/workbook.xml", _workbook_xml())
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml())
        archive.writestr("xl/worksheets/sheet1.xml", _worksheet_xml(rows))


def _write_markdown(path: Path, entry: BenchmarkHistoryEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Benchmark Results",
        "",
        "## Benchmark Context",
        "",
        _markdown_table(["Field", "Value"], _context_rows(entry)),
        "",
        "## Result Rows",
        "",
        _markdown_table(_export_headers(), _export_rows(entry)),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _export_headers() -> list[str]:
    return [
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


def _export_rows(entry: BenchmarkHistoryEntry) -> list[list[Any]]:
    rows: list[list[Any]] = []
    environment_values = _environment_export_values(entry.environment)
    for case in entry.cases:
        status = "ok" if case.error is None else "error"
        runs = case.runs or [None]
        for run in runs:
            rows.append(
                [
                    entry.created_at,
                    entry.status,
                    entry.options.audio_path,
                    entry.options.audio_name,
                    _display_value(entry.options.model_names),
                    entry.options.device,
                    entry.options.compute_type,
                    _display_value(entry.options.webgpu_devices),
                    entry.options.runs,
                    entry.options.beam_size,
                    entry.options.language,
                    entry.options.vad_filter,
                    entry.options.warmup,
                    entry.options.threads,
                    entry.options.model_dir,
                    *environment_values,
                    "run" if run is not None else "case",
                    case.model,
                    case.device,
                    case.compute_type,
                    run.run_index if run is not None else "",
                    run.seconds if run is not None else "",
                    run.audio_duration_seconds if run is not None else "",
                    run.real_time_factor if run is not None else "",
                    run.transcript_chars if run is not None else "",
                    run.transcript_words if run is not None else "",
                    run.detected_language if run is not None else "",
                    run.language_probability if run is not None else "",
                    case.download_seconds,
                    case.load_seconds,
                    len(case.runs),
                    case.avg_seconds,
                    case.stdev_seconds,
                    case.avg_rtf,
                    status,
                    case.runtime_details,
                    case.error or "",
                ]
            )
    return rows


def _context_rows(entry: BenchmarkHistoryEntry) -> list[list[Any]]:
    rows = [
        ["Created at", entry.created_at],
        ["Benchmark status", entry.status],
        ["Audio file", entry.options.audio_path],
        ["Audio name", entry.options.audio_name],
        ["Selected models", entry.options.model_names],
        ["Standard device", entry.options.device],
        ["Compute type", entry.options.compute_type],
        ["ONNX device targets", entry.options.webgpu_devices],
        ["Runs per case", entry.options.runs],
        ["Beam size", entry.options.beam_size],
        ["Language", entry.options.language],
        ["VAD filter", entry.options.vad_filter],
        ["Warmup", entry.options.warmup],
        ["Threads", entry.options.threads],
        ["Model directory", entry.options.model_dir],
    ]
    rows.extend(
        [field_name, value]
        for field_name, value in entry.environment.summary_details().items()
        if _display_value(value)
    )
    return rows


def _environment_export_values(environment: BenchmarkEnvironment) -> list[Any]:
    frameworks = [
        f"{name} {version}" for name, version in environment.frameworks.items()
    ]
    return [
        environment.os,
        environment.python,
        environment.cpu,
        environment.logical_cpus,
        environment.memory,
        _display_value(environment.gpus),
        _display_value(frameworks),
        environment.node,
    ]


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(_escape_markdown_cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _header in headers) + " |",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join(_escape_markdown_cell(value) for value in row) + " |"
        )
    return "\n".join(lines)


def _escape_markdown_cell(value: Any) -> str:
    text = _display_value(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _worksheet_xml(rows: list[list[Any]]) -> str:
    body: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row, start=1):
            reference = f"{_column_name(column_index)}{row_index}"
            cells.append(_cell_xml(reference, value))
        body.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(body)}</sheetData>"
        "</worksheet>"
    )


def _cell_xml(reference: str, value: Any) -> str:
    if isinstance(value, bool):
        text = "TRUE" if value else "FALSE"
    elif (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return f'<c r="{reference}"><v>{value}</v></c>'
    else:
        text = _display_value(value)
    return (
        f'<c r="{reference}" t="inlineStr"><is><t>'
        f"{escape(text)}"
        "</t></is></c>"
    )


def _display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_display_value(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _content_types_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""


def _root_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def _workbook_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Benchmark Results" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>"""


def _workbook_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_limit(value: int) -> int:
    try:
        keep = int(value)
    except (TypeError, ValueError):
        return 1
    if keep < 0:
        return 0
    return keep
