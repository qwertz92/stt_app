# IBM Granite Speech - Evaluation for stt_app

Date: 2026-04-18 · Updated: 2026-06-17 (Granite 4.1 2B now runs as a q4 WebGPU
model and tops the Open ASR Leaderboard)

This note keeps the Granite-specific evaluation discoverable. The broader
comparison lives in:

- [Local ASR Model Candidates - 2026 Re-evaluation](local-asr-model-candidates-2026.md)
- [Local ONNX Runtime Guide](local-onnx-runtime.md)

## Summary

- Selectable local GPU models, batch mode only.
- **Models:** `granite-4.0-1b-speech`, `granite-speech-4.1-2b`,
  `granite-speech-4.1-2b-plus`, `granite-speech-4.1-2b-nar`
- **Runtime:** Granite 4.0 **and Granite 4.1 2B** use q4 ONNX through the
  Transformers.js helper (the WebGPU pipeline). Granite 4.1 **Plus and NAR** are
  different architectures with no faithful q4 package yet and stay on raw INT8
  ONNX Runtime graphs (CPU-bound); see
  [Granite Speech 4.1 ONNX variants](granite-speech-4.1-onnx-variants.md).
- **Best target on the tested Ryzen 7600X + Arc A750:** WebGPU for Granite 4.0
  and Granite 4.1 2B; CPU for the raw Granite 4.1 Plus/NAR graphs.

`granite-speech-4.1-2b` currently **tops the
[Open ASR Leaderboard](https://huggingface.co/spaces/hf-audio/open_asr_leaderboard)**
(~5.3% mean English WER) and is one of the recommended high-quality local models.
Granite 4.0 remains a smaller GPU fallback. Confirm German/English quality on your
own audio before changing the zero-setup `small` default.

## Runtime Findings

The app can run Granite 4.0 through:

- `webgpu`: works on the tested Intel GPU and is materially faster than CPU.
- `cpu`: works and is the reliable fallback.
- `dml`: loads, then fails during inference on the tested machine with a
  DirectML `Reshape` error.

The DirectML failure means this model is not currently a good DirectML target in
the app, even though DirectML is a vendor-neutral Windows GPU API.

Granite 4.1 now has two runtime paths. The **2B** base model ships as a q4
Transformers.js package (`onnx-community/granite-speech-4.1-2b-ONNX`) and runs on
the **same WebGPU pipeline as Granite 4.0** — verified on the Arc A750 on
2026-06-17 with correct German, English, and French output and no `Einsum` crash.
The **Plus and NAR** variants still use raw INT8 `onnxruntime-node` graphs whose
first WebGPU inference fails when ONNX Runtime Web cannot build a valid shader
module for the encoder's `Einsum` operator, so `auto` falls back to CPU for those
two. The clean q4 export avoids that bug — which is exactly why the 2B model moved
to the pipeline path.

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

Granite Speech 4.1 2B is a q4 download of about 1.84 GB (the same size class as
Granite 4.0). The Plus and NAR raw INT8 downloads are larger: about 4.1 GB for
`granite-speech-4.1-2b-plus` and 2.5 GB for `granite-speech-4.1-2b-nar`. The AR
models use a KV-cache decode loop; the NAR model uses a separate encoder/editor
path and should not be treated as the same runtime flag.

The app chunks Granite audio at quiet boundaries with a maximum chunk size of
30 seconds before generation. This bounds prompt/audio-token growth for long
recordings, but Granite should still be treated as a dictation model rather
than a long-meeting transcription pipeline.

Benchmarks close each Granite helper process after the case. Normal dictation
also closes the helper by default. The expert keep-loaded setting can keep it
warm after dictation to avoid the next load cost.

## Recommendation

Keep Granite in the benchmark set next to Cohere:

1. Benchmark WebGPU against CPU on the target machine; Granite 4.1 2B runs on
   WebGPU, while the Plus/NAR raw graphs still fall back to CPU.
2. Ignore DirectML unless a future ONNX Runtime/Transformers.js release fixes
   the observed DirectML `Reshape` failure.
3. Compare transcript usefulness, not only speed. Granite may behave more like a
   speech-language model than a classic Whisper-style recognizer.

## Sources

- IBM Granite model card:
  <https://huggingface.co/ibm-granite/granite-4.0-1b-speech>
- Granite ONNX/WebGPU model card:
  <https://huggingface.co/onnx-community/granite-4.0-1b-speech-ONNX>
- Granite Speech 4.1 2B q4 ONNX-web export (used by the app):
  <https://huggingface.co/onnx-community/granite-speech-4.1-2b-ONNX>
- Open ASR Leaderboard:
  <https://huggingface.co/spaces/hf-audio/open_asr_leaderboard>
- Granite Speech 4.1 2B Plus ONNX export:
  <https://huggingface.co/smcleod/ibm-granite-speech-4.1-2b-plus-onnx>
- Granite Speech 4.1 2B NAR ONNX export:
  <https://huggingface.co/smcleod/ibm-granite-speech-4.1-2b-nar-onnx>
