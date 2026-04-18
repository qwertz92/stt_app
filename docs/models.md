# Models & Offline Setup

Everything about model choices, downloading, and configuring models for offline or corporate use.

## Available models

The app has two local runtime families:

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) models in CTranslate2 format.
- Experimental q4 ONNX/WebGPU models through `@huggingface/transformers`.

For deeper background on WebGPU, DirectML, CPU fallback, memory behavior, and
language handling, see [Local ONNX Runtime Guide](local-onnx-runtime.md).

| Model | Runtime | Size | Languages | Best for |
|-------|---------|------|-----------|----------|
| `tiny` | CTranslate2 | ~75 MB | Multilingual | Quick testing, fallback |
| `base` | CTranslate2 | ~141 MB | Multilingual | Light usage |
| `small` | CTranslate2 | ~484 MB | Multilingual | **Default — good balance for German + English** |
| `medium` | CTranslate2 | ~1.4 GB | Multilingual | Better quality, slower |
| `large-v3` | CTranslate2 | ~3.1 GB | Multilingual | Best Whisper quality (NVIDIA GPU recommended) |
| `large-v3-turbo` | CTranslate2 | ~809 MB | Multilingual | Fast + high quality — pruned version of large-v3 |
| `distil-large-v3.5` | CTranslate2 | ~756 MB | **English only** | Fastest high-quality English transcription |
| `cohere-transcribe-03-2026` | ONNX/WebGPU | ~2.13 GB q4 | Multilingual, explicit `de`/`en` in the app | Experimental quality trial, batch mode only |
| `granite-4.0-1b-speech` | ONNX/WebGPU | ~1.84 GB q4 | Multilingual, explicit `de`/`en` in the app | Experimental compact speech-LM trial, batch mode only |

### Which model should I use?

| Situation | Recommendation |
|-----------|---------------|
| German + English, normal laptop | `small` (default) |
| Better quality, still reasonable speed | `large-v3-turbo` |
| Best possible quality, NVIDIA GPU available | `large-v3` |
| English only, maximum speed | `distil-large-v3.5` |
| Highest experimental local ASR quality trial | `cohere-transcribe-03-2026` |
| Speech-LM comparison trial | `granite-4.0-1b-speech` |
| Testing / very limited resources | `tiny` |

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

### Experimental ONNX/WebGPU local models

Cohere Transcribe and IBM Granite Speech are selectable under the normal local
model list, but they are not CTranslate2 models. They use a separate
Transformers.js q4 ONNX runtime and run in **batch mode only**. The helper
process is kept alive only while a transcription or benchmark case is active, so
the app does not keep a large ONNX runtime idling after normal dictation.

The runtime automatically tries WebGPU first, then DirectML on Windows, and
falls back to CPU if no compatible GPU runtime loads. It attempts WebGPU even
when Node's `navigator.gpu` probe is unavailable, because the Transformers.js
runtime can still expose a working WebGPU backend on some machines. The app
shows a red warning under the model selector because pure CPU fallback can be
much slower than the CTranslate2 Whisper models.

The app also falls back from DirectML/WebGPU to CPU during transcription when a
model loads on a GPU runtime but the first generation call fails because an ONNX
operator is not supported by that provider. Benchmark `GPU only` may move
between WebGPU and DirectML, but intentionally does not use CPU fallback, so GPU
provider failures remain visible.

Node.js cannot decode arbitrary audio files through `AudioContext`. The ONNX
runner decodes WAV input itself and passes Float32 audio directly to
Transformers.js. Use the app's last recording or another WAV file when
benchmarking Cohere/Granite.

Unlike faster-whisper models, Cohere and Granite are not preloaded when the app
starts. This avoids expensive background CPU model loading before the user
actually starts an experimental transcription. The Local tab has an expert
setting to keep the last experimental ONNX model loaded after dictation when
warm latency matters more than RAM/VRAM use.

`wasm` is not a valid device in the Transformers.js Node runtime used by this
app. It appears in the browser/web ONNX bundle, but the app process uses the
Node ONNX runtime where the practical targets are DirectML, WebGPU, and CPU.

NVIDIA Parakeet is still not implemented. Its best-supported local path is the
NeMo/PyTorch stack, which would add a second heavyweight Python ML runtime and
does not solve the Intel-GPU requirement cleanly. See
[Local ASR Model Candidates - 2026 Re-evaluation](local-asr-model-candidates-2026.md).

### CPU vs GPU

The CTranslate2/faster-whisper runtime works on **CPU** (default) and
**NVIDIA GPU** (if CUDA is available).

- **Intel/AMD CPU**: works out of the box. Most users run on CPU.
- **NVIDIA GPU**: much faster. Set device to `auto` or `cuda` in the benchmark script.
- **Intel iGPU / AMD GPU**: not supported by the CTranslate2 backend. Use CPU.

The experimental ONNX/WebGPU runtime is designed to be vendor-neutral when
WebGPU or DirectML is available, so Intel, AMD, and NVIDIA GPUs are all valid
targets. If neither GPU runtime can be selected by the JavaScript runtime, the
model uses CPU and will likely be slower than `large-v3-turbo`.

---

## First-time model download

On first use, the app downloads the selected model automatically from HuggingFace Hub. The `small` model (~484 MB) takes about a minute. After that, it loads from cache in seconds.

The model is stored in the HuggingFace cache (`%USERPROFILE%\.cache\huggingface\hub\` on Windows) and persists across restarts, reboots, and updates.

For Cohere and Granite, source checkouts use the system Node.js executable. If
`@huggingface/transformers` is missing, the app attempts `npm install`
automatically from the repository root on first ONNX use. The packaged Windows
release includes the JavaScript dependency tree when `node_modules` is present
at build time, but it still needs a Node.js executable unless the distribution
bundle adds one separately. Set `STT_APP_NODE_PATH` if Node.js is installed in a
non-standard location.

---

## Offline download

If the app cannot reach HuggingFace Hub (corporate firewall, air-gapped network, SSL/proxy issues), download models in advance on a machine with internet access.

### Method 1: Download script (recommended)

```powershell
# Download the default model (small):
uv run python scripts/download_model.py

# Download a specific model:
uv run python scripts/download_model.py --model large-v3-turbo

# Download an experimental ONNX/WebGPU model:
uv run python scripts/download_model.py --model cohere-transcribe-03-2026

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
use `scripts/download_model.py`; it downloads only the q4 ONNX files required by
the app and stores them in a real local folder to avoid Windows symlink
privilege errors.

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
```

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

### Custom Model Dir

Setting **Model Dir** (e.g. `D:\whisper-models`) causes all model downloads and cache lookups to use that directory instead of the default HuggingFace cache. The same internal structure is created there.

Useful for: USB transfer, network share, keeping models separate from user profile.
