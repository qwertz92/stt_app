# NVIDIA Parakeet - Evaluation for stt_app

This note is retained for discoverability because earlier discussions and
learning-log entries referenced Parakeet directly.

The current canonical evaluation is:

- [Local ASR Model Candidates - 2026 Re-evaluation](local-asr-model-candidates-2026.md)

## Current decision

This note is about the **NeMo/PyTorch path only**. The model itself ships: since
the onnx-asr engine was added, `parakeet-tdt-0.6b-v3` is a selectable local
model and the fastest one in the app. See
[Models & Offline Setup](models.md#available-models).

- **Status:** The NeMo runtime is not implemented, by design.
- **Decision:** Do not add the official NVIDIA NeMo Parakeet path to the
  production app.
- **Reason:** The official path remains NVIDIA-centered and does not solve the
  target Intel GPU use case. The community ONNX export reaches the same model
  without a second heavyweight ML runtime.

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
