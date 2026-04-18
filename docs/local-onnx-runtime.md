# Local ONNX Runtime Guide

Date: 2026-04-18

This document explains the experimental local ONNX path used for Cohere
Transcribe and IBM Granite Speech in `stt_app`.

## Runtime Stack

The production local baseline is still `faster-whisper` through CTranslate2.
That path is CPU-first in this app and remains the most predictable local
runtime.

The experimental Cohere and Granite models use a separate out-of-process stack:

1. Python/PySide starts a controlled Node.js helper process.
2. The helper loads `@huggingface/transformers`.
3. Transformers.js loads q4 ONNX model files from the local Hugging Face cache.
4. Inference runs on WebGPU, DirectML, or CPU depending on the selected target
   and runtime support.
5. Python sends WAV paths over stdin and receives JSON results over stdout.

The helper is a child process by design. If the JavaScript runtime crashes, the
main app can report the error and continue instead of taking down the UI.

## Execution Targets

`stt_app` exposes these ONNX targets for Cohere and Granite benchmarks:

| Target | Meaning | Recommendation |
| --- | --- | --- |
| `auto` | Try WebGPU, then DirectML on Windows, then CPU | Default for normal use |
| `gpu` | Try GPU targets only, currently WebGPU then DirectML | Diagnostic benchmark target |
| `webgpu` | Force Transformers.js WebGPU | Best current target on the Intel test machine |
| `dml` | Force ONNX Runtime DirectML | Diagnostic only for current Cohere/Granite |
| `cpu` | Force CPU | Most compatible, usually slowest |

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

By default, the app does not keep experimental ONNX models loaded after normal
dictation. The Node helper process is closed after the transcription finishes.
This avoids surprise RAM/VRAM use and avoids idle CPU load.

The Local tab has an expert option:

`Keep experimental ONNX model loaded after dictation`

When enabled, the last selected ONNX model remains loaded until settings change
or the app exits. This removes the next transcription's model-load latency, but
keeps the model's RAM/VRAM allocation alive.

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

Why a q4 model can still be around 2 GB:

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

Runtime memory can be higher than these values. For exact values on a target
machine, use Task Manager while running a fixed benchmark and check that the
helper process exits afterward.

## Parameter Counts and Quantization

Parameter-count names such as `1B`, `2B`, or `3B` are approximate marketing and
architecture labels. They do not directly predict download size.

The common estimate is:

`parameter_count * bits_per_parameter / 8`

That estimate only covers quantized weight tensors. Real packages include
metadata, tokenizer files, unquantized tensors, split graph files, external data
files, embeddings, and runtime-specific duplicated structures.

Examples from currently evaluated candidates:

- Granite's "1B Speech" name refers to its base LLM family, but the full speech
  package includes an audio stack and HF metadata reports a larger total model.
- Qwen3-ASR-0.6B community notes report the speech model as roughly 782M-900M
  parameters once audio encoder and LLM pieces are counted.
- Cohere's ONNX package is q4 but still downloads around 2.13 GB because it is a
  multi-part ASR model, not a single flat 4-bit tensor file.

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

This means `Auto` is not identical across local model families. If a transcript
quality issue appears on Cohere, explicitly selecting German or English is the
first thing to test.

## Best Current Practice

For the current Windows Intel GPU test machine:

1. Use `auto` for normal experimental ONNX dictation.
2. Use the Benchmark tab to compare `webgpu`, `dml`, and `cpu`.
3. Treat DirectML failures as provider/operator compatibility issues, not model
   download problems.
4. Enable keep-loaded only if the faster warm latency is worth the RAM/VRAM use.
5. Keep `large-v3-turbo` as the practical fallback until enough real dictation
   samples prove that an ONNX model is better.

## Sources

- MDN WebGPU `requestAdapter()`:
  <https://developer.mozilla.org/en-US/docs/Web/API/GPU/requestAdapter>
- Transformers.js v4 announcement:
  <https://huggingface.co/blog/transformersjs-v4>
- ONNX Runtime execution providers:
  <https://onnxruntime.ai/docs/execution-providers/>
- ONNX Runtime DirectML execution provider:
  <https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html>
