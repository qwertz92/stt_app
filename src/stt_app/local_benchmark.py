from __future__ import annotations

import csv
import dataclasses
import io
import logging
import math
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .benchmark_environment import BenchmarkEnvironment
from .config import (
    CANARY_MODEL_SIZE,
    LOCAL_MODEL_RUNTIME,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_ONNX_ASR_MODEL_SIZES,
    LOCAL_ONNX_MODEL_PRECISION,
    LOCAL_WEBGPU_BENCHMARK_DEVICE_GROUPS,
    nemotron_provider_order,
)
from .csv_safety import export_safe_text, spreadsheet_safe_mapping
from .persistence import atomic_write_bytes


class BenchmarkCancelled(RuntimeError):
    """Raised when a benchmark run is canceled between measurable steps."""


_LOGGER = logging.getLogger(__name__)


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
    # Keep the actual model output so benchmarks can compare recognition
    # quality as well as speed.  The default preserves compatibility with
    # history written before transcript capture was introduced.
    transcript: str = ""


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


def _run_from_dict(data: dict[str, Any]) -> BenchmarkRun:
    """Drop fields this build does not declare instead of dying on them.

    `BenchmarkRun(**entry)` raises `TypeError` for one unexpected key,
    and `%APPDATA%` is shared between builds -- `transcript` itself was
    added to this dataclass later, so the same edit read the other way
    round is a newer build's field reaching an older one. That TypeError
    escaped `benchmark_history`'s `except ValueError`, so the store's
    backup recovery never ran and `SettingsDialog.__init__` -- which
    calls `recent_entries` with no guard -- could not build at all.
    """
    fields = {f.name for f in dataclasses.fields(BenchmarkRun)}
    return BenchmarkRun(**{k: v for k, v in data.items() if k in fields})


def _case_from_dict(data: dict[str, Any]) -> BenchmarkCase:
    raw_runs = data.get("runs")
    runs = [
        _run_from_dict(entry)
        for entry in (raw_runs if isinstance(raw_runs, list) else [])
        if isinstance(entry, dict)
    ]
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
    runtime_model = getattr(model, "model", None)
    resolved_device = str(getattr(runtime_model, "device", "") or device)
    _raise_if_canceled(cancel_check)

    if progress_callback is not None:
        progress_callback(
            f"Model loaded on {resolved_device} ({_format_seconds(load_seconds)})"
        )

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
            # `segments` is a generator: decoding happens as it is consumed,
            # so this is the same granularity the app's own faster-whisper
            # cancel has. Without it a single long recording is uninterruptible
            # even though the run loop above polls between runs.
            _raise_if_canceled(cancel_check)
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
                transcript=transcript,
            )
        )

    return BenchmarkCase(
        model=model_name,
        device=resolved_device,
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
    from .transcriber.local_onnx_asr import LocalOnnxAsrTranscriber
    from .transcriber.local_webgpu_asr import LocalOnnxWebGpuTranscriber

    total_steps = runs + (1 if warmup else 0)
    step = 0
    # Canary has no auto-detect: onnx-asr hardcodes the source/target token, so
    # a wrong language makes it *translate* and the benchmark would record the
    # translation as the transcript. Defaulting cannot be right here — the
    # sample's language is not knowable — so refuse instead of guessing.
    # (Picking "the model's first declared mode" looked like a fix but is a
    # no-op for Canary, whose first mode is "de".)
    if model_name == CANARY_MODEL_SIZE and not language:
        raise ValueError(
            "Canary cannot detect the language. Choose the language spoken in "
            "the sample before benchmarking it; with the wrong one this model "
            "translates instead of transcribing."
        )
    # Parakeet ignores the language and only offers "auto".
    default_language = (
        "auto"
        if model_name in LOCAL_NEMOTRON_MODEL_SIZES
        or model_name in LOCAL_ONNX_ASR_MODEL_SIZES
        else "de"
    )
    language_mode = language or default_language

    _raise_if_canceled(cancel_check)
    if progress_callback is not None:
        progress_callback("Loading local ONNX model...")
    model_start = time.perf_counter()
    if model_name in LOCAL_NEMOTRON_MODEL_SIZES:
        provider_order = nemotron_provider_order(device)
        transcriber = LocalNemotronTranscriber(
            model_size=model_name,
            language_mode=language_mode,
            provider_order=provider_order,
            use_runtime_vad=vad_filter,
            model_dir=model_dir,
        )
    elif model_name in LOCAL_ONNX_ASR_MODEL_SIZES:
        # CPU-only runtime: the device policy does not apply, and Parakeet
        # normalizes any language to its single supported mode itself.
        transcriber = LocalOnnxAsrTranscriber(
            model_size=model_name,
            language_mode=language_mode,
            model_dir=model_dir,
        )
    else:
        transcriber = LocalOnnxWebGpuTranscriber(
            model_size=model_name,
            language_mode=language_mode,
            device=device,
            model_dir=model_dir,
        )
    # `_raise_if_canceled` only polls between measurable steps, so before this
    # the load itself was uninterruptible: an uncached model downloads from the
    # transcriber's own load path and waits on the machine-wide slot, and the
    # inference below is one blocking call. In the app a cancel kills the whole
    # worker process, but `run_benchmark_cases` is also called in-process by
    # the CLI, where nothing else could stop it.
    #
    # Installed inside the `try`, so a setter that raises still reaches the
    # `close()` below instead of leaking a constructed runtime.
    set_cancel = getattr(transcriber, "set_cancel_check", None)
    try:
        if callable(set_cancel) and cancel_check is not None:
            set_cancel(cancel_check)
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
                    transcript=str(transcript or "").strip(),
                )
            )
    finally:
        if callable(set_cancel) and cancel_check is not None:
            try:
                set_cancel(None)
            except Exception:
                # Never let the cleanup call swallow `close()`: the runtime is
                # the expensive thing here, and an unclosed one keeps a model
                # -- or, for the Node runtime, a child process -- alive for
                # the rest of the benchmark.
                _LOGGER.exception("Failed to clear the benchmark cancel hook")
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


def benchmark_device_targets(
    runtime: str,
    requested: list[str],
    fallback_device: str,
) -> list[str]:
    """Device targets to measure for one model.

    ONNX/WebGPU models run once per requested target. Nemotron sits in the same
    ONNX Device picker in Settings, so it has to be measurable on a pinned
    device too — otherwise the General tab offers a choice the benchmark can
    never compare, and "All explicit targets" silently measured it on `auto`
    only. It runs on ONNX Runtime GenAI, which has no WebGPU provider, so
    several requested targets resolve to the same provider order; each is
    renamed to what it will actually run on and duplicates are dropped, because
    reporting one configuration twice under two names is worse than not
    offering it.
    """
    if runtime == "onnx-webgpu":
        return list(requested)
    if runtime != "onnxruntime-genai":
        return [fallback_device]
    targets: list[str] = []
    for target in requested:
        order = nemotron_provider_order(target)
        resolved = target if len(order) > 1 else order[0]
        if resolved not in targets:
            targets.append(resolved)
    return targets or [fallback_device]


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
    # Function-local. It was originally written this way because
    # `stt_app.transcriber.__init__` imported all seven remote providers, and
    # at module scope that reached the download and inventory-scan workers,
    # which exist precisely to stay cheap. The package resolves its names
    # lazily now, so the saving is much smaller -- `base` pulls only stdlib
    # plus `config` -- and this stays function-local only to keep the module
    # importable without touching the transcriber package at all.
    from .transcriber.base import TranscriptionCanceled

    path = Path(audio_path)
    cases: list[BenchmarkCase] = []
    webgpu_device_targets = normalize_webgpu_benchmark_devices(webgpu_devices)
    total_cases = sum(
        len(
            benchmark_device_targets(
                LOCAL_MODEL_RUNTIME.get(model_name, ""),
                webgpu_device_targets,
                device,
            )
        )
        for model_name in model_names
    )
    case_index = 0
    for model_name in model_names:
        _raise_if_canceled(cancel_check)
        runtime = LOCAL_MODEL_RUNTIME.get(model_name, "")
        device_targets = benchmark_device_targets(
            runtime, webgpu_device_targets, device
        )
        for device_target in device_targets:
            _raise_if_canceled(cancel_check)
            case_index += 1
            display_compute_type = (
                f"onnx-{LOCAL_ONNX_MODEL_PRECISION.get(model_name, 'q4')}"
                if runtime in {"onnx-webgpu", "onnxruntime-genai", "onnx-asr"}
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
                elif runtime in {"onnxruntime-genai", "onnx-asr"}:
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
            except TranscriptionCanceled as exc:
                # A transcriber's own model download hit the cancelled
                # download slot (an explicit cancel, or shutdown). That ends
                # the run; recording it as a failed case would leave a
                # permanent "error" row for something the user stopped.
                raise BenchmarkCancelled(str(exc) or "Benchmark canceled.") from exc
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
    """Build the whole file, then replace the destination in one step.

    The path comes from `--csv-out`, so it is routinely a file that already
    exists. Writing into it directly truncates it before the first row is
    produced: measured on a transcript carrying one lone surrogate, an 880-byte
    file of the user's became a 422-byte fragment, because `UnicodeEncodeError`
    landed part-way through. That is the same defect the three settings-dialog
    exporters already fixed; this writer was the last one left, and it also
    skipped their `export_safe_text` pass, so a control byte a benchmarked
    model emits was written raw into the CSV here and replaced with U+FFFD
    there -- the same run, two different files.
    """
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
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
            "transcript",
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
                spreadsheet_safe_mapping(
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
                        "transcript": run.transcript,
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
            )

        writer.writerow(
            spreadsheet_safe_mapping(
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
                    "transcript": "",
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
        )

    atomic_write_bytes(path, export_safe_text(buffer.getvalue()).encode("utf-8"))


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
    "BenchmarkCancelled",
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
    "normalize_webgpu_benchmark_devices",
    "run_benchmark_cases",
]
