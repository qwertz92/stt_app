# Advanced Setup

This guide covers corporate deployment, SSL/proxy troubleshooting, packaging, and benchmarking. For basic setup, see the [Quick Start](quick-start.md).

---

## Corporate deployment (Windows)

The app requires **native Windows** (not Linux/WSL) because it uses Win32 APIs for hotkey registration, clipboard access, and text insertion.

### Without uv

If `uv.exe` is blocked by Group Policy or AppLocker, use standard Python + pip:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev-win.txt
python main.py
```

### Offline package installation (wheelhouse)

A "wheelhouse" is a folder containing pre-downloaded Python packages (`.whl` files). This lets you install everything without internet access on the target machine.

**Why?** In air-gapped or heavily restricted corporate networks, `pip install` cannot reach PyPI. A wheelhouse bundles all needed packages for offline installation.

#### Step 1: Build the wheelhouse (on a machine with internet)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip download -r requirements-dev-win.txt -d wheelhouse
```

This downloads all packages (including compiled binaries like `pywin32`, `ctranslate2`, `numpy`) as `.whl` files into the `wheelhouse/` folder.

#### Step 2: Transfer

Copy these to the target machine:
- The `wheelhouse/` folder
- The project source code
- `requirements-dev-win.txt`

Use ZIP archive, USB drive, or internal file share.

#### Step 3: Install on target (no internet)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --no-index --find-links .\wheelhouse -r requirements-dev-win.txt
python main.py
```

### Using an internal package index

If your organization has an Artifactory/Nexus/DevPI server:

```powershell
python -m pip install --index-url https://your-artifactory.corp/pypi/simple -r requirements-dev-win.txt
```

### Recommended rollout

1. Build and version an internal wheelhouse.
2. Standardize on Python 3.12 + venv + offline install.
3. Include the smoke test as a verification step: `python scripts/smoke_test.py`.

---

## Packaging as EXE (PyInstaller)

The project includes a starter spec file `tts_app.spec`.

```powershell
python -m pip install pyinstaller
pyinstaller tts_app.spec
```

Notes:
- The resulting EXE should be code-signed for corporate environments (unsigned binaries may be blocked by EDR/antivirus).
- Some EDR solutions may block `SendInput` (used for text insertion). This requires a policy exception for the signed application.
- The spec does not bundle model files — the user still needs to download models separately.

---

## SSL / proxy issues

### Symptom

```
SSL: CERTIFICATE_VERIFY_FAILED
certificate verify failed: unable to get local issuer certificate
```

This happens when a corporate proxy (Zscaler, BlueCoat, Forcepoint) intercepts HTTPS and replaces the SSL certificate. Python does not trust the proxy's certificate.

The app and download script detect this error and print actionable instructions.

### Fix 1: Corporate CA bundle (recommended)

Ask IT for the corporate root CA certificate (`.pem` or `.crt` file). Then set environment variables:

```powershell
$env:REQUESTS_CA_BUNDLE = "C:\path\to\corporate-ca-bundle.pem"
$env:SSL_CERT_FILE      = "C:\path\to\corporate-ca-bundle.pem"
python scripts/download_model.py --model small
```

To make permanent:
```powershell
[System.Environment]::SetEnvironmentVariable("REQUESTS_CA_BUNDLE", "C:\path\to\corporate-ca-bundle.pem", "User")
[System.Environment]::SetEnvironmentVariable("SSL_CERT_FILE", "C:\path\to\corporate-ca-bundle.pem", "User")
```

### Fix 2: Export CA from browser

1. Open `https://huggingface.co` in Chrome/Edge.
2. Click lock icon → "Connection is secure" → "Certificate".
3. Go to **Certification Path** → select the **root** certificate.
4. Click "View Certificate" → "Details" → "Copy to File..." → Base-64 encoded X.509 (.CER).
5. Set `REQUESTS_CA_BUNDLE` to the exported file path.

### Fix 3: Inject CA into certifi

```powershell
# Find certifi's CA bundle:
python -c "import certifi; print(certifi.where())"

# Append corporate CA (make a backup first!):
Get-Content "C:\path\to\corporate-ca.pem" | Add-Content (python -c "import certifi; print(certifi.where())")
```

### Fix 4: Download elsewhere

Download models on an unrestricted machine and transfer files. See [Models & Offline Setup](models.md#offline-download).

### Remote providers (AssemblyAI etc.)

The same SSL issue affects cloud providers. The CA bundle fix applies to all HTTPS connections. Alternatively, use the local engine which only needs internet for the initial model download.

---

## WSL / Linux

WSL is useful for development tools (git, editors, linters), but the app itself requires Windows-specific APIs:
- `RegisterHotKey` (global hotkey)
- `SendInput` / `WM_PASTE` (text insertion)
- Win32 clipboard and foreground window APIs

Running the full app from Linux/WSL is not supported.

---

## Benchmarking

The project includes a local benchmark script to compare model speed on your hardware.

### Quick start

```powershell
# List available models:
uv run python scripts/benchmark_local.py --list-models --show-model-sizes

# Benchmark with the included sample file:
uv run python scripts/benchmark_local.py .\samples\benchmark_sample.wav --models tiny,base,small --device cpu --compute-types int8 --runs 3 --warmup
```

### Full benchmark with export

```powershell
uv run python scripts/benchmark_local.py .\samples\benchmark_sample.wav \
    --models tiny,base,small,medium \
    --device cpu \
    --compute-types int8,float32 \
    --runs 3 --warmup \
    --csv-out .\benchmark\result.csv \
    --json-out .\benchmark\result.json
```

### Understanding the output

| Column | Meaning |
|--------|---------|
| Load | Model initialization time (includes download on first run) |
| Avg | Average transcription time over `--runs` |
| StdDev | Variation between runs |
| RTF | Real-Time Factor: `transcription_time / audio_duration`. Below 1.0 = faster than real-time |
| Lang | Detected language |

### Why short audio can still take long

Benchmark time is dominated by model loading, not audio length. Larger models take longer to load. The first run may also include a download step.

### Parameters

| Parameter | Description |
|-----------|-------------|
| `audio_path` | Input audio file |
| `--models` | Comma-separated model IDs |
| `--device` | `auto`, `cpu`, or `cuda` |
| `--compute-types` | Precision: `int8`, `float32`, `float16` |
| `--runs` | Number of measured runs per case |
| `--warmup` | Run one unmeasured pass first |
| `--beam-size` | Decoding beam size (higher = better but slower) |
| `--language` | Force language code (e.g. `de`, `en`) |
| `--vad-filter` | Enable built-in VAD filtering |
| `--csv-out` | Export results as CSV |
| `--json-out` | Export results as JSON |
| `--isolated-case` / `--no-isolated-case` | Per-case subprocess isolation (default: on) |

### Sample audio

The repo includes `samples/benchmark_sample.wav` (synthetic tones for pipeline validation). For realistic quality testing, use real speech recordings.

Regenerate the sample:
```powershell
uv run python scripts/generate_sample_audio.py
```

---

## Smoke test

Basic functionality check:

```powershell
uv run python scripts/smoke_test.py
```

With device and model verification:

```powershell
uv run python scripts/smoke_test.py --check-mic --check-model
```

---

## Technical notes

### Text insertion

The app inserts transcribed text at the cursor using clipboard-based paste:

1. Saves current clipboard content.
2. Puts transcribed text on clipboard.
3. Sends paste command to the target app.
4. Restores original clipboard content.

**Paste mode** (configurable in Settings):
- **Auto** (default): Tries `SendInput` (simulates Ctrl+V) first, falls back to `WM_PASTE` if that fails.
- **SendInput only**: Always simulates Ctrl+V keystrokes.
- **WM_PASTE only**: Sends WM_PASTE message directly to the target window.

A short delay after SendInput prevents race conditions where the target app reads the clipboard asynchronously.

### Hotkey

- Uses Win32 `RegisterHotKey` with the configured key combination.
- Default: `Ctrl+Alt+Space`. If registration fails, falls back to `Ctrl+Win+LShift`.
- Win-key combinations can fail due to OS reservations.
- Settings UI uses key capture (no manual typing needed).

### Settings storage

- Settings: `%APPDATA%\tts_app\settings.json` (JSON with automatic schema migration)
- Secrets (API keys): Windows Credential Manager via `keyring`
- Logs: `%APPDATA%\tts_app\logs\dictation.log` (rotating, max 1 MB)

### Project structure

```
src/tts_app/
  config.py           — centralized configuration constants
  controller.py       — main orchestrator / state machine
  audio_capture.py    — microphone recording + VAD auto-stop
  overlay_ui.py       — always-on-top status overlay
  hotkey.py           — Win32 hotkey registration
  text_inserter.py    — clipboard-safe paste
  window_focus.py     — foreground window tracking
  settings_store.py   — JSON settings + migration
  settings_dialog.py  — Settings UI
  secret_store.py     — keyring wrapper for API keys
  logger.py           — rotating file logger
  vad.py              — energy-based voice activity detection
  transcriber/
    base.py           — transcriber interface
    factory.py        — engine selection
    local_faster_whisper.py — local transcription (faster-whisper / CTranslate2)
    assemblyai_provider.py  — AssemblyAI cloud transcription (batch + streaming)
    openai_provider.py      — OpenAI cloud transcription (batch + chunked streaming)
    groq_provider.py        — Groq cloud transcription (batch)
    deepgram_provider.py    — Deepgram cloud transcription (batch + streaming)
```
