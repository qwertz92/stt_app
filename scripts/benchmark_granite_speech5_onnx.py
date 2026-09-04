#!/usr/bin/env python3
"""Benchmark a Granite Speech 5 ONNX graph on one local WAV file."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import statistics
import time
from pathlib import Path

from export_granite_speech5_onnx import MODEL_ID, MODEL_REVISION, _write_json

os.environ["ORT_DISABLE_TELEMETRY"] = "1"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onnx", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--revision", default=MODEL_REVISION)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--local-files-only", action="store_true")
    return parser.parse_args(argv)


def benchmark(args: argparse.Namespace) -> dict[str, object]:
    import numpy as np
    import onnxruntime
    import soundfile
    from transformers import AutoProcessor

    if not args.onnx.is_file():
        raise FileNotFoundError(f"ONNX graph not found: {args.onnx}")
    if not args.audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {args.audio}")
    if args.threads < 1 or args.repeats < 1:
        raise ValueError("Threads and repeats must both be positive.")

    source_kwargs: dict[str, object] = {"local_files_only": args.local_files_only}
    if not Path(args.model).is_dir():
        source_kwargs["revision"] = args.revision
    processor = AutoProcessor.from_pretrained(args.model, **source_kwargs)

    samples, sampling_rate = soundfile.read(args.audio, dtype="float32")
    if samples.ndim != 1 or sampling_rate != 16_000:
        raise ValueError(
            f"Expected mono 16 kHz audio; got shape={samples.shape}, "
            f"rate={sampling_rate}."
        )
    audio_seconds = len(samples) / sampling_rate
    feature_start = time.perf_counter()
    features = processor(
        samples, sampling_rate=sampling_rate, return_tensors="np"
    ).input_features
    feature_seconds = time.perf_counter() - feature_start

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = args.threads
    options.inter_op_num_threads = 1
    options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    load_start = time.perf_counter()
    session = onnxruntime.InferenceSession(
        str(args.onnx), options, providers=["CPUExecutionProvider"]
    )
    load_seconds = time.perf_counter() - load_start

    feed = {"input_features": np.asarray(features, dtype=np.float32)}
    session.run(["logits"], feed)
    wall_times = []
    cpu_times = []
    logits = None
    for _ in range(args.repeats):
        cpu_start = time.process_time()
        wall_start = time.perf_counter()
        logits = session.run(["logits"], feed)[0]
        wall_times.append(time.perf_counter() - wall_start)
        cpu_times.append(time.process_time() - cpu_start)
    assert logits is not None

    token_ids = logits.argmax(axis=-1)
    canonical_token_bytes = token_ids.astype("<u4", copy=False).tobytes()
    transcript = processor.batch_decode(token_ids)[0]
    median_seconds = statistics.median(wall_times)
    result = {
        "onnx_path": str(args.onnx),
        "onnx_bytes": args.onnx.stat().st_size,
        "audio_path": str(args.audio),
        "audio_seconds": audio_seconds,
        "feature_shape": list(features.shape),
        "output_shape": list(logits.shape),
        "threads": args.threads,
        "repeats": args.repeats,
        "feature_seconds": feature_seconds,
        "session_load_seconds": load_seconds,
        "inference_seconds": wall_times,
        "median_inference_seconds": median_seconds,
        "median_rtf": median_seconds / audio_seconds,
        "median_rtfx": audio_seconds / median_seconds,
        "mean_cpu_cores_used": sum(cpu_times) / sum(wall_times),
        "max_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "token_sha256_u32le": hashlib.sha256(canonical_token_bytes).hexdigest(),
        "transcript": transcript,
        "toolchain": {
            "onnxruntime": onnxruntime.__version__,
        },
    }
    if args.report:
        _write_json(args.report, result)
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = benchmark(args)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
