"""Out-of-process benchmark worker.

The local benchmark loads faster-whisper / ONNX models back-to-back and runs
inference on each. Model loading in particular does not release the Python GIL
reliably, so running it in a background *thread* of the GUI process still
freezes the Qt UI. This worker runs the exact same pure ``run_benchmark_cases``
in a dedicated child process and streams progress/case events back to the
parent as prefixed JSON lines on stdout. The GUI process only reads those lines,
so it stays fully responsive no matter what the benchmark does.

The parent launcher and event protocol live in ``benchmark_process.py``.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .local_benchmark import BenchmarkCancelled, run_benchmark_cases

# Prefix that frames a benchmark event line on stdout. Library noise printed to
# stdout by faster-whisper / onnxruntime is ignored by the parent because it
# does not carry this prefix.
BENCHMARK_EVENT_PREFIX = "@@STTBENCH@@"


def _emit(event: dict) -> None:
    stream = sys.stdout
    if stream is None:
        return
    stream.write(f"{BENCHMARK_EVENT_PREFIX}{json.dumps(event)}\n")
    stream.flush()


def run_from_options(options: dict) -> int:
    def progress_callback(text: str) -> None:
        _emit({"event": "progress", "text": str(text)})

    def case_callback(case) -> None:
        _emit({"event": "case", "case": asdict(case)})

    webgpu_devices = options.get("webgpu_devices")
    if isinstance(webgpu_devices, tuple):
        webgpu_devices = list(webgpu_devices)

    try:
        run_benchmark_cases(
            audio_path=str(options.get("audio_path", "")),
            model_names=list(options.get("model_names") or []),
            device=str(options.get("device", "auto")),
            compute_type=str(options.get("compute_type", "int8")),
            runs=int(options.get("runs", 1)),
            beam_size=int(options.get("beam_size", 5)),
            language=options.get("language"),
            vad_filter=bool(options.get("vad_filter", False)),
            warmup=bool(options.get("warmup", False)),
            threads=int(options.get("threads", 0)),
            model_dir=str(options.get("model_dir", "")),
            webgpu_devices=webgpu_devices,
            progress_callback=progress_callback,
            case_callback=case_callback,
            # Cancellation is driven by the parent terminating this process;
            # completed cases are already streamed and preserved there.
            cancel_check=None,
        )
    except BenchmarkCancelled:
        _emit({"event": "canceled"})
        return 0
    except Exception as exc:  # noqa: BLE001 - reported to the parent verbatim
        _emit({"event": "error", "message": str(exc)})
        return 1
    _emit({"event": "done"})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local STT benchmark.")
    parser.add_argument(
        "--options",
        required=True,
        help="Path to a JSON file with the benchmark options.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        options = json.loads(Path(args.options).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _emit({"event": "error", "message": f"Invalid benchmark options: {exc}"})
        return 1
    if not isinstance(options, dict):
        _emit({"event": "error", "message": "Benchmark options must be an object."})
        return 1
    return run_from_options(options)


if __name__ == "__main__":
    sys.exit(main())
