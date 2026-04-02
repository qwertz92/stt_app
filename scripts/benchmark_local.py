from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stt_app.local_benchmark import (
    BenchmarkCase,
    BenchmarkRun,
    _case_from_dict,
    _format_number,
    _format_seconds,
    _run_case as _shared_run_case,
    _safe_float,
    _successful_cases as _shared_successful_cases,
    _write_csv as _shared_write_csv,
)
from faster_whisper.utils import _MODELS


def _parse_csv(value: str | None, *, fallback: list[str]) -> list[str]:
    if not value:
        return fallback
    items = [part.strip() for part in value.split(",")]
    return [item for item in items if item]


def _validate_models(models: list[str]) -> list[str]:
    unknown = [model for model in models if model not in _MODELS]
    if unknown:
        names = ", ".join(sorted(_MODELS.keys()))
        raise ValueError(
            "Unknown model(s): "
            + ", ".join(unknown)
            + ". Available models: "
            + names
        )
    return models


def _bytes_to_human(value: int | None) -> str:
    if value is None or value < 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"


def _resolve_model_size_bytes(model_name: str) -> int | None:
    # Best-effort lookup using Hugging Face metadata.
    try:
        from huggingface_hub import model_info  # type: ignore
    except Exception:
        return None

    repo_id = _MODELS.get(model_name)
    if not repo_id:
        return None

    try:
        info = model_info(repo_id, files_metadata=True)
    except Exception:
        return None

    siblings = getattr(info, "siblings", None) or []
    total = 0
    for item in siblings:
        size = getattr(item, "size", None)
        if isinstance(size, int) and size > 0:
            total += size
    if total <= 0:
        return None
    return total


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark local faster-whisper transcription runs over one audio file."
        )
    )
    parser.add_argument(
        "audio_path",
        nargs="?",
        type=Path,
        help="Path to a local audio file (wav/mp3/m4a/etc).",
    )
    parser.add_argument(
        "--models",
        default="tiny,base,small,medium,large-v3",
        help="Comma-separated model names to benchmark.",
    )
    parser.add_argument(
        "--device",
        default="auto",
        help="Device passed to WhisperModel (e.g. auto, cpu, cuda).",
    )
    parser.add_argument(
        "--compute-types",
        default="int8",
        help="Comma-separated compute types (e.g. int8,float32,float16).",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of measured transcription runs per model/compute-type.",
    )
    parser.add_argument(
        "--beam-size",
        type=int,
        default=5,
        help="Beam size passed to transcribe.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional language code (e.g. de, en). Default: auto detect.",
    )
    parser.add_argument(
        "--vad-filter",
        action="store_true",
        default=False,
        help="Enable Silero VAD filtering in faster-whisper.",
    )
    parser.add_argument(
        "--warmup",
        action="store_true",
        default=False,
        help="Run one warmup transcription before measurements.",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=0,
        help="CPU thread count for CTranslate2 (0 = library default).",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        default=False,
        help="Print supported faster-whisper model names and exit.",
    )
    parser.add_argument(
        "--show-model-sizes",
        action="store_true",
        default=False,
        help="Attempt to fetch model repository sizes from Hugging Face metadata.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write full benchmark result JSON to this path.",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Write benchmark runs and summary rows to this CSV file.",
    )
    parser.add_argument(
        "--no-best",
        action="store_true",
        default=False,
        help="Disable the best-model comparison view in console output.",
    )
    parser.add_argument(
        "--isolated-case",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Run each case in an isolated subprocess. "
            "Recommended on Windows so Ctrl+C can abort a running case reliably."
        ),
    )
    return parser


def _print_model_table(show_sizes: bool) -> None:
    print("Supported faster-whisper models:")
    print("")
    header = f"{'Model':<24} {'Hub Repo':<40} {'Approx Repo Size':<18}"
    print(header)
    print("-" * len(header))
    for model in sorted(_MODELS.keys()):
        repo = _MODELS[model]
        size_human = "-"
        if show_sizes:
            size_human = _bytes_to_human(_resolve_model_size_bytes(model))
        print(f"{model:<24} {repo:<40} {size_human:<18}")


def _ensure_models_available(
    model_names: list[str],
) -> dict[str, float]:
    """Pre-download all models not yet in the HuggingFace cache.

    Returns a dict mapping model_name → download_seconds (0.0 if cached).
    Download progress bars are shown by huggingface_hub automatically.
    If uncached models are found, the user is prompted before downloading.
    """
    from huggingface_hub import snapshot_download

    download_times: dict[str, float] = {}

    # Phase 1: classify models as cached or uncached.
    cached: list[str] = []
    uncached: list[str] = []
    for model_name in model_names:
        repo_id = _MODELS.get(model_name)
        if not repo_id:
            download_times[model_name] = 0.0
            continue
        try:
            snapshot_download(repo_id, local_files_only=True)
            cached.append(model_name)
            download_times[model_name] = 0.0
        except Exception:
            uncached.append(model_name)

    if cached:
        print(f"  Cached models: {', '.join(cached)}")
    if not uncached:
        print("  All requested models are cached.")
        return download_times

    # Phase 2: show uncached models with estimated sizes and ask user.
    print("")
    print("  The following models need to be downloaded:")
    print("")
    _APPROX_SIZES: dict[str, str] = {
        "tiny": "~75 MB",
        "base": "~141 MB",
        "small": "~484 MB",
        "medium": "~1.4 GB",
        "large-v3": "~3.1 GB",
        "large-v3-turbo": "~809 MB",
        "distil-large-v3.5": "~756 MB",
    }
    for model_name in uncached:
        size_hint = _APPROX_SIZES.get(model_name, "unknown size")
        print(f"    • {model_name}  ({size_hint})")
    print("")

    try:
        answer = input(
            "  Download these models now? [y]es / [s]kip (benchmark cached only) / [a]bort: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "a"

    if answer in ("a", "abort"):
        print("  Aborted by user.")
        sys.exit(0)

    if answer in ("s", "skip"):
        print("  Skipping downloads — benchmarking cached models only.")
        for model_name in uncached:
            download_times.pop(model_name, None)
        return download_times

    # Phase 3: download uncached models.
    for i, model_name in enumerate(uncached, 1):
        repo_id = _MODELS.get(model_name)
        if not repo_id:
            download_times[model_name] = 0.0
            continue

        label = f"  [{i}/{len(uncached)}] {model_name}"
        print(f"{label} — downloading ({repo_id})...")
        dl_start = time.perf_counter()
        try:
            snapshot_download(repo_id)
            elapsed = time.perf_counter() - dl_start
            print(f"  Downloaded in {_format_seconds(elapsed)}")
            download_times[model_name] = elapsed
        except Exception as exc:
            elapsed = time.perf_counter() - dl_start
            print(f"  Download failed ({_format_seconds(elapsed)}): {exc}")
            download_times[model_name] = elapsed

    return download_times


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
    download_seconds: float = 0.0,
) -> BenchmarkCase:
    return _shared_run_case(
        audio_path=audio_path,
        model_name=model_name,
        device=device,
        compute_type=compute_type,
        runs=runs,
        beam_size=beam_size,
        language=language,
        vad_filter=vad_filter,
        warmup=warmup,
        threads=threads,
        download_seconds=download_seconds,
        progress_callback=lambda text: print(f"  {text}", flush=True),
    )


def _run_case_worker(params: dict[str, Any], output_queue) -> None:
    try:
        case = _run_case(**params)
        output_queue.put({"ok": True, "case": asdict(case)})
    except KeyboardInterrupt:
        output_queue.put({"ok": False, "error": "Interrupted by user."})
    except Exception as exc:
        output_queue.put({"ok": False, "error": str(exc)})


def _run_case_isolated(params: dict[str, Any]) -> BenchmarkCase:
    context = mp.get_context("spawn")
    output_queue = context.Queue()
    process = context.Process(
        target=_run_case_worker,
        args=(params, output_queue),
        daemon=True,
    )
    process.start()

    try:
        while process.is_alive():
            process.join(timeout=0.15)
    except KeyboardInterrupt:
        process.terminate()
        process.join(timeout=2.0)
        raise

    payload: dict[str, Any] | None = None
    if not output_queue.empty():
        payload = output_queue.get_nowait()

    if payload and payload.get("ok"):
        raw_case = payload.get("case", {})
        if isinstance(raw_case, dict):
            return _case_from_dict(raw_case)
        return BenchmarkCase(
            model=str(params.get("model_name", "")),
            device=str(params.get("device", "")),
            compute_type=str(params.get("compute_type", "")),
            download_seconds=_safe_float(params.get("download_seconds"), default=0.0),
            load_seconds=math.nan,
            runs=[],
            error="Invalid worker result payload.",
        )

    error_text = ""
    if payload and isinstance(payload.get("error"), str):
        error_text = payload["error"]
    if not error_text:
        error_text = f"Worker exited with code {process.exitcode}."
    return BenchmarkCase(
        model=str(params.get("model_name", "")),
        device=str(params.get("device", "")),
        compute_type=str(params.get("compute_type", "")),
        download_seconds=_safe_float(params.get("download_seconds"), default=0.0),
        load_seconds=math.nan,
        runs=[],
        error=error_text,
    )


def _print_results(cases: list[BenchmarkCase]) -> None:
    print("")
    print("Benchmark summary:")
    print("")
    header = (
        f"{'Model':<14} {'Device':<8} {'Compute':<10} {'Download':<10} {'Load':<9} "
        f"{'Avg':<9} {'StdDev':<9} {'RTF':<8} {'Lang':<8} {'Status':<10}"
    )
    print(header)
    print("-" * len(header))

    for case in cases:
        language = "-"
        if case.runs:
            language = case.runs[0].detected_language or "-"
        status = "ok" if case.error is None else "error"
        dl_str = _format_seconds(case.download_seconds) if case.download_seconds > 0 else "-"
        print(
            f"{case.model:<14} {case.device:<8} {case.compute_type:<10} "
            f"{dl_str:<10} "
            f"{_format_seconds(case.load_seconds):<9} "
            f"{_format_seconds(case.avg_seconds):<9} "
            f"{_format_seconds(case.stdev_seconds):<9} "
            f"{_format_number(case.avg_rtf):<8} "
            f"{language:<8} {status:<10}"
        )
        if case.error:
            print(f"  error: {case.error}")

    print("")
    print("RTF reference: < 1.0 means faster than real-time.")
    print("Download: time spent downloading (- = already cached, not counted).")
    print("Load: time to initialize the model in memory (pure load, no download).")


def _successful_cases(cases: list[BenchmarkCase]) -> list[BenchmarkCase]:
    return _shared_successful_cases(cases)


def _print_best_cases(cases: list[BenchmarkCase]) -> None:
    successful = _successful_cases(cases)
    if not successful:
        return

    fastest = min(successful, key=lambda case: case.avg_seconds)
    best_rtf = min(successful, key=lambda case: case.avg_rtf)

    print("")
    print("Best model comparison:")
    print(
        f"- Best latency: {fastest.model} ({fastest.device}/{fastest.compute_type}), "
        f"avg={_format_seconds(fastest.avg_seconds)}, rtf={_format_number(fastest.avg_rtf)}"
    )
    print(
        f"- Best RTF: {best_rtf.model} ({best_rtf.device}/{best_rtf.compute_type}), "
        f"avg={_format_seconds(best_rtf.avg_seconds)}, rtf={_format_number(best_rtf.avg_rtf)}"
    )

    print("")
    print("Top by latency:")
    ranked = sorted(successful, key=lambda case: case.avg_seconds)
    for index, case in enumerate(ranked[:3], start=1):
        print(
            f"  {index}. {case.model:<14} {case.device:<8} {case.compute_type:<10} "
            f"avg={_format_seconds(case.avg_seconds):<9} rtf={_format_number(case.avg_rtf)}"
        )


def _write_csv(path: Path, cases: list[BenchmarkCase]) -> None:
    _shared_write_csv(path, cases)


def main() -> int:
    mp.freeze_support()
    parser = _build_parser()
    args = parser.parse_args()

    if args.list_models:
        _print_model_table(show_sizes=args.show_model_sizes)
        return 0

    if args.audio_path is None:
        parser.error("audio_path is required unless --list-models is used.")
        return 2

    audio_path: Path = args.audio_path
    if not audio_path.exists():
        parser.error(f"audio_path does not exist: {audio_path}")
        return 2

    if args.runs < 1:
        parser.error("--runs must be >= 1")
        return 2

    model_names = _parse_csv(args.models, fallback=["small"])
    compute_types = _parse_csv(args.compute_types, fallback=["int8"])
    try:
        model_names = _validate_models(model_names)
    except ValueError as exc:
        parser.error(str(exc))
        return 2

    print("faster-whisper local benchmark")
    print(f"timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"audio: {audio_path.resolve()}")
    print(f"models: {', '.join(model_names)}")
    print(f"device: {args.device}")
    print(f"compute_types: {', '.join(compute_types)}")
    print(f"runs per case: {args.runs}")
    print(f"beam_size: {args.beam_size}")
    print(f"vad_filter: {args.vad_filter}")
    print(f"warmup: {args.warmup}")
    print(f"threads: {args.threads if args.threads > 0 else 'default'}")
    print(f"isolated_case: {args.isolated_case}")

    # Pre-download phase: ensure all models are available before timing.
    print("")
    print("Ensuring models are available...")
    download_times = _ensure_models_available(model_names)
    # User may have chosen to skip uncached models.
    model_names = [m for m in model_names if m in download_times]
    if not model_names:
        print("No models available for benchmarking.")
        return 0
    print("")

    cases: list[BenchmarkCase] = []
    failures = 0
    interrupted = False
    total_cases = len(model_names) * len(compute_types)
    case_index = 0

    try:
        for model_name in model_names:
            for compute_type in compute_types:
                case_index += 1
                print(
                    f"[Case {case_index}/{total_cases}] model={model_name}, "
                    f"device={args.device}, compute_type={compute_type}"
                )
                params = {
                    "audio_path": audio_path,
                    "model_name": model_name,
                    "device": args.device,
                    "compute_type": compute_type,
                    "runs": args.runs,
                    "beam_size": args.beam_size,
                    "language": args.language,
                    "vad_filter": args.vad_filter,
                    "warmup": args.warmup,
                    "threads": args.threads,
                    "download_seconds": download_times.get(model_name, 0.0),
                }
                if args.isolated_case:
                    case = _run_case_isolated(params)
                else:
                    try:
                        case = _run_case(**params)
                    except Exception as exc:
                        case = BenchmarkCase(
                            model=model_name,
                            device=args.device,
                            compute_type=compute_type,
                            download_seconds=download_times.get(model_name, 0.0),
                            load_seconds=math.nan,
                            runs=[],
                            error=str(exc),
                        )
                if case.error:
                    failures += 1
                cases.append(case)
    except KeyboardInterrupt:
        interrupted = True
        print("")
        print("Interrupted by user (Ctrl+C).")
        print("Stopped benchmark early and keeping completed results.")

    _print_results(cases)
    if not args.no_best:
        _print_best_cases(cases)

    if args.json_out is not None:
        payload = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "audio_path": str(audio_path.resolve()),
            "device": args.device,
            "compute_types": compute_types,
            "models": model_names,
            "runs_per_case": args.runs,
            "beam_size": args.beam_size,
            "vad_filter": args.vad_filter,
            "warmup": args.warmup,
            "threads": args.threads,
            "results": [asdict(case) for case in cases],
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Saved JSON report to: {args.json_out.resolve()}")

    if args.csv_out is not None:
        _write_csv(args.csv_out, cases)
        print(f"Saved CSV report to: {args.csv_out.resolve()}")

    if interrupted:
        return 130
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
