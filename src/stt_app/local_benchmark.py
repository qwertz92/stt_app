from __future__ import annotations

import csv
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .benchmark_environment import BenchmarkEnvironment
from .config import (
    LOCAL_MODEL_RUNTIME,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_ONNX_MODEL_PRECISION,
    LOCAL_WEBGPU_BENCHMARK_DEVICE_GROUPS,
)


class BenchmarkCancelled(RuntimeError):
    """Raised when a benchmark run is canceled between measurable steps."""


def _raise_if_canceled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise BenchmarkCancelled("Benchmark canceled.")


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


def normalize_webgpu_benchmark_devices(
    value: str | list[str] | tuple[str, ...] | None,
) -> list[str]:
    if value is None:
        return ["auto"]
    if isinstance(value, str):
        if value in LOCAL_WEBGPU_BENCHMARK_DEVICE_GROUPS:
            return list(LOCAL_WEBGPU_BENCHMARK_DEVICE_GROUPS[value])
        raw_items = value.split(",")
    else:
        raw_items = list(value)

    devices: list[str] = []
    for item in raw_items:
        device = str(item or "").strip().lower()
        if not device:
            continue
        if device in LOCAL_WEBGPU_BENCHMARK_DEVICE_GROUPS:
            for grouped_device in LOCAL_WEBGPU_BENCHMARK_DEVICE_GROUPS[device]:
                if grouped_device not in devices:
                    devices.append(grouped_device)
            continue
        if device not in {"auto", "gpu", "cpu", "dml", "webgpu"}:
            raise ValueError(
                "Unsupported ONNX device target "
                f"'{device}'. Use auto, gpu, cpu, dml, webgpu, 'gpu,cpu', or all."
            )
        if device not in devices:
            devices.append(device)
    return devices or ["auto"]


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
    runtime_details: str = ""

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
        runtime_details=str(data.get("runtime_details", "")),
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
    cancel_check: Callable[[], bool] | None = None,
) -> BenchmarkCase:
    from faster_whisper import WhisperModel

    total_steps = runs + (1 if warmup else 0)
    step = 0

    _raise_if_canceled(cancel_check)
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
    _raise_if_canceled(cancel_check)

    if progress_callback is not None:
        progress_callback(f"Model loaded ({_format_seconds(load_seconds)})")

    if warmup:
        step += 1
        _raise_if_canceled(cancel_check)
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
        _raise_if_canceled(cancel_check)
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


def _run_onnx_case(
    *,
    audio_path: Path,
    model_name: str,
    runs: int,
    language: str | None,
    warmup: bool,
    device: str = "auto",
    vad_filter: bool = False,
    model_dir: str = "",
    progress_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> BenchmarkCase:
    from .transcriber.local_nemotron import LocalNemotronTranscriber
    from .transcriber.local_webgpu_asr import LocalOnnxWebGpuTranscriber

    total_steps = runs + (1 if warmup else 0)
    step = 0
    language_mode = language or (
        "auto" if model_name in LOCAL_NEMOTRON_MODEL_SIZES else "de"
    )

    _raise_if_canceled(cancel_check)
    if progress_callback is not None:
        progress_callback("Loading local ONNX model...")
    model_start = time.perf_counter()
    if model_name in LOCAL_NEMOTRON_MODEL_SIZES:
        provider_order = {
            "cpu": ("cpu",),
            "dml": ("dml",),
        }.get(device, ("dml", "cpu"))
        transcriber = LocalNemotronTranscriber(
            model_size=model_name,
            language_mode=language_mode,
            provider_order=provider_order,
            use_runtime_vad=vad_filter,
            model_dir=model_dir,
        )
    else:
        transcriber = LocalOnnxWebGpuTranscriber(
            model_size=model_name,
            language_mode=language_mode,
            device=device,
            model_dir=model_dir,
        )
    try:
        transcriber.preload_model()
        load_seconds = time.perf_counter() - model_start
        _raise_if_canceled(cancel_check)
        runtime_device = transcriber.runtime_device or "auto"
        final_runtime_device = runtime_device
        runtime_details = str(
            getattr(transcriber, "runtime_details_text", "") or ""
        )

        if progress_callback is not None:
            progress_callback(
                f"Model loaded on {runtime_device} ({_format_seconds(load_seconds)})"
            )

        if warmup:
            step += 1
            _raise_if_canceled(cancel_check)
            if progress_callback is not None:
                progress_callback(f"[{step}/{total_steps}] Warmup transcription...")
            transcriber.transcribe_batch(audio_path)
            final_runtime_device = transcriber.runtime_device or final_runtime_device
            runtime_details = str(
                getattr(transcriber, "runtime_details_text", "") or runtime_details
            )

        duration_hint = _audio_duration_seconds(audio_path) or math.nan

        all_runs: list[BenchmarkRun] = []
        for run_index in range(1, runs + 1):
            step += 1
            _raise_if_canceled(cancel_check)
            if progress_callback is not None:
                progress_callback(
                    f"[{step}/{total_steps}] {model_name}: run {run_index}/{runs}..."
                )
            started = time.perf_counter()
            transcript = transcriber.transcribe_batch(audio_path)
            elapsed = time.perf_counter() - started
            final_runtime_device = transcriber.runtime_device or final_runtime_device
            runtime_details = str(
                getattr(transcriber, "runtime_details_text", "") or runtime_details
            )

            transcript_words = len(
                [piece for piece in transcript.split(" ") if piece]
            )
            rtf = elapsed / duration_hint if duration_hint > 0 else math.nan

            all_runs.append(
                BenchmarkRun(
                    run_index=run_index,
                    seconds=elapsed,
                    audio_duration_seconds=duration_hint,
                    real_time_factor=rtf,
                    transcript_chars=len(transcript),
                    transcript_words=transcript_words,
                    detected_language=language_mode,
                    language_probability=math.nan,
                )
            )
    finally:
        transcriber.close()

    return BenchmarkCase(
        model=model_name,
        device=final_runtime_device,
        compute_type=f"onnx-{LOCAL_ONNX_MODEL_PRECISION.get(model_name, 'q4')}",
        download_seconds=0.0,
        load_seconds=load_seconds,
        runs=all_runs,
        runtime_details=runtime_details,
    )


def _run_webgpu_case(**kwargs) -> BenchmarkCase:
    """Compatibility entry point for existing WebGPU benchmark callers."""
    return _run_onnx_case(**kwargs)


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
    webgpu_devices: str | list[str] | tuple[str, ...] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    case_callback: Callable[[BenchmarkCase], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[BenchmarkCase]:
    path = Path(audio_path)
    cases: list[BenchmarkCase] = []
    webgpu_device_targets = normalize_webgpu_benchmark_devices(webgpu_devices)
    total_cases = sum(
        len(webgpu_device_targets)
        if LOCAL_MODEL_RUNTIME.get(model_name) == "onnx-webgpu"
        else 1
        for model_name in model_names
    )
    case_index = 0
    for model_name in model_names:
        _raise_if_canceled(cancel_check)
        runtime = LOCAL_MODEL_RUNTIME.get(model_name, "")
        device_targets = (
            webgpu_device_targets
            if runtime == "onnx-webgpu"
            else [device]
        )
        for device_target in device_targets:
            _raise_if_canceled(cancel_check)
            case_index += 1
            display_compute_type = (
                f"onnx-{LOCAL_ONNX_MODEL_PRECISION.get(model_name, 'q4')}"
                if runtime in {"onnx-webgpu", "onnxruntime-genai"}
                else compute_type
            )
            if progress_callback is not None:
                progress_callback(
                    f"[Case {case_index}/{total_cases}] "
                    f"{model_name} ({device_target}/{display_compute_type})"
                )
            try:
                if runtime == "faster-whisper":
                    case = _run_case(
                        audio_path=path,
                        model_name=model_name,
                        device=device_target,
                        compute_type=compute_type,
                        runs=runs,
                        beam_size=beam_size,
                        language=language,
                        vad_filter=vad_filter,
                        warmup=warmup,
                        threads=threads,
                        model_dir=model_dir,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                    )
                elif runtime == "onnx-webgpu":
                    case = _run_webgpu_case(
                        audio_path=path,
                        model_name=model_name,
                        runs=runs,
                        language=language,
                        warmup=warmup,
                        device=device_target,
                        vad_filter=vad_filter,
                        model_dir=model_dir,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                    )
                elif runtime == "onnxruntime-genai":
                    case = _run_onnx_case(
                        audio_path=path,
                        model_name=model_name,
                        runs=runs,
                        language=language,
                        warmup=warmup,
                        device=device_target,
                        vad_filter=vad_filter,
                        model_dir=model_dir,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                    )
                else:
                    raise ValueError(
                        f"Benchmark runtime for '{model_name}' is unknown. "
                        "Restart the app after updating, then refresh the local "
                        "model inventory."
                    )
            except BenchmarkCancelled:
                raise
            except Exception as exc:
                case = BenchmarkCase(
                    model=model_name,
                    device=device_target,
                    compute_type=display_compute_type,
                    download_seconds=0.0,
                    load_seconds=math.nan,
                    runs=[],
                    error=str(exc),
                )
            cases.append(case)
            if case_callback is not None:
                case_callback(case)
    return cases


def _successful_cases(cases: list[BenchmarkCase]) -> list[BenchmarkCase]:
    return [case for case in cases if case.error is None and case.runs]


def _format_detail_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_format_detail_value(item) for item in value)
    if value is None or value == "":
        return "-"
    return str(value)


def format_benchmark_summary(
    cases: list[BenchmarkCase],
    details: dict[str, Any] | None = None,
    environment: BenchmarkEnvironment | None = None,
) -> str:
    if not cases:
        lines = ["No benchmark results available."]
        if details:
            lines.extend(["", "Benchmark details:"])
            lines.extend(
                f"- {key}: {_format_detail_value(value)}"
                for key, value in details.items()
            )
        if environment is not None:
            lines.extend(["", "System details:"])
            lines.extend(
                f"- {key}: {_format_detail_value(value)}"
                for key, value in environment.summary_details().items()
                if _format_detail_value(value) != "-"
            )
        return "\n".join(lines)

    lines = ["Benchmark summary:", ""]
    if details:
        lines.extend(["Benchmark details:"])
        lines.extend(
            f"- {key}: {_format_detail_value(value)}"
            for key, value in details.items()
        )
        lines.append("")
    if environment is not None:
        lines.extend(["System details:"])
        lines.extend(
            f"- {key}: {_format_detail_value(value)}"
            for key, value in environment.summary_details().items()
            if _format_detail_value(value) != "-"
        )
        lines.append("")

    for case in cases:
        status = "ok" if case.error is None else f"error: {case.error}"
        if case.runtime_details:
            status = f"{status}; runtime: {case.runtime_details}"
        lines.append(
            f"- {case.model} ({case.device}/{case.compute_type}): "
            f"load={_format_seconds(case.load_seconds)}, "
            f"avg={_format_seconds(case.avg_seconds)}, "
            f"rtf={_format_number(case.avg_rtf)} [{status}]"
        )
        # With more than one run, also list each run so outliers/variance are
        # visible instead of only the average.
        if len(case.runs) > 1:
            for run in case.runs:
                lines.append(
                    f"    run {run.run_index}: "
                    f"{_format_seconds(run.seconds)}, "
                    f"rtf={_format_number(run.real_time_factor)}"
                )

    successful = _successful_cases(cases)
    if successful:
        fastest = min(successful, key=lambda case: case.avg_seconds)
        best_rtf = min(successful, key=lambda case: case.avg_rtf)
        lines.extend(
            [
                "",
                "Fastest average latency: "
                f"{fastest.model} on {fastest.device} "
                f"({_format_seconds(fastest.avg_seconds)})",
                "Best real-time factor: "
                f"{best_rtf.model} on {best_rtf.device} "
                f"({_format_number(best_rtf.avg_rtf)})",
                "RTF < 1.0 means faster than real-time.",
            ]
        )
    return "\n".join(lines)


def _write_csv(
    path: Path,
    cases: list[BenchmarkCase],
    environment: BenchmarkEnvironment | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
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
                "avg_seconds",
                "stdev_seconds",
                "avg_rtf",
                "status",
                "runtime_details",
                "error",
            ],
        )
        writer.writeheader()

        environment_row = _environment_csv_values(environment)
        for case in cases:
            status = "ok" if case.error is None else "error"
            for run in case.runs:
                writer.writerow(
                    {
                        **environment_row,
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
                        "runtime_details": case.runtime_details,
                        "error": case.error or "",
                    }
                )

            writer.writerow(
                {
                    **environment_row,
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
                    "runtime_details": case.runtime_details,
                    "error": case.error or "",
                }
            )


def _environment_csv_values(
    environment: BenchmarkEnvironment | None,
) -> dict[str, Any]:
    if environment is None:
        return {
            "environment_os": "",
            "environment_python": "",
            "environment_cpu": "",
            "environment_logical_cpus": "",
            "environment_memory": "",
            "environment_gpus": "",
            "environment_frameworks": "",
            "environment_node": "",
        }
    frameworks = [
        f"{name} {version}" for name, version in environment.frameworks.items()
    ]
    return {
        "environment_os": environment.os,
        "environment_python": environment.python,
        "environment_cpu": environment.cpu,
        "environment_logical_cpus": environment.logical_cpus,
        "environment_memory": environment.memory,
        "environment_gpus": ", ".join(environment.gpus),
        "environment_frameworks": ", ".join(frameworks),
        "environment_node": environment.node,
    }


__all__ = [
    "BenchmarkCase",
    "BenchmarkCancelled",
    "BenchmarkRun",
    "_case_from_dict",
    "_format_number",
    "_format_seconds",
    "_run_case",
    "_safe_float",
    "_successful_cases",
    "_write_csv",
    "format_benchmark_summary",
    "normalize_webgpu_benchmark_devices",
    "run_benchmark_cases",
]
