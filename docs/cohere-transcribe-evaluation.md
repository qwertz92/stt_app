# Cohere Transcribe - Evaluation for stt_app

This note is retained for discoverability because earlier discussions and
learning-log entries referenced Cohere directly.

The current canonical evaluation is:

- [Local ASR Model Candidates - 2026 Re-evaluation](local-asr-model-candidates-2026.md)

## Summary

- `cohere-transcribe-03-2026` is a selectable local ONNX/WebGPU model and one of
  the recommended high-quality options. Batch mode only.
- The default model stays `small` because it needs no GPU or Node.js.
- On the tested Ryzen 7600X + Arc A750 it runs at RTF 0.071 on WebGPU (0.137 on
  CPU) — faster than `small` or `large-v3-turbo`. See
  [Local Benchmark Results](benchmarks/README.md).

## Why this is not a drop-in local model

`cohere-transcribe-03-2026` is a real local candidate now: it has open weights,
an Apache 2.0 license, official `transformers` usage, and an ONNX/WebGPU model
variant. However, it is not a CTranslate2 Whisper model and cannot be added to
`MODEL_REPO_MAP` as another local model size.

The current integration therefore uses a separate runtime path, model cache
detection, download filters, settings UI constraints, error handling, and
packaging hooks.

## Short recommendation

On a machine with a working GPU, prefer Cohere (or Granite Speech 4.1) over the
Whisper models for quality and speed — it is a far newer system than the
multi-year-old Whisper models. Confirm German/English quality on your own audio
with the [benchmark](advanced-setup.md#benchmarking), which is the per-machine
source of truth.
