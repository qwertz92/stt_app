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
- Right-click the transcript text in overlay and choose `Copy text`.
- Tray menu also has `Copy last transcript`.

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
- Streaming mode currently uses local provider only and performs periodic full-buffer partial updates (higher CPU usage than batch).
- Remote providers are placeholders (not implemented yet).

## Packaging note (PyInstaller)

A starter spec file is included: `tts_app.spec`.

Example:

```powershell
uv run pyinstaller tts_app.spec
```
