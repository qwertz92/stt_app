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
- Phase 2a implemented:
  - **AssemblyAI** as first working remote provider (batch transcription)
  - Engine settings for `Local`, `AssemblyAI`, `OpenAI`, `Azure`, `Deepgram`
  - Mode settings for `Batch` / `Streaming` (streaming is available for local provider as experimental mode)
  - Provider plugin interface with streaming methods
  - API key fields saved via `keyring`
  - OpenAI, Azure, Deepgram remote providers remain placeholders

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
- AssemblyAI provider supports batch mode only (streaming planned for Phase 2b).
- OpenAI, Azure, Deepgram providers are placeholders (not implemented yet).

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

> **Wichtig:** Der Befehl ist immer `uv run python scripts/download_model.py`, nicht `uv scripts/...` oder `python3 scripts/...` (außerhalb des venv).
>
> **Ohne uv** (z.B. in der aktivierten venv): `python scripts/download_model.py --model small`
>
> **Außerhalb einer venv** ohne uv funktioniert das Script nicht, weil `huggingface_hub` fehlt. Nutze entweder `uv run python ...` oder aktiviere zuerst die venv.

Then transfer the files to the target machine:
- **Default cache:** Copy the entire `%USERPROFILE%\.cache\huggingface\` folder to the same location on the target machine.
- **Custom directory:** Copy the `--output-dir` folder to the target machine and set **Model Dir** in the app settings to that path.

### Manual download (alternative)

If you cannot run the script, download models manually from HuggingFace.

Each model requires these files: `config.json`, `model.bin`, `tokenizer.json`, `vocabulary.txt` (or `vocabulary.json` for large-v3).

| Model | Size | Language | HuggingFace page |
|-------|------|----------|------------------|
| `tiny` | ~75 MB | Multilingual | [Systran/faster-whisper-tiny](https://huggingface.co/Systran/faster-whisper-tiny/tree/main) |
| `base` | ~141 MB | Multilingual | [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base/tree/main) |
| `small` | ~484 MB | Multilingual | [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small/tree/main) |
| `medium` | ~1.43 GB | Multilingual | [Systran/faster-whisper-medium](https://huggingface.co/Systran/faster-whisper-medium/tree/main) |
| `large-v3` | ~3.09 GB | Multilingual | [Systran/faster-whisper-large-v3](https://huggingface.co/Systran/faster-whisper-large-v3/tree/main) |
| `large-v3-turbo` | ~809 MB | Multilingual | [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/tree/main) |
| `distil-large-v3.5` | ~756 MB | **English only** | [distil-whisper/distil-large-v3.5-ct2](https://huggingface.co/distil-whisper/distil-large-v3.5-ct2/tree/main) |

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

### How model loading works

Wenn du in den App-Einstellungen z.B. `small` als Modell wählst, passiert Folgendes:

1. **Die App gibt den Kurznamen `"small"` an faster-whisper weiter.**
2. **faster-whisper übersetzt diesen intern** in eine HuggingFace Repo-ID (z.B. `Systran/faster-whisper-small`).
3. **faster-whisper lädt das Modell über HuggingFace Hub herunter** (beim ersten Mal) und speichert es im HuggingFace-Cache (siehe nächster Abschnitt).
4. **Bei weiteren Starts** findet faster-whisper das Modell im Cache und lädt es direkt – kein Internet nötig.

**Das ist der Normalfall.** Du musst nichts manuell kopieren oder Pfade angeben. Einfach Modellgröße wählen → App startet → Download passiert automatisch.

**Sonderfälle (nur für Offline-/Corporate-Setups):**
- Wenn du "Model Dir" in den Einstellungen setzt (z.B. `D:\whisper-models`), werden Modelle dort statt im Standard-Cache gespeichert.
- Wenn du "Offline mode" aktivierst, versucht die App keinen Download – das Modell muss bereits im Cache (oder Model Dir) vorhanden sein.

### HuggingFace Cache: how it works

The HuggingFace cache is the place where models are stored after download. Key facts:

- **Per-user, in the user's home directory.** Not system-global.
  - Windows: `%USERPROFILE%\.cache\huggingface\hub\`  (e.g. `C:\Users\YourName\.cache\huggingface\hub\`)
  - Linux: `~/.cache/huggingface/hub/`
- **Persistent.** Files survive app restarts, reboots, updates. They are **never** deleted automatically — not when the app closes, not on reboot, not on update.
- **Shared across all Python programs** that use `huggingface_hub`. If you use `faster-whisper` in another project, it reuses the same cached models.
- **Can be overridden** with the `Model Dir` setting or the `HF_HOME` / `HF_HUB_CACHE` environment variables.

#### Cache directory structure

The cache is **not** a flat folder. It uses HuggingFace's internal structure:

```
%USERPROFILE%\.cache\huggingface\hub\
  models--Systran--faster-whisper-small\          ← one folder per model
    refs\
      main                                         ← text file: commit hash
    snapshots\
      abc123def456...\                             ← actual model files
        config.json
        model.bin
        tokenizer.json
        vocabulary.txt
    blobs\
      sha256-...\                                  ← raw content-addressed files
  models--mobiuslabsgmbh--faster-whisper-large-v3-turbo\
    ...same structure...
```

**This is why you cannot just drop files into a folder.** The short name `"small"` is resolved to `Systran/faster-whisper-small`, which maps to the internal path `models--Systran--faster-whisper-small/snapshots/<hash>/`. The download script handles this automatically.

#### Custom Model Dir

When you set **Model Dir** in Settings (e.g. `D:\whisper-models`):
- All model downloads go into that directory instead of the default HF cache.
- The same internal structure is created there: `D:\whisper-models\models--Systran--faster-whisper-small\snapshots\<hash>\...`
- The default cache is NOT touched.
- Useful for: USB stick transfer, network share, keeping models separate from user profile.

#### Where the download script puts files

- **Without `--output-dir`:** Into the default HF cache (`%USERPROFILE%\.cache\huggingface\hub\`).
- **With `--output-dir C:\whisper-models`:** Into `C:\whisper-models\` with the full HF structure.

#### Best practice for offline transfer

1. On a machine with internet: `python scripts/download_model.py --model small --output-dir C:\whisper-export`
2. Copy `C:\whisper-export\` to target machine (USB, network share).
3. On target machine: set **Model Dir** = `C:\whisper-export` and enable **Offline mode** in Settings.
4. Done — the app resolves the model from that directory.

### Model recommendations

| Use case | Recommended model |
|----------|-------------------|
| German + English, CPU-only | `small` (default) |
| German + English, better quality | `medium` or `large-v3-turbo` |
| Best quality, GPU available | `large-v3` |
| Fast multilingual, good quality | `large-v3-turbo` (~809 MB, pruned large-v3) |
| English only, fast + accurate | `distil-large-v3.5` (latest, best distil) |
| Quick testing | `tiny` |

**Notes:**
- `large-v3-turbo` is a pruned version of `large-v3` (4 decoder layers instead of 32). It is **multilingual** (99 languages) and much faster, with only a minor quality loss compared to `large-v3`.
- `distil-large-v3.5` is the latest distilled model (trained on 98k hours vs 22k for v3). It is ~1.5x faster than `large-v3-turbo` but **English-only**. For English dictation, it is the best speed/quality tradeoff. Not suitable for German or other languages.

## Packaging note (PyInstaller)

A starter spec file is included: `tts_app.spec`.

Example:

```powershell
uv run pyinstaller tts_app.spec
```
