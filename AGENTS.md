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
- Remote providers: AssemblyAI (SDK batch + Universal-3.5 Pro realtime),
  OpenAI (REST API), Groq (SDK), Deepgram (REST + WebSocket),
  ElevenLabs (REST API), Azure LLM Speech / MAI-Transcribe (REST, batch-only),
  Fun-ASR / Alibaba (DashScope WebSocket, batch-only, no German)
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
| `transcriber/local_onnx_asr.py` | Batch-only NVIDIA NeMo models (Parakeet TDT, Canary) via the pure-Python `onnx-asr` runtime; CPU only, no Node.js; mid-run cancel via ONNX Runtime `RunOptions.terminate` |
| `transcriber/local_webgpu_asr.py` | Shared local ONNX inventory/download helpers plus the batch-only Cohere/Granite Node.js runtime (supported daily-use GPU models); cancel kills the child |
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
| `settings_dialog_general.py` | General tab: hotkeys, display, engine/model/language/mode selection, and text-insertion mixin (owns `model_combo` for local models and `remote_model_combo` for remote models, unified in one stacked "Model" row) |
| `settings_dialog_audio.py` | Audio & Recording tab: microphone picker, warm stream, VAD, silence gate, start/completion tones, and recordings retention mixin (split from the General tab) |
| `settings_dialog_local.py` | Local tab: local-model management mixin (inventory, scan, download queue, delete only; model selection lives on the General tab) |
| `settings_dialog_benchmark.py` | Benchmark tab (history + results + live status) plus the pop-out Run Benchmark window (model selection, options, run controls) mixin |
| `settings_dialog_remote.py` | Remote tab: provider API keys and connection-test mixin |
| `settings_dialog_history.py` | History tab: transcript list, edit, copy, delete, retained-audio reveal/retranscription mixin |
| `settings_dialog_import.py` | Import Audio tab and recordings-directory helpers mixin |
| `settings_dialog_persistence.py` | Settings load/populate/build/save and key persistence mixin |
| `settings_store.py` | JSON settings persistence (`%APPDATA%\stt_app\settings.json`) |
| `persistence.py` | Atomic file writes, strict JSON booleans, recovery helpers, and shared path-scoped locks |
| `csv_safety.py` | Spreadsheet-formula neutralization for user-controlled CSV cells |
| `benchmark_history.py` | Persistent benchmark run history (JSON) with export |
| `ui_feedback.py` | Shared Qt button feedback styles, stable feedback widths, scroll restoration helpers |
| `dialog_style.py` | Shared message-box/dialog colours plus the app-wide filter that makes error text selectable |
| `local_model_inventory_store.py` | Persistent cache of last-known local model inventories keyed by `model_dir` |
| `local_model_download.py` | Cancellable source/packaged worker-process launcher for local model downloads |
| `model_download_coordinator.py` | The single download slot; serializes every download path — in-process and, via `file_lock`, across processes |
| `file_lock.py` | OS-level cross-process advisory lock (`msvcrt.locking` / `fcntl.flock`) used to make the download slot machine-wide |
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
| `window_focus.py` | Win32 foreground/focus/caret window tracking for text insertion |
| `win_tray_icon.py` | Hand-registered Windows notification icon (`Shell_NotifyIcon` + native menu) with a `QSystemTrayIcon` fallback |
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
  (`_GeneralTabMixin`, `_AudioTabMixin`, `_LocalModelsMixin`, `_BenchmarkMixin`,
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
- **General tab hosts daily-use settings; capture setup lives on Audio &&
  Recording**: the General tab kept growing until it needed its own scroll
  marathon, so the set-and-forget capture groups ("Audio && Voice Detection"
  and "Recordings") moved to a dedicated Audio && Recording tab directly
  after General (`settings_dialog_audio.py`). General keeps Hotkeys, Display,
  Engine && Mode, and Text Insertion — what actually changes during daily
  dictation. Widget attribute names are unchanged, so persistence and the
  controller are unaffected; `_build_audio_tab` must run after
  `_build_general_tab` because it applies the shared label column across both
  tabs.
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
- **`SMTO_ABORTIFHUNG` is why the readiness probe needs its own sleep**: that
  flag makes `SendMessageTimeoutW` return *immediately* when the target thread
  is already hung, instead of waiting out the timeout it was given. So
  `wait_for_paste_target_ready`'s loop had no delay in it at all against the
  one case it exists for: measured 953,446 probes inside a single budget
  window, one core pinned and the Qt thread unavailable for the whole time,
  from nothing worse than pasting into a frozen application. It polls at
  `PASTE_TARGET_RESPONSIVE_POLL_INTERVAL_S` and returns early for a handle
  that is no longer a window. Never assume a Win32 timeout throttles a loop.
- **A short `SendInput` past the key-down may already have pasted**: the batch
  is `[Ctrl down, V down, V up, Ctrl up]` and applications paste on the
  key-down, so two delivered events are already a paste. `_send_input_batch`
  takes `committed_after` and raises `TextMayHaveBeenPastedError` once the
  count reaches it, rather than reporting a clean failure -- which had
  `send_paste_with_mode`'s auto path fall through to `WM_PASTE` and paste the
  transcript a second time, with the clipboard restore running as if nothing
  had happened. Every arm that sees that exception must also leave the
  clipboard alone. Known gap, currently unreachable: the re-raise does not
  honour `allow_clipboard_fallback`, because no caller combines the two.
- **Deferred queue inserts are coalesced**: `_flush_deferred_background_results`
  groups token-ordered pending results by their captured insertion target and
  pastes each group as one space-joined text. Each separate paste is its own
  clipboard set/paste/restore race window, so N queued results used to mean N
  chances to lose one. Do not flush deferred results one paste per result.
- **Transcript spacing is local to one coalesced queue paste**: normal
  foreground, background, and streaming inserts preserve their supplied text
  exactly. `_flush_deferred_background_results` is the only path that joins
  separate completed queue messages, using `_join_transcripts` to place one
  space between adjacent messages in that one paste. Do not infer or prepend
  whitespace across separate pastes.
- **`immediate_background_insert` (default off)**: continuous queue delivery —
  a finished queued transcription inserts into its captured window as soon as
  it completes, even while another transcription or an active **batch**
  recording is running (focus is restored to the job's target window; the
  original queue behavior). The modifier-release wait above is what makes this
  safe: the historical "insert near a hotkey press fails" bug was the
  held-modifier Ctrl+V corruption. A streaming capture never allows
  mid-recording pastes (live inserts write at the caret, and a focus change
  suspends them); an in-progress recording start/stop always blocks.
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
  always attempt `close()` even when `stop()` fails. The overlay must not show
  the ready-to-speak instruction until `capture.start()` has succeeded; its
  preceding wait message keeps slow cold opens from inviting speech that cannot
  yet be recorded. A streaming controller session is published before
  `capture.start()` so a first callback delivered from inside that call reaches
  the active transcriber instead of being discarded.
- **Microphone selection and audio device changes**: `input_device_name`
  (Audio && Recording-tab picker; empty = system default) is resolved to a PortAudio index
  only at stream open via `audio_devices.resolve_input_device`; a selected but
  missing microphone fails the recording with an actionable error — never
  silently record from another device. Explicit selections resolve to WASAPI
  device indices, and WASAPI shared mode rejects the app's 16 kHz capture
  rate with paInvalidSampleRate (-9997) when the endpoint mix format differs
  (typically 48 kHz) — the MME sound mapper behind the default path resamples
  transparently, which is why only explicit selections failed. Every
  `sd.InputStream` open therefore passes
  `audio_devices.input_stream_extra_settings(device_index)`, which returns
  `WasapiSettings(auto_convert=True)` for WASAPI devices so PortAudio
  resamples like the default path; do not open input streams without it. PortAudio freezes its device list at
  initialization, so `audio_devices.try_refresh_input_devices` re-initializes
  PortAudio; a shared open-lock plus live-stream registry makes re-enumeration
  impossible while any stream is open (it is refused and retried, never allowed
  to invalidate a running capture). `audio_device_listener.py` registers an
  `IMMNotificationClient` (comtypes, event-driven — no polling) so a Windows
  default-capture switch or hot-plug immediately triggers the controller's
  coalesced reaction: close the idle warm stream, re-enumerate, reopen. While
  a recording is active the refresh defers and resumes on the stop/abort
  paths. The warm stream owns its lifecycle races: `request_restart` /
  `request_close` defer while a consumer is attached and execute on detach, so
  disabling the setting, a resume, or a device event can never cut off a
  running recording's audio source; `attach` additionally requires the warm
  stream's `opened_device_key` to match the recording's selected device. A
  first-callback watchdog timeout on a warm capture restarts the warm stream
  automatically (self-heal) instead of only suggesting to disable the feature.
  Without COM/comtypes the listener is inert and the Settings "Refresh" button
  plus watchdog self-heal remain the manual/backstop paths.
- **First audio callback watchdog**: after a successful capture start, a bounded
  Qt timer verifies that PortAudio actually delivered a callback. A timeout is
  an abort, never a normal stop/transcription: a callback can race just after
  the timeout check, so any late bytes are retained for Retry but are not
  submitted automatically while an Error is shown. Snapshot warm-stream and
  callback-count diagnostics before `capture.stop()` mutates them.
- **Silence gate (`silence_gate_enabled` + `silence_gate_threshold`, default
  on/0.004)**: batch recordings whose loudest 100 ms window stays below the
  threshold skip transcription entirely (speech models hallucinate words from
  silence). It defaults to on because it is the only guard that covers *every*
  engine: the Cohere/Granite runtime exposes no VAD and no no-speech
  probability at all, and faster-whisper's `vad_filter` is tied to the
  auto-stop checkbox, which is off by default. The threshold is ~-48 dBFS on
  the *loudest* window (`measure_peak_windowed_rms`), which a whisper clears
  with room to spare (measured: -40 dBFS whisper → 0.0071 vs. 0.0040 gate,
  while room tone at -54 dBFS is blocked). Every batch stop logs
  `recording_peak_level` for tuning, and gated audio stays available as the
  last recording. Unmeasurable audio returns `None` and must never be gated —
  undecodable bytes are a failure to surface, not silence. Schema 22 turns the
  gate on once for older settings files: every file written before the default
  flip carries "off", so a stored "off" could not be told apart from a
  deliberate choice; an "off" saved at schema >= 22 is kept. Field data from
  one session backs the threshold: 26 silent/hallucinated recordings measured
  0.0006-0.0034 and 7 real utterances 0.0075-0.0290, so 0.0040 separates them
  with 1.9x margin below and 1.2x above.
- **Empty model text is a failure, not "no speech"**: once a recording has
  passed the silence gate, a blank `transcribe_batch` result means the model
  missed the utterance. Parakeet TDT does this on some 1-2 s clips that
  still contain clear speech (replayed: peak 0.12, Whisper recovered the
  text, padding either dropped words or invented new ones). Do not treat
  that as the silence-gate Done path: show Error, keep the WAV for Retry,
  leave `_last_transcript` alone, and log `chars=0
  outcome=empty_transcript`. Streaming finalization may still legitimately
  be empty.
- **Overlay changes of one event go through `batched_update`**: most
  transitions touch the queue panel *and* the state text (a finished
  transcription clears its queue row, then publishes the transcript). Applied
  separately the window resized twice — measurably 183 → 137 → 269 px — and the
  frame in between showed the previous content at the already-changed size.
  `OverlayUI.batched_update()` defers the geometry to the end of the block and
  resizes with painting suppressed, then repaints, so size and content land
  together. The controller wraps its Qt slots via `_overlay_batch()`
  (`toggle_recording`, transcription ready/failed). Geometry stays synchronous
  outside a batch, so direct `set_state` calls still resize immediately.
- **`show_idle_status` never overwrites a live session**: the preload
  completion arms `singleShot(..., show_idle_status)` when nothing is running,
  but the timer fires 1.2-1.8 s later — long enough for the user to have
  started dictating. The overlay then showed "Idle" during an active capture,
  and pressing the hotkey again to "start" actually stopped it mid-sentence.
  `show_idle_status` therefore re-checks `_overlay_session_active()` at fire
  time; any new delayed overlay writer must do the same.
- **Background transcription failures are reported, never silent**: a queued
  job that fails while a newer session owns the overlay emits
  `background_transcription_failed` (tray notification in `main.py`) naming the
  recording and whether its audio was kept for Retry, and additionally shows
  the error on the overlay when no live session owns it. Delivering a success
  but dropping a failure made a lost recording indistinguishable from one that
  was never transcribed.
- **Overlay window resizes go through `_resize_window`**: `QWidget.resize`
  clamps to the widget's *current* minimum size, and that minimum is only
  recomputed when the layout is activated (normally deferred to the next event
  loop pass). Right after shrinking the detail/queue areas the window still
  carried the previous state's larger minimum, so the resize was silently
  swallowed and a short error after a long transcript kept the expanded height.
  `_resize_window` activates both layouts first; never call `self.resize(...)`
  directly for the overlay window. The styled container's stylesheet border
  becomes part of its contents margins, so every size computation adds
  `_container_frame_margins()` and `set_state` applies the state stylesheet
  *before* measuring — without that the computed target was 2 px below the real
  layout minimum and `OVERLAY_MAX_HEIGHT` did not hold.
- **Overlay primary action and shared action slot**: the header starts with the
  Record/Stop button (`record_toggle_requested` → `controller.toggle_recording`)
  so dictation can be started without a keyboard; its caption swaps between two
  fixed-width captions and the recording state is a stylesheet property, so
  neither changes the layout. Its state indicator is a *generated icon*
  (`_OverlayRecordButton`), not a caption glyph: "●"/"■" sit on the font
  baseline, so the dot rendered 1.5 px below the button's middle, the square
  1 px, and the indicator jumped because the glyphs differ in height. Qt lays
  an icon out vertically centred; the icon carries its own trailing gap
  because Qt otherwise places icon and caption almost flush. The button keeps
  the same fill as its neighbours — a lighter fill reads as a permanent hover
  state — and is marked as primary by a brighter border only.
- **`get_foreground_window` never answers with one of our own windows**: it
  used to end in `self._remembered_foreign_window() or hwnd`, and that `or
  hwnd` fires exactly when one of our tool windows is in front and nothing
  foreign has been remembered yet -- on a fresh session, every path before the
  first recording. The tray menu is one: the notification-icon contract
  requires `SetForegroundWindow` on the hidden 0x0 host window before
  `TrackPopupMenu`, so the first dictation started from the menu aimed at that
  window. Worse than the lost paste, `restore_target_window` calls
  `ShowWindow(SW_SHOW)` on the target, which makes the helper window visible,
  so it then passes the own-non-target predicate and is cached as the last
  foreign window for the rest of the session. `None` is the honest answer and
  the insert path reports it. `note_foreground_window()` (best-effort, records
  only a valid foreign window) is the other half: `main.on_tray_activated`
  calls it first, because `activated` is emitted before the menu takes the
  foreground.
- **Our own popups are never a dictation target**: `Win32WindowFocusHelper`
  remembers the last foreground window of another application and returns it
  while one of our *popups* (tray menu, overlay) holds the foreground, so
  dictation started from the tray menu still inserts into the window the user
  was working in. Only `WS_EX_TOOLWINDOW` windows of our own process are
  skipped — the Settings dialog is a normal window and stays a valid target. Cancel, Retry and Insert never apply at the same
  time and therefore share one slot: exactly one of them is visible with
  identical fixed sizes, which keeps the controls row width constant while
  showing only the action that is actually available. Retry re-transcribes, so
  it is wrong after a *successful* transcription whose insertion failed — that
  state passes `error_action=OVERLAY_ERROR_ACTION_INSERT` and offers Insert
  (`insert_again_requested` → `controller.repaste_last_transcript`) instead.
  Before this, the Error state after a failed paste offered a Retry that could
  only answer "No failed transcription to retry".
- **The header's two button groups are kept equally wide, and that is what
  centres the status text**: the header is
  `[Record][Pinned] <state label> [Clear][Copy]`, and the label is its only
  stretching item, so Qt gives it exactly the span the four fixed-width
  buttons leave over and `AlignCenter` centres the text in *that span*. That
  span's midpoint is the header's midpoint only while the two groups are
  equally wide, and they were not: 78 + 6 + 74 = 158 px on the left against
  64 + 6 + 64 = 134 px on the right put every status word 12 px right of the
  overlay's centre line in every state — 7 px until the 78 px Record button
  replaced the 68 px History button as the first item, so it was never
  centred and got worse. `_balance_header_flanks` runs once in `__init__`,
  before the header layout is filled, and widens the narrower group's buttons
  until the totals match (Clear and Copy are 76 px each). Measured after:
  0.0 px offset in every state, in both pin modes, with and without the
  queue, and the overlay is still 470 px wide because the header's sizeHint
  stays below the controls row, which is the row `_target_window_width`
  actually takes.
  Three properties are load-bearing:
  - **It measures the width `setFixedWidth` pinned, not `sizeHint()`.** A
    pinned width is often deliberately below the style's natural width, so
    `sizeHint()` would discard the constant. For a button that is *not*
    pinned, `minimumWidth()` is the style minimum instead — near zero — so
    its group measures too narrow and the deficit is spread evenly over its
    members, pinning that button under its own caption. It takes a group of
    two to see that (with one per group the wrong number cancels out), the
    flanks still come out equal, and asserting the precondition afterwards
    cannot catch it either because the balancing itself calls
    `setFixedWidth`. Hence the `sizeHint()` fallback plus a warning rather
    than a silent wrong number, and a test that drives exactly that shape;
    raising instead would trade a clipped caption for an overlay that does
    not open. Any later `setFixedWidth` on Record, Pinned, Clear or Copy
    still reinstates the offset — nothing does today (the runtime syncs
    change only caption, icon, tooltip, enabled state and stylesheet
    properties), and the regression test drives all of them.
  - **Do not replace this with a spacer.** An 18 px spacer between the label
    and Clear yields the identical label span, but the visible clear space
    around the text then reads 28 px left against 52 px right, and the
    constant has to be re-derived by hand on every button-width change.
    Moving Pinned to the controls row is worse still: it puts the text 28 px
    off centre the other way *and* widens the overlay by ~80 px, because the
    controls row is what sets the window width.
  - **Equal flanks centre the text only because the container's horizontal
    margins are symmetric** (1 px frame + 14 px layout margin per side).
    `test_the_status_text_is_centred_on_the_overlay_in_every_state`
    therefore measures the *painted* glyph pixels against the container's
    centre instead of asserting the button arithmetic, and separately pins
    the label's rectangle as identical across every state, error action,
    hover, press, copy-feedback swap, queue change and pin mode — so a
    symmetric reflow that keeps the text centred while moving it still
    fails.
- **Overlay `set_state(copy_text=...)`**: when the detail area shows more than
  the transcript — an insertion error followed by the transcript preview — the
  Copy action must still yield exactly the transcript. A failed insertion shows
  the transcript because it is otherwise invisible until it is inserted again,
  and the Error state scrolls to the top so the reason stays in view.
- **Compact overlay states grow to fit their text**: Idle/Listening/Processing
  used to pin the detail area to `OVERLAY_DETAIL_MIN_HEIGHT`, which silently
  clipped anything longer than two lines — most visibly the startup hotkey
  notice, the one message a user must be able to read. Compact now sizes to its
  content up to `OVERLAY_COMPACT_DETAIL_MAX_HEIGHT` and adds only the overflow
  to the compact window height, so short status text still produces exactly the
  captured compact size `ensure_compact_size()` relies on.
- **Overlay must never re-wrap or blink**: the transcript label wraps at a
  width derived from the target window width (never the live scroll
  viewport, which changes with deferred queue resizes and scrollbar
  visibility) and pre-measures the scrollbar case; `_apply_window_flags`
  calls `setWindowFlags` only when permanent pinning flags actually change
  because it recreates the native window. Temporary foreground reveals first
  use `_apply_native_z_order` (`HWND_TOPMOST` / `HWND_NOTOPMOST`) without
  changing Qt window flags; if that native call fails, use a temporary
  `WindowStaysOnTopHint` fallback rather than leave a floating overlay hidden.
  Successful recording start confirms `ensure_compact_size()` before and after
  its bounded Qt event drain; pending layout work can otherwise leave the
  previous expanded result geometry visible.
  The overlay Language button owns its `QMenu` popup and a centered chevron in
  a reserved right-hand zone. Do not use `QPushButton.setMenu()` here: the
  native Qt/Windows menu indicator can be vertically misaligned under the
  overlay stylesheet. Its regression test renders the button and verifies that
  the chevron pixels remain inside that zone and centered on it.
  The Local/General model runtime note keeps a reserved
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
  field buttons match the corresponding input height via
  `_match_field_button_height`, which also tags them with the
  `inlineFieldButton` stylesheet property: the dialog-level base QPushButton
  rule has a larger QSS box (min-height + padding) that would otherwise beat
  the fixed height once the button is reparented into the styled dialog and
  render it taller than its field or clipped at the bottom. Action rows keep
  explicit spacing rather than relying on platform defaults. Settings tab selection must
  not change tab font weight or measured tab width; use color/border changes for
  the selected state. General and Audio && Recording form sections share one
  measured label column (applied by `_build_audio_tab` after both tabs exist)
  so fields align across group boxes and when switching between the two tabs. Pressing Save with no effective setting or
  API-key changes must not emit `settings_changed`; otherwise the controller can
  reload or preload local models unnecessarily. The Benchmark tab hosts the
  *viewing* side directly (viewing results/history is frequent, running a
  benchmark is rare): a compact header row ("Run Benchmark..." button plus a
  fixed-height live status label) above the History/Results vertical splitter.
  The *run* side (audio sample picker, installed-model list with one compact
  row of small Select all/Deselect all/Refresh buttons, collapsible Run
  Options, Run/Cancel controls) lives only in the resizable, non-modal
  `benchmark_window` ("Run Benchmark", 860x880 default bounded to the
  available screen so expanding Run Options keeps the model list usable,
  owned by the settings dialog so it hides when the dialog closes). Re-clicking the button raises/activates
  the existing window rather than creating a second one via
  `_open_benchmark_window`, which also refreshes the model list. Status is set
  through the single `_set_benchmark_status`, which feeds both the tab label
  and the window's own status line. Benchmark Results tables use per-pixel
  scroll modes. The Local Models group and its inventory list expand into
  available vertical space instead of leaving an unusable blank area below the
  group. All benchmark widget attribute names are unchanged; only
  containers moved. Completed and partial canceled runs are saved to Benchmark
  History automatically; Export only creates a shareable file. New
  faster-whisper results store CTranslate2's resolved device instead of `auto`.
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
- **General/Audio-tab field hints have explicit visual ownership**: a control
  and its descriptive hint use `_field_with_hint` with a 2 px internal gap;
  these forms use a 10 px row gap before the next setting. Changing model/language
  notes reserve two fixed lines so engine switches never move later fields.
  Delayed paint/prewarm callbacks use dialog-owned `QTimer`s and must disappear
  with the dialog instead of invoking deleted Qt objects.
- **Streaming abort keeps the partial transcript**: `_abort_streaming_session`
  saves the best-known live transcript to history, keeps it as the last
  transcript for the overlay Copy action, shows it in the abort message, and
  reveals the overlay. An aborted stream must never lose already-transcribed
  text from UI/history. **A dying stream runtime is the same case**:
  `_on_transcription_failed` reads `_current_streaming_partial_text()` (the
  single shared reader, so the two paths cannot drift on which field wins)
  *before* `_reset_streaming_state()` wipes it, saves it to history with the
  retained audio path, takes it as `_last_transcript`, and offers it as the
  Error state's `copy_text`. Before this, a dropped WebSocket left minutes of
  dictation only in the target window.
- **Shutdown aborts a live stream, never stops it**: `shutdown()` is wired to
  `app.aboutToQuit` and therefore runs on the Qt main thread, while
  `stop_stream()` joins the stream worker with *no* timeout through a final
  transcription (the whole recording under `streaming_full_final_transcript`).
  Its result is discarded there anyway, so quitting mid-dictation only bought a
  frozen UI. Every teardown path now prefers `abort_stream()`.
  Only that *history write* is skipped while `_has_pending_streaming_job()`
  (which excludes an `aborting` job, since a canceled finalize delivers
  nothing): a finalize in flight delivers that session's text itself, and both
  AssemblyAI and Deepgram record a socket error *and* still return accumulated
  text from `stop_stream()`, so saving the partial too wrote two history
  entries for one dictation. **The teardown itself is never conditional** —
  gating `_teardown_active_stream_runtime` on the same check abandoned a live
  capture, its transcriber and its runtime lease, leaving the microphone
  recording after the overlay already showed Error. Known gap: if that pending
  finalize then raises or returns empty, the partial is lost (the pre-existing
  behaviour); preserving it needs the streaming job's terminal handler to write
  a stashed partial when it produced no text. Reading `job.mode` instead of `_active_session_mode` in
  `_on_transcription_ready` was tried and reverted: every writer of
  `_active_session_mode = "batch"` also resets the streaming text state, so
  `committed_text` is already empty by then and the delivery is identical
  either way — it only relabelled history and suppressed the completion beep.
- **Custom vocabulary** (`custom_vocabulary`, General tab): user terms parsed
  by `config.parse_custom_vocabulary` (newline/comma/semicolon split,
  case-insensitive dedupe, 100-term cap). Biasing per provider: faster-whisper
  `initial_prompt` (batch + rolling-window streaming), OpenAI/Groq `prompt`,
  AssemblyAI Universal-3.5 Pro batch/streaming `keyterms_prompt`,
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
- **The spreadsheet export neutralizes what XML cannot carry.** XML 1.0
  permits #x9, #xA, #xD, #x20-#xD7FF, #xE000-#xFFFD and #x10000-#x10FFFF and
  nothing else -- not even escaped -- while `saxutils.escape` only rewrites
  `&`, `<` and `>`. So one control byte anywhere in a benchmark row produced a
  worksheet that will not parse, inside a `.xlsx` written without error that
  Excel then refuses to open. Verified with `ElementTree`: NUL, BEL, vertical
  tab and a lone surrogate each fail; tab, newline and an emoji are fine. The
  route is the text nobody types -- `runtime_details` built from a runtime's
  own error output, the environment strings read off the system, and a
  transcript returned by a remote provider. `_xml_safe_text` replaces exactly
  those characters with U+FFFD and nothing else: stripping non-ASCII instead
  would trade an unopenable file for a silently mangled German transcript, and
  the test compares the round-tripped cell text rather than only asserting
  that the file parses. `_cell_xml` is the single place user text enters XML;
  every other part of the workbook is static.
- **A store's existence check covers the backup too, not just the primary.**
  `atomic_write_json(keep_backup=True)` writes a `.bak` beside every store and
  `load_json_with_backup` reads it when the primary will not parse -- but five
  stores opened their load with a bare `if not path.exists(): return <empty>`,
  which the backup never got past. So a *deleted* primary read as "nothing
  saved yet", and the next write put that emptiness over the backup as well.
  Measured on the transcript history: five entries, delete
  `transcript_history.json`, load returns 0, one more dictation leaves the
  backup holding 1. `settings_store` was worse -- a missing primary writes
  defaults *and* `save` refreshes the `.bak` in the same call, so every setting
  was reset and the last copy destroyed together. The primary goes missing for
  ordinary reasons (an antivirus quarantine, a sync client, a user tidying
  `%APPDATA%`), which is precisely what the backup is for. The guard had to
  *widen*, not disappear: with neither file present there is nothing to
  recover and the store must still return its empty default without
  quarantining anything. Every store that recovers from the backup also
  republishes the primary, so a second loss cannot take the data.
  `tests/test_store_backup_recovery.py` holds all three properties for all
  five stores.
- **Persistent JSON read-modify-write operations are path-serialized**:
  `persistence.lock_for_path` is the single in-process lock registry. Stores for
  history, benchmarks, settings, provider diagnostics, local inventory, last
  recording, and insecure keys reuse it so separate store instances cannot
  overwrite each other's concurrent updates. Keep writes atomic as well.
- **A stored file that is not UTF-8 must reach the recovery path**:
  `Path.read_text(encoding="utf-8")` raises `UnicodeDecodeError`, not
  `json.JSONDecodeError`. Both are `ValueError`s and only the JSON one was
  named, so a single non-UTF-8 byte in settings, history or the inventory
  cache escaped the store constructor and the app died before its first
  window -- with the recovery path that exists for exactly this sitting
  unused, because the damage was of the wrong kind. `persistence` and
  `local_model_scan` catch `(OSError, ValueError)` and say why both members
  are meant; `transcript_history.import_from_file` has its own arm with a
  user-facing message, since there the file is one the user chose.
- **An unreadable insecure key file is not an empty one**: the fallback store
  read its JSON through one `try/except` returning `{}` for every failure, so
  a permission error, a lock held by a backup tool or a truncated write looked
  exactly like "no file yet" -- and the next `set_api_key` wrote that `{}`
  plus one key back, silently deleting every other provider's key while the UI
  reported success. `_load_insecure_payload` returns `(payload, damaged)`;
  `_set_insecure_api_key` and `delete_api_key(provider, strict=True)` raise a
  `RuntimeError` naming the path instead of overwriting. The stale-copy
  cleanup after a successful keyring write stays tolerant on purpose --
  failing there would undo a save that already succeeded.
- **Update checks**: update discovery uses GitHub Releases directly through
  `update_checker.py`; no custom domain or update server is required. The app
  schedules one asynchronous check after startup and shows a tray notification
  only when a newer release exists. Manual checks are available from Settings
  and the tray menu. Keep update checks non-blocking and avoid downloading or
  executing installers automatically without a separate review. Release JSON is
  size-bounded, tags use strict numeric SemVer, and release links are restricted
  to this repository's HTTPS GitHub release paths. A manual request during the
  startup check promotes the active request so its result remains visible.
  Update dialogs use an explicit high-contrast stylesheet instead of platform
  button hover colors. An in-app download requires the exact installer and
  `.sha256` release assets, trusted HTTPS GitHub redirects, the declared byte
  size, and a matching checksum; incomplete data stays in `.partial`. Launching
  the installer additionally requires Windows to report a valid Authenticode
  signature whose full subject is pinned in
  `TRUSTED_WINDOWS_PUBLISHER_SUBJECTS`. Keep that set empty until the real
  signing identity exists; a GitHub Verified commit does not sign release EXEs.
- **Local model download queue**: Settings downloads run serially through one
  worker process so Hugging Face cache writes and network usage remain
  predictable and the active download can be terminated safely. Additional
  models can be queued while a download is active, and each waiting row shows
  its place (`Queued, 2 of 3`) because only one download runs at a time and
  several rows reading just "Queued" hid the order. Parallel downloads are
  deliberately not offered: the total bandwidth is the same, while every extra
  writer needs its own worker process, its own progress row, and its own share
  of the cancel/partial-cleanup bookkeeping. Cancel clears the queue and
  removes unusable `*.incomplete` files while preserving completed files for a
  later resume. Progress and its rolling transfer rate are approximate because
  they are derived from cache growth and the estimated total sizes in
  `MODEL_ESTIMATED_SIZE_MB`.
- **Error text must be selectable**: Qt hands a `QMessageBox` only
  `LinksAccessibleByMouse`, so its text could be captured only by retyping it
  or screenshotting it. `dialog_style.install_selectable_message_text` installs
  one application-wide event filter that marks every message box selectable as
  it is shown. That is the only place that reaches the `QMessageBox.critical`
  and friends convenience statics, which build *and* show the box in a single
  call and give the caller no chance to configure it — do not "simplify" this
  into per-call-site changes unless every static is migrated first. Inline
  status/error labels use `dialog_style.make_label_selectable`, including the
  update dialog's status and details labels; the overlay detail label already
  carried the flags. Always OR the flags onto the existing ones: replacing them
  strips `LinksAccessibleByMouse` and makes the links in the update dialogs
  dead. The whole body of `make_message_text_selectable` is guarded, because it
  runs from an event filter and an exception there escapes the caller's
  `show()`.
- **There is exactly one download slot, and it is enforced machine-wide**
  (`model_download_coordinator`). It exists because the controller's preload
  path and the Local tab's queue each used to spawn a worker against the same
  cache directory, which the user hit as three failures in one sitting:
  selecting an uncached model and pressing Save downloaded it while the Local
  tab showed nothing; starting that same model from the Local tab then sat at
  0% forever, because progress is directory growth and the other process owned
  the directory; and switching model terminated the preload download *and*
  deleted its partial files, so a multi-gigabyte model restarted from a few
  hundred megabytes.
  - **Every** download goes through it, including the ones a transcriber starts
    from its own load path (`_ensure_snapshot` / `_resolve_model_path`) via
    `run_coordinated_download`. Not an edge case: with `keep_onnx_model_loaded`
    off the Cohere/Granite family never preloads, so the transcriber's own
    download is the only one it has. **faster-whisper is the easiest to miss** —
    `WhisperModel(...)` downloads inside its own constructor through
    `huggingface_hub`, which no grep of this repo reveals, so `_ensure_model`
    fetches through the slot first. That pre-fetch gates on
    `download_destination_dir` + `_has_valid_model_snapshot`, i.e. the directory
    the constructor actually resolves; `find_cached_models` is too broad (it
    also accepts the default cache and a flat layout) and let a custom Model Dir
    bypass the slot.
  - `acquire()` blocks until the slot is free and returns `ACQUIRE_JOINED` when
    the very same model finished while the caller waited, so the second caller
    never re-downloads. It is idempotent: several waiters on one finished model
    all join.
  - **Explicit (Local-tab) requests register interest at *enqueue***, not when
    the entry reaches the slot, and the preload path checks
    `has_explicit_interest` before deleting partials — otherwise a model still
    queued behind another download loses the bytes it is about to resume from.
    Every path that abandons the queue must give that claim back
    (`_discard_queued_downloads_locked`), and every exit from
    `_download_local_model_in_subprocess` must clear
    `_local_model_download_claimed`; leaving either behind blocks partial
    cleanup for the process lifetime and strands the entry so the model can
    never be queued again that session.
  - **`claimed` and `active` are different things.** A popped entry waiting for
    the slot is `claimed`: the model list, the pending set and the duplicate
    check must see it, but the progress bar must not — it measures directory
    growth, so pointing it at a model nothing is writing to invents a
    percentage. The bar reads `_local_model_download_active` only.
  - The Local tab renders a controller-started download in both the list and
    the progress bar, so `_poll_preload_download_state` drives
    `_refresh_local_model_download_progress` too; without that the progress
    branch is unreachable. The bar tracks its own shown-state rather than asking
    the widget, because Qt reports a child of a hidden dialog as invisible and
    this dialog persists hidden for the app lifetime.
  - **Nothing may wait for the slot on the Qt thread.** Nemotron's `start_stream`
    used to load the model there; once loads could queue behind an unrelated
    download that froze the whole UI with no progress and no way out, so it
    loads in its stream worker instead. `main` connects
    `request_download_shutdown` to `aboutToQuit` *before* the dialog and
    controller teardown, because the dialog shutdown releases the slot and a
    waiter would otherwise start a fresh multi-gigabyte download on a
    non-daemon executor thread that the interpreter joins at exit.
  - The download queue worker is wrapped in `try/except BaseException`: an
    exception used to
    kill the thread holding the queue, leaving interest registered, the running
    flag set and the tab's controls disabled with no way back.
  - **The slot has two layers, and both are load-bearing.** Inside the process
    a `threading.Condition` serializes callers and provides the join and
    explicit-interest behaviour the Local tab depends on. Across processes an
    OS-level lock (`file_lock.CrossProcessLock`, `msvcrt.locking` on Windows /
    `fcntl.flock` elsewhere) covers the out-of-process benchmark worker,
    `scripts/download_model.py`, and a second Windows user sharing one Model
    Dir (a second copy of the app is separately refused by the
    single-instance guard in `main.py`) — none of which
    the in-process half can even see. It is a real kernel lock rather than a
    PID file on purpose: the OS drops it when the owner exits for any reason,
    so there is no stale-lock detection, no heartbeat, and no timeout that
    guesses whether the other side is alive — the three things a PID file gets
    wrong, each of which leaves downloading permanently broken.
  - The machine-wide lock is keyed on the **cache directory**, not the model:
    two writers corrupt each other through the shared blob and ref trees even
    when fetching different models, and directory-growth progress becomes
    meaningless for both. An empty Model Dir maps onto one shared identity
    because every such caller uses the default Hugging Face cache.
  - It is taken **after** the in-process slot and **outside** the condition —
    waiting for another process can take minutes, and holding the condition
    would freeze every observer (`active()`, the progress poll,
    `has_explicit_interest`) with it. Every exit from that wait must hand the
    in-process slot back, or a cancel strands it for the process lifetime and
    no download can ever start again. A filesystem that cannot lock (some
    network shares) logs a warning and degrades to process-local serialization
    rather than making downloads impossible.
  - **Holding the OS lock and storing it are two steps, and the gap between
    them must not be able to raise.** `acquire()` gives the in-process slot
    back on any exception, but it cannot see the `CrossProcessLock` object, and
    `_release_cache_lock` finds it through `self._cache_lock` -- so a raise
    after `lock.acquire()` returned True and before the assignment strands a
    real kernel lock with no reference to it. Because the lock is keyed on the
    cache directory and is machine-wide, that blocks the benchmark worker,
    `scripts/download_model.py` and any second user of that Model Dir until
    this process exits, not just this process. The publication therefore
    releases the lock and re-raises. `CrossProcessLock.release()` is idempotent
    and swallows `OSError`, so the defensive call costs nothing.
  - The app's own download subprocess needs no lock of its own: the parent
    holds the slot for the whole life of `_run_download_worker`.
    `scripts/download_model.py` does take it, so running the script while the
    app is downloading now waits instead of racing — the standalone script was
    the one path the process-wide lock could never cover.
- **Download progress measures the download *destination*, never a candidate
  copy**: because progress is cache growth, `estimate_cached_model_bytes` must
  watch exactly the directory the downloader writes into.
  `local_faster_whisper.download_destination_dir` is that single source of
  truth: local ONNX models resolve through
  `local_webgpu_asr.webgpu_download_destination` (the flat `local_dir` that
  `download_webgpu_model_snapshot` passes to `snapshot_download`),
  faster-whisper models to `models--<repo>` under the configured `cache_dir`.
  Do not reintroduce a `max()` over the *candidate* layouts and cache roots in
  `_model_cache_dirs` — that exists for detection/delete/cleanup and
  legitimately includes both the flat and `models--<repo>` layouts plus the
  default cache. Sizing those made a foreign directory masquerade as the
  download: a conversion script pulled a repo's fp32 weights with `cache_dir=`
  (9.4 GB in `models--smcleod--…-nar-onnx`, since retired), so that download
  reported a fixed `10078/2490 MB, approx. 100%, measuring speed` while the real
  flat destination was still filling. A parametrized test pins
  `webgpu_download_destination` to the `local_dir` actually downloaded into so
  the two cannot drift. Snapshot entries that are symlinks are skipped when
  summing, because `stat()` follows them into an already-counted blob and would
  report 100% at half a download.
  **The one permitted fallback**: while the destination directory does not
  exist, `_complete_cached_model_root` may size a cache root that holds a
  *complete, loadable* snapshot — validated by
  `resolve_cached_webgpu_model_root` for local ONNX models and by
  `_has_valid_model_snapshot` for faster-whisper. Without it a model cached in
  the legacy `models--<repo>` layout — which the loader still resolves and
  uses, as Cohere does here — showed a 0% "Downloading" bar during every
  preload. Requiring a *valid* snapshot is what separates this from the bug
  above: the fp32 conversion copy carries none of the required `int8/*` files
  and can never qualify, and an in-flight download has no valid snapshot
  anywhere, so it correctly starts at 0%. A complete copy in another root is
  reported at 100% on purpose — the app would load that copy rather than
  download anything.
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
  The snapshot cannot observe a half-written file either: `save_recording` and
  `snapshot_managed_recording` take the same `lock_for_path` lock, and the
  write is atomic, so a snapshot taken while dictation overwrites the managed
  recording either predates the new one entirely or sees all of it. Both
  properties are pinned by tests -- the interleaving one in
  `tests/test_store_concurrency.py`.
- **History export/import/clear parity**: the standalone History dialog and the
  Settings History tab share the same export, import (including the overflow
  choice between "import only free slots" and "import all and set unlimited"),
  and clear flows via `history_ui_actions.py`, so the logic exists exactly once.
  Only feedback presentation (popup vs. inline status label) and how the active
  limit is read/persisted differ per caller. The Settings tab persists a
  switch-to-unlimited decision immediately (via `_settings_store` plus
  `dataclasses.replace` on `_loaded_settings`), the same way the dialog does,
  so a later Save does not see it as a phantom change.
- **History audio linkage is shared, and the two retranscribe paths differ on
  purpose**: `history_audio.py` owns resolving an entry's retained audio
  (stored `source_audio_path` first, then the managed last recording only
  while it still describes that exact entry) plus the file-manager
  reveal/open calls; both history views use it, and
  `app_paths.resolve_recordings_dir` is the single "configured dir else
  default" rule. The overlay's "Recent Transcriptions" dialog offers
  Retranscribe/Show audio file per entry (buttons plus a right-click menu)
  and a Recordings-folder shortcut. `retranscribe_dialog.py` preselects the
  entry's own engine, model, and language — repeating a run with a corrected
  language is the case it exists for — but all three stay changeable so a
  quick "try the bigger model on this one" needs no detour through Settings.
  The dialog is resizable (long transcripts) and its pickers are dependent:
  changing the engine repopulates the models (restoring the entry's model when
  the user returns to its engine) and the language list follows both via
  `config.language_modes_for_selection`. It duplicates none of the Import Audio
  tab's machinery: `settings_dialog_helpers.model_choices_for_engine` and
  `local_model_label` are the shared source for every model picker, and
  `settings_store.apply_engine_model_selection` is the one place that maps an
  engine/model pair onto the engine's own model field. Settings > History >
  Retranscribe... still prefills the Import Audio tab, which additionally
  offers credential checks and progress for one-off external files. Both paths
  write a new history entry and never modify the original.
- **Ctrl+C in either history view copies the whole selection**: both the
  standalone History dialog and the Settings History tab install an explicit
  `QKeySequence.Copy` shortcut on their list/table bound to the same handler as
  "Copy selected". Without it the view's own handling yields only the current
  cell, so a three-row selection silently produced one entry in the clipboard.
- **Benchmark surface stylesheets must stay scoped to widget types**: an
  unscoped property block is inherited by every child, so a bare
  `border: 1px; border-radius: 4px` on a `QTableWidget` gave each header
  section and the corner button its own rounded box.
  `_BENCHMARK_RESULT_SURFACE_STYLESHEET` and `_BENCHMARK_DETAILS_STYLESHEET`
  therefore scope every rule (`QTableWidget`, `QHeaderView::section`,
  `QTableCornerButton::section`, `QTabWidget::pane`, `QTabBar::tab`). Inside
  the details tabs the pane draws the frame, so the views in it are
  `NoFrame` and their content is wrapped with a margin instead of sitting
  flush against the border. The Benchmark History and Results tables share the
  one surface stylesheet so the tab reads as a single design.
- **A produced-but-not-pasted queued transcript is reported, never silent**:
  a background/deferred insert that fails logs *and* emits
  `background_insertion_failed` (tray notification in `main.py`), because a
  silent failure there is indistinguishable from a successful insert — which
  is exactly how a transcript goes missing unnoticed. When nothing newer owns
  the overlay it additionally shows the Error state with the transcript and
  `OVERLAY_ERROR_ACTION_INSERT`, and takes over `_last_transcript` so Copy and
  Insert act on exactly what is displayed. `_foreground_delivery_pending`
  guards the gap inside `_on_transcription_ready` between clearing the session
  state and writing the foreground result's own overlay state: the flush runs
  there, so without the guard the Error would flash and be overwritten one
  statement later. The notification always fires regardless.
- **AssemblyAI pre-recorded model selection**: use the current `speech_models`
  parameter for batch/import requests. `universal-3-5-pro` is sent alone when
  selected; never silently add `universal-2` as a fallback. Legacy
  `universal-3-pro`/`best`/`nano` settings migrate to the current default and
  are not shown in the UI.
- **ElevenLabs batch model selection**: `scribe_v2` is the only supported model.
  ElevenLabs removed `scribe_v1` on 2026-07-09; legacy stored selections migrate
  to `scribe_v2` and the removed identifier must not be sent to the API.
- **AssemblyAI Universal-3.5 Pro realtime**: the legacy v2 realtime and earlier
  Universal-Streaming model are retired paths and must not be reintroduced.
  Streaming uses `assemblyai.streaming.v3.StreamingClient` with the
  explicit `universal-3-5-pro` model and optional `keyterms_prompt`; its native
  18-language code switching needs no legacy `language_detection` or
  `format_turns` parameter. The batch selector does not alter realtime routing.
  Turn text is keyed by `turn_order` because later events can refine the same
  turn. Bound SDK `disconnect` joins with a helper thread; they can hang on dead
  connections.
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
- **A hotkey's key must never be a modifier**: `RegisterHotKey` matches the
  modifier state *exactly*, and pressing a modifier raises its own modifier
  bit, so `Ctrl+Win+LShift` registers `Ctrl+Win` + key `LSHIFT` while the real
  keystroke reports `Ctrl+Win+Shift` — a different hotkey. Registration
  *succeeds*, so this failed silently: the app reported a working hotkey that
  could never fire, which is exactly what the old `FALLBACK_HOTKEY` did. Proven
  by registering both variants at once: Windows accepts them as two separate
  hotkeys. `parse_hotkey` now rejects a modifier as the key, which also stops a
  user configuring one in Settings, and every entry in `FALLBACK_HOTKEYS` ends
  in a real key. `Ctrl+Win+Space` is deliberately not among them: Windows owns
  it for input-language switching.
- **A busy hotkey never overwrites the user's choice**: another program holding
  the preferred combination (a terminal, an IDE) is temporary, but persisting
  the fallback into settings made it permanent — once that program closed, the
  app had already forgotten what the user wanted. `_register_hotkey_with_fallback`
  keeps `settings.hotkey` and records the substitution in `_active_hotkey`
  only; `_hotkey_reclaim_timer` retries the preferred one every
  `HOTKEY_RECLAIM_INTERVAL_MS` and stops once it succeeds. The idle line shows
  `_active_hotkey`, because printing the stored preference would name a key
  that does nothing. The reclaim never swaps the binding while a dictation is
  running.
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
  overlay visibility and refresh all global hotkey registrations after
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
  The release workflow publishes `stt_app-win-x64-setup.exe.sha256`, generated
  only after the final installer bytes. Authenticode signing must run before
  that checksum step once a managed signing identity is configured.
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
  **Granite 4.1 Plus and NAR were retired on 2026-08-26**, and the raw
  `onnxruntime-node` graph runtime that served them was removed with them, so
  there is exactly one ONNX inference path here and `onnxruntime-node` is no
  longer a top-level npm dependency (Transformers.js keeps its own nested pin).
  They were removed on measurement, not preference: in the 2026-08-25 run the
  base 4.1 2B ran at mean RTF 0.099 on WebGPU while NAR managed 0.447 and Plus
  4.149 -- both CPU-only, NAR with merged and dropped German words (63.2%
  word-sequence agreement with `large-v3`, 43 words against 52), Plus looping
  one clause to the token limit (2.8% agreement, 378 words), which is also what
  makes its RTF so bad: it is autoregressive and kept generating. **Every RTF
  in this file is the mean of that case's runs**, which is the convention the
  benchmark report uses; quoting a single run instead is how the same
  measurement came to be published as three different values -- 0.098, 0.100
  and 0.099 -- across four places in this repository. The graph-level cause is
  recorded in `docs/granite-speech-4.1-onnx-variants.md`: their encoders carry
  16 `Einsum` nodes each (`b m h c d, c r d -> b m h c r`, a 5-D contraction the
  WebGPU EP has no shader for), plus the 5-D attention `MatMul`s DirectML
  cannot execute, while the
  `onnx-community` export of the base model writes the same attention as
  Reshape/Transpose/MatMul and has none. Before re-adding any raw-graph model,
  read that document and verify every required file against the actual repo
  listing rather than copying a sibling's list -- a required file the repo does
  not ship is unrecoverable at runtime, and a test asserts each required file is
  covered by the download allow-patterns. Do not relabel a Plus build as base
  `granite_speech` to force it onto the pipeline path either: that produces
  broken English (verified with the valoomba build).
  `keep_onnx_model_loaded` now defaults to **on**: the flag only takes effect
  once such a model is selected, and without it every single dictation pays the
  full Node + ONNX load while faster-whisper and Nemotron stay warm. With it on
  the runtime is preloaded and kept like the other local engines; turning it
  off restores the old behavior (no preload, closed after each batch) for
  machines where RAM/VRAM pressure matters more. Existing settings files keep
  whatever they stored.
  The resolved runtime device is reported through transcriber progress messages
  so the overlay/import UI can show whether WebGPU, DirectML, or CPU was used.
  **`DEFAULT_MODEL_SIZE` is `parakeet-tdt-0.6b-v3`, not a Whisper model**
  (changed 2026-08-27). The earlier note here said to keep faster-whisper
  "until real target-hardware benchmarks justify switching"; those benchmarks
  now exist and say the opposite. On one 24.3 s German recording on a Ryzen 5
  7600X, both at `device=cpu`, Parakeet measured mean RTF 0.043 against
  `small`'s 0.154 -- 3.6x faster -- for 670 MB against 486 MB, with the 25 European
  languages its model card lists and its own
  language detection. It keeps everything that made `small` the default: pure
  Python, CPU only, no GPU, no Node.js. The one capability it drops is
  streaming (`onnx-asr` is batch-only), and `DEFAULT_MODE` is `batch`, so the
  out-of-the-box combination is consistent; a user who switches to streaming
  mode is told to pick a streaming model. Changing this constant does not
  touch an existing install -- `SettingsStore.load` falls back to the default
  only when the key is absent -- so it is strictly a first-run decision.
  `DEFAULT_FASTER_WHISPER_MODEL_SIZE` (`small`) stays the default *within*
  that runtime, which is what `LocalFasterWhisperTranscriber` and the
  benchmark CLI use.
  Keep `granite-4.0-1b-speech` selectable as a smaller q4 option until real
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
  Two Ryzen 5 7600X CPU runs measured it: RTF 0.21 with a 1.78 s cold load on
  a 24.3 s recording (2026-08-25) and RTF 0.24 with a 1.90 s load on a 28.1 s
  one (2026-07-10). The "0.229 RTF, 0.81 s cold load on the repository sample"
  this entry carried until 2026-08-28 matches neither run, and that sample is
  2.1 s of synthetic sine tones that no benchmark run used. Nemotron stays preloaded and cached like faster-whisper so
  pressing the recording hotkey does not block on model loading. Its internal
  runtime VAD follows the app's VAD setting. The language UI exposes only the
  transcription-ready and broad-coverage official prompt IDs.
- **`onnxruntime-node` is no longer a direct dependency**: it was only ever
  needed by the raw Granite 4.1 Plus/NAR graph sessions, which were retired on
  2026-08-26. The pipeline models run on the copy Transformers.js pins itself
  (exactly 1.24.3 across 4.0-4.2), so `npm ls onnxruntime-node` must show one
  nested entry and nothing at the top level. **Do not add it back.** Declaring
  a newer version alongside makes npm install two different native ORT runtimes
  into one Node process (observed API-version mismatch warnings), and nothing
  in the app would use the newer copy. A 2026-07-21 benchmark found Transformers.js
  4.1->4.2 and CTranslate2 4.7.1->4.8.1 performance-neutral on AMD hardware
  (CT2 4.8.0's int8 PACKED_GEMM speedup is Intel-MKL-only). Re-checked on
  2026-08-11 against Transformers.js 4.2.0: the nested pin is still exactly
  1.24.3. `onnxruntime-node`'s
  `postinstall` being blocked by npm 12's install-script policy is harmless —
  the package ships its native binaries bundled and reports cpu/dml/webgpu.
- **`sharp` is pinned forward through `overrides`**: `@huggingface/transformers`
  declares `sharp: ^0.34.5`, which npm cannot resolve past on its own, so the
  tree inherited GHSA-f88m-g3jw-g9cj (libvips CVE-2026-33327/33328/35590/35591,
  high). `package.json`'s `overrides` therefore forces `sharp: ^0.35.0`. Keep
  that entry until Transformers.js widens its own range; sharp 0.35 requires
  Node >= 20.9, which this project already exceeds.
- **onnx-asr engine (Parakeet TDT 0.6B v3, Canary 1B v2)**: a third local ONNX
  path in `transcriber/local_onnx_asr.py`, separate from the Cohere/Granite Node
  runtime and from Nemotron's ORT GenAI path. It is **pure Python and needs no
  Node.js**, and it adds no ONNX Runtime: `onnx-asr[cpu,hub]` resolves the exact
  `onnxruntime` distribution `onnxruntime-genai` already requires. Only
  inference lives in that module — download, cache detection, size estimation
  and deletion reuse the shared `_OnnxModelLayout` entries in
  `local_webgpu_asr`, whose allow-patterns fetch only the int8 tier (both repos
  also ship fp32 graphs worth 2.4 GB and 3.3 GB).
  Measured on a Ryzen 5 7600X, CPU only: Parakeet 670 MB at **mean RTF 0.043**
  on a 24.3 s German recording (no English run was retained; earlier text here
  said 0.046 EN / 0.043 DE and neither figure is in the benchmark history),
  and Canary at 1029 MB. **Canary has no RTF in the benchmark history**: its
  only case there errored ("Canary cannot detect the language"), so the
  "0.134 / 0.135" this entry used to give came from the same retracted note as
  the Parakeet figures above and must not be requoted without a real run --
  nor may any ratio derived from it, which is how "~3x slower than Parakeet"
  survived one round longer than the number it came from.
  **Parakeet is not the fastest case in that run, and the qualifier is load-
  bearing**: `tiny` measured 0.033, 1.29x quicker. What separates them is the
  report's agreement column, and only in one direction. `tiny` is the weakest
  of the models that transcribed the recording -- 82.7%, and 10th or 11th of
  12 whichever transcript is taken as the reference. Parakeet is in the
  leading cluster, and **that cluster cannot be ordered by this measure**:
  Parakeet's own rank moves between 1st and 8th depending on the reference,
  because the differences are one or two tokens out of 52 and `large-v3` is
  the one that is wrong on the deciding token. So the supportable claim is
  "the fastest of the models that transcribed the recording" -- never
  "fastest" flat, and never "the most accurate". Saying it matched
  `large-v3` best *was* the second version of this defect: an unsourced
  superlative was replaced by a sourced number that does not mean what the
  sentence around it claimed.
  Against the GPU models it needs no qualifier: the quickest GPU case in that
  run is `cohere-transcribe-03-2026` at 0.083 on WebGPU, so Parakeet on plain
  CPU is **1.9x** faster than the best local GPU result and no GPU path is
  needed for the best local latency. (Granite Speech 4.1 2B at 0.099 is the
  *slowest* of the three GPU cases; comparing against it gave the 2.3x this
  entry used to state. An earlier "six times" compared against a stale Granite
  figure.)
  **Never add `onnxruntime-directml`.** It installs happily beside
  `onnxruntime` — `pip check` reports nothing wrong — but both distributions
  own the same `onnxruntime/` package directory (620 of 625 files), so the
  DirectML wheel silently overwrites the CPU build and downgrades the reported
  version. `onnxruntime-genai` then dies with "The requested API version [26]
  is not available" and a DLL init failure, i.e. it would trade a Parakeet
  speedup measured at roughly 1.9x -- from a manual DirectML run recorded in
  `docs/learning-log.md`, not from any benchmark case, so treat it as
  indicative only -- for the whole Nemotron engine. `onnxruntime-webgpu==1.27.0` is the one
  GPU distribution that coexists, but it measured *slower* than CPU here. If a
  GPU path is ever wanted it must be an isolated subprocess environment, like
  the Node runner already is.
  **Canary must never expose `auto`.** onnx-asr hardcodes the `<|en|>` source
  and target token, so with no explicit language it *translates* German into
  English instead of transcribing it (observed: "The automatic speaker
  recognition wandels spoken language..."). It is therefore in
  `LOCAL_EXPLICIT_LANGUAGE_MODELS` alongside Cohere, and
  `_normalize_language_mode` maps any unsupported code onto a trained one
  because an untrained ISO code raises `KeyError` deep inside the runtime.
  Parakeet is the mirror image: it accepts `language=` and *ignores* it (a
  bogus code yields byte-identical output), so it exposes only `auto` and sends
  no language at all rather than faking control. Both are batch-only, and both
  are excluded from the ONNX Device picker because they are CPU-only and would
  otherwise let the UI claim a setting that does nothing. PyInstaller needs
  `collect_all('onnx_asr')`, not just a hidden import: the mel/resampler graphs
  are package *data* loaded via `importlib.resources`, and without them every
  model fails while constructing its preprocessor.
- **Local ONNX execution device (`local_onnx_device`, default `auto`, schema
  23)**: the Benchmark tab could always pin a device, but daily dictation
  always ran on `auto` because `factory.py` never passed one. The General tab's
  "ONNX Device" row now feeds the same policy (`LOCAL_WEBGPU_DEVICE_POLICIES`)
  into `LocalOnnxWebGpuTranscriber`, with the same wording as the benchmark
  choices so a device proven faster there can be selected for real use.
  `auto` keeps every existing behaviour. (A per-model CPU preference,
  `LOCAL_ONNX_AUTO_CPU_MODELS`, existed for the two retired raw-graph Granite
  variants and was removed with them; re-add it if a model ever again loads on
  a GPU and only fails at inference, which a load-time probe cannot detect.)
  Unlike `language_mode`, the device **is** part of the
  transcriber cache key *and* the preload key: it is baked into the loaded
  runtime, so changing it must reload rather than reuse. An unknown stored
  value falls back to `auto` via `normalize_local_onnx_device` instead of
  failing the load. Nemotron is in the picker and therefore *must* receive the
  policy: `config.nemotron_provider_order` is the shared mapping used by both
  the factory and the benchmark, and because ORT GenAI has no WebGPU provider
  every GPU-flavoured policy resolves to DirectML for it, which its note says. The row is always present and only toggles enabled state
  and note text — hiding it for faster-whisper or a remote engine would shift
  every field below it, which a test pins by asserting the Language row's
  y-position is identical across all four cases.
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
  The local faster-whisper paths merge windows with
  `merge_rolling_window_transcript`, not `append_only_stream_partial_candidate`,
  because a trailing-audio window is not a full-text revision. It closes two
  losses: an empty window (trailing silence, or one that simply decodes to
  nothing) wiped everything and produced an *empty final transcript* for a
  whole dictation at `_stream_worker`'s fast finalization; and because
  `_suffix_prefix_overlap_len` anchors every candidate alignment at the
  window's *first* word — the word the 8 s boundary cut in half — one
  mistranscribed fragment defeated the search, so the merge now re-anchors up
  to `_WINDOW_BOUNDARY_SKIP_WORDS` words in.
  **A window that still cannot be aligned replaces the accumulated text, and
  must not append**, unless the pause before it was measured *and* the
  decoded window is shown to hold real speech (see the
  measured-silence entry below). Appending unconditionally was tried and
  reverted: a silent microphone
  makes the model emit a fresh hallucination on every 0.35 s partial, none of
  which can ever align, so the accumulated text grew without bound (measured:
  896 words for 8 words of speech after two minutes of silence) and
  finalization *pasted* it — turning a lost transcript into hundreds of junk
  words typed into the user's document. That advice — gate the window on
  audio energy rather than making the merge more permissive — is now
  implemented: the caller measures the decoded window with
  `vad.measure_longest_speech_run_s` against `silence_gate_threshold` and only
  then passes `new_segment=True`. The merge itself is still not allowed to
  get more permissive on its own.
  **`StreamingTextState` deliberately does not join a candidate that has lost
  the `committed_text` prefix onto it.** That would unfreeze insertion — once
  the prefix is gone `compute_stream_locked_prefix` can never advance again, so
  live insertion stays frozen for the rest of the session while the overlay
  still reports `Done` — but a provider revising a word inside the
  already-pasted region then re-emits the whole dictation (measured: 86 pasted
  words for a 48-word truth on an AssemblyAI turn revision, scaling with
  session length). Pasting the transcript twice is worse than stopping early.
- **Streaming finalization**: the full re-transcription of the recording when
  local faster-whisper streaming stops is opt-in via
  `streaming_full_final_transcript` (default off). When off, finalization
  transcribes only the trailing partial window and merges it into the
  provider-tracked live transcript, so stop returns quickly and the history
  entry matches the streamed text. Inserted text stays append-only either way.
- **Streaming decodes nothing during silence, at either end**: faster-whisper
  invents words from silence (the same reason the batch silence gate exists),
  and in the streaming path an invented window can never be aligned against
  the accumulated text, so the merge fell back to *replacing* it. The rolling
  partial measures the audio that arrived since the last partial and skips it
  below `silence_gate_threshold`; the fast finalizer measures exactly its own
  trailing window (`_stream_tail_window_is_silent`). Without the finalizer
  half, a dictation that simply ended with a quiet stretch lost its entire
  transcript at the last step. Unmeasurable audio returns `None` from the
  meter and is never treated as silence — refusing to decode something that
  could not be measured would drop real speech. Note the asymmetry: the
  partial gate looks at the *increment*, not the 8 s window it decodes, which
  is why the post-pause case below needs its own, stronger measurement.
- **A quiet microphone can still gate a whole streaming dictation to nothing**,
  exactly as it can for a batch recording; the threshold is the same and is
  backed by the field data in the batch entry. The result surfaces as the
  ordinary "No speech detected" path.
- **A focus change suspends live insertion; it no longer aborts the stream**:
  live inserts write at the caret, so once another window is in front the
  words land in the wrong document. Ending the whole dictation for that was
  far more disruptive than the problem — people switch windows mid-thought
  and everything said afterwards was gone. `_on_stream_focus_poll` now sets
  `_stream_insertion_suspended`; the session keeps recording and the whole
  tail past `committed_text` is inserted when the target comes back to the
  front, or at stop. Two caveats: with `insert_target=current_window`
  the stop-time insert goes to whatever is focused then, so the
  transcript can still end up split across two windows; and if the
  provider's final text no longer extends `committed_text`, the tail
  exists only in history.
  Three things this depends on, each of which was wrong once:
  - **The poll timer must be armed unconditionally.** Starting it only when
    `STREAMING_ABORT_ON_FOCUS_CHANGE` was set made the suspension dead code
    *and* removed the old protection, so partials pasted into whatever
    window happened to be in front — strictly worse than the abort.
  - **`live_text` must keep updating while suspended.** It is what
    `_current_streaming_partial_text` prefers, so leaving it stale made an
    abort or a dropped socket save the pre-switch text and silently drop
    everything dictated after the switch. Only `committed_text` stays put —
    that tracks what actually reached a document.
  - `STREAMING_ABORT_ON_FOCUS_CHANGE` still selects the old hard abort, and
    the tests for that behaviour opt into it explicitly.
- **The post-pause speech measurement buckets at 20 ms, not 100 ms**
  (`STREAMING_SPEECH_RUN_WINDOW_MS`). It takes the longest *unbroken* run
  above the threshold, and the bucket size is what makes that meaningful: at
  100 ms two keystrokes 100–150 ms apart fall into adjacent buckets, the run
  never breaks, and typing at 120 wpm measured a 1.5 s "speech" run — longer
  than most words. At 20 ms the gap between keystrokes gets its own bucket.
  Measured: a 150 ms word 0.16 s and a 300 ms word 0.30 s, against 0.02 s
  for typing at 80–120 wpm and 0.04 s for a mouse double-click. Summing all
  loud buckets instead of taking the longest run does not work either: at
  100 ms buckets two clicks 300 ms apart totalled exactly as much as one
  150 ms word. (At 20 ms the sums differ, but the longest run separates
  them by far more — 0.02 s against 0.16 s.)
- **Inline locale markers are stripped by matching the app's own language codes**
  (`transcriber/base.py:strip_language_tags`). Nemotron emits "<de-DE>" or
  "<|en|>" inline in automatic-language mode and it was pasted into the
  document. Matching "<xx-anything>" deletes far too much real dictation —
  measured, it ate `<my-widget>`, `<el-button>`, `<dom-if>` and `<log-2026>`
  — so a region must be two uppercase letters or three digits and a script
  four letters (any case). A bare `<xx>` is never matched: `<tr>`, `<br>`
  and `<td>` are markup, and "tr" is a real language code. The function must
  **not** trim its result: it runs per decoded chunk and the caller
  concatenates, so trimming welds "Guten Tag" onto "heute". Nemotron strips
  the accumulated text too, because a chunk boundary can fall inside a
  marker and neither half matches alone.
- **An unalignable window replaces only the current segment**
  (`merge_rolling_window_transcript(protected_prefix=...)`). Each measured
  pause closes off the text before it as `segment_floor`, and a later replace
  cannot reach past it. Without this, one admitted transient could cost the
  whole dictation: its hallucination becomes the text the next real window
  has to align against, that alignment fails, and the replace wiped
  everything. Properties this depends on, each wrong once:
  - **The floor check accepts a raw prefix OR a word prefix.** Word-only was
    tried and reverted: `stream_join_text` welds leading punctuation onto the
    previous word, so the floor's last word gains a "." on the very call that
    pins the floor, the word comparison fails, and the whole dictation is
    replaced. Raw-only misses a provider that re-cases a committed word.
  - **The floor advances only when a window ALIGNED and ADDED something**
    (`merge_rolling_window` reports the branch; the caller must not infer it
    from `startswith`, which is wrong in both directions -- once a floor
    exists the replace branch also returns text starting with `previous`).
    Both halves are load-bearing. Aligning is the corroboration: two
    overlapping windows agreed on the seam. Requiring growth stops a ratchet:
    whisper repeats the same invented phrase across windows sharing 96% of
    their audio, two IDENTICAL windows align trivially, and pinning that made
    the phrase permanent while the next drift appended a fresh one after it
    (measured: 53 words from 4 of real speech, growing linearly with the
    pause). A repeat leaves the text unchanged, so requiring growth skips
    exactly that case. It closes the identical-repeat ratchet only: a
    hallucination that grows in an alignable way is indistinguishable from
    speech to the merge and is still pinned. That is inherent to an
    energy-gated, text-alignment design without a spectral VAD.
  - **Pinning only at a measured pause left a hole against its own boundary.**
    Alignment already fails around 7.2-8.0 s of silence, before `new_segment`
    fires at 8.0 s, so an ordinary thinking pause had neither overlap nor
    floor and the replace wiped the whole dictation. The text then went
    backwards, so the locked prefix could never advance again and live
    insertion froze for the rest of the session too.
  - **The bound is one growing window's contribution**: a replace discards
    what the last window that ADDED text contributed, not what the
    current one did (the current one added nothing, which is why the
    floor stalled). The magnitude is one window: a `new_segment` window can carry up to
    `STREAMING_PARTIAL_WINDOW_S` of freshly decoded speech.
  - **A hallucination that survives to the next pause is pinned permanently.**
    Accepted: bounded junk that stays beats real text that disappears.
- **A window after a pause is appended only when it is shown to hold speech**:
  `_StreamResult.silent_seconds` accumulates the skipped audio (tracked even
  when the gate is switched off — wiring two behaviours to one checkbox meant
  disabling the gate silently disabled the pause handling too; the reset to
  zero must stay inside the "this slice carried sound" branch, or the
  counter is incremented and zeroed on the same call and never accumulates). A window
  arriving after more than `stream_partial_window_s` of silence shares no
  audio with what is already transcribed, so the overlap search has nothing
  to anchor on and the window is taken on trust. That is the most dangerous
  input there is, so the decision measures the window that will *actually be
  decoded* with `vad.measure_longest_speech_run_s` and requires
  `STREAMING_NEW_SEGMENT_MIN_SPEECH_S` (**0.08 s** — the value in
  `config.py` is authoritative; read its comment before touching it) of
  above-threshold audio. A peak measurement is not enough: a 5 ms keyboard
  click clears it, and each click ending a pause appended a fresh
  hallucination that the merged-text callback pasted straight into the
  document.

  **Both sides of this cut are transcript loss, and three values have already
  been wrong.** The history matters, because three of them were set as if a
  clean separation existed:

  | value | status |
  | ----- | ------------------- |
  | 0.35  | deleted short answers after a pause ("Ja.", "Stopp.") |
  | 0.15  | carried across the 100—>20 ms bucket change without being rederived |
  | 0.18  | deleted "Bitte." (0.085 s) and "Stopp." (0.100 s) |
  | **0.08** | **current.** Blocks silence; admits transients, by design |

  One derivation was also methodologically wrong and is worth not repeating:
  it sliced the sample into 300 ms excerpts, which truncates every run at the
  excerpt edge and invents short values the code never computes — production
  measures the longest run in the whole trailing window.

  **An energy gate cannot separate a resonant thump from a short word** — a
  heavy low-frequency knock and a 200 ms word both measure ~0.20 s, and a key
  clack (0.080 s) and "Bitte." (0.085 s) are one bucket apart. So the residual
  risk is handled by bounding the damage (`protected_prefix`), never by moving
  this number further. A window that fails
  this test is not decoded at all, because too little speech to append on
  trust is also too little to trust a *replace* — decoding it is how an
  invented sentence wiped a real dictation. The finalizer applies the same
  rule to its trailing window. Never relax this back to `silent_seconds`
  alone, and never make the append unconditional: that is what grew to 896
  junk words during two minutes of an open microphone.
- **The stream partial callback carries the merged transcript, not the window**:
  the controller's locked-prefix insertion compares against what it has already
  pasted, and a raw rolling window does not contain that text — so live
  insertion froze for the rest of the session as soon as the window rolled past
  it, while the overlay still reported progress. The transcriber merges and
  emits `session.result.merged_text`; the controller must not re-merge.
- **A live insert that fails gives its words back — unless it may have landed**:
  `apply_partial_append_only` marks text committed the moment it is handed to
  the inserter, so a failed paste would otherwise lose it for good: the locked
  prefix can never offer it again. The controller calls
  `StreamingTextState.rollback_commit(previous)` and retries on the next
  partial. **Two failure paths run *after* the paste keystroke** — the
  post-paste clipboard-contention check and "text pasted but clipboard restore
  failed" — and rolling those back pastes the same words twice, up to the
  retry limit. They therefore raise `TextMayHaveBeenPastedError`, which the
  retry refuses to act on. **Classification is driven by one `paste_sent`
  flag, not by picking a class at each raise site** — that approach missed
  the likeliest site of all, a clipboard verification read failing because
  a clipboard manager had the clipboard open. A post-paste failure also
  must not offer the Insert action, which would paste the text again.
  `STREAMING_LIVE_INSERT_RETRY_LIMIT` is deliberately small (3): each attempt
  runs on the Qt thread and a held modifier costs the full 1.5 s modifier-
  release timeout, so a large limit turns a stuck target into seconds of
  unresponsive UI.
- **A remote stream handshake never runs on the Qt thread**: Deepgram waits up
  to 8 s for its socket (`connected.wait(timeout=8.0)`) and the AssemblyAI SDK
  connects synchronously. Called inline from `_start_streaming_recording` that
  froze the overlay, tray and settings for the whole handshake, at exactly the
  moment the user pressed the hotkey to start talking. `_begin_stream_connect`
  opens the microphone first and runs the handshake on a worker thread; audio
  recorded meanwhile is buffered (`_stream_preconnect_chunks`, bounded by
  `STREAMING_PRECONNECT_BUFFER_MAX_BYTES` — past that the newest chunks are
  dropped with a warning) and flushed **in order** on that same worker before
  the completion signal. The overlay says "Connecting to the speech service.
  You can speak now." until the stream is live, because the microphone
  genuinely is open. Consequence for tests: a stream-start failure now arrives
  through a queued signal, not on the caller's stack.
- **Stopping or aborting must retire an in-flight handshake, never race it**:
  `stop_stream()` called while `start_stream()` is still running is not a
  no-op. The provider rejects the stop because the session is not active
  *yet*, the handshake then publishes a socket nobody owns and marks it
  active, and every later dictation fails with "Streaming session already
  active" until the app restarts — from one hotkey press inside the 8 s
  window, or from a single microphone failure. `_submit_stream_finalize`
  therefore hands the connect thread to the job and
  `_finalize_stream_worker` joins it (bounded by
  `STREAMING_CONNECT_JOIN_TIMEOUT_S`, off the Qt thread) before stopping;
  the capture-start failure path uses `_teardown_pending_stream_connect` for
  the same reason. Both also bump `_stream_connect_generation` and clear the
  buffer, which is what keeps a late flush out of the next session. A stale
  flush must **refuse to push without clearing the buffer** — clearing it
  destroyed the live session's buffer and killed the new dictation.
- **Remote stream finalizes have their own worker**: `_executor` stays
  `max_workers=1` so two local models never load at once, but a remote finalize
  loads nothing — `stop_stream()` drains a socket. Sharing that queue meant
  pressing stop on an AssemblyAI or Deepgram dictation left it "Processing"
  until an unrelated local batch transcription ahead of it had finished.
  `_stream_finalize_executor_for` routes by engine; local streaming still
  finalizes on the shared worker because it really does re-transcribe audio.
- **Concurrent transcription mode + cooperative cancel**: a finished
  transcription is *never* discarded. `concurrent_transcription_mode`
  (`insert` default / `history` / `cancel`) decides what happens to the
  in-flight transcription when a new recording starts: `insert` keeps it and
  inserts its result into the window that was focused when it was recorded
  (plus history); `history` keeps it but only saves to history; `cancel`
  requests a real stop and, if it still finishes, keeps it in history. Local and
  remote *batch* work shares the single `max_workers=1` transcription
  executor, so those jobs serialize — this only changes delivery. The one
  exception is a remote stream finalize, which has its own single worker
  (`_stream_finalize_executor_for`) because it drains a socket instead of
  loading a model: it can overlap one local batch job. **That lane is skipped
  while an older job is still working** (`_has_undelivered_older_job`).
  Delivery is otherwise in recording order — one shared worker runs everything
  else in FIFO, and a foreground result flushes the deferred older ones before
  pasting its own — but neither holds for a result that does not exist yet, so
  a fast remote finalize could overtake an older transcription and paste the
  *later* dictation first. Reaching it needs an engine switch between two
  dictations while the first is still transcribing, which is narrow but real.
  While such a job exists the finalize joins the shared queue, i.e. it behaves
  exactly as it did before the lane existed. Each recording
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
  the future if it has not started. **Every local engine can now be stopped
  mid-run**; the remote providers still only skip-if-not-started and otherwise
  run to completion with their result kept in history. See "Cancelling a
  running local transcription" below for how each local engine does it.
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
- **Cancelling a running local transcription**: every local engine polls
  `set_cancel_check` during its compute and raises `TranscriptionCanceled`;
  before this, Cancel only worked for faster-whisper. The others kept a CPU
  core busy, held their model in memory, **and held the single
  `max_workers=1` transcription worker**, so the next dictation queued behind
  a job the user had already given up on. Worse, a preload started afterwards
  waits for the *shared* runtime lease that the stuck job owns, so the overlay
  then reports "still loading" forever while each dictation quietly pays for
  its own isolated runtime. Reported from the field: an accidental Canary run
  that Cancel could not stop.
  - `faster-whisper` — between segments (unchanged).
  - `onnx-asr` (Parakeet, Canary) — onnx-asr offers no hook and
    `recognize()` is one blocking call whose encoder pass alone runs for
    seconds. `_install_cancel_hooks` therefore walks the loaded model for its
    `InferenceSession` objects and wraps `run` so every call carries a
    `RunOptions` the app owns; a watchdog thread polls the cancel check every
    `_CANCEL_POLL_INTERVAL_S` and sets `terminate`, which ONNX Runtime honours
    from another thread within milliseconds. Measured on the real Canary and
    Parakeet models: 0.66 s to cancel against a 4.46 s / 3.21 s run. Three
    properties are load-bearing: the handle is **per call**, because ONNX
    Runtime never clears `terminate` and a reused handle would fail the next
    transcription instantly; the abort surfaces as a generic ORT `Fail`, so it
    is mapped to `TranscriptionCanceled` **only when we asked for it**, never
    by matching the message; and the wrapped sessions are shared, so
    `transcribe_batch` serializes on `_inference_lock` — overlapping runs would
    let one job's cancel abort the other. A session stays fully usable after an
    abort (verified), so the model is not reloaded.
  - `Nemotron` — one check per fixed 560 ms chunk in the batch loop.
  - Cohere/Granite Node runtime — the response reader polls between its 0.25 s
    ticks; a cancel **kills the child process**, because the request is already
    in flight and the child would otherwise keep transcribing. That discards
    the loaded model, which is the point: freeing the CPU and the memory is
    what Cancel is for.
  The pre-run check must sit **before the model load**, not only before the
  run: a job cancelled while it waited in the queue would otherwise still pull
  a multi-gigabyte model into memory to throw the result away.
  Three properties of the shared machinery hold this together:
  - **The cancel check lives on `ITranscriber`, not per engine.**
    `transcriber/base.py` owns `set_cancel_check`, `_is_cancel_requested`
    (which logs a raising check once and then latches) and `_raise_if_canceled`.
    A subclass that overrides the setter must call `super()`: assigning
    `self._cancel_check` directly skips the latch reset, and because a runtime
    is cached for the whole app lifetime that turns "once per installed check"
    into once per process, so the second broken check that session is silent.
    faster-whisper had exactly that override.
  - **`close()` unwraps under `_model_lock` *and* `_inference_lock`.** Removing
    the wrappers while a `recognize()` is in flight switches that run back to
    the session's own `run`, so the watchdog keeps setting `terminate` on a
    `RunOptions` nobody passes any more and the transcription finishes in full
    with no log line — the cancel turned off, silently. No caller reaches that
    today (every close path waits for the runtime lease first), and the two
    locks are acquired in the order `transcribe_batch` takes them, which it
    holds sequentially rather than nested, so nesting them here cannot
    deadlock against it.
  - **A canceled *download* is a cancel too.** A transcriber that finds its
    model missing downloads it from its own load path, and pressing Cancel
    makes the shared slot raise `ModelDownloadCanceled`. Every local engine
    wraps that path in `base.canceled_download_is_a_cancel()`, which remaps it
    to `TranscriptionCanceled`; without it the user got an error dialog for the
    thing they had just asked to stop. Two consumers had to follow:
    `_preload_model_worker` reports it as a cancel instead of "could not be
    loaded" (the failure branch also *persists* that result, so the next
    dictation re-raised it rather than retrying), and `run_benchmark_cases`
    raises `BenchmarkCancelled` instead of recording a case with an `error`,
    which would have written a permanent error row into benchmark history.
- **The preload says which half of its work is running**: a preload downloads
  and then loads, and only the first half has measurable progress (the bar is
  directory growth). Reporting both as a download printed a frozen
  "Downloading ... approx. 100%" for a model that was already complete on
  disk, and the recording-start notice said "is still loading" while a
  multi-gigabyte fetch was running. `_preload_phase` holds
  `(generation, phase)`; it is generation-scoped so a retired worker cannot
  describe what the current preload is doing, and `_preload_phase_word` feeds
  both the recording-start notice and the streaming-mode refusal. There is a
  third phase, `queued`: a preload waiting behind another one is doing neither,
  and borrowing "loading" for it named the wrong wait. The phase must be
  cleared on **both** paths that end a preload — `_on_model_preload_done` and
  the branch of `on_settings_changed` that cancels it outright when the new
  engine is remote — or `_current_preload_phase()` keeps answering for a
  preload that ended, breaking its own "empty when none is running" contract.
- **A preload must not hide a failed hotkey registration**: `show_idle_status`
  returns early while `_preload_owns_overlay()`, because a running preload
  rewrites the status line every 600 ms and replacing it with "Idle" only
  produces two content swaps and two window resizes. That gate belongs
  **below** the four hotkey-error branches, not above them: `reload_settings`
  calls `show_idle_status` specifically to reprint a hotkey the save may have
  changed, and gating first swallowed the one message the user has to see.
- **A language change never reloads a runtime**: `language_mode` is
  deliberately absent from both the transcriber cache key and the preload key.
  Every engine takes the language as a per-request/per-session parameter
  (faster-whisper `transcribe(language=...)`, Nemotron's `lang_id` runtime
  option, the Cohere/Granite JSON request field, and the remote providers'
  request parameters), so `ITranscriber.set_language_mode` applies it to the
  live instance when a job acquires the runtime — acquisition is serialized by
  the runtime lock, so a reused runtime can never transcribe with a stale
  language. Providers that restrict the accepted values override
  `_normalize_language_mode`; anything derived from the language must be
  recomputed there or read per request. `controller.set_language_mode`
  therefore only persists and syncs the UI: it must not reset the transcriber
  cache or start a preload. Before this, switching language tore down the
  loaded model, so a mistyped selection blocked the correction behind a full
  reload and transcribing one recording in another language evicted the model
  the next dictation needed. `tests/test_factory.py` asserts the setter exists
  and works for every engine — keep that guard.
- **A settings save reloads the model only when the model changed**: the
  language exemption above was one case of a broader defect. `reload_settings`
  used to reset the transcriber cache on *every* save and `on_settings_changed`
  to preload unconditionally, so changing the overlay opacity, a hotkey, or the
  completion tone closed a multi-gigabyte local model and loaded the identical
  one again. `_transcriber_identity(settings)` is now the single description of
  what `create_transcriber` bakes in; the reset runs only when it differs, and
  `_local_model_preload_needed` starts a preload only when the shared cache
  does not already hold that exact runtime (a previously *failed* preload is
  still retried on every save, which is when the user expects a fix to be
  picked up). Three consequences to keep intact:
  - **The identity must list every constructor argument, and only those.** The
    unconditional reset hid three omissions — `custom_vocabulary`,
    `silence_gate_enabled` and `silence_gate_threshold` were absent, which was
    harmless only because the cache was thrown away anyway. A parametrized test
    asserts a reload for each field and a *no* reload for unrelated ones, and
    both halves guard against a no-op parameter (a value equal to the default
    would make the test pass without testing anything, which happened once with
    `keep_onnx_model_loaded`).
  - **The identity is built per engine, and for `local` per runtime.**
    `local` is four runtimes with four different constructor signatures, so
    one flat list of every local field is wrong in the other direction: it made
    Parakeet reload its 670 MB model when the user typed a custom-vocabulary
    term that onnx-asr never receives, and a Nemotron reload for
    `keep_onnx_model_loaded`, which only the Node runtime reads. The branches
    in `_transcriber_identity` mirror `_create_local_transcriber` exactly and
    must be kept in step; `_LOCAL_RUNTIME_FIELDS` in `tests/test_controller.py`
    pins, per runtime, which settings its identity reads and which it ignores.
    The remote half looks up `_ENGINE_MODEL_FIELDS[engine]` and
    `_ENGINE_KEY_FLAGS[engine]` strictly rather than with `.get(..., "")`,
    backed by a test that both maps cover every remote engine — a missing entry
    now fails the suite instead of silently reading no key at all.
  - **API keys are not in `AppSettings`.** `has_*_key` flips only when a key is
    added or removed, so replacing a key with a different value leaves the
    settings snapshot byte-identical and the identity cannot see it. The
    settings dialog therefore emits `provider_keys_changed` in addition to
    `settings_changed`, and `main` connects it to
    `controller.invalidate_transcriber_credentials` *before* the settings
    signal so the stale runtime is gone before the preload decision runs. That
    signal **carries the affected provider names**, and the invalidation is
    scoped to them: a key belongs to exactly one engine, so a loaded local
    model (which reads no key at all) and a Groq runtime under an OpenAI key
    change are both left alone. Selecting that provider later changes
    `settings.engine`, which the identity does see. The loaded engine is read
    from `_TranscriberIdentity.engine` rather than a tuple slot, which is why
    the identity is a `NamedTuple`.
- **Bookkeeping in a worker's `finally` must not be able to skip the release**:
  `_transcribe_worker` clears diagnostics and cancel hooks before releasing the
  runtime lease, which is the required order -- so anything raising in that
  bookkeeping stranded `_transcriber_runtime_lock` for the process lifetime,
  after which every dictation pays for its own isolated runtime and a preload
  waits forever. The order is unchanged; the bookkeeping is wrapped so its own
  failure is logged and the release still runs from an inner `finally`.
- **Evict the cached transcriber before closing it**: both
  `_reset_transcriber_cache_locked` and
  `_reset_resume_sensitive_transcriber_cache` set `self._transcriber_cache =
  None` first. Closing first meant a `close()` that raises left the dead
  runtime installed as the cache, and the next dictation used it.
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
  A drag claims the manual position on its **first movement**, not on mouse
  release, and `_reposition_within_current_screen` returns early while
  `_drag_active`. Startup keeps updating the overlay (preload progress,
  "Model loaded", the idle status) and every such update repositions a
  not-yet-manual overlay back to its configured corner — so with the claim
  deferred to the release, dragging the overlay during startup made it jump
  out from under the cursor.
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
- **Every ONNX-device choice must be measurable**: the General tab pins
  `local_onnx_device` for Cohere/Granite *and* Nemotron, so the benchmark has
  to be able to compare the same targets. It used to expand
  `webgpu_device_targets` only for the `onnx-webgpu` runtime and run Nemotron
  on the hardcoded `device="auto"`, so "All explicit targets" silently
  measured nothing for it. `local_benchmark.benchmark_device_targets` is the
  one place that maps a runtime plus the requested targets onto the cases to
  run, and both the case loop and `total_cases` go through it. For Nemotron it
  renames each target to the provider it actually resolves to
  (`nemotron_provider_order`) and drops duplicates: ORT GenAI has no WebGPU
  provider, so `webgpu`, `gpu` and `dml` are one configuration and reporting
  it three times under three names would be worse than not offering it.
- **Canary needs an explicit language, and the benchmark says so before the
  run**: `run_benchmark_cases` refuses Canary without a language, because
  onnx-asr hardcodes `<|en|>` and the model would *translate* German instead of
  transcribing it. That refusal happens per model, i.e. only when that model's
  turn comes, so with several models selected the whole run finished before the
  single failure was visible. `_run_local_benchmark` now rejects the
  combination up front, mirroring the existing German/English-only guard.
- **Model size estimates are measured, not copied**: `MODEL_ESTIMATED_SIZE_MB`
  drives the download percentage, so a wrong number is directly visible.
  `distil-large-v3.5` was listed at 756 MB against a real 1513 MB `model.bin`,
  so its bar read "approx. 100%" at half the transfer and kept counting. Verify
  a new entry against the repository with the download allow-patterns applied.
- **Download parallelism is a measured non-lever**: `snapshot_download`'s
  `max_workers` parallelizes across *files*, and every local ONNX model is one
  dominant weight file (Parakeet 652 of 671 MB), so it cannot help. Measured on
  a ~70 Mbit/s line: 2 workers 76.7/77.6 s against 8 workers 76.6/76.4 s. Do
  not raise it without a new measurement, and do not add parallel *model*
  downloads: the bandwidth is shared either way while every extra writer needs
  its own worker process, progress row and cancel/cleanup bookkeeping.
- **Ruff's rule set is written out, never inherited**: `pyproject.toml` names
  every selected rule and every ignore with its reason. Before this ruff ran on
  its bare defaults, so the CI gate checked pyflakes and a handful of
  pycodestyle errors only — a naive-datetime elapsed counter, three unchecked
  `zip()` length assumptions and loop-variable closures all passed it. A ruff
  upgrade must be allowed to surface new findings; it must never silently
  change what the gate means.
- **Benchmark transcripts are first-class results**: every measured run stores
  and exports its complete transcript. The Benchmark tab renders History as a
  column table and compares each model/device run with run 1; keep all runs
  because GPU/runtime numeric differences can occasionally change decoded
  text. Legacy entries without transcript text remain readable.
- **Selected local models are strict**: recording may start while the selected
  runtime preloads, but transcription waits off the Qt thread for that exact
  settings snapshot. Never choose, persist, or transcribe with a fallback
  model. Preload results and cancellation are generation-scoped; a canceled or
  stale worker cannot publish failure/readiness into a newer preload. Batch may
  use an isolated runtime with the same settings when a live stream leases the
  shared runtime, preventing a stream-finalizer/executor deadlock.
- **`stt_app/transcriber/__init__.py` resolves its names lazily (PEP 562)**:
  importing any submodule runs the package first, so
  `import stt_app.transcriber.local_faster_whisper` used to pull in the
  AssemblyAI, Azure, Deepgram, ElevenLabs, Fun-ASR, Groq and OpenAI modules
  with it. The download and inventory-scan worker subprocesses do exactly
  that and paid 0.232 s / 330 modules per launch for provider code they never
  call; they now pay 0.114 s / 234. `__getattr__` caches each resolved name in
  `globals()`, `__all__` is derived from the lazy map, and a test pins both
  the public surface (so a name cannot be dropped by editing the map alone)
  and the agreement between the `TYPE_CHECKING` imports and that map -- a name
  typed for static checkers but missing from the map is an `AttributeError`
  no type checker can see. **Do not delete the `if TYPE_CHECKING` block.** It
  keeps editors and linters working, and it is also the packaged app's only
  static link to `factory` and the providers: PyInstaller's modulegraph scans
  `IMPORT_NAME` opcodes without following control flow, so it walks into that
  block and finds them, while `_LAZY_ATTRIBUTES` is strings it cannot read.
  Verified on PyInstaller 6.22: a graph rooted at
  `from stt_app.transcriber import create_transcriber` contains `factory`, all
  seven providers and all four local runtimes. The typed/lazy agreement test
  is what keeps the block from being deleted or renamed.
  Note that the package no longer binds its submodules as attributes until
  something resolves a lazy name, so `stt_app.transcriber.base` raises
  `AttributeError` in a fresh interpreter. `unittest.mock` and pytest both fall
  back to `import_module`, so every existing patch target still works; do not
  write new code that reaches a submodule through `getattr` on the package.
- **The benchmark CLI cancels by thread, the app cancels by killing the
  process**: `scripts/benchmark_local.py --isolated-case` (the default) and
  the Settings benchmark both terminate the child process, which is why
  `run_benchmark_cases`' `cancel_check` had no production caller. The
  `--no-isolated-case` path ran the case on the main thread, where Python
  cannot run a signal handler while the process sits inside
  `InferenceSession.run` -- Ctrl+C was invisible until the call returned
  (4.46 s for one Canary run, times `--runs`). `_run_case_threaded` runs the
  case on a worker thread and keeps the main thread in a *poll* loop, and the
  flag Ctrl+C sets is handed to the model as `cancel_check`, which ONNX
  Runtime honours mid-run. The case-level `except Exception` must keep
  re-raising `BenchmarkCancelled` first, or a cancel is recorded as a failed
  case.
  Every wait in this script polls at `_CASE_POLL_INTERVAL_S`; none of them is
  a single long `join`, and that holds for the child process as much as for
  the thread. Measured: `Process.join(6.0)` delivered an interrupt raised at
  0.5 s only after 6.01 s, the poll after 0.62 s. `_join_case_worker` is the
  one helper both use.
  **The parent reads the child's queue while it is still running**
  (`_collect_worker_payload`), never after waiting for it to exit. A
  `multiprocessing.Queue.put` returns immediately and a feeder thread writes
  the pickled payload into an OS pipe, so the child blocks at exit until the
  parent drains it -- waiting for the exit first is a deadlock as soon as the
  payload outgrows the pipe buffer. Measured with the real classes: 8 KB
  completed, 16 KB hung forever, and the wait had no budget, so the CLI hung
  with no output. The payload is `asdict(case)`, i.e. every run's full
  transcript, so a few minutes of audio or a short clip at `--runs 3` reaches
  it; the repository's own 24 s sample is why it went unnoticed. The shipped
  app is not affected and must stay that way: `benchmark_process.py` uses
  `subprocess` with a dedicated stdout reader thread that drains the pipe
  concurrently, and `src/` uses no `multiprocessing` at all.
- **`_TranscriberRuntimeLease.release()` always hands the runtime back, and
  swallows both a failed close and a failed deferred cache reset.** The close
  and the hand-back used to be two bare statements, and
  `_close_cached_transcriber` swallows `Exception` but not
  `BaseException`, while `_released` is set before either runs -- so a close
  that died stranded `_transcriber_runtime_lock` for the process lifetime and
  a retry was a no-op. It is also the single root of three symptoms, because
  `_transcribe_worker`, `_finalize_stream_worker` and `_preload_model_worker`
  all call `release()` from a `finally` that sits *outside* their own
  `except BaseException` arm and emit their terminal signal afterwards: the
  escaping exception swallowed that signal too, leaving the overlay in
  Processing with no error and no Retry, or `_streaming_recording` stuck True
  so every later hotkey press was refused. Hence log-and-drop rather than
  re-raise: handing back is the contract, closing is best-effort on top of it.
  **Both statements need the guard, and guarding only the close was the first,
  insufficient fix.** The hand-back applies the deferred cache reset, which
  closes the *cached* transcriber through that same helper, so the identical
  `BaseException` still reached the worker through the other door. Nothing is
  stranded by swallowing it: the admission lock and the use count are handed
  back inside `_release_transcriber_runtime`'s own `finally` before anything
  can escape, and a reset that failed leaves `_pending_transcriber_cache_reset`
  set so the next release retries it. What is *not* claimed is that `release()`
  cannot raise at all -- a logging handler that throws still escapes, and
  chasing that would be defence without a failure mode behind it.
- **Clear a resource's owner flag *before* the call that disposes of it.**
  `_acquire_transcriber_runtime`'s shutdown branch closed the runtime and then
  set `orphan = None`; a close raising `BaseException` therefore left `orphan`
  set, the outer arm closed the same runtime again, and that second raise
  replaced the first -- so the caller got the close's error instead of the
  `TranscriptionCanceled` the branch exists to deliver.
- **Every arm that abandons a stream start must tear the handshake down, not
  just release the lease.** `_begin_stream_connect` has already spawned it, so
  releasing alone lets `start_stream` publish a session nobody owns; every
  provider then refuses the next one with "Streaming session already active"
  and a remote socket stays open and billed until restart. All three arms of
  `_start_streaming_recording` do this now, teardown before release and unable
  to raise past it.
- **A swallowed streaming partial callback is logged once per session.**
  That callback is what puts live text on screen and into the document, so
  a bare `pass` made a dead live-insertion path indistinguishable from a
  user who had simply stopped talking. It must stay swallowed -- one lost
  delivery costs nothing, the next partial carries the whole merged text
  again -- and it must stay latched behind `partial_callback_failed`, the
  same shape as `noise_floor_warned`, because it runs about every 350 ms.
- **`tests/conftest.py` blocks the real `create_transcriber`.** The isolated
  arm of `_acquire_transcriber_runtime` -- taken whenever the shared lock is
  already held -- calls the module-level function, so the 27 patch sites that
  replace `_get_or_create_transcriber` do not cover it, and a test slipping
  onto that arm builds a real provider client or a real local runtime that
  downloads its model. The fixture raises a named `AssertionError`; the 64
  tests that patch `stt_app.controller.create_transcriber` themselves are
  unaffected, because `monkeypatch` applies theirs afterwards.
- **`_start_streaming_recording` has two capture-failure arms, and a test that
  fails `_build_audio_capture` reaches only the first.** That call returns
  before `capture.start()` exists, so the `AudioCaptureError` arm below it is
  never entered; reverting its guard left the whole suite green. A test for
  that arm needs a capture object that builds and then refuses to `start()`,
  which is what a microphone held by another application does. Same shape for
  `_acquire_transcriber_runtime`: the isolated branch and the shared branch
  need separate tests, and only the shared one holds
  `_transcriber_runtime_lock`.
- **Every worker that emits its terminal signal after the `finally` needs a
  last-resort `except BaseException`**: `_transcribe_worker`,
  `_finalize_stream_worker` and `_preload_model_worker` all have that shape,
  and anything escaping the `try` skips the emit entirely. Measured
  consequences, one per worker: the overlay stays in Processing with no error
  and no Retry for the rest of the session; `_streaming_recording` stays True
  so every later hotkey press is refused with "Streaming transcript is still
  finalizing"; and `_preload_phase` goes on describing a preload that ended,
  breaking its "empty when none is running" contract. The arms deliberately do
  not re-raise -- a `BaseException` here can only come from a callback, since
  CPython delivers KeyboardInterrupt to the main thread only and a SystemExit
  on a worker thread just ends it, so reporting beats vanishing.
  **`_acquire_transcriber_runtime`'s two cleanup arms are `BaseException` for
  the opposite reason**: they only undo their own bookkeeping and re-raise, so
  the broader catch can hide nothing, while missing one strands
  `_transcriber_runtime_lock` for the process lifetime. The worker `finally`
  cannot cover that -- the lease is still `None` when the acquire raises.
- **A failed capture in `_start_streaming_recording` must tear down the
  handshake, not just release the lease**: `_begin_stream_connect` has already
  spawned it, so `start_stream` completes and publishes a session nobody owns.
  Every streaming provider refuses a second session, so the next dictation
  fails with "Streaming session already active" and a remote provider's socket
  stays open and billed until then. Use `_teardown_pending_stream_connect`, the
  same call the `AudioCaptureError` arm below it makes. Note that no statement
  in `_build_audio_capture` is currently known to raise -- `AppSettings`
  already coerces and clamps `vad_energy_threshold`, and the `EnergyVad` and
  `AudioCapture` constructors are attribute assignment -- so both guards are
  depth, kept because the blast radius is out of proportion to the cost.
- **A picker label never hand-writes a model's size**: `LOCAL_MODEL_LABELS` is
  built from a name-and-notes table plus `MODEL_ESTIMATED_SIZE_MB`, which is
  the table corrected whenever a real download disagrees with it. Written
  twice, the two drifted: `distil-large-v3.5` read "~756 MB" against a measured
  1516 and `large-v3-turbo` "~809 MB" against 1622 -- the two models a user
  picks between by size, both understating themselves by half, while AGENTS.md
  already recorded the 756 MB figure as a *fixed* defect. Nearly every other
  entry was a few percent out from dividing by 1000. A test rejects any label
  stating a size more than 5% from the table.
- **The delete confirmation names every folder it will remove**: the inventory
  searches the Model Dir *and* the default Hugging Face cache, so one row can
  mean a copy in either, and the shared cache holds models other tools put
  there. "This removes downloaded files from disk" did not say which disk.
- **`scripts/smoke_test.py` must never touch the real settings file**:
  `SettingsStore.load` is not a read -- it creates the file and a `.bak` when
  none exists, rewrites it whenever the stored payload differs from the
  normalized one, and renames both the file and its backup to
  `*.corrupt.<timestamp>` when the JSON will not parse. A diagnostic that
  quarantines a user's configuration is worse than no diagnostic, so it loads a
  throwaway copy. Its model step also skips every non-`local` engine: the
  transcriber is built without a secret store there and dies with "API key is
  missing", and no remote provider implements `preload_model` at all.
  **And it must not move the folder the settings live in.** Loading a model
  reaches `appdata_root()` -- measured chain: `preload_model` ->
  `_coordinated_download_if_missing` -> `run_coordinated_download` ->
  `acquire` -> `_acquire_cache_lock` -> `_download_lock_dir` -- which is a
  *setup* call that renames a legacy `tts_app` install onto the current name.
  So the commit that stopped the script touching `settings.json` reintroduced
  the same side effect one level out, through a call chain no grep of the
  script reveals, and made it *more* likely by adding a fresh-install branch
  that reaches the model step where the old code returned early.
  `_legacy_data_folder_would_be_moved()` now declines the step and says why.
  Creating a data folder that does not exist yet is left alone: it holds
  nothing of the user's, and refusing to create it would strand a legacy
  install forever -- an empty `stt_app` beside `tts_app` is exactly the state
  in which `appdata_root()` stops migrating.
- **A diagnostic must read the file it proved it can read, not the one it
  was handed.** The settings reader falls through to the `.bak` when the
  primary raises `OSError`, and then copied *the primary* into the sandbox --
  reading again the file that had just refused, so every `OSError` the backup
  exists to survive came back as a crash and `--strict` 1 for an install the
  app starts fine on. Tracking `usable_path` removes the failure mode instead
  of guarding it: the file that parsed cannot fail to be copied for that
  reason.
- **When the settings are unusable, check the defaults -- do not check
  nothing.** `SettingsStore.load` quarantines a file that will not parse and
  writes defaults, so the app runs on the default model. Reporting the
  problem and skipping the model check made `--check-model` verify nothing on
  exactly the broken install it exists for, while the script's own message
  already said "the app will discard it".
- **The suite's Hugging Face isolation lives in `pytest_configure`, not a
  fixture**: `huggingface_hub` computes `HF_HUB_CACHE` and `HF_HUB_OFFLINE` at
  **import**, and a test module imports it at module scope, which pytest does
  during collection -- before any fixture runs. Set from a fixture they did
  nothing at all: measured after collection, the constants still pointed at the
  developer's real `~/.cache/huggingface/hub` with offline False, and
  `download_model_snapshot` passes no `cache_dir` when Model Dir is empty, so
  those frozen constants decide where a download lands. One session directory,
  not one per test: `tmp_path_factory` rescans its base directory on every
  call. The per-test `_coordinated_download_if_missing` stub is the half that
  does belong in a fixture, and `real_model_prefetch` restores it for the two
  files that assert the pre-fetch happens.
- **The benchmark CLI's `--isolated-case` payload is read while the child
  runs**: see the CLI entry above. The shipped app must stay on the
  `subprocess`-plus-reader-thread pattern in `benchmark_process.py`; `src/` uses
  no `multiprocessing` at all, and that is what keeps the same deadlock out of
  the app.
- **A best-effort cleanup on an error path must not be able to raise, and must
  not come before the release it precedes**: `_teardown_pending_stream_connect`
  reaches provider code and starts a thread, so a plain `RuntimeError` from
  `Thread.start` was enough to skip `runtime_lease.release()` once the teardown
  was placed in front of it -- stranding `_transcriber_runtime_lock` for the
  process lifetime and escaping the Qt slot, which left the overlay on
  "Listening" instead of showing the capture failure. The helper is now
  exception-tight and both arms release in a `finally`. General rule: adding a
  call to an error arm can make a failure *newly* reachable, so re-derive the
  arm's guarantees rather than assuming the fix only adds.
- **Copying an exception arm copies its preconditions too**: the preload's
  `except BaseException` was copied from the `except Exception` above it
  without the `_preload_generation_was_canceled` check that arm begins with,
  so a cancel was written to `_preload_results` as "could not be loaded" and
  `toggle_recording` re-raised it on the next dictation instead of retrying.
- **`MODEL_ESTIMATED_SIZE_MB` is decimal megabytes**, it says so, and
  `model_download_progress` converts it with `* 1_000_000`. Anything rendering
  a size from it divides by 1000. Dividing by 1024 and writing "GB" names
  neither unit, and the test that should have caught it divided by 1024 as
  well -- a test that shares the code's misunderstanding is not a check.
- **A reservation is measured, not predicted**: `retranscribe_dialog` reserves
  a `heightForWidth`, so the candidate is chosen by measuring every candidate
  through the polished label. `key=len` is character count, not drawn width
  (`W` x29 draws 319 px against 159 px for the 30-character longest label);
  `key=horizontalAdvance` is drawn width, and what is reserved is a wrapped
  height, which a width ordering does not order. **This is live, not
  hypothetical**: for a `granite-speech-4.1-2b-nar` entry the advance key
  under-reserves by 15 px at label widths 594-606, i.e. dialog widths 678-690,
  well inside the shipped 560 px minimum. The band is narrow -- the candidates
  differ at 134 of 841 reachable widths there, 100-102 for other entries --
  which is why three hand-picked width lists missed it and one review round
  concluded it could not happen. Measuring at construction time is wrong
  regardless: `setStyleSheet` alone does not apply the font, so the label
  reports 9 pt and a 16 px line height until it is polished, 11 px and 15 px
  after. (That does not flip the advance key's *winner* for today's names,
  only the ordering below it.)
- **`QLabel.heightForWidth` is floored by `minimumHeight()`, and the
  reservation *is* that minimum.** `QLabelPrivate::sizeForWidth` ends in
  `.expandedTo(minimumSize())`, so reading through a label that already
  carries a reservation returns the reservation. Measured on a bare label,
  one identical call: 15 px at `minimumHeight() == 0`, 400 px at 400, 15 px
  again at 0. Two consequences, and both bit:
  - `_reserve_note_height` was a one-way ratchet. `max(...)` over readings
    that cannot fall below the installed floor can only grow, so narrowing
    the dialog and widening it again kept the taller note -- 6 px for `small`,
    30 px for a 63-char imported id, taken off the transcript view -- while
    `resizeEvent` documented the reservation as correct at every size. It now
    clears the floor before measuring.
  - **Every wrong claim this pair of entries has carried came from measuring
    that way.** "The identical call returned 60 px at label width 556 and
    90 px at 476" was the dialog's own reservation at those two widths, not
    impurity; a later sweep reporting all 15 candidates agreeing at every
    reachable width was one installed floor read 841 times; and the
    corresponding test asserted `needed <= reserved` against a `needed` that
    was `reserved`, which is why an advance-key shortcut survived mutation.
  `heightForWidth(w)` itself *is* pure in `w`, verified with the argument held
  fixed while the label's width varied. The two clamps around it are not:
  `minimumWidth()` raises the argument (`heightForWidth(200)` returns 120 at
  minimum width 0 and 60 at 600), `minimumHeight()` raises the result. The
  rule that survived all four versions of this entry: measure through the
  real, polished widget, at the width in question, with the previous
  reservation removed.
- **Nothing that only reads may call `appdata_root`**: it creates the data
  folder and renames a legacy `tts_app` install onto the current name, so a
  path *lookup* migrated a user's settings, history and recordings. Use
  `existing_appdata_root` / `existing_settings_path`, which return `None`
  rather than a path that does not exist yet. `SettingsStore` is the one
  legitimate caller -- it is about to write there.
- **Every string a script can print is ASCII**, enforced for all of `scripts/`
  by `tests/test_script_output_is_ascii.py`. Redirected output on Windows is
  cp1252: a character outside it raises `UnicodeEncodeError` on stdout (this
  crashed `--validate-only` on a *valid* model) and is escaped to a literal
  `\uXXXX` on stderr (this wrapped the SSL-proxy guidance in two walls of
  `\u2550`). A character *inside* cp1252, such as an em dash, still renders as
  U+FFFD wherever the log is opened as UTF-8. Comments and docstrings are
  exempt -- except a module docstring in a script that passes `__doc__` to
  `argparse`, which `--help` prints; the test computes that per file.
- **The benchmark report's agreement column cannot rank the leading cluster,
  and must never be quoted as if it could.** It is a `difflib` ratio of word
  tokens against `large-v3`, and re-running it with each working transcript as
  the reference moves Parakeet between 1st and 8th -- the differences are one
  or two tokens out of 52, and on the deciding token the reference itself is
  wrong (`transkriptiere` is not a German word). What survives every choice of
  reference, and is all it may be used for: Plus last of 12, NAR 11th-12th
  (neither transcribed the recording), `tiny` 10th-11th. Between any two
  models that both worked it supports nothing -- even Parakeet against
  `small`, 98.1% to 91.3%, reverses under five of the thirteen references,
  because models that agree with each other are not thereby correct. Quote it
  with
  `autojunk=False` and the argument order the report states: `difflib` discards
  popular elements of its *second* sequence past 200 items, which is why one
  transcript scored 1.4% one way round and 2.8% the other.
- **The supportable claim for the default is "the fastest local model that
  transcribed the recording", never "fastest" and never "most accurate".**
  `tiny` is genuinely quicker (0.033 against 0.043). Both earlier versions of
  this claim were reached by finding a number that supported the conclusion
  instead of asking what would refute it.
- **A retraction is a claim and needs the same search.** Two figures were
  withdrawn as having "no source" after checking only `benchmark_history.json`;
  both were in `docs/learning-log.md`, from a manual session on a different
  clip. "Not comparable" and "unsourced" are different sentences.
- **Normal transcription stays threaded, not isolated**: batch/stream
  transcription runs in the shared `max_workers=1` executor with models
  preloaded (remote stream finalizes excepted — see the concurrent-mode
  entry above); faster-whisper (CTranslate2) and ONNX Runtime release the GIL
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
- **Show-overlay hotkey (preset, clearable)**: `show_overlay_hotkey`
  (default `Ctrl+Alt+F11`, schema 21) registers a third global hotkey
  (`DEFAULT_SHOW_OVERLAY_HOTKEY_ID`) whose only action is
  `controller.bring_overlay_to_front` — the same reveal as the tray "Show
  overlay" action, e.g. to check the last transcript on a floating overlay.
  Optional hotkeys use `_normalize_optional_hotkey`: an empty stored value is
  a deliberate disable and must stay empty (saving never substitutes the
  default combo back); only invalid non-empty values fall back to the
  default. Schema-20 files briefly stored "" for "never configured", so a
  `< 21` empty value migrates to the default once. The Save flow validates
  the combo and rejects conflicts with the recording and cancel hotkeys;
  registration mirrors the cancel hotkey (disabled -> unregister, failure ->
  notice + Error idle state) and is included in the resume-path
  `refresh_hotkey_registration`.
- **Re-paste last transcript**: `controller.repaste_last_transcript` pastes
  `_last_transcript` into the currently focused window through the normal
  `_insert_text_at_target` path (paste mode, clipboard semantics, modifier
  release wait), reachable via the tray action "Insert last transcript
  again" and the optional fourth global hotkey `repaste_hotkey` (default
  empty — a global paste combo is riskier than an overlay reveal, so nothing
  is preset). It is blocked while a recording/stream is active so a paste can
  never interfere with a capture, and it never writes a new history entry.
  Save-time validation rejects conflicts with the recording, cancel, and
  overlay hotkeys.
- **Completion tone (`completion_beep_enabled` + `completion_beep_tone`,
  default off/chime)**: after a successful transcript insertion (foreground
  batch, queued background insert, re-paste) the controller plays the
  configured tone via the shared `_play_tone` table on a short-lived worker
  thread (winsound is synchronous; only the recording-start beep stays
  deliberately synchronous so the microphone cannot record it). Streaming
  appends are many small pastes and stay silent by design. History-only
  delivery and failed inserts never beep.
- **The tray icon is registered by hand on Windows (`win_tray_icon.py`)**:
  `QSystemTrayIcon`'s menu closed Windows 11's "hidden icons" flyout while
  other apps in the same flyout kept it open. Everything observable at menu
  time was measured and refuted — Qt's `SetForegroundWindow`, the menu taking
  the foreground (the reference app's does too), window styles and owners
  (identical), and activating our icon window first (`accepted=True`, flyout
  still closed). Two experiments then isolated it: a hand-registered icon keeps
  the flyout open, and of two such icons differing only in their menu, only the
  one with a native `TrackPopupMenu` does. **Both the registration and the menu
  must be native**; do not "simplify" either half back to Qt.
  `WindowsTrayIcon` mirrors the `QSystemTrayIcon` API this app uses
  (`activated` with the same `ActivationReason` values, `showMessage`, `show`,
  `setContextMenu`, `setToolTip`), so callers do not branch, and
  `create_tray_icon` falls back to `QSystemTrayIcon` on other platforms and on
  any Win32 failure. The context menu stays a `QMenu` — it is the model
  (labels, order, enabled state, callbacks) and is only *rendered* natively.
  Menu width is `longest label + 70 px`: that 70 px is Windows' own padding and
  is constant for any content (measured across five label sets), so the labels
  are the only lever — `MNS_NOCHECK` reclaims the check-mark column while no
  entry is checkable (233 -> 205 px), and dropping the redundant "last" from
  three labels took it to 184 px. Entries report their checkable state, so
  adding a checkable action brings the column back instead of losing its check
  mark. Anything narrower would need owner-drawn items, which means drawing
  hover/disabled/dark-mode states by hand — not worth it.
  Invariants that Qt used to provide and that this module must keep: the window
  class is registered once per process with a dispatcher that routes by HWND (a
  per-instance window procedure dangles as soon as one instance is collected),
  every `ctypes` call declares `argtypes`/`restype` (defaults truncate handles
  and overflow on a large `LPARAM`), the icon is re-added on `TaskbarCreated`
  after an Explorer restart, and it is deleted before its window is destroyed
  or a dead icon lingers in the tray. `scripts/diagnose_tray_flyout.py` and
  `scripts/experiment_native_tray_icon.py` reproduce the measurements.
- **Tray left-click reveals the overlay**: a single left click (`Trigger`) has
  no other meaning and there is no main window, so it calls
  `controller.bring_overlay_to_front`. Together with the overlay's Record
  button this is the keyboard-free path to dictation; double-click still opens
  Settings.
- **Tray middle-click toggle (`tray_middle_click_toggle`, default on)**:
  middle-clicking the tray icon calls `controller.toggle_recording`, exactly
  like the recording hotkey; double-click keeps opening Settings. The guard
  reads `controller.settings` at click time so the Display-tab checkbox takes
  effect without restart.
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
  on one desktop. Use `-o faulthandler_timeout=<secs>` to get thread tracebacks
  if a single test hangs.
- Do **not** substitute `QT_QPA_PLATFORM=offscreen` for the commands above. It
  shifts widget metrics by 1-4 px and makes the two pixel-exact layout tests
  (`test_overlay_record_button_indicator_stays_centered_in_both_states`,
  `test_bottom_status_does_not_move_the_save_and_close_buttons`) fail. Failures
  from an offscreen run are artifacts, not repository problems.
- Two autouse fixtures in `tests/conftest.py` make desktop side effects
  impossible: `_forbid_handing_paths_to_the_desktop_shell` blocks
  `QProcess.startDetached` and `QDesktopServices.openUrl`, and
  `_forbid_blocking_modal_dialogs` blocks the `QMessageBox` statics and the
  `QFileDialog` getters. Both raise a named `AssertionError` rather than
  no-opping: an unstubbed modal dialog does not fail a run, it hangs it forever
  with no output naming the cause. A test that legitimately drives one of these
  paths patches it and asserts on the call, which every current one does — so
  these fixtures are preventive and currently catch nothing.

## Known limitations

- **A signal landing between "the resource is held" and "the flag says so"
  can strand it, and this is accepted rather than fixed.** Several places set
  a bookkeeping flag on the statement after the acquiring call returns --
  `incremented = True` in both branches of `_acquire_transcriber_runtime`,
  `acquired = True` in `_download_local_model_in_subprocess`, and the
  `try:` that follows `coordinator.acquire(...)` in
  `_download_model_for_preload` and `run_coordinated_download`. An exception
  delivered *between* those two statements leaves the resource held with the
  cleanup arm believing it was not. CPython only delivers signals to the main
  thread between bytecodes, and a windowed run has no SIGINT source at all, so
  the window is a `KeyboardInterrupt` in a console-launched process. It is
  recorded rather than closed because every plausible fix restructures locking
  on paths where restructuring has itself introduced defects three rounds
  running, and the same window reappears one statement further in whatever the
  new shape is. The one case that *was* closed is the cross-process download
  lock, because there the loser is every other process on the machine, not
  just this one.
- **`_teardown_pending_stream_connect` swallows a `KeyboardInterrupt`.** Its
  guard is `except BaseException` by design -- it is best-effort cleanup on an
  error path that still has a lease to release and an overlay to update -- so
  a Ctrl+C landing inside `thread.join()` or the provider call is logged and
  dropped. Only reachable in a console-launched process.

- **Cancel reaches a download only while it is *waiting* for the slot, not
  while it is transferring.** `run_coordinated_download` passes `cancel_check`
  into `acquire()`, so with the slot free -- the ordinary single-user case --
  the check is polled zero times and the transfer runs to completion.
  `snapshot_download` exposes no progress or cancel callback, and the
  ModelScope fallback reads its response in a plain loop with no poll. The
  visible consequence: with `keep_onnx_model_loaded` off, a Cohere/Granite
  model's only download is the one its transcriber starts from its own load
  path, and pressing Cancel during a multi-gigabyte fetch does nothing while
  that job holds the single `max_workers=1` transcription worker. The Local
  tab's own queue is unaffected -- it downloads in a child process and Cancel
  kills it. Closing this properly means routing the transcriber's load-path
  download through that same worker process; a poll inside the mirror loop
  would only cover the fallback and would make Cancel look like it works.
- Streaming: inserted text is append-only and never rewritten. A focus
  change suspends live insertion rather than aborting the session; the
  detection is best-effort polling, so a very brief switch can be missed.
- The post-pause append gate is an energy measurement, not a real VAD. A
  sustained non-speech sound after a long pause (a ~400 ms cough or chair
  scrape, or a heavy desk knock) can still authorise appending one
  hallucinated window, and a word whose longest voiced piece is under 80 ms
  is dropped. **The gate blocks silence, and nothing else.** Digital silence
  and room tone measure 0.000 s, which is the case that once grew a
  transcript to 896 invented words; a single key clack measures exactly the
  cut and typing above ~130 wpm reports seconds of "speech" because the decay
  tails bridge the gaps. "Bitte." (0.085 s) and a key clack (0.080 s) are 0.005 s
  apart, so no threshold separates them. Everything louder than
  silence passes and is bounded by `protected_prefix` rather than prevented.
- **The whole pause mechanism is inert in a room above the silence gate.**
  With a noise floor over `silence_gate_threshold` no slice is ever quiet,
  so `silent_seconds` never accumulates, `new_segment` never fires and
  `segment_floor` is never set — the protection is simply absent, and an
  unalignable window destroys the whole transcript again. A fan, an open
  window, or Windows microphone boost is enough. Nothing in the UI shows it,
  so the transcriber logs `streaming_noise_floor_above_gate` once per
  session after 20 s of unbroken above-gate audio. The condition is rolling,
  so a fan that starts mid-dictation is reported; a latching flag never
  reported it, because every session begins with a moment of silence. It
  cannot tell a loud room from 20 s of speech without a pause, which the
  message says.
- **Every number behind that gate is synthetic.** `samples/benchmark_sample.wav`
  is generated by `scripts/generate_sample_audio.py` and contains sine tones,
  not speech, so the repository has no recorded audio to calibrate against.
  A threshold was once derived from it and read back the generator's own
  parameters as if they were speech statistics. Separating a knock from a
  short word needs spectral features (a real VAD); until then, do not move
  the threshold on synthetic evidence alone.
- ARM CPUs: not supported (CTranslate2 requires x86 AVX/SSE).
- Clipboard restore: Unicode text only.
- The NVIDIA *NeMo* runtime remains intentionally unimplemented. Parakeet itself
  ships through the pure-Python onnx-asr path and Nemotron through ONNX Runtime
  GenAI, so no NeMo/PyTorch stack is needed. See
  `docs/local-asr-model-candidates-2026.md` for rationale.
