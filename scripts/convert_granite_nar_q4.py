#!/usr/bin/env python3
"""Convert IBM Granite Speech 4.1 **NAR** ONNX graphs to a high-quality **q4** bundle.

Background
----------
The app already ships Granite Speech 4.1 NAR at INT8 (``smcleod/...-nar-onnx``,
``int8/`` tier) on the hand-written ``onnxruntime-node`` NAR runtime
(``loadGranite41NarRuntime`` in ``webgpu_asr_runner.mjs``). There is no public q4
ONNX build for NAR, so we produce one ourselves.

What this does
--------------
Re-quantises the **FP32** NAR graphs to 4-bit weight-only
(``com.microsoft.MatMulNBits``) via ``onnxruntime``'s ``MatMulNBitsQuantizer``:

* ``editor.onnx`` — the 2B "editor" LLM. Quantised to 4-bit; the main win.
* ``encoder.onnx`` — conformer + CTC heads. **Kept at INT8 by default.** The
  encoder is Conv-heavy and the MatMul-nbits quantiser cannot touch Conv, so a
  q4 encoder leaves Convs at FP32 and ends up *larger* (~1.1 GB) and noisier
  (audio_embeds max-abs-err ~1.24 vs INT8 ~0.78) than the proven INT8 encoder
  (~0.63 GB). ``--encoder-precision q4`` stays available for experiments.
* ``embed_tokens.onnx`` — a Gather (no MatMul), nothing to quantise. Reused
  from the ``fp16w`` tier (FP32-grade) by default.

The ``q4/`` folder therefore holds a mixed-precision set (INT8 encoder +
fp16w embed + q4 editor), labelled "q4" after its dominant decoder
quantisation — the same convention the app's Transformers.js q4 packages use.

Key decisions (see docs/granite-speech-4.1-onnx-variants.md):

* **Source = FP32, not FP16w.** The ``fp16w`` graphs hide weights behind
  ``Cast(FP16->FP32)`` nodes, so the MatMul-nbits quantiser (which needs a
  constant initialiser feeding MatMul) cannot see them. ``fp32`` feeds weights
  directly.
* **MatMulNBits (``com.microsoft``) is fine here.** The upstream repo stays
  ``ai.onnx``-only for the Rust ``ort`` crate; that contract does not apply to
  this app, whose ``onnxruntime-node`` runtime runs MatMulNBits on CPU
  (guaranteed) and DirectML (GPU, per-node CPU fallback) — the same op the
  Cohere / Granite-4.0 q4 packages already use here.
* **Method = HQQ by default for CPU-quality experiments.** RTN
  (``--method rtn``) is the fast, torch-free baseline and the practical publish
  target from the 2026-06 investigation: it is smaller/faster and was the only
  q4 method that stayed correct on DirectML. HQQ was slightly cleaner on CPU but
  larger/slower and broken by DirectML's asymmetric ``MatMulNBits`` path.

Output layout (mirrors the ``int8/`` tier so the app required-file set maps 1:1):

    <out>/granite_export_metadata.json
    <out>/preprocessor_config.json
    <out>/tokenizer.json
    <out>/tokenizer_config.json
    <out>/special_tokens_map.json
    <out>/q4/encoder.onnx       (+ encoder.onnx_data)
    <out>/q4/embed_tokens.onnx  (+ embed_tokens.onnx_data)
    <out>/q4/editor.onnx        (+ editor.onnx_data)

Dependencies are tooling-only (NOT app runtime deps). Run in an isolated env:

    # RTN baseline (no torch needed):
    uv run --with onnx --with onnx-ir \
        python scripts/convert_granite_nar_q4.py --method rtn --out-dir <out>

    # HQQ, highest quality (needs torch):
    uv run --with onnx --with onnx-ir --with torch \
        python scripts/convert_granite_nar_q4.py --method hqq --out-dir <out>

    # Fast smoke run (encoder only) with golden-fixture parity check:
    uv run --with onnx --with onnx-ir \
        python scripts/convert_granite_nar_q4.py --method rtn --graphs encoder \
        --validate --out-dir <out>
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from pathlib import Path

SOURCE_REPO = "smcleod/ibm-granite-speech-4.1-2b-nar-onnx"

# Shared bundle-root files (tokeniser / processor / metadata), copied verbatim.
ROOT_FILES = (
    "granite_export_metadata.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)

# Golden fixtures shipped by the upstream repo, used by --validate.
FIXTURE_FILES = (
    "test_fixtures/expected_input_features.npy",
    "test_fixtures/expected_attention_mask.npy",
    "test_fixtures/expected_audio_embeds.npy",
)

# The editor's final vocab projection. Optionally excluded from 4-bit
# quantisation to protect logit/argmax quality at the cost of a larger sidecar.
EDITOR_LMHEAD_NODE = "/lm_head/MatMul"

GRAPHS_DEFAULT = "encoder,embed,editor"


def download_sources(
    repo: str,
    cache_dir: str | None,
    encoder_precision: str,
    embed_precision: str,
    graphs: set[str],
    want_fixtures: bool,
) -> Path:
    """Download only the files the selected graphs actually need."""
    from huggingface_hub import snapshot_download

    patterns = list(ROOT_FILES)
    if "editor" in graphs:
        patterns += ["fp32/editor.onnx", "fp32/editor.onnx_data"]
    if "embed" in graphs:
        patterns += [f"{embed_precision}/embed_tokens.onnx", f"{embed_precision}/embed_tokens.onnx_data"]
    if "encoder" in graphs:
        src_enc = "fp32" if encoder_precision == "q4" else encoder_precision
        patterns += [f"{src_enc}/encoder.onnx", f"{src_enc}/encoder.onnx_data"]
    if want_fixtures:
        patterns += list(FIXTURE_FILES)
    print(f"Downloading source graphs from {repo} (this can be several GB)...")
    local = snapshot_download(repo, allow_patterns=patterns, cache_dir=cache_dir)
    return Path(local)


def make_algo_config(method: str, bits: int, block: int):
    import onnxruntime.quantization.matmul_nbits_quantizer as mq
    from onnxruntime.quantization.quant_utils import QuantFormat

    kw = dict(
        block_size=block,
        bits=bits,
        quant_format=QuantFormat.QOperator,  # -> com.microsoft.MatMulNBits
        op_types_to_quantize=("MatMul",),
    )
    if method == "hqq":
        return mq.HQQWeightOnlyQuantConfig(**kw)
    if method == "rtn":
        return mq.DefaultWeightOnlyQuantConfig(**kw)
    raise SystemExit(f"unknown --method {method!r} (expected hqq or rtn)")


def consolidate_single_sidecar(src_onnx: Path, dst_onnx: Path) -> None:
    """Re-save a graph with a single ``<stem>.onnx_data`` sidecar.

    Mirrors the upstream ``quantise.py`` convention so the layout matches the
    other precision tiers and the app's required-file set.
    """
    import onnx

    proto = onnx.load(str(src_onnx), load_external_data=True)
    if proto.ir_version < 10:
        proto.ir_version = 10
    for tensor in proto.graph.initializer:
        tensor.ClearField("data_location")
        tensor.ClearField("external_data")

    sidecar = dst_onnx.name + "_data"
    dst_onnx.parent.mkdir(parents=True, exist_ok=True)
    if (dst_onnx.parent / sidecar).exists():
        (dst_onnx.parent / sidecar).unlink()
    if dst_onnx.exists():
        dst_onnx.unlink()
    onnx.save_model(
        proto,
        str(dst_onnx),
        save_as_external_data=True,
        all_tensors_to_one_file=True,
        location=sidecar,
        size_threshold=1024,
        convert_attribute=False,
    )


def _sidecar_sizes(onnx_path: Path) -> tuple[float, float]:
    graph_mb = onnx_path.stat().st_size / 1e6
    data = onnx_path.with_name(onnx_path.name + "_data")
    data_gb = data.stat().st_size / 1e9 if data.exists() else 0.0
    return graph_mb, data_gb


def quantize_graph(
    src_onnx: Path,
    dst_onnx: Path,
    method: str,
    bits: int,
    block: int,
    accuracy_level: int,
    exclude_nodes: list[str],
) -> None:
    import onnxruntime.quantization.matmul_nbits_quantizer as mq

    cfg = make_algo_config(method, bits, block)
    print(
        f"  quantising {src_onnx.parent.name}/{src_onnx.name} -> {dst_onnx} "
        f"[{method} {bits}b block{block} acc{accuracy_level}] "
        f"exclude={exclude_nodes or '(none)'}"
    )
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="nar_q4_") as scratch_dir:
        scratch = Path(scratch_dir)
        # Stage a private copy of the graph + sidecar. onnx shape-inference
        # inside the quantiser refuses multi-hard-linked files; staging avoids
        # that and keeps the source cache pristine.
        staged = scratch / src_onnx.name
        shutil.copyfile(src_onnx, staged)
        sidecar = src_onnx.with_name(src_onnx.name + "_data")
        if sidecar.exists():
            shutil.copyfile(sidecar, scratch / sidecar.name)

        quantizer = mq.MatMulNBitsQuantizer(
            model=str(staged),
            bits=bits,
            block_size=block,
            accuracy_level=accuracy_level,
            nodes_to_exclude=list(exclude_nodes),
            algo_config=cfg,
        )
        quantizer.process()
        tmp_out = scratch / ("q_" + dst_onnx.name)
        quantizer.model.save_model_to_file(str(tmp_out), use_external_data_format=True)
        consolidate_single_sidecar(tmp_out, dst_onnx)

    graph_mb, data_gb = _sidecar_sizes(dst_onnx)
    print(
        f"    done in {time.time() - t0:.1f}s  "
        f"graph={graph_mb:.2f} MB  sidecar={data_gb:.2f} GB"
    )


def copy_graph(src_onnx: Path, dst_onnx: Path) -> None:
    print(f"  copying {src_onnx.parent.name}/{src_onnx.name} -> {dst_onnx} (no quant)")
    dst_onnx.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_onnx, dst_onnx)
    sidecar = src_onnx.with_name(src_onnx.name + "_data")
    if sidecar.exists():
        shutil.copyfile(sidecar, dst_onnx.with_name(dst_onnx.name + "_data"))
    graph_mb, data_gb = _sidecar_sizes(dst_onnx)
    print(f"    graph={graph_mb:.2f} MB  sidecar={data_gb:.2f} GB")


def validate_encoder(bundle: Path, source: Path) -> None:
    """Smoke-check the q4 encoder against the upstream FP32 golden fixture."""
    import numpy as np
    import onnxruntime as ort

    missing = [f for f in FIXTURE_FILES if not (source / f).exists()]
    if missing:
        print(f"  [validate] skipped — missing fixtures {missing}")
        return
    feats = np.load(source / "test_fixtures/expected_input_features.npy")
    mask = np.load(source / "test_fixtures/expected_attention_mask.npy")
    ref = np.load(source / "test_fixtures/expected_audio_embeds.npy")
    if feats.ndim == 2:
        feats = feats[None]
    if mask.ndim == 1:
        mask = mask[None]

    sess = ort.InferenceSession(
        str(bundle / "q4/encoder.onnx"), providers=["CPUExecutionProvider"]
    )
    in_names = {i.name for i in sess.get_inputs()}
    feed = {"input_features": feats.astype(np.float32), "attention_mask": mask.astype(np.int64)}
    feed = {k: v for k, v in feed.items() if k in in_names}
    out_names = [o.name for o in sess.get_outputs()]
    outs = sess.run(None, feed)
    audio_embeds = outs[out_names.index("audio_embeds")]
    n = min(audio_embeds.shape[1], ref.shape[1])
    err = np.abs(audio_embeds[:, :n].astype(np.float32) - ref[:, :n].astype(np.float32))
    print(
        f"  [validate] q4 encoder audio_embeds vs FP32 ref: "
        f"max_abs_err={err.max():.4f} mean_abs_err={err.mean():.5f} "
        f"(upstream INT8 ref: max~0.78, mean~0.067)"
    )


def bundle_total_gb(out: Path) -> float:
    return sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 1e9


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", required=True, type=Path, help="Destination bundle directory.")
    ap.add_argument("--method", choices=("hqq", "rtn"), default="hqq", help="4-bit method (default: hqq).")
    ap.add_argument("--encoder-precision", choices=("int8", "q4", "fp16w", "fp32"), default="int8",
                    help="Encoder tier. Default int8 (proven, smaller and more accurate than a q4 "
                         "encoder, whose FP32 Convs bloat it). q4 quantises its MatMuls; fp16w/fp32 "
                         "copy that tier.")
    ap.add_argument("--embed-precision", choices=("fp16w", "int8", "fp32"), default="fp16w",
                    help="embed_tokens tier to reuse (no MatMul to quantise). Default fp16w (FP32-grade).")
    ap.add_argument("--bits", type=int, default=4)
    ap.add_argument("--block-size", type=int, default=32)
    ap.add_argument("--accuracy-level", type=int, default=0,
                    help="MatMulNBits compute precision (0=unset/highest, 4=int8/fastest). Default 0.")
    ap.add_argument("--editor-exclude-lmhead", action="store_true",
                    help="Keep the editor's /lm_head/MatMul out of 4-bit (protects logits, larger sidecar).")
    ap.add_argument("--graphs", default=GRAPHS_DEFAULT,
                    help=f"Comma list of graphs to build (default: {GRAPHS_DEFAULT}).")
    ap.add_argument("--source-repo", default=SOURCE_REPO)
    ap.add_argument("--source-dir", type=Path, default=None,
                    help="Use an already-downloaded snapshot dir instead of downloading.")
    ap.add_argument("--cache-dir", default=None, help="HF cache dir for the download.")
    ap.add_argument("--validate", action="store_true",
                    help="Smoke-check the q4 encoder against the upstream golden fixture.")
    args = ap.parse_args(argv)

    graphs = {g.strip() for g in args.graphs.split(",") if g.strip()}
    out: Path = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    qdir = out / "q4"
    qdir.mkdir(parents=True, exist_ok=True)

    if args.source_dir:
        source = args.source_dir
        print(f"Using pre-downloaded source: {source}")
    else:
        source = download_sources(
            args.source_repo, args.cache_dir, args.encoder_precision,
            args.embed_precision, graphs,
            want_fixtures=args.validate and "encoder" in graphs,
        )
    print(f"Source snapshot: {source}\n")

    print("Copying bundle-root metadata...")
    for fname in ROOT_FILES:
        sp = source / fname
        if sp.exists():
            shutil.copyfile(sp, out / fname)
        else:
            print(f"  WARNING: root file missing in source: {fname}")

    exclude = [EDITOR_LMHEAD_NODE] if args.editor_exclude_lmhead else []

    if "encoder" in graphs:
        print("Encoder:")
        if args.encoder_precision == "q4":
            quantize_graph(source / "fp32/encoder.onnx", qdir / "encoder.onnx",
                           args.method, args.bits, args.block_size, args.accuracy_level, [])
        else:
            copy_graph(source / f"{args.encoder_precision}/encoder.onnx", qdir / "encoder.onnx")

    if "embed" in graphs:
        print(f"Embed tokens (reused from {args.embed_precision}, no quant):")
        copy_graph(source / f"{args.embed_precision}/embed_tokens.onnx", qdir / "embed_tokens.onnx")

    if "editor" in graphs:
        print("Editor:")
        quantize_graph(source / "fp32/editor.onnx", qdir / "editor.onnx",
                       args.method, args.bits, args.block_size, args.accuracy_level, exclude)

    if args.validate and "encoder" in graphs:
        print("Validation:")
        validate_encoder(out, source)

    print(f"\nDone. Bundle at {out}  (total {bundle_total_gb(out):.2f} GB)")


if __name__ == "__main__":
    main()
