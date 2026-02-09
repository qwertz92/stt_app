# AGENTS.md

## Purpose
This file is the running project memory for `tts_app`.
Keep it updated whenever behavior, architecture, dependencies, or operational learnings change.

## Current scope
- Phase 1 (MVP) implemented: local batch dictation on Windows 11.
- Phase 2 skeleton implemented: provider selection, streaming placeholders, API key fields.

## Runtime stack
- Python 3.12
- PySide6 UI/tray/overlay
- Win32 RegisterHotKey + SendInput (no low-level keyboard hook)
- sounddevice for mic capture
- faster-whisper local provider
- keyring for secret storage

## Core flow
1. Global hotkey toggles recording.
2. Overlay moves through states: `Idle -> Listening -> Processing -> Done/Error`.
3. Recorded WAV bytes go to local transcriber.
4. Transcribed text is inserted at caret via clipboard-safe paste.
5. Clipboard text content is restored.

## Central configuration
All key global defaults are centralized in `src/tts_app/config.py`.
Important defaults:
- `DEFAULT_HOTKEY = "Ctrl+Alt+Space"`
- `FALLBACK_HOTKEY = "Ctrl+Win+LShift"`
- `DEFAULT_MODEL_SIZE = "small"`
- `DEFAULT_ENGINE = "local"`
- `DEFAULT_MODE = "batch"`

## Hotkey notes
- `RegisterHotKey` supports the configured hotkey syntax with one non-modifier key.
- `Ctrl+Alt+Space` is default.
- Pure modifier-only combinations (e.g. Ctrl+Shift+Alt alone) are not supported by RegisterHotKey.
- Win-key combinations can fail due to OS reservations; controller attempts fallback hotkey when preferred registration fails.
- Settings UI hotkey field now uses key capture (`QKeySequenceEdit`) instead of manual text input.

## Settings and secrets
- Settings JSON: `%APPDATA%\tts_app\settings.json`
- Secrets: Windows Credential Manager via `keyring`
- JSON never stores provider API keys in plaintext

## Tests
Run:
- `uv run python -m pytest`
- `uv run python scripts/smoke_test.py`

Covered modules:
- SettingsStore
- SecretStore
- HotkeyManager
- VAD
- TextInserter
- Local transcriber
- AudioCapture
- Controller fallback hotkey behavior
- Current test count: 49 passing tests

## Known limitations
- Streaming transcription not implemented (Phase 2 placeholder).
- Remote providers not implemented (placeholder classes only).
- Clipboard restore currently handles Unicode text content only.

## Learning log
### 2026-02-08
- `faster-whisper` model/runtime path can fail with `ModuleNotFoundError: requests` on some environments.
- Fix: add pinned `requests` dependency and improve transcription error message with explicit `uv sync --group dev` guidance.
- Win key combos can fail to register depending on reserved shortcuts. Runtime fallback to a safe hotkey significantly improves startup robustness.
- Hotkey validation in settings dialog prevents storing invalid combinations that would break registration at next launch.
- `Ctrl+Win+LShift` works as configurable hotkey format with RegisterHotKey parsing, but availability still depends on OS shortcut reservations.
- Added stronger hotkey error handling: conflict/registration failures are now surfaced instead of being hidden by idle-state overwrite.
- `huggingface_hub` may warn on Windows if symlinks are not available. This is non-fatal; enabling Windows Developer Mode improves cache efficiency.
- Unit tests with mocks do not reveal OS-level failures like UIPI/SendInput blocking; smoke/runtime checks are required for those paths.
- Existing user settings can preserve old defaults; schema migrations must explicitly rewrite old default values when behavior should change globally.
- `uv run tts-app` executes the installed package entrypoint; after code edits, run `uv sync --group dev` to ensure entrypoint uses latest code.
- Controller now keeps hotkey registration errors visible (no immediate idle overwrite), so registration issues are surfaced to users.
- Hotkey registration errors now include Win32 error details (e.g., 1409 already registered).
- Default hotkey reverted to `Ctrl+Alt+Space` on user request.
- Hotkey assignment changed to key-capture UI (`QKeySequenceEdit`) to avoid manual typing errors.
- Root cause for `SendInput` WinError 87 found: `INPUT` union structure was incomplete, causing wrong struct size (32 instead of 40 on x64).
- Fixed by adding full Win32 `INPUT` union (`MOUSEINPUT`, `KEYBDINPUT`, `HARDWAREINPUT`) and regression test for struct size.
- Before paste, app now attempts best-effort restore of the originally focused target window.
- On insertion failure, transcript is copied to clipboard automatically.
- Overlay detail text is selectable and supports right-click copy; tray menu now has `Copy last transcript`.
- Root cause for stale paste identified: immediate clipboard restore can race with asynchronous paste handling.
- Text inserter now attempts synchronous `WM_PASTE` first and adds a short restore delay only on `SendInput` fallback.
- Added setting `paste_mode` (`auto`, `wm_paste`, `send_input`) and wired it through controller/text inserter.
- Added setting `keep_transcript_in_clipboard` to keep recognized text available for manual paste after each successful transcription.
