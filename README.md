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

If the app cannot reach HuggingFace Hub (corporate firewall, air-gapped machine), you must provide models manually.

### Step 1: Enable offline mode

In Settings, check **"Offline mode (use cached models only, no internet)"**.
This sets `HF_HUB_OFFLINE=1` internally so the app never attempts network access.

Alternatively, set the environment variable manually before launching:
```powershell
$env:HF_HUB_OFFLINE = "1"
```

### Step 2: Download the model files

Each model needs these files placed in a single folder: `config.json`, `model.bin`, `tokenizer.json`, `vocabulary.txt` (or `vocabulary.json` for large-v3).

Download all files for your chosen model from the links below:

| Model | Size | HuggingFace page | Direct downloads |
|-------|------|-------------------|------------------|
| `tiny` | ~75 MB | [Systran/faster-whisper-tiny](https://huggingface.co/Systran/faster-whisper-tiny) | [config.json](https://huggingface.co/Systran/faster-whisper-tiny/resolve/main/config.json) · [model.bin](https://huggingface.co/Systran/faster-whisper-tiny/resolve/main/model.bin) · [tokenizer.json](https://huggingface.co/Systran/faster-whisper-tiny/resolve/main/tokenizer.json) · [vocabulary.txt](https://huggingface.co/Systran/faster-whisper-tiny/resolve/main/vocabulary.txt) |
| `base` | ~141 MB | [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base) | [config.json](https://huggingface.co/Systran/faster-whisper-base/resolve/main/config.json) · [model.bin](https://huggingface.co/Systran/faster-whisper-base/resolve/main/model.bin) · [tokenizer.json](https://huggingface.co/Systran/faster-whisper-base/resolve/main/tokenizer.json) · [vocabulary.txt](https://huggingface.co/Systran/faster-whisper-base/resolve/main/vocabulary.txt) |
| `small` | ~484 MB | [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small) | [config.json](https://huggingface.co/Systran/faster-whisper-small/resolve/main/config.json) · [model.bin](https://huggingface.co/Systran/faster-whisper-small/resolve/main/model.bin) · [tokenizer.json](https://huggingface.co/Systran/faster-whisper-small/resolve/main/tokenizer.json) · [vocabulary.txt](https://huggingface.co/Systran/faster-whisper-small/resolve/main/vocabulary.txt) |
| `medium` | ~1.43 GB | [Systran/faster-whisper-medium](https://huggingface.co/Systran/faster-whisper-medium) | [config.json](https://huggingface.co/Systran/faster-whisper-medium/resolve/main/config.json) · [model.bin](https://huggingface.co/Systran/faster-whisper-medium/resolve/main/model.bin) · [tokenizer.json](https://huggingface.co/Systran/faster-whisper-medium/resolve/main/tokenizer.json) · [vocabulary.txt](https://huggingface.co/Systran/faster-whisper-medium/resolve/main/vocabulary.txt) |
| `large-v3` | ~3.09 GB | [Systran/faster-whisper-large-v3](https://huggingface.co/Systran/faster-whisper-large-v3) | [config.json](https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/config.json) · [model.bin](https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/model.bin) · [tokenizer.json](https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/tokenizer.json) · [vocabulary.json](https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/vocabulary.json) |

All models are multilingual (99+ languages). The default is `small` — best balance of speed and accuracy for CPU-only machines.

**Tip:** You can download using `git clone` on a machine with internet if individual downloads are difficult:
```bash
git clone https://huggingface.co/Systran/faster-whisper-small
```

### Step 3: Place files in the HuggingFace cache

The `huggingface_hub` library caches models under:
```
%USERPROFILE%\.cache\huggingface\hub\models--Systran--faster-whisper-<model>\
```

**Easiest approach:** Run the app once on any machine with internet access (e.g. personal laptop). It will download and cache the model automatically. Then copy the entire folder to the target machine:
```
%USERPROFILE%\.cache\huggingface\
```

**Manual placement alternative:** You can also pass a local directory path directly as the model name to faster-whisper. To do this, download all files into a folder (e.g. `C:\models\faster-whisper-small\`) and set the model size in the app to the folder path. *(Note: the settings UI currently only supports the standard model names.)*

### Model quality notes

All offered models are the original OpenAI Whisper models converted to CTranslate2 format by Systran. They are multilingual and support 99+ languages including German and English. For multilingual dictation (especially German), `small` or `medium` are recommended — `tiny` and `base` have noticeably higher word error rates for non-English speech.

## Packaging note (PyInstaller)

A starter spec file is included: `tts_app.spec`.

Example:

```powershell
uv run pyinstaller tts_app.spec
```
