#!/usr/bin/env python3
"""Create a validated FP16 or dynamic INT8 Granite Speech 5 ONNX variant."""

from __future__ import annotations

import argparse
import gc
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from export_granite_speech5_onnx import _sha256, _write_json

# Prevent ONNX Runtime 1.29 from creating a telemetry identifier during conversion.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("build/granite-speech5-onnx/model.onnx"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--precision", choices=("fp16", "int8"), required=True)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing output only after the new graph passes ONNX checks",
    )
    return parser.parse_args(argv)


def _tensor_types(model: object) -> dict[str, int]:
    import onnx

    counts = Counter(initializer.data_type for initializer in model.graph.initializer)
    return {
        onnx.TensorProto.DataType.Name(data_type): count
        for data_type, count in sorted(counts.items())
    }


def _topologically_sort_graph(graph: object) -> None:
    available = {value.name for value in graph.input}
    available.update(initializer.name for initializer in graph.initializer)
    pending = list(graph.node)
    ordered = []
    while pending:
        ready = [
            node
            for node in pending
            if all(not name or name in available for name in node.input)
        ]
        if not ready:
            raise RuntimeError(
                "FP16 conversion produced a graph with unresolved inputs."
            )
        for node in ready:
            available.update(node.output)
            pending.remove(node)
            ordered.append(node)
    del graph.node[:]
    graph.node.extend(ordered)


def _convert_fp16(source: Path, destination: Path) -> None:
    import onnx
    from onnxruntime.transformers.float16 import convert_float_to_float16

    model = onnx.load(source)
    converted = convert_float_to_float16(model, keep_io_types=True)
    # The ORT converter appends its input cast after its consumers for this graph.
    _topologically_sort_graph(converted.graph)
    onnx.save_model(converted, destination)
    del model, converted
    gc.collect()


def _convert_int8(source: Path, destination: Path) -> None:
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from onnxruntime.quantization.shape_inference import quant_pre_process

    with tempfile.TemporaryDirectory(
        prefix=".granite-speech5-int8-", dir=destination.parent
    ) as temporary:
        preprocessed = Path(temporary) / "preprocessed.onnx"
        previous_tempdir = tempfile.tempdir
        tempfile.tempdir = temporary
        try:
            quant_pre_process(
                source,
                preprocessed,
                skip_optimization=True,
                skip_symbolic_shape=True,
            )
            quantize_dynamic(
                preprocessed,
                destination,
                op_types_to_quantize=["MatMul", "Gemm"],
                per_channel=True,
                reduce_range=False,
                weight_type=QuantType.QInt8,
                use_external_data_format=False,
            )
        finally:
            tempfile.tempdir = previous_tempdir


def convert(args: argparse.Namespace) -> dict[str, object]:
    import onnx
    import onnxruntime

    if not args.input.is_file():
        raise FileNotFoundError(f"Source ONNX graph not found: {args.input}")
    if args.input.resolve() == args.output.resolve():
        raise ValueError("Input and output paths must differ.")
    if args.output.exists() and not args.force:
        raise FileExistsError(
            f"Refusing to replace {args.output}; pass --force after preserving it."
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial = args.output.with_name(f".{args.output.stem}.partial{args.output.suffix}")
    partial.unlink(missing_ok=True)

    if args.precision == "fp16":
        _convert_fp16(args.input, partial)
    else:
        _convert_int8(args.input, partial)

    try:
        onnx.checker.check_model(str(partial), full_check=True)
    except onnx.checker.ValidationError as error:
        raise RuntimeError(
            f"Converted ONNX graph failed validation: {error}"
        ) from error
    graph = onnx.load(partial, load_external_data=False)
    report = {
        "precision": args.precision,
        "source_path": str(args.input),
        "source_sha256": _sha256(args.input),
        "source_bytes": args.input.stat().st_size,
        "output_path": str(args.output),
        "output_sha256": _sha256(partial),
        "output_bytes": partial.stat().st_size,
        "size_ratio": partial.stat().st_size / args.input.stat().st_size,
        "initializer_types": _tensor_types(graph),
        "operators": dict(
            sorted(Counter(node.op_type for node in graph.graph.node).items())
        ),
        "toolchain": {
            "onnx": onnx.__version__,
            "onnxruntime": onnxruntime.__version__,
        },
    }
    del graph
    gc.collect()

    partial.replace(args.output)
    _write_json(args.output.with_suffix(".conversion.json"), report)
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = convert(args)
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1
    print(json.dumps(report, indent=2))
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
