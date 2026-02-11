# AGENTS.md

## Purpose
This file is the running project memory for `tts_app`.
Keep it updated whenever behavior, architecture, dependencies, or operational learnings change.
Agents: prefer reading this file first before making changes. It contains critical context about known issues, intentional design decisions, and architecture constraints.

## Quality principle
Quality has the highest priority. Take as much time as needed — every bug is more expensive than finishing a bit later.
- No duplicated logic: every function/constant should exist in exactly one place.
- No dead code or unused imports.
- Every change must pass all existing tests.
- Document decisions in this file so future agents/developers understand why.

## Current scope
- Phase 1 (MVP) implemented: local batch dictation on Windows 11.
- Phase 2a implemented: AssemblyAI as first working remote provider (batch transcription).
- Phase 2 continuing: local streaming mode is implemented as experimental; AssemblyAI streaming (Phase 2b) and other remote providers planned.

## Runtime stack
- Python 3.12
- PySide6 UI/tray/overlay
- Win32 RegisterHotKey + SendInput (no low-level keyboard hook)
- sounddevice for mic capture
- faster-whisper local provider (CTranslate2 Whisper models from HuggingFace Systran)
- assemblyai SDK for AssemblyAI remote provider (Universal-3-Pro + Universal-2)
- keyring for secret storage
- Platform: Windows 11 only (Linux/WSL for dev tooling, not app runtime)

## Architecture overview

### Module responsibilities
- `config.py` — all tunables/constants centralized here; includes `MODEL_REPO_MAP` (single source of truth for model→repo mapping); never hardcode values elsewhere
- `ssl_utils.py` — shared `is_ssl_error()` helper for SSL/Zscaler detection (used by local transcriber, AssemblyAI provider, download script)
- `controller.py` — main orchestrator/state machine (~890 lines); connects hotkey, audio, transcriber, overlay, inserter; model preloading with fallback
- `audio_capture.py` — sounddevice mic recording + optional VAD auto-stop + streaming chunk callback
- `transcriber/local_faster_whisper.py` — batch + streaming transcription via faster-whisper; temp-file based audio input; find_cached_models; preload_model
- `transcriber/assemblyai_provider.py` — batch transcription via AssemblyAI REST API (SDK-based); Phase 2a; test_connection
- `transcriber/factory.py` — creates transcriber instance from settings; routes engine to provider; passes all settings (incl. offline_mode, model_dir) to both primary and fallback branches
- `text_inserter.py` — clipboard-safe paste: save clipboard → set text → paste → restore clipboard
- `overlay_ui.py` — always-on-top frameless overlay with state colors, copy button, scrollable detail
- `hotkey.py` — Win32 RegisterHotKey + Qt native event filter
- `window_focus.py` — capture/compare/restore foreground window + focused control + caret window
- `settings_store.py` — JSON settings with schema migration
- `settings_dialog.py` — PySide6 settings UI (includes model_dir browse, offline mode toggle)
- `secret_store.py` — keyring wrapper for API keys
- `app_paths.py` — %APPDATA% path helpers
- `logger.py` — rotating file logger
- `scripts/download_model.py` — automated model download for offline/corporate use

### Key design decisions
- **Temp files vs BytesIO for audio**: `transcribe_batch` writes WAV bytes to a temp file because `faster-whisper`'s `WhisperModel.transcribe()` accepts file paths (its most reliable input path). BytesIO could work via PyAV but temp files avoid edge cases and are proven stable. Keep as-is.
- **GUITHREADINFO duplication**: defined in both `text_inserter.py` and `window_focus.py`. Intentional — both modules are self-contained and should not depend on each other.
- **Controller size**: ~810 lines including streaming delta logic. Could be split but streaming state is tightly coupled with recording state. Refactoring risk outweighs benefit currently.

## Core flow
1. Global hotkey toggles recording.
2. Overlay moves through states: `Idle → Listening → Processing → Done/Error`.
3. In batch mode, recorded WAV bytes are transcribed once on stop.
4. In streaming mode (local experimental), live chunks are pushed and partial text updates are shown and incrementally inserted during recording.
5. Transcribed text is inserted at caret via clipboard-safe paste.
6. Clipboard text content is restored.

## Text insertion (paste) behavior
- **Auto mode** (default `paste_mode=auto`): tries `SendInput` (Ctrl+V) first, falls back to `WM_PASTE` if SendInput fails.
- **WM_PASTE mode**: sends `WM_PASTE` message directly to the target window's focused control.
- **SendInput mode**: synthesizes Ctrl+V keystrokes via Win32 `SendInput`.
- After `SendInput`, a short delay (`SENDINPUT_RESTORE_DELAY_S=0.16s`) is applied before restoring the clipboard, to avoid race conditions where the target app reads the clipboard asynchronously.
- Insertion target prefers: caret window > focused control > foreground window (captured at recording start).

## Central configuration
All key global defaults are centralized in `src/tts_app/config.py`.
Important defaults:
- `DEFAULT_HOTKEY = "Ctrl+Alt+Space"`
- `FALLBACK_HOTKEY = "Ctrl+Win+LShift"`
- `DEFAULT_MODEL_SIZE = "small"`
- `DEFAULT_ENGINE = "local"`
- `DEFAULT_MODE = "batch"`
- `DEFAULT_OFFLINE_MODE = False`
- `DEFAULT_MODEL_DIR = ""` (empty = standard HF cache)
- `VALID_MODEL_SIZES` includes `distil-large-v3.5` (English-only, ~756 MB, improved), `large-v3-turbo` (multilingual, ~809 MB, pruned large-v3)

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
- Current test count: 131 tests passing
## Known limitations
- Streaming mode currently available for local provider only.
- Streaming partial updates use a trailing audio window for lower latency, but still cost more CPU than batch mode.
- Live insertion in streaming mode is append-oriented and still cannot delete already inserted words when model revisions disagree.
- Streaming auto-abort uses foreground + focused-control + caret signature; it is still best-effort and not a low-level caret hook.
- Remote providers not implemented (placeholder classes only).
- AssemblyAI provider supports batch mode only (streaming planned for Phase 2b).
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
- Text inserter auto mode tries `SendInput` (Ctrl+V) first, falling back to `WM_PASTE` if it fails. A short restore delay is applied after `SendInput` to prevent stale clipboard paste races.
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
- Inserter target handling improved: paste now prefers captured caret/focus handle instead of top-level window for better WM_PASTE fallback behavior.
- Root cause of corporate machine transcription failure: `huggingface_hub` cannot reach the Hub to download the model and no local cache snapshot exists. Improved error message with actionable offline/corporate setup instructions (copy `%USERPROFILE%\.cache\huggingface`, or set `HF_HUB_OFFLINE=1`).
- Consolidated streaming state cleanup in `LocalFasterWhisperTranscriber` via `_reset_stream_fields()` helper, removing duplicated 11-line reset block from `stop_stream` and `abort_stream`.
- Simplified `_play_abort_beep` to avoid redundant duplicate `winsound` import attempts.
- Identified `GUITHREADINFO` struct duplication between `text_inserter.py` and `window_focus.py` (cosmetic; left as-is since both modules are intentionally self-contained).
- Added `offline_mode` setting (`DEFAULT_OFFLINE_MODE = False`) with UI checkbox, wired through settings_store → factory → transcriber; sets `HF_HUB_OFFLINE=1` env var before model load.
- Added comprehensive offline model download section to README.md with direct HuggingFace file download links for all supported models.
- Fixed AGENTS.md: corrected paste-order documentation (auto mode uses SendInput first, not WM_PASTE first), added Architecture overview section, restructured for better agent consumption.
- Fixed streaming abort race condition: worker thread now checks `_stream_abort_requested` inside the main loop under lock before processing each queue item, preventing orphaned workers from running stale transcriptions after abort.
- Made streaming finalization guard safe: after abort, worker only writes `_stream_final_text` if the session is still active (not already reset by `abort_stream`/_reset_stream_fields`).
- Removed Win32 focus-change check from `_on_stream_audio_chunk` (PortAudio callback thread); Win32 API calls from a real-time audio thread violate constraints. Focus-change abort is handled exclusively by `_focus_poll_timer` (Qt main thread, 25ms cadence).
- Removed dead `_stream_tail` method from controller.py (replaced by `_best_stream_finalize_tail`); removed corresponding test.
- Added test `test_offline_mode_sets_hf_hub_offline_env_var` verifying env var is set before model factory call.
- Current test count: 72 tests (69 pass on Linux; 3 are Windows-only: 2 windll/ctypes, 1 INPUT struct size).
- Replaced `HF_HUB_OFFLINE=1` env var hack with WhisperModel's native `local_files_only=True` parameter for offline mode.
- Added `model_dir` setting (all layers: config → settings_store → dialog with Browse button → factory → transcriber) that sets `download_root` on WhisperModel, controlling where HF caches model snapshots.
- Added `distil-large-v3` to `VALID_MODEL_SIZES` — English-only distilled model (~756 MB, 6x faster than large-v3). NOT suitable for German/multilingual dictation.
- Created `scripts/download_model.py` — automated model download script using `huggingface_hub.snapshot_download()` with correct `cache_dir` and `allow_patterns`. Supports `--model`, `--output-dir`, `--all`, `--list`.
- Completely rewrote README offline section: script-based download (primary), manual download (alternative), detailed HF cache structure explanation, model path resolution docs, model recommendation table.
- Key root cause of user's failed offline setup: the old README told users to place files in a flat folder, but `faster-whisper` passes short names through `huggingface_hub.snapshot_download()` which expects HF's internal `models--<org>--<name>/snapshots/<hash>/` structure. Flat folders are only used when the model name is a direct path (os.path.isdir check). The download script now creates the correct structure automatically.
- WhisperModel constructor parameters: `model_size_or_path` (name or path), `download_root` (cache dir), `local_files_only` (no network).
- Current test count: 75 tests (72 pass on Linux; 3 are Windows-only: 2 windll/ctypes, 1 INPUT struct size).
### 2026-02-11
- Added `large-v3-turbo` to VALID_MODEL_SIZES — multilingual (~809 MB), pruned large-v3 (4 decoder layers instead of 32). Much faster than large-v3 with minor quality loss. Already in faster-whisper's `_MODELS` dict as `mobiuslabsgmbh/faster-whisper-large-v3-turbo`.
- Added `distil-large-v3.5` to VALID_MODEL_SIZES — English-only (~756 MB), improved over distil-large-v3 (98k hours training data vs 22k). CTranslate2 version at `distil-whisper/distil-large-v3.5-ct2`. Already in faster-whisper's `_MODELS` dict.
- Researched `nvidia/parakeet-tdt-0.6b-v3`: NOT compatible with faster-whisper. Uses FastConformer-TDT architecture (NeMo framework), not Whisper/CTranslate2. Would require a completely new provider implementation. 25 EU languages, 600M params, excellent WER (DE 5.04%, EN 4.85%) but different inference pipeline.
- Researched AssemblyAI Universal-3 Pro: API-only ($0.21/hour), promptable speech model, 6 languages (EN, ES, DE, FR, PT, IT). Would need new remote provider implementation (assemblyai SDK). Not implemented — noted for Phase 2 provider work.
- Added comprehensive HF cache documentation to README: per-user persistence, cache structure, custom Model Dir, download script behavior, offline transfer best practice.
- faster-whisper `_MODELS` dict (utils.py) already contains: `"large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"`, `"turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"`, `"distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2"`.
- Current test count: 75 tests (72 pass on Linux; 3 are Windows-only: 2 windll/ctypes, 1 INPUT struct size).
- Removed `distil-large-v3` from VALID_MODEL_SIZES — superseded by `distil-large-v3.5` (strictly better: 98k vs 22k training hours).
- Rewrote README "How model paths work" section for clarity (user found original 3-step explanation incomprehensible).
- Fixed README download script commands: prominent `uv run python` syntax, explicit notes about `uv` vs plain python vs venv.
- **Implemented AssemblyAI as first working remote provider (Phase 2a):**
  - New module `transcriber/assemblyai_provider.py`: batch transcription via `assemblyai` SDK, Universal-3-Pro + Universal-2 models, auto language detection.
  - Added `"assemblyai"` to `VALID_ENGINES` in config.py.
  - Added `has_assemblyai_key` to `AppSettings` + `DEFAULTS` in settings_store.py.
  - Added AssemblyAI API key field + engine label in settings_dialog.py.
  - Updated factory.py to route `engine="assemblyai"` → `AssemblyAITranscriber`; factory now accepts optional `secret_store` parameter.
  - Updated controller.py: removed "Remote providers planned for Phase 2" block; added `secret_store` parameter; streaming-only check for non-local providers.
  - Updated main.py to pass `secret_store` to controller.
  - Added `assemblyai>=0.37.0` to pyproject.toml dependencies.
  - 27 new tests in `test_assemblyai_provider.py` (constructor, batch, errors, language config, streaming stubs, factory routing, settings).
  - Updated spec sheet (stt-dictation-spec.md) with Phase 2a/2b/3 details including AssemblyAI and Parakeet/NeMo.
- Current test count: 102 tests (99 pass on Linux; 3 are Windows-only: 2 windll/ctypes, 1 INPUT struct size).
### 2026-02-12
- **SSL/Zscaler error detection added throughout the app:**
  - New `_is_ssl_error()` helper in `local_faster_whisper.py` and `assemblyai_provider.py` — walks exception chain (`__cause__`) to detect SSL certificate verification failures.
  - `download_model.py` now catches SSL errors from `snapshot_download()` and prints actionable Zscaler/corporate-proxy guidance (CA bundle, alternative download methods, docs link).
  - `_format_transcription_error()` in local transcriber now detects SSL errors and suggests `docs/offline-usage-guide.md`.
  - `AssemblyAITranscriber.transcribe_batch()` now detects SSL errors and raises with actionable message.
  - Root cause: corporate proxies (Zscaler) intercept HTTPS and replace SSL certs; Python's SSL library rejects the proxy's cert → `[SSL: CERTIFICATE_VERIFY_FAILED]`.
- **Comprehensive offline usage guide created: `docs/offline-usage-guide.md`**
  - Three download methods: download script, git clone, manual browser download.
  - SSL/Zscaler troubleshooting section with 4 fix methods (CA bundle, browser export, certifi injection, download on unrestricted machine).
  - Model transfer instructions (USB, network share, HF cache copy).
  - App configuration for offline use (settings UI, JSON, env var).
- **`find_cached_models()` function added to `local_faster_whisper.py`:**
  - Scans HF cache (default + custom model_dir) for locally available models.
  - Checks both HF-style cache structure (`models--<org>--<name>/snapshots/<hash>/`) and flat directories (`faster-whisper-<model>/`).
  - Returns list of model short names in canonical order from `VALID_MODEL_SIZES`.
  - Exported via `transcriber/__init__.py`.
- **Model preloading at startup:**
  - `controller.initialize()` now triggers background model preload for local engine.
  - Overlay shows "Loading model..." during preload.
  - On success: shows idle status; on failure: attempts fallback to any cached model (prefers `tiny`).
  - Fallback shows warning overlay with available model list.
  - If no models found at all: shows error with docs reference.
  - New `FALLBACK_MODEL = "tiny"` constant in `config.py`.
  - `preload_model()` and `is_model_loaded` property added to `LocalFasterWhisperTranscriber`.
- **AssemblyAI connection test:**
  - `test_connection()` method on `AssemblyAITranscriber`: makes lightweight GET request to `/v2/transcript?limit=1` to validate API key and connectivity.
  - Returns `(success, message)` tuple; detects auth failures (401), SSL errors, and general connection errors.
- **Settings dialog improvements:**
  - "Test Connection" button for remote providers — tests API key validity and network connectivity, detects SSL/Zscaler errors.
  - "Local Models" info box showing currently cached models or guidance when none found.
  - `find_cached_models()` used for model scanning.
- **README fixes:**
  - Translated German "How model loading works" section and download blockquote to English.
  - Expanded "How model loading works" with: path resolution order (3-step), offline mode activation (UI + env var), Model Dir configuration (UI + JSON), special cases section.
  - Added SSL/Zscaler troubleshooting entry to Troubleshooting section with link to offline guide.
- **New test file `tests/test_ssl_and_preload.py` (23 tests):**
  - `_is_ssl_error()` detection (direct, chained, negative cases).
  - `_format_transcription_error()` SSL path.
  - `find_cached_models()` (HF cache, custom dir, flat dirs, incomplete models, multiple models).
  - `preload_model()` (success, failure, idempotency).
  - AssemblyAI SSL detection in `transcribe_batch()`.
  - `test_connection()` (success, auth failure, SSL error).
- **3 new controller tests** for preload: local engine triggers preload, remote engine skips preload, preload done handler behavior.
- Tiny model (~75 MB) not bundled in repo — too large for git; instead, preload logic auto-downloads on first start and falls back to any available cached model.
- Current test count: 128 tests (125 pass on Linux; 3 are Windows-only: 2 windll/ctypes, 1 INPUT struct size).
### 2026-02-13
- **Code quality review and deduplication:**
  - Extracted `_is_ssl_error()` into shared `ssl_utils.py` module — was identically duplicated in `local_faster_whisper.py`, `assemblyai_provider.py`, and `download_model.py` (3 copies → 1).
  - Moved `MODEL_REPO_MAP` to `config.py` as single source of truth — was duplicated as `_MODEL_REPO_MAP` in `local_faster_whisper.py` and `MODELS` in `download_model.py` (2 copies → 1).
  - Fixed **bug** in `_print_ssl_help()` in `download_model.py`: hardcoded `Systran/faster-whisper-{model_name}` for ALL models, but `large-v3-turbo` is under `mobiuslabsgmbh/` and `distil-large-v3.5` is under `distil-whisper/`. Now uses actual repo ID from `MODEL_REPO_MAP`.
  - Fixed `factory.py` fallback branch: was missing `offline_mode` and `model_dir` parameters, causing the fallback to ignore user's offline/model-dir settings.
  - Removed unnecessary `getattr()` calls in `factory.py` and `settings_dialog.py` — `AppSettings` always has `offline_mode` and `model_dir` since schema v5+.
  - Removed unused imports: `import re` in `local_faster_whisper.py`, `from urllib.request import Request, urlopen` in `assemblyai_provider.py`, `import ssl` in `assemblyai_provider.test_connection()`.
  - `download_model.py` now imports `MODEL_REPO_MAP` from `config.py` and `is_ssl_error` from `ssl_utils.py` via `sys.path` adjustment (script lives outside `src/`).
- Model directory naming: HF-style (`models--Systran--faster-whisper-small/snapshots/<hash>/`) works automatically with short names. Flat dirs (`faster-whisper-small/`) only work when the full path is passed as `model_size_or_path`. The download script creates HF structure automatically.
- Current test count: 128 tests (125 pass on Linux; 3 are Windows-only: 2 windll/ctypes, 1 INPUT struct size).
### 2026-02-11 (critical review pass)
- Split background executors in `controller.py`: preload now runs on dedicated `_preload_executor`, while dictation/transcription remains on `_executor`. This removes queue-blocking where model preload could delay first real transcription task.
- Added transcriber cache lock in controller (`_transcriber_cache_lock`) to avoid race conditions when preload and normal transcription request the transcriber concurrently.
- Fallback model chosen during preload is now persisted to `settings.json` via `SettingsStore.save()`, so the app does not retry the failing model on every restart.
- Made `_ensure_model()` in `LocalFasterWhisperTranscriber` thread-safe via `_model_lock`; prevents duplicate concurrent model construction.
- Improved offline/corporate error classification: HuggingFace hub detection in `_format_transcription_error()` is now case-insensitive and recognizes `LocalEntryNotFoundError`.
- Settings dialog local-model indicator now refreshes immediately when `model_dir` text changes.
- Added regression tests:
  - `test_preload_worker_persists_fallback_model`
  - `test_controller_initialize_local_uses_preload_executor_only`
  - `test_hub_error_message_is_case_insensitive`
- Validation after review: `uv run python -m pytest` → `131 passed`; `uv run python scripts/smoke_test.py` passed.
