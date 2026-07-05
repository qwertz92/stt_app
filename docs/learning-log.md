# Learning Log

Project history, decisions, and operational learnings. Referenced by `AGENTS.md`.
Agents and developers: use this as a knowledge base for past issues and solutions.

## 2026-07-05

- **Canceling the newest queued job dropped earlier finished transcripts.**
  With several recordings pending and insert mode, a transcript that finished
  while a newer recording was still live is deferred behind the blocking
  session (`_deferred_background_results`). Canceling the newest/foreground job
  from the overlay queue row (or Clear queue) went through
  `cancel_queued_transcription` → `_request_job_stop`, which clears
  `_active_request_token` (a blocking condition) but — unlike
  `cancel_current_action` — never flushed the deferred inserts. Result: nothing
  was inserted at all, not even the earlier recordings that had completed and
  should have been pasted. `cancel_queued_transcription` now flushes deferred
  background inserts after the stop; the flush no-ops while anything is still
  blocking, so Clear queue still drops each deferred job to history via its own
  per-row cancel (order-independent because `_jobs` is insertion-ordered and
  every deferred job is canceled in the loop). Regression test:
  `test_cancel_newest_queued_flushes_earlier_deferred_insert`.
- **Overlay now surfaces on the hotkey stop, not only after the transcript.**
  A floating overlay could sit behind other windows, so pressing the hotkey to
  stop gave no visible feedback until the transcript finished — masking the case
  where the stop was fumbled and the recording actually kept running.
  `stop_recording` now reveals the overlay the moment the stop is processed
  (and a hotkey press during a pending streaming finalize reveals the
  "still finalizing" state too), mirroring the existing reveal on
  `start_recording`. The reveal is non-activating (`reveal_temporarily`:
  `WS_EX_NOACTIVATE` / `SWP_NOACTIVATE` / `MA_NOACTIVATE` /
  `WindowDoesNotAcceptFocus`) and the insertion path restores focus to the
  captured target window, so it never steals focus from the app receiving the
  paste. Regression test: `test_stop_recording_reveals_overlay_on_hotkey_press`.

## 2026-07-04

- **Benchmark no longer freezes the app (process isolation).** Running a
  benchmark loads faster-whisper/ONNX models back-to-back; the benchmark already
  ran in a background `threading.Thread`, yet the whole Qt UI still froze (no tab
  switching, no actions) because model loading does not release the Python GIL
  reliably. The benchmark now runs in a dedicated child process:
  `benchmark_worker.py` runs the pure `local_benchmark.run_benchmark_cases` and
  streams `progress`/`case`/`done` events as `@@STTBENCH@@`-prefixed JSON lines
  on stdout; `benchmark_process.py` launches it (source and frozen), translates
  the events back into the same `progress_callback`/`case_callback`, and returns
  the same `list[BenchmarkCase]`. The settings-dialog facade re-exports this
  under the name `run_benchmark_cases`, so the Qt code and the test seam are
  unchanged; the pure in-process function stays for the CLI and the worker.
  Cancel terminates the child process tree (`taskkill /T` on Windows) and raises
  `BenchmarkCancelled`, keeping already-streamed partial cases. A dedicated
  stderr pump avoids a full-pipe deadlock. Normal transcription was checked and
  intentionally left threaded (not isolated): models are preloaded and
  CTranslate2/ONNX release the GIL during inference, and the Cohere/Granite Node
  path is already its own subprocess, so dictation does not freeze the UI.
- **Overlay comes to the front after a result.** A floating (non-pinned) overlay
  is a tool window (not in Alt+Tab) and could hide behind other windows, so a
  finished transcript — or, worse, an insertion failure — could stay invisible
  with no easy way to see/copy it. The controller now reveals the overlay after
  a result: briefly on success (`OVERLAY_RESULT_REVEAL_MS`) and longer on
  errors/insertion failures (`OVERLAY_ERROR_REVEAL_MS`). A tray "Show overlay"
  action (`controller.bring_overlay_to_front`) is the manual escape hatch.
- **Settings dialog shows the app icon on the Windows taskbar.** Without an
  explicit AppUserModelID, Windows groups our windows under python.exe and shows
  its generic icon on the taskbar (most visibly for the Settings dialog).
  `main._set_windows_app_user_model_id` now sets a stable `APP_USER_MODEL_ID`
  before the first window is created.
- **Transcription queue scrolls and resets its size.** With the queue visible the
  overlay grew toward full screen height to render all rows, and after the queue
  emptied it could stay large when the final result was short (a regression of an
  old pre-queue bug). The queue rows now live in a scroll area, so the overlay
  grows only up to `OVERLAY_QUEUE_MAX_HEIGHT` (bounded by the screen) and scrolls
  beyond that, like long transcript text. Two subtleties: the rows are measured
  via the *layout* sizeHint (the widget sizeHint is inflated by the minimum
  height we set to keep the rows from being compressed by `widgetResizable`,
  which would be self-reinforcing), and `set_transcription_queue` re-asserts the
  size after the event loop drains (deferred `_refresh_size_after_queue_change`)
  because switching between very different queue sizes otherwise leaves a stale
  pending resize from the previous state.

## 2026-07-01

- **`settings_dialog.py` split from ~6.4k lines into a mixin facade.** The
  monolithic `SettingsDialog` god-class is now composed from per-tab mixins
  (`settings_dialog_general/local/benchmark/remote/history/import/persistence.py`)
  plus `settings_dialog_helpers.py` for shared widgets/constants/pure helpers.
  `settings_dialog.py` keeps the dialog lifecycle, shared-UI helpers, the Qt
  `Signal`s, and re-exports the module's public API. Method bodies moved
  verbatim (same `self`), so behavior is unchanged — the full suite passes with
  only the one pre-existing offscreen width test failing. Two constraints drove
  the shape: Qt signals must stay on the `QObject`-derived class (mixins are
  plain classes and only touch `self.<signal>`), and the test suite monkeypatches
  ~40 names on `stt_app.settings_dialog`, so those names must remain resolvable
  there. Global patches (`threading.Thread`, `time.monotonic`,
  `TranscriptEditDialog.get_text`) survive the split because they mutate shared
  module/class objects; the six patched *function* bindings are reached through
  a lazy `_facade()` accessor in the local/benchmark mixins so the facade stays
  the resolution point without a module-scope import cycle (a mixin can be
  imported directly; `test_settings_dialog_modules.py` guards this). The split
  was done with an AST tool that
  asserts every one of the 203 methods lands in exactly one module, then `ruff`
  pruned the import supersets.
- **Canceling an active recording now flushes deferred background inserts.**
  A queued insert-mode transcript that finished while a newer recording was
  active is held in `_deferred_background_results` until the blocking session
  ends. `start_recording`/`stop_recording` already flushed on completion, but
  `cancel_current_action` did not: canceling the blocking recording (or the
  active transcription) left the completed transcript pending in the queue
  overlay until some later, unrelated recording. Both cancel branches now call
  `_flush_deferred_background_results()` so the transcript is delivered as soon
  as nothing is blocking it. The transcript was always safe in history; this
  only fixes the delayed paste.
- **Settings reloads defer closing an in-use transcriber runtime.** A non-modal
  settings Save runs `reload_settings` on the Qt thread even while a batch
  worker or a live stream still holds the cached transcriber. Unconditionally
  closing it there could break that in-flight run — a keep-loaded ONNX
  subprocess shares one stdin with the worker (its `close()` does not take the
  batch lock), and a live Nemotron stream would be torn down mid-utterance.
  faster-whisper (the default) has no `close()`, so it was only a reference
  drop, but the advanced local engines were exposed. `reload_settings` now sets
  `_pending_transcriber_cache_reset` when `_transcription_runtime_active()`
  instead of closing immediately; `_get_or_create_transcriber` applies the
  deferred reset before building the next transcriber, once the serial worker
  has finished, so changed settings and API keys still take effect on the next
  run. Mirrors the existing resume-path guard and shares its condition via the
  new `_transcription_runtime_active()` helper.

## 2026-06-24

- **Queued background inserts stay visible until paste delivery completes.**
  A background transcription result that must wait for the active recording to
  stop now remains registered in `_jobs` with a "Pending insert" queue label.
  The row is removed only after the deferred paste flushes, so the overlay no
  longer hides a transcript that still has delivery work pending.
- **Deferred inserts now wait for the current transcription to finish.**
  Pending background inserts are no longer flushed immediately when the next
  recording stops while that recording's transcription is still running. The
  queue remains visible through Processing, then completed transcripts are
  delivered in token order once the current transcription resolves. Foreground
  failure/cancel paths also flush older pending inserts so they cannot hang.
- **Rapid hotkey toggles during recording startup are serialized.** If the
  recording hotkey arrives while `start_recording()` is still initializing the
  microphone, the controller queues the toggle and applies it after startup
  completes instead of re-entering `start_recording()`. This prevents nested
  captures and closes the gap where a WAV could be saved without a matching
  transcription worker submission.
- **Import Audio file picking no longer uses a blocking native dialog.** The
  Import Audio tab opens a non-modal Qt file dialog so global recording hotkeys
  can still be processed while the picker is open.
- **History timestamps are display-configurable.** History entries continue to
  be stored in UTC, but Settings now has a General > Display time selector that
  defaults to local time and can be switched to UTC for diagnostics.
- **Benchmark layout gives Run Benchmark room to breathe.** The Benchmark tab
  keeps a taller history list and reserves substantially more height for the
  Run Benchmark panel, especially when Run Options is expanded, instead of
  squeezing those controls under an oversized Results area.
- **Clipboard restore race hardened again after rare stale paste reports.**
  The previous 160 ms SendInput restore window remains unchanged; the stronger
  fix is to defer queued/background result insertion until the active recording
  has stopped when an old transcription result arrives during the next
  recording. The history entry is still saved immediately, but the paste is
  played back later in token order. This avoids pasting in fragile focus and
  clipboard handoff windows while rapid short recordings are being started and
  stopped.
- **ONNX/WebGPU GPU fallback is no longer sticky after sleep/resume.** Windows
  resume now closes cached Cohere/Granite ONNX/WebGPU runtimes so the next
  transcription recreates the graphics backend. If an `auto`/`gpu` ONNX runtime
  falls back to CPU during a request, the result is still returned, then the
  Node runtime is closed so the following request retries WebGPU/DirectML
  instead of staying on CPU until the app restarts. Transcription timing logs now
  include `runtime_device`, `gpu_available`, and fallback details for future
  diagnostics.
- **Clipboard contention now checks text after sequence-only changes.** Windows
  clipboard sequence bumps with the expected transcript still present no longer
  abort insertion as a false user-copy race.
- **Background queue insert failures no longer silently copy transcripts.** If a
  queued/background insertion fails while another recording is active, the
  transcript stays in history and the user's clipboard is left alone.
- **Queue rows now include rank and time.** In-flight rows show oldest/newest
  markers and a submission timestamp so multiple queued recordings are easier
  to distinguish before canceling one.

## 2026-06-22

- **Clipboard paste delivery is guarded against user-copy races.**
  `TextInserter` now serializes app-initiated paste operations and verifies the
  Win32 clipboard sequence/content after setting the transcript, before sending
  paste, and before restoring the previous clipboard. If the user changes the
  clipboard during that narrow SendInput window, the app leaves the user's new
  clipboard untouched and reports a contention error instead of fallback-copying
  the transcript over it.
- **Recording start snapshots the target before draining pending events.** A
  queued transcription result can arrive while `start_recording()` is painting
  the "Starting recording" state. The controller now captures the new target
  window/signature before that `processEvents()` window and restores it if an
  old background delivery briefly moves focus back to its own target.

## 2026-06-21

- **History refreshes now avoid full Qt rebuilds when possible:**
  - Transcript history views use the history file mtime/size as a cheap reload
    signature and return immediately when Refresh sees no storage change.
  - When new transcript entries were only appended, the newest-first History
    dialog and Settings History tab prepend just the new visible rows/items and
    keep existing Qt items, selection, and scroll state instead of clearing and
    rebuilding the whole view.
  - Follow-up: refreshes now reconcile inserts, deletes, limit trims/expansions,
    row replacements, and in-place text edits through one shared diff plan.
    Selection/current-row restore is mapped through that diff instead of relying
    on non-unique timestamps, so same-second entries remain distinct.
- **History multi-select copy now pastes oldest selected transcript first:**
  - Both the overlay History dialog and the Settings History tab still display
    recent entries newest-first, but `Copy selected` reverses the selected
    recent entries before joining them so pasted text is chronological.
  - Batch transcription queue delivery remains serialized through the single
    controller transcription executor; local and remote jobs are not currently
    uploaded or transcribed in parallel.

## 2026-06-19

- **Release and dialog polish after 0.4.1.** The Windows release workflow now
  writes release notes with a literal PowerShell here-string so Markdown
  backticks around asset names survive GitHub Actions. The published History
  dialog default size was increased and the native maximize button restored.
  The Remote settings provider grid now keeps provider labels, key fields,
  Azure Endpoint, Clear buttons, and status badges on shared columns with
  fixed status-badge widths; "Last test" rows reserve the same bottom padding
  before and after a connection test. General-tab form labels now share a
  measured minimum width, and Settings tab selection no longer changes text
  weight, preventing one-pixel tab jitter while preserving a visible selected
  state.
- **Settings follow-up polish after the first post-release pass.** The Benchmark
  Results box now uses a vertical splitter between the table and summary and
  gets more vertical stretch inside the tab. Settings Save now detects true
  no-op saves and avoids emitting `settings_changed`, preventing unnecessary
  controller reloads/model preloads. Remote provider connection test results are
  persisted in a separate diagnostic JSON store and restored when Settings is
  reopened. Saving a replacement key or deleting a provider key now invalidates
  that provider's saved connection-test result so the Remote tab cannot show a
  stale "OK" for a missing or changed credential.
- **GitHub Releases update checks were added without an updater framework.** The
  app now has a Settings and tray "Check for updates" action backed by
  `update_checker.py`, and startup schedules one delayed background check that
  only notifies through the tray when a newer release exists. This deliberately
  stops at discovery/opening the release page; automatic installer download and
  execution remains out of scope until it is reviewed separately.
- **Benchmark tab layout now prioritizes reading results.** The Benchmark tab
  order is History, Results, then Run Benchmark. Results and the run controls are
  separated by a vertical splitter, Run Options start collapsed, and the Results
  table scrolls per pixel horizontally and vertically instead of jumping by
  table item.
- **Settings and overlay UI polish before 0.4.1.** The Settings dialog now opens
  larger by default, keeps one stable size while switching tabs, and ignores
  scroll-area size-hint changes that previously caused small resize jitter. The
  Remote API key rows use a compact grid with calculated status-badge widths,
  and inline field buttons share the corresponding input height. Local ONNX model
  labels now show consistent precision tags (`Q4`, `INT8`, `INT4`), while the
  red Local runtime note is shorter and sits directly under the model selector.
  The overlay queue's per-item cancel action now uses a visible `Cancel` label
  instead of a symbol that could render ambiguously.
- **Fixed two queue/history UI regressions before cutting another release.**
  The standalone History dialog and Settings History size spin boxes now disable
  keyboard tracking, so typing an increased limit (for example `224` → `300`)
  does not apply the temporary `3` and show a trim-confirmation dialog. The
  overlay transcription queue now renders every in-flight row, recomputes its
  layout before measuring height, can temporarily grow beyond the normal
  transcript-text height cap when the queue needs room, and returns to the normal
  non-queue size as soon as the queue is empty.
- **Review hardening for queued transcription progress.** Progress events now
  use the same foreground-job check as ready/failed results, so a background or
  aborting transcription cannot switch the overlay back to Processing while a
  newer recording owns the live UI or after the user canceled the job.
- **Standalone History now matches Settings History.** The overlay History
  dialog now supports multi-select copy/delete with single-entry-only editing.
  Its first load can be deferred until after the window is shown, and repeated
  History clicks now present the existing window instead of stacking reloads.
- **Settings import and history refresh polish.** The Import Audio tab now has a
  copy button for transcription results and uses a vertical splitter so long
  transcripts can take more space without hiding provider controls. Dialog copy
  buttons reserve enough width for both normal and "Copied" states, and shared
  button feedback styling gives hover/pressed/copy states clearer visual
  feedback. Refreshes for Settings History, local model lists, benchmark model
  lists, and the standalone History dialog now preserve selection, current item,
  and scroll position when the same entry still exists.
- **Transcription queue branch was never merged into main.** Ported the queue
  implementation from `claude/transcription-queue-history`: Settings now expose
  `concurrent_transcription_mode` (`insert` default, `history`, `cancel`), the
  overlay renders in-flight transcription jobs with per-item cancel and Clear
  queue, and the controller tracks each recording as a `_TranscriptionJob` with
  captured target window/signature. A finished transcription is never discarded:
  background results are inserted into their captured target or saved to history
  depending on mode. Cancel requests cooperative stop where supported; local
  faster-whisper polls `set_cancel_check` between segments and raises
  `TranscriptionCanceled`, while engines without a cancel hook may still finish
  and are then kept in history.
- **Settings History multi-select was also still only on the queue branch.**
  Ported the desired History-tab multi-select behavior: multiple selected
  transcripts can be copied as blank-line-separated text, deleted together after
  one confirmation, and editing remains limited to a single selected entry.
- **Granite Speech 4.1 NAR was completely broken in the app — root-caused and
  fixed.** NAR emitted token-garbage at **every** precision, including the shipped
  INT8. The bug was host-side in `webgpu_asr_runner.mjs` (`ctcDraftTokenIds`): the
  encoder's BPE/CTC head emits **100353** classes (vocab 100352 **+1**) with the
  **blank prepended at index 0**, and non-blank class `c` maps to LLM token `c−1`.
  The app stripped the wrong blank (`100257`, the LLM eos), skipped the `−1`
  offset, and did a non-reference decode→re-encode round-trip that corrupted the
  editor's `[blank, t0, blank, t1, …]` slots. Fix: argmax→collapse→drop blank
  `0`→subtract `1`→feed ids directly. Verified: English verbatim-correct, German
  good (CPU). Lesson: gate the baseline through the real pipeline before trusting
  it — INT8 was "shipped" but never end-to-end verified. Note
  `config.blank_token_id=100257` is the *editor/slot* blank, NOT the *CTC* blank
  (`0`) in smcleod's ONNX export.
- **Self-converted a q4 (INT4) NAR build; not worth shipping on current hardware.**
  New `scripts/convert_granite_nar_q4.py` re-quantises smcleod's FP32 editor to
  4-bit `MatMulNBits` (HQQ default, RTN fallback), keeping encoder INT8 (a q4
  encoder is *larger*: Convs stay FP32) and embed_tokens fp16w. Vs INT8 on a
  7600X: q4 is **slower on CPU** (RTF 0.62–0.70 vs 0.53), only **~9–16 % smaller**
  (not half), quality comparable. q4 is a GPU/bandwidth optimisation; on a VNNI
  CPU, native INT8 GEMM beats q4's dequant overhead. **INT8 stays the NAR default.**
- **NAR has no working GPU path here (separate from q4).** DirectML fails at the
  conformer encoder's first attention (5-D batched MatMul unsupported by the DML
  EP) — identically for INT8 and q4. WebGPU has the Einsum bug. AR models run on
  GPU via the Transformers.js WebGPU pipeline, not this raw `onnxruntime-node`
  conformer path — so it's the encoder ops, not autoregression.
- **GPU benchmarked (Arc A750):** the q4 editor runs on DirectML (~2–3× faster
  than INT8-CPU *in isolation* at N≥256), but the conformer encoder is ~90 % of the
  runtime and is CPU-locked, so GPU gives **no end-to-end win** (slightly slower);
  q4-HQQ is broken on DirectML (use RTN). Making NAR GPU-fast needs an encoder
  re-export — separate R&D, not part of the q4 publication pass. A 2026-06-24
  graph check found 32 high-rank attention `MatMul` nodes plus 16 `Einsum` nodes,
  so this is a repeated conformer-layer export problem, not a one-node patch.
  Full write-up + HF-card source: `docs/granite-speech-4.1-nar-q4.md`. Plan:
  publish the RTN q4 artifact to HF (`qwertz92`) with a prominent
  INT8-preferred warning; keep HQQ local/documented because DirectML breaks it.
- **SSL CA bundle validation should reject existing-but-invalid files.** The SSL
  env sync helper previously treated any existing file as a usable CA bundle.
  Test placeholders such as `cert` could leak through `REQUESTS_CA_BUNDLE`, then
  provider tests failed while creating `ssl.SSLContext` before mocked network
  calls. Centralized validation now loads the bundle with `ssl.create_default_context`;
  nonexistent or unparsable bundles are ignored/removed, and tests use real PEMs
  when they expect a valid bundle.

## 2026-06-18

- **Standalone History dialog matched Settings History resizing.** The overlay
  History button's dialog now uses a vertical splitter between the transcript
  table and selected text detail, just like the Settings History tab. The import
  file picker starts in the active transcript-history store directory instead of
  an empty/default folder. Limit changes now update the count label with the
  configured limit, avoid rebuilding the table when the visible row count would
  not change, and keep full transcript text out of table cells by rendering only
  a preview there; the detail pane remains the full text source.
- **Tightened the 0.4 release path and final docs drift.** Clarified that the
  current q4 `~2 GB` explanation applies to 1B/2B-class speech models, not every
  possible quantized model size. Fixed stale Granite 4.1 wording in
  `scripts/download_model.py`, the Settings streaming tooltip, and `models.md`
  so DirectML fallback is described consistently with `webgpu_asr_runner.mjs`.
  GitHub release notes now explain the installer vs portable ZIP and warn that
  GitHub's automatic source archives are developer snapshots, not app builds.
  `scripts/create_release.py` now proposes the already-bumped current project
  version when it is newer than the latest release tag and can tag that state
  without creating a dummy release-metadata commit.

## 2026-06-17

- **Documentation refresh across the docs set.** Reframed the model docs so the
  GPU/ONNX models (Cohere, Granite, Nemotron) read as first-class, recommended
  options instead of Whisper-centric afterthoughts: README and `models.md` lead
  with them, cite the Open ASR Leaderboard (Granite 4.1 2B is #1) and the Arc
  A750 benchmark RTF numbers, and explain real-time factor. Removed leftover
  "experimental" wording for the shipped local models (kept only for streaming
  and the AGENTS policy note), described what the WebGPU `Einsum` shader bug is,
  and brought the Cohere / Granite / local-candidates evaluation docs to the
  current state. Standard: describe what each model is now, consistently, without
  status labels that other models don't get.
- **Added Alibaba Fun-ASR as a remote batch provider (`funasr`).** Decided to
  implement the hosted path after all: the app is general-purpose, and Fun-ASR
  adds SOTA accuracy for Chinese (incl. dialects) and East/SE-Asian languages
  the other engines don't cover as well. Key facts: Fun-ASR's hosted preview
  tops the Artificial Analysis leaderboard (~1.7% WER), but it supports **31
  languages and NOT German** (so `FUNASR_LANGUAGE_MODES` excludes `de`). The
  batch "recording file recognition" API requires a public OSS URL (rejects
  local files/base64), so the provider drives the **realtime WebSocket API in a
  batch fashion** (`funasr_provider.py`): `run-task` → stream PCM → `finish-task`
  → collect `result-generated` sentences → `task-finished`. Key-only (Singapore
  `wss://dashscope-intl.aliyuncs.com`), batch mode only. Local weights NOT
  implemented (7.7B too big; 0.8B nano has no ONNX export + different runtime +
  no German). Tests mock the WebSocket (`tests/test_funasr_provider.py`). Updated
  `docs/funasr-and-fleurs-evaluation.md` from "deferred" to "implemented".
- **FLEURS is a benchmark, not a model.** Clarified that it cannot be
  implemented as a transcription engine; "leads on FLEURS" is a property of a
  model measured against the FLEURS test set. See
  `docs/funasr-and-fleurs-evaluation.md`.
- **Added Azure LLM Speech (MAI-Transcribe) as a remote batch provider.**
  Research finding first: the Azure "LLM Speech" / "Speech 05 2026" model is a
  **remote, cloud-only** service (Microsoft Foundry), not a local/ONNX model.
  Enhanced mode is backed by the Microsoft AI (MAI) team's `mai-transcribe-1.5`
  / `mai-transcribe-1` models. Microsoft does **not** publish the parameter
  count. Pricing is ~$0.36/hour pay-as-you-go with a Free (F0) tier of 5 audio
  hours/month (hard cap). Quality: 2.4% WER on Artificial Analysis (#3 there)
  and best-in-class FLEURS multilingual; it is *not* the current #1 on the
  Hugging Face Open ASR Leaderboard (that is led by open models). The model is
  in public preview (no SLA). Because it is cloud-only, the "run via ONNX
  runtime" option does not apply.
  - Implemented `transcriber/azure_provider.py` as a batch-only REST provider on
    the `:transcribe` fast-transcription endpoint with `enhancedMode` enabled,
    mirroring the ElevenLabs/Deepgram pattern (urllib + shared `_http_utils`
    multipart helper). It posts `audio` + a `definition` JSON and reads
    `combinedPhrases[].text`.
  - Unlike every other provider, Azure needs **two** inputs: the resource key
    (stored in the secret store under `azure`) *and* a per-resource endpoint.
    Added a dedicated, non-secret `azure_endpoint` setting plus a text field in
    the Settings "Remote Provider API Keys" box. `normalize_azure_endpoint`
    accepts a full URL, bare host, or resource name.
  - Connection test posts a tiny in-memory silent WAV to validate
    endpoint + key + region support without needing a list endpoint.
  - Wiring: `config.py` (engine, models, API version, 42/24-language maps,
    `nb` locale override for app code `no`), `settings_store.py`
    (`has_azure_key`, `azure_speech_model`, `azure_endpoint`; schema 16 -> 17),
    `factory.py`, `controller.py` (model-name display + transcriber cache key),
    and `settings_dialog.py` (engine/import combos, model selector, language
    hints, connection target, key states, settings build). Tests in
    `tests/test_azure_provider.py`; updated `tests/test_factory.py` and
    `tests/test_settings_dialog_connection.py` which had encoded "azure not
    implemented". Costs/quality captured in `docs/provider-costs.md`.
  - Validation: `QT_QPA_PLATFORM=offscreen uv run --extra dev pytest -q` (all
    green) and `uv run --extra dev ruff check` (clean).
- **Granite Speech 4.1 2B moved to the q4 WebGPU pipeline path.** A faithful q4
  Transformers.js package now exists at
  `onnx-community/granite-speech-4.1-2b-ONNX` (created 2026-05-13), in the exact
  Granite 4.0 layout (`audio_encoder`/`embed_tokens`/`decoder_model_merged` q4).
  This supersedes the 2026-06-16 note below that "none exists yet": that check ran
  in a sandbox without Hugging Face access and used web search only. The base 2B
  config is dimension-for-dimension identical to Granite 4.0, so it loads through
  the same `GraniteSpeechForConditionalGeneration` pipeline.
  - Verified on the Windows / Intel Arc A750 dev machine: loads on **WebGPU**
    with no `Einsum` shader crash, transcribes German, English, and French
    correctly, ~0.13–0.19 real-time factor — materially faster than the raw CPU
    path. German was spot-checked with a Windows SAPI (Hedda) TTS clip.
  - Code: `config.py` points `granite-speech-4.1-2b` at the onnx-community repo,
    precision `q4`, label `ONNX/WebGPU q4`, size ~1.84 GB; `local_webgpu_asr.py`
    adds `_GRANITE_4_1_AR_Q4_LAYOUT` (reuses the 4.0 q4 required-file set);
    `webgpu_asr_runner.mjs` adds a `GRANITE_PIPELINE_MODELS` set so 2B routes
    through the same branch as Granite 4.0. Tests updated in
    `tests/test_local_webgpu_asr.py`.
- **Plus and NAR deliberately NOT moved to the pipeline path.** Investigated and
  documented in the new `docs/granite-speech-4.1-onnx-variants.md`. Summary:
  Plus is `granite_speech_plus` (distinct projector that consumes intermediate
  encoder hidden states, plus speaker/timestamp features); the only public q4
  build (valoomba) is a base-architecture mis-export and produces broken English
  (`<unk>` spam / empty), Transformers.js has no `granite_speech_plus` class, and
  optimum has no `granite_speech`/`granite_speech_plus` ONNX export config. NAR is
  `granite_speech_nar` (non-autoregressive; no JS class, no q4). Both stay on the
  raw INT8 `onnxruntime-node` path. The doc records exactly what would have to
  change to enable them and the scope of a custom conversion.
- **Added `docs/local-onnx-q4-conversion.md`** — a neutral, user-friendly
  explainer of ONNX export + q4 quantization (what q4 is, q4 vs int4, why
  downloads are ~2 GB, why the conversion is deterministic so re-converting adds
  no value), with a glossary. `models.md` and `local-onnx-runtime.md` updated for
  the 2B q4/WebGPU status.

## 2026-06-16

- Re-checked HuggingFace for a Transformers.js-packaged Granite Speech 4.1
  export (q4/ONNX-web layout like `onnx-community/granite-4.0-1b-ONNX-web`).
  None exists yet: only raw multi-graph INT8 community exports (`smcleod/*`)
  and an `onnx-internal-testing/tiny-random-GraniteSpeechForConditionalGeneration`
  CI fixture. `GraniteSpeechForConditionalGeneration` is supported by
  Transformers.js (the app already uses it for Granite 4.0), so a proper q4
  ONNX-web export of 4.1 should load through the same pipeline path and is the
  cleaner GPU route than the hand-written raw-graph runtime. Producing it needs
  an Optimum/Transformers.js ONNX export + quantization run on a machine that
  can load the 2B model; it cannot be done in this repo's sandbox (HF is not in
  the network allowlist, no GPU).
- Reconciled the docs with the lifted DirectML block: `models.md` and
  `local-onnx-runtime.md` now state honestly that Granite 4.1 GPU is
  unverified and often still runs on CPU (WebGPU `Einsum` shader bug, DirectML
  operator gaps), rather than implying GPU acceleration works.

## 2026-06-15

- Bumped to 0.4.0 (minor): the work since 0.3.1 includes new features (opt-in
  streaming finalize, app icon, larger settings dialog, DirectML GPU path for
  Granite 4.1) beyond pure bugfixes, so a minor bump fits the project's 0.x
  scheme. The 0.3.2 metadata commit was superseded; tag v0.4.0 from a normal
  clone (this environment cannot push tags).
- Granite 4.1 raw ONNX graphs can now use the GPU: `onnxruntime-node` ships the
  DirectML execution provider on Windows, so `ortExecutionProviders` returns
  `dml` instead of throwing, and auto/gpu mode tries WebGPU -> DirectML -> CPU.
  This only affects the raw-graph Granite 4.1 path, not the Cohere/Granite 4.0
  Transformers.js pipeline. Needs verification on real Windows GPU hardware.
- No public q4/int4 ONNX export for Granite Speech 4.1 was found (HF is not in
  this environment's network allowlist, so the check used web search only;
  community ONNX repos still ship INT8 as the smallest tier). Granite 4.0 has
  q4; Granite 4.1 stays INT8 until a verified q4/int4 export appears.
- Removed "experimental" framing from the local ONNX models (Cohere/Granite)
  in UI labels and user-facing model docs; they are supported daily-use models.
  Streaming mode keeps its experimental label.

## 2026-06-11

- Released v0.3.2 with the streaming provider fixes. Note: the remote
  execution environment used for the work could push branches but not tags,
  so the `v0.3.2` tag itself must be created and pushed from a normal clone.
- Stopping a local faster-whisper streaming session no longer re-transcribes
  the whole recording by default. The full final pass is now the opt-in
  `streaming_full_final_transcript` setting; the fast path merges a trailing
  window transcription into the provider-tracked live transcript by word
  overlap.
- Saving settings moved a manually dragged overlay back to the configured
  corner because the save handler always called `move_to_corner`. The new
  `OverlayUI.apply_corner_setting` repositions only when the corner setting
  actually changed.
- The app got a custom microphone icon generated by a committed QPainter
  script (`scripts/generate_app_icon.py`); it replaces the Qt standard tray
  icon and is wired into the wheel, PyInstaller EXE/bundle, and installer.
- The initial settings dialog size grew from 680x720 to 680x860; it is still
  bounded to the available screen geometry.

## 2026-06-10

- Deepgram streaming with the default auto language never connected: the
  live WebSocket API rejects `detect_language` (HTTP 400 during the
  handshake), unlike the pre-recorded API. Streaming auto now sends
  `language=multi`, which nova-2 and nova-3 support for live multilingual
  code-switching; batch keeps `detect_language=true`.
- Deepgram streaming previously called `ws.send` directly from the PortAudio
  callback thread. Blocking socket writes there can stall real-time capture,
  so chunks are now queued and sent by a dedicated sender thread that drains
  before Finalize on stop and reports send failures via the error callback.
- AssemblyAI retired the legacy v2 realtime API (`RealtimeTranscriber`);
  sessions fail with a model-deprecated error. Streaming now uses the
  Universal-Streaming v3 `StreamingClient` with the
  `universal-streaming-multilingual` model, language detection, and formatted
  turns. Transcript text is keyed by `turn_order` because the formatted
  end-of-turn transcript arrives as a second event for the same turn. SDK
  `disconnect` joins are bounded by a helper thread because they can hang on
  dead connections.
- Local-tab settings tests that select models after `qWait(250)` were flaky
  under full-suite load because the verified inventory scan had not flagged
  items as cached yet; they now poll for the expected models and cached
  state with a bounded helper.

## 2026-06-09

- Download transfer rates now use a short rolling cache-growth window instead
  of one poll interval, so bursty Hugging Face writes no longer make the UI
  flash immediately between a real rate and `0.0`.
- Settings and startup model downloads use a cancellable worker process that
  works in source and packaged runs. Canceling clears queued downloads and
  removes unusable `*.incomplete` files while preserving completed files for a
  later retry. The command-line downloader applies the same cleanup on
  `Ctrl+C`.
- The non-`uv` Windows requirements are checked against `pyproject.toml` and
  now include the Nemotron `onnxruntime-genai` runtime.
- Background model scan/download workers no longer emit Qt signals after a
  Settings dialog has already been deleted.
- The Windows release workflow uses Node.js 24-compatible major versions of
  checkout, setup-python, setup-uv, and upload-artifact after GitHub announced
  that hosted runners will force Node.js 24 for actions on 2026-06-16.
  setup-uv v8.1.0 is pinned to its published commit because its moving `v8`
  major tag is not currently resolvable by GitHub Actions.
- Local benchmark routing now uses `LOCAL_MODEL_RUNTIME` instead of treating
  every non-WebGPU model as faster-whisper. This prevents a newly added local
  runtime from reaching `WhisperModel` and failing with an invalid model-size
  error. A running app must still be restarted after its source files change.
- ONNX benchmark cases now retain concise provider fallback reasons in
  summaries, history, CLI output, and exports. CPU results therefore explain
  which WebGPU or DirectML attempt failed.
- A real Intel Arc A750 benchmark showed that Granite Speech 4.1 INT8 can load
  on WebGPU but fails on its first inference because ONNX Runtime Web cannot
  create the `Einsum` shader pipeline. Granite 4.0 q4 remains functional on
  WebGPU because it uses a different graph/runtime path. Granite 4.1 `auto`
  correctly falls back to CPU because DirectML is not exposed for its raw
  `onnxruntime-node` graph sessions.
- Nemotron benchmark routing was verified on current `main`: the repository
  sample ran on CPU at 0.224 RTF. DirectML fallback reported that the installed
  ORT GenAI package was not built with DML support.
- Benchmark system details now include app/source revision, GPU driver, Python
  ONNX Runtime variants, ORT GenAI provider capability, Transformers.js,
  Tokenizers.js, ONNX Runtime Node/Web, and detected CUDA driver/toolkit
  versions.

## 2026-06-08

- Overlay visibility and compact sizing were hardened. `Clear` restores the
  cached startup size again after the button event completes, every recording
  start re-presents the overlay regardless of pinned/floating mode, and Windows
  resume events reassert native z-order, visibility, screen bounds, and global
  hotkey registrations.
- Opening the recordings folder now schedules a global hotkey refresh. Explorer
  still becomes the foreground target, so recording works but text cannot be
  meaningfully inserted into the Explorer folder view.
- The embedded Settings History tab now uses a vertical splitter between the
  transcript list and selected transcript text while preserving the previous
  2:1 initial layout.
- General-tab language choices are rebuilt from centralized model-aware
  metadata. Whisper families expose the full Whisper language set; OpenAI,
  AssemblyAI, Deepgram, Cohere, and Granite use their documented subsets; Auto
  remains the default where the runtime supports it. ElevenLabs converts the
  app's canonical language codes to its documented Scribe codes.
- Groq now reuses its cached SDK/HTTP client instead of creating one for every
  transcription. Transcription workers log `transcription_timing` phase data so
  first-request delays can be separated into app initialization versus the
  provider/network request.
- A Granite Speech 4.1 2B Q4_K GGUF is now public, but it targets a separate
  CrispASR/GGUF runtime. The current Granite 4.1 ONNX repositories still expose
  INT8 as their smallest compatible graph tier, so the app remains on INT8.
- NVIDIA Nemotron 3.5 ASR Streaming 0.6B is selectable through its official
  multilingual INT4 ONNX Runtime GenAI export. Unlike faster-whisper rolling
  windows, it keeps cache-aware FastConformer/RNNT state and emits incremental
  tokens for each fixed 560 ms chunk.
- The Nemotron language list uses Microsoft's official prompt-ID mapping and
  exposes only transcription-ready and broad-coverage languages.
  Adaptation-ready languages remain hidden because the model card requires
  fine-tuning.
- The app ships the installable CPU ORT GenAI package and attempts DirectML
  before CPU. Microsoft's current DirectML GenAI package cannot yet be locked
  because its required `onnxruntime-directml>=1.26.0` wheel is unpublished.
- A real Ryzen 5 7600X run loaded Nemotron in 0.81 seconds and transcribed the
  repository benchmark sample at 0.229 RTF in automatic-language CPU mode.
- Local-model downloads started from Settings now use a serial, deduplicated
  queue. The model list remains selectable during an active download so more
  models can be queued, while cache refresh, deletion, and model-directory
  changes stay disabled until the queue finishes.
- Settings model downloads now show active/queued states plus an approximate
  progress bar and transfer rate in MB/s and Mbit/s. The shared progress helper
  also keeps startup preload reporting consistent; values are estimated from
  cache growth and `MODEL_ESTIMATED_SIZE_MB`.

## 2026-05-31

- Granite Speech 4.1 ONNX exports are selectable local models. The public 4.1
  exports currently provide INT8/fp16w/fp32 raw ONNX graph bundles rather than
  q4/int4 Transformers.js packages, so the app uses the INT8 tier by default
  and labels it separately from q4 Cohere/Granite 4.0.
- `local_webgpu_asr.py` now keeps layout-aware download and required-file
  metadata for selectable Cohere q4, Granite 4.0 q4, Granite 4.1 AR INT8, and
  Granite 4.1 NAR INT8. The Node helper has separate raw-ONNX runtime paths for
  4.1 AR and NAR because their graph contracts are different. Granite 4.0
  remains selectable as the smaller q4 Granite option.

## 2026-05-06

- Benchmark runs are now persisted separately from transcript history. The
  Settings Benchmark tab can load previous runs, export current or selected
  runs as CSV/XLSX, cancel a benchmark between measurable steps, and update the
  result table incrementally after each completed case.
- Benchmark summaries now include the benchmark context, including audio file,
  selected models, device targets, compute type, run count, beam size, language,
  VAD, warmup, thread count, model directory, and run status. This makes
  historical results comparable without relying on memory of the UI settings.
- Benchmark summaries and exports also include best-effort system context:
  OS, CPU, logical cores, memory, GPU names on Windows, Python, Node.js, and
  local runtime/framework versions. The same metadata is persisted in benchmark
  history so old results remain self-contained.
- Transcript history entries can be edited in both the standalone History dialog
  and the Settings History tab. Edits preserve the original metadata and update
  the persisted history record in place.
- Remote API keys have their own `Save API Keys` action so key updates can be
  stored without applying all settings or emitting the full settings refresh
  signal. Key badges now distinguish secure keyring storage from insecure
  fallback storage with non-red warning colors.
- Controller tests previously fell back to the real `%APPDATA%\stt_app`
  transcript history when no explicit test history store was passed. That could
  pollute a developer's real History tab with fixture texts and provider/model
  combinations such as Deepgram `nova-2`. The test suite now isolates `APPDATA`
  per test by default so production history is never touched by tests.
- The overlay now exposes transcript editing through an `Edit` button. It opens
  the shared transcript edit dialog, updates the last saved history entry, and
  refreshes the overlay text without making the no-activate overlay itself a
  text editor.
- Overlay buttons were rebalanced after adding transcript editing: stable
  status/navigation buttons stay in the header, while Retry/Cancel/Edit/Reset
  live in the action row so the overlay width does not expand just because Edit
  exists.
- Benchmark exports now use one flat result schema across CSV, XLSX, and
  Markdown. Keeping the same columns in every format avoids drift between
  spreadsheet and text exports and keeps per-run details visible everywhere.
- The transcript edit dialog keeps the validation label hidden until it is
  needed. This removes the empty vertical gap between the editor and action
  buttons while still showing the error inline when the user tries to save an
  empty transcript.

## 2026-05-03

- Release metadata was advanced to `0.2.1` before tagging so Python package
  metadata, the app `__version__`, and the installer fallback version match the
  GitHub release tag.
- Streaming text reconciliation moved from the controller into
  `streaming_text.py`. The controller now keeps only the Qt/audio/focus/insertion
  orchestration while the locked-prefix, live-tail, and finalization behavior is
  covered by pure unit tests.
- Release version handling now has an explicit helper script for bumping and
  verifying metadata. Tag-triggered release builds also compare against existing
  numeric release tags so older accidental releases fail before artifacts are
  published.
- `scripts/create_release.py` is the standard guarded release entry point. It
  runs only from clean, up-to-date `main`, prompts for the next release version,
  requires explicit confirmation, then bumps metadata, runs checks, commits,
  pushes, tags, and pushes the release tag.
- Settings presentation no longer applies an extra active-window state after
  showing the dialog. The Local and Benchmark tabs also render from the
  last-known local model inventory first, then automatically verify disk state
  after the tab has had a chance to paint. App startup also refreshes the
  persistent inventory in the background. Source-tree and packaged runs perform
  that scan in a subprocess so Python filesystem work cannot stall the Qt UI
  thread. Settings dialog lifecycle, tab paint, inventory render, and inventory
  scan timings are logged as `settings_timing` diagnostics. Local/Benchmark
  list widgets keep `AdjustToContents`; use timing diagnostics before changing
  that policy again. The tray schedules a hidden settings-dialog preparation
  after startup so first visible open and first Local tab paint avoid lazy Qt
  layout work. A hidden prepared dialog reloads settings from disk before it is
  shown.

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
- **Streaming live insertion reverted to append-only for safety:**
  - Root cause: local faster-whisper partials are based on a rolling audio
    window, and provider partials are inherently revisable. Treating them as a
    mutable target-text tail meant the app could select/delete text in the
    target editor if the caret moved, an app changed selection behavior, or a
    partial shrank.
  - Controller streaming insertion now only appends stable text. It never calls
    the replacement/delete path for live partials or finalization.
  - Rolling local faster-whisper windows are reconciled by safe word overlap so
    the live text can keep growing without treating the rolling window as a
    full mutable transcript.
  - Removed the unused text replacement wrapper/API so the controller cannot
    accidentally reintroduce Shift+Left/Backspace-based live correction.
  - Finalization uses the final transcript when present and does not re-append
    stale `last_partial` text when final is shorter.
  - General-tab copy now explains local faster-whisper versus ONNX/WebGPU and
    clarifies SendInput versus WM_PASTE behavior.
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

## 2026-07-02

- **Settings-dialog thread-safety pass:**
  - The connection-test worker read Qt widgets (`key_field.text()`,
    `language_combo.currentData()`, the Azure endpoint field) from its
    background thread. Widget values are now snapshotted on the GUI thread
    into a frozen `_ConnectionTestSnapshot` per provider before the thread
    starts, and `_build_connection_tester` became a table-driven module
    function keyed by provider name with lazy transcriber imports.
  - Benchmark, import, connection-test, and update-check workers emitted Qt
    signals directly; a dialog destroyed mid-operation raised `RuntimeError`
    in the daemon thread. All of them now go through
    `_emit_background_signal` like the local-models mixin already did.
- **Duplication cleanup:** `_save` and `_build_current_settings` share
  `_construct_settings_from_widgets`; the engine/mode/paste/corner/tone/
  timezone label dicts moved to module-level constants in
  `settings_dialog_helpers.py`; the 7 remote provider names collapsed from
  five hardcoded copies into the `_REMOTE_PROVIDERS` table (key persistence
  now iterates in canonical UI order, which is functionally irrelevant but
  test-visible).
- **History UX parity:** the Settings History tab gained Export/Import/Clear
  and a stored-count label via the new shared `history_ui_actions.py`;
  re-clicking History now force-reloads the open dialog once (selection and
  scroll preserved); double-clicking an entry copies its transcript in both
  surfaces; Settings and History dialogs now set the app window icon through
  the new shared `app_icon.py`.
- **Validation:** ruff plus the full pytest suite (with
  `test_ssl_and_preload.py` run separately) after every task on the Linux
  VPS via xvfb.

- **Concurrent transcription queue validation pass (three defects fixed):**
  - Canceling the pending streaming finalize (Cancel button/hotkey, overlay
    row ✕, Clear queue, or Retry) left `_streaming_recording` True forever:
    the canceled job resolves in the background, which never resets foreground
    session state, so every later `toggle_recording` was blocked with
    "Streaming transcript is still finalizing" and
    `_transcription_runtime_active()` kept deferring transcriber cache resets.
    `_request_job_stop` now clears the streaming session state when it stops
    the active pending finalize; the late transcript stays history-only.
  - Deferred background inserts were not flushed when a live streaming session
    was torn down: `_abort_streaming_session` (cancel during streaming,
    focus-change abort) never flushed, and `_on_transcription_failed` flushed
    *before* tearing down the failed stream's capture, so the deferred result
    stayed "Pending insert" until some later recording. The abort path now
    flushes at the end, and the failure path flushes after the teardown/reset.
  - `clear_transcription_queue` canceled the foreground job without updating
    the overlay, leaving a permanent stale "Processing" state. It now
    delegates to `cancel_queued_transcription` per token, which also removes
    the duplicated stop logic.
  - Added deterministic queue tests for all three defects plus coverage that a
    background failure cannot disturb a live recording session and that a late
    canceled-finalize transcript cannot reset a new live session.
