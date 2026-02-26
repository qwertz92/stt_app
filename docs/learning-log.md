# Learning Log

Project history, decisions, and operational learnings. Referenced by `AGENTS.md`.
Agents and developers: use this as a knowledge base for past issues and solutions.

## 2026-02-08

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

## 2026-02-09

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

## 2026-02-10

- Streaming mode now performs incremental live insertion at caret while speaking and only inserts remaining tail on finalize.
- Streaming session now auto-aborts when target foreground window changes and triggers a short alert beep.
- Benchmark script now supports isolated per-case execution (`--isolated-case`, default on) for better Ctrl+C interruption behavior on Windows.
- Fixed streaming finalization logic to avoid "mismatch -> copy full transcript to clipboard" behavior; finalization now appends only detected tail.
- Added fast stream abort path (`abort_stream`) so focus-change abort and beep are immediate and not blocked by expensive final re-transcription.
- Improved streaming delta detection with word-overlap fallback, reducing cases where partial inserts were dropped due strict prefix mismatch.
- Streaming live insertion now uses stable-prefix commit with trailing-word guard and suffix/prefix overlap reconciliation to avoid "stops after first inserts" behavior.
- Final streaming tail now scores candidates (`final`, `last_partial`) and prefers the one that best extends committed text, reducing bad corrections at finalize.
- Streaming partial decoding now uses a trailing audio window (`STREAMING_PARTIAL_WINDOW_S`) so partial latency does not grow linearly with utterance length.
- Root cause of corporate machine transcription failure: `huggingface_hub` cannot reach the Hub to download the model and no local cache snapshot exists.
- Fixed streaming abort race condition: worker thread now checks `_stream_abort_requested` inside the main loop under lock before processing each queue item.
- Removed Win32 focus-change check from `_on_stream_audio_chunk` (PortAudio callback thread); Win32 API calls from a real-time audio thread violate constraints.
- Added `offline_mode` setting with UI checkbox, wired through settings_store → factory → transcriber.
- Replaced `HF_HUB_OFFLINE=1` env var hack with WhisperModel's native `local_files_only=True` parameter for offline mode.
- Added `model_dir` setting (config → settings_store → dialog with Browse button → factory → transcriber).
- Created `scripts/download_model.py` — automated model download script using `huggingface_hub.snapshot_download()`.
- Key root cause of user's failed offline setup: the old README told users to place files in a flat folder, but `faster-whisper` expects HF's internal `models--<org>--<name>/snapshots/<hash>/` structure.

## 2026-02-11

- Added `large-v3-turbo` and `distil-large-v3.5` to VALID_MODEL_SIZES.
- Removed `distil-large-v3` — superseded by `distil-large-v3.5` (strictly better).
- Researched `nvidia/parakeet-tdt-0.6b-v3`: NOT compatible with faster-whisper (FastConformer-TDT, NeMo framework).
- **Implemented AssemblyAI as first working remote provider:**
  - New module `transcriber/assemblyai_provider.py`: batch transcription via `assemblyai` SDK.
  - Factory routing, settings store, settings dialog updates, 27 new tests.
- Split background executors in `controller.py`: preload now runs on dedicated `_preload_executor`.
- Added transcriber cache lock to avoid race conditions during concurrent preload/transcription.
- Fallback model chosen during preload is now persisted to `settings.json`.
- Made `_ensure_model()` thread-safe via `_model_lock`.

## 2026-02-12

- **SSL/Zscaler error detection added:** `ssl_utils.py` shared helper, used in local transcriber, AssemblyAI, download script.
- Root cause: corporate proxies (Zscaler) intercept HTTPS → `[SSL: CERTIFICATE_VERIFY_FAILED]`.
- Created `docs/offline-usage-guide.md` with SSL troubleshooting.
- Added `find_cached_models()` to scan for locally available models.
- Added model preloading at startup with fallback to any cached model.
- Added `test_connection()` for AssemblyAI provider.
- Settings dialog: "Test Connection" button, "Local Models" info box.

## 2026-02-13

- **Code quality review and deduplication:**
  - Extracted `_is_ssl_error()` into shared `ssl_utils.py` (was 3 copies → 1).
  - Moved `MODEL_REPO_MAP` to `config.py` (was 2 copies → 1).
  - Fixed bug in `_print_ssl_help()`: hardcoded repo path for all models.
  - Fixed `factory.py` fallback branch: was missing `offline_mode` and `model_dir`.

## 2026-02-16

- **Documentation overhaul:** English-only language rule, translated enterprise guide, created quick-start.md.
- Created `scripts/import_model.py` for importing manually downloaded models.
- **Test coverage overhaul (74% → 80%):** 52 new tests across 6 files.

## 2026-02-17

- **Git LFS pointer detection in `import_model.py`:**
  - Root cause: `git clone` without `git-lfs` produces small (~135 bytes) LFS pointer files → CTranslate2 error `Unsupported model binary version v1936876918`.
  - Added `is_lfs_pointer(path)` function and minimum size check (`_MODEL_BIN_MIN_BYTES = 10 MB`).
- **Benchmark script improvements:** Separated download time from load time via `_ensure_models_available()`.
- **Settings dialog model picker:** Downloaded models (✓) above separator, undownloaded below.
- Git LFS requirement warning added to `docs/models.md`.

## 2026-02-20

- **AGENTS.md refactored:** Extracted learning log to `docs/learning-log.md` to reduce context window usage.
- **Groq provider implemented:** `transcriber/groq_provider.py` with whisper-large-v3 and whisper-large-v3-turbo models.
- **Git LFS documentation improved:** Installation instructions for Ubuntu and Windows, manual download alternatives.
- **Benchmark download confirmation:** User is now asked before downloading uncached models.
- **Settings dialog overhaul:** Tabs for Local/Remote, save confirmation status bar, provider activation/testing dialog.

## 2026-02-21

- **AssemblyAI streaming implemented:** Real-time transcription via `aai.RealtimeTranscriber` (WebSocket).
  - `start_stream` connects to AssemblyAI's real-time API and registers data/error callbacks.
  - `push_audio_chunk` forwards raw PCM16 audio to the WebSocket.
  - `stop_stream` closes connection and returns accumulated final + partial text.
  - `abort_stream` closes connection immediately and discards all text.
  - Accumulated text: all `FinalTranscript` segments + current `PartialTranscript`, combined for on_partial callback.
- **`STREAMING_ENGINES` constant added to `config.py`:** `("local", "assemblyai")` — engines that support streaming mode.
- Controller streaming guard updated: was `engine != DEFAULT_ENGINE` → now `engine not in STREAMING_ENGINES`.
- **Code review finding:** Groq integration pattern (config → settings → factory → provider → UI) is the correct abstraction level. Each provider touches ~5 predictable locations — a registry/base pattern would add complexity without reducing touchpoints. Not recommended to refactor.
- 15 new streaming tests in `test_assemblyai_provider.py` (replaced 4 stub tests).
- Total tests: ~240 (Linux: all pass except 3 Windows-only ctypes/windll tests).
- Removed unimplemented OpenAI/Azure runtime placeholders and hid them from settings UI; `VALID_ENGINES` now includes only implemented engines (`local`, `assemblyai`, `groq`, `deepgram`).
- Settings dialog connection tests now run asynchronously in a background thread to keep UI responsive during network checks.
- Added settings migration cleanup for legacy `has_openai_key` / `has_azure_key` flags and legacy unimplemented engine values.
- Added focused settings-dialog tests for async connection behavior and stale-result handling.
- Implemented `OpenAITranscriber` with batch transcription (`/v1/audio/transcriptions`), connection test (`/v1/models/{model}`), and chunked streaming support via the existing provider streaming interface.
- Re-enabled OpenAI in runtime config/UI/settings (`VALID_ENGINES`, OpenAI API key storage, OpenAI model selection).
- Implemented Deepgram provider-native streaming via WebSocket (`wss://api.deepgram.com/v1/listen`) with partial/final transcript merging.
- Expanded provider test coverage (`test_openai_provider.py`, deepgram streaming tests, settings-store OpenAI model migration/validation tests).
- Removed NeMo/Parakeet provider and optional dependencies after final product decision against NVIDIA-only runtime paths.
- Simplified settings persistence: removed legacy migration code and old compatibility rewrites; settings now use direct validation + normalization.
- Removed OpenAI chunked pseudo-streaming; OpenAI is now batch-only while streaming remains local, AssemblyAI, and Deepgram.
- Improved controller transcriber cache invalidation on settings reload and expanded cache key to include provider model selections.
- Synced project docs to current runtime behavior (no roadmap-only features in user-facing docs).
- Restored `docs/parakeet-evaluation.md` as an explicit architecture decision record (kept out of runtime scope but retained for future context).
- Added `docs/provider-costs.md` with cross-provider pricing comparison and billing caveats.
- Added `ruff` to dev requirements for non-`uv` environments (`requirements-dev-win.txt`) to keep lint tooling available everywhere.

## 2026-02-22

- **Comprehensive code review** of entire repository (all source files, tests, scripts, docs).
- **Bug fix: `import_model.py` partial matching** — `detect_model_name()` now sorts `_FOLDER_HINTS` longest-first to prevent "large-v3" matching before "large-v3-turbo".
- **Bug fix: `local_faster_whisper.py` thread safety** — `_maybe_emit_partial()` now holds `_stream_lock` when setting `_stream_error`.
- **Bug fix: `settings_dialog.py` save behavior** — `_save()` now calls `self.accept()` to close the dialog, ensuring controller reloads settings. Removed unused `save_status` label and timer.
- **Naming fix:** `APP_DISPLAY_NAME` changed from "TTS Dictation App" to "Voice Dictation App" in `config.py`.
- **Test refactoring:** Extracted shared controller test fakes/fixtures into `tests/conftest.py` (~150 lines deduplication). Moved misplaced benchmark tests from `test_import_model.py` to `test_benchmark_script.py`.
- **Fixed 2 Linux test failures:** Added missing `window_focus_helper=FakeWindowFocusHelper()` to two controller tests.
- **Dependency cleanup:** Removed unused `requests` from `pyproject.toml` (transitive via assemblyai SDK). Added `pytest-cov` to `[project.optional-dependencies]`.
- **Documentation updates:** Engine tables in README, quick-start, streaming-mode now list all 5 engines.
- **AGENTS.md trimmed:** Removed sections obvious from code (Text insertion details, Configuration defaults, per-module `test_connection` notes, trivial modules). Updated test count to 305 (1 Windows-only failure on Linux, down from 3).

## 2026-02-24

- **Copy button freeze fix:** Root cause was `_restore_external_foreground_window()` after clipboard copy — calling `SetForegroundWindow` on Windows with `WS_EX_NOACTIVATE` overlay makes the overlay lose all mouse input. Removed focus restoration from `copy_detail_text()` and all related dead code (`_remember_external_foreground_window`, `_restore_external_foreground_window`, `_get_foreground_window`, `_set_foreground_window`). Added try/except around clipboard operations.
- **Local model switch fix:** Added `on_settings_changed()` method to controller that re-triggers model preload when switching back to local engine. Updated `open_settings_dialog()` in main.py to call it. Previously, switching from remote to local didn't preload the model, causing delayed first transcription.
- **SSL/Zscaler documentation overhaul:** Expanded `docs/advanced-setup.md` SSL section with step-by-step combined CA bundle creation, DER-to-PEM conversion, permanent env var setup, and clear scope notes (all remote providers affected, not just model download).
- 5 new tests: `test_overlay_copy_button_stays_functional_after_repeated_clicks`, `test_overlay_copy_button_survives_clipboard_error`, `test_on_settings_changed_preloads_for_local_engine`, `test_on_settings_changed_skips_preload_for_remote_engine`. Total: 310.

### Session 3

- **Clipboard default fix:** Changed `DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD` from `True` to `False` in `config.py`. The transcript was always ending up in the clipboard because the default was opt-out instead of opt-in.
- **Settings dialog stays open on save:** Removed `self.accept()` from `_save()`. Save now shows a "✓ Settings saved" status label (auto-clears after 3 seconds) and emits a `settings_changed` signal. Button label changed from "Cancel" to "Close". `main.py` connects `settings_changed` signal to `controller.on_settings_changed()` instead of checking for `Accepted` result.
- **Tray icon double-click opens settings:** Connected `tray_icon.activated` signal — double-click opens the Settings dialog.
- **Tab styling improvement:** Added QTabBar stylesheet to settings dialog with distinct `::tab:selected` (white background, blue bottom border, bold font) vs `::tab:hover:!selected` (light blue) states.
- **Overlay single-click copy fix:** Added `nativeEvent` override to `OverlayUI` that intercepts `WM_MOUSEACTIVATE` on Windows and returns `MA_NOACTIVATE`. This prevents the OS from activating the overlay window on first click, allowing the copy button to respond immediately.
- 6 new tests: `test_save_emits_settings_changed_signal`, `test_save_shows_status_feedback`, `test_settings_dialog_has_tab_stylesheet`, `test_overlay_has_native_event_override`, `test_tray_double_click_connected`, `test_keep_transcript_in_clipboard_defaults_to_false`. Total: 316.

### Session 3b — SSL fix

- **Root cause of SSL/Zscaler failure:** The Groq SDK uses `httpx` (not `requests`). `httpx` does **not** read `REQUESTS_CA_BUNDLE` and does not reliably honour `SSL_CERT_FILE`. Similarly, OpenAI/Deepgram providers use `urllib.request` which only reads `SSL_CERT_FILE` via Python's `ssl` module.
- **Fix:** Added `resolve_ca_bundle()` and `create_ssl_context()` to `ssl_utils.py`. These check both `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` env vars and return an explicit SSL context.
- **Groq provider:** `_build_client()` now passes `httpx.Client(verify=<SSLContext>)` when a custom CA bundle is detected.
- **OpenAI provider:** All `urlopen()` calls now pass `context=create_ssl_context()` explicitly.
- **Deepgram provider:** Same as OpenAI — all `urlopen()` calls pass `context=create_ssl_context()`.
- **AssemblyAI provider:** Already worked because the `assemblyai` SDK uses `requests` internally, which reads `REQUESTS_CA_BUNDLE`.
- 10 new tests: `TestResolveCABundle` (6 tests), `TestCreateSSLContext` (2 tests), `TestGroqSSLBundle` (2 tests). Total: 326.
