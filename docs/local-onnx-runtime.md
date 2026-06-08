# Local ONNX Runtime Guide

Date: 2026-05-31

This document explains the experimental local ONNX paths used for Cohere
Transcribe, IBM Granite Speech, and NVIDIA Nemotron 3.5 in `stt_app`.

## Runtime Stack

The production local baseline is still `faster-whisper` through CTranslate2.
That path is CPU-first in this app and remains the most predictable local
runtime.

The experimental Cohere and Granite models use a separate out-of-process stack:

1. Python/PySide starts a controlled Node.js helper process.
2. The helper loads the JavaScript ONNX runtime dependencies.
3. Cohere and Granite 4.0 load q4 Transformers.js ONNX packages from the local
   Hugging Face cache.
4. Granite 4.1 loads raw INT8 ONNX Runtime graph files from the local Hugging
   Face cache.
5. Inference runs on the selected ONNX target when supported, with CPU fallback
   for normal `auto` use.
6. Python sends WAV paths over stdin and receives JSON results over stdout.

The helper is a child process by design. If the JavaScript runtime crashes, the
main app can report the error and continue instead of taking down the UI.

Nemotron uses a separate in-process Python path through ONNX Runtime GenAI. It
does not use the Node helper. ORT GenAI owns the cache-aware FastConformer/RNNT
streaming state and emits tokens incrementally for each new 560 ms audio chunk.

Granite Speech 4.1 is part of this runtime stack and is user-selectable. The
public 4.1 ONNX repos currently ship raw INT8/fp16w/fp32 graph bundles instead
of q4/int4 Transformers.js packages, so the app defaults to the INT8 tier and
labels it separately from q4 Cohere/Granite 4.0.

## Runtime Formats

ONNX is not a GPU-only format. ONNX is a portable model graph format, and ONNX
Runtime chooses one or more execution providers to run that graph. The same
ONNX model can run on CPU, CUDA, DirectML, WebGPU, OpenVINO, or another provider
if the model graph and provider support match.

CTranslate2 is different. It is a custom inference runtime and model format
optimized for supported Transformer architectures. In this app, CTranslate2 is
the mature production path because `faster-whisper` already handles Whisper
preprocessing, decoding, timestamps, language detection, quantization, and CPU
performance well.

GGUF is also different. It is a model-file format used by ggml/llama.cpp-style
runtimes. A GGUF file does not run by itself and is not automatically compatible
with ONNX Runtime or CTranslate2. Adding a GGUF ASR model means selecting,
packaging, testing, and maintaining a compatible GGUF runtime for that exact ASR
architecture.

## Execution Targets

`stt_app` exposes these ONNX targets for Cohere and Granite benchmarks:

| Target | Meaning | Recommendation |
| --- | --- | --- |
| `auto` | Try WebGPU, then DirectML on Windows, then CPU | Default for normal use |
| `gpu` | Try GPU targets only, currently WebGPU then DirectML | Diagnostic benchmark target |
| `webgpu` | Force Transformers.js WebGPU | Best current target on the Intel test machine |
| `dml` | Force ONNX Runtime DirectML | Diagnostic only for current Cohere/Granite |
| `cpu` | Force CPU | Most compatible, usually slowest |

For Granite Speech 4.1 raw ONNX graphs, the Node helper uses direct
`onnxruntime-node` sessions. In the current package, WebGPU and CPU are the
supported raw-graph targets; DirectML is skipped for 4.1 and normal `auto`
falls through to CPU if WebGPU cannot load.

Nemotron attempts DirectML and then CPU through ORT GenAI. The normal dependency
lock currently installs the CPU package because the published
`onnxruntime-genai-directml` package requires an `onnxruntime-directml>=1.26.0`
wheel that is not yet available from PyPI.

`wasm` is not a valid device for the Node runtime used here. In browser-oriented
ONNX stacks, WASM can mean CPU execution through WebAssembly. In this app's
Transformers.js v4 Node stack, the accepted CPU target is `cpu`.

## What `navigator.gpu.requestAdapter()` Means

`navigator.gpu.requestAdapter()` is the standard WebGPU API call that asks the
host environment for a GPU adapter. In browser terms, it returns a `GPUAdapter`
or `null` if no suitable adapter is available.

The first ONNX implementation used that probe to decide whether WebGPU should
be attempted. That was too conservative. On the Windows test machine, the probe
reported no adapter, but explicit Transformers.js `device: "webgpu"` inference
worked correctly. Transformers.js v4 has a rewritten WebGPU runtime that can run
in Node, Bun, Deno, browser, and desktop-like JavaScript environments, so the
app now treats the adapter probe as informational rather than authoritative.

Current policy:

- `auto` always attempts WebGPU first.
- If WebGPU fails to load or fails during inference, `auto` tries DirectML.
- If DirectML also fails, `auto` falls back to CPU.
- `gpu` may move from WebGPU to DirectML, but never falls back to CPU.

## WebGPU vs DirectML

The names are misleading if read literally.

WebGPU is not "a web-only GPU." It is a cross-platform GPU API exposed to
JavaScript runtimes. In Transformers.js v4, it sits behind an optimized ONNX
runtime path and can run locally in Node. It can use Intel, AMD, and NVIDIA GPUs
through the platform graphics stack.

DirectML is a Windows DirectX 12 machine-learning API. It is also
vendor-neutral and can use Intel, AMD, NVIDIA, and Qualcomm hardware. ONNX
Runtime's DirectML execution provider is broad and useful, but it has operator
and shape constraints. A model can load on DirectML and still fail when the
first unsupported graph operation is executed.

Observed on the target Windows/Intel machine:

- WebGPU works for Cohere and Granite.
- DirectML loads both models but fails during inference:
  - Cohere fails in `MultiHeadAttention`.
  - Granite fails in `Reshape`.
- CPU works for both, but is materially slower than WebGPU.

There is no universal "GPU is always faster" rule. Integrated GPUs in notebooks
share memory with the CPU, may throttle thermally, and may have weaker drivers.
For these models, WebGPU was faster on the tested Intel system. For other
machines, the benchmark tab is the source of truth.

## Memory Lifecycle

By default, the app does not keep Cohere and Granite ONNX models loaded after
normal dictation. The Node helper process is closed after the transcription
finishes. This avoids surprise RAM/VRAM use and avoids idle CPU load.

The Local tab has an expert option:

`Keep Cohere/Granite ONNX model loaded after dictation`

When enabled, the last selected Cohere or Granite model remains loaded until
settings change or the app exits. This removes the next transcription's
model-load latency, but keeps the model's RAM/VRAM allocation alive.

Nemotron remains preloaded and cached like faster-whisper because loading it
when the recording hotkey is pressed would block the start of a true-streaming
session. It is smaller than the Cohere and Granite ONNX models, but its memory
allocation still remains active until settings change or the app exits.

Important details:

- The app cannot ask the operating system to "keep the model only if memory is
  free." Loaded model memory is real process memory.
- Other applications can still pressure the OS and GPU driver, but the app
  cannot promise soft reclaim behavior.
- Benchmarks always close each ONNX case after measuring it.
- Startup/preload failures are explicitly cleaned up so failed runs do not
  leave orphaned Node helper processes.
- GPU memory shown in Task Manager can remain elevated briefly after a process
  exits because drivers and runtimes cache allocations. The decisive check is
  whether a `webgpu_asr_runner.mjs` process is still alive.

## Model Size vs RAM and VRAM

The on-disk download size is not the same thing as runtime memory.

The lower-bound estimate for weight storage is:

`parameter_count * bits_per_parameter / 8`

That estimate is only useful when the parameter count and quantized tensor set
are known. It does not mean every file in a package is 4-bit, and it does not
include metadata, runtime buffers, activations, or duplicate graph structures.

Why the current q4 ONNX downloads are still around 2 GB:

- Not every tensor is quantized to 4 bits.
- ONNX models often split encoder, decoder, embeddings, and external data into
  separate files.
- Some formats duplicate or separate embeddings, decoder-init, and decoder-step
  weights for runtime convenience.
- Tokenizers, configs, and pre/post-processing assets add size.
- Runtime memory includes graph optimizations, activation buffers, decoded audio,
  token buffers, and possibly KV cache.
- GPU drivers may reserve or cache extra memory beyond the model weights.

Approximate current downloads:

| Model | Runtime | Approximate download |
| --- | --- | ---: |
| `tiny` | CTranslate2 | 75 MB |
| `small` | CTranslate2 | 484 MB |
| `medium` | CTranslate2 | 1.4 GB |
| `large-v3-turbo` | CTranslate2 | 809 MB |
| `cohere-transcribe-03-2026` | q4 ONNX | 2.13 GB |
| `granite-4.0-1b-speech` | q4 ONNX | 1.84 GB |
| `granite-speech-4.1-2b` | AR INT8 raw ONNX graphs | 4.0 GB |
| `granite-speech-4.1-2b-plus` | AR INT8 raw ONNX graphs | 4.1 GB |
| `granite-speech-4.1-2b-nar` | NAR INT8 raw ONNX graphs | 2.5 GB |
| `nemotron-3.5-asr-streaming-0.6b-int4` | ORT GenAI INT4 | 793 MB |

Runtime memory can be higher than these values. For exact values on a target
machine, use Task Manager while running a fixed benchmark and check that the
helper process exits afterward.

## Parameter Counts and Quantization

Parameter-count names such as `1B`, `2B`, or `3B` are approximate marketing and
architecture labels. They do not directly predict download size.

Examples from currently evaluated candidates:

- Granite's "1B Speech" name refers to its base LLM family, but the full speech
  package includes an audio stack, adapter/projector components, tokenizer
  assets, and multiple ONNX graph files.
- Qwen3-ASR-0.6B community notes report the speech model as roughly 782M-900M
  parameters once audio encoder and LLM pieces are counted.
- Cohere's ONNX package is q4 and 2B parameters, so a raw 4-bit weight lower
  bound is already about 1 GB before unquantized tensors, model graph overhead,
  external data files, tokenizer assets, and runtime buffers are counted.
- Granite Speech 4.1 INT8 exports are larger than Granite 4.0 q4 and are not a
  drop-in replacement. The autoregressive variants use separate encoder,
  embedding, prompt-encode, and decode-step graph orchestration with a KV-cache
  loop. The NAR variant uses a separate encoder/editor contract with CTC draft
  decoding and insertion slots.

## Long Audio Behavior

The app is a dictation tool, not a long-form transcription server. Long audio
needs explicit handling because memory can grow in several places: decoded WAV
samples, mel features, audio tokens, generated text tokens, and decoder KV
cache.

Current behavior:

- The Node helper decodes the whole WAV file into one 16 kHz mono `Float32Array`
  before model-specific processing starts. This is usually fine for dictation,
  but very long files still allocate the decoded waveform in one process.
- Cohere uses the Transformers.js Cohere ASR pipeline. That pipeline calls the
  Cohere feature extractor's `split_audio()` helper, which splits long audio at
  quiet boundaries and joins per-chunk transcripts.
- Granite does not use the generic ASR pipeline. The app now chunks Granite
  audio at quiet boundaries with a maximum chunk size of 30 seconds before
  running generation for each chunk. This keeps the prompt/audio-token size
  bounded for long recordings.
- If the JavaScript helper still crashes or is killed by the OS, the main PySide
  app should report a transcription error instead of crashing with it.

Important limitation: chunking reduces peak model memory, but it is not the
same as a full long-form transcription system with diarization, overlap merging,
timestamp alignment, or context carry-over. For long meetings, a dedicated
segmentation/VAD workflow is still the safer product direction.

## Language Handling

`faster-whisper` can use automatic language detection. That is why the local
Whisper models support `Auto`.

The experimental ONNX models are model-specific:

- Cohere requires an explicit language. In this app, `Auto` maps to German for
  Cohere because German is the primary local workflow and it is safer than
  silently choosing English.
- Granite can use a generic transcription prompt. In this app, `Auto` keeps the
  generic prompt, while explicit German or English uses language-specific
  prompts.
- Nemotron supports automatic language detection and language-ID conditioning.
  The UI exposes the transcription-ready and broad-coverage languages from the
  official ORT GenAI example's language-ID mapping. It omits adaptation-ready
  languages because the model card says they require fine-tuning.

This means `Auto` is not identical across local model families. If a transcript
quality issue appears on Cohere, explicitly selecting German or English is the
first thing to test.

Granite Speech 4.1 ONNX exports are tagged and documented primarily for English
ASR. German dictation quality is not yet proven in this app, so explicit German
mode is available for daily use but should be benchmarked before replacing a
Whisper model.

## Best Current Practice

For the current Windows Intel GPU test machine:

1. Use `auto` for normal experimental ONNX dictation.
2. Use the Benchmark tab to compare `webgpu`, `dml`, and `cpu`.
3. Treat DirectML failures as provider/operator compatibility issues, not model
   download problems.
4. Enable keep-loaded only if the faster warm latency is worth the RAM/VRAM use.
5. Keep `large-v3-turbo` as the practical fallback until enough real dictation
   samples prove that an ONNX model is better.
6. Treat Nemotron as the first true-streaming local candidate; its measured CPU
   RTF on the Ryzen 5 7600X test machine is 0.229, but Intel GPU validation must
   wait for a reproducibly installable DirectML package.

## Sources

- MDN WebGPU `requestAdapter()`:
  <https://developer.mozilla.org/en-US/docs/Web/API/GPU/requestAdapter>
- Transformers.js v4 announcement:
  <https://huggingface.co/blog/transformersjs-v4>
- ONNX Runtime execution providers:
  <https://onnxruntime.ai/docs/execution-providers/>
- ONNX Runtime DirectML execution provider:
  <https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html>
- CTranslate2 project overview:
  <https://github.com/OpenNMT/CTranslate2>
- GGUF format reference:
  <https://www.mintlify.com/ggml-org/llama.cpp/concepts/gguf-format>
- Cohere Transcribe model card:
  <https://huggingface.co/CohereLabs/cohere-transcribe-03-2026>
- Granite ONNX/WebGPU model card:
  <https://huggingface.co/onnx-community/granite-4.0-1b-speech-ONNX>
- Granite Speech 4.1 2B ONNX export:
  <https://huggingface.co/smcleod/ibm-granite-speech-4.1-2b-onnx>
- Granite Speech 4.1 2B Plus ONNX export:
  <https://huggingface.co/smcleod/ibm-granite-speech-4.1-2b-plus-onnx>
- Granite Speech 4.1 2B NAR ONNX export:
  <https://huggingface.co/smcleod/ibm-granite-speech-4.1-2b-nar-onnx>
- Nemotron 3.5 multilingual INT4 ONNX export:
  <https://huggingface.co/onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4>
- Official ORT GenAI Nemotron Python example and language-ID mapping:
  <https://github.com/microsoft/onnxruntime-genai/blob/main/examples/python/nemotron_speech.py>
- ONNX Runtime GenAI:
  <https://github.com/microsoft/onnxruntime-genai>
