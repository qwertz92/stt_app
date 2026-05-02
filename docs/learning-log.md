# Learning Log

Project history, decisions, and operational learnings. Referenced by `AGENTS.md`.
Agents and developers: use this as a knowledge base for past issues and solutions.

## 2026-05-02

- Streaming availability now uses a shared `config.supports_streaming()` helper
  instead of duplicating partial checks in the settings UI and controller.
  This fixes a case where selecting a batch-only local ONNX/WebGPU model could
  incorrectly disable streaming for remote providers that do support it.
- The controller now rejects invalid local ONNX/WebGPU streaming settings before
  creating a transcriber, so corrupt or stale settings fail with a clear
  batch-mode-only message.
- Streaming finalization now snapshots the stream settings before submitting
  the background stop worker. Queued final results keep the model/engine that
  actually produced the transcript even if active stream state is cleared before
  the Qt result signal is handled.
- Quick-start and streaming docs were aligned with the current UI: Import Audio
  no longer has a confirmation prompt, and ONNX/WebGPU local models are
  documented as batch-only.
- Release builds now fail fast when a `v*` tag does not match the version in
  `pyproject.toml`, and tests keep `stt_app.__version__` aligned with that
  project metadata.

## 2026-04-29

- Local/Benchmark tab model inventory refresh is now deferred briefly after tab
  selection. This lets the tab paint immediately and then starts the background
  availability scan, while any cached model inventory stays visible.
- The Local tab "Download Selected" action now disables itself when every
  selected model is already downloaded. Mixed selections still allow downloading
  the missing models, and downloaded selections can still be deleted.
- Transcript history retention was raised from 20 to 500 entries by default.
  Existing settings files that still carry the old 20-entry default are migrated
  upward so normal daily dictation does not silently prune most entries.
- Successful transcriptions are now appended to history before text insertion.
  If focus or paste insertion fails, the transcript remains available in history
  and the last recording is finalized instead of being left in a transcribing
  state.
- History model names are covered by a snapshot regression test so entries keep
  the model that actually produced the transcript, even if current settings
  change before the result is handled.
- Windows release docs now distinguish the local portable-bundle build script
  from the installer build script. The GitHub Action runs both scripts and then
  uploads the ZIP, installer, and expanded bundle as one workflow artifact.

## 2026-04-28

- Settings density was tightened again after the tab layout grew too loose:
  history, local-model, benchmark-model, benchmark-result, and standalone
  history rows now use explicit compact row heights instead of relying on
  platform style defaults.
- The embedded Settings -> History transcript box now expands with the dialog
  instead of keeping a small fixed-feeling scroll area and leaving blank space
  below it.
- Settings dialog first-show sizing is computed before the window is shown.
  This avoids the visible show-resize-present sequence that looked like the
  dialog briefly disappeared on first open from the tray.
- Combo popup animation effects are disabled for the settings dialog to reduce
  flicker when opening dropdowns on Windows.
- Local ONNX/WebGPU transcription now reports the actual resolved runtime
  device through progress messages. Normal dictation shows it in the overlay;
  Import Audio shows it in the import progress label.
- "Use last recording" now considers the configured archived recordings folder
  when recording archival is enabled, while still preferring a recoverable
  managed last recording so retry/recovery state is not lost.

## 2026-04-21

- Benchmark audio selection now starts in the effective recordings directory,
  matching the folder used for archived normal recordings.
- Opening Settings from the tray now presents the dialog immediately after
  creation. On Windows, a newly shown tray-launched window can otherwise stay
  behind other windows until the next activation path raises it.
- AssemblyAI pre-recorded import now uses the current `speech_models` request
  parameter. The old `speech_model` parameter caused API failures for legacy
  "best"/"nano" selections after AssemblyAI deprecated that field.
- The Import Audio tab now starts transcription immediately without a
  confirmation prompt, shows remote-provider progress, and puts failures in
  the selectable result text area so errors can be copied.
- Windows reports AltGr as Ctrl+Alt. The hotkey manager now ignores Ctrl+Alt
  hotkey messages while right Alt is down so AltGr combinations do not start
  dictation accidentally.

## 2026-04-18

- **Local ASR candidates were re-evaluated against the app's Windows/Intel GPU
  goals:**
  - Added `docs/local-asr-model-candidates-2026.md` as the canonical evaluation
    for Cohere Transcribe, NVIDIA Parakeet, IBM Granite Speech, and adjacent
    2026 ASR candidates.
  - Updated the older Cohere and Parakeet notes to point at the canonical
    evaluation instead of duplicating model/runtime analysis.
  - Key conclusion: keep `faster-whisper`/CTranslate2 as the production local
    engine for now. Cohere and Granite are worth an isolated ONNX/WebGPU
    benchmark on the user's Intel GPU, but they are not drop-in CTranslate2
    models.
  - Official Parakeet through NeMo remains out of scope because its strongest
    path is NVIDIA-centered and does not solve the Intel GPU requirement.
- **Experimental Cohere/Granite local ASR was integrated behind the local model
  selector:**
  - Added `cohere-transcribe-03-2026` and `granite-4.0-1b-speech` to the local
    model catalog with a separate q4 ONNX/WebGPU runtime.
  - Added a persistent Transformers.js helper process for batch transcription,
    automatic GPU selection, and CPU fallback warnings.
  - Kept these models batch-only and disabled Auto language mode because the
    app currently sends explicit German/English language hints to this runtime.
  - Left NVIDIA Parakeet unimplemented because the practical local path remains
    NeMo/PyTorch and would add a heavier, NVIDIA-oriented runtime.
- **Local model UX now distinguishes runtime classes:**
  - The Settings dialog labels Cohere/Granite as ONNX/WebGPU models, disables
    streaming for them, and shows a red CPU fallback warning under the
    model selector.
  - Local model scanning and downloads now include both CTranslate2 and q4
    ONNX/WebGPU snapshots while keeping manual import CTranslate2-only.
  - ONNX/WebGPU downloads use a symlink-free local folder layout so Windows
    systems without Developer Mode/admin symlink privileges do not fail with
    `WinError 1314`.
  - Experimental ONNX/WebGPU models are not preloaded at app startup to avoid
    surprise CPU load on machines where a GPU runtime is not selected.
  - Transformers.js v4 on Node does not accept `wasm` as a device. Auto device
    selection now tries WebGPU, then Windows DirectML, then CPU.
  - WebGPU is attempted even when Node's `navigator.gpu` adapter probe returns
    false; explicit WebGPU can still work through the Transformers.js backend.
  - ONNX helper processes are not cached after normal dictation, so they cannot
    keep consuming CPU while idle after one experimental transcription.
  - An expert keep-loaded setting can keep the last ONNX helper warm after
    dictation, and shutdown/settings changes close the cached helper.
  - Benchmark startup and preload failures close their ONNX helper process to
    avoid orphaned Node processes holding RAM or GPU memory.
  - Benchmarking can run Cohere/Granite on Auto, GPU-only, CPU-only, DirectML,
    WebGPU, or GPU+CPU comparison targets and now shows the resolved device.
  - The ONNX runner decodes WAV input directly because Transformers.js cannot
    use browser `AudioContext` path loading in Node.
  - Cohere's Transformers.js ASR pipeline chunks long audio internally. Granite
    now gets app-side quiet-boundary chunking before generation to avoid one
    giant prompt/audio feature block for long recordings.
  - `auto` can fall back from a GPU runtime to CPU during transcription if an
    ONNX operator fails after the model loaded successfully.
  - `gpu` can fall back between GPU runtimes during transcription, but never
    falls back to CPU.
  - Granite keeps automatic language mode generic; Cohere maps Auto to German
    because its ONNX path requires an explicit language.
  - Qwen3-ASR 0.6B/1.7B community ONNX and GGUF packages exist, but were not
    implemented because they require custom runtime code and do not currently
    show a clear app-specific quality/speed win over Cohere/Granite.
  - App startup now uses a single-instance lock to avoid duplicate tray/overlay
    processes competing for hotkeys and background work.
- **Runtime packaging hooks were added:**
  - Added `package.json`/`package-lock.json` for `@huggingface/transformers`.
  - Included the JavaScript runner in wheel/PyInstaller data files and include
    `node_modules` in packaged builds when available.
  - Source checkouts try to install missing JavaScript dependencies on first
    ONNX use instead of requiring a manual `npm install` upfront.
- **Test coverage was added for the new path:**
  - Factory routing, settings persistence, Settings dialog model constraints,
    WebGPU snapshot detection, q4 download filters, and provider request/cleanup
    behavior now have regression tests.

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
- `uv run stt-app` executes the installed package entrypoint; after code edits, run `uv sync --group dev` to ensure entrypoint uses latest code.
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

## 2026-04-08

- Optimized `find_cached_models()` to probe only the known faster-whisper cache paths instead of enumerating the entire HuggingFace cache root.
- Added `local_model_inventory_store.py`, a dedicated JSON cache for last-known local model inventories keyed by `model_dir`.
- Settings dialog Local and Benchmark model views now use cached inventory immediately when available, then verify in the background and refresh automatically.
- Empty cached inventories are treated as valid cached state, so the "no local models found" view can also render immediately instead of falling back to a fresh scanning placeholder.
- Added a low-impact startup prewarm for the local model inventory cache: on app start, a background thread refreshes the inventory only when no cached entry exists yet for the active `model_dir`.
- **Benchmark download confirmation:** User is now asked before downloading uncached models.
- **Settings dialog overhaul:** Tabs for Local/Remote, save confirmation status bar, provider activation/testing dialog.

## 2026-04-12

- Local model inventory refresh is now demand-driven by the Local/Benchmark tabs instead of being kicked off during every settings-dialog initialization.
- The Local tab now renders either cached inventory or a neutral "not yet verified" placeholder immediately, then refreshes in the background after the tab is visible.
- `model_dir` changes are now debounced before re-scanning, which avoids stacking repeated cache probes while the user edits the path.
- Removed the startup local-model inventory prewarm because it could race with the dialog's own refresh path and contribute to first-open UI stalls.

## 2026-04-13

- The settings dialog now computes its initial size from the widest tab, bounded by the available screen size, so it opens without unnecessary horizontal scrolling on normal displays.
- The Local Models group now expands with the dialog height, while its list keeps a small minimum height so inner scrolling only appears when the available space is genuinely limited.
- Compact list-item padding is shared across the Local and History views to reduce wasted vertical space without changing their overall structure.

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

### Session 4 — Windows testing fixes

- **AssemblyAI SpeechModel fix:** SDK 0.50.0 does not have `SpeechModel.universal_3_pro` or `SpeechModel.universal_2`. Changed `_build_config()` to use `speech_model=aai.SpeechModel.best` (singular key, single value). This auto-selects the best available model.
- **Groq dependency fix:** `groq` package was missing from `requirements-win.txt`, causing `[Errno 2] No such file or directory` when Groq SDK wasn't installed. Added `groq>=0.9.0`. Also tightened `except Exception: pass` to `except ImportError: pass` in `_build_client()` to avoid swallowing real errors.
- **Settings dialog non-modal:** Changed `setModal(True)` to `setModal(False)` so the overlay Copy button and text selection remain interactive while the Settings dialog is open. Added `_active_settings_dialog` tracking in `main.py` to prevent duplicate dialogs.
- **Preload guard in `start_recording()`:** If `_preload_future` is still running when hotkey is pressed, show "Model is still loading. Please wait a moment." error and return early instead of attempting transcription with no model loaded.
- Test count unchanged at 326 (fixed FakeSpeechModel in `test_assemblyai_provider.py` and `test_ssl_and_preload.py` to match new `best` model).

### Session 4b — SSL truststore, overlay activation, dialog lifecycle

- **`truststore` integration:** Added `truststore>=0.9.1` dependency. `inject_system_trust_store()` calls `truststore.inject_into_ssl()` at startup, making Python use the OS certificate store. On Windows, this automatically trusts corporate proxy CAs (Zscaler, BlueCoat) without any manual env-var setup, because IT installs the proxy CA into the Windows cert store.
- **`sync_ca_bundle_env_vars()`:** If the user has set only `SSL_CERT_FILE` or only `REQUESTS_CA_BUNDLE`, the other is now auto-populated. Different HTTP libraries read different vars (`requests` reads `REQUESTS_CA_BUNDLE`, `httpx`/`urllib` read `SSL_CERT_FILE`). Syncing ensures one setting covers all providers.
- **Copy-button two-click fix:** Added `showEvent` override + `_apply_noactivate_style()` that sets `WS_EX_NOACTIVATE` directly via Win32 `SetWindowLongW`. Qt's `WindowDoesNotAcceptFocus` flag is not always honoured by Windows. Direct `WS_EX_NOACTIVATE` is more reliable. Re-applied on every show because Qt may reset extended styles.
- **Settings dialog no longer blocks event loop:** Changed `dialog.exec()` → `dialog.show()` + `WA_DeleteOnClose` + `finished` signal cleanup. `exec()` created a nested event loop that could starve the main loop, causing overlay unresponsiveness. `show()` keeps everything in the single main event loop.
- **Clipboard setting:** `DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD` is `False` since Session 3, but existing `settings.json` files keep the old `True` value. User must toggle it off in Settings → General tab. No migration added (intentional — users who set it to `True` deliberately should keep their choice).
- 9 new tests: `TestInjectSystemTrustStore` (3), `TestSyncCABundleEnvVars` (5), `test_overlay_has_show_event_override` (1). Total: 335.

## 2026-03-02

- **Settings dialog clarity: debug WAV location shown inline.** Added a persistent hint below `Save last WAV for debugging` that displays the exact file path (`%APPDATA%\\stt_app\\last_recording.wav`) and that it is overwritten on each recording.
- **Engine-aware language control in settings UI.**
  - Added centralized language metadata constants in `config.py` (`LANGUAGE_MODE_LABELS`, `ENGINE_LANGUAGE_MODES`, `LOCAL_ENGLISH_ONLY_MODELS`).
  - Language combo is now rebuilt dynamically based on selected engine/mode/model.
  - AssemblyAI + streaming: language is locked to `Auto` (provider handles realtime language detection).
  - Local + `distil-large-v3.5`: language options reduced to `Auto` + `English` (German disabled because model is English-only).
  - Added explanatory note text in the UI when language choices are constrained.
- Added focused settings-dialog tests for dynamic language availability and visible debug WAV path hint.
- **Local model preload UX upgrade (non-blocking fallback + progress):**
  - Local startup/settings preload now tracks download progress and renders a textual progress bar with MB/s in the overlay while the selected model downloads.
  - Hotkey recording is no longer hard-blocked during local model download if another cached model exists.
  - During preload, batch recording automatically uses the closest smaller cached fallback model for that recording only.
  - After the selected model finishes loading, the app keeps the selected model and uses it automatically for subsequent recordings.
  - Added tests for fallback selection logic, preload-time fallback start behavior, and model-cache byte estimation.
- **Recording archive and discoverability improvements:**
  - Added `Archive every recording to folder` setting with configurable retention count (`Keep Recordings`).
  - Added recordings directory picker plus `Open Folder` action directly in settings.
  - Added dedicated app path helpers for recordings and transcript history files.
- **Transcription history and recovery UX:**
  - Added persistent transcript history store (`transcript_history.json`) with configurable max size.
  - Added overlay `History` button and tray `History` action with a dedicated `HistoryDialog`.
  - Added `Retry` support for failed transcriptions (`Retry` overlay button + tray action), reusing the same failed audio payload.
  - Added settings `History` tab with transcript list/details and direct file-import transcription workflow.
- **Cancellation and control improvements:**
  - Added separate cancel hotkey setting (`DEFAULT_CANCEL_HOTKEY`), independent registration, conflict validation against main hotkey (equal/subset/superset blocked).
  - Added overlay `Cancel` button and tray `Cancel current action` action.
  - Recording cancel now stops active capture immediately; in-flight transcription cancel is best-effort (result suppressed when it returns).
- **Overlay behavior and ergonomics:**
  - Overlay is now draggable by mouse.
  - Added `Reset Pos` button and startup corner selection (`top-right`, `top-left`, `bottom-right`, `bottom-left`).
  - Overlay control strip expanded with `History`, `Retry`, `Cancel`, and position reset.
  - Improved detail rendering robustness (`PlainText` detail + viewport-based width calculation) to avoid visual overlap on long download/progress messages.
- **Whisper quiet-speech tuning:**
  - Added configurable VAD energy threshold in settings (`VAD Threshold`) to make local whispering/quiet speech detection adjustable.
  - Lower threshold increases sensitivity; values are clamped in settings schema validation.
- **Local model lifecycle controls:**
  - Added Local-tab model management list with delete action for already-downloaded models (`Delete Selected`).
  - Added cache deletion helpers in local transcriber module (`cached_model_paths`, `delete_cached_model`).
  - Added preload download cancellation path: pressing cancel while local model preload/download is active requests cancellation and terminates the helper download process.

## 2026-03-03

- **Overlay transparency control added directly in overlay UI:**
  - Added bottom `Opacity` slider in `OverlayUI` with immediate effect (`setWindowOpacity`).
  - Value is clamped to `25..100%` to prevent accidental invisible overlay states.
  - Opacity setting persists via `AppSettings.overlay_opacity_percent` and updates live through controller (`set_overlay_opacity_percent`).
- **History defaults and limits updated:**
  - Increased default history size from `10` to `20`.
  - Added `0 = unlimited` support across config, settings schema, history store, and settings UI spinbox.
- **History dialog upgraded for management workflows:**
  - Added in-dialog history limit control (with persistence).
  - Added confirmation prompt before shrinking limit when it would delete stored entries.
  - Added `Export...`, `Import...`, and `Clear history` actions.
  - Added import overflow decision: import only free slots or import all and switch to unlimited history.
  - Added visual feedback on `Copy selected` action.
- **Settings history save safety improved:**
  - On save, reducing history limit now asks for confirmation before deletion and trims only when the limit actually changed.
  - History copy button in settings tab now shows explicit copied feedback.
- **Transcript history storage API expanded:**
  - Added `count`, `append_entries`, `apply_max_items`, `clear`, `export_to_file`, and `import_from_file` helpers.
  - Centralized trimming logic so all call sites enforce the same retention behavior.
- **Overlay size behavior hardened for active states:**
  - Listening/processing/idle use compact detail mode to reduce stale large overlay height during new dictation cycles.
  - Fallback preload listening message was shortened to avoid oversized overlay growth.
- Added/updated tests for history dialog, history store retention/import-export, overlay opacity behavior, unlimited history settings persistence, and settings schema updates.
- Verification note: full `pytest` run was blocked in the current environment due unavailable dependencies/network; syntax verification completed via `python -m compileall src tests`.

## 2026-03-03 — Session 5: Bug fixes and code review

- **Groq/AssemblyAI `[Errno 2]` fix (keyring robustness):** `secret_store.get_api_key()` now wraps `keyring.get_password()` in `try/except Exception` to prevent `FileNotFoundError` (or any backend error) from propagating. On Windows corporate machines, keyring backends may fall back to file-based storage that fails if the credential directory is missing.
- **Transcriber initialization error isolation:** `_transcribe_worker()` now separates `_get_or_create_transcriber()` from `transcribe_batch()` in distinct `try` blocks. Errors during transcriber creation emit `Transcriber initialization failed: <detail>` instead of the generic `Unexpected transcription error` message, improving diagnostics.
- **Start beep no longer interferes with recording:** Moved `_play_start_beep()` before `capture.start()` in both `_start_batch_recording()` and `_start_streaming_recording()`. `winsound.Beep()` is synchronous/blocking and plays through the audio device. Previously, the beep was captured by the microphone because it played while recording was active, drowning out early speech and causing only the last few words to be transcribed.
- **Overlay expands during model download:** Added optional `compact` keyword argument to `OverlayUI.set_state()` that allows callers to override the default compact-mode behavior. Download progress polling now passes `compact=False` so the overlay expands to fit the progress bar text (model name, percentage, speed, fallback hint).
- **Preload download failure now tries fallback models:** Previously, a download failure in `_download_model_for_preload()` caused `_preload_model_worker()` to exit immediately. Now it logs a warning and continues to the cache-based fallback logic, so a cached smaller model can serve transcription while the desired model is unavailable.
- **Thread-safety fix in settings dialog import:** `_transcribe_import_file()` was called from a background thread but accessed Qt widgets (combo boxes, check boxes, spinboxes) to build `AppSettings`. Widget access from non-GUI threads is undefined behavior in Qt. Extracted `_build_current_settings()` helper that reads all widgets on the GUI thread before the background thread starts.
- **Error-tolerant API key persistence:** `set_api_key()` calls in `_save()` are now wrapped in `try/except` to prevent a failing keyring backend from aborting the entire settings save.
- **Eliminated duplicate `find_cached_models()` scan:** `_refresh_local_model_views()` now scans once and passes the result to both `_refresh_local_models_label()` and `_refresh_cached_models_list()`.
- **Test fixes:** Corrected `test_select_cached_fallback_model_prefers_closest_smaller` expectation (large-v3-turbo is 809 MB, smaller than medium at 1400 MB). Fixed `test_groq_language_note_explains_auto_and_hints` to use `isVisibleTo(dialog)` instead of `isVisible()` (which checks parent-chain visibility on unshown dialogs).
- 381 tests (380 + 1 known Windows-only). All passing on Linux.

## 2026-03-03 — Session 6: ENOENT hardening + key-storage fallback + History UX

- **Remote ENOENT hardening:** AssemblyAI and Groq providers now create temporary WAV files in app-controlled `%APPDATA%\stt_app\temp` instead of relying on system TEMP/TMP defaults. This avoids failures on locked-down corporate machines with broken/missing temp env paths.
- **Clearer missing-file diagnostics:** Added explicit `FileNotFoundError` handling in remote providers and controller worker path so users get actionable messages instead of opaque `Unexpected transcription error`.
- **API key storage fallback option:** Added settings flag `allow_insecure_key_storage` (schema v11). When enabled, `KeyringSecretStore` falls back to plain-text local storage (`insecure_api_keys.json`) if keyring is unavailable.
- **Immediate key storage feedback:** Settings save now validates that key writes succeeded and shows clear status/warning in the Remote tab.
- **Recording persistence hardening:** On transcription failure, if `save_last_wav` is enabled, the failed WAV payload is written again to `last_recording.wav` as a safety net.
- **UI stability improvement:** Language note row now uses fixed height to avoid small layout jumps when switching engine/model/mode constraints.
- **History import workflow upgrade:** Import now uses a two-step flow (select file first, then explicit start with confirmation), plus a quick action to reuse the last recorded file.

## 2026-03-05

- **Overlay Clear behavior aligned with initial onboarding hint:**
  - `OverlayUI.clear_detail_text()` now restores the current idle instruction
    text instead of clearing to an empty detail area.
  - Idle detail is cached when `set_state("Idle", detail)` is called, so
    `Clear` restores either the initial onboarding hint or the current
    hotkey/cancel-hint idle text managed by the controller.
  - Keeps compact overlay sizing behavior after clear so stale expanded size is
    removed immediately.
  - Updated overlay UI test coverage to assert Idle state + restored hint text
    after pressing `Clear`.

## 2026-03-27

- **Overlay compact reset now restores the real startup size:**
  - `OverlayUI` now caches the actual initial compact window size after the
    first idle render.
  - All later compact transitions (`Idle`, `Listening`, `Processing`,
    `Reset Pos`, `Clear`) reuse that cached size instead of recomputing a fresh
    compact height from current layout state.
  - This hardens the overlay against cases where it stayed visually enlarged
    after a long transcript and then only changed state without returning to
    the original startup footprint.
  - Added focused overlay tests that assert exact restoration to the initial
    size after `Clear`, `Reset Pos`, and a retry-style `Processing` transition.

- **Last-recording recovery is now first-class instead of a debug-only side path:**
  - Added `LastRecordingStore` with persisted audio + metadata state
    (`last_recording.wav` + `last_recording.json`).
  - The latest recording is now always preserved until transcription either
    succeeds, fails, or is canceled; `save_last_wav` now means
    "keep after successful transcription".
  - Recovery survives crashes and interrupted transcriptions: startup now
    prompts to reopen Settings -> History with the unfinished recording loaded.
  - `History -> Use last recording` no longer depends on the old debug-WAV
    checkbox; orphaned leftover audio without metadata is still treated as
    recoverable.
  - Failure/cancel messaging was updated to explicitly say when the recording
    remains available for re-transcription.

- **Remote model selection was unified per provider:**
  - Added persisted `deepgram_model` and `assemblyai_model` settings alongside
    the existing Groq/OpenAI model settings.
  - Replaced separate Groq/OpenAI controls with one provider-aware
    `Remote Speech Model` selector that changes with the active remote engine.
  - Deepgram model selection now flows through factory/provider creation.
  - AssemblyAI batch model selection now supports both enum-backed values
    (`best`, `nano`) and named routed models such as `universal-3-pro`.
  - AssemblyAI streaming remains SDK-default-controlled for now; the UI
    disables model switching in streaming mode and explains that the selection
    still applies to batch/import transcription.

- **History deletion and settings-save overlay reset were tightened:**
  - Added `delete_entry` / `delete_entries` helpers in the transcript history
    store and exposed `Delete selected` in both history UIs.
  - Saving settings now explicitly restores the compact overlay size after
    applying the new corner setting, closing a remaining reset gap after
    recordings.

- **Validation note:**
  - Full Windows suite now runs successfully via
    `.venv\Scripts\pytest.exe -q`.
  - The Windows `.venv` is uv-managed; `pytest.exe` is available, but
    `python -m pytest` / `python -m pip` are not reliable entry points there.

- **Dependency baseline was refreshed and re-locked intentionally:**
  - Updated direct app/dev/build dependencies to the latest verified PyPI
    releases in `pyproject.toml`, including PySide6 6.11.0, numpy 2.4.3,
    pywin32 311, AssemblyAI 0.59.0, Groq 1.1.2, pytest 9.0.2, and
    hatchling 1.29.0.
  - Kept `requirements-win.txt` and `requirements-dev-win.txt` aligned with
    the same direct dependency set so the non-`uv` installation path does not
    drift from the `pyproject.toml` source of truth.
  - Rebuilt `uv.lock` with `uv lock --upgrade`, which restored the modern
    `revision = 3` header and refreshed transitive dependencies such as
    PySide6/shiboken, Hugging Face tooling, `onnxruntime`, and `protobuf`.
  - Synced the Windows uv-managed `.venv` via `uv sync --group dev`, then
    re-ran the full Windows suite successfully on the upgraded dependency
    graph.

- **Low-risk lint debt was cleaned up while verifying the new stack:**
  - Removed unused imports and a dead local variable uncovered by `ruff`.
  - Marked the root `main.py` bootstrap import as an intentional post-path
    insertion import, instead of leaving it as a standing E402 violation.
  - Normalized a few no-op f-strings in helper scripts so `ruff check`
    passes cleanly on the current codebase.

## 2026-03-29

- **ElevenLabs was added as a new hosted transcription provider:**
  - Added `ElevenLabsTranscriber` with batch transcription, provider-specific
    HTTP/auth handling, connection testing, and explicit error messages for
    auth, rate limits, SSL interception, and missing files.
  - Added provider constants in `config.py`, persisted
    `has_elevenlabs_key` / `elevenlabs_model` settings, and wired
    provider-specific model selection through the controller/transcriber
    factory.
  - Extended the settings UI with ElevenLabs API key storage, model selection,
    connection testing, import-engine visibility, and provider-aware help text
    that explains the current batch-only app support.
  - Updated user-facing documentation (`README`, quick start, advanced setup,
    streaming mode, provider costs) to include ElevenLabs availability,
    pricing, free-tier details, and the batch-vs-realtime distinction.
  - Added targeted provider/settings tests and re-ran the full Windows suite
    successfully after the integration.

- **Cohere Transcribe was evaluated and documented, but not integrated:**
  - Added `docs/cohere-transcribe-evaluation.md` as a decision record similar
    to the existing Parakeet evaluation.
  - Refined the analysis to distinguish the **local/open-weights** question
    from the **hosted API** question instead of treating Cohere only as another
    cloud provider.
  - Captured the current official product shape: `cohere-transcribe-03-2026`
    is documented by Cohere as an audio transcription model and open source
    research release, the hosted endpoint has a documented 25 MB limit, trial
    API access is publicly available, and self-deployed/open-weights licensing
    is still routed through Cohere's deployment/licensing guidance.
  - Deferred implementation because the current public evidence is still too
    weak for a trustworthy local-engine decision, while hosted pricing and
    speech-specific quality evidence are not explicit enough to justify adding
    another remote provider.
  - Added a separate "researched but not integrated" note in
    `docs/provider-costs.md` so Cohere stays visible for product comparison
    without being misread as a supported engine.

- **Validation note:**
  - `python3 -m compileall src tests`
  - `cmd.exe /d /c ".venv\\Scripts\\python.exe -m pytest -q"`

- **Recovery prompt false-positives and settings/history UI density were tightened:**
  - Successful transcriptions now attach a `source_recording_id` to history
    entries, and `LastRecordingStore` persists a `recording_id` alongside the
    managed WAV state.
  - Startup recovery prompting now suppresses stale prompts when the last
    recording already has a matching successful history entry, with a small
    timestamp fallback for older/orphaned metadata cases.
  - The remote speech model selector was moved next to the engine selection in
    the General tab so provider/model choice is visible where users actually
    switch engines.
  - Settings/history spacing was tightened, the embedded history list now uses
    the same font size as the detail pane, and combo-box popups were switched
    to uniform single-pass list views to avoid the "jumping" popup effect on
    open.

- **Windows distribution now has an explicit end-user release path:**
  - Switched the PyInstaller spec from a bare EXE-oriented setup to a more
    robust `onedir` bundle layout for Windows end-user builds.
  - Added `scripts/build_windows_release.ps1` to produce a repeatable Windows
    release folder/zip without requiring end users to clone the repo or use
    `uv`.
  - Added `PyInstaller` to the dev toolchain and verified that the Windows
    release script can build a real `release\stt_app-win-x64` bundle.
  - Added `docs/windows-distribution.md` and linked it from the main docs so
    the preferred rollout path is now "GitHub Releases first, installer/winget
    later" instead of "repo checkout + terminal".

- **Windows tooltip noise was reduced defensively:**
  - Removed non-essential overlay button tooltips and the Windows tray tooltip
    to reduce transient `QLabel` helper windows that can trigger harmless but
    noisy `QWindowsWindow::setGeometry` warnings on some systems.
- **Windows packaging moved from "spec exists" to a real release pipeline:** The
  repo now treats PyInstaller `onedir` as the portable base artifact, adds an
  Inno Setup wrapper on top of that portable bundle, and introduces a GitHub
  Actions workflow that builds candidate artifacts on manual dispatch and
  publishes official release assets on `v*` tags.
- **Distribution guidance clarified for maintainers and end users:** The docs now
  explain what `onedir` actually means, when to use the ZIP vs the installer,
  and why the release workflow should be manual or tag-driven instead of
  running on every commit.

## 2026-04-02

- **Streaming runtime failures now fail fast instead of lingering until Stop:**
  - Added an explicit streaming runtime error callback path from transcribers to
    the controller.
  - Controller now tears down active streaming capture/transcriber state on
    mid-stream failures, preserves captured audio for retry, and marks the last
    recording as failed.
  - Fixed a cleanup gap where chunk-push failures could leave the microphone
    capture and provider session alive even though the overlay already showed an
    error.
- **Deepgram finalization is now less truncation-prone:** `stop_stream()` sends
  `Finalize`, waits briefly for trailing final transcript messages, and only
  then closes the socket.
- **Provider consistency/testing improved:**
  - Local, AssemblyAI, and Deepgram streaming paths now report runtime errors
    immediately to the controller.
  - Added regression coverage for controller mid-stream failure cleanup,
    AssemblyAI/Deepgram runtime-error callbacks, local streaming runtime-error
    propagation, and delayed Deepgram finalize messages.
- **Streaming live insertion is now revisable instead of append-only:**
  - Controller now keeps a locked prefix plus a mutable live tail, so partial
    revisions can replace or shrink recent inserted text instead of only
    appending more words.
  - Finalization can now replace or delete the remaining live tail in place,
    which reduces duplicated trailing words when the provider shortens or
    rewrites the ending.
  - Added regression coverage for shrinking partials, tail deletion on
    finalize, and the new replacement path in `text_inserter.py`.
- **Win32 input structs are now defined with fixed Windows-width ctypes:**
  - Replaced platform-dependent `ctypes.wintypes` fields in `INPUT`-related
    structures with explicit 16/32/64-bit Windows types.
  - This fixed the cross-platform `INPUT` size mismatch in
    `tests/test_text_inserter.py` and makes the low-level input path testable on
    Linux/WSL too.
- **Validation:**
  - `.venv/bin/python -m pytest tests/test_controller.py tests/test_controller_coverage.py tests/test_text_inserter.py tests/test_assemblyai_provider.py tests/test_deepgram_provider.py tests/test_transcriber.py -q`
  - `.venv/bin/python -m pytest -q`

- **Line-ending churn across Windows/WSL was a repository policy gap:**
  - Root cause: tracked text files were stored with LF in Git, but some local
    edits rewrote them to CRLF because the repo had no shared line-ending policy.
  - Added `.gitattributes` to normalize repository text files to LF and mark
    common binary assets explicitly.
  - Added `.editorconfig` so editors save LF consistently on every machine.
  - Renormalized the affected text files so CRLF-only noise no longer appears as
    fake code changes.
