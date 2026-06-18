# Local ONNX Models: How q4 Conversion Works

This page explains, in plain terms, how the local speech models in this app are
turned into the compact files they ship as, what "q4" means, and why several
models are only available as 4-bit versions. It is background reading for anyone
curious about the local ONNX models; it is not required to use the app.

For how these models run (CPU vs. GPU, memory, device selection) see
[local-onnx-runtime.md](local-onnx-runtime.md). For which model to pick for
day-to-day use see [models.md](models.md).

## Management summary

- A neural-network model is first **exported** from its training format into
  **ONNX** (a portable model-graph format), then **quantized** (its numbers are
  stored with fewer bits) to make it smaller and faster.
- **q4** means the model's large weight matrices are stored using only **4 bits**
  per value (plus a small scale factor per block of values). This is roughly 8×
  smaller than the original 32-bit form.
- A q4 package for the current 1B/2B speech models is still around 1.5–2 GB on
  disk because not every part is 4-bit, and the package includes several graph
  files plus the tokenizer.
- The conversion is **deterministic** for the default method: converting the same
  source model with the same settings produces an equivalent model every time.
  Doing it again locally yields the same quality — it does not improve anything —
  so the app reuses existing public q4 packages instead of re-converting.
- Some models are published **only** as 4-bit because that is the sweet spot of
  size vs. quality for running locally on a normal GPU; larger formats exist but
  are mostly useful for conversion, not everyday use.

## Glossary

- **ASR** — Automatic Speech Recognition (turning audio into text).
- **LLM** — Large Language Model (the text-generating part of a speech model).
- **ONNX** — Open Neural Network Exchange, a portable file format describing a
  model's computation graph and weights. It is not GPU-only; a separate runtime
  decides where it runs (CPU, GPU, etc.).
- **Weight** — a learned number inside the model. Modern speech models have
  billions of them.
- **Quantization** — storing weights (and sometimes intermediate values) with
  fewer bits to shrink the model and speed it up, accepting a small accuracy loss.
- **fp32 / fp16** — 32-bit / 16-bit floating-point numbers (full / half
  precision).
- **q8 / INT8** — 8-bit integer weights.
- **q4 / INT4** — 4-bit integer weights, stored block-wise with per-block scale
  factors.
- **q4f16** — 4-bit weights combined with 16-bit values for the non-quantized
  parts.
- **RTN** — Round-To-Nearest, the default, data-free quantization method: each
  weight is simply rounded to the closest representable value.
- **GPTQ / AWQ / HQQ** — more advanced, data-driven quantization methods that can
  improve quality at the same bit width, at the cost of much more compute and a
  calibration dataset.
- **MatMulNBits** — the ONNX Runtime operator that performs a matrix
  multiplication using block-wise N-bit (here 4-bit) weights.
- **Opset** — the ONNX operator-set version; 4-bit integer types require opset 21
  or newer.
- **AR / NAR** — Autoregressive (generates text one token at a time) /
  Non-Autoregressive (generates in parallel).

## How a model becomes a q4 ONNX package

Conversion has three stages. None of them is training; they only repackage an
already-trained model.

1. **Export (original format → ONNX).** A tool such as Hugging Face *Optimum*
   loads the original model and traces one run through it, recording the
   computation graph and the full-precision (fp32) weights into ONNX files. For a
   speech model this is split into components — typically an **audio encoder**
   (audio → features), an **embedding** table, and the **decoder** language model
   that produces text.
2. **Graph optimization.** Redundant operations are removed or fused. This changes
   speed, not results.
3. **Quantization.** The fp32 weights are re-stored at lower precision. A single
   conversion run usually emits several precision tiers at once (fp16, q8, q4,
   q4f16), which is why a published repository can contain far more files than any
   one app needs.

This app only downloads the **q4** files it needs; the other tiers in a public
repository are ignored.

## Precision tiers

| Tier | Weight storage | Relative size | Typical use |
| --- | --- | --- | --- |
| fp32 | 32-bit float | 1× (largest) | source for conversion |
| fp16 | 16-bit float | ~0.5× | GPUs with plenty of memory |
| q8 / INT8 | 8-bit integer | ~0.25× | CPU-friendly, good quality |
| **q4 / INT4** | **4-bit integer + scales** | **~0.15×** | **local GPU default** |
| q4f16 | 4-bit + 16-bit | ~0.13× | local GPU, slightly smaller |

## What "q4" actually is

With q4 the large weight matrices are divided into small **blocks** (commonly 32
or 64 values). Within each block, every weight is rounded to a 4-bit integer
(one of 16 levels), and the block stores one floating-point **scale** (and
sometimes an offset). At run time the original value is reconstructed
approximately as `integer × scale`. In ONNX this is carried out by the
`MatMulNBits` operator.

`q4f16` is the same 4-bit weight scheme, but the parts that are not quantized use
16-bit instead of 32-bit values — slightly smaller and often faster on a GPU, at
a small additional accuracy cost.

### Why a 1B/2B q4 speech model is still ~2 GB

For the 1B/2B-class q4 speech models used by this app, the lower bound
"parameters × bits ÷ 8" only covers the weights that are actually quantized. Real
packages are larger because:

- not every tensor is 4-bit (embeddings and some layers are kept higher);
- the model is split across several graph files, sometimes with separate external
  data files;
- the tokenizer, configuration, and pre/post-processing assets add size.

See [local-onnx-runtime.md](local-onnx-runtime.md) for the difference between
on-disk size and run-time memory.

## q4 vs. INT4 vs. INT8

"q4" and "INT4" describe the **same idea**: 4-bit integer weights stored
block-wise with scale factors. The different names come from different toolchains:
*q4* is the Transformers.js / ONNX Runtime label; *INT4* is the generic term used
by, for example, ONNX Runtime GenAI (the runtime behind this app's Nemotron
model). The file formats and operators are not interchangeable between runtimes,
but the underlying numeric representation is the same family. **INT8** is the same
concept at 8 bits: larger files, a little more accuracy.

## Is the conversion deterministic? Would re-doing it help?

For the default **RTN** method, the quantization math is deterministic: there is
no randomness, so the same source model plus the same settings (method, block
size, symmetric/asymmetric, opset, and tool versions) yields a numerically
equivalent result every time.

Two caveats:

- The **output file is not guaranteed to be byte-identical** between runs, because
  graph-node ordering, operator fusions, and metadata depend on the exact versions
  of the conversion tools. The *model behaviour* is equivalent even when the file
  hash differs.
- The result *can* be changed deliberately by turning "knobs": switching from RTN
  to a data-driven method (GPTQ/AWQ/HQQ) for potentially better quality; changing
  the block size; mixing precisions per component; or changing the opset.

Practical consequence: re-converting a model locally with the **same** settings
that a reputable public package already used produces the same quality and brings
no benefit — only spent compute and a redundant upload. This app therefore reuses
existing public q4 packages where they exist and are verified, and only considers
a custom conversion when a needed variant has no faithful public package (see
[granite-speech-4.1-onnx-variants.md](granite-speech-4.1-onnx-variants.md) for a
worked example).

## Why some models are published only as 4-bit

For running locally on a typical consumer GPU, 4-bit is the practical sweet spot:
it fits in memory, runs fast, and keeps accuracy close to the original for speech
transcription. Publishers often ship 4-bit as the smallest *usable* tier and may
omit larger tiers to save space. When a model in this app is offered only as q4
(or only as INT8), it reflects what is available and verified to run, not a
limitation of the app.

## Sources

- ONNX Runtime quantization (block-wise 4-bit, `MatMulNBits`, RTN/GPTQ/HQQ):
  <https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html>
- Transformers.js (ONNX models for the web/Node runtime):
  <https://huggingface.co/docs/transformers.js/index>
- Hugging Face Optimum (ONNX export):
  <https://huggingface.co/docs/optimum-onnx/onnx/overview>
