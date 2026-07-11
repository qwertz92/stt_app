# Benchmark: AMD Ryzen 7600X and Intel Arc A750

Date: 2026-07-11 (the stored timestamp is 2026-07-10 23:59 UTC)

This report records the newest complete benchmark retained by the app on this
machine. The values below come from `benchmark_history.json`; they are not
estimates. The 28.10-second speech recording itself is not committed because it
may contain private audio.

## Hardware and runtime

| Component | Value |
| --------- | ----- |
| CPU | AMD Ryzen 5 7600X, 6 cores / 12 logical CPUs |
| GPU | Intel Arc A750 8 GB, driver `32.0.101.8860` |
| RAM | 31.6 GB |
| OS | Windows 11 `10.0.26200` |
| App/source | `0.6.0`, source `335bb8e76c98` |
| Python / Node.js | CPython 3.12.10 / Node.js 24.18.0 |
| faster-whisper / CTranslate2 | 1.2.1 / 4.7.1 |
| Transformers.js / ONNX Runtime Node | 4.1.0 / 1.24.3 |
| ONNX Runtime / ORT GenAI | 1.26.0 / 0.14.1 |

## Settings

| Setting | Value |
| ------- | ----- |
| Audio duration | 28.10 seconds |
| Compute type | `int8` for faster-whisper |
| Runs | 4 measured runs per model |
| Beam size | 5 |
| Language | Auto |
| Warm-up | no |
| VAD | no |
| ONNX target | Auto |

## Results

Lower times and RTF are better. Load is measured separately. Average, standard
deviation, and RTF include all four measured runs.

| Model | Resolved device | Load | Runs | Average | StdDev | RTF |
| ----- | --------------- | ---: | ---- | ------: | -----: | --: |
| `tiny` | `auto` (not resolved by app 0.6.0) | 0.70s | 0.75s, 0.63s, 0.66s, 0.63s | 0.67s | 0.05s | 0.024 |
| `small` | `auto` (not resolved by app 0.6.0) | 0.80s | 3.43s, 3.55s, 3.74s, 3.53s | 3.56s | 0.11s | 0.127 |
| `medium` | `auto` (not resolved by app 0.6.0) | 2.60s | 14.38s, 15.58s, 14.45s, 14.22s | 14.66s | 0.54s | 0.522 |
| `large-v3-turbo` | `auto` (not resolved by app 0.6.0) | 2.96s | 11.58s, 11.51s, 11.74s, 11.64s | 11.62s | 0.09s | 0.414 |
| `cohere-transcribe-03-2026` | `webgpu` | 4.12s | 3.19s, 2.33s, 2.42s, 2.32s | 2.57s | 0.36s | 0.091 |
| `granite-4.0-1b-speech` | `webgpu` | 5.13s | 3.00s, 2.29s, 2.31s, 2.24s | 2.46s | 0.31s | 0.088 |
| `granite-speech-4.1-2b` | `webgpu` | 4.93s | 3.14s, 2.26s, 2.25s, 2.28s | 2.48s | 0.38s | 0.088 |
| `nemotron-3.5-asr-streaming-0.6b-int4` | `cpu` | 1.90s | 6.71s, 6.69s, 6.62s, 6.50s | 6.63s | 0.08s | 0.236 |

The Nemotron run attempted DirectML first, but this ORT GenAI build exposed only
the CPU provider, so the recorded fallback to CPU is expected.

## What the no-warm-up run shows

The model load step is already complete before any timed transcription begins,
but that does not prime every inference path. The first measured WebGPU run was
still slower than the mean of runs 2-4:

| Model | First run | Mean of runs 2-4 | First-run overhead |
| ----- | --------: | ---------------: | -----------------: |
| `cohere-transcribe-03-2026` | 3.19s | 2.36s | 35% |
| `granite-4.0-1b-speech` | 3.00s | 2.28s | 32% |
| `granite-speech-4.1-2b` | 3.14s | 2.27s | 39% |

This is consistent with first-use graph compilation, GPU pipeline creation, and
runtime cache initialization happening during the first inference. A warm-up
performs one complete transcription after loading but excludes it from the
measured runs. Enable warm-up when comparing steady-state throughput; leave it
disabled when measuring what a user experiences on the first transcription
after a model has loaded. Warm-up does not hide or remove the separately
reported model-load time.

## Interpretation

- All successful models were faster than real time on this 28.10-second sample.
- `tiny` had the lowest latency, while the three WebGPU ONNX models completed in
  roughly 2.3 seconds after their first-use overhead had settled.
- `large-v3-turbo` was faster than `medium` on this workload.
- These measurements compare speed, not transcript quality. Use the same real
  speech sample and settings when repeating the run.
- App 0.6.0 retained the configured value `auto` for faster-whisper cases. The
  benchmark runner now records CTranslate2's resolved `cpu` or `cuda` device for
  new results, matching the already resolved ONNX device reporting.
