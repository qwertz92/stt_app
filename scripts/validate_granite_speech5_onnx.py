#!/usr/bin/env python3
"""Validate a Granite Speech 5 ONNX graph against independent PyTorch output."""

from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from export_granite_speech5_onnx import (
    MIN_FEATURE_FRAMES,
    MODEL_ID,
    MODEL_REVISION,
    _sha256,
    _write_json,
)

# ONNX Runtime 1.29 enables POSIX telemetry in official wheels. Disable it before
# the native runtime initializes so validation creates neither events nor a device ID.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

DATASET_ID = "hf-internal-testing/librispeech_asr_dummy"
DATASET_REVISION = "5be91486e11a2d616f4ec5db8d3fd248585ac07a"
REAL_CLIP_COUNT = 20
BOUNDARY_FRAMES = (MIN_FEATURE_FRAMES, 127, 128, 129, 255, 256, 257, 511, 512, 513)
MAX_ABS_LIMIT = 1e-3
MEAN_ABS_LIMIT = 1e-5
MIN_ARGMAX_AGREEMENT = 1.0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=MODEL_ID,
        help="Hugging Face model ID or a local snapshot directory",
    )
    parser.add_argument(
        "--revision",
        default=MODEL_REVISION,
        help="Hugging Face source revision",
    )
    parser.add_argument(
        "--onnx",
        type=Path,
        default=Path("build/granite-speech5-onnx/model.onnx"),
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("build/granite-speech5-fixtures"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("build/granite-speech5-onnx/validation.json"),
    )
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="Forbid network access while loading the source checkpoint",
    )
    return parser.parse_args(argv)


def _fixture_rows(fixtures: Path) -> list[dict[str, str]]:
    metadata_path = fixtures / "meta.json"
    if metadata_path.exists():
        rows = json.loads(metadata_path.read_text(encoding="utf-8"))
        if len(rows) == REAL_CLIP_COUNT and all(
            (fixtures / row["file"]).is_file() for row in rows
        ):
            return rows

    query = urlencode(
        {
            "dataset": DATASET_ID,
            "revision": DATASET_REVISION,
            "config": "clean",
            "split": "validation",
            "offset": 0,
            "length": REAL_CLIP_COUNT,
        }
    )
    request = Request(
        f"https://datasets-server.huggingface.co/rows?{query}",
        headers={"User-Agent": "stt-app-granite-speech5-validator"},
    )
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    api_rows = payload.get("rows", [])
    if len(api_rows) != REAL_CLIP_COUNT:
        raise RuntimeError(
            f"Expected {REAL_CLIP_COUNT} fixture rows, received {len(api_rows)}."
        )

    fixtures.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, item in enumerate(api_rows):
        row = item["row"]
        audio = row["audio"]
        source = audio["src"] if isinstance(audio, dict) else audio[0]["src"]
        if not source.startswith("https://datasets-server.huggingface.co/"):
            raise RuntimeError(f"Refusing unexpected fixture URL: {source}")
        filename = f"clip{index:02d}.flac"
        destination = fixtures / filename
        temporary = destination.with_suffix(".flac.partial")
        download = Request(
            source,
            headers={"User-Agent": "stt-app-granite-speech5-validator"},
        )
        with (
            urlopen(download, timeout=60) as response,
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        temporary.replace(destination)
        rows.append({"file": filename, "id": row["id"]})
    _write_json(metadata_path, rows)
    return rows


def _source_kwargs(args: argparse.Namespace) -> dict[str, object]:
    kwargs: dict[str, object] = {"local_files_only": args.local_files_only}
    if not Path(args.model).is_dir():
        kwargs["revision"] = args.revision
    return kwargs


def _reference_cases(
    args: argparse.Namespace,
    reference_dir: Path,
) -> tuple[object, list[dict[str, object]]]:
    import numpy as np
    import soundfile
    import torch
    from transformers import AutoModelForCTC, AutoProcessor

    processor = AutoProcessor.from_pretrained(args.model, **_source_kwargs(args))
    model = AutoModelForCTC.from_pretrained(
        args.model,
        dtype=torch.float32,
        **_source_kwargs(args),
    ).eval()
    model.set_attn_implementation("eager")
    cases: list[dict[str, object]] = []

    def save_case(name: str, kind: str, features: object) -> None:
        with torch.inference_mode():
            logits = (
                model(input_features=features, return_dict=True).logits.cpu().numpy()
            )
        feature_path = reference_dir / f"{name}.features.npy"
        logits_path = reference_dir / f"{name}.logits.npy"
        np.save(feature_path, features.cpu().numpy())
        np.save(logits_path, logits)
        cases.append(
            {
                "name": name,
                "kind": kind,
                "features": feature_path,
                "reference_logits": logits_path,
                "reference_transcripts": processor.batch_decode(logits.argmax(axis=-1)),
            }
        )

    for fixture in _fixture_rows(args.fixtures):
        samples, sampling_rate = soundfile.read(
            args.fixtures / fixture["file"], dtype="float32"
        )
        if samples.ndim != 1 or sampling_rate != 16_000:
            raise RuntimeError(
                f"Fixture {fixture['id']} must be mono 16 kHz audio; got "
                f"shape={samples.shape}, rate={sampling_rate}."
            )
        features = processor(
            samples,
            sampling_rate=sampling_rate,
            return_tensors="pt",
        ).input_features
        save_case(str(fixture["id"]), "real_audio", features)

    generator = torch.Generator().manual_seed(0)
    input_width = model.config.encoder_config.num_mel_bins * 4
    for frames in BOUNDARY_FRAMES:
        save_case(
            f"synthetic-{frames}",
            "dynamic_length",
            torch.randn(1, frames, input_width, generator=generator),
        )
    save_case(
        "synthetic-batch-2x129",
        "dynamic_batch",
        torch.randn(2, 129, input_width, generator=generator),
    )

    return processor, cases


def validate(args: argparse.Namespace) -> dict[str, object]:
    import numpy as np
    import onnx
    import onnxruntime
    import torch
    import transformers

    if not args.onnx.is_file():
        raise FileNotFoundError(f"ONNX graph not found: {args.onnx}")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    onnx.checker.check_model(str(args.onnx), full_check=True)

    with tempfile.TemporaryDirectory(
        prefix=".granite-speech5-references-",
        dir=args.report.parent,
    ) as temporary:
        processor, cases = _reference_cases(args, Path(temporary))
        gc.collect()
        session = onnxruntime.InferenceSession(
            str(args.onnx), providers=["CPUExecutionProvider"]
        )
        input_metadata = session.get_inputs()
        output_metadata = session.get_outputs()
        if len(input_metadata) != 1 or len(output_metadata) != 1:
            raise RuntimeError(
                "Expected exactly one ONNX input and one ONNX output; got "
                f"{len(input_metadata)} and {len(output_metadata)}."
            )
        if (
            input_metadata[0].name != "input_features"
            or output_metadata[0].name != "logits"
        ):
            raise RuntimeError(
                "Expected ONNX input_features -> logits, got "
                f"{input_metadata[0].name} -> {output_metadata[0].name}."
            )

        case_reports = []
        total_abs = 0.0
        total_values = 0
        max_abs = 0.0
        min_argmax = 1.0
        exact_transcripts = 0
        for case in cases:
            features = np.load(case["features"])
            reference = np.load(case["reference_logits"])
            actual = session.run(["logits"], {"input_features": features})[0]
            difference = np.abs(actual - reference)
            case_max_abs = float(difference.max())
            case_mean_abs = float(difference.mean(dtype=np.float64))
            reference_ids = reference.argmax(axis=-1)
            actual_ids = actual.argmax(axis=-1)
            argmax_agreement = float((actual_ids == reference_ids).mean())
            actual_transcripts = processor.batch_decode(actual_ids)
            transcript_match = actual_transcripts == case["reference_transcripts"]
            expected_shape = [
                features.shape[0],
                features.shape[1] // 4,
                reference.shape[2],
            ]
            shape_match = list(actual.shape) == expected_shape

            max_abs = max(max_abs, case_max_abs)
            min_argmax = min(min_argmax, argmax_agreement)
            total_abs += float(difference.sum(dtype=np.float64))
            total_values += difference.size
            exact_transcripts += transcript_match
            case_reports.append(
                {
                    "name": case["name"],
                    "kind": case["kind"],
                    "input_shape": list(features.shape),
                    "output_shape": list(actual.shape),
                    "shape_match": shape_match,
                    "max_abs": case_max_abs,
                    "mean_abs": case_mean_abs,
                    "argmax_agreement": argmax_agreement,
                    "transcript_match": transcript_match,
                }
            )

        mean_abs = total_abs / total_values
        passed = (
            all(case["shape_match"] for case in case_reports)
            and max_abs <= MAX_ABS_LIMIT
            and mean_abs <= MEAN_ABS_LIMIT
            and min_argmax >= MIN_ARGMAX_AGREEMENT
            and exact_transcripts == len(case_reports)
        )
        report = {
            "passed": passed,
            "source_model": MODEL_ID,
            "source_revision": args.revision,
            "loaded_from": args.model,
            "dataset": DATASET_ID,
            "dataset_revision": DATASET_REVISION,
            "onnx_path": str(args.onnx),
            "onnx_sha256": _sha256(args.onnx),
            "onnx_bytes": args.onnx.stat().st_size,
            "graph": {
                "minimum_feature_frames": MIN_FEATURE_FRAMES,
                "input": {
                    "name": input_metadata[0].name,
                    "type": input_metadata[0].type,
                    "shape": input_metadata[0].shape,
                },
                "output": {
                    "name": output_metadata[0].name,
                    "type": output_metadata[0].type,
                    "shape": output_metadata[0].shape,
                },
            },
            "limits": {
                "max_abs": MAX_ABS_LIMIT,
                "mean_abs": MEAN_ABS_LIMIT,
                "min_argmax_agreement": MIN_ARGMAX_AGREEMENT,
                "exact_transcripts": True,
            },
            "summary": {
                "cases": len(case_reports),
                "real_audio_cases": sum(
                    case["kind"] == "real_audio" for case in case_reports
                ),
                "exact_transcripts": exact_transcripts,
                "max_abs": max_abs,
                "mean_abs": mean_abs,
                "min_argmax_agreement": min_argmax,
            },
            "cases": case_reports,
            "toolchain": {
                "python": sys.version.split()[0],
                "torch": torch.__version__,
                "transformers": transformers.__version__,
                "onnx": onnx.__version__,
                "onnxruntime": onnxruntime.__version__,
            },
        }
        _write_json(args.report, report)
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = validate(args)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report["summary"], indent=2))
    print(f"Validation {'passed' if report['passed'] else 'failed'}: {args.report}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
