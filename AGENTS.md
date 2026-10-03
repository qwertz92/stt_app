# AGENTS.md

## Purpose

Running project memory for `stt_app`. Agents: read this first before making changes.
This file is loaded into every agent session, so it holds only the rules every
change needs plus an index. The binding per-area design decisions live in
`docs/agents/` (see "Design decisions by area" below) and must be read before
changing that area. Detailed history is in `docs/learning-log.md`; ideas kept
for later are in `docs/ROADMAP.md`; accepted defects are in
`docs/agents/known-limitations.md`.

## Quality principle

Quality has the highest priority. Take as much time as needed.

- No duplicated logic: every function/constant should exist in exactly one place.
- No dead code or unused imports.
- Every change must pass all existing tests.
- Document decisions in the matching `docs/agents/<area>.md` file (a rule every
  change needs goes here instead); document history in `docs/learning-log.md`.
- Keep this file small: it is loaded into every session. An entry here states
  the rule and points to its area file; the measurements, rejected
  alternatives and history behind it belong in that file or the learning log.
- User requests may come through speech-to-text and can contain mistranscribed words or malformed phrases.
- If the intent is unclear, ask for clarification before making a change that may not match the user's actual goal.

## Commit style

- After validated code changes, commit the agent's own changes and push the
  commit unless the user explicitly asks not to.
- Repository-wide improvement work is not complete while it exists only on a
  feature/review branch. Unless the user requested a PR-only or review-first
  workflow, merge validated work into `main` and push `main` before reporting
  completion.
- Every final handoff must state the current branch and whether the work is in
  `main`. If it is intentionally not merged, place a conspicuous
  **NOT MERGED TO MAIN** warning at both the beginning and end of the final
  response so the publication state cannot be overlooked.
- Delete local and remote branches only after proving they are fully contained
  in the pushed `main`; preserve and explicitly report every unmerged branch.
- Use logical commits for distinct bugfix/feature/refactor units.
- Match the existing history: short conventional subject line, blank line, then concise `-` bullet points.
- Hard-wrap every commit body line at a maximum of 100 characters.
- Never include literal escape sequences such as `\n` in commit messages; use real newlines.
- For shell-driven commits, prefer a message file or stdin with real line breaks, then verify with `git log -1 --format=%B`.
- Do not include validation blocks or lists of executed test commands in commit messages.
- It is fine to mention newly added or updated tests as part of the change summary.

## Language rule

**All project content must be in English.** Code, comments, docs, commits, error messages, UI labels, logs.
Exception: `stt-dictation-spec.md` (legacy bilingual).

## Runtime stack

- Python 3.12, PySide6 UI/tray/overlay
- Win32 RegisterHotKey + SendInput (Windows 11 only; Linux/WSL for dev tooling)
- sounddevice for mic capture
- faster-whisper (CTranslate2) for local transcription
- ONNX Runtime GenAI for Nemotron 3.5 cache-aware local streaming
- onnx-asr (pure Python) for NVIDIA Parakeet TDT and Canary, CPU only
- numpy + ONNX Runtime (CPU provider) + `tokenizers` for IBM Granite Speech 5.0
  470M TurboCTC (INT8, English only, batch only)
- Remote providers: AssemblyAI (SDK batch Universal-3.5 Pro + Universal-3.6 Pro realtime),
  OpenAI (REST API), Groq (SDK), Deepgram (REST + WebSocket),
  ElevenLabs (REST API), Azure LLM Speech / MAI-Transcribe (REST, batch-only),
  Fun-ASR / Alibaba (DashScope WebSocket, batch-only, no German),
  Speechmatics (REST batch jobs, polled; EU/US/AU regions),
  Mistral Voxtral (REST, batch-only),
  Custom endpoint (any OpenAI-compatible REST API, batch-only)
- keyring for secret storage
- comtypes for MMDevice audio endpoint change notifications (Windows)

## Architecture

### Module responsibilities

| Module | Purpose |
| ------ | ------- |
| `config.py` | All tunables/constants; `MODEL_REPO_MAP` (single source of truth) |
| `controller.py` | Main orchestrator/state machine; hotkey, audio, transcriber, overlay, inserter, history, preload |
| `streaming_text.py` | Pure streaming text normalization, locked-prefix, live-tail, and finalization logic |
| `audio_capture.py` | sounddevice mic recording + VAD auto-stop + streaming chunk callback; `WarmMicrophoneStream` with deferred restart/close and device-keyed attach |
| `audio_devices.py` | Input-device inventory and name→index resolution (WASAPI-first); PortAudio re-enumeration guarded by a shared open-lock plus live-stream registry |
| `audio_device_listener.py` | Event-driven MMDevice endpoint notifications (default capture switch, hot-plug) via a comtypes `IMMNotificationClient`; inert without COM |
| `transcriber/local_faster_whisper.py` | Batch + streaming via faster-whisper; `find_cached_models`; `preload_model`; cooperative batch cancel via `set_cancel_check` |
| `transcriber/local_nemotron.py` | Batch + true cache-aware streaming for Nemotron 3.5 INT4 via ONNX Runtime GenAI |
| `transcriber/local_onnx_asr.py` | Batch-only NVIDIA NeMo models (Parakeet TDT, Canary) via the pure-Python `onnx-asr` runtime; CPU only, no Node.js; mid-run cancel via ONNX Runtime `RunOptions.terminate`; also home of the WAV reader, the abort handle and `resolve_or_download_onnx_model`, which the Granite CTC runtime shares |
| `transcriber/local_granite_ctc.py` | Batch-only IBM Granite Speech 5.0 470M TurboCTC: numpy log-mel features, the INT8 CTC graph on ONNX Runtime's CPU provider, greedy CTC and `tokenizers` decode; English only, passes of at most 180 s, mid-run cancel via `RunOptions.terminate` |
| `transcriber/_pcm_audio.py` | Linear resampling (Nemotron, Granite CTC), the quiet-point splitter (Granite CTC passes, remote batch parts) and the 16-bit mono WAV encoder |
| `transcriber/_audio_parts.py` | Sends a remote batch recording past its engine's limit (`config.remote_batch_part_limit`) as consecutive WAV parts through the provider's own single-request code (OpenAI, Groq, Azure) |
| `transcriber/local_webgpu_asr.py` | Shared local ONNX inventory/download helpers plus the batch-only Cohere/Granite Node.js runtime (supported daily-use GPU models); cancel kills the child |
| `transcriber/assemblyai_provider.py` | Batch + streaming via AssemblyAI SDK |
| `transcriber/openai_provider.py` | Batch via OpenAI API |
| `transcriber/groq_provider.py` | Batch via Groq SDK |
| `transcriber/deepgram_provider.py` | Batch via REST + streaming via WebSocket |
| `transcriber/elevenlabs_provider.py` | Batch via ElevenLabs REST API |
| `transcriber/azure_provider.py` | Batch via Azure LLM Speech fast-transcription REST (enhanced mode / MAI-Transcribe); needs endpoint + key |
| `transcriber/funasr_provider.py` | Batch via Alibaba Fun-ASR over the DashScope realtime WebSocket (key-only; no German) |
| `transcriber/speechmatics_provider.py` | Batch via Speechmatics' job API: upload, poll, fetch the text transcript; Melia 1 (default, Auto), Enhanced, Standard; region `eu1`/`us1`/`au1` |
| `transcriber/mistral_provider.py` | Batch via Mistral's `/v1/audio/transcriptions` (Voxtral Mini Transcribe 2); custom vocabulary as `context_bias` |
| `transcriber/_job_poll.py` | Bounded polling of a remote batch job (`poll_job`) and retried result fetch (`fetch_with_retries`): total budget, shutdown flag, job id in every error |
| `transcriber/custom_endpoint_provider.py` | Batch via a bring-your-own OpenAI-compatible endpoint: `/audio/transcriptions` or chat completions with audio input, static key or a key command, `/models` listing |
| `process_tree.py` | `run_bounded` (a subprocess with a hard timeout that kills the whole process tree -- a job object on Windows -- and returns once the child exited even if a grandchild holds its pipes) and `kill_process_tree`, shared by the custom endpoint's key command and the benchmark worker |
| `transcriber/factory.py` | Creates transcriber from settings; routes engine to provider |
| `text_inserter.py` | Clipboard-safe paste: save > set > paste > restore with contention guard |
| `overlay_ui.py` | Always-on-top frameless overlay with state colors, controls, opacity slider, transcription queue panel |
| `settings_dialog.py` | Facade: composes the `SettingsDialog` from tab mixins and keeps dialog lifecycle/shared-UI code; re-exports the module API |
| `settings_dialog_helpers.py` | Shared settings-dialog widgets, constants, and pure helpers (hotkey conversion, benchmark labels) |
| `settings_dialog_general.py` | Transcription tab: engine/model/language/mode selection and text-insertion mixin (owns `model_combo` for local models and `remote_model_combo` for remote models, unified in one stacked "Model" row) |
| `settings_dialog_hotkeys.py` | Hotkeys & Display tab: the four global hotkeys, overlay corner, and tray middle-click toggle mixin (split from the Transcription tab) |
| `settings_dialog_audio.py` | Audio tab: microphone picker, warm stream, VAD, silence gate, start/completion tones, and recordings retention mixin (split from the Transcription tab) |
| `settings_dialog_local.py` | Models tab: local-model management mixin (inventory, scan, download queue, delete; model selection lives on the Transcription tab) plus the "Local runtime" group (ONNX Device, Keep ONNX model loaded) |
| `settings_dialog_benchmark.py` | Benchmark tab (history + results + live status) plus the pop-out Run Benchmark window (model selection, options, run controls) mixin |
| `settings_dialog_remote.py` | API Keys tab: provider API keys, data-residency region selectors (AssemblyAI, Deepgram, Speechmatics) and connection-test mixin |
| `settings_dialog_history.py` | History tab: transcript list, edit, copy, delete, retained-audio reveal/retranscription mixin |
| `settings_dialog_import.py` | Import Audio tab and recordings-directory helpers mixin |
| `settings_dialog_persistence.py` | Settings load/populate/build/save and key persistence mixin |
| `settings_dialog_unsaved.py` | Unsaved-changes tracking: a fingerprint of the settings inputs, the Save button's enabled state and the Save / Discard / Cancel prompt on close |
| `settings_store.py` | JSON settings persistence (`%APPDATA%\stt_app\settings.json`) |
| `persistence.py` | Atomic file writes, strict JSON booleans, recovery helpers, and shared path-scoped locks |
| `csv_safety.py` | Spreadsheet-formula neutralization for user-controlled CSV cells |
| `benchmark_history.py` | Persistent benchmark run history (JSON) with export |
| `ui_feedback.py` | Shared Qt button feedback styles, stable feedback widths, scroll restoration helpers |
| `dialog_style.py` | Shared message-box/dialog colours plus the app-wide filter that makes error text selectable |
| `local_model_inventory_store.py` | Persistent cache of last-known local model inventories keyed by `model_dir` |
| `local_model_download.py` | Cancellable source/packaged worker-process launcher for local model downloads |
| `model_download_coordinator.py` | The single download slot; serializes every download path — in-process and, via `file_lock`, across processes |
| `file_lock.py` | OS-level cross-process advisory lock (`msvcrt.locking` / `fcntl.flock`) that makes the download slot hold across every process of one Windows user |
| `model_download_progress.py` | Shared approximate model download percent and transfer-rate calculation |
| `local_model_download_worker.py` | Subprocess entry point that downloads one model |
| `local_model_scan.py` | Local model inventory scan shared by the app and its worker |
| `local_model_scan_worker.py` | Subprocess entry point for the inventory scan |
| `secret_store.py` | keyring wrapper for API keys with optional insecure plain-text fallback for restricted environments |
| `provider_connection_test_store.py` | Persistent last-known remote-provider connection test status keyed by provider |
| `update_checker.py` | GitHub Releases update check and version comparison helpers |
| `update_ui.py` | Shared Qt dialogs/actions for presenting update-check results |
| `update_installer.py` | Verified download and launch of a release installer |
| `transcript_history.py` | Persistent transcript history store (JSON) with import/export |
| `history_dialog.py` | History dialog with table view, copy, export/import, clear, limit control, per-entry audio reveal and retranscription, recordings-folder shortcut |
| `transcript_edit_dialog.py` | Edit one history entry's transcript |
| `history_ui_actions.py` | Shared export/import/clear flows and stored-count label formatting for the History dialog and Settings History tab |
| `history_audio.py` | Shared history-entry-to-audio resolution plus file-manager reveal/open helpers for both history views |
| `retranscribe_dialog.py` | Compact language-only retranscription of one history entry's retained audio |
| `app_paths.py` | Centralized app data/config path helpers |
| `last_recording_store.py` | Managed last-recording state (path, status, recovery) for Retry and Import |
| `app_icon.py` | Shared app icon path/loader for the app, tray, and dialog window icons |
| `logger.py` | Application logging setup and diagnostics text |
| `ssl_utils.py` | System trust store injection and CA bundle resolution |
| `vad.py` | Energy-based voice activity detection with configurable threshold |
| `silero_vad.py` | Silero VAD v6 speech check (the graph faster-whisper ships, ONNX Runtime CPU, one thread, built on a background thread with a 60 s retry after a failed load): `check_speech_*` for the batch silence gate, `measure_speech_pcm16` for the streaming post-pause gate; `None` = unmeasured, never gates |
| `window_focus.py` | Win32 foreground/focus/caret window tracking for text insertion |
| `paste_target_check.py` | Report-only check after a paste whether the focus showed a caret (Win32 caret, MSAA `OBJID_CARET` in Chromium windows) on one MTA worker thread; "not a text field" makes the controller report the paste as doubtful |
| `win_tray_icon.py` | Hand-registered Windows notification icon (`Shell_NotifyIcon` + native menu) with a `QSystemTrayIcon` fallback |
| `hotkey.py` | Global hotkey registration via Win32 RegisterHotKey |
| `benchmark_environment.py` | Best-effort benchmark system metadata |
| `local_benchmark.py` | Pure benchmark runner (`run_benchmark_cases`) + result models; used by the CLI and the out-of-process worker |
| `benchmark_worker.py` | Subprocess entry point: runs `run_benchmark_cases` and streams progress/case/done events as prefixed JSON lines |
| `benchmark_process.py` | Launches/streams the benchmark worker; re-exports `run_benchmark_cases` (same signature) for the settings dialog so the UI never freezes |
| `transcriber/_http_utils.py` | Safe multipart construction, audio MIME inference, HTTP error detail and the JSON transcript reader (`transcript_from_json`, `is_markup_page`) shared by REST providers |
| `scripts/import_model.py` | Import manually downloaded models; validates for Git LFS pointers |
| `scripts/download_model.py` | Automated model download for offline/corporate use |


## Design decisions by area

Each file below is binding. Before touching an area, read its file; when a
change creates, corrects or retires a decision, update that file in the same
commit. Entries are written as "rule, then why, then what was measured".

| Area file | Read before changing |
| --------- | -------------------- |
| [`docs/agents/settings-dialog.md`](docs/agents/settings-dialog.md) | any `settings_dialog*.py` module: tabs, sizing and minimum width, status lines, save/merge baseline, unsaved-changes prompt |
| [`docs/agents/overlay.md`](docs/agents/overlay.md) | `overlay_ui.py`, overlay sizing/centering/reveal, any controller code that paints the overlay |
| [`docs/agents/text-insertion.md`](docs/agents/text-insertion.md) | `text_inserter.py`, `window_focus.py`, clipboard capture/restore, deferred/queued inserts, the Insert offer, re-paste |
| [`docs/agents/audio-capture.md`](docs/agents/audio-capture.md) | `audio_capture.py`, `audio_devices.py`, `audio_device_listener.py`, the warm stream, the watchdog, the silence gate |
| [`docs/agents/streaming.md`](docs/agents/streaming.md) | `streaming_text.py`, rolling-window merge, pause/segment logic, stream handshake, finalize and runtime failures |
| [`docs/agents/controller-and-jobs.md`](docs/agents/controller-and-jobs.md) | `controller.py` job delivery, cancel, retry slot, last-recording marks, preload, runtime lease and cache identity |
| [`docs/agents/persistence-and-history.md`](docs/agents/persistence-and-history.md) | `persistence.py`, any JSON store, `secret_store.py`, transcript history and its audio linkage |
| [`docs/agents/local-models-and-downloads.md`](docs/agents/local-models-and-downloads.md) | local runtimes (faster-whisper, onnx-asr, Granite CTC, Nemotron, Node/ONNX), ONNX device policy, model inventory, download slot and progress, dependency versions |
| [`docs/agents/remote-providers.md`](docs/agents/remote-providers.md) | `transcriber/*_provider.py`, remote model rosters, batch splitting, provider error text |
| [`docs/agents/benchmark.md`](docs/agents/benchmark.md) | `local_benchmark.py`, `benchmark_*.py`, the Benchmark tab and windows, benchmark claims in docs |
| [`docs/agents/windows-platform.md`](docs/agents/windows-platform.md) | `hotkey.py`, `win_tray_icon.py`, update checks/installer, taskbar identity, Win32 `WinDLL` handles |
| [`docs/agents/build-release-and-testing.md`](docs/agents/build-release-and-testing.md) | packaging, release scripts, CI, ruff config, diagnostic scripts, script output |
| [`docs/agents/testing.md`](docs/agents/testing.md) | `tests/conftest.py` fixtures, layout/pixel tests, subprocess tests, `scripts/release_check_*.py` |
| [`docs/agents/known-limitations.md`](docs/agents/known-limitations.md) | recording or fixing an accepted defect |

### Cross-cutting invariants

Short forms of rules that recur across areas; the area files hold the detail.

- A finished transcription is never discarded, and a failure is never silent:
  background failures and failed inserts reach the tray (controller, overlay).
- Nothing waits on the Qt thread for a model load, a download slot, a remote
  handshake or an unbounded remote poll (controller, streaming, providers).
- Streaming insertion is append-only; a paste whose keystroke may have gone out
  is never retried or re-offered (streaming, text insertion).
- Never close a transcriber runtime that is in use; evict a cached object
  before closing it (controller).
- A delayed overlay writer re-checks the session and never paints over a live
  session or a finished Done/Error result (overlay).
- Every last-recording mark is keyed by the job's own recording id (controller).
- A store that cannot be read is neither empty nor damaged and is never written
  over; stores recover from, and delete, their `.bak` too (persistence).
- A selected local model is strict: never substitute or persist a fallback
  model (controller, local models).
- Every download goes through the single download slot (local models).
- Every settings save applies the user's edits, diffed against
  `_populated_settings`, onto a fresh `load()` (settings dialog).

## Core flow

1. Global hotkey toggles recording.
2. Overlay: `Idle → Listening → Processing → Done/Error`.
3. Batch mode: recorded WAV transcribed on stop.
4. Streaming mode (local, AssemblyAI, Deepgram): live chunks with partial text
   and append-only stable insertion. Nemotron local streaming is cache-aware;
   faster-whisper local streaming uses rolling windows.
5. Text inserted at caret via clipboard-safe paste; clipboard restored.

## Engines

- **VALID_ENGINES**: local, assemblyai, openai, groq, deepgram, elevenlabs,
  azure, funasr, speechmatics, mistral, custom
- **STREAMING_ENGINES**: local, assemblyai, deepgram (others are batch-only)
- **OpenAI** model select picks `gpt-transcribe` (default) or one of the three
  models OpenAI removes on 2027-02-26.
- **Azure LLM Speech** needs two settings: `azure_endpoint` (per-resource, e.g.
  `https://<resource>.cognitiveservices.azure.com`) and the `azure` key in the
  secret store. Model select picks `mai-transcribe-2` (default),
  `mai-transcribe-1.5` or the deprecated `mai-transcribe-1`. "Azure LLM
  Speech" is the service and MAI-Transcribe the model behind it: one engine,
  not two.
- **Fun-ASR (Alibaba)** is key-only (`funasr` key, Singapore-region DashScope),
  driven over the realtime WebSocket in batch mode. It covers 31 languages but
  **not German** (`FUNASR_LANGUAGE_MODES` excludes `de`).
- **Speechmatics** (`speechmatics`): `speechmatics_model` `melia-1`
  (default; the only one with Auto), `enhanced`, `standard`;
  `speechmatics_region` `eu1` (default), `us1`, `au1` (Melia 1 not in
  `au1`).
- **Mistral** (`mistral`): `voxtral-mini-2602`, no region choice.
- **Regions**: `assemblyai_region` (`auto` default, `us`, `eu`),
  `deepgram_region` (`global` default, `eu`) and `speechmatics_region` are
  picked on the API Keys tab; a label claims only what the vendor
  guarantees (`docs/agents/remote-providers.md`); the field map is
  `settings_store._REMOTE_REGION_FIELDS`.
- **Custom endpoint** (`custom`): base URL, free-text model, API style
  (`transcriptions` or `chat`) and an optional key command that prints a
  short-lived Bearer token; the `custom` key is the static fallback.
- All engine/model constants defined in `config.py`

## Tests

- Preferred on Windows: `.venv\Scripts\pytest.exe` -- **without `-q`**.
  `pyproject.toml` already sets `addopts = "-q"`, so passing it again is
  `-qq`, and `-qq` suppresses the final `N passed in Xs` line entirely. That
  is what every run in this repository did until 2026-08-28: a green run
  printed dots and `[100%]` and no count, so a run that collected three tests
  was indistinguishable from one that collected the whole suite, and "the
  suite is green" rested on an exit code alone. Measured on one module: 59
  dots and nothing with the extra `-q`, `59 passed in 0.26s` without it. The
  full suite on 2026-08-28 was `1831 passed, 1 skipped in 102.08s`.
- Alternate when the environment supports it: `uv run python -m pytest` or `python -m pytest`
- Note: the project uses a uv-managed Windows `.venv`; `pytest.exe` may be available even when `python -m pytest` or `python -m pip` is not.
- Always bound a run with a hard wall-clock limit (`timeout <secs> ...`), and
  never start a second suite while one is running: Qt suites open real windows
  on one desktop. `pyproject.toml` sets `faulthandler_timeout = 600`, so a test
  still running after ten minutes gets every thread's traceback written to the
  log (it is not killed); pass `-o faulthandler_timeout=<secs>` for a shorter
  one.
- Do **not** substitute `QT_QPA_PLATFORM=offscreen` for the commands above. It
  shifts widget metrics by 1-4 px and makes the two pixel-exact layout tests
  (`test_overlay_record_button_indicator_stays_centered_in_both_states`,
  `test_bottom_status_does_not_move_the_save_and_close_buttons`) fail. Failures
  from an offscreen run are artifacts, not repository problems.
- Detailed test rules (autouse fixtures, CI-runner assumptions, release
  workflow and the `scripts/release_check_*.py` contract) are in
  `docs/agents/testing.md`; read it before adding a fixture, a layout test or a
  release check.

## Known limitations

Accepted defects are recorded in `docs/agents/known-limitations.md`.
