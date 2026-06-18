# Granite Speech 4.1 ONNX Variants: Pipeline-Path Status & Future Work

Last verified: **2026-06-17** (on the Windows / Intel Arc A750 development machine,
with live Hugging Face access and a working WebGPU runtime).

This document records why, of the three IBM Granite Speech 4.1 variants, only the
base 2B model runs on the fast Transformers.js **q4 pipeline path**, while **Plus**
and **NAR** stay on the slower raw INT8 path. It is meant as a durable research
record so the analysis below does not have to be repeated, and so a future agent
can pick up Plus/NAR integration with full context when the upstream situation
changes.

For the general concepts referenced here (ONNX, q4, the pipeline vs. raw runtime),
see [local-onnx-q4-conversion.md](local-onnx-q4-conversion.md) and
[local-onnx-runtime.md](local-onnx-runtime.md).

## Summary

| Variant | HF `model_type` | App runtime path | q4 pipeline today? | Blocker |
| --- | --- | --- | --- | --- |
| `granite-speech-4.1-2b` (AR) | `granite_speech` | **q4 Transformers.js pipeline** | **Yes** | — (shipped) |
| `granite-speech-4.1-2b-plus` (AR) | `granite_speech_plus` | raw INT8 graphs | No | distinct architecture; no faithful q4; no JS class |
| `granite-speech-4.1-2b-nar` (NAR) | `granite_speech_nar` | raw INT8 graphs | No | non-autoregressive; no JS class; no q4 |

"AR" = autoregressive (token-by-token generation). "NAR" = non-autoregressive
(parallel decoding). "Pipeline path" = the high-level Transformers.js
`GraniteSpeechForConditionalGeneration` class on WebGPU, the same path used by
`granite-4.0-1b-speech`. "Raw path" = hand-written `onnxruntime-node` graph
sessions in `webgpu_asr_runner.mjs` (CPU-bound in practice; see the WebGPU
`Einsum` shader bug noted in `local-onnx-runtime.md`).

## Why the base 2B works (reference)

`granite-speech-4.1-2b` has `model_type: granite_speech` and architecture
`GraniteSpeechForConditionalGeneration` — identical, dimension-for-dimension, to
`granite-4.0-1b-speech` (hidden size 2048, 40 text layers, vocab 100353, audio
encoder 1024-dim × 16 layers). A faithful q4 Transformers.js package already
exists at
[`onnx-community/granite-speech-4.1-2b-ONNX`](https://huggingface.co/onnx-community/granite-speech-4.1-2b-ONNX)
in the exact 4.0 layout (`onnx/audio_encoder_q4.onnx`, `onnx/embed_tokens_q4.onnx`,
`onnx/decoder_model_merged_q4.onnx`, plus `.onnx_data` and the config/tokenizer
files). It was verified on 2026-06-17 to load on WebGPU (no `Einsum` crash) and
transcribe German, English, and French correctly at roughly 0.13–0.19 real-time
factor on the Arc A750. The app therefore points `granite-speech-4.1-2b` at this
repo and routes it through the pipeline path.

## Plus — why it stays on the raw INT8 path

### What "Plus" is

`granite-speech-4.1-2b-plus` is a **separate architecture**: `model_type:
granite_speech_plus`, architecture `GraniteSpeechPlusForConditionalGeneration`
(native in `transformers >= 5.8`, no `trust_remote_code` needed). Relative to the
base model it:

- **Enhances the projector**: it consumes the concatenation of the speech
  encoder's *final* hidden states with *an arbitrary subset of its intermediate*
  hidden states along the feature dimension. This is a real graph-level
  difference, even though the high-level component set (encoder, projector,
  language model, optional LoRA adapter) is the same as the base model.
  ("LoRA" = Low-Rank Adaptation, a small set of adapter weights applied to the
  language model.)
- Adds two prompt-activated features: **speaker attribution** (`[Speaker N]:`
  tags) and **word-level timestamps** (`[T:N]` end-time tags in centiseconds).
- **Drops punctuation and capitalization** compared with the base model.

Config facts (checked 2026-06-17): `text_config.tie_word_embeddings: True`,
`has_lora_adapter: True`, plus `projector_config` / `window_size` /
`downsample_rate` keys. Source weights:
[`ibm-granite/granite-speech-4.1-2b-plus`](https://huggingface.co/ibm-granite/granite-speech-4.1-2b-plus).

### What was actually tested

The only public Transformers.js q4 build is
[`valoomba/granite-speech-4.1-2b-plus-ONNX`](https://huggingface.co/valoomba/granite-speech-4.1-2b-plus-ONNX).
Its `config.json` declares `model_type: granite_speech` and
`GraniteSpeechForConditionalGeneration` — i.e. the **Plus weights were exported
through the *base* architecture**, discarding the Plus projector behaviour.

Loaded through the app's pipeline path on WebGPU (Arc A750, 2026-06-17):

- German (10.98 s TTS clip): correct — "Die schnelle Diktiergerät wandelt
  gesprochene Sprache zuverlässig in geschriebenen Text um. …"
- English (24.94 s clip, generic prompt): **broken** — long runs of `<unk>`
  tokens.
- English (2 s clip, `en` prompt): **broken** — empty output.

Conclusion: this build is unusable for English. The failure is consistent with
the base-architecture mis-export starving the projector of the intermediate
encoder hidden states it needs.

### The three blockers

1. **No JS model class.** The installed Transformers.js
   (`@huggingface/transformers`, latest published 4.2.0 as of 2026-06-17)
   registry maps only `granite_speech -> GraniteSpeechForConditionalGeneration`.
   There is no `granite_speech_plus` / `GraniteSpeechPlusForConditionalGeneration`
   entry, so a correctly-labelled Plus package would not load at all; the only way
   to make it load is to relabel it as base `granite_speech`, which is exactly the
   unfaithful path that breaks English.
2. **No ONNX export config.** `optimum-onnx` `model_configs.py` registers only a
   text-generation `granite` config (Llama-based). There is **no** `granite_speech`
   or `granite_speech_plus` ONNX export config in stock optimum. The base
   `granite_speech` packages from `onnx-community` are produced by bespoke export
   tooling that is not part of the public optimum/transformers.js converter, and
   nothing public exports `granite_speech_plus`.
3. **Unverified pipeline compatibility even with a faithful export.** The base JS
   processor (`processing_granite_speech.js`) computes the number of audio
   placeholder tokens from `projector_window_size` / `projector_downsample_rate`.
   If Plus's projector changes the encoder-length → audio-token relationship, even
   a faithful `audio_encoder.onnx` might not splice correctly through the base JS
   class. This is plausible-but-unproven and would need empirical testing.

### What would make Plus easy (re-check triggers)

Any **one** of the following would reduce Plus to a config-only change, almost
identical to the base-2B change (update `MODEL_REPO_MAP`, set precision to `q4`,
add the q4 layout, add the model to `GRANITE_PIPELINE_MODELS` in
`webgpu_asr_runner.mjs`):

- `onnx-community` (or another reputable publisher) ships a **faithful** q4
  `granite-speech-4.1-2b-plus-ONNX` in the 4.0 layout — *and* it loads through the
  base `GraniteSpeechForConditionalGeneration` JS class. Re-check:
  `https://huggingface.co/onnx-community/granite-speech-4.1-2b-plus-ONNX`.
- Transformers.js adds a `granite_speech_plus` class to its model registry
  (`src/models/registry.js`) — then a Plus-labelled package could load directly.
- optimum gains a `granite_speech` / `granite_speech_plus` ONNX export config,
  making a faithful local export a stock `optimum-cli export onnx` run.

### Doing it ourselves (scope, if Plus is ever required)

A self-conversion is *possible* but is an R&D task, not a script run. A future
agent tasked with it should expect to:

1. **Build the export tooling.** Stock optimum cannot export `granite_speech`.
   Either locate `onnx-community`'s bespoke Granite speech export script/notebook
   (start from the base `granite_speech` export that produced the 4.0/4.1-2b
   packages) or write a custom `optimum` `OnnxConfig` that splits the model into
   the `audio_encoder` / `embed_tokens` / `decoder_model_merged` components.
2. **Trace the *real* Plus forward.** Load the model as
   `GraniteSpeechPlusForConditionalGeneration` (`transformers >= 5.8`) so the
   exported `audio_encoder.onnx` actually contains the intermediate-hidden-state
   projector. This is the step valoomba got wrong.
3. **Quantize to q4** with ONNX Runtime's `MatMulNBits` (RTN, block size 32 or
   64), matching the base packages.
4. **Verify** on WebGPU through the base JS class (German + English, no `<unk>`
   spam, correct text, device reported as `webgpu`). If splicing fails because of
   blocker 3 above, the base JS class is insufficient and a `granite_speech_plus`
   JS class would also be required (a much larger, fork-maintenance undertaking).
5. **Publish** the q4 package (Apache-2.0, attribution to IBM) and point the app
   at it.

Resource profile of such a conversion (CPU/RAM/disk bound, **GPU not used** for
the conversion itself): ~16–32 GB system RAM peak for a 2B fp32 export, ~30–40 GB
free disk for the full dtype set plus the source download, tens of minutes to a
couple of hours of CPU time, and a ~2 GB upload. The Arc A750 only matters for
the inference *verification*, not the conversion.

**Cost/benefit note:** for a dictation app, Plus's distinguishing features
(speaker tags, word timestamps) are not used, and Plus drops punctuation and
capitalization. The base 2B already transcribes de/en/fr well on WebGPU. Plus is
therefore low priority unless its specific rich-transcription features become a
product requirement.

## NAR — why it stays on the raw INT8 path

`granite-speech-4.1-2b-nar` has `model_type: granite_speech_nar` (architecture is
non-autoregressive, with a CTC draft + an "editor" pass and insertion slots, and
it carries `custom_code`). It is appreciably faster than the AR models because it
does not run a token-by-token generation loop.

It cannot use the pipeline path because:

- It is **not** `GraniteSpeechForConditionalGeneration`; there is no autoregressive
  `generate` loop to drive, and Transformers.js has no NAR class.
- There is **no** Transformers.js q4 build (only `smcleod` raw INT8 ONNX, a GGUF
  build for the CrispASR/llama.cpp runtime, and MLX builds for Apple silicon).

NAR therefore stays on the app's existing raw `onnxruntime-node` path
(`loadGranite41NarRuntime` in `webgpu_asr_runner.mjs`), which executes the model's
real exported graphs. Making NAR fast on the GPU would require either upstream
Transformers.js NAR support or a custom WebGPU runtime for its graph contract —
both substantial. Re-check: a future `onnx-community/granite-speech-4.1-2b-nar-ONNX`
or `granite_speech_nar` support in Transformers.js.

## How to re-check (quick commands)

- List public ONNX builds: search the Hub for `granite speech 4.1` filtered to
  `library: transformers.js`, or open the candidate repo URLs above directly.
- Confirm the Transformers.js model registry:
  look for `granite_speech_plus` in
  `node_modules/@huggingface/transformers/src/models/registry.js`.
- Confirm a candidate package's architecture before trusting it: read its
  `config.json` `model_type` / `architectures`. A genuine Plus build must be
  `granite_speech_plus`, not `granite_speech`.
- Smoke-test any candidate on WebGPU with **both** a German and an English 16 kHz
  mono WAV before integrating; reject builds that emit `<unk>` runs or empty text
  for either language.
