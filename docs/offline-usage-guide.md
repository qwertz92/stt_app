# Offline Usage Guide

This guide covers all methods for downloading and configuring faster-whisper models when the target machine cannot reach HuggingFace Hub — whether due to a corporate firewall, Zscaler proxy, air-gapped network, or SSL certificate issues.

## Table of contents

- [Quick reference](#quick-reference)
- [Method 1: Download script (recommended)](#method-1-download-script-recommended)
- [Method 2: Git clone](#method-2-git-clone)
- [Method 3: Manual browser download](#method-3-manual-browser-download)
- [Transferring models to the target machine](#transferring-models-to-the-target-machine)
- [App configuration for offline use](#app-configuration-for-offline-use)
- [SSL / Zscaler troubleshooting](#ssl--zscaler-troubleshooting)
- [Verifying your setup](#verifying-your-setup)
- [Available models](#available-models)

---

## Quick reference

| Step | Action |
|------|--------|
| 1 | Download the model on a machine **with** internet access (see methods below) |
| 2 | Copy the downloaded folder to the target machine |
| 3 | In the app: Settings → enable **Offline mode** |
| 4 | If you used a custom directory: Settings → set **Model Dir** to that path |

---

## Method 1: Download script (recommended)

The included download script handles the HuggingFace cache structure automatically.

**On a machine with internet access:**

```powershell
# Download the default model (small, ~484 MB):
uv run python scripts/download_model.py

# Download a specific model:
uv run python scripts/download_model.py --model medium

# Download into a custom directory (USB stick / network share):
uv run python scripts/download_model.py --model small --output-dir C:\whisper-export

# Download all models at once:
uv run python scripts/download_model.py --all

# List available models:
uv run python scripts/download_model.py --list
```

**Without uv** (inside an activated venv):
```powershell
python scripts/download_model.py --model small
```

> The script requires `huggingface_hub` to be installed. It is included in the project dependencies and therefore inside the venv.

### Transfer to target machine

- **Default cache download** (no `--output-dir`): Copy `%USERPROFILE%\.cache\huggingface\` to the same location on the target machine.
- **Custom directory** (`--output-dir`): Copy the entire output folder to the target machine, then set **Model Dir** in the app settings to that path.

---

## Method 2: Git clone

If `git` traffic is not blocked by your proxy (some corporate proxies allow git but block Python/pip HTTP requests):

```bash
# Clone a specific model:
git clone https://huggingface.co/Systran/faster-whisper-small

# Other models:
git clone https://huggingface.co/Systran/faster-whisper-tiny
git clone https://huggingface.co/Systran/faster-whisper-base
git clone https://huggingface.co/Systran/faster-whisper-medium
git clone https://huggingface.co/Systran/faster-whisper-large-v3
git clone https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo
git clone https://huggingface.co/distil-whisper/distil-large-v3.5-ct2
```

**Using the cloned model:**

A git-cloned model is a flat directory containing the model files directly. You can use it two ways:

1. **Direct path:** In app Settings → set **Model Dir** to the parent directory of the cloned folder. Then set the model size to match (e.g. `small`). The app will look for a subfolder named `faster-whisper-small` inside Model Dir.

2. **Manual override:** Edit `%APPDATA%\tts_app\settings.json` and set `model_size` to the full path of the cloned directory:
   ```json
   "model_size": "C:\\models\\faster-whisper-small"
   ```
   When `model_size` is an existing directory path, faster-whisper uses it directly.

---

## Method 3: Manual browser download

If neither the script nor git works, download model files manually from the HuggingFace website.

### Required files per model

Each model needs these files: `config.json`, `model.bin`, `tokenizer.json`, and `vocabulary.txt` (or `vocabulary.json` for large-v3).

### Download pages

| Model | HuggingFace page |
|-------|------------------|
| tiny (~75 MB) | [Systran/faster-whisper-tiny](https://huggingface.co/Systran/faster-whisper-tiny/tree/main) |
| base (~141 MB) | [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base/tree/main) |
| small (~484 MB) | [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small/tree/main) |
| medium (~1.4 GB) | [Systran/faster-whisper-medium](https://huggingface.co/Systran/faster-whisper-medium/tree/main) |
| large-v3 (~3 GB) | [Systran/faster-whisper-large-v3](https://huggingface.co/Systran/faster-whisper-large-v3/tree/main) |
| large-v3-turbo (~809 MB) | [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/tree/main) |
| distil-large-v3.5 (~756 MB, EN only) | [distil-whisper/distil-large-v3.5-ct2](https://huggingface.co/distil-whisper/distil-large-v3.5-ct2/tree/main) |

### Arranging manually downloaded files

After downloading the individual files, place them in a flat directory:

```
C:\whisper-models\faster-whisper-small\
    config.json
    model.bin
    tokenizer.json
    vocabulary.txt
```

Then use the **direct path method**: set `model_size` in `settings.json` to the full folder path:
```json
"model_size": "C:\\whisper-models\\faster-whisper-small"
```

Or set **Model Dir** to the parent (`C:\whisper-models`) and the app's flat-directory scan will find it.

---

## Transferring models to the target machine

### USB stick / network share

1. Download models to a portable directory: `--output-dir E:\whisper-models`
2. Copy the entire directory to the target machine.
3. On the target: Settings → **Model Dir** = path to the copied directory.
4. Settings → enable **Offline mode**.

### Copying the HuggingFace cache

If you downloaded without `--output-dir`, the models are in the default HF cache:
- **Windows:** `%USERPROFILE%\.cache\huggingface\`
- **Linux:** `~/.cache/huggingface/`

Copy this entire folder to the same location on the target machine. The app will find models there automatically (no Model Dir setting needed).

---

## App configuration for offline use

After transferring model files:

1. **Enable Offline mode:** Settings → check **Offline mode**.
   - This sets `local_files_only=True`, preventing any network access attempts.

2. **Set Model Dir** (only if you used a custom directory):
   - Settings → **Model Dir** → browse to your model directory.
   - Or edit `%APPDATA%\tts_app\settings.json`:
     ```json
     "offline_mode": true,
     "model_dir": "D:\\whisper-models"
     ```

3. **Alternative: environment variable** (before launching):
   ```powershell
   $env:HF_HUB_OFFLINE = "1"
   python main.py
   ```

---

## SSL / Zscaler troubleshooting

### Symptom

```
SSL: CERTIFICATE_VERIFY_FAILED
certificate verify failed: unable to get local issuer certificate
```

This happens when a corporate proxy (Zscaler, BlueCoat, Forcepoint, etc.) intercepts HTTPS connections and replaces the server's SSL certificate with its own. Python's SSL library doesn't trust the proxy's certificate.

### Fix 1: Set corporate CA bundle (best solution)

Ask your IT department for the corporate root CA certificate (`.pem` or `.crt` file). Then set these environment variables **before** running the app or download script:

```powershell
# PowerShell:
$env:REQUESTS_CA_BUNDLE = "C:\path\to\corporate-ca-bundle.pem"
$env:CURL_CA_BUNDLE     = "C:\path\to\corporate-ca-bundle.pem"
$env:SSL_CERT_FILE      = "C:\path\to\corporate-ca-bundle.pem"

# Then run:
python scripts/download_model.py --model small
```

To make this permanent, add the environment variables to your Windows user profile:
```powershell
[System.Environment]::SetEnvironmentVariable("REQUESTS_CA_BUNDLE", "C:\path\to\corporate-ca-bundle.pem", "User")
[System.Environment]::SetEnvironmentVariable("SSL_CERT_FILE", "C:\path\to\corporate-ca-bundle.pem", "User")
```

### Fix 2: Export CA from browser

If IT won't provide the .pem file, you can often export it yourself:

1. Open `https://huggingface.co` in your browser (Chrome/Edge).
2. Click the lock icon → "Connection is secure" → "Certificate".
3. Go to the **Certification Path** tab → select the **root** certificate at the top.
4. Click "View Certificate" → "Details" tab → "Copy to File...".
5. Export as **Base-64 encoded X.509 (.CER)**.
6. Set `REQUESTS_CA_BUNDLE` to the exported file path.

### Fix 3: pip/certifi certificate injection

```powershell
# Find where Python's certifi bundle is:
python -c "import certifi; print(certifi.where())"

# Append your corporate CA to certifi's bundle:
# (Make a backup first!)
Get-Content "C:\path\to\corporate-ca.pem" | Add-Content (python -c "import certifi; print(certifi.where())")
```

### Fix 4: Download on an unrestricted machine

Use any of the download methods above on a personal machine or VPN without SSL interception, then transfer the files.

### AssemblyAI / remote providers with Zscaler

The same SSL issue affects remote providers (AssemblyAI, OpenAI, etc.). The CA bundle fix (Fix 1) applies to all HTTPS connections. Alternatively, use the **local** provider which only needs network access for the initial model download.

---

## Verifying your setup

After transferring models and configuring offline mode:

1. **Check available models** — the app shows locally cached models at startup:
   - Look for "Model loaded: small" (or your chosen model) in the overlay.
   - If the model is not found, the app will list available local models.

2. **Smoke test:**
   ```powershell
   uv run python scripts/smoke_test.py --check-model
   ```

3. **Check the log file** at `%APPDATA%\tts_app\logs\dictation.log` for model loading details.

---

## Available models

| Model | Size | Languages | Best for |
|-------|------|-----------|----------|
| `tiny` | ~75 MB | Multilingual | Quick testing, fallback |
| `base` | ~141 MB | Multilingual | Light usage |
| `small` | ~484 MB | Multilingual | **Default — good balance** |
| `medium` | ~1.4 GB | Multilingual | Better quality |
| `large-v3` | ~3 GB | Multilingual | Best quality (GPU recommended) |
| `large-v3-turbo` | ~809 MB | Multilingual | Fast + high quality (pruned large-v3) |
| `distil-large-v3.5` | ~756 MB | **English only** | Fastest high-quality English |

**Recommendation for German + English dictation:** `small` (default) or `large-v3-turbo` (better quality, needs more RAM).
