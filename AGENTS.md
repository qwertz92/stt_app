# AGENTS.md

## Purpose
This file is the running project memory for `tts_app`.
Keep it updated whenever behavior, architecture, dependencies, or operational learnings change.

## Current scope
- Phase 1 (MVP) implemented: local batch dictation on Windows 11.
- Phase 2 in progress: provider selection and API key fields are present; local streaming mode is now implemented as experimental.

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
3. In batch mode, recorded WAV bytes are transcribed once on stop.
4. In streaming mode (local experimental), live chunks are pushed and partial text updates are shown and incrementally inserted during recording.
5. Transcribed text is inserted at caret via clipboard-safe paste.
6. Clipboard text content is restored.

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
- No-uv fallback commands:
- `python -m pytest`
- `python scripts/smoke_test.py`

Covered modules:
- SettingsStore
- SecretStore
- HotkeyManager
- VAD
- TextInserter
- Local transcriber
- AudioCapture
- Controller fallback hotkey behavior
- Streaming mode controller/transcriber behavior
- Streaming auto-abort on focus change + beep notification
- Benchmark script CSV output helpers
- Current test count: 70 passing tests

## Known limitations
- Streaming mode currently available for local provider only.
- Streaming partial updates use a trailing audio window for lower latency, but still cost more CPU than batch mode.
- Live insertion in streaming mode is append-oriented and still cannot delete already inserted words when model revisions disagree.
- Streaming auto-abort uses foreground + focused-control signature; it is still best-effort and not a low-level caret hook.
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
- In corporate environments, `uv.exe` can be blocked by Group Policy/AppLocker; native Python + pip setup is required as fallback.
- Added `requirements-win.txt` and `requirements-dev-win.txt` for no-uv installation flow.
- Added `pywin32` platform marker in `pyproject.toml`, so non-Windows environments (e.g. WSL Linux) can resolve dependencies without failing on Windows-only wheels.
- WSL can help development tooling, but the full app runtime (hotkey/input insertion) must run on native Windows.
### 2026-02-09
- Added detailed enterprise deployment runbook at `docs/enterprise-deployment-guide.md` (no-uv setup, wheelhouse/offline flow, PyInstaller distribution notes).
- For locked corporate environments, safest practice is pinning pip inside the project venv (e.g. `pip<26`) instead of updating globally.
- Added local benchmarking script `scripts/benchmark_local.py` with per-model/device/compute-type timing, RTF output, and optional JSON report.
- Added model and benchmarking documentation at `docs/local-models-and-benchmark.md` (wheels, model choices, Intel iGPU behavior, upstream benchmark links).
- Implemented local streaming mode (experimental): controller now starts/stops transcriber streams, pushes audio chunks, and shows partial overlay text during recording.
- Added audio chunk callback plumbing in `AudioCapture` and local transcriber stream buffering/finalization in `LocalFasterWhisperTranscriber`.
- Added benchmark improvements: CSV export (`--csv-out`) and console comparison view for best latency/RTF.
- Added sample benchmark audio generation script `scripts/generate_sample_audio.py` and committed `samples/benchmark_sample.wav`.
- Added benchmark-model error-rate references from upstream sources (Whisper paper tables + faster-whisper benchmark WER snippet) in docs.
- Added implementation note doc `docs/streaming-mode.md` describing architecture, tradeoffs, and default-mode recommendation.
- Test stability learning: mixing `QCoreApplication` and widget tests can crash on Windows; use `QApplication` consistently for controller tests when widget dialogs are also tested.
### 2026-02-10
- Streaming mode now performs incremental live insertion at caret while speaking and only inserts remaining tail on finalize.
- Streaming session now auto-aborts when target foreground window changes and triggers a short alert beep.
- Added docs `docs/model-error-rate-reference.md` with curated published WER references for offered models and language examples.
- Benchmark script now supports isolated per-case execution (`--isolated-case`, default on) for better Ctrl+C interruption behavior on Windows.
- Added benchmark docs for parameter-by-parameter meaning and why model load/download dominates runtime.
- Fixed streaming finalization logic to avoid "mismatch -> copy full transcript to clipboard" behavior; finalization now appends only detected tail.
- Added fast stream abort path (`abort_stream`) so focus-change abort and beep are immediate and not blocked by expensive final re-transcription.
- Improved streaming delta detection with word-overlap fallback, reducing cases where partial inserts were dropped due strict prefix mismatch.
- Abort beep now tries explicit `winsound.Beep(900, 120)` first, then falls back to `MessageBeep`/Qt beep.
- Overlay now includes a dedicated `Copy` button so users can copy text without selection/context-menu steps.
- Streaming focus-abort detection now polls every 25ms and compares foreground + focus + caret window signatures for faster cursor/focus-change abort.
- Abort beep is triggered immediately on abort request (before transcriber teardown), reducing perceived notification latency.
- Streaming live insertion now uses stable-prefix commit with trailing-word guard and suffix/prefix overlap reconciliation to avoid "stops after first inserts" behavior.
- Final streaming tail now scores candidates (`final`, `last_partial`) and prefers the one that best extends committed text, reducing bad corrections at finalize.
- Streaming partial decoding now uses a trailing audio window (`STREAMING_PARTIAL_WINDOW_S`) so partial latency does not grow linearly with utterance length.
- Default streaming cadence tuned for lower latency: `STREAMING_PARTIAL_INTERVAL_S=0.35`, `STREAMING_PARTIAL_MIN_AUDIO_S=0.25`.
- Overlay copy UX improved: visible pressed/copy feedback, "Copied" state, and best-effort restoration of previously focused external window.
- Overlay detail area now grows with transcript content up to `OVERLAY_MAX_HEIGHT` (4x base) and then becomes scrollable.
- Added regression tests for focused-control abort, partial-stability delta computation, and finalize-tail fallback.
- Added controller regression test for "continues inserting after partial revisions" to catch the prior stall-after-first-inserts behavior.
