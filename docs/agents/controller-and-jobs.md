# Controller, jobs and runtime lifecycle: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
`controller.py`: job delivery, cancel, retry, last-recording marks, preload and the transcriber runtime lease. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **Empty model text is a failure, not "no speech"**: once a recording has
  passed the silence gate, a blank `transcribe_batch` result means the model
  missed the utterance. Parakeet TDT does this on some 1-2 s clips that
  still contain clear speech (replayed: peak 0.12, Whisper recovered the
  text, padding either dropped words or invented new ones). Do not treat
  that as the silence-gate Done path: show Error, keep the WAV for Retry,
  leave `_last_transcript` alone, and log `chars=0
  outcome=empty_transcript`. Streaming finalization may still legitimately
  be empty.
- **Background transcription failures are reported, never silent**: a queued
  job that fails while a newer session owns the overlay emits
  `background_transcription_failed` (tray notification in `main.py`) naming the
  recording and whether its audio was kept for Retry, and additionally shows
  the error on the overlay when no live session owns it. Delivering a success
  but dropping a failure made a lost recording indistinguishable from one that
  was never transcribed. **And a queued job's end marks its own recording**
  (wave 17): the background success completes it and the background failure
  marks it failed, through the keyed helpers, so the state file no longer
  says "transcribing" for a transcript already in history and the recovery
  prompt sees the failure; with `save_last_wav` off the completion deletes
  the audio, as the foreground's does. The marks were left out while they
  were unkeyed, and they have been keyed since wave 14 (the wave-17
  concurrency lens, on the real store). Two roads keep their mark: a
  canceled job's late result or failure leaves the cancel's mark -- the
  user's decision, and the audio it keeps reachable for Import (the
  wave-14 real-store retry test caught the first, unconditional shape of
  the mark deleting exactly that) -- and a success whose history write was
  refused is not completed, because that audio is then the transcript's
  only copy. A demoted streaming finalize that returns nothing still marks
  nothing: it takes the early return before the mark, `_finish_transcription_job`
  writes its stash, and the status stays "transcribing" with the audio
  kept; recorded.
- **Clear queue stops every job before it delivers anything**: cancelling the
  rows one at a time is not the same operation.
  `cancel_queued_transcription` flushes the deferred inserts on purpose, so
  that the ✕ on one row does not strand the finished transcripts on the rows
  beside it -- and on the first iteration of a loop over every row, those rows
  are exactly what is about to be cancelled. Measured with two finished
  transcripts and one running job: `transcript B.` was pasted into the focused
  window while `transcript A.`, reached before the flush, was discarded, so
  which ones survived depended purely on the loop order. Stopping every job
  first removes each deferred entry with its job, leaving the flush nothing to
  deliver.
- **A worker that cannot be scheduled is reported, not dropped**:
  `Executor.submit` raises once the pool has been shut down and again when the
  interpreter cannot start a worker thread. Both submit sites now catch it and
  route through `_on_transcription_failed`. Without that the exception escaped
  into the Qt slot, the queue row sat at "Processing" with no error and no
  Retry for the rest of the session, the recording -- often the only copy --
  was never offered for a retry, and the streaming job's runtime lease, which
  only its worker's `finally` releases, held `_transcriber_runtime_lock` for
  the process lifetime. **The preload's submit is the third site**: the
  result slot and the overlay both said "loading" before the submit, and no
  worker was ever going to complete that generation, so they said so for
  good. `_start_local_model_preload` records the failure against the key and
  routes it through `_on_model_preload_done`, so the next dictation raises it
  instead of substituting a model and the next save retries. That arm first
  drops the previous worker's future: `_matching_model_preload_running` and
  `_preload_owns_overlay` read it, and a worker `cancel()` could not stop kept
  both answering "running" for the *new* key, so the save meant to fix the
  problem found nothing to retry until the stale worker finished -- for a
  preload worker, its model load -- and the idle line stayed off the overlay
  for as long.
- **`_get_or_create_transcriber` evicts before it closes** -- the third site
  of the rule the two `_reset_*` helpers already follow. `create_transcriber`
  raises for a missing API key or an absent model, and the closed runtime was
  then still installed under its *old* key, so switching the settings back
  handed that dead runtime to the next dictation. The key is cleared with it,
  because `_local_model_preload_needed` reads the key alone: a stale one
  beside an empty cache answers "already loaded" and no preload is started.
- **Custom vocabulary** (`custom_vocabulary`, Transcription tab): user terms parsed
  by `config.parse_custom_vocabulary` (newline/comma/semicolon split,
  case-insensitive dedupe, 100-term cap). Biasing per provider: faster-whisper
  `initial_prompt` (batch + rolling-window streaming), OpenAI/Groq `prompt`,
  AssemblyAI Universal-3.5 Pro batch/streaming `keyterms_prompt`,
  Deepgram repeated `keyterm` (nova-3) / `keywords` (nova-2) query params with
  `doseq` encoding. ElevenLabs, Azure, Fun-ASR, Nemotron, and Cohere/Granite
  ONNX expose no biasing input and stay unwired.
- **Managed audio imports snapshot content and identity**: importing the managed
  last recording captures immutable bytes plus `recording_id` before submitting
  work to the controller's serialized inference lane. Completion/failure state
  uses compare-and-set transitions, so an old import cannot clear or relabel a
  newer recording. A background or import result delivered while the
  foreground transcript stays on screen never replaces its Edit target
  (the Copy/Edit pair entry below says what does). VAD auto-stop crosses
  from the audio worker through a Qt signal before touching controller/UI
  state.
  The snapshot cannot observe a half-written file either: `save_recording` and
  `snapshot_managed_recording` take the same `lock_for_path` lock, and the
  write is atomic, so a snapshot taken while dictation overwrites the managed
  recording either predates the new one entirely or sees all of it. Both
  properties are pinned by tests -- the interleaving one in
  `tests/test_store_concurrency.py`.
- **The Copy/Edit pair moves together, the Edit target is read before the
  modal, and every last-recording mark carries the job's own id** (F05 and
  F06 of the 2026-09-12 external review). `_last_transcript` is what Copy
  and the re-paste act on and `_last_history_entry` is what Edit writes
  to. `edit_last_transcript` read the text before
  `TranscriptEditDialog.get_text` and the entry after it -- and that call
  is a modal `exec()`, a nested Qt loop that keeps delivering transcription
  results, each of which moves both fields (the recording hotkey reaches
  the controller through a native event filter, so a whole dictation fits
  inside the dialog). Measured: a result B delivered while A's edit was
  open put A's edited text into B's entry and left A's untouched. The entry
  is now read beside the text, the edit is applied to that entry, and when
  the pair has moved on by the time the dialog closes the edit stays in
  history without repainting the newer result or taking the pair back
  (`transcript_edit_saved_behind_newer_result`). Two writers moved the text
  alone -- `_save_stashed_streaming_partial` and the overlay takeover in
  `_report_background_insertion_failure` -- and after either, Edit offered
  the shown text and wrote the edit into the previous dictation's entry.
  Both go through `_set_last_transcript(text, entry)`; the job keeps the
  entry its background delivery appended (`_TranscriptionJob.history_entry`),
  and a coalesced paste of several queued transcripts has no single entry,
  so Edit refuses ("No saved history entry") rather than guessing. The
  identity half: `_mark_last_recording_completed` called
  `LastRecordingStore.mark_completed()` without `expected_recording_id`,
  while `_persist_last_recording_audio` runs before the silence gate. An
  older job A finishing after a newer, silence-gated recording B therefore
  completed B -- and with `keep_after_success` off that deletes B's audio,
  backup and state (measured: `has_recoverable_recording()` False,
  `selectable_path()` None), the recording the gate had just promised to
  keep; with `save_last_wav` on, B was stamped `completed` and stopped
  being recoverable. The completion mark and the foreground failure mark
  now pass the job's `source_recording_id` (recorded at registration); an
  empty id means unknown and keeps the unconditional write, because the
  store could never match "". **A retry names the recording whose bytes
  it resubmits** (wave 14): registered from the store's slot, the retry's
  job took the newest recording's id -- the one
  `retry_last_transcription`'s own stop had just marked canceled --
  relabelled it transcribing through the one mark that was still unkeyed,
  and on completion deleted that recording's audio and state although it
  never completed under its own name (measured on the real store by the
  wave-14 concurrency lens). `_last_failed_recording_id` is retained
  beside the bytes on all three roads that keep audio for Retry -- the
  failed job's own id when its audio is promoted, and the id
  `save_recording` handed back (`_last_persisted_recording_id`) when the
  watchdog abort or a dying stream runtime persists what it keeps;
  "" when that write failed. `_submit_batch_transcription` and
  `_register_transcription_job` take it as an explicit
  `source_recording_id`, the transcribing mark is keyed like its siblings
  (`_mark_last_recording_transcribing`, beside
  `_mark_last_recording_failed`; the completion mark takes the job), and
  a job whose retained identity is "" carries `marks_last_recording=False`
  and marks nothing on any road: the store never received those bytes,
  the slot holds someone else's recording, and the unconditional write an
  empty id means on the recording roads would relabel or delete it. The
  recording roads pass the id their persist handed back, "" when that
  write did not happen (wave 16), and no longer re-read the slot for it
  (wave 18, the last paragraph of the next entry). And because the gate
  submits nothing, nothing retargeted the active token and A became the
  live session again
  -- in `history` mode its text was pasted although the user's mode had
  declined exactly that. `_is_foreground_transcription` keeps a job demoted
  to history-only in the background; `insert` mode still delivers the older
  result as before, into the job's own captured window.
  **The retry slot is retired only by its own recording** (wave 15).
  `_last_failed_wav_bytes` beside `_last_failed_recording_id` holds the
  most recent failure kept for Retry, and every foreground success used to
  empty it. A queued dictation Q that fails while the next one, X, is
  already recorded is promoted there and reported with "The audio was kept
  -- use Retry to try again"; X's success then discarded those bytes, and
  they were Q's only copy: `save_recording` keeps one managed file, X's
  save had replaced Q's, and X's completion cleared X's own (measured on
  the real store; the wave-15 concurrency lens found the shape from the
  other side, a background failure landing during a retry).
  `_retire_retry_audio_delivered_by` drops the delivered job's request
  audio and retires the slot only when those bytes are the slot's own --
  the retry of the failure it holds -- and a foreground failure with no
  bytes of its own leaves the previous failure retryable as well. So the
  streaming finalize registers its session's audio
  (`_submit_stream_finalize(wav_bytes=...)`): a finalize that fails
  promotes it under the job's own id as a batch failure does, where before
  its Error offered a Retry that answered "No failed transcription to
  retry" while the slot was emptied underneath, and a canceled finalize
  whose handshake failed now reports the audio as kept. A foreground
  failure with no bytes of its own is now a stream that died before its
  capture produced audio, or the finalize of one; its Error offers no
  Retry since wave 16 (the next entry), while the tray's "Retry
  transcription" still names the slot. (This entry said for one round
  that the button transcribes what the slot holds, which is what the
  tray's label means; the button is the Error's own.)
  And the finalize's transcribing mark goes through
  `_mark_last_recording_transcribing` like the batch submit's, keyed by
  the job's id: it was the one mark still written unkeyed, one statement
  after the persist that wrote the id it marks, so nothing observable
  changed (the lens's hypothesis).
  **A road whose persist did not write hands its job no identity, and an
  Error with no retry audio of its own offers no Retry** (wave 16). Three
  shapes on the roads the wave-15 fixes touched. First, the retire
  compares the id as well as the bytes: two recordings with byte-identical
  audio -- a fixed test phrase, a silent room -- cross-retired, X's
  success emptying Q's promoted failure although the slot was Q's, so
  `_retire_retry_audio_delivered_by` retires the slot only when the
  delivered job's bytes *and* its `source_recording_id` are the slot's
  own. What that cannot tell apart is two recordings the store never
  received -- "" beside "" -- with identical bytes (Known limitations).
  Second, the recording roads persist the statement before they submit
  and read the slot for their job's identity (read it until wave 18, the
  last paragraph below), and a persist that failed (a full disk, a
  locked file) left the slot to the previous recording:
  the batch stop and the finalize then carried that id, and their job's
  completion deleted a recording kept for Retry that was never theirs
  (the wave-16 concurrency lens, on the real store; the wave-14 entry
  above had recorded it under Known limitations). The stop road passes
  `source_recording_id=""` when its persist did not write --
  `_submit_stream_finalize(source_recording_id=)` forwards it, and a
  streaming capture that produced no bytes persisted nothing as well --
  and "" already means "the store never received these bytes": such a
  job marks nothing and its history entry names no recording. The same
  class one level out, found by the lead's real-store test for that
  unit: the failure arm's and the abort road's partial writers named the
  slot's recording and the abort's canceled mark was unkeyed. Both
  writers pass the id their own persist handed back (the failure arm
  persists a dying stream's audio itself; `_teardown_active_stream_runtime()`
  hands the bytes back and takes no `preserve_audio`), the abort's mark
  is keyed by it and skipped when the write failed, and the no-job failed
  mark is keyed the same way (`_mark_last_recording_failed(...,
  session_recording_id=)`). Third, the Retry button. The wave-15 rule "a
  foreground failure with no bytes of its own leaves the previous failure
  retryable" left the button on that Error, and pressing it transcribed
  the previous failure's audio under an Error about this one (the wave-16
  reach lens): `_on_transcription_failed` starts from `preserved_audio =
  False`, promotes only the job's own audio or the dying stream's bytes,
  and paints `error_action=OVERLAY_ERROR_ACTION_NONE` otherwise; the
  aborted stream's Error, the background report on an idle overlay whose
  audio was not kept, and the watchdog abort with no late bytes offer no
  button either, and `_retry_guidance(owns_last_recording=)` calls the
  last recording file "this recording" only when the caller's own persist
  wrote it. Two more roads found while reading those: the watchdog abort
  wrote the slot unconditionally and emptied an older failure's only copy
  on the common timeout with no late bytes -- it writes the slot for late
  bytes alone, persisted and marked failed under the id the persist handed
  back -- and `cancel_current_action`'s batch branch persists its audio
  itself, keys its canceled mark by that id, skips it when the write
  failed, and says "this recording" only then (this entry called that
  mark the last unkeyed one for one round; the silence gate's was the
  other, closed in wave 17);
  `_stop_active_capture` lost its `persist_audio` parameter with it. The
  tray's "Retry transcription" always reaches the slot; the overlay's
  button is the Error's own.
  **And the job is named by the id its persist handed back, never by a
  re-read of the slot** (wave 18). The stop road passed `None` for a
  persisted recording and `_register_transcription_job` read the slot
  again for the id -- the state the persist had just returned -- and a
  read refused in between, a scanner holding the freshly written state
  file, answered "" while the job kept `marks_last_recording`: its
  completion and failure then wrote unkeyed, and demoted behind a newer
  recording they deleted that recording's audio and state with
  `save_last_wav` off or relabelled it, and the recovery prompt never
  offered it (the wave-18 reach lens, on the real store; reachable since
  wave 17 gave the background roads their marks). `stop_recording`
  passes `_last_persisted_recording_id` when its persist wrote and ""
  otherwise, `_submit_stream_finalize` forwards it, and
  `marks_last_recording` is derived from the resolved id on the `None`
  road as well, so a job that cannot name its recording marks nothing;
  no production road passes `None` any more. The wave-14 rule "an empty
  id keeps the unconditional write" is gone with it, and only a mark
  with no job at all still writes unkeyed. `FakeLastRecordingStore`
  hands back an id per save and answers `load()`, as the real store
  does; it answered neither, so every fake-store job registered from
  the slot had marked unkeyed since the marks were keyed in wave 14,
  and four tests pinned that incidentally.
- **Model-aware language selection**: `config.language_modes_for_selection()`
  is the shared source of truth for the Transcription-tab language list, the overlay
  quick selector, and provider validation. The overlay persists a selection for
  the next recording, disables changes while listening/processing, and shows a
  disabled `Lang: Auto` button when automatic detection is the only mode.
  Auto remains the persisted default where supported; Cohere requires an
  explicit language and therefore never exposes Auto.
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
  not-yet-started worker skips and a cooperative transcriber stops), cancels
  the future if it has not started, and marks the job's own recording
  canceled in the last-recording store (`_mark_job_recording_canceled`, keyed
  by the job's recording id like the completion and failure marks, so the X
  on an older row never relabels the newest recording; a job whose id is
  unknown is marked only while it is the foreground one). Wave 13: the cancel
  hotkey marked and the queue row's X did not -- the job it stops is
  background from then on, and a background failure marked nothing until
  wave 17 -- so the store kept "transcribing" for a job that had ended; the
  two roads differed in the state file alone, since the recovery prompt
  reads the status only for "failed". **Every local engine can now be
  stopped mid-run**; the remote
  providers still only skip-if-not-started and otherwise
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
    `local` is five runtimes with four different constructor signatures
    (onnx-asr and the Granite CTC graph take the same four arguments), so
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
    `settings_changed`, and `main` routes it to
    `controller.invalidate_transcriber_credentials` so the stale runtime is
    gone before the preload decision runs. **What orders the two is the
    dialog's emit order, not the order of the two `connect` calls** -- they
    are different signals, so connection order does not relate them at all,
    and an earlier version of this entry credited the wrong mechanism (the
    comment in `main.py` says so at the call site). Both save paths emit the
    key signal first, including the arm that reports a key-storage failure and
    returns. That signal **carries the affected provider names**, and the
    invalidation is scoped to them: a key belongs to exactly one engine, so
    a loaded local model (which reads no key at all) and a Groq runtime
    under an OpenAI key change are both left alone. Selecting that provider
    later changes `settings.engine`, which the identity does see. The loaded
    engine is read from `_TranscriberIdentity.engine` rather than a tuple
    slot, which is why the identity is a `NamedTuple`.
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
- **Last recording selection**: `LastRecordingStore.selectable_path()` is the
  single selection point for "Use last recording". When an archived recordings
  directory is supplied, it chooses the newest managed/archive WAV, but
  recoverable managed recordings still win so retry/recovery state remains
  intact.
- **Selected local models are strict**: recording may start while the selected
  runtime preloads, but transcription waits off the Qt thread for that exact
  settings snapshot. Never choose, persist, or transcribe with a fallback
  model. Preload results and cancellation are generation-scoped; a canceled or
  stale worker cannot publish failure/readiness into a newer preload. Batch may
  use an isolated runtime with the same settings when a live stream leases the
  shared runtime, preventing a stream-finalizer/executor deadlock.
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
- **Completion tone (`completion_beep_enabled` + `completion_beep_tone`,
  default off/chime)**: after a successful transcript insertion (foreground
  batch, queued background insert, re-paste) the controller plays the
  configured tone via the shared `_play_tone` table on a short-lived worker
  thread (winsound is synchronous; only the recording-start beep stays
  deliberately synchronous so the microphone cannot record it). Streaming
  appends are many small pastes and stay silent by design. History-only
  delivery and failed inserts never beep. **The tone's `Thread.start` is
  guarded**: a starved interpreter raises `RuntimeError` there, and raised
  out of the deferred flush's success arm -- the tone is the last
  statement of a paste that already landed -- it reported the pasted
  transcript as not inserted and armed Insert, which pasted it a second
  time (measured through the flush and the overlay's Insert). Logged and
  skipped instead.
