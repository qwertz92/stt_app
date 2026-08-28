# Models & Offline Setup

Everything about model choices, downloading, and configuring models for offline or corporate use.

## Available models

The app has four local runtime families:

- **GPU-accelerated ONNX models** — Cohere Transcribe and IBM Granite Speech, run
  on the GPU through WebGPU via a Node.js helper. These are the highest-accuracy
  local models and, on a machine with a working GPU, usually more accurate than
  the Whisper models. Batch mode only.
- **NVIDIA Nemotron 3.5** (int4, ONNX Runtime GenAI) — the local true cache-aware
  streaming model; also supports batch.
- **onnx-asr models** (Parakeet TDT, Canary) — pure Python, CPU only, no Node.js
  and no GPU. Parakeet is the fastest local model in this app that
  transcribed the benchmark recording correctly: RTF 0.043 on a Ryzen 5 7600X,
  second only to Whisper `tiny` (0.033), which is the weakest of the models
  that did. Batch mode only.
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** (CTranslate2) —
  CPU-based, no extra setup, the broad-compatibility baseline; also supports the
  experimental rolling-window streaming mode.

Granite Speech 4.1 2B (the base autoregressive model) runs as a q4
Transformers.js ONNX package on the same WebGPU pipeline path as Granite 4.0, and
currently tops the [Open ASR Leaderboard](https://huggingface.co/spaces/hf-audio/open_asr_leaderboard)
for accuracy. The Plus and NAR variants were **retired on 2026-08-26**: their
exports cannot use any GPU here, they measured 4.5x and 42x slower than the base
4.1 2B on WebGPU, and their transcripts were unusable. See
[Granite Speech 4.1 ONNX variants](granite-speech-4.1-onnx-variants.md) for the
measurements and what upstream would have to change. Granite 4.0 q4 remains as a smaller GPU
fallback and may be retired once Granite 4.1 is established on real hardware,
since 4.1 supersedes it on accuracy.

For deeper background on WebGPU, DirectML, CPU fallback, memory behavior, and
language handling, see [Local ONNX Runtime Guide](local-onnx-runtime.md).

| Model | Runtime | Size | Languages | Best for |
|-------|---------|------|-----------|----------|
| `tiny` | CTranslate2 | ~78 MB | Multilingual | Quick testing, fallback |
| `base` | CTranslate2 | ~148 MB | Multilingual | Light usage |
| `small` | CTranslate2 | ~486 MB | Multilingual | Good balance for German + English |
| `medium` | CTranslate2 | ~1.53 GB | Multilingual | Better quality, slower |
| `large-v3` | CTranslate2 | ~3.09 GB | Multilingual | Best Whisper quality (NVIDIA GPU recommended) |
| `large-v3-turbo` | CTranslate2 | ~1.62 GB | Multilingual | Fast + high quality — pruned version of large-v3 |
| `distil-large-v3.5` | CTranslate2 | ~1.52 GB | **English only** | Fastest high-quality English transcription |
| `cohere-transcribe-03-2026` | ONNX/WebGPU | ~2.13 GB q4 | 14 explicit languages; no Auto | High-quality local ASR, batch mode only |
| `granite-4.0-1b-speech` | ONNX/WebGPU | ~1.84 GB q4 | Auto + `de/en/fr/es/pt/ja` | Smaller GPU fallback (q4), batch mode only |
| `granite-speech-4.1-2b` | ONNX/WebGPU | ~1.84 GB q4 | Auto + `de/en/fr/es/pt/ja` | **Top accuracy** — Open ASR Leaderboard #1 (q4, WebGPU), batch mode only |
| `nemotron-3.5-asr-streaming-0.6b-int4` | ORT GenAI INT4 | ~793 MB | Auto + 28 transcription-ready/broad-coverage languages | True cache-aware local streaming at fixed 560 ms chunks |
| `parakeet-tdt-0.6b-v3` | onnx-asr INT8 (CPU) | ~670 MB | Auto (multilingual, no selection needed) | **Fastest accurate local model** — RTF 0.043 on CPU, no GPU or Node.js needed, batch mode only |
| `canary-1b-v2` | onnx-asr INT8 (CPU) | ~1.03 GB | 25 explicit languages; **no Auto** | Higher published German accuracy than Parakeet; slower, though no run on this machine has measured it, batch mode only |

### Which model should I use?

Accuracy and speed no longer point at the same model. For **accuracy** with a GPU
and Node.js, start with the GPU/ONNX models. For **speed**, `parakeet-tdt-0.6b-v3`
needs neither: it measured RTF 0.043 on a Ryzen 5 7600X CPU, which beats every
GPU model in the same run on its GPU. Whisper `tiny` is quicker still (0.033)
and is the only local model that is, but it is also the weakest of the models
that transcribed the recording, which is why the default is Parakeet. The Whisper models remain a
solid, zero-setup CPU baseline with the broadest language coverage. The surest way
to choose is to run the [benchmark](advanced-setup.md#benchmarking) on your own
hardware.

| Situation | Recommendation |
|-----------|---------------|
| Best accuracy (tops the Open ASR Leaderboard) | `granite-speech-4.1-2b` (GPU) |
| High accuracy, fastest on GPU | `cohere-transcribe-03-2026` (GPU) |
| Lowest-latency live streaming | `nemotron-3.5-asr-streaming-0.6b-int4` |
| Zero setup: fastest accurate local transcription, no GPU and no Node.js | `parakeet-tdt-0.6b-v3` (default, CPU) |
| Whisper on CPU, German + English, supports streaming | `small` |
| Better Whisper quality on CPU | `large-v3-turbo` |
| English only, maximum speed | `distil-large-v3.5` |
| Smaller GPU model / Granite 4.0 fallback | `granite-4.0-1b-speech` |
| Testing / very limited resources | `tiny` |

> **Real-time factor (RTF)** measures speed: processing time ÷ audio length.
> RTF 0.1 means a 10-second clip transcribes in ~1 second — lower is faster, and
> anything below 1.0 is faster than real time. In the most recent run on the
> tested Ryzen 7600X + Arc A750
> ([2026-08-25](benchmarks/amd-ryzen-7600x-intel-arc-a750-2026-08-25.md)),
> `granite-4.0-1b-speech` measured RTF 0.098 on WebGPU against 0.409 on CPU and
> `cohere-transcribe-03-2026` 0.083 on WebGPU — both faster than `small` (0.154)
> or `large-v3-turbo` (0.376). See
> [Local Benchmark Results](benchmarks/README.md).

### Accuracy reference (Word Error Rate)

Lower is better. These are published benchmark values — your results depend on microphone, accent, and environment.

**German (FLEURS benchmark):**

| Model | WER (%) |
|-------|--------:|
| tiny | 27.8 |
| base | 17.9 |
| small | 10.2 |
| medium | 6.5 |
| large-v3 | ~4.5 (est. from large-v2) |
| large-v3-turbo | similar to large-v3 |

**English (LibriSpeech clean):**

| Model | WER (%) |
|-------|--------:|
| tiny | 6.7 |
| base | 4.9 |
| small | 3.3 |
| medium | 2.7 |
| large-v3 | ~2.5 (est. from large-v2) |
| large-v3-turbo | ~2.5 |
| distil-large-v3.5 | ~2.5 |

Sources: [Whisper paper](https://arxiv.org/abs/2212.04356), [faster-whisper benchmarks](https://github.com/SYSTRAN/faster-whisper).

**GPU/ONNX models (Open ASR Leaderboard):** Cohere and Granite are newer
Conformer-encoder + LLM-decoder systems and generally beat the older Whisper
models on the public
[Open ASR Leaderboard](https://huggingface.co/spaces/hf-audio/open_asr_leaderboard).
`granite-speech-4.1-2b` currently tops it (~5.3% mean English WER). Real-world and
German quality still depend on your microphone and audio, so benchmark on your own
samples before changing the default.

### ONNX local models

Cohere Transcribe and IBM Granite Speech are selectable under the normal local
model list, but they are not CTranslate2 models. They use a separate Node.js
ONNX runtime and run in **batch mode only**. Cohere, Granite 4.0, and Granite
4.1 2B all use Transformers.js q4 ONNX packages loaded through the high-level
`GraniteSpeechForConditionalGeneration` pipeline -- there is exactly one ONNX
path here since the raw-graph variants were retired. The helper process is kept
alive only while a transcription or benchmark case is active, so the app does
not keep a large ONNX runtime idling after normal dictation.

Granite 4.1 2B uses the q4 package
[`onnx-community/granite-speech-4.1-2b-ONNX`](https://huggingface.co/onnx-community/granite-speech-4.1-2b-ONNX),
which has the exact same component layout as Granite 4.0
(`audio_encoder` / `embed_tokens` / `decoder_model_merged`). On 2026-06-17 it was
verified on an Intel Arc A750 to load on **WebGPU** (no `Einsum` shader failure)
and transcribe German, English, and French correctly, at roughly 0.13–0.19
real-time factor in that first check — materially faster than the raw CPU
path. That figure is superseded: the 2026-08-25 benchmark measured the same
model at mean RTF 0.099 on WebGPU. Do not compare the 0.13–0.19 against the
0.098 quoted for Granite 4.0 above; they come from different sittings, and in
the one run that measured both the two are within 0.001 of each other.

Community GGUF builds
([2B Q4_K](https://huggingface.co/cstr/granite-speech-4.1-2b-GGUF), and others)
exist for the CrispASR/GGUF runtime and cannot be loaded by the app's ONNX
paths.

### Nemotron 3.5 cache-aware streaming

`nemotron-3.5-asr-streaming-0.6b-int4` is a separate local runtime path, not a
Node/WebGPU or faster-whisper model. It uses Microsoft's ONNX Runtime GenAI
`StreamingProcessor` and preserves the model's encoder/RNNT state between new
audio chunks. That avoids the repeated rolling-window transcription used by the
current faster-whisper streaming implementation.

The published multilingual INT4 ONNX export is approximately 793 MB and is
optimized for a fixed 560 ms chunk. NVIDIA's original NeMo model supports
80/160/320/560/1120 ms profiles, but changing the app setting cannot change the
fixed graph contract. Additional latency choices should be exposed only when
compatible ONNX exports exist.

The installable app dependency currently provides CPU execution. The runtime
tries DirectML first and falls back to CPU, but as of June 8, 2026 Microsoft's
`onnxruntime-genai-directml` package depends on an
`onnxruntime-directml>=1.26.0` wheel that is not yet published on PyPI. On the
test Ryzen 5 7600X, two runs measured:

- cold model load: 1.78 s (2026-08-25) and 1.90 s (2026-07-10),
- transcription RTF on CPU: 0.21 on a 24.3 s recording, 0.24 on a 28.1 s one,
- automatic language mode and the DML-to-CPU fallback both loaded correctly.

That CPU result is comfortably faster than real time on the test desktop, but
laptop performance and German dictation quality still need real user samples.

### Language selection

Settings -> General rebuilds the language list for the selected engine and
model. Auto is selected by default when supported. The app keeps one canonical
language code and adapts it where a provider requires a different code format,
such as ElevenLabs Scribe's three-letter codes.

The overlay uses the same model-aware list in its `Lang` button. A selection is
saved immediately and applies to the next recording; the button is disabled
while a recording or transcription is active.

The model-aware lists are based on the current primary documentation:

- [OpenAI speech-to-text languages](https://platform.openai.com/docs/guides/speech-to-text#supported-languages)
- [Groq speech-to-text](https://console.groq.com/docs/speech-to-text)
- [Deepgram model/language overview](https://developers.deepgram.com/docs/models-languages-overview)
- [AssemblyAI models](https://www.assemblyai.com/docs/getting-started/models)
- [ElevenLabs Scribe](https://elevenlabs.io/docs/models#scribe-v2)
- [Cohere Transcribe model card](https://huggingface.co/CohereLabs/transcribe-03-2026)
- [Granite Speech 4.1 model card](https://huggingface.co/ibm-granite/granite-speech-4.1-2b)
- [Nemotron 3.5 INT4 ONNX model card](https://huggingface.co/onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4)
- [Official ORT GenAI Nemotron example and language-ID mapping](https://github.com/microsoft/onnxruntime-genai/blob/main/examples/python/nemotron_speech.py)

The runtime automatically tries an ONNX GPU target first and falls back to CPU
if no compatible GPU runtime loads. Cohere, Granite 4.0, and Granite 4.1 2B try
WebGPU, then DirectML on Windows, then CPU. The app shows a red warning under
the model selector because pure CPU fallback can be much slower than the
CTranslate2 Whisper models.

The app also falls back from DirectML/WebGPU to CPU during transcription when a
model loads on a GPU runtime but the first generation call fails because an ONNX
operator is not supported by that provider. Benchmark `GPU only` may move
between WebGPU and DirectML, but intentionally does not use CPU fallback, so GPU
provider failures remain visible. Benchmark summaries and exports retain the
concise fallback reason. On the tested Intel Arc A750, Cohere, Granite 4.0 and
Granite 4.1 2B use the Transformers.js pipeline graph and work on WebGPU.

Node.js cannot decode arbitrary audio files through `AudioContext`. The ONNX
runner decodes WAV input itself and passes Float32 audio directly to
Transformers.js. Use the app's last recording or another WAV file when
benchmarking Cohere/Granite.

Long audio is handled conservatively. Cohere uses the Transformers.js Cohere
ASR pipeline, which chunks long audio internally. Granite is processed through a
model-specific path, so the app chunks Granite audio at quiet boundaries with a
maximum chunk size of 30 seconds before generation. This keeps long recordings
from being sent as one very large prompt/audio feature block, but it is still
not a replacement for a dedicated long-form transcription pipeline.

Unlike faster-whisper and Nemotron, Cohere and Granite are not preloaded when
the app starts. This avoids expensive background CPU model loading before the
user actually starts a local ONNX transcription. The Local tab has an
expert setting to keep the last Cohere or Granite ONNX model loaded after
dictation when warm latency matters more than RAM/VRAM use.

`wasm` is not a valid device in the Transformers.js Node runtime used by this
app. It appears in the browser/web ONNX bundle, but the app process uses the
Node ONNX runtime where the practical targets are DirectML, WebGPU, and CPU.

NVIDIA Parakeet ships as `parakeet-tdt-0.6b-v3` through the pure-Python
`onnx-asr` runtime (CPU only). What remains unimplemented is the *official
NeMo/PyTorch path*, which would add a second heavyweight Python ML runtime and
does not solve the Intel-GPU requirement cleanly. See
[Local ASR Model Candidates - 2026 Re-evaluation](local-asr-model-candidates-2026.md).

### CPU vs GPU

The CTranslate2/faster-whisper runtime works on **CPU** (default) and
**NVIDIA GPU** (if CUDA is available).

- **Intel/AMD CPU**: works out of the box. Most users run on CPU.
- **NVIDIA GPU**: much faster. Set device to `auto` or `cuda` in the benchmark script.
- **Intel iGPU / AMD GPU**: not supported by the CTranslate2 backend. Use CPU.

The ONNX/WebGPU runtime is designed to be vendor-neutral when
WebGPU or DirectML is available, so Intel, AMD, and NVIDIA GPUs are all valid
targets. If neither GPU runtime can be selected by the JavaScript runtime, the
model uses CPU and will likely be slower than `large-v3-turbo`.

**Choosing the device yourself.** Settings → General → **ONNX Device** applies to
the local ONNX models (Cohere, Granite, Nemotron). It offers the same choices as
the benchmark, so a device that proves faster there can be selected for everyday
dictation:

| Choice | Meaning |
|--------|---------|
| `Auto` | WebGPU, then DirectML, then CPU (the default) |
| `GPU only` | WebGPU then DirectML; fails rather than falling back to CPU |
| `WebGPU only` / `DirectML only` | Pin one backend |
| `CPU only` | Never try the GPU |

Nemotron runs on ONNX Runtime GenAI, which has DirectML and CPU
only, so every GPU choice means DirectML for it. Parakeet and Canary run through
`onnx-asr` on CPU and ignore the setting entirely; the row is disabled while one
of them is selected.

Nemotron currently ships with the reproducibly installable CPU ORT GenAI
runtime. Its DirectML path is already attempted by the app, but cannot be part
of the normal dependency lock until Microsoft publishes the matching DirectML
runtime wheel.

---

## First-time model download

On first use, the app downloads the selected model automatically from HuggingFace Hub. The default `parakeet-tdt-0.6b-v3` (~670 MB) takes a couple of minutes; `small` (~486 MB) takes about a minute. After that, it loads from cache in seconds.

The model is stored in the HuggingFace cache (`%USERPROFILE%\.cache\huggingface\hub\` on Windows) and persists across restarts, reboots, and updates.

The Settings **Local** tab downloads models one at a time. You can select and
queue more models while the current download continues. The active and queued
models are marked in the list, and the tab shows approximate percentage,
downloaded size, MB/s, and Mbit/s. Percentage and speed are estimated from
on-disk cache growth. The speed uses a short rolling window so bursty cache
writes do not immediately display `0.0`, but short pauses or jumps can still
occur while Hugging Face finalizes files.

**Cancel Downloads** stops the active worker and clears the remaining queue.
The app removes unusable `*.incomplete` files left by the interrupted transfer
but preserves files that finished downloading so a later retry can reuse them.
The command-line download script applies the same cleanup after `Ctrl+C`.

For Cohere and Granite, source checkouts use the system Node.js executable. If
`@huggingface/transformers` is missing, the app attempts `npm install`
automatically from the repository root on first ONNX use. That is the only
declared JavaScript dependency; `onnxruntime-node` comes with it. The packaged Windows
release includes the JavaScript dependency tree when `node_modules` is present
at build time, but it still needs a Node.js executable unless the distribution
bundle adds one separately. Set `STT_APP_NODE_PATH` if Node.js is installed in a
non-standard location.

If the machine-wide Node.js installer is blocked by corporate policy, use the
no-admin portable install: run `python scripts/setup_node_windows.py` (Windows
Python), which downloads the portable Node.js ZIP and sets `STT_APP_NODE_PATH`
for you. See
[Advanced Setup → Node.js for the GPU/ONNX models](advanced-setup.md#nodejs-for-the-gpuonnx-models-no-admin-install).

---

## Automatic ModelScope fallback

If a Hugging Face download fails for any reason, the app and the download script
**automatically retry against the [ModelScope](https://modelscope.cn) mirror**
(Alibaba's model hub) -- for every model except the three listed below, which
are not mirrored there and have Hugging Face as their only source. ModelScope mirrors the same repository IDs
(`onnx-community/…`, `Systran/…`, etc.) and serves the large LFS weights from its
own CDN instead of redirecting back to Hugging Face, so it usually works even
when a corporate proxy blocks Hugging Face wholesale under a "Generative AI and
ML Applications" category rule (see
[SSL / proxy issues → Category block](advanced-setup.md#category-block-hugging-face-fully-blocked-not-an-ssl-error)).

- No setup is required; the fallback is transparent and lands the files in the
  same cache location a Hugging Face download would use.
- Disable it with the environment variable `STT_APP_DISABLE_MODELSCOPE=1`.
- Override the mirror host with `STT_APP_MODELSCOPE_ENDPOINT` if needed.
- Some repositories are **not** mirrored on ModelScope. Verified against the
  ModelScope API on 2026-08-18:

  | Model | Upstream repo |
  |-------|---------------|
  | `distil-large-v3.5` | `distil-whisper/distil-large-v3.5-ct2` |
  | `parakeet-tdt-0.6b-v3` | `istupakov/parakeet-tdt-0.6b-v3-onnx` |
  | `canary-1b-v2` | `istupakov/canary-1b-v2-onnx` |

  On a network that blocks Hugging Face wholesale these models cannot be
  fetched at all, and the app says so instead of blaming the connection. The
  list lives in `config.MODELS_WITHOUT_MODELSCOPE_MIRROR`. Download them on an
  unrestricted machine and point **Model Dir** at the result, or have IT allow
  `huggingface.co`, `hf.co` and `cas-bridge.xethub.hf.co`.

## Offline download

If the app cannot reach HuggingFace Hub (corporate firewall, air-gapped network, SSL/proxy issues), download models in advance on a machine with internet access.

### Method 1: Download script (recommended)

```powershell
# Download the default model (parakeet-tdt-0.6b-v3):
uv run python scripts/download_model.py

# Download a specific model:
uv run python scripts/download_model.py --model large-v3-turbo

# Download a GPU ONNX/WebGPU model:
uv run python scripts/download_model.py --model cohere-transcribe-03-2026

# Download true-streaming Nemotron INT4:
uv run python scripts/download_model.py --model nemotron-3.5-asr-streaming-0.6b-int4

# Download into a custom directory (USB stick, network share):
uv run python scripts/download_model.py --model small --output-dir C:\whisper-models

# Download all models:
uv run python scripts/download_model.py --all

# List available models:
uv run python scripts/download_model.py --list
```

<details>
<summary>Without uv (inside an activated venv)</summary>

```powershell
# Create and activate a venv
...\stt_app> python -m venv .venv
...\stt_app> .\.venv\Scripts\Activate.ps1
# Download the model (small in this case)
python scripts/download_model.py --model small
```

</details>

### Method 2: Git clone

If git traffic is allowed through your proxy:

> **Important:** You must have [Git LFS](https://git-lfs.com/) installed **before** cloning.
> Without it, `git clone` downloads only tiny LFS pointer files (~130 bytes) instead of the actual
> model weights. The app will fail with `Unsupported model binary version` errors.
>
> **`git lfs install` is NOT a built-in Git command** — you must install the
> `git-lfs` package first via your system package manager:
>
> **Ubuntu / Debian:**
> ```bash
> sudo apt install git-lfs
> git lfs install      # one-time per-user hook setup
> ```
>
> **Windows (winget):**
> ```powershell
> winget install GitHub.GitLFS
> git lfs install      # one-time per-user hook setup
> ```
>
> **Windows (manual):** Download from https://git-lfs.com/ and run the installer,
> then run `git lfs install` in a terminal.
>
> **macOS (Homebrew):**
> ```bash
> brew install git-lfs
> git lfs install
> ```
>
> If you already cloned without git-lfs, run `git lfs pull` inside the cloned folder to fetch
> the actual model files.

```bash
git lfs install           # one-time setup (skip if already done)
git clone https://huggingface.co/Systran/faster-whisper-small
```

Then import CTranslate2/faster-whisper models into the app's cache structure:

```powershell
uv run python scripts/import_model.py C:\Downloads\faster-whisper-small
```

The import script is intentionally CTranslate2-only. For Cohere and Granite,
use `scripts/download_model.py`; it downloads only the ONNX precision tier
required by the app (q4 for Cohere, Granite 4.0, and Granite 4.1 2B; INT8 for
Parakeet and Canary) and stores it in a real local folder to avoid Windows
symlink privilege errors.

<details>
<summary>All model repositories</summary>

```bash
git lfs install           # one-time setup (skip if already done)
git clone https://huggingface.co/Systran/faster-whisper-tiny
git clone https://huggingface.co/Systran/faster-whisper-base
git clone https://huggingface.co/Systran/faster-whisper-small
git clone https://huggingface.co/Systran/faster-whisper-medium
git clone https://huggingface.co/Systran/faster-whisper-large-v3
git clone https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo
git clone https://huggingface.co/distil-whisper/distil-large-v3.5-ct2
git clone https://huggingface.co/onnx-community/cohere-transcribe-03-2026-ONNX
git clone https://huggingface.co/onnx-community/granite-4.0-1b-speech-ONNX
git clone https://huggingface.co/onnx-community/granite-speech-4.1-2b-ONNX
git clone https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx
git clone https://huggingface.co/istupakov/canary-1b-v2-onnx
git clone https://huggingface.co/onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4
```

The two `istupakov` repositories are the onnx-asr models, and
`parakeet-tdt-0.6b-v3` is the app's
default. They matter most here: together with `distil-large-v3.5` they are the
three models with no ModelScope mirror, so on a network that blocks Hugging
Face a clone from a machine that can reach it is the only route.

</details>

### Method 3: Manual browser download

Manual browser import is supported for CTranslate2/faster-whisper models only.
Download these files from the HuggingFace model page: `config.json`, `model.bin`,
`tokenizer.json`, `vocabulary.txt` (or `vocabulary.json`).

| Model | Download page |
|-------|---------------|
| tiny | [Systran/faster-whisper-tiny](https://huggingface.co/Systran/faster-whisper-tiny/tree/main) |
| base | [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base/tree/main) |
| small | [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small/tree/main) |
| medium | [Systran/faster-whisper-medium](https://huggingface.co/Systran/faster-whisper-medium/tree/main) |
| large-v3 | [Systran/faster-whisper-large-v3](https://huggingface.co/Systran/faster-whisper-large-v3/tree/main) |
| large-v3-turbo | [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/tree/main) |
| distil-large-v3.5 | [distil-whisper/distil-large-v3.5-ct2](https://huggingface.co/distil-whisper/distil-large-v3.5-ct2/tree/main) |

Place the files in a folder (e.g. `C:\Downloads\faster-whisper-small\`) and run the import script:

```powershell
uv run python scripts/import_model.py C:\Downloads\faster-whisper-small
# or if uv doesn't work
.\.venv\Scripts\Activate.ps1
python.exe .\scripts\import_model.py C:\Downloads\faster-whisper-small
```

### Transfer to target machine

**If you used `--output-dir`:** Copy the entire directory to the target machine and set **Model Dir** in Settings to that path.

**If you used the default cache:** Copy `%USERPROFILE%\.cache\huggingface\` to the same location on the target machine. The app finds models there automatically.

---

## Configure the app for offline use

After transferring model files to the target machine:

1. Open **Settings** (right-click tray icon → Settings).
2. Check **Offline mode** — prevents any network access.
3. Set **Model Dir** (only if you used a custom directory, not the default cache).

Alternatively, set an environment variable before launching:

```powershell
$env:HF_HUB_OFFLINE = "1"
python main.py
```

---

## How model loading works (technical)

When you select e.g. `small` in Settings, faster-whisper resolves the model in this order:

1. **Is the model name a directory path?** → If `model_size_or_path` points to an existing folder on disk (e.g. `C:\models\faster-whisper-small\`), it uses that folder directly.

2. **Is the model in the cache?** → The short name (`small`) is mapped to a HuggingFace repo ID (`Systran/faster-whisper-small`). faster-whisper checks the HuggingFace cache (or the configured Model Dir) for a downloaded snapshot. If found → loads from cache, no internet needed.

3. **Download from HuggingFace Hub** → If no cache hit, the model is downloaded and cached. This only happens once per model.

**Fallback behavior:** If the selected model cannot be loaded (download fails, file missing), the app falls back to any locally cached model (preferring `tiny` as last resort) and shows a warning.

### HuggingFace cache structure

The cache uses HuggingFace's internal directory format, not flat files:

```
%USERPROFILE%\.cache\huggingface\hub\
  models--Systran--faster-whisper-small\
    refs\main                              ← commit hash reference
    snapshots\abc123...\                   ← actual model files
      config.json
      model.bin
      tokenizer.json
      vocabulary.txt
```

This is why you cannot just drop files into a folder — the download script and import script handle this structure automatically.

### Reclaiming disk from retired models

The Local tab lists and deletes only models the app currently offers, so when a
model is retired its downloaded snapshot stays on disk and becomes invisible to
that list. Granite Speech 4.1 **Plus** and **NAR** were retired on 2026-08-26
and are the only case so far; if you ever downloaded them, their caches are
still there and can be deleted by hand. Measured on the development machine:

| directory (under `%USERPROFILE%\.cache\huggingface\hub`) | size |
| --- | --- |
| `ibm-granite-speech-4.1-2b-plus-onnx` | 3.8 GB |
| `ibm-granite-speech-4.1-2b-nar-onnx` | 2.4 GB |
| `granite-speech-4.1-2b-plus-ONNX` | 1.8 GB |
| `models--smcleod--ibm-granite-speech-4.1-2b-nar-onnx` | 1.0 GB |
| `models--smcleod--ibm-granite-speech-4.1-2b-plus-onnx` | 8 MB |
| `models--valoomba--granite-speech-4.1-2b-plus-ONNX` | negligible |

Delete them only if you are not keeping them for your own experiments; the app
will never use them again. If you set a custom **Model Dir**, look there
instead. Nothing else in the cache is orphaned — every other directory belongs
to a model the app still offers.

### Custom Model Dir

Setting **Model Dir** (e.g. `D:\whisper-models`) causes all model downloads and cache lookups to use that directory instead of the default HuggingFace cache. The same internal structure is created there.

Useful for: USB transfer, network share, keeping models separate from user profile.

### onnx-asr models (Parakeet, Canary)

`parakeet-tdt-0.6b-v3` and `canary-1b-v2` run through
[`onnx-asr`](https://github.com/istupakov/onnx-asr), a pure-Python runtime. Unlike
the Cohere/Granite models they need **no Node.js**, and unlike Nemotron they need
no extra ONNX Runtime — they reuse the one the app already ships.

Both are **CPU only and batch only**. That is not a limitation in practice:
measured on a Ryzen 5 7600X, Parakeet transcribes a 24.3-second recording in
about 1.03 s (RTF 0.043), which is faster than any GPU model in this app.

The ONNX Device setting does not apply to them and is disabled while one is
selected. A DirectML build of ONNX Runtime would be roughly twice as fast again,
but installing it overwrites the ONNX Runtime that Nemotron depends on and breaks
that engine, so the app deliberately does not ship it.

**Language selection differs between the two, and it matters:**

- **Parakeet** is implicitly multilingual and ignores any language you give it,
  so it offers only `Auto`.
- **Canary** has no automatic detection. Left to itself it would *translate* into
  English instead of transcribing, so the app requires you to pick one of its 25
  trained languages and never offers `Auto`.

Parakeet's TDT decoder can return no text for a short but real utterance
(about one to two seconds). That is a model miss, not the silence gate: the
overlay shows an Error with Retry, and History → Use last recording can send
the same clip to another model. Do not pad the audio as a workaround — extra
silence can drop words or invent new ones.
