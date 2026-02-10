# tts_app

Windows 11 dictation desktop app MVP (Phase 1) with:
- Global hotkey via `RegisterHotKey` (no low-level keyboard hooks)
- Always-on-top overlay (`Idle`, `Listening`, `Processing`, `Done`, `Error`)
- Microphone capture from default device
- Batch transcription with local `faster-whisper` (+ optional experimental local streaming)
- Clipboard-safe text insertion (`save -> set -> WM_PASTE/Ctrl+V -> restore`)
- Settings JSON under `%APPDATA%\tts_app\settings.json`
- API secret storage via Windows Credential Manager (`keyring`)
- File logging under `%APPDATA%\tts_app\logs\dictation.log`

## Phase coverage

- Phase 1 implemented and wired.
- Phase 2 in progress:
  - Engine settings for `Local`, `OpenAI`, `Azure`, `Deepgram`
  - Mode settings for `Batch` / `Streaming` (streaming is available for local provider as experimental mode)
  - Provider plugin interface with streaming methods
  - API key fields saved via `keyring`
  - Remote providers remain placeholders

## Requirements

- Windows 11
- Python 3.12 (recommended for `faster-whisper` compatibility)
- `uv`

## Setup (uv)

```powershell
uv python pin 3.12
uv sync --group dev
```

## Corporate Setup (No uv)

If `uv.exe` is blocked by Group Policy/AppLocker, use plain Python + pip on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev-win.txt
```

Run app:

```powershell
python main.py
```

Run tests:

```powershell
python -m pytest
```

Notes for locked networks:
- Prefer your internal artifact proxy/index (Artifactory/Nexus), not direct internet installers.
- Avoid installer scripts like `irm ... | iex` if Zscaler blocks them.
- If outbound package access is restricted, build an internal wheelhouse and install with `--no-index --find-links`.

Detailed corporate deployment guide:
- `docs/enterprise-deployment-guide.md`
- `docs/local-models-and-benchmark.md`
- `docs/streaming-mode.md`
- `docs/model-error-rate-reference.md`

## Run app

```powershell
uv run python main.py
```

Or:

```powershell
uv run tts-app
```

Default hotkey: `Ctrl+Alt+Space` (with automatic fallback to `Ctrl+Win+LShift` if unavailable).

## Stop app

- Preferred: system tray icon -> `Quit`.
- Console run: `Ctrl+C` now requests graceful shutdown.
- Fallback if UI/tray is stuck: `taskkill /IM python.exe /F` (or close from Task Manager).

## Command difference

- `uv run tts-app`: runs the installed console entrypoint (`tts_app.main:run`) from environment metadata.
- `uv run python main.py`: runs local script file directly.
- Both end up in the same app code, but after code changes `uv run tts-app` may require `uv sync --group dev` first to refresh the installed package.

## WSL note

- WSL is useful for git/tooling, but this app is Windows-native (global hotkey, Win32 input, clipboard, foreground window APIs).
- Running the full app from Linux/WSL is not supported.
- Best path for company laptop: run on native Windows Python environment.

## Run tests

```powershell
uv run python -m pytest
```

## Hotkey assignment

- In Settings, the hotkey is now captured with a key recorder field (`QKeySequenceEdit`).
- Click the field and press your combination; no manual typing needed.
- `Mode` can be switched to:
  - `Batch` (recommended default)
  - `Streaming (Experimental)` (local provider only, live insertion while speaking)
- In streaming mode, dictation auto-aborts with a short beep when foreground window or focused text control changes.
- In Settings, `Paste Mode` can be chosen:
  - `Auto (SendInput -> WM_PASTE)`
  - `WM_PASTE only`
  - `SendInput only`
- `Keep transcript in clipboard after transcription` keeps recognized text available for manual paste/copy.

## Transcript copy

- Overlay transcript text is selectable.
- Overlay has a direct `Copy` button to copy the shown text instantly.
- Copy button gives visual feedback (`Copied`) and best-effort restores the previously focused external window.
- Right-click the transcript text in overlay and choose `Copy text`.
- Tray menu also has `Copy last transcript`.
- Overlay transcript area auto-grows with content up to 4x base height, then enables vertical scrolling.

## Smoke test

Basic smoke test:

```powershell
uv run python scripts/smoke_test.py
```

Optional device/model checks:

```powershell
uv run python scripts/smoke_test.py --check-mic --check-model
```

Local model benchmark:

```powershell
uv run python scripts/benchmark_local.py --list-models --show-model-sizes
uv run python scripts/benchmark_local.py .\samples\benchmark_sample.wav --models tiny,base,small --device cpu --compute-types int8 --runs 3 --warmup --csv-out .\benchmark\result.csv --json-out .\benchmark\result.json
```

Sample benchmark file in repo:
- `samples/benchmark_sample.wav`
- For parameter explanations, interruption behavior, and interpretation:
- `docs/local-models-and-benchmark.md`

## Project structure

- `src/tts_app/hotkey.py` - global hotkey registration + Qt native event filter
- `src/tts_app/config.py` - centralized global configuration values
- `src/tts_app/overlay_ui.py` - always-on-top status overlay
- `src/tts_app/audio_capture.py` - microphone recording + optional VAD stop
- `src/tts_app/vad.py` - energy-based VAD logic
- `src/tts_app/transcriber/` - transcriber interface + local faster-whisper provider + remote stubs
- `src/tts_app/text_inserter.py` - clipboard-safe paste and restore
- `src/tts_app/settings_store.py` - JSON settings + migration
- `src/tts_app/secret_store.py` - keyring-backed secret storage
- `src/tts_app/logger.py` - rotating file logger + diagnostics export
- `src/tts_app/settings_dialog.py` - settings UI (includes Phase 2 controls)
- `src/tts_app/controller.py` - orchestration/state machine

## Troubleshooting

- **Model download fails / "cannot find snapshot" on corporate machine:**
  - The app uses `faster-whisper` models from HuggingFace Hub. On restricted networks, the download fails.
  - See the [Offline model setup](#offline-model-setup-restricted-networks) section below for instructions.
- Hotkey registration fails:
  - The app auto-falls back to `Ctrl+Win+LShift`.
  - If both fail, choose another combo in Settings.
- Shortcut did not change to new default:
  - Existing `%APPDATA%\\tts_app\\settings.json` is preserved.
  - This version migrates old defaults (`Ctrl+Win+LShift`, `Ctrl+Shift+Alt+Space`) to `Ctrl+Alt+Space` automatically.
- Error `No module named 'requests'` during local transcription:
  - Run `uv sync --group dev` and restart app.
- No text inserted in elevated target app:
  - Run dictation app with matching privileges (UIPI limitation).
- Transcribed text not inserted but shown in overlay:
  - Ensure the target app is focused at stop/insert time.
  - If insertion fails, app now copies transcript to clipboard automatically.
  - This build uses synchronous `WM_PASTE` first and delays clipboard restore on `SendInput` fallback to avoid stale clipboard paste races.
- No microphone:
  - Verify default input device in Windows Sound settings.
- Slow first transcription:
  - `faster-whisper` model download/load happens on first use.

## Limitations (current MVP)

- Text insertion preserves text clipboard content only (not arbitrary binary formats).
- Streaming mode currently uses local provider only and performs periodic trailing-window partial updates (still higher CPU usage than batch).
- Remote providers are placeholders (not implemented yet).

## Offline model setup (restricted networks)

If the app cannot reach HuggingFace Hub (corporate firewall, air-gapped machine), you must download models in advance.

### Automatic download (recommended)

On a machine **with** internet access, run the download script:

```powershell
# Download the default model (small, ~484 MB):
uv run python scripts/download_model.py

# Download a specific model:
uv run python scripts/download_model.py --model medium

# Download into a custom directory (e.g. for a USB stick / network share):
uv run python scripts/download_model.py --model small --output-dir C:\whisper-models

# Download all models at once:
uv run python scripts/download_model.py --all

# List available models:
uv run python scripts/download_model.py --list
```

Without `uv`:
```powershell
python scripts/download_model.py --model small
```

Then transfer the files to the target machine:
- **Default cache:** Copy the entire `%USERPROFILE%\.cache\huggingface\` folder to the same location on the target machine.
- **Custom directory:** Copy the `--output-dir` folder to the target machine and set **Model Dir** in the app settings to that path.

### Manual download (alternative)

If you cannot run the script, download models manually from HuggingFace.

Each model requires these files: `config.json`, `model.bin`, `tokenizer.json`, `vocabulary.txt` (or `vocabulary.json` for large-v3/distil-large-v3).

| Model | Size | Language | HuggingFace page |
|-------|------|----------|------------------|
| `tiny` | ~75 MB | Multilingual | [Systran/faster-whisper-tiny](https://huggingface.co/Systran/faster-whisper-tiny/tree/main) |
| `base` | ~141 MB | Multilingual | [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base/tree/main) |
| `small` | ~484 MB | Multilingual | [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small/tree/main) |
| `medium` | ~1.43 GB | Multilingual | [Systran/faster-whisper-medium](https://huggingface.co/Systran/faster-whisper-medium/tree/main) |
| `large-v3` | ~3.09 GB | Multilingual | [Systran/faster-whisper-large-v3](https://huggingface.co/Systran/faster-whisper-large-v3/tree/main) |
| `distil-large-v3` | ~756 MB | **English only** | [Systran/faster-distil-whisper-large-v3](https://huggingface.co/Systran/faster-distil-whisper-large-v3/tree/main) |

You can also clone with `git`:
```bash
git clone https://huggingface.co/Systran/faster-whisper-small
```

### App configuration for offline use

1. **Enable offline mode** in Settings → check "Offline mode". This tells faster-whisper to never attempt network access (`local_files_only=True`).

2. **Set Model Dir** (optional): If you downloaded to a custom directory, set "Model Dir" in Settings to that path. If you used the default HuggingFace cache, leave this empty.

3. Alternatively, set the environment variable before launching:
   ```powershell
   $env:HF_HUB_OFFLINE = "1"
   ```

### How model paths work internally

`faster-whisper`'s `WhisperModel` resolves models as follows:

1. If the model name is an **existing directory** path → uses it directly (must contain model files).
2. Otherwise, maps short names (e.g. `"small"`) to HuggingFace repo IDs (e.g. `Systran/faster-whisper-small`) and downloads via `huggingface_hub.snapshot_download()`.

The HuggingFace cache structure is **not** a simple flat folder. It uses:
```
<cache_dir>/models--Systran--faster-whisper-small/
  refs/main                    (text file with commit hash)
  snapshots/<commit_hash>/     (actual model files live here)
  blobs/                       (SHA256-named raw file data)
```

This is why simply placing files in a folder does not work — you need either the download script or a copy of a working cache.

### Model recommendations

| Use case | Recommended model |
|----------|-------------------|
| German + English, CPU-only | `small` (default) |
| German + English, better quality | `medium` |
| Best quality, GPU available | `large-v3` |
| English only, fast + accurate | `distil-large-v3` |
| Quick testing | `tiny` |

**Note:** `distil-large-v3` is ~6x faster than `large-v3` and within 1% WER on English, but it is **English-only** and will perform poorly for German or other languages.

## Packaging note (PyInstaller)

A starter spec file is included: `tts_app.spec`.

Example:

```powershell
uv run pyinstaller tts_app.spec
```
