# Local Models, Wheels, and Benchmarking

This document explains:
- what Python wheels are (and what they are not),
- which local `faster-whisper` models are supported,
- expected CPU/GPU behavior on Intel notebooks and AMD hardware,
- what "VAD-heavy cases" means in practice,
- how to run local benchmarks with CSV/JSON output,
- and where published error-rate numbers come from.

For curated WER/CER tables focused on models offered in this app, see:
- `docs/model-error-rate-reference.md`

## 1) Wheels: what they do (and what they do not)

A wheel (`.whl`) is a prebuilt Python package.

- It avoids local compilation in most cases.
- It installs faster and more reliably than source builds.
- It still must be installed into a Python environment (`venv`) with `pip install ...`.

Important clarification:
- A wheel is not automatically your full app with all dependencies pre-bundled.
- For "run without installing dependencies first", use an app bundle (e.g. PyInstaller EXE).

## 2) Models currently supported in this app UI

Current settings expose:
- `tiny`
- `base`
- `small` (default)
- `medium`
- `large-v3`

This scope keeps the MVP predictable while still covering clear speed/accuracy tiers.

## 3) Models available in faster-whisper 1.2.1

With `faster-whisper==1.2.1`, supported model IDs include:

- `tiny`, `tiny.en`
- `base`, `base.en`
- `small`, `small.en`
- `medium`, `medium.en`
- `large`, `large-v1`, `large-v2`, `large-v3`
- `large-v3-turbo`, `turbo`
- `distil-small.en`, `distil-medium.en`, `distil-large-v2`, `distil-large-v3`, `distil-large-v3.5`

## 4) Practical model trade-offs

General rule:
- larger model -> better accuracy (especially noisy/multilingual/accented speech),
- but slower runtime and more memory usage.

Approximate repository sizes for models currently exposed in this app:
- `tiny`: ~74.6 MB
- `base`: ~141.0 MB
- `small`: ~463.7 MB
- `medium`: ~1.43 GB
- `large-v3`: ~2.88 GB

These values are best-effort Hub metadata and can vary by revisions/files.

## 5) CPU vs GPU (Intel iGPU, AMD GPU, NVIDIA GPU)

### Intel notebook with integrated GPU

In this stack, most Intel iGPU-only notebooks run inference on CPU.

Why:
- `faster-whisper` uses CTranslate2.
- CTranslate2 prebuilt GPU path is CUDA-focused (NVIDIA).
- Intel iGPU is not the default accelerated backend here.

### Intel Arc (dedicated Intel GPU)

For this project path (`faster-whisper` + CTranslate2 prebuilt wheels), Intel Arc is currently not the standard acceleration path.

- CPU remains the default reliable path in this app.
- Intel GPU offload generally requires alternative backends/toolchains (for example OpenVINO-centric paths), which are outside this MVP.

### AMD GPUs

For this project path (`faster-whisper` + CTranslate2 prebuilt wheels), AMD GPU acceleration is generally not available as the standard path.

- AMD CPUs are fine on CPU inference.
- AMD GPU acceleration would require a different backend/toolchain than this MVP currently uses.

### NVIDIA GPUs

If CUDA-compatible NVIDIA GPU + drivers are available, `device=auto` can use GPU and speed up significantly.

## 6) What are "VAD-heavy cases"?

`VAD` = voice activity detection.  
"VAD-heavy" means audio where start/stop boundaries are difficult:

- lots of short pauses between words,
- background office noise,
- keyboard sounds/clicks,
- breathing/filler sounds,
- multiple speakers or interruptions.

In such cases:
- aggressive VAD can cut off words,
- conservative VAD can keep too much non-speech.

This is why updates like Silero VAD version changes can influence results, even when model size is unchanged.

## 7) Error-rate benchmarks (published references)

There is no single universal "one WER per model" number.  
WER depends heavily on language, dataset, decoding settings, and text normalization.

### 7.1 Whisper paper (OpenAI) - English ASR benchmark table

Source table: *English transcription WER (%) with beam search and temperature fallback*.

Selected rows (multilingual models):
- `tiny`: LibriSpeech test-clean WER 6.7, average across 14 English sets 21.71
- `base`: LibriSpeech test-clean WER 4.9, average across 14 English sets 17.61
- `small`: LibriSpeech test-clean WER 3.3, average across 14 English sets 13.96
- `medium`: LibriSpeech test-clean WER 2.7, average across 14 English sets 12.50
- `large` (not `large-v3`): LibriSpeech test-clean WER 2.8, average across 14 English sets 12.05
- `large-v2`: LibriSpeech test-clean WER 2.5, average across 14 English sets 11.80

Note:
- this table is from the Whisper paper release family (`large` / `large-v2`),
- it does not directly provide a fully equivalent `large-v3` table in the same format.

### 7.2 faster-whisper README - Distil benchmark example

Published GPU benchmark includes:
- `distil-whisper-large-v3` on YT Commons WER: 13.527 (compared to transformers baseline in same table).

## 8) faster-whisper 1.1.0 -> 1.2.1: what changed?

Based on upstream release notes:

- 1.2.0:
  - support for `distil-large-v3.5`,
  - support for loading private HF models,
  - revision-pinned download support,
  - batched transcription fixes/improvements.
- 1.2.1:
  - Silero VAD v6 upgrade,
  - clip timestamp/batched inference fixes,
  - retry logic refinement via HF Hub.

Impact for this app:
- mostly compatibility/stability improvements in the local path,
- potential behavior changes in VAD-sensitive inputs.

## 9) Local benchmark script

Script:
- `scripts/benchmark_local.py`

### 9.1 List available models

```powershell
uv run python scripts/benchmark_local.py --list-models
uv run python scripts/benchmark_local.py --list-models --show-model-sizes
```

### 9.2 Example sample file provided in this repo

- `samples/benchmark_sample.wav`

This sample is synthetic tones to validate pipeline timing mechanics.  
For realistic WER/quality conclusions, benchmark with real speech recordings.

Regenerate the sample file:

```powershell
uv run python scripts/generate_sample_audio.py
```

### 9.3 Benchmark one file (console + JSON + CSV)

```powershell
uv run python scripts/benchmark_local.py .\samples\benchmark_sample.wav --models tiny,base,small --device cpu --compute-types int8,float32 --runs 3 --warmup --json-out .\benchmark\result.json --csv-out .\benchmark\result.csv
```

Output includes:
- summary table (`Load`, `Avg`, `StdDev`, `RTF`, language),
- best-model comparison (`best latency` and `best RTF`),
- optional JSON and CSV artifacts.

Interpretation:
- `RTF = transcription_seconds / audio_seconds`.
- `RTF < 1.0` means faster than real-time.

Summary table columns:
- `Load`: model initialization/load time before measured runs.
- `Avg`: average measured transcription time over `--runs`.
- `StdDev`: variability between measured runs.
- `RTF`: average real-time factor (`transcription_seconds / audio_seconds`).
- `Lang`: detected language from model metadata.
- `Status`: `ok` or `error` per case.

### 9.3.1 Why a 5-second sample can still take long

Benchmark runtime is often dominated by model initialization and download:
- first load can download model weights from Hugging Face,
- larger models (`medium`, `large`, distil variants) can take much longer to load,
- warmup + multiple runs multiplies total runtime.

So short audio duration does not imply short full benchmark runtime.

### 9.3.2 Interrupting long runs

The script now supports isolated case execution by default:
- `--isolated-case` (default) runs each case in a subprocess,
- this improves Ctrl+C responsiveness on Windows.

If you interrupt:
- completed cases are still summarized,
- exit code is `130` for user interrupt.

You can disable isolation with:

```powershell
uv run python scripts/benchmark_local.py .\samples\benchmark_sample.wav --models tiny,base --no-isolated-case
```

### 9.3.3 Parameter reference

- `audio_path`: input audio file to transcribe.
- `--models`: comma-separated model IDs to benchmark.
- `--device`: execution device (`auto`, `cpu`, `cuda`, depending on environment).
- `--compute-types`: numeric precision/backend mode (`int8`, `float32`, `float16`, etc., if supported).
- `--runs`: measured runs per case.
- `--warmup`: run one unmeasured warmup pass before measured runs.
- `--beam-size`: decoding beam size; higher can improve quality but increases runtime.
- `--language`: force language code (e.g. `de`, `en`) instead of auto detect.
- `--vad-filter`: enable built-in VAD filtering for transcription.
- `--threads`: CPU thread count for CTranslate2 (`0` = default backend behavior).
- `--json-out`: write machine-readable full results.
- `--csv-out`: write flat run+summary table for Excel/BI analysis.
- `--no-best`: disable "best model by latency/RTF" console section.
- `--isolated-case` / `--no-isolated-case`: enable/disable per-case subprocess isolation.
- `--list-models`: list model IDs and exit.
- `--show-model-sizes`: add best-effort repository size lookup to model list.

### 9.4 Recommended first baseline for Intel laptop CPUs

```powershell
uv run python scripts/benchmark_local.py .\samples\benchmark_sample.wav --models tiny,base,small --device cpu --compute-types int8 --runs 3 --warmup --csv-out .\benchmark\cpu-int8.csv
```

Then compare:

```powershell
uv run python scripts/benchmark_local.py .\samples\benchmark_sample.wav --models small,medium --device cpu --compute-types int8,float32 --runs 2 --csv-out .\benchmark\cpu-compare.csv
```

## Sources

- faster-whisper repository and benchmarks:
  - https://github.com/SYSTRAN/faster-whisper
  - https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/README.md
- faster-whisper release notes:
  - https://github.com/SYSTRAN/faster-whisper/releases/tag/v1.2.0
  - https://github.com/SYSTRAN/faster-whisper/releases/tag/v1.2.1
- Whisper paper source archive (tables):
  - https://arxiv.org/e-print/2212.04356
- Whisper repository README/model card pointers:
  - https://github.com/openai/whisper
  - https://raw.githubusercontent.com/openai/whisper/main/model-card.md
- CTranslate2 hardware support:
  - https://opennmt.net/CTranslate2/hardware_support.html
- OpenVINO supported devices (for alternative Intel GPU-focused stacks):
  - https://docs.openvino.ai/2025/about-openvino/release-notes-openvino/system-requirements.html
