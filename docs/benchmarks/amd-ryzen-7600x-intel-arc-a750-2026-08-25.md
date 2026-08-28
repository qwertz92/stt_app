# Benchmark: AMD Ryzen 7600X and Intel Arc A750

Date: 2026-08-25 (the stored timestamp is 2026-08-25 22:34 UTC)

This is the run that made `parakeet-tdt-0.6b-v3` the default model and the
one every Parakeet, Canary and Nemotron figure in this repository cites. The
values come from `benchmark_history.json`; they are not estimates. It is also
the first run to measure the onnx-asr models and the first to compare all
three ONNX device targets in one sitting.

The 24.30-second German speech recording is not committed because it
contains private audio, and neither are the decoded transcripts.

## Hardware and runtime

| Component | Value |
| --------- | ----- |
| CPU | AMD Ryzen 5 7600X 6-Core Processor, 12 logical CPUs |
| GPU | Intel(R) Arc(TM) A750 Graphics (driver 32.0.101.8860) |
| RAM | 31.6 GB |
| OS | Windows 11 (10.0.26200) AMD64 |
| App/source | `0.9.0`, source `6f3ff3e9ff73` |
| Python | CPython 3.12.10 64bit |
| faster-whisper / CTranslate2 | 1.2.1 / 4.8.1 |
| Transformers.js / ONNX Runtime Node | 4.1.0 / 1.24.3 |
| ONNX Runtime / ORT GenAI | 1.28.0 / 0.15.2 |
| Nemotron providers | CPU only |

## Settings

| Setting | Value |
| ------- | ----- |
| Audio duration | 24.30 seconds |
| Compute type | `int8` for faster-whisper |
| Runs | 2 measured runs per model |
| Beam size | 5 |
| Language | Auto |
| Warm-up | yes |
| VAD | no |
| ONNX targets | webgpu, dml, cpu |

## Results

Lower times and RTF are better. Load is measured separately. Average and RTF
cover all measured runs of that case.

| Model | Device | Load | Runs | Average | RTF |
| ----- | ------ | ---: | ---- | ------: | --: |
| `tiny` | `cpu` | 0.62s | 0.80s, 0.81s | 0.80s | 0.033 |
| `base` | `cpu` | 0.33s | 1.66s, 1.74s | 1.70s | 0.070 |
| `small` | `cpu` | 0.96s | 3.69s, 3.77s | 3.73s | 0.154 |
| `medium` | `cpu` | 2.50s | 10.09s, 9.68s | 9.89s | 0.407 |
| `large-v3` | `cpu` | 4.45s | 15.28s, 15.01s | 15.15s | 0.623 |
| `large-v3-turbo` | `cpu` | 2.18s | 9.18s, 9.08s | 9.13s | 0.376 |
| `cohere-transcribe-03-2026` | `webgpu` | 3.74s | 2.05s, 1.97s | 2.01s | 0.083 |
| `cohere-transcribe-03-2026` | `cpu` | 2.91s | 3.22s, 3.22s | 3.22s | 0.132 |
| `granite-4.0-1b-speech` | `webgpu` | 4.43s | 2.42s, 2.36s | 2.39s | 0.098 |
| `granite-4.0-1b-speech` | `cpu` | 2.26s | 10.00s, 9.86s | 9.93s | 0.409 |
| `granite-speech-4.1-2b` | `webgpu` | 4.47s | 2.42s, 2.39s | 2.41s | 0.099 |
| `granite-speech-4.1-2b` | `cpu` | 2.33s | 11.23s, 11.13s | 11.18s | 0.460 |
| `granite-speech-4.1-2b-plus` | `cpu` | 7.77s | 101.11s, 100.55s | 100.83s | 4.149 |
| `granite-speech-4.1-2b-nar` | `cpu` | 4.52s | 11.19s, 10.53s | 10.86s | 0.447 |
| `nemotron-3.5-asr-streaming-0.6b-int4` | `cpu` | 1.78s | 5.11s, 5.01s | 5.06s | 0.208 |
| `parakeet-tdt-0.6b-v3` | `cpu` | 1.92s | 1.04s, 1.03s | 1.03s | 0.043 |

## What this run settled

- `parakeet-tdt-0.6b-v3` is the fastest local model here by a wide margin:
  mean RTF 0.043 on plain CPU against 0.154 for `small` on the same recording
  and the same device, which is what made it the default a fresh install uses.
  The per-run values are 0.0428/0.0423 and 0.1520/0.1553; this repository's
  0.042 and 0.152 quote the faster run of each, and the ratio is 3.6x either
  way.
- It is also faster than the quickest GPU model measured (Granite Speech 4.1
  2B at 0.099 on WebGPU), so no GPU path is needed to get the best local
  latency.
- Granite Speech 4.1 Plus and NAR were retired on the strength of these
  numbers: both fall back to CPU, NAR at 0.43-0.46 and Plus at 4.14.
- DirectML is not usable for the Cohere/Granite runtime on this machine;
  every `dml` case failed and the results above show only what ran.

## Cases that did not produce a measurement

| Model | Device | Error |
| ----- | ------ | ----- |
| `cohere-transcribe-03-2026` | `dml` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running MultiHeadAttention node. Name:'/... |
| `granite-4.0-1b-speech` | `dml` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running Reshape node. Name:'node_view' S... |
| `granite-speech-4.1-2b` | `dml` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running Reshape node. Name:'node_view' S... |
| `granite-speech-4.1-2b-plus` | `webgpu` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running Einsum node. Name:'/encoder/laye... |
| `granite-speech-4.1-2b-plus` | `dml` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running FusedMatMul node. Name:'/encoder... |
| `granite-speech-4.1-2b-nar` | `webgpu` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running Einsum node. Name:'/encoder/laye... |
| `granite-speech-4.1-2b-nar` | `dml` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running FusedMatMul node. Name:'/encoder... |
| `canary-1b-v2` | `auto` | Canary cannot detect the language. Choose the language spoken in the sample before benchmarking it; with the wrong on... |

`canary-1b-v2` has no measurement in any run retained on this machine, so no
Canary RTF should be quoted anywhere until one exists.
