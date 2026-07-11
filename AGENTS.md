# AGENTS.md

## Purpose

Running project memory for `stt_app`. Agents: read this first before making changes.
Detailed history is in `docs/learning-log.md`.

## Quality principle

Quality has the highest priority. Take as much time as needed.

- No duplicated logic: every function/constant should exist in exactly one place.
- No dead code or unused imports.
- Every change must pass all existing tests.
- Document decisions here; document history in `docs/learning-log.md`.
- User requests may come through speech-to-text and can contain mistranscribed words or malformed phrases.
- If the intent is unclear, ask for clarification before making a change that may not match the user's actual goal.

## Commit style

- After validated code changes, commit the agent's own changes and push the
  commit unless the user explicitly asks not to.
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
- Remote providers: AssemblyAI (SDK batch + Universal-Streaming v3),
  OpenAI (REST API), Groq (SDK), Deepgram (REST + WebSocket),
  ElevenLabs (REST API), Azure LLM Speech / MAI-Transcribe (REST, batch-only),
  Fun-ASR / Alibaba (DashScope WebSocket, batch-only, no German)
- keyring for secret storage

## Architecture

### Module responsibilities

| Module | Purpose |
| ------ | ------- |
| `config.py` | All tunables/constants; `MODEL_REPO_MAP` (single source of truth) |
| `controller.py` | Main orchestrator/state machine; hotkey, audio, transcriber, overlay, inserter, history, preload |
| `streaming_text.py` | Pure streaming text normalization, locked-prefix, live-tail, and finalization logic |
| `audio_capture.py` | sounddevice mic recording + VAD auto-stop + streaming chunk callback |
| `transcriber/local_faster_whisper.py` | Batch + streaming via faster-whisper; `find_cached_models`; `preload_model`; cooperative batch cancel via `set_cancel_check` |
| `transcriber/local_nemotron.py` | Batch + true cache-aware streaming for Nemotron 3.5 INT4 via ONNX Runtime GenAI |
| `transcriber/local_webgpu_asr.py` | Shared local ONNX inventory/download helpers plus the batch-only Cohere/Granite Node.js runtime (supported daily-use GPU models) |
| `transcriber/assemblyai_provider.py` | Batch + streaming via AssemblyAI SDK |
| `transcriber/openai_provider.py` | Batch via OpenAI API |
| `transcriber/groq_provider.py` | Batch via Groq SDK |
| `transcriber/deepgram_provider.py` | Batch via REST + streaming via WebSocket |
| `transcriber/elevenlabs_provider.py` | Batch via ElevenLabs REST API |
| `transcriber/azure_provider.py` | Batch via Azure LLM Speech fast-transcription REST (enhanced mode / MAI-Transcribe); needs endpoint + key |
| `transcriber/funasr_provider.py` | Batch via Alibaba Fun-ASR over the DashScope realtime WebSocket (key-only; no German) |
| `transcriber/factory.py` | Creates transcriber from settings; routes engine to provider |
| `text_inserter.py` | Clipboard-safe paste: save > set > paste > restore with contention guard |
| `overlay_ui.py` | Always-on-top frameless overlay with state colors, controls, opacity slider, transcription queue panel |
| `settings_dialog.py` | Facade: composes the `SettingsDialog` from tab mixins and keeps dialog lifecycle/shared-UI code; re-exports the module API |
| `settings_dialog_helpers.py` | Shared settings-dialog widgets, constants, and pure helpers (hotkey conversion, benchmark labels) |
| `settings_dialog_general.py` | General tab: engine/model/language/mode selection mixin (owns `model_combo` for local models and `remote_model_combo` for remote models, unified in one stacked "Model" row) |
| `settings_dialog_local.py` | Local tab: local-model management mixin (inventory, scan, download queue, delete only; model selection lives on the General tab) |
| `settings_dialog_benchmark.py` | Benchmark tab (history + results + live status) plus the pop-out Run Benchmark window (model selection, options, run controls) mixin |
| `settings_dialog_remote.py` | Remote tab: provider API keys and connection-test mixin |
| `settings_dialog_history.py` | History tab: transcript list, edit, copy, delete mixin |
| `settings_dialog_import.py` | Import Audio tab and recordings-directory helpers mixin |
| `settings_dialog_persistence.py` | Settings load/populate/build/save and key persistence mixin |
| `settings_store.py` | JSON settings persistence (`%APPDATA%\stt_app\settings.json`) |
| `persistence.py` | Atomic file writes, strict JSON booleans, recovery helpers, and shared path-scoped locks |
| `csv_safety.py` | Spreadsheet-formula neutralization for user-controlled CSV cells |
| `ui_feedback.py` | Shared Qt button feedback styles, stable feedback widths, scroll restoration helpers |
| `local_model_inventory_store.py` | Persistent cache of last-known local model inventories keyed by `model_dir` |
| `local_model_download.py` | Cancellable source/packaged worker-process launcher for local model downloads |
| `model_download_progress.py` | Shared approximate model download percent and transfer-rate calculation |
| `secret_store.py` | keyring wrapper for API keys with optional insecure plain-text fallback for restricted environments |
| `provider_connection_test_store.py` | Persistent last-known remote-provider connection test status keyed by provider |
| `update_checker.py` | GitHub Releases update check and version comparison helpers |
| `update_ui.py` | Shared Qt dialogs/actions for presenting update-check results |
| `transcript_history.py` | Persistent transcript history store (JSON) with import/export |
| `history_dialog.py` | History dialog with table view, copy, export/import, clear, limit control |
| `history_ui_actions.py` | Shared export/import/clear flows and stored-count label formatting for the History dialog and Settings History tab |
| `app_paths.py` | Centralized app data/config path helpers |
| `app_icon.py` | Shared app icon path/loader for the app, tray, and dialog window icons |
| `vad.py` | Energy-based voice activity detection with configurable threshold |
| `window_focus.py` | Win32 foreground/focus/caret window tracking for text insertion |
| `hotkey.py` | Global hotkey registration via Win32 RegisterHotKey |
| `benchmark_environment.py` | Best-effort benchmark system metadata |
| `local_benchmark.py` | Pure benchmark runner (`run_benchmark_cases`) + result models; used by the CLI and the out-of-process worker |
| `benchmark_worker.py` | Subprocess entry point: runs `run_benchmark_cases` and streams progress/case/done events as prefixed JSON lines |
| `benchmark_process.py` | Launches/streams the benchmark worker; re-exports `run_benchmark_cases` (same signature) for the settings dialog so the UI never freezes |
| `transcriber/_http_utils.py` | Safe multipart construction and audio MIME inference shared by REST providers |
| `scripts/import_model.py` | Import manually downloaded models; validates for Git LFS pointers |
| `scripts/download_model.py` | Automated model download for offline/corporate use |

### Key design decisions

- **Settings dialog is a mixin facade**: `settings_dialog.py` composes
  `SettingsDialog` from per-tab mixins in `settings_dialog_*.py`
  (`_GeneralTabMixin`, `_LocalModelsMixin`, `_BenchmarkMixin`,
  `_RemoteProvidersMixin`, `_HistoryTabMixin`, `_ImportTabMixin`,
  `_PersistenceMixin`) plus shared code in `settings_dialog_helpers.py`. Rules to
  keep intact: Qt `Signal`s stay on the `QObject`-derived `SettingsDialog`
  (mixins are plain classes and only use `self.<signal>`); every method reaches
  peers/attributes through `self`, so scattering across mixins is safe. The
  module's public names must remain importable/patchable as
  `stt_app.settings_dialog.<name>` — tests monkeypatch there — so the facade
  re-exports them (guarded by `__all__`). The six external functions the tests
  patch (`run_benchmark_cases`, `_scan_cached_models`,
  `start_model_download_process`, `delete_cached_model`,
  `estimate_cached_model_bytes`, `cleanup_incomplete_model_download`) are called
  through a lazy `_facade()` accessor (`_facade().<name>(...)`) in the
  local/benchmark mixins so the patch target still resolves after the split. The
  accessor imports the facade lazily (not at module scope) so a mixin can be
  imported directly without an import cycle.
- **Model selection is unified on the General tab; Local tab is management-only**:
  "what do I use" (engine, model, language, mode) all live in the General tab's
  "Engine && Mode" group box. A single "Model" form row hosts a
  `model_selector_stack` `QStackedWidget` with page 0 (`model_combo` plus
  `local_model_runtime_warning_label`) for the local engine and page 1
  (`remote_model_provider_label`/`remote_model_combo`/`remote_model_note_label`)
  for remote engines; `_update_remote_model_selector` flips the page via
  `_update_model_selector_page` whenever the engine changes.
  `QStackedWidget.sizeHint()` already reflects the largest page regardless of
  the current index, so switching pages never shifts the rows below. The Local
  tab keeps Model Dir, cached-model inventory, scan/refresh, download queue,
  and delete only, with a short gray note pointing to the General tab for the
  active model.
- **Temp files for audio**: `transcribe_batch` writes WAV to temp file because `WhisperModel.transcribe()` is most reliable with file paths.
- **GUITHREADINFO duplication**: defined in both `text_inserter.py` and `window_focus.py`. Intentional — modules are self-contained.
- **SendInput restore delay (160ms)**: Empirical value. Some apps
  (Electron/Chrome) read clipboard asynchronously 50-100ms after Ctrl+V. 160ms
  prevents stale paste. `TextInserter` serializes app-initiated paste operations
  and checks the Win32 clipboard sequence/content before paste and before
  restore; if the user changes the clipboard during that window, leave the
  user's clipboard untouched and do not fallback-copy the transcript over it.
- **Paste hardening (2026-07-09)**: two real intermittent-paste races are
  closed in `text_inserter.py` and must not be reintroduced:
  - *Held hotkey modifiers*: inserts are often triggered straight from the
    WM_HOTKEY press (stop, cancel, queue flush), so the user's physical
    Ctrl/Alt was still down and the injected Ctrl+V reached the target as
    Ctrl+Alt+V (AltGr+V on German layouts) — silently pasting nothing (the
    transcript then existed "only in history"). The inserter now waits via
    `wait_for_modifier_release` (GetAsyncKeyState poll, bounded timeout)
    before injecting; WM_PASTE mode skips the wait because messages ignore
    keyboard state.
  - *Late clipboard read vs. restore*: a busy target (likely under local
    transcription CPU load) processes the injected Ctrl+V after the fixed
    restore delay and pastes the restored old clipboard instead of the
    transcript. The restore is now gated on the target thread answering
    WM_NULL again (`wait_for_paste_target_ready`); if the target stays
    unresponsive past the budget the restore is skipped so the eventual paste
    still reads the transcript. With `keep_transcript_in_clipboard` enabled
    the restore is skipped entirely, which closes this race completely.
  There is no Windows API that signals "the target read the clipboard"
  (delayed rendering is defeated by clipboard history/managers), so the
  fixed delay after the responsiveness gate remains a heuristic; the gates
  above shrink the window to practical irrelevance.
- **Deferred queue inserts are coalesced**: `_flush_deferred_background_results`
  groups token-ordered pending results by their captured insertion target and
  pastes each group as one space-joined text. Each separate paste is its own
  clipboard set/paste/restore race window, so N queued results used to mean N
  chances to lose one. Do not flush deferred results one paste per result.
- **`immediate_background_insert` (default off)**: continuous queue delivery —
  a finished queued transcription inserts into its captured window as soon as
  it completes, even while another transcription or an active **batch**
  recording is running (focus is restored to the job's target window; the
  original queue behavior). The modifier-release wait above is what makes this
  safe: the historical "insert near a hotkey press fails" bug was the
  held-modifier Ctrl+V corruption. A streaming capture never allows
  mid-recording pastes (live inserts write at the caret and a focus change
  aborts the stream); an in-progress recording start/stop always blocks.
  Deferral is decided per job in the flush
  (`_can_insert_during_active_recording`). In the UI this is folded into the
  "While transcribing" combo as a fourth choice (`insert_immediate` UI value in
  `_CONCURRENT_MODE_UI_CHOICES`); the stored settings stay
  `concurrent_transcription_mode` + `immediate_background_insert`.
- **`insert_target` setting**: `recording_window` (default) pastes into the
  window/control snapshotted at recording start; `current_window` pastes into
  whatever is focused when the transcript is ready. The caret position inside
  the target is always the position at insert time — Windows cannot paste at
  a remembered caret offset. With `current_window`, deferred flushes coalesce
  into a single paste since every result goes to the same target.
- **Warm microphone stream (`keep_microphone_warm`, default off)**: one shared
  PortAudio input stream stays open (`WarmMicrophoneStream`); a recording
  attaches as its consumer, so capture start is instant even where opening
  the microphone takes seconds (EDR/GPO-hooked audio stacks) and the first
  words were cut off. The controller owns its lifecycle (settings change,
  system resume, shutdown); a capture falls back to a cold stream when the
  warm one is not running. `recording_start_timing` logs beep and
  capture-start durations and warns above 500 ms.
  Warm-device opening happens outside its state lock so recording start never
  blocks behind an in-progress background open. Each capture installs a
  generation-scoped callback; callbacks retained by PortAudio after detach are
  ignored and cannot append audio to the next recording. Stream cleanup must
  always attempt `close()` even when `stop()` fails.
- **Silence gate (`silence_gate_enabled` + `silence_gate_threshold`, default
  off/0.004)**: batch recordings whose loudest 100 ms window stays below the
  threshold skip transcription entirely (speech models hallucinate words from
  silence). The windowed peak (`peak_windowed_rms_from_wav`) keeps short
  whispers detectable; every batch stop logs `recording_peak_level` for
  threshold tuning, and gated audio stays available as the last recording.
- **Overlay must never re-wrap or blink**: the transcript label wraps at a
  width derived from the target window width (never the live scroll
  viewport, which changes with deferred queue resizes and scrollbar
  visibility) and pre-measures the scrollbar case; `_apply_window_flags`
  calls `setWindowFlags` only when the flags actually change because it
  recreates the native window (a visible blink on every hotkey reveal
  otherwise). The Local/General model runtime note keeps a reserved
  three-line area and shows a neutral gray note for faster-whisper models so
  model switches never shift the layout.
- **Local model inventory cache**: last-known local model lists are stored in a dedicated JSON cache file, not `settings.json`, so the Local tab can render immediately without silently mutating user settings.
  Cached inventories are used for initial Local/Benchmark tab rendering, then
  disk verification starts automatically after the tab has had a chance to
  paint. App startup also refreshes the persistent inventory in the background.
  Source-tree and packaged runs isolate that scan in a subprocess so Python
  filesystem work cannot stall the Qt UI thread.
  Settings dialog lifecycle, tab paint, inventory render, and inventory scan
  timings are logged as `settings_timing` diagnostics for later troubleshooting.
  Local/Benchmark list widgets intentionally keep `AdjustToContents`; if first
  paint regresses again, use the timing diagnostics before changing this policy.
  The tray schedules a hidden settings-dialog preparation after startup so the
  first visible open and first Local tab paint avoid lazy Qt layout work. A
  hidden prepared dialog reloads settings from disk before it is shown.
- **Qt dialog feedback and refresh state**: transient button text such as
  "Copied" must reserve enough width for all feedback states via
  `ui_feedback.py` so layouts do not jump. Dialog/list refreshes should preserve
  selection, current item, and scroll position when the same entry still exists;
  use the shared scroll helper instead of rebuilding lists in a way that resets
  the user's place. Settings tabs use a session-stable default dialog size and
  `QScrollArea` `AdjustIgnored` to avoid small tab-switch resize jitter. Inline
  field buttons match the corresponding input height; action rows keep explicit
  spacing rather than relying on platform defaults. Settings tab selection must
  not change tab font weight or measured tab width; use color/border changes for
  the selected state. General-tab form sections share a measured label column so
  fields align across group boxes. Pressing Save with no effective setting or
  API-key changes must not emit `settings_changed`; otherwise the controller can
  reload or preload local models unnecessarily. The Benchmark tab hosts the
  *viewing* side directly (viewing results/history is frequent, running a
  benchmark is rare): a compact header row ("Run Benchmark..." button plus a
  fixed-height live status label) above the History/Results vertical splitter.
  The *run* side (audio sample picker, installed-model list with one compact
  row of small Select all/Deselect all/Refresh buttons, collapsible Run
  Options, Run/Cancel controls) lives only in the resizable, non-modal
  `benchmark_window` ("Run Benchmark", ~640x560, owned by the settings dialog
  so it hides when the dialog closes). Re-clicking the button raises/activates
  the existing window rather than creating a second one via
  `_open_benchmark_window`, which also refreshes the model list. Status is set
  through the single `_set_benchmark_status`, which feeds both the tab label
  and the window's own status line. Benchmark Results tables use per-pixel
  scroll modes. All benchmark widget attribute names are unchanged; only
  containers moved.
- **Settings dialog persists for the app lifetime**: closing Settings hides the
  existing dialog instead of deleting it. The dialog owns background model
  downloads, benchmark work, imports, scans, and connection/update checks, so
  recreating it while an old worker was alive could start overlapping work and
  discard the only UI tracking that worker. Reopening reloads stored settings
  into the same object before showing it. Every hide path, including
  `QDialog.reject()`, also hides the independent `Qt.Window` benchmark dialog.
  Reopening while idle reloads stored settings and discards unsaved provider-key
  edits; while dialog-owned work is active, that reload is deferred so the
  operation's snapshotted controls and busy state stay intact. Application
  shutdown calls `SettingsDialog.shutdown()` before controller shutdown so
  active model-download and benchmark child-process work is canceled and given
  a bounded cleanup window.
- **Streaming abort keeps the partial transcript**: `_abort_streaming_session`
  saves the best-known live transcript to history, keeps it as the last
  transcript for the overlay Copy action, shows it in the abort message, and
  reveals the overlay. An aborted stream must never lose already-transcribed
  text from UI/history.
- **Custom vocabulary** (`custom_vocabulary`, General tab): user terms parsed
  by `config.parse_custom_vocabulary` (newline/comma/semicolon split,
  case-insensitive dedupe, 100-term cap). Biasing per provider: faster-whisper
  `initial_prompt` (batch + rolling-window streaming), OpenAI/Groq `prompt`,
  AssemblyAI batch `word_boost` (streaming v3 has no biasing parameter),
  Deepgram repeated `keyterm` (nova-3) / `keywords` (nova-2) query params with
  `doseq` encoding. ElevenLabs, Azure, Fun-ASR, Nemotron, and Cohere/Granite
  ONNX expose no biasing input and stay unwired.
- **Multi-select lists use ExtendedSelection**: Shift selects ranges, Ctrl
  toggles, matching the file explorer. Do not reintroduce `MultiSelection`.
- **Remote connection test persistence**: last-known provider connection test
  results live in `provider_connection_tests.json`, not `settings.json`, because
  they are diagnostic UI state rather than configuration. The Remote tab should
  restore these labels on open and overwrite only the providers tested. Saving a
  new provider key or deleting a provider key must clear that provider's stored
  test result because the old result no longer describes the active credential.
- **Settings and credential saves are explicit and failure-safe**: toggling the
  insecure-storage checkbox changes only its pending UI until Save/Save API
  Keys. Failed key operations retain the typed value or pending delete and must
  stop unrelated settings/history mutations. Because credential backends are
  not transactional, any provider changed before a later failure still emits
  `settings_changed` to invalidate cached clients. Persist the settings file
  before trimming history; a failed settings write must never delete history.
- **Persistent JSON read-modify-write operations are path-serialized**:
  `persistence.lock_for_path` is the single in-process lock registry. Stores for
  history, benchmarks, settings, provider diagnostics, local inventory, last
  recording, and insecure keys reuse it so separate store instances cannot
  overwrite each other's concurrent updates. Keep writes atomic as well.
- **Update checks**: update discovery uses GitHub Releases directly through
  `update_checker.py`; no custom domain or update server is required. The app
  schedules one asynchronous check after startup and shows a tray notification
  only when a newer release exists. Manual checks are available from Settings
  and the tray menu. Keep update checks non-blocking and avoid downloading or
  executing installers automatically without a separate review. Release JSON is
  size-bounded, tags use strict numeric SemVer, and release links are restricted
  to this repository's HTTPS GitHub release paths. A manual request during the
  startup check promotes the active request so its result remains visible.
- **Local model download queue**: Settings downloads run serially through one
  worker process so Hugging Face cache writes and network usage remain
  predictable and the active download can be terminated safely. Additional
  models can be queued while a download is active. Cancel clears the queue and
  removes unusable `*.incomplete` files while preserving completed files for a
  later resume. Progress and its rolling transfer rate are approximate because
  they are derived from cache growth and the estimated total sizes in
  `MODEL_ESTIMATED_SIZE_MB`.
- **ModelScope mirror downloads are transactional and path-contained**:
  Treat every path in the remote file listing as untrusted. Only normalized
  POSIX-relative repository paths contained by the requested destination are
  accepted; absolute, drive-qualified, traversal, and backslash paths are
  rejected. The endpoint and redirects stay on HTTPS. Downloads and resumes
  write only to `*.incomplete`; a resume appends only after a matching HTTP 206
  and `Content-Range`, while an ignored range restarts the incomplete file.
  Publish a model file only after flushing, syncing, exact-size validation, and
  atomic replacement. Never expose a partial download at its final filename.
- **Manual model imports are transactional**: `scripts/import_model.py` hashes
  every imported model file, stages a complete snapshot under a temporary name,
  repairs legacy partial snapshots, publishes by atomic rename, and only then
  atomically updates `refs/main`. Copy failures must leave neither a final
  snapshot nor a reference to incomplete content.
- **Transcript history retention**: history defaults to 500 saved entries, and
  legacy settings that still have the old 20-entry default are migrated upward.
  Successful transcriptions are added to history before text insertion, so a
  paste/focus failure does not drop the transcript. The stored model name comes
  from the transcription settings snapshot, not from later UI changes.
  Settings History and the overlay History dialog both support multi-select
  copy/delete for bulk cleanup; editing remains single-entry only. History-limit
  spin boxes disable keyboard tracking: typed intermediate values are not
  applied until the edit is committed, so increasing a limit such as `224` to
  `300` never prompts to trim at the temporary `3` value. Re-clicking History
  while the dialog is open re-presents the existing window and refreshes it
  once via `reload(force=True)` (selection and scroll position are preserved);
  it must not create another dialog.
- **Managed audio imports snapshot content and identity**: importing the managed
  last recording captures immutable bytes plus `recording_id` before submitting
  work to the controller's serialized inference lane. Completion/failure state
  uses compare-and-set transitions, so an old import cannot clear or relabel a
  newer recording. Background/import history entries never replace the
  foreground transcript's Edit target. VAD auto-stop crosses from the audio
  worker through a Qt signal before touching controller/UI state.
- **History export/import/clear parity**: the standalone History dialog and the
  Settings History tab share the same export, import (including the overflow
  choice between "import only free slots" and "import all and set unlimited"),
  and clear flows via `history_ui_actions.py`, so the logic exists exactly once.
  Only feedback presentation (popup vs. inline status label) and how the active
  limit is read/persisted differ per caller. The Settings tab persists a
  switch-to-unlimited decision immediately (via `_settings_store` plus
  `dataclasses.replace` on `_loaded_settings`), the same way the dialog does,
  so a later Save does not see it as a phantom change.
- **AssemblyAI pre-recorded model selection**: use the current `speech_models`
  parameter for batch/import requests. `universal-3-pro` is sent with
  `universal-2` fallback; legacy `best`/`nano` settings are migrated to the
  current default in settings persistence and are not shown in the UI.
- **ElevenLabs batch model selection**: `scribe_v2` is the only supported model.
  ElevenLabs removed `scribe_v1` on 2026-07-09; legacy stored selections migrate
  to `scribe_v2` and the removed identifier must not be sent to the API.
- **AssemblyAI streaming (Universal-Streaming v3)**: the legacy v2 realtime
  API is retired and must not be reintroduced. Streaming uses
  `assemblyai.streaming.v3.StreamingClient` with the
  `universal-streaming-multilingual` model, language detection, and formatted
  turns; the batch model selection does not apply to streaming. Turn text is
  keyed by `turn_order` because the formatted end-of-turn transcript arrives
  as a second event for the same turn. Bound SDK `disconnect` joins with a
  helper thread; they can hang on dead connections.
- **Streaming provider sends must not block the audio callback**:
  `push_audio_chunk` runs on the PortAudio callback thread. Providers must
  only enqueue there (Deepgram has a dedicated sender thread; the AssemblyAI
  SDK and local transcribers queue internally) and never perform blocking
  socket I/O.
- **Remote streaming sessions are generation-scoped**: AssemblyAI SDK events
  and Deepgram WebSocket callbacks must match both the current session
  generation and the exact client/socket before changing transcript, error, or
  lifecycle state. Starting and retiring are explicit states, so a partially
  connected or bounded-shutdown session cannot overlap a replacement session.
  Deepgram's sender queue is bounded and `push_audio_chunk` uses only
  `put_nowait`; saturation fails the stream rather than dropping audio or
  blocking PortAudio. Normal stop first drains queued binary audio through a
  sender barrier, then sends `Finalize`, waits best-effort for the optional
  `from_finalize` response, and sends the documented `CloseStream` command.
  Control sends and all waits are bounded; a failed drain/control path closes
  the socket without allowing control frames to overtake queued audio.
- **Deepgram streaming language**: the live WebSocket API rejects
  `detect_language`; auto maps to `language=multi` (nova-2/nova-3
  multilingual code-switching). Batch keeps `detect_language=true`.
- **AltGr hotkey alias**: Windows reports AltGr as Ctrl+Alt. The hotkey
  manager ignores Ctrl+Alt hotkey messages while the right Alt key is down so
  AltGr combinations do not trigger dictation accidentally.
- **Hotkey state follows Win32 cleanup success**: a failed `UnregisterHotKey`
  keeps the manager marked registered and blocks replacement registration.
  Shutdown logs and continues, while disabling a cancel hotkey reports the
  cleanup failure instead of pretending the key was released.
- **Overlay visibility after activity/resume**: every recording start *and
  stop* (and a hotkey press while a streaming finalize is pending) re-presents
  the overlay without activation and reasserts native Windows topmost z-order,
  so a floating overlay shows the new state on the hotkey press itself rather
  than only after the transcript finishes. The reveal is non-activating
  (`reveal_temporarily`), so focus stays on the target window and the pending
  insertion is unaffected. `WM_POWERBROADCAST` resume events also restore
  overlay visibility and refresh both global hotkey registrations after
  display/session state has stabilized.
- **Model-aware language selection**: `config.language_modes_for_selection()`
  is the shared source of truth for the General-tab language list, the overlay
  quick selector, and provider validation. The overlay persists a selection for
  the next recording, disables changes while listening/processing, and shows a
  disabled `Lang: Auto` button when automatic detection is the only mode.
  Auto remains the persisted default where supported; Cohere requires an
  explicit language and therefore never exposes Auto.
- **Remote first-request diagnostics**: transcription workers log
  `transcription_timing` with initialization, transcription, and total
  durations. Groq reuses its SDK/HTTP client for the lifetime of the cached
  transcriber so later requests can reuse connections.
- **Line endings**: Repository text files are normalized to LF via `.gitattributes`; `.editorconfig` mirrors that policy so Windows/WSL edits do not create CRLF-only diffs.
- **Windows packaging**: end-user builds are layered. PyInstaller `onedir`
  is the base portable bundle; Inno Setup wraps that bundle into the
  installer; GitHub Actions builds artifacts manually on demand and publishes
  only on version tags. Official `v*` release tags must match
  `pyproject.toml`'s project version and must not be older than an existing
  numeric release tag. Standard releases should use
  `python scripts/create_release.py` from a clean, up-to-date `main`; the script
  prompts for the version, bumps metadata, runs checks, commits when metadata
  changed, pushes, tags, and pushes the tag. GitHub Actions release notes that
  contain Markdown backticks must use a literal PowerShell here-string (`@'`) so
  asset-name backticks are not consumed as PowerShell escapes.
- **Continuous quality gates**: `.github/workflows/quality.yml` runs Ruff and
  the complete pytest suite on Windows for `main`, review branches, and pull
  requests. It also audits the locked production JavaScript dependency tree on
  Linux. Keep release publishing separate in `windows-release.yml`.
- **Release builds are locked and prevalidated**: Windows builds use
  `uv sync --locked` and `npm ci`, then run Ruff, all tests, and the production
  dependency audit. Release creation rejects tracked and untracked worktree
  changes. Version bumping prepares every metadata edit before writing and
  rolls back earlier files if a later atomic write fails.
- **Portable Node bootstrap security**: `scripts/setup_node_windows.py` accepts
  numeric `major.minor.patch` versions only, verifies every downloaded archive
  against that release directory's `SHASUMS256.txt`, and rejects ZIP members
  that escape the selected install directory. Keep all three checks when
  changing download mirrors or extraction behavior.
- **Local ONNX ASR**: Cohere Transcribe, IBM Granite Speech 4.0,
  and IBM Granite Speech 4.1 are selectable local models through
  `transcriber/local_webgpu_asr.py`. They are batch-only and require Node.js.
  These are supported daily-use models, not experimental trials; do not
  reintroduce "experimental" framing in UI labels or user-facing model docs.
  Cohere, Granite 4.0, and Granite 4.1 2B use q4 ONNX snapshots through the
  high-level Transformers.js `GraniteSpeechForConditionalGeneration` pipeline.
  Granite 4.1 2B points at `onnx-community/granite-speech-4.1-2b-ONNX` (verified
  on WebGPU / Arc A750 on 2026-06-17: correct de/en/fr, no `Einsum` crash).
  Granite 4.1 **Plus** and **NAR** stay on raw INT8 `onnxruntime-node` graph
  sessions because they are different architectures (`granite_speech_plus` /
  `granite_speech_nar`) with no faithful q4 Transformers.js package — see
  `docs/granite-speech-4.1-onnx-variants.md` for the full status, the three
  blockers, and what would change that. Do not relabel a Plus build as base
  `granite_speech` to force it onto the pipeline path: that produces broken
  English (verified with the valoomba build).
  The raw Granite 4.1 Plus/NAR graphs run through `onnxruntime-node` execution
  providers: `webgpu_asr_runner.mjs` `ortExecutionProviders` returns
  `webgpu`/`dml`/`cpu`, so `auto`/`gpu` mode tries WebGPU, then DirectML, then
  CPU. DirectML ships with `onnxruntime-node` on Windows. GPU acceleration of
  these raw graphs is unverified (WebGPU `Einsum` shader bug, DirectML operator
  gaps) and they usually run on CPU; the active device is reported in the runtime
  status. This raw path is separate from the Cohere / Granite 4.0 / Granite 4.1
  2B Transformers.js pipeline path.
  They are not preloaded and are closed after normal batch dictation to avoid
  idle ONNX/Node CPU load.
  The resolved runtime device is reported through transcriber progress messages
  so the overlay/import UI can show whether WebGPU, DirectML, or CPU was used.
  Keep faster-whisper as the stable local default until real target-hardware
  benchmarks justify switching.
  The Granite 4.1 Plus (AR) and NAR raw paths must stay separate from each other
  and from the pipeline path because their ONNX graph contracts differ. Keep
  `granite-4.0-1b-speech` selectable as a smaller q4 option until real
  benchmarks justify removing it.
- **Nemotron 3.5 true streaming**:
  `nemotron-3.5-asr-streaming-0.6b-int4` uses the published 793 MB multilingual
  ONNX Runtime GenAI export through `transcriber/local_nemotron.py`. It reuses
  the model's encoder cache and emits incremental RNNT tokens every fixed
  560 ms chunk instead of re-transcribing a rolling window. The published ONNX
  graph is fixed to 560 ms even though the original NeMo model supports other
  latency profiles. The app ships the installable CPU ORT GenAI package and
  tries DirectML first when a compatible DirectML runtime is present. As of
  2026-06-08, Microsoft's DirectML GenAI package depends on an unpublished
  `onnxruntime-directml>=1.26.0`, so reproducible installs fall back to CPU.
  A Ryzen 5 7600X benchmark measured 0.229 RTF and 0.81 s cold load on the
  repository sample. Nemotron stays preloaded and cached like faster-whisper so
  pressing the recording hotkey does not block on model loading. Its internal
  runtime VAD follows the app's VAD setting. The language UI exposes only the
  transcription-ready and broad-coverage official prompt IDs.
- **Streaming availability**: `config.supports_streaming()` is the shared
  source of truth for UI and controller checks. Cohere/Granite ONNX/WebGPU
  models are batch-only; Nemotron is true streaming. A local model selection
  must not disable remote provider streaming for AssemblyAI or Deepgram.
- **Streaming text state**: Keep provider partial-text reconciliation in
  `streaming_text.py`; the controller should only orchestrate
  Qt/audio/focus/insertion side effects.
  Streaming insertion is append-only: do not use live partial revisions to
  select/delete previously inserted text.
  Local rolling-window partials may be merged by safe word overlap, but only to
  append new text.
- **Streaming finalization**: the full re-transcription of the recording when
  local faster-whisper streaming stops is opt-in via
  `streaming_full_final_transcript` (default off). When off, finalization
  transcribes only the trailing partial window and merges it into the
  provider-tracked live transcript, so stop returns quickly and the history
  entry matches the streamed text. Inserted text stays append-only either way.
- **Concurrent transcription mode + cooperative cancel**: a finished
  transcription is *never* discarded. `concurrent_transcription_mode`
  (`insert` default / `history` / `cancel`) decides what happens to the
  in-flight transcription when a new recording starts: `insert` keeps it and
  inserts its result into the window that was focused when it was recorded
  (plus history); `history` keeps it but only saves to history; `cancel`
  requests a real stop and, if it still finishes, keeps it in history. Local and
  remote share the single `max_workers=1` transcription executor, so jobs
  serialize — this only changes delivery, never runs two at once. Each recording
  snapshots its target window into a `_TranscriptionJob`; the job also carries
  `background_delivery` (`insert`/`history`) and `aborting`. A result is
  "foreground" only when its token is active, no newer recording is active, and
  the job is not aborting — `_new_recording_active()` intentionally excludes
  `_streaming_recording` because a pending streaming finalize keeps that flag
  True. Background results are delivered via `_handle_background_transcription_ready`
  per `background_delivery` (streaming finalize is always history-only).
  Progress, ready, and failed signals must all use the same foreground check;
  background or aborting job progress must not switch the overlay back to
  Processing. Never reset foreground session state from a background result
  handler.
  Explicit cancel — the overlay per-row ✕, Clear queue, and the Cancel button —
  goes through `_request_job_stop` (delivery `history`): it sets `aborting` (so a
  not-yet-started worker skips and a cooperative transcriber stops) and cancels
  the future if it has not started. Real mid-run abort exists for faster-whisper
  via `set_cancel_check` (polled between segments → raises `TranscriptionCanceled`
  → worker emits `transcription_canceled`); other engines only skip-if-not-started
  and otherwise run to completion with their result kept in history.
  Stopping the pending streaming finalize ends that streaming session:
  `_request_job_stop` clears the session state so the next recording is not
  blocked behind a finalize that now resolves history-only. Clear queue routes
  through the per-row cancel so a canceled foreground job is reflected in the
  overlay instead of leaving a stale "Processing" state.
  The overlay queue is a temporary size extension: all in-flight rows are
  rendered inside a scroll area (`_queue_scroll`), so the overlay grows only up
  to `OVERLAY_QUEUE_MAX_HEIGHT` (bounded by the screen) and the queue scrolls
  beyond that instead of expanding to full screen height, the same way long
  transcript text does. `_apply_queue_scroll_height` bounds the rows so the
  detail area keeps at least `OVERLAY_DETAIL_MIN_HEIGHT`; it measures the rows
  via the *layout* sizeHint (the widget sizeHint is inflated by the minimum
  height it sets, which would be self-reinforcing). `set_transcription_queue`
  re-asserts the size after the event loop drains (a deferred
  `_refresh_size_after_queue_change`) because switching between very different
  queue sizes, or clearing a grown queue with a short final result, otherwise
  leaves a stale pending resize; hiding the queue must return the window to the
  normal compact/non-queue size. The cancel hook must be cleared after each batch
  run so it cannot leak into the cached
  transcriber's next request.
  Deferred background inserts (`_deferred_background_results`) must be flushed on
  every path that clears the blocking session — recording start/stop,
  streaming-session abort and stream runtime failure (after the capture/stream
  teardown, not before), `cancel_current_action`, and
  `cancel_queued_transcription` (the overlay per-row ✕ / Clear queue) — so a
  completed insert-mode transcript is never left pending in the queue after
  nothing is blocking it. In particular, canceling the newest/foreground job
  from the queue clears `_active_request_token`, which was blocking earlier
  finished transcripts; those must be delivered, not dropped alongside the
  canceled job.
  `_should_defer_background_insertion`/`_flush_deferred_background_results` take
  `ignore_active_transcription`: an active recording/capture (or in-progress
  start/stop) is always a hard blocker (never insert mid-recording), but on an
  **explicit user cancel** (`cancel_current_action` incl. its "nothing to
  cancel" path, `cancel_queued_transcription`, and `_abort_streaming_session`)
  the flush passes `ignore_active_transcription=True` so a completed result is
  delivered immediately instead of waiting behind an *unrelated* in-flight
  transcription. Deferred tokens are always older than the active one, so
  delivering them first keeps token order intact; the still-running
  transcription delivers itself later with no duplicate. Normal (non-cancel)
  flow keeps the `_active_request_token` guard so background text is not
  inserted mid-foreground-session.
- **Do not close an in-use transcriber runtime**: never close/reset the cached
  transcriber while `_transcription_runtime_active()` (an active capture,
  in-progress start, live stream, or in-flight transcription). Closing there can
  break a keep-loaded ONNX subprocess (its `close()` shares the worker's stdin
  and takes no batch lock) or tear down a live Nemotron stream. `reload_settings`
  defers the reset via `_pending_transcriber_cache_reset`. Preload, batch, and
  streaming acquire a `_TranscriberRuntimeLease`: one lease owns the shared
  cache, while overlapping normal work receives an isolated close-on-release
  runtime so the Qt thread never waits behind inference. Preload waits off-thread
  for the shared lease so a successful preload remains cached. A shared owner
  applies deferred reset/close only on release; isolated owners leave it for the
  next shared acquisition. Canceled workers count as active until their lease is
  released. Worker terminal signals are emitted only after hooks are cleared and
  the lease (including any deferred close) is finished. Shutdown marks the
  controller closed before canceling work; late signals are ignored and an
  in-use cache closes from its final owner rather than the shutdown thread. The
  resume path uses the same shared-runtime admission lock.
- **Overlay corner vs. dragged position**: after a settings save, apply the
  corner through `OverlayUI.apply_corner_setting`, which repositions only when
  the configured corner changed. Never call `move_to_corner` unconditionally
  on save; it would discard a manually dragged overlay position.
- **App icon**: `src/stt_app/assets/app_icon.ico`/`.png` are generated by
  `scripts/generate_app_icon.py` and committed. `app_icon.py` is the single
  loader; the icon is wired into the Qt app/tray icons and the Settings and
  History dialog windows (with a standard-icon fallback), the wheel, the
  PyInstaller bundle/EXE, and the Inno Setup installer. Rerun the script only
  when the design changes.
- **Release script behavior**: `scripts/create_release.py` can tag an already
  bumped current project version when it is newer than the latest numeric
  release tag. It commits release metadata only when files actually changed, so
  a pre-bumped `0.4.0` main can still be released as `v0.4.0` without a dummy
  bump commit.
- **Last recording selection**: `LastRecordingStore.selectable_path()` is the
  single selection point for "Use last recording". When an archived recordings
  directory is supplied, it chooses the newest managed/archive WAV, but
  recoverable managed recordings still win so retry/recovery state remains
  intact.
- **Benchmark environment metadata**: benchmark summaries and exports include a
  best-effort system context from `benchmark_environment.py`. Keep hardware,
  OS, Python, Node.js, and local runtime/framework version collection there so
  Settings, history exports, and the CLI benchmark do not drift. ONNX benchmark
  cases also persist concise runtime fallback details so a CPU result explains
  why WebGPU or DirectML was rejected.
- **Benchmark runs out-of-process**: the Settings benchmark loads
  faster-whisper/ONNX models back-to-back; model loading does not release the
  Python GIL reliably, so running it in a background *thread* still froze the Qt
  UI. `benchmark_process.run_benchmark_cases` therefore launches
  `benchmark_worker` (a child process running the pure
  `local_benchmark.run_benchmark_cases`) and streams `progress`/`case`/`done`
  events as `@@STTBENCH@@`-prefixed JSON lines on stdout; the parent translates
  them back into the same `progress_callback`/`case_callback` and returns the
  same `list[BenchmarkCase]`. The settings-dialog facade re-exports this under
  the name `run_benchmark_cases`, so the Qt-facing benchmark code and the test
  seam (`stt_app.settings_dialog.run_benchmark_cases`) are unchanged. Cancel
  terminates the child process tree (`taskkill /T` on Windows) and raises
  `BenchmarkCancelled`; cases finished before the cancel are already streamed
  and kept. Keep the pure in-process function for the CLI and the worker; only
  the settings dialog goes through the process path. Wire new worker args into
  the frozen entry point (`main.py`) and the PyInstaller `hiddenimports`.
- **Normal transcription stays threaded, not isolated**: batch/stream
  transcription runs in the shared `max_workers=1` executor with models
  preloaded; faster-whisper (CTranslate2) and ONNX Runtime release the GIL
  during inference and the Cohere/Granite Node path is already its own
  subprocess, so dictation does not freeze the UI. Do not move it to a
  subprocess — that would break the preload latency guarantee and streaming.
- **Local streaming/runtime state is generation-scoped**: faster-whisper and
  Nemotron workers own immutable session objects, so a timed-out retired worker
  cannot consume or publish into a replacement session. Nemotron keeps native
  model/runtime objects alive until every retired worker exits. The ONNX Node
  parent serializes lifecycle/stdin, uses process-local bounded reader state and
  absolute deadlines, and kills a timed-out or protocol-poisoned child before
  reuse. The JS server serializes requests and rejects oversized protocol lines
  and malformed/out-of-bounds WAV layouts before allocation.
- **Overlay reveal after a result**: a floating (non-pinned) overlay is a tool
  window (no Alt+Tab) and can hide behind other windows. The controller calls
  `_reveal_overlay_result` after a finished transcription — briefly on success
  (`OVERLAY_RESULT_REVEAL_MS`) and longer on errors/insertion failures
  (`OVERLAY_ERROR_REVEAL_MS`) so the transcript can still be copied. A tray
  "Show overlay" action (`controller.bring_overlay_to_front`) is the manual
  escape hatch. Reveals are best-effort (wrapped so a missing overlay method
  never breaks delivery).
- **Windows taskbar identity**: `main._set_windows_app_user_model_id` sets an
  explicit `APP_USER_MODEL_ID` before the first window is created. Without it
  Windows groups our windows under the host process (python.exe) and shows its
  generic icon on the taskbar (most visibly for the Settings dialog). Keep the
  ID stable so taskbar pinning/grouping is consistent.

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
  azure, funasr
- **STREAMING_ENGINES**: local, assemblyai, deepgram (others are batch-only)
- **Azure LLM Speech** needs two settings: `azure_endpoint` (per-resource, e.g.
  `https://<resource>.cognitiveservices.azure.com`) and the `azure` key in the
  secret store. Model select picks `mai-transcribe-1.5` / `mai-transcribe-1`.
- **Fun-ASR (Alibaba)** is key-only (`funasr` key, Singapore-region DashScope),
  driven over the realtime WebSocket in batch mode. It covers 31 languages but
  **not German** (`FUNASR_LANGUAGE_MODES` excludes `de`).
- All engine/model constants defined in `config.py`

## Tests

- Preferred on Windows: `.venv\Scripts\pytest.exe -q`
- Alternate when the environment supports it: `uv run python -m pytest` or `python -m pytest`
- Note: the project uses a uv-managed Windows `.venv`; `pytest.exe` may be available even when `python -m pytest` or `python -m pip` is not.

## Known limitations

- Streaming: inserted text is append-only and never rewritten; focus-change
  abort remains best-effort.
- ARM CPUs: not supported (CTranslate2 requires x86 AVX/SSE).
- Clipboard restore: Unicode text only.
- NVIDIA Parakeet remains intentionally unimplemented through NeMo; Nemotron
  uses the separate ONNX Runtime GenAI path. See
  `docs/local-asr-model-candidates-2026.md` for rationale.
