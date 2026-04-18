# Qwen3-ASR - Evaluation for stt_app

Date: 2026-04-18

This document evaluates `Qwen/Qwen3-ASR-0.6B` and `Qwen/Qwen3-ASR-1.7B` for
possible local use in `stt_app`.

## Current Decision

- **Status:** Not implemented in the app.
- **Reason:** No clean, maintained WebGPU/Transformers.js ONNX path was found
  for these models.
- **Recommendation:** Track Qwen3-ASR, but do not add it to the selectable local
  model list until a runtime fits the app's Windows Intel/AMD/NVIDIA goal.

This is a runtime decision, not a quality rejection. Qwen3-ASR is a serious ASR
family, but adding it today would require a new runtime family rather than a
small extension of the current ONNX/WebGPU helper.

The installed Transformers.js 4.1.0 build was also checked locally. Its Node
bundle does not expose a Qwen3-ASR model implementation, while it does expose
the Cohere and Granite ASR classes used by the current helper.

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
| ONNX CPU | Mainly 0.6B | CPU-only, no WebGPU path, different Python dependencies |
| GGUF / ggml | 0.6B and 1.7B variants | Promising sizes, but requires a new native runtime or CLI integration |
| MLX | Apple Silicon | Not useful for Windows Intel GPU |
| vLLM | Official fast path | Strong for servers/NVIDIA-style deployments, heavy for this desktop app |

The 0.6B ONNX CPU community package is technically interesting, but it does not
solve the target GPU requirement. It also brings separate audio preprocessing,
chunking, tokenizer, decoder-init, decoder-step, and KV-cache behavior that the
current app would need to own and test.

GGUF is more attractive than PyTorch for local CPU memory, but it is still not a
drop-in change. The app would need to vendor or manage a native runtime, package
platform binaries, define model file validation, handle process lifecycle, and
benchmark quality independently.

## Size Notes

The model names are not exact file-size promises.

Observed community package examples:

- Qwen3-ASR-0.6B GGUF q4 variants are roughly 0.5-0.7 GB.
- Qwen3-ASR-1.7B GGUF q4 variants are roughly 1.2 GB.
- A Qwen3-ASR-0.6B ONNX CPU package lists roughly 2.5 GB total because it splits
  encoder, decoder, and embeddings into separate files and not every component
  is 4-bit.

This explains why "0.6B" does not automatically mean "about 300 MB at 4-bit."
The ASR package includes an audio encoder, projector, LLM decoder, tokenizer,
and runtime-specific graph layout.

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

1. Wait for a maintained Transformers.js/WebGPU ONNX export.
2. Build a separate GGUF runner and package it with the app.
3. Add an optional Python `qwen-asr` provider and accept the heavy PyTorch/vLLM
   dependency tradeoff.
4. Add the community ONNX CPU package for 0.6B only and clearly label it CPU
   only.

The first option best matches the current app architecture. The second option
may be worth a separate experiment if GGUF Qwen3-ASR proves clearly better than
Cohere/Granite on real dictation samples.

## Recommendation

Do not add Qwen3-ASR to the selectable local model list in this branch. Doing so
without a compatible runtime would create a misleading model option that cannot
use the same WebGPU pipeline as Cohere and Granite.

Revisit Qwen3-ASR when one of these becomes true:

- a maintained ONNX/WebGPU model appears for Transformers.js,
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
- Qwen3-ASR 0.6B ONNX CPU community package:
  <https://huggingface.co/wolfofbackstreet/Qwen3-ASR-0.6B-ONNX-CPU>
- Qwen3-ASR GGUF community package:
  <https://huggingface.co/Alkd/qwen3-asr-gguf>
- Qwen3-ASR 1.7B GGUF community package:
  <https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF>
