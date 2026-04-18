# IBM Granite Speech - Evaluation for stt_app

Date: 2026-04-18

This note keeps the Granite-specific evaluation discoverable. The broader
comparison lives in:

- [Local ASR Model Candidates - 2026 Re-evaluation](local-asr-model-candidates-2026.md)
- [Local ONNX Runtime Guide](local-onnx-runtime.md)

## Current Decision

- **Status:** Implemented as an experimental batch-only local model.
- **Model:** `granite-4.0-1b-speech`
- **Runtime:** q4 ONNX through the Transformers.js helper process.
- **Best target on the tested Intel Windows machine:** WebGPU.

Granite should remain experimental. It is useful to benchmark because it is
close to Cohere on public English ASR quality signals and is notably faster than
CPU when WebGPU works. It should not replace `large-v3-turbo` as the default
until real German and English dictation samples show a consistent quality win.

## Runtime Findings

The app can run Granite through:

- `webgpu`: works on the tested Intel GPU and is materially faster than CPU.
- `cpu`: works and is the reliable fallback.
- `dml`: loads, then fails during inference on the tested machine with a
  DirectML `Reshape` error.

The DirectML failure means this model is not currently a good DirectML target in
the app, even though DirectML is a vendor-neutral Windows GPU API.

## Language Behavior

Granite is prompt-based. The app now uses:

- a generic transcription prompt for `Auto`,
- a German-specific prompt when German is selected,
- an English-specific prompt when English is selected.

This is different from Cohere, where `Auto` must be mapped to an explicit
language because Cohere's ONNX path expects one.

## Size and Memory

The q4 ONNX download is about 1.84 GB. Runtime RAM/VRAM can be higher because
the model includes an audio encoder, decoder, tokenizer assets, activation
buffers, and GPU driver allocations.

The app chunks Granite audio at quiet boundaries with a maximum chunk size of
30 seconds before generation. This bounds prompt/audio-token growth for long
recordings, but Granite should still be treated as a dictation model rather
than a long-meeting transcription pipeline.

Benchmarks close each Granite helper process after the case. Normal dictation
also closes the helper by default. The expert keep-loaded setting can keep it
warm after dictation to avoid the next load cost.

## Recommendation

Keep Granite in the benchmark set next to Cohere:

1. Benchmark WebGPU against CPU on the target machine.
2. Ignore DirectML unless a future ONNX Runtime/Transformers.js release fixes
   the observed DirectML `Reshape` failure.
3. Compare transcript usefulness, not only speed. Granite may behave more like a
   speech-language model than a classic Whisper-style recognizer.

## Sources

- IBM Granite model card:
  <https://huggingface.co/ibm-granite/granite-4.0-1b-speech>
- Granite ONNX/WebGPU model card:
  <https://huggingface.co/onnx-community/granite-4.0-1b-speech-ONNX>
