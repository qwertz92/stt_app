from __future__ import annotations

import csv
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


def _audio_duration_seconds(path: Path) -> float | None:
    try:
        import wave

        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            if rate > 0:
                return frames / float(rate)
    except Exception:
        return None
    return None


def _safe_float(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _format_seconds(value: float) -> str:
    if not math.isfinite(value):
        return "-"
    return f"{value:.2f}s"


def _format_number(value: float, digits: int = 3) -> str:
    if not math.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


@dataclass
class BenchmarkRun:
    run_index: int
    seconds: float
    audio_duration_seconds: float
    real_time_factor: float
    transcript_chars: int
    transcript_words: int
    detected_language: str
    language_probability: float


@dataclass
class BenchmarkCase:
    model: str
    device: str
    compute_type: str
    download_seconds: float
    load_seconds: float
    runs: list[BenchmarkRun]
    error: str | None = None

    @property
    def avg_seconds(self) -> float:
        if not self.runs:
            return math.nan
        return statistics.mean(run.seconds for run in self.runs)

    @property
    def avg_rtf(self) -> float:
        if not self.runs:
            return math.nan
        return statistics.mean(run.real_time_factor for run in self.runs)

    @property
    def stdev_seconds(self) -> float:
        if len(self.runs) < 2:
            return math.nan
        return statistics.pstdev(run.seconds for run in self.runs)


def _case_from_dict(data: dict[str, Any]) -> BenchmarkCase:
    runs = [BenchmarkRun(**entry) for entry in data.get("runs", [])]
    return BenchmarkCase(
        model=str(data.get("model", "")),
        device=str(data.get("device", "")),
        compute_type=str(data.get("compute_type", "")),
        download_seconds=_safe_float(data.get("download_seconds"), default=0.0),
        load_seconds=_safe_float(data.get("load_seconds"), default=math.nan),
        runs=runs,
        error=data.get("error"),
    )


def _run_case(
    *,
    audio_path: Path,
    model_name: str,
    device: str,
    compute_type: str,
    runs: int,
    beam_size: int,
    language: str | None,
    vad_filter: bool,
    warmup: bool,
    threads: int,
    model_dir: str = "",
    download_seconds: float = 0.0,
    progress_callback: Callable[[str], None] | None = None,
) -> BenchmarkCase:
    from faster_whisper import WhisperModel

    total_steps = runs + (1 if warmup else 0)
    step = 0

    if progress_callback is not None:
        progress_callback("Loading model...")
    model_start = time.perf_counter()
    model_kwargs: dict[str, Any] = {
        "device": device,
        "compute_type": compute_type,
        "cpu_threads": threads if threads > 0 else 0,
        "local_files_only": True,
    }
    if model_dir:
        model_kwargs["download_root"] = model_dir
    model = WhisperModel(model_name, **model_kwargs)
    load_seconds = time.perf_counter() - model_start

    if progress_callback is not None:
        progress_callback(f"Model loaded ({_format_seconds(load_seconds)})")

    if warmup:
        step += 1
        if progress_callback is not None:
            progress_callback(f"[{step}/{total_steps}] Warmup transcription...")
        warm_segments, _ = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
        )
        list(warm_segments)

    duration_hint = _audio_duration_seconds(audio_path) or math.nan

    all_runs: list[BenchmarkRun] = []
    for run_index in range(1, runs + 1):
        step += 1
        if progress_callback is not None:
            progress_callback(
                f"[{step}/{total_steps}] {model_name}: run {run_index}/{runs}..."
            )
        started = time.perf_counter()
        segments, info = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
        )
        pieces: list[str] = []
        for segment in segments:
            text = getattr(segment, "text", "")
            if text:
                stripped = str(text).strip()
                if stripped:
                    pieces.append(stripped)
        elapsed = time.perf_counter() - started

        transcript = " ".join(pieces).strip()
        transcript_words = len([piece for piece in transcript.split(" ") if piece])
        duration_seconds = _safe_float(
            getattr(info, "duration", duration_hint),
            default=duration_hint,
        )
        rtf = elapsed / duration_seconds if duration_seconds > 0 else math.nan

        all_runs.append(
            BenchmarkRun(
                run_index=run_index,
                seconds=elapsed,
                audio_duration_seconds=duration_seconds,
                real_time_factor=rtf,
                transcript_chars=len(transcript),
                transcript_words=transcript_words,
                detected_language=str(getattr(info, "language", "")),
                language_probability=_safe_float(
                    getattr(info, "language_probability", math.nan)
                ),
            )
        )

    return BenchmarkCase(
        model=model_name,
        device=device,
        compute_type=compute_type,
        download_seconds=download_seconds,
        load_seconds=load_seconds,
        runs=all_runs,
    )


def run_benchmark_cases(
    *,
    audio_path: str | Path,
    model_names: list[str],
    device: str = "auto",
    compute_type: str = "int8",
    runs: int = 1,
    beam_size: int = 5,
    language: str | None = None,
    vad_filter: bool = False,
    warmup: bool = False,
    threads: int = 0,
    model_dir: str = "",
    progress_callback: Callable[[str], None] | None = None,
) -> list[BenchmarkCase]:
    path = Path(audio_path)
    cases: list[BenchmarkCase] = []
    total_cases = len(model_names)
    for case_index, model_name in enumerate(model_names, start=1):
        if progress_callback is not None:
            progress_callback(
                f"[Case {case_index}/{total_cases}] {model_name} ({compute_type})"
            )
        try:
            case = _run_case(
                audio_path=path,
                model_name=model_name,
                device=device,
                compute_type=compute_type,
                runs=runs,
                beam_size=beam_size,
                language=language,
                vad_filter=vad_filter,
                warmup=warmup,
                threads=threads,
                model_dir=model_dir,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            case = BenchmarkCase(
                model=model_name,
                device=device,
                compute_type=compute_type,
                download_seconds=0.0,
                load_seconds=math.nan,
                runs=[],
                error=str(exc),
            )
        cases.append(case)
    return cases


def _successful_cases(cases: list[BenchmarkCase]) -> list[BenchmarkCase]:
    return [case for case in cases if case.error is None and case.runs]


def format_benchmark_summary(cases: list[BenchmarkCase]) -> str:
    if not cases:
        return "No benchmark results available."

    lines = ["Benchmark summary:", ""]
    for case in cases:
        status = "ok" if case.error is None else f"error: {case.error}"
        lines.append(
            f"- {case.model} ({case.compute_type}): "
            f"load={_format_seconds(case.load_seconds)}, "
            f"avg={_format_seconds(case.avg_seconds)}, "
            f"rtf={_format_number(case.avg_rtf)} [{status}]"
        )

    successful = _successful_cases(cases)
    if successful:
        fastest = min(successful, key=lambda case: case.avg_seconds)
        best_rtf = min(successful, key=lambda case: case.avg_rtf)
        lines.extend(
            [
                "",
                f"Fastest average latency: {fastest.model} ({_format_seconds(fastest.avg_seconds)})",
                f"Best real-time factor: {best_rtf.model} ({_format_number(best_rtf.avg_rtf)})",
                "RTF < 1.0 means faster than real-time.",
            ]
        )
    return "\n".join(lines)


def _write_csv(path: Path, cases: list[BenchmarkCase]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
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
                "avg_seconds",
                "stdev_seconds",
                "avg_rtf",
                "status",
                "error",
            ],
        )
        writer.writeheader()

        for case in cases:
            status = "ok" if case.error is None else "error"
            for run in case.runs:
                writer.writerow(
                    {
                        "row_type": "run",
                        "model": case.model,
                        "device": case.device,
                        "compute_type": case.compute_type,
                        "run_index": run.run_index,
                        "seconds": run.seconds,
                        "audio_duration_seconds": run.audio_duration_seconds,
                        "real_time_factor": run.real_time_factor,
                        "transcript_chars": run.transcript_chars,
                        "transcript_words": run.transcript_words,
                        "detected_language": run.detected_language,
                        "language_probability": run.language_probability,
                        "download_seconds": case.download_seconds,
                        "load_seconds": case.load_seconds,
                        "avg_seconds": case.avg_seconds,
                        "stdev_seconds": case.stdev_seconds,
                        "avg_rtf": case.avg_rtf,
                        "status": status,
                        "error": case.error or "",
                    }
                )

            writer.writerow(
                {
                    "row_type": "summary",
                    "model": case.model,
                    "device": case.device,
                    "compute_type": case.compute_type,
                    "run_index": "",
                    "seconds": "",
                    "audio_duration_seconds": "",
                    "real_time_factor": "",
                    "transcript_chars": "",
                    "transcript_words": "",
                    "detected_language": (
                        case.runs[0].detected_language if case.runs else ""
                    ),
                    "language_probability": (
                        case.runs[0].language_probability if case.runs else ""
                    ),
                    "download_seconds": case.download_seconds,
                    "load_seconds": case.load_seconds,
                    "avg_seconds": case.avg_seconds,
                    "stdev_seconds": case.stdev_seconds,
                    "avg_rtf": case.avg_rtf,
                    "status": status,
                    "error": case.error or "",
                }
            )


__all__ = [
    "BenchmarkCase",
    "BenchmarkRun",
    "_case_from_dict",
    "_format_number",
    "_format_seconds",
    "_run_case",
    "_safe_float",
    "_successful_cases",
    "_write_csv",
    "format_benchmark_summary",
    "run_benchmark_cases",
]