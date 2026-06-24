# Granite Speech 4.1 NAR: q4 self-conversion, the pipeline fix, and the GPU question

Status: **living research record** (last updated 2026-06-24). This is the durable
write-up of the NAR q4 investigation so the work — and the dead-ends — do not have
to be repeated. It is also the source text for the eventual Hugging Face model card.

Related: [granite-speech-4.1-onnx-variants.md](granite-speech-4.1-onnx-variants.md),
[local-onnx-q4-conversion.md](local-onnx-q4-conversion.md),
[local-onnx-runtime.md](local-onnx-runtime.md).

---

## TL;DR (read this first)

1. **NAR was completely broken in this app at every precision** (including the
   shipped INT8). It emitted token garbage. **Root cause found and fixed** — a
   host-side CTC-decode bug in `webgpu_asr_runner.mjs` (wrong blank id + missing
   vocab offset). NAR now transcribes correctly (English verbatim-correct,
   German good).
2. **INT8 is the preferred NAR precision.** On a Ryzen 5 7600X it is the
   **fastest** (RTF ~0.53) and top quality.
3. **q4 (= INT4) brings no end-to-end benefit on current hardware.** It is
   **slower on CPU** than INT8, only **~9–16 % smaller** (not "half"), and **GPU
   does not help**: the q4 **editor** does run on DirectML (2–3× faster there at
   large sizes), but the **conformer encoder is ~90 % of NAR's runtime and is
   CPU-locked** (DirectML can't run its 5-D attention MatMul; WebGPU has the
   Einsum bug). Accelerating only the editor makes NAR *slower* end-to-end
   (GPU↔CPU transfer of the 100k-vocab logits). q4's win is a GPU/bandwidth
   optimisation that NAR's CPU-bound encoder nullifies. **q4-HQQ is also broken on
   DirectML** (asymmetric quant) — use RTN on GPU.
4. We keep the q4 build + converter and publish the **RTN** q4 build to Hugging
   Face (under `qwertz92`) as an honest community artifact, **with a prominent
   warning at the top that INT8 is preferred**. HQQ stays documented as a local
   research artifact because it is larger/slower and broken on DirectML.
5. **GPU investigation: done (2026-06-21).** The q4 editor runs on DirectML, but
   the encoder (90 % of the time) cannot, so the GPU gives **no end-to-end
   speed-up** on the A750 — it is slightly slower. GPU-accelerating NAR would
   require **re-exporting the encoder** to GPU-friendly ops — a separate, large
   effort, not yet attempted.

> If you only need a usable local NAR: pick **INT8**. q4 exists for research /
> future GPU runtimes, not for a speed-up today.

---

## What NAR is, and why we wanted q4

`ibm-granite/granite-speech-4.1-2b-nar` is the **non-autoregressive** Granite
Speech 4.1 ASR model: a conformer **encoder** with a BPE **CTC** head produces a
draft transcript in a single pass, an **editor** LLM (2B) refines it in parallel.
No token-by-token loop → appreciably faster than the autoregressive (AR) variants
in principle. We wanted a 4-bit (q4/INT4) build for smaller size + speed.

There is **no public q4 ONNX build for NAR** (only INT8/fp16 ONNX from `smcleod`,
GGUF for CrispASR, MLX for Apple). So we self-convert.

---

## Benchmark results (Ryzen 5 7600X, Arc A750, CPU EP unless noted)

Two 16 kHz mono clips: an English LibriSpeech fixture (16.9 s, known reference
transcript) and a German Windows-SAPI "Hedda" TTS clip (13.3 s).

| Variant | Editor graph | Bundle on disk | CPU RTF | DML-hybrid RTF† | English quality | German quality |
| --- | --- | --- | --- | --- | --- | --- |
| **INT8** (smcleod) | 1.63 GB | **2.32 GiB** | **0.53–0.60** | 0.64–0.66 | verbatim-correct | good |
| q4-RTN (ours) | 1.05 GB | 1.96 GiB | 0.62 | 0.60–0.69 | verbatim-correct | good |
| q4-HQQ (ours, local only) | 1.22 GB | 2.12 GiB | 0.70 | **broken** | verbatim-correct (CPU) | good, cleanest (CPU) |

† "DML-hybrid" = encoder/embed on CPU, editor on DirectML (the encoder cannot run
on the GPU — see GPU section). It is **not faster**: the CPU-bound encoder
dominates (~90 % of the time) and the GPU editor adds GPU↔CPU transfer overhead.
**q4-HQQ produces garbage on DirectML** (asymmetric quant); only RTN is GPU-safe.

Notes:
- **CPU RTF lower = faster. INT8 on CPU is the fastest config overall.** Quality
  was comparable across all three on these clips (the editor's contribution is
  marginal — see below — so RTN vs HQQ barely moved the transcript; HQQ's German
  was slightly cleaner on CPU).
- "GiB on disk" is binary GiB; the converter prints decimal GB (2.28 GB ≈ 2.12 GiB).

---

## The pipeline bug (the real finding)

NAR produced token-soup like `"after stylesheet obligation has standard memoria
java …"` at **every** precision — INT8 (the shipped model) included. That ruled
out quantisation and pointed at the host-side decode in
`webgpu_asr_runner.mjs` (`loadGranite41NarRuntime`).

Replicating the upstream reference algorithm (`modeling_granite_speech_nar.py`,
`GraniteSpeechNarForASR`) in Python against smcleod's INT8 graphs + the golden
fixture localised it. Two facts the app got wrong in the **CTC draft decode**:

1. **Wrong CTC blank id.** The encoder's `bpe_logits_dense` has **100353**
   classes (= vocab 100352 **+ 1**). The CTC **blank is the prepended class at
   index 0** (empirically it is 167/211 frames on the test clip). The app stripped
   `100257` (the LLM eos) instead, so blanks were never removed.
2. **Missing vocab offset.** A non-blank CTC class `c` maps to **LLM token
   `c − 1`** (the blank is prepended, shifting the vocab up by one). The app used
   the raw class id, so every token was off-by-one.
3. (Secondary) the app also did a **decode-to-text → re-encode round-trip** on the
   draft, which the reference does not. Re-tokenisation changes the id
   sequence/length and corrupts the editor's trained `[blank, t0, blank, t1, …]`
   slot structure.

The fix (in `ctcDraftTokenIds`): per-valid-frame argmax → collapse consecutive
repeats → drop blank **0** → subtract **1** → feed ids **directly** into the
editor slots. The slot-fill token (LLM eos `100257`) and the editor-output decode
were already correct.

With `CTC_BLANK=0, OFFSET=1` the reference replication reproduced the published
transcript exactly: *"after his nap timothy lazily stretched first one grey velvet
foot then another strolled indolently to his plate … upon the clean hearth."*

**Surprising corollary:** the **CTC draft alone is already an excellent
transcript** — the editor only lightly refines it. So the editor (the part we
quantise to q4) contributes marginally to quality; the **INT8 encoder carries the
accuracy.** This is why even q4-RTN transcribes well once the pipeline is fixed.

### Research narrative / dead-ends (so we don't repeat them)
- We first blamed **RTN**, then **HQQ**, then **`accuracy_level`** for the
  garbage. All wrong: the **INT8 baseline was also garbage**, which immediately
  proved it was a pre-existing pipeline bug, not our q4 work. **Lesson: gate the
  baseline through the real pipeline before trusting it** — "the model is shipped"
  is not "the path is verified". NAR had clearly never been validated end-to-end.
- The config naming is a genuine trap: `config.blank_token_id = 100257` is the
  *editor/slot* blank (LLM eos), **not** the *CTC* blank, which is `0` in
  smcleod's export. The upstream `_ctc_collapse_decode` uses `100257` because in
  the PyTorch model the CTC head is laid out differently than smcleod's 100353-class
  ONNX export. Worth flagging for anyone consuming these ONNX graphs.

---

## The q4 conversion (how)

`scripts/convert_granite_nar_q4.py` re-quantises smcleod's **FP32** NAR graphs to
4-bit weight-only (`com.microsoft.MatMulNBits`) via onnxruntime's
`MatMulNBitsQuantizer`. Mixed-precision bundle, chosen empirically:

- **editor → q4** (HQQ default; RTN = fast, torch-free fallback). The only graph
  that benefits from 4-bit.
- **encoder → INT8** (reused from smcleod). A q4 encoder is *larger and noisier*:
  the conformer is Conv-heavy and `MatMulNBits` only touches MatMul, so Convs stay
  FP32 → a q4 encoder is ~1.1 GB (vs INT8 0.63 GB) with worse `audio_embeds`
  (max-abs-err ~1.24 vs ~0.78). So q4-on-the-encoder is counter-productive.
- **embed_tokens → fp16w** (a Gather, nothing to quantise; FP32-grade).

Source must be **FP32**, not fp16w: the fp16w graphs hide weights behind
`Cast(FP16→FP32)` nodes, so the MatMul-nbits quantiser can't see them. `MatMulNBits`
(`com.microsoft`) is fine here — the app's `onnxruntime-node` runs it (CPU; it's
the same op the AR q4 packages use). It is *not* `ai.onnx`-only, so these graphs
are **not** compatible with the Rust `ort`/parakeet-rs contract smcleod targets.

Reproduce:
```bash
# HQQ (highest quality, needs torch):
uv run --with onnx --with onnx-ir --with torch \
    python scripts/convert_granite_nar_q4.py --method hqq --out-dir <dir>
# RTN (fast, torch-free):
uv run --with onnx --with onnx-ir \
    python scripts/convert_granite_nar_q4.py --method rtn --out-dir <dir>
```
Conversion cost: ~16–32 GB RAM peak (FP32 editor is 6.5 GB; HQQ adds torch),
~10–17 GB transient disk, ~40 s (RTN) to ~5 min (HQQ) for the editor.

---

## Why q4 is NOT half the size, slower on CPU, and not clearly better — explained

These are the three things that look wrong at first glance.

### 1. Why a q4 bundle is only ~9–16 % smaller, not ~50 %
"4-bit = half of 8-bit" is true **only for the raw quantised weight tensors**, not
the bundle. A q4 bundle carries a lot of non-4-bit weight:
- We keep **encoder INT8** (0.63 GB) and **embed_tokens fp16w** (0.41 GB) for
  quality — unchanged by editor quantisation.
- Inside the editor, ~22 % of MatMuls are **activation × activation** (attention
  QK^T and weights×V) — no constant weight to quantise, they stay FP32.
- Each 4-bit block stores a **fp16 scale** (and HQQ a zero-point) → per-block
  metadata overhead.
- `lm_head` and layernorms add bulk.

So the editor only shrinks INT8 1.63 GB → q4 1.05–1.22 GB (~25–35 %), and the
encoder+embed don't shrink at all → total 2.32 → 1.96–2.12 GiB.

### 2. Why INT8 is FASTER than q4 on the CPU (the counter-intuitive one)
4-bit means *fewer bytes to move*, which helps when you're **memory-bandwidth
bound** — i.e. on a GPU. On a CPU it is usually **compute bound**, and there:
- **INT8** maps to native integer GEMM with **AVX-512 VNNI** on the 7600X — a
  hardware int8 dot-product instruction. Extremely fast and mature.
- **q4 `MatMulNBits`** must **dequantise** each 4-bit block on the fly (multiply by
  the per-block scale, apply zero-point) **before** the matmul, and its CPU kernel
  is less optimised than native int8 GEMM. The dequant overhead dominates.
- The model fits in RAM, so bandwidth (q4's advantage) is not the bottleneck.

Net: **q4 is a GPU optimisation, not a CPU optimisation.** On CPU the dequant cost
makes it lose to well-vectorised INT8. This is the core reason the original premise
("q4 = faster") doesn't hold here — it's hardware-dependent and needs a working GPU.

### 3. Why HQQ is bigger but better than RTN — and RTN is faster than HQQ
- **RTN** (Round-To-Nearest) just rounds each weight to the nearest 4-bit level —
  fast, data-free, larger error on outlier weights. Tends **symmetric** (no
  zero-point) → smaller and a hair faster to dequant at inference.
- **HQQ** (Half-Quadratic Quantisation) runs an iterative optimisation to pick
  scales/zero-points that minimise reconstruction error (handles outliers) →
  slightly **bigger** (zero-points/metadata) and a hair **slower** to dequant, but
  **more faithful**.
- In our case both transcribed well on CPU because the editor barely matters
  post-fix; HQQ's German was marginally cleaner. But HQQ is **not** a good public
  default here: it is larger, slower, and DirectML's `MatMulNBits` kernel
  mishandles its asymmetric quantisation. The publishable artifact is therefore
  **RTN**: smaller, faster, and the only DML-safe q4 variant observed.

---

## The GPU question: why NAR fails where AR works

On the Arc A750, **DirectML fails for NAR at the encoder's first attention layer**,
identically for INT8 and q4:
```
FusedMatMul '/encoder/layers.0/attn/MatMul/MatMulScaleFusion' -> parameter is incorrect
(and with graph fusions disabled: plain MatMul '/encoder/layers.0/attn/MatMul' -> same)
```
Disabling ORT's MatMul-scale fusion only moved the error to the **plain MatMul** —
so it's not a fusion issue. The conformer block-local attention uses **5-D batched
MatMuls** (`[B, num_blocks, heads, context, dim]`); the **DirectML EP's MatMul
kernel doesn't support that rank/shape**. (WebGPU/ORT-web separately has the
`Einsum` shader-pipeline bug noted in `local-onnx-runtime.md`.)

Four-eyes feasibility check (2026-06-24): shape inspection of the shipped INT8
encoder found **32** high-rank attention `MatMul` nodes and **16** `Einsum` nodes
with the block-local equation `b m h c d, c r d -> b m h c r`. This is a repeated
conformer-layer pattern, not a single bad fused node. A GPU-capable encoder needs
a deliberate re-export or graph rewrite that preserves parity across all layers.

The **editor** (a standard 2B transformer), however, runs fine on DirectML, and at
realistic sizes the q4 editor on DML is ~2–3× faster than INT8 on CPU *in
isolation* — editor-only microbench (ms/iter): INT8/CPU 688 (N=256) / 1535 (N=512);
q4-RTN/DML 324 / 540; q4-HQQ/DML 278 (N=256). But that does **not** help, because
profiling the full pipeline (16.9 s clip, one chunk N=257) shows the encoder is the
bottleneck:

```
INT8/CPU   : encoder 9142 ms   editor 843 ms    (editor ~8 %)
q4-RTN/DML : encoder 9964 ms   editor 1617 ms   (editor ~14 %)
```

The **encoder is ~90 % and CPU-locked**, so a hybrid (encoder CPU + editor DML)
gives no speed-up — it is slightly *slower*, because the single real editor call
must ship the `[1, N, 100352]` logits (~103 MB at N=257) back from the GPU, which
outweighs the compute saving at these sizes. The hybrid was implemented, measured,
and reverted (no benefit, added complexity).

**q4-HQQ is broken on DirectML.** In the full pipeline q4-HQQ/DML emits garbage
(`"ivateazaivateaza…"`) while q4-RTN/DML is correct — DML's `MatMulNBits` kernel
mishandles HQQ's asymmetric (zero-point) quantisation. **On GPU, use RTN, not HQQ.**

**Why AR runs on GPU but NAR doesn't** — it is **not** simply "because NAR is
non-autoregressive". It's the **runtime + ops**:
- The AR models (`granite-4.0-1b-speech`, `granite-speech-4.1-2b`) run through the
  mature **Transformers.js WebGPU pipeline** (`GraniteSpeechForConditionalGeneration`),
  which has proper WebGPU kernels for a standard transformer. Verified working on
  the A750 (~0.13–0.19 RTF).
- NAR has **no Transformers.js class**, so it runs on the **hand-written raw
  `onnxruntime-node` graph path**, whose **conformer encoder** uses ops the GPU EPs
  here can't run (5-D MatMul on DML; Einsum on WebGPU).

So the blocker is the **encoder graph shape**, not autoregression per se. Making
NAR GPU-capable means re-exporting the encoder attention into GPU-friendly shapes
(collapse the 5-D batched MatMul to 3-D/4-D, e.g. fold `num_blocks` into the batch
dim) and/or a WebGPU path that avoids the Einsum bug — a real R&D task (needs the
upstream PyTorch model + a custom export like smcleod's `export_nar_encoder.py`).
Not attempted in this q4 publication pass because it is larger than a packaging
or integration change and needs its own parity/benchmark loop.

---

## Disk / artifacts produced this session (~16.8 GB)

| Size | Path (under `~/.cache/stt_app/` unless noted) | Keep? |
| --- | --- | --- |
| 9.39 GB | HF cache `models--smcleod--…-nar-onnx` (FP32+INT8+fp16w sources) | re-downloadable; prune when done |
| 2.32 GB | `nar-int8-baseline/` | the preferred build |
| 2.12 GB | `nar-q4-hqq/` | q4 quality build; keep local/documented only |
| 1.96 GB | `nar-q4-rtn/` | q4 fast/GPU-safe build; publish artifact |
| 1.03 GB | `nar-q4-smoke/` | **delete** (encoder-only test) |
| ~0 | `gate/` (clips, `gate.py`, `ref_pipeline.py`), `nar-upstream-src/` | tiny, keep for re-verification |

---

## Decisions & plan

- **Ship the NAR pipeline fix** (`webgpu_asr_runner.mjs`) regardless — it makes the
  shipped INT8 NAR actually work.
- **Keep INT8 as the NAR default.** Do **not** switch the app to q4 (slower on CPU,
  no GPU win, marginal size).
- **Keep the q4 build + converter.** Publish the **RTN** artifact to Hugging Face
  under `qwertz92/ibm-granite-speech-4.1-2b-nar-q4-rtn-onnx`, with a
  **prominent up-front warning** (INT8 preferred; q4 = slower on CPU, marginal
  size, no end-to-end GPU win today) so the community benefits and nobody redoes
  this for nothing. Do not publish HQQ as the normal artifact unless a future
  runtime proves its asymmetric `MatMulNBits` path is sound.
- **GPU work (done 2026-06-21):** the q4 editor runs on DirectML but the encoder
  (~90 % of the time) cannot, so GPU gives no end-to-end win and q4-HQQ is
  DML-broken (RTN only). Making NAR GPU-fast needs an **encoder re-export** (5-D
  MatMul + einsum → GPU-friendly ops) — a separate R&D task, not started.
- **Tooling stays out of app runtime deps:** the conversion needs
  `onnx`/`onnx-ir`/`torch` via `uv run --with`; the app must never depend on them.
