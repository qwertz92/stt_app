# Models & Offline Setup

Everything about model choices, downloading, and configuring models for offline or corporate use.

## Available models

The app uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) models (CTranslate2 format, downloaded from HuggingFace).

| Model | Size | Languages | Best for |
|-------|------|-----------|----------|
| `tiny` | ~75 MB | Multilingual | Quick testing, fallback |
| `base` | ~141 MB | Multilingual | Light usage |
| `small` | ~484 MB | Multilingual | **Default — good balance for German + English** |
| `medium` | ~1.4 GB | Multilingual | Better quality, slower |
| `large-v3` | ~3.1 GB | Multilingual | Best quality (GPU recommended) |
| `large-v3-turbo` | ~809 MB | Multilingual | Fast + high quality — pruned version of large-v3 |
| `distil-large-v3.5` | ~756 MB | **English only** | Fastest high-quality English transcription |

### Which model should I use?

| Situation | Recommendation |
|-----------|---------------|
| German + English, normal laptop | `small` (default) |
| Better quality, still reasonable speed | `large-v3-turbo` |
| Best possible quality, NVIDIA GPU available | `large-v3` |
| English only, maximum speed | `distil-large-v3.5` |
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

### CPU vs GPU

The app works on **CPU** (default) and **NVIDIA GPU** (if CUDA is available).

- **Intel/AMD CPU**: works out of the box. Most users run on CPU.
- **NVIDIA GPU**: much faster. Set device to `auto` or `cuda` in the benchmark script.
- **Intel iGPU / AMD GPU**: not supported by the CTranslate2 backend. Use CPU.

---

## First-time model download

On first use, the app downloads the selected model automatically from HuggingFace Hub. The `small` model (~484 MB) takes about a minute. After that, it loads from cache in seconds.

The model is stored in the HuggingFace cache (`%USERPROFILE%\.cache\huggingface\hub\` on Windows) and persists across restarts, reboots, and updates.

---

## Offline download

If the app cannot reach HuggingFace Hub (corporate firewall, air-gapped network, SSL/proxy issues), download models in advance on a machine with internet access.

### Method 1: Download script (recommended)

```powershell
# Download the default model (small):
uv run python scripts/download_model.py

# Download a specific model:
uv run python scripts/download_model.py --model large-v3-turbo

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
python scripts/download_model.py --model small
```

</details>

### Method 2: Git clone

If git traffic is allowed through your proxy:

```bash
git clone https://huggingface.co/Systran/faster-whisper-small
```

Then import into the app's cache structure:

```powershell
uv run python scripts/import_model.py C:\Downloads\faster-whisper-small
```

<details>
<summary>All model repositories</summary>

```bash
git clone https://huggingface.co/Systran/faster-whisper-tiny
git clone https://huggingface.co/Systran/faster-whisper-base
git clone https://huggingface.co/Systran/faster-whisper-small
git clone https://huggingface.co/Systran/faster-whisper-medium
git clone https://huggingface.co/Systran/faster-whisper-large-v3
git clone https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo
git clone https://huggingface.co/distil-whisper/distil-large-v3.5-ct2
```

</details>

### Method 3: Manual browser download

Download these files from the HuggingFace model page: `config.json`, `model.bin`, `tokenizer.json`, `vocabulary.txt` (or `vocabulary.json`).

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
