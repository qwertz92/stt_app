#!/usr/bin/env python3
"""Export IBM Granite Speech 5.0 TurboCTC to a dynamic FP32 ONNX graph.

This is intentionally separate from the application's dependency environment.
See ``docs/granite-speech-5-onnx-export.md`` for the pinned toolchain.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
import warnings
from pathlib import Path
from types import MethodType

MODEL_ID = "ibm-granite/granite-speech-5.0-470m-turboctc"
MODEL_REVISION = "18ca3c1de6cd092b5a30c39fb0f04550b38ed1a0"
SAMPLE_FRAMES = 500
MIN_FEATURE_FRAMES = 4
OPSET_VERSION = 17


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
        help="Hugging Face revision recorded in the export report",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/granite-speech5-onnx/model.onnx"),
    )
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="Forbid network access while loading the source checkpoint",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing output only after the new graph passes ONNX checks",
    )
    return parser.parse_args(argv)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _layer_input_lengths(config: object, frames: int) -> list[int]:
    lengths = []
    subsample_layers = set(config.subsample_layers)
    for layer_index in range(config.num_hidden_layers):
        lengths.append(frames)
        if layer_index in subsample_layers:
            frames //= 2
    return lengths


def _export_subsampling_forward(
    self: object,
    hidden_states: object,
    attention_mask: object | None = None,
    position_embeddings: object | None = None,
    **kwargs: object,
) -> object:
    """Replace only the dynamic-unfriendly residual ``unfold`` operation."""
    import torch

    residual = hidden_states
    hidden_states = self.feed_forward1(self.norm_feed_forward1(hidden_states))
    hidden_states = residual + 0.5 * hidden_states

    normalized = self.norm_self_att(hidden_states)
    attention_output, _ = self.self_attn(
        hidden_states=normalized,
        attention_mask=attention_mask,
        position_embeddings=position_embeddings,
        **kwargs,
    )
    hidden_states = hidden_states + attention_output

    convolution_output = self.conv(
        self.norm_conv(hidden_states), attention_mask=attention_mask
    )
    pooled = torch.nn.functional.avg_pool1d(
        hidden_states.transpose(1, 2), kernel_size=2, stride=2
    ).transpose(1, 2)
    hidden_states = pooled + convolution_output[:, : pooled.shape[1]]

    feed_forward_output = self.feed_forward2(self.norm_feed_forward2(hidden_states))
    hidden_states = hidden_states + 0.5 * feed_forward_output
    return self.norm_out(hidden_states)


def _patch_subsampling_layers(model: object) -> None:
    for layer_index in model.config.encoder_config.subsample_layers:
        layer = model.encoder.layers[layer_index]
        layer.forward = MethodType(_export_subsampling_forward, layer)


def _source_kwargs(args: argparse.Namespace) -> dict[str, object]:
    kwargs: dict[str, object] = {"local_files_only": args.local_files_only}
    if not Path(args.model).is_dir():
        kwargs["revision"] = args.revision
    return kwargs


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def export(args: argparse.Namespace) -> dict[str, object]:
    if args.output.exists() and not args.force:
        raise FileExistsError(
            f"Refusing to replace {args.output}; pass --force after preserving it."
        )

    import onnx
    import torch
    import transformers
    from packaging.version import Version
    from torch import nn
    from transformers import AutoModelForCTC

    if Version(torch.__version__.split("+", maxsplit=1)[0]) < Version("2.14"):
        raise RuntimeError(
            "Granite Speech 5 dynamic export requires torch>=2.14; "
            f"found {torch.__version__}."
        )
    if Version(transformers.__version__) < Version("5.16"):
        raise RuntimeError(
            "Granite Speech 5 requires transformers>=5.16; "
            f"found {transformers.__version__}."
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial = args.output.with_name(f".{args.output.stem}.partial{args.output.suffix}")
    partial.unlink(missing_ok=True)

    model = AutoModelForCTC.from_pretrained(
        args.model,
        dtype=torch.float32,
        **_source_kwargs(args),
    ).eval()
    model.set_attn_implementation("eager")

    config = model.config.encoder_config
    traced_lengths = _layer_input_lengths(config, SAMPLE_FRAMES)
    if min(traced_lengths) < 1 or any(
        length % config.context_size == 0 for length in traced_lengths
    ):
        raise RuntimeError(
            "The fixed trace sample must take the padding branch in every layer; "
            f"got layer lengths {traced_lengths}."
        )

    torch.manual_seed(0)
    input_width = config.num_mel_bins * 4
    sample = torch.randn(1, SAMPLE_FRAMES, input_width, dtype=torch.float32)
    with torch.inference_mode():
        reference_logits = model(input_features=sample, return_dict=True).logits.cpu()

    _patch_subsampling_layers(model)
    with torch.inference_mode():
        patched_logits = model(input_features=sample, return_dict=True).logits.cpu()
    torch.testing.assert_close(patched_logits, reference_logits, rtol=1e-6, atol=1e-6)
    patch_max_abs = float((patched_logits - reference_logits).abs().max())

    class LogitsOnly(nn.Module):
        def __init__(self, core: object) -> None:
            super().__init__()
            self.core = core

        def forward(self, input_features: object) -> object:
            return self.core(input_features=input_features, return_dict=True).logits

    wrapper = LogitsOnly(model).eval()
    with warnings.catch_warnings():
        # The torch.export path fails on Granite's symbolic block reshape in
        # torch 2.14. The legacy path is retained until that upstream defect is
        # fixed and is guarded by multi-length PyTorch/ORT parity validation.
        warnings.filterwarnings(
            "ignore",
            message="You are using the legacy TorchScript-based ONNX export.*",
            category=DeprecationWarning,
        )
        # SAMPLE_FRAMES deliberately takes the padded branch in every layer.
        # A zero-width Pad is the unpadded branch, and the validator exercises
        # lengths on both sides of every relevant block boundary.
        warnings.filterwarnings(
            "ignore",
            message="Converting a tensor to a Python boolean might cause.*",
            category=torch.jit.TracerWarning,
        )
        torch.onnx.export(
            wrapper,
            (sample,),
            partial,
            input_names=["input_features"],
            output_names=["logits"],
            dynamic_axes={
                "input_features": {0: "batch_size", 1: "feature_frames"},
                "logits": {0: "batch_size", 1: "logit_frames"},
            },
            opset_version=OPSET_VERSION,
            dynamo=False,
            external_data=False,
        )

    del wrapper, model, sample, reference_logits, patched_logits
    gc.collect()
    onnx.checker.check_model(str(partial), full_check=True)

    report = {
        "source_model": MODEL_ID,
        "source_revision": args.revision,
        "loaded_from": args.model,
        "source_weights_sha256": (
            _sha256(Path(args.model) / "model.safetensors")
            if Path(args.model).is_dir()
            else None
        ),
        "onnx_sha256": _sha256(partial),
        "onnx_bytes": partial.stat().st_size,
        "opset": OPSET_VERSION,
        "input": ["batch_size", "feature_frames", input_width],
        "minimum_feature_frames": MIN_FEATURE_FRAMES,
        "output": ["batch_size", "logit_frames", int(config.vocab_size)],
        "sample_frames": SAMPLE_FRAMES,
        "subsampling_patch_max_abs": patch_max_abs,
        "toolchain": {
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "onnx": onnx.__version__,
        },
    }
    partial.replace(args.output)
    _write_json(args.output.with_suffix(".export.json"), report)
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = export(args)
    except (FileExistsError, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
