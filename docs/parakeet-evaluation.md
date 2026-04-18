# NVIDIA Parakeet - Evaluation for stt_app

This note is retained for discoverability because earlier discussions and
learning-log entries referenced Parakeet directly.

The current canonical evaluation is:

- [Local ASR Model Candidates - 2026 Re-evaluation](local-asr-model-candidates-2026.md)

## Current decision

- **Status:** Not implemented by design.
- **Decision:** Do not add the official NVIDIA NeMo Parakeet path to the
  production app.
- **Reason:** The official path remains NVIDIA-centered and does not solve the
  target Intel GPU use case.

## What changed

Parakeet is still a strong model family. `nvidia/parakeet-tdt-0.6b-v3` is the
relevant multilingual candidate for German and English. Community ONNX and
quantized variants now exist, so Parakeet can be included in an experimental
WebGPU benchmark.

That does not change the product decision: the official NeMo path is still a new
large runtime stack and mainly benefits NVIDIA users.

## Short recommendation

Do not implement Parakeet through NeMo. Optionally benchmark a community
ONNX/WebGPU Parakeet variant after Cohere and Granite, but do not prioritize it
unless it proves reliable on the target Intel GPU.
