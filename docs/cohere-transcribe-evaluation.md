# Cohere Transcribe - Evaluation for stt_app

This note is retained for discoverability because earlier discussions and
learning-log entries referenced Cohere directly.

The current canonical evaluation is:

- [Local ASR Model Candidates - 2026 Re-evaluation](local-asr-model-candidates-2026.md)

## Current decision

- **Status:** Implemented as experimental batch-only local model.
- **Decision:** Cohere Transcribe is available only as an experimental local
  ONNX/WebGPU model, not as the stable/default local engine.
- **Best next step:** Benchmark `cohere-transcribe-03-2026` on the target Intel
  GPU against the current CTranslate2 `faster-whisper` models.

## Why this is not a drop-in local model

`cohere-transcribe-03-2026` is a real local candidate now: it has open weights,
an Apache 2.0 license, official `transformers` usage, and an ONNX/WebGPU model
variant. However, it is not a CTranslate2 Whisper model and cannot be added to
`MODEL_REPO_MAP` as another local model size.

The current integration therefore uses a separate runtime path, model cache
detection, download filters, settings UI constraints, error handling, and
packaging hooks.

## Short recommendation

Treat Cohere as the first model to benchmark in an experimental ONNX/WebGPU
runner. Do not recommend it over `large-v3-turbo` or `large-v3` until it proves
better for German and English dictation on the user's actual Intel GPU machine.
