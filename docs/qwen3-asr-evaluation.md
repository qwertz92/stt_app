# Qwen3-ASR - Evaluation for stt_app

Date: 2026-04-18

This document evaluates `Qwen/Qwen3-ASR-0.6B` and `Qwen/Qwen3-ASR-1.7B` for
possible local use in `stt_app`.

## Current Decision

- **Status:** Not implemented in the app.
- **Reason:** ONNX and GGUF community exports exist, but they use different
  runtime assumptions than the current Cohere/Granite Transformers.js helper.
- **Recommendation:** Track Qwen3-ASR, but do not add it to the selectable local
  model list until a runtime fits the app's Windows Intel/AMD/NVIDIA goal.

This is a runtime decision, not a quality rejection. Qwen3-ASR is a serious ASR
family, but adding it today would require a new runtime family rather than a
small extension of the current ONNX/WebGPU helper.

The installed Transformers.js 4.1.0 build was also checked locally. Its Node
bundle does not expose a Qwen3-ASR model implementation, while it does expose
the Cohere and Granite ASR classes used by the current helper.

This means the Hugging Face "Quantized" tab is not enough by itself. A
quantized ONNX or GGUF artifact can be real and useful, but the app still needs
a compatible runtime that owns audio preprocessing, prompt construction, decoder
init/step, KV cache management, language selection, chunking, and output
joining.

## Official Runtime

The official Qwen model card recommends the `qwen-asr` Python package. It
provides:

- a Transformers backend,
- a vLLM backend,
- batch inference,
- streaming inference through vLLM,
- optional forced alignment.

The official examples use PyTorch-style devices such as CUDA for acceleration.
That does not match this app's main hardware requirement: Windows local
inference that can benefit Intel, AMD, and NVIDIA users without an NVIDIA-only
stack.

## Community Runtime Options

Community alternatives exist:

| Runtime | Models | Notes |
| --- | --- | --- |
| ONNX Runtime custom pipeline | Mainly 0.6B | Includes FP32 and int4 exports, but needs custom decoder/KV-cache code |
| ONNX CPU packaged pipeline | 0.6B | CPU-only packages exist with silence chunking and `onnxruntime` dependencies |
| GGUF / ggml | 0.6B and 1.7B variants | Promising sizes, but requires a new native runtime or CLI integration |
| MLX | Apple Silicon | Not useful for Windows Intel GPU |
| vLLM | Official fast path | Strong for servers/NVIDIA-style deployments, heavy for this desktop app |

The 0.6B ONNX Runtime exports are technically interesting. One community export
includes FP32 and int4 MatMulNBits variants, but it expects a custom inference
engine rather than the Transformers.js ASR pipeline. Another community package
wraps a CPU-only ONNX pipeline and reports long-audio silence chunking.

These options do not solve the target GPU requirement. They also bring separate
audio preprocessing, chunking, tokenizer, decoder-init, decoder-step, and
KV-cache behavior that the current app would need to own and test.

GGUF is more attractive than PyTorch for local CPU memory, but it is still not a
drop-in change. The app would need to vendor or manage a native runtime, package
platform binaries, define model file validation, handle process lifecycle, and
benchmark quality independently.

The currently visible Rust/Candle path for Qwen3-ASR is useful, but its GPU
story is CUDA on Windows/Linux and Metal on macOS. It does not satisfy this
app's Intel/AMD Windows GPU goal.

## Quality and Speed Evidence

There is no strong public evidence that Qwen3-ASR is a better fit than the two
models already implemented for this app.

Public English leaderboard numbers currently favor Cohere and Granite over
Qwen3-ASR-1.7B:

| Model | English Open ASR average WER |
| --- | ---: |
| Cohere Transcribe | 5.42 |
| IBM Granite 4.0 1B Speech | 5.52 |
| Qwen3-ASR-1.7B | 5.76 |

Lower WER is better. These numbers do not prove Qwen is worse for every real
dictation use case, but they do mean Qwen is not an obvious upgrade for English
or German dictation.

Community performance data is mixed:

- The CPU ONNX package reports desktop RTF around 0.32 and Intel N100 long-audio
  RTF around 0.71 with VAD chunking. That is useful CPU performance, but not
  clearly better than the Cohere WebGPU result measured in this branch.
- The Qwen3-ASR-0.6B GGUF package reports roughly realtime CPU performance on a
  short sample and an average Open ASR WER of 6.42, behind Cohere and Granite in
  the English leaderboard framing.
- A third-party Japanese RTX 5090 benchmark found Qwen3-ASR-1.7B more accurate
  than Whisper and Granite for Japanese media audio, but slower than Whisper and
  tied to hardware/runtime assumptions that do not match the target Intel GPU
  desktop.

The practical conclusion is conservative: Qwen3-ASR is worth watching, but it
does not justify adding a new runtime family until real app-specific benchmark
data shows a quality win that Cohere and Granite do not provide.

## Size Notes

The model names are not exact file-size promises.

Observed community package examples:

- Qwen3-ASR-0.6B GGUF q4 variants are roughly 0.7 GB, with q8 around 1 GB and
  f16 around 1.9 GB in one community package.
- Some Qwen3-ASR-1.7B GGUF variants are roughly 1.2 GB or more depending on the
  quantization and packaging.
- A Qwen3-ASR-0.6B ONNX CPU package lists roughly 2.5 GB total because it splits
  encoder, decoder, and embeddings into separate files and not every component
  is 4-bit.

This explains why "0.6B" does not automatically mean "about 300 MB at 4-bit."
The ASR package includes an audio encoder, projector, LLM decoder, tokenizer,
and runtime-specific graph layout.

## Long Audio and Memory

Qwen3-ASR needs explicit long-audio strategy. The official model card says the
models support long audio, but the runtime is expected to manage batching,
chunking, and memory. The official examples expose `max_inference_batch_size`
and recommend FlashAttention 2 for lower memory and faster long-input handling.

Community evidence points in the same direction. The CPU ONNX package reports
that a 10-minute file can exceed 15 GB and get OOM-killed without chunking, but
uses 30-second silence chunks to bring peak memory down to about 5.7 GB in that
test. The Rust Qwen3-ASR documentation also recommends resetting or segmenting
long streams because per-step latency grows with session duration.

So Qwen should not be added to this app without a proven segmentation policy.

## Language Behavior

The official Qwen3-ASR card says the 0.6B and 1.7B models support language
identification and ASR for 30 languages plus 22 Chinese dialects. The official
API allows `language=None` for automatic language detection, or an explicit
language when the caller wants to force it.

That is a better language story than Cohere's current ONNX path, but it depends
on using a runtime that faithfully implements Qwen's official preprocessing and
generation behavior.

## What Would Be Required to Add It

To add Qwen3-ASR properly, choose one of these paths:

1. Wait for a maintained Transformers.js/WebGPU ASR pipeline export.
2. Build or vendor a separate Qwen ONNX Runtime runner and package it.
3. Build or vendor a separate GGUF/Rust runner and package it.
4. Add an optional Python `qwen-asr` provider and accept the heavy PyTorch/vLLM
   dependency tradeoff.
5. Add the community ONNX CPU package for 0.6B only and clearly label it CPU
   only.

The first option best matches the current app architecture. The second or third
option may be worth a separate experiment if Qwen3-ASR proves clearly better
than Cohere/Granite on real dictation samples.

## Recommendation

Do not add Qwen3-ASR to the selectable local model list in this branch. Doing so
without a compatible runtime would create a misleading model option that cannot
use the same WebGPU pipeline as Cohere and Granite.

Revisit Qwen3-ASR when one of these becomes true:

- a maintained ONNX/WebGPU ASR pipeline appears for Transformers.js,
- a reliable GGUF runner is selected for the app,
- the user explicitly accepts a separate CPU-only or NVIDIA-oriented runtime for
  Qwen.

## Sources

- Official Qwen3-ASR 0.6B model card:
  <https://huggingface.co/Qwen/Qwen3-ASR-0.6B>
- Official Qwen3-ASR 1.7B model card:
  <https://huggingface.co/Qwen/Qwen3-ASR-1.7B>
- Qwen3-ASR 0.6B GGUF community package:
  <https://huggingface.co/cstr/qwen3-asr-0.6b-GGUF>
- Qwen3-ASR 0.6B ONNX Runtime community package:
  <https://huggingface.co/andrewleech/qwen3-asr-0.6b-onnx>
- Qwen3-ASR 0.6B ONNX CPU community package:
  <https://huggingface.co/wolfofbackstreet/Qwen3-ASR-0.6B-ONNX-CPU>
- Qwen3-ASR 1.7B FP8 community package:
  <https://huggingface.co/vrfai/Qwen3-ASR-1.7B-fp8>
- Qwen3-ASR GGUF community package:
  <https://huggingface.co/Alkd/qwen3-asr-gguf>
- Qwen3-ASR 1.7B GGUF community package:
  <https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF>
- Qwen3-ASR Rust runtime:
  <https://docs.rs/crate/qwen3-asr/0.2.1>
- Cohere English Open ASR comparison:
  <https://cohere.com/blog/transcribe>
- Japanese ASR comparison:
  <https://neosophie.com/en/blog/20260226-japanese-asr-benchmark>
