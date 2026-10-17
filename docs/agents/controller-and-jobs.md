# Controller, jobs and runtime lifecycle: design decisions

Binding rules for `controller.py`: job delivery, cancel, retry,
last-recording marks, preload and the transcriber runtime lease. Read before
changing that area. Condensed on 2026-09-30; measurements, earlier versions
and review history are in `docs/learning-log.md` and git history. Entry
order is kept, so "above/below" refers to this file; "Known limitations" is
`docs/agents/known-limitations.md`.

Verbatim pre-condensation text: `git show e608f86:docs/agents/controller-and-jobs.md` (original AGENTS.md: `df2642a`).

- **Empty model text after the silence gate is a failure, not "no speech"**:
  Error, keep the WAV for Retry, leave `_last_transcript`, log `chars=0
  outcome=empty_transcript`. Parakeet TDT returns blank on some 1-2 s clips
  with clear speech. Streaming finalization may legitimately be empty.
- **Background failures are reported, never silent**: a queued job failing
  under a newer session emits `background_transcription_failed` (tray, in
  `main.py`) naming the recording and whether audio was kept, and paints the
  overlay when no session owns it. Its end marks its own recording via the
  keyed helpers (success completes, deleting audio with `save_last_wav`
  off; failure marks failed). Exceptions: a canceled job keeps the cancel's
  mark; a success whose history write was refused is not completed (audio is
  the only copy); a demoted streaming finalize returning nothing marks
  nothing (`_finish_transcription_job` writes its stash).
- **Clear queue stops every job before delivering anything**:
  `cancel_queued_transcription` flushes deferred inserts on purpose, so a
  loop of per-row cancels pasted or dropped finished transcripts by loop
  order.
- **A worker that cannot be scheduled is reported**: both `Executor.submit`
  sites route a raise (pool shut down, no thread) through
  `_on_transcription_failed`; else the row stays "Processing" and a
  streaming lease holds `_transcriber_runtime_lock` forever. The preload
  submit (`_start_local_model_preload`) records the failure against the
  key via `_on_model_preload_done` (next dictation raises it, next save
  retries), after dropping the previous worker's future, which
  `_matching_model_preload_running` and `_preload_owns_overlay` read.
- **`_get_or_create_transcriber` evicts, and clears the key, before it
  closes**: `create_transcriber` can raise, and a dead runtime left under its
  old key was reused; a stale key makes `_local_model_preload_needed` answer
  "already loaded".
- **Custom vocabulary** (`custom_vocabulary`): `config.parse_custom_vocabulary`
  (newline/comma/semicolon split, case-insensitive dedupe, 100-term cap).
  Wired to faster-whisper `initial_prompt` (batch + rolling streaming),
  OpenAI/Groq `prompt`, AssemblyAI `keyterms_prompt` (batch Universal-3.5
  Pro, streaming Universal-3.6 Pro), Deepgram repeated `keyterm` (nova-3) / `keywords` (nova-2) with `doseq`.
  ElevenLabs, Azure, Fun-ASR, Nemotron, Cohere/Granite ONNX: no input.
- **Managed audio imports snapshot bytes plus `recording_id`** before
  submitting; completion/failure are compare-and-set, so an old import cannot
  relabel a newer recording. A background/import result never replaces the
  on-screen transcript's Edit target. VAD auto-stop reaches controller/UI
  state via a Qt signal. `save_recording` and `snapshot_managed_recording`
  share one `lock_for_path` lock over an atomic write
  (`tests/test_store_concurrency.py`).
- **Copy/Edit pair, Edit target and last-recording identity** (F05/F06,
  2026-09-12 review):
  - `_last_transcript` (Copy, re-paste) and `_last_history_entry` (Edit)
    change only together, via `_set_last_transcript(text, entry)` (also in
    `_save_stashed_streaming_partial` and
    `_report_background_insertion_failure`). A job keeps its appended entry
    (`_TranscriptionJob.history_entry`); a coalesced multi-transcript paste
    has none, so Edit refuses ("No saved history entry").
  - `_last_transcript` is a property over `_shown_transcript`; writing it
    clears `_delivered_after_shown` (2026-10-01). A background success sets
    only `_delivered_after_shown` (text, entry or None when coalesced), which
    the re-paste reads; Copy and Edit stay on the shown pair.
  - `edit_last_transcript` reads the entry with the text before
    `TranscriptEditDialog.get_text`, whose modal `exec()` keeps delivering
    results; if the pair moved on, the edit goes to history only
    (`transcript_edit_saved_behind_newer_result`), plus whatever of that
    entry still waits to be inserted.
  - **An edit is followed by what was not inserted** (2026-10-09):
    `_follow_transcript_edit`, from the overlay's Edit and from both history
    editors (`on_history_entry_edited`), rewrites waiting rows, queued
    results, the last background delivery, the shown pair and the offer
    for the edited entry; it writes `_shown_transcript` past the setter so
    the shown transcript keeps its row and queued token. The overlay Edit
    still clears `_delivered_after_shown` (the edited text is what F10
    re-pastes) and paints its confirmation only with no session active.
    Detail: `docs/agents/text-insertion.md`.
  - Every mark is keyed by the job's `source_recording_id`
    (`LastRecordingStore.mark_completed()` etc. get `expected_recording_id`;
    `_persist_last_recording_audio` runs before the silence gate):
    `_mark_last_recording_completed` (takes the job),
    `_mark_last_recording_transcribing`,
    `_mark_last_recording_failed(..., session_recording_id=)`. Unkeyed, an
    older job completed a newer silence-gated recording and, with
    `keep_after_success` off, deleted its audio. Only a mark with no job
    writes unkeyed.
  - The id is the one the job's own persist handed back
    (`_last_persisted_recording_id`), never a re-read of the slot (a refused
    read answered ""). `stop_recording` passes it, or "" when the persist did
    not write; `_submit_stream_finalize(source_recording_id=)` forwards it;
    `_submit_batch_transcription` / `_register_transcription_job` take it
    explicitly; no production road passes `None`. "" means the store never
    got the bytes: `marks_last_recording=False`, no marks, no recording in
    the history entry.
  - A retry (`retry_last_transcription`) resubmits under
    `_last_failed_recording_id`, kept beside `_last_failed_wav_bytes` on
    every road that keeps audio for Retry (the job's own id, or the
    persist's id for the watchdog abort and a dying stream, "" if that write
    failed).
  - The silence gate submits nothing, so `_is_foreground_transcription`
    keeps a demoted history-only job in the background (else its text was
    pasted in `history` mode).
  - **The retry slot is retired only by its own recording**:
    `_retire_retry_audio_delivered_by` retires it only when the delivered
    job's bytes *and* `source_recording_id` match (a later success had
    emptied a queued failure's only copy; "" vs "" with identical bytes
    remains, Known limitations). The finalize registers its audio
    (`_submit_stream_finalize(wav_bytes=...)`) so its failure is promoted
    like a batch failure.
  - **Every writer of the slot goes through `_hold_failed_audio_for_retry`**
    (2026-10-03): a failure used to replace the slot, so a queued dictation
    failing while another failure waited for Retry (or while its retry ran)
    took that one's only in-memory copy. The replaced failure now waits in
    `_older_failed_audio` (oldest first, at most `RETRY_OLDER_FAILURES_MAX`
    = 2, then the oldest is dropped with a `retry_failure_dropped` warning);
    the same failure promoted again (a failed retry) is the slot already and
    is not stacked. `_retire_retry_audio_delivered_by` clears the slot and
    brings the newest older failure forward, and drops an older entry that
    is itself the delivered recording; the background delivery of a result
    runs it too, so a retry delivered behind a newer recording resolves its
    failure. Retry still takes the slot only.
  - **A second Retry is ignored while the first retry of the same recording
    still runs with the same request** (owner decision 2026-10-17:
    the same audio to the same provider twice costs money twice and returns
    the same text twice; before, the second press stopped the first job and
    started another, and a remote request already sent still finished into
    history, so the provider was billed twice and history got two entries).
    `_running_retry_of_the_slot(settings)` scans `_jobs` for a job that is not
    aborting and not a finished result waiting for its paste, whose request
    equals the one the new retry would send and whose request audio and
    `source_recording_id` equal the slot's own -- the identity
    `_retire_retry_audio_delivered_by` uses, so the same bytes under another
    recording id are another recording. Being derived from `_jobs`, the block
    ends with the job: delivered, failed or cancelled, it leaves the registry
    and Retry works again (a failed retry is promoted to the slot again; after a
    success the slot is retired and the older failure behind it comes forward).
  - **"The same request" is `_request_identity`, not engine and model alone**
    (review 2026-10-17; the first version compared only those two, so a retry
    after the user changed the vocabulary, a region, an endpoint or the key was
    refused as a duplicate although the running request had gone out with the
    old values). It is `_transcriber_identity(settings)` -- everything
    `create_transcriber` bakes into the runtime, per engine, so engine, model,
    custom vocabulary where the model takes one, Azure endpoint, region, the
    custom endpoint's URL, API style and key command, silence-gate threshold,
    key presence -- plus `language_mode` (the identity leaves it out on purpose,
    but the request carries it) plus `_credentials_generation`. A key value is
    in no settings field, so `invalidate_transcriber_credentials` (the Settings
    dialog's report of a replaced key) counts up a generation and each job
    remembers the one it was submitted under; any provider's key change counts,
    which can only allow a retry, never block one. Settings that no provider
    receives (opacity, history size, hotkeys) stay out of it: such a retry is
    still a duplicate. A changed request is another request: it replaces the
    running one (next bullet). A job being stopped does not count, so Cancel,
    then Retry, works; a retry that went behind a newer dictation still counts.
  - **A Retry stops the jobs working on its own recording, and nothing else**
    (owner decision 2026-10-17, review round 2). `_live_jobs_of_the_slot()` --
    not aborting, not a finished result waiting for its paste, request audio and
    `source_recording_id` equal to the slot's, the filter
    `_running_retry_of_the_slot` applies before it compares requests -- are
    each stopped with `_request_job_stop(delivery=history)`; a late result of
    theirs goes to history only. It used to stop `_active_request_token`'s job,
    which was wrong both ways: a retry R1 that had lost the token (a newer
    dictation was recorded and cancelled, or is still in flight) kept running
    beside a changed retry R2, and both delivered -- two history entries, two
    pastes of one text -- and a Retry of an older failure stopped whatever held
    the token, which could be the newest dictation: diverted to history only,
    never pasted. Now the newest dictation is left alone and keeps its
    `insert` delivery. The retry takes the overlay's token as every submit
    does: it paints and keeps its "Processing", Cancel on the overlay cancels
    it (`cancel_current_action` stops the token's job), and the newer job
    becomes a queued one that delivers by the normal order rules (its paste
    waits while a foreground job is active, and goes to its own window).
    The one cost: `_executor` has a single worker, so the retry starts only when
    the running job has ended -- behind a long local transcription that can be
    minutes, which the old stop cut short for the newest dictation at the price
    of discarding its paste. Cancel on that job's queue row frees the worker.
    **One job cannot be left running: a pending streaming finalize**
    (`_streaming_recording` with the microphone closed). It owns the session
    and only its own foreground result clears it; with the token moved to the
    retry its result would be a background one and the session would stay open,
    refusing every new recording ("Streaming transcript is still finalizing").
    Stopping it would be the diversion again, so the Retry is refused with a
    tray note ("The streaming transcript is still finalizing. Retry the last
    failed transcription when it has finished.") and works a few seconds later.
    Before this the comment at the guard said a pending finalize "is stopped by
    design". Tests: `test_a_retry_never_stops_a_job_of_another_recording`,
    `test_a_changed_request_retry_stops_the_running_retry_without_the_overlay_token`,
    `test_a_changed_retry_beside_a_newer_dictation_replaces_only_its_own_job`,
    `test_a_retry_waits_for_a_streaming_finalize_instead_of_stopping_it`.
  - **The ignored press is answered on the tray, never on the overlay**: it
    logs `retry_ignored_duplicate`, returns `False` and emits
    `busy_overlay_error` -- "A retry of this recording is already running with
    the same settings; cancel it first to retry again." -- the second half says
    what to do, since the first alone leaves the user with a button that does
    nothing. It does not go through `show_overlay_error`: that paints a red
    Error with a Retry button whenever `_overlay_session_active()` is false,
    which happens while the retry runs (`_active_request_token` is None after a
    newer dictation was cancelled), and that button is the press just refused.
    Tests: `test_a_second_retry_is_ignored_while_the_first_runs_with_the_same_engine_and_model`,
    `test_a_retry_with_other_request_settings_is_allowed_while_the_first_runs`,
    `test_the_duplicate_notice_never_repaints_an_overlay_no_session_owns` and
    their neighbours in `tests/test_controller_queue.py`.
  - **An Error without retry audio of its own offers no Retry**:
    `_on_transcription_failed` starts at `preserved_audio = False`, promotes
    only the job's own or the dying stream's bytes, else paints
    `error_action=OVERLAY_ERROR_ACTION_NONE` (also for aborted streams,
    unkept background reports, watchdog aborts without late bytes); the tray
    "Retry transcription" still reaches the slot.
    `_retry_guidance(owns_last_recording=)` says "this recording" only when
    the caller's persist wrote it.
  - Roads persist their own audio and key their marks by that id, skipping
    the mark when the write failed: the failure arm (a dying stream;
    `_teardown_active_stream_runtime()` returns the bytes, no
    `preserve_audio`), the abort, `cancel_current_action`'s batch branch
    (`_stop_active_capture` has no `persist_audio`), and the watchdog abort,
    which writes the slot only for late bytes.
  - `FakeLastRecordingStore` returns an id per save and answers `load()`.
- **Model-aware language selection**: `config.language_modes_for_selection()`
  feeds the Transcription tab, the overlay selector (persists for the next
  recording, disabled while busy, disabled `Lang: Auto` when auto is the only
  mode) and provider validation. Cohere never exposes Auto. The controller asks
  it through `_offered_language_modes()` -- the active engine's own model
  (`_selected_model_name`), never `model_size`, which is the local model:
  asked with it, every remote model with a list of its own (Deepgram nova-2 and
  nova-3, AssemblyAI Universal 3.5 Pro, Azure MAI-Transcribe 1 and 1.5,
  Speechmatics Enhanced and Standard) showed, and `set_language_mode` accepted,
  the engine's default list (2026-10-17;
  `test_overlay_language_list_is_the_remote_models_own`).
- **Concurrent mode + cooperative cancel: a finished transcription is never
  discarded.** `concurrent_transcription_mode` (`insert` default /
  `history` / `cancel`): on a new recording the in-flight job inserts into
  its own captured window plus history / history only / is stopped, a late
  result kept in history.
  - Batch jobs share the `max_workers=1` executor and run in FIFO; a
    foreground result flushes the deferred older ones before pasting its own.
    The mode changes only delivery. A remote stream finalize has its own worker
    (`_stream_finalize_executor_for`) unless an older job is still working
    (`_has_undelivered_older_job`), else it could paste the later dictation
    first. A streaming dictation's live inserts wait for an earlier result
    for their window (`_stream_live_insert_held`, 2026-10-09); the order
    rule and every case it covers: `docs/agents/text-insertion.md`.
  - A `_TranscriptionJob` holds the target window, `background_delivery`
    and `aborting`. Foreground = token active, no newer recording, not
    aborting; `_new_recording_active()` excludes `_streaming_recording`.
    Progress/ready/failed use the same check; background progress never
    shows Processing; background handlers
    (`_handle_background_transcription_ready`) never reset foreground state.
  - Explicit cancel (row ✕, Clear queue, Cancel) goes through
    `_request_job_stop` (delivery `history`): sets `aborting`, cancels an
    unstarted future, marks the job's recording canceled
    (`_mark_job_recording_canceled`, keyed; unknown id only while
    foreground). Stopping the pending finalize clears the session so the
    next recording is not blocked. Remote providers only skip-if-not-started.
  - Queue rows scroll in `_queue_scroll` past `OVERLAY_QUEUE_MAX_HEIGHT`;
    `_apply_queue_scroll_height` keeps `OVERLAY_DETAIL_MIN_HEIGHT` using the
    *layout* sizeHint; `set_transcription_queue` re-asserts size after the
    loop drains (`_refresh_size_after_queue_change`), and hiding the queue
    returns the overlay to the normal compact size.
  - Clear the cancel hook after each batch run (cached transcriber).
  - Flush `_deferred_background_results` on every path clearing the blocking
    session: recording start/stop, stream abort and runtime failure (after
    teardown), `cancel_current_action`, `cancel_queued_transcription`.
    `_should_defer_background_insertion` / `_flush_deferred_background_results`
    take `ignore_active_transcription`: capture or start/stop always blocks;
    True on explicit cancel (`cancel_current_action` incl. "nothing to
    cancel", `cancel_queued_transcription`, `_abort_streaming_session`),
    otherwise the `_active_request_token` guard holds. Deferred tokens are
    always older than the active one, so delivering them first keeps token
    order; the running transcription delivers itself later, no duplicate.
    A `paste_paced` job (held by the restore-window pace, see
    `docs/agents/text-insertion.md`) passes the active-transcription guard
    (a capture still blocks unless immediate mode) and is flushed by the
    single-shot `_paste_pace_timer`, stopped in `shutdown`; a held group is
    put back with the wait it still owes. While the pace is all that holds
    it (`pace_held`), `_update_queue_overlay` leaves it out and
    `clear_transcription_queue` / `cancel_queued_transcription` skip it; a
    flush that defers it for a recording or a window clears the flag. The
    same timer runs a re-paste held by the pace (`_pending_repaste`) after
    the flush.
- **Every local engine cancels mid-run** via `set_cancel_check`, raising
  `TranscriptionCanceled`; else a canceled job holds the worker, its model
  and the shared lease: the next dictation queues behind it, and a later
  preload waits on that shared lease, so the overlay says "still loading"
  forever while each dictation quietly pays for its own isolated runtime.
  - `faster-whisper`: between segments. `Nemotron`: per 560 ms chunk.
    Cohere/Granite Node: reader polls per 0.25 s and kills the child (the
    request is already in flight); discarding the loaded model is the point,
    since freeing CPU and memory is what Cancel is for.
  - `onnx-asr`: `_install_cancel_hooks` wraps each `InferenceSession.run`
    with a per-call app-owned `RunOptions` (ORT never clears `terminate`); a
    watchdog polls every `_CANCEL_POLL_INTERVAL_S` (0.66 s to cancel a 4.46 s
    run). ORT's generic `Fail` maps to `TranscriptionCanceled` only when we
    asked; `transcribe_batch` serializes on `_inference_lock`; `close()`
    unwraps under `_model_lock` and `_inference_lock`: removing the wrappers
    mid-`recognize()` makes the watchdog set `terminate` on a `RunOptions`
    nobody passes, so the run finishes in full with no log line.
  - Check before the model load, not only before the run.
  - The check lives on `ITranscriber` (`transcriber/base.py`:
    `set_cancel_check`, `_is_cancel_requested` logs a raising check once,
    `_raise_if_canceled`); an overriding setter must call `super()`.
  - `base.canceled_download_is_a_cancel()` maps `ModelDownloadCanceled` to
    `TranscriptionCanceled`; `_preload_model_worker` reports it as a cancel,
    `run_benchmark_cases` raises `BenchmarkCancelled`.
- **The preload names its phase**: `_preload_phase` = `(generation, phase)`,
  phases download / load / `queued`; `_preload_phase_word` feeds the
  recording-start notice and streaming refusal. Clear it in both
  `_on_model_preload_done` and the `on_settings_changed` remote-engine
  cancel branch, or `_current_preload_phase()` answers for a finished one.
- **A preload must not hide a failed hotkey registration**: the
  `_preload_owns_overlay()` early return in `show_idle_status` sits below
  the four hotkey-error branches (`reload_settings` relies on them).
- **A language change never reloads a runtime**: `language_mode` is in
  neither the cache key nor the preload key; `ITranscriber.set_language_mode`
  applies it when a job acquires the runtime. Restricting providers override
  `_normalize_language_mode`. `controller.set_language_mode` only persists
  and syncs UI. Guard: `tests/test_factory.py`.
  - **A queued recording keeps the settings it was recorded under**
    (owner wish and verified state, 2026-10-09). The snapshot is taken when the
    recording starts (`_start_batch_recording(replace(self._settings))` ->
    `_active_batch_settings`; streaming `_active_stream_settings`), is the
    `_TranscriptionJob.settings` and the `settings` argument of the worker, and
    covers language, engine, model and custom vocabulary alike. The worker takes
    the language from that snapshot when it acquires the runtime
    (`_get_or_create_transcriber`); acquisition is serialized by the runtime
    lease, so recording 1 in English and recording 2 in German run with their
    own language one after the other on one cached runtime. A switch while a job
    runs changes nothing for it, and it neither cancels nor reloads anything
    (the Cohere Node runner takes `language` per request, `local_webgpu_asr.py`
    `_language_arg`; it is a fresh runtime per job unless Keep ONNX model loaded
    is on, whatever the language). Delivery-time preferences -- paste mode,
    insert target, clipboard keeping, completion tone -- stay the current ones on
    purpose: they describe where text goes now, not how audio was made.
    Tests: `test_each_queued_recording_is_transcribed_in_the_language_it_was_recorded_in`
    and `test_a_language_switch_never_changes_or_reloads_queued_work`.
  - **Retry transcribes in the language the recording was made in; engine and
    model stay the current ones** (owner decision 2026-10-17, replacing the
    2026-10-09 rule that Retry used what is selected now; a user switches
    provider after a provider failure and retries, but the audio was spoken in
    one language). `retry_last_transcription` resubmits the kept bytes under
    `_retry_settings()`: `replace(self._settings)` with `language_mode` set to
    `_recorded_language_by_recording_id[_last_failed_recording_id]`, which
    `_persist_last_recording_audio(wav_bytes, language_mode)` fills where a
    recording's id is born -- the stop (batch and streaming), the watchdog abort,
    a dying stream, a cancel -- from the recording's own snapshot
    (`_open_recording_language`; the stream's `_active_stream_settings`).
    A streaming session is retried as a batch job in its stream's language. The
    overlay says "(recorded language: X)". Two cases keep the language selected
    now, as before, and the overlay says which applied: a recording this process
    has no language for (the store refused the write, an id from an earlier
    run), and a language the current engine and model do not offer
    (`language_modes_for_selection(engine, model, "batch")`, logged as
    `retry_recorded_language_not_offered`). The second is not passed on to the
    provider because each provider's own `_normalize_language_mode` then picks a
    stand-in -- Canary falls back to the first language of its list (German),
    which makes it translate into it instead of transcribing, Cohere forces
    German for `auto`, Granite CTC (English only) decodes German speech as
    English -- while the selection is one the user picked for the engine in use.
    A saved selection the engine does not offer either (it stays saved when the
    engine is switched, and the overlay's menu shows the engine's first language
    instead) is clamped to that first language, the overlay's own rule
    (`OverlayUI.set_language_options`), so the language named in the overlay
    text, the log (`using=`) and the startup notice is the one the provider
    receives. Before the review of 2026-10-17 (round 2) the selection was named
    as it stood: a stale Afrikaans on Deepgram nova-2 was reported while the
    provider's own normalization ran Auto.
    **One helper decides it for both roads**: `_pin_recorded_language(settings,
    recorded, recording_id)` returns the pinned settings and `True`, or the
    settings with the language that runs and `False` (the decision is
    `_language_a_retry_runs_in`; `_pin_recorded_language` writes the log line
    once); `_retry_settings` words the overlay text from it and
    `transcribe_unfinished_recording` uses it too. The restart road had its own
    unchecked pin until the review of 2026-10-17: a recording made in Auto and
    retried after a restart under Canary was sent as `auto` and came out
    translated, and with recordings retention off the WAV was deleted after the
    wrong transcript was saved.
    The language also travels to disk, so the same holds after a restart:
    `LastRecordingStore.language_mode` and the unfinished recording's file name
    (`docs/agents/persistence-and-history.md`), which the quit writes
    (`_keep_unfinished_recordings`) and the startup notice transcribes with
    (`transcribe_unfinished_recording`). The file names the language the
    recording was **made** in, never the one a fallback retry ran under: a job's
    own snapshot is used only for a recording this process never named
    (`_recorded_language_by_recording_id` wins), or the next start would pin the
    fallback for good. Where the fallback applied after a restart the startup
    notice's row says so -- `Transcribed, in History (English; Auto not
    offered)`: the language that ran, then the one the engine did not offer.
    The text comes from `unfinished_recording_language_note(recording)` (the
    same `_language_a_retry_runs_in` decision, no second log line), handed to
    `UnfinishedRecordingsDialog(language_note=)` and asked right before each
    row's transcription; the dialog's `(ok, text)` contract is unchanged
    because many callers unpack it. History retranscribe and the Import tab
    take their settings from their own pickers at the click; that is where a
    wrong language is now corrected, no longer by switching and pressing Retry
    (the Import tab's "Use last recording" starts its language picker from the
    recording's own, `docs/agents/settings-dialog.md`). Tests in
    `tests/test_controller_queue.py` (`test_retry_uses_the_language_the_recording_was_made_in`,
    the streaming, dying-stream, failed-retry and unknown-language cases, and
    `test_retry_names_the_language_that_runs_when_the_selection_is_not_offered_either`),
    `tests/test_controller_quit.py` (the restart road for Canary, Cohere and
    Granite CTC, the note, a quit during a fallback retry, a stale selection
    named as what ran),
    `tests/test_unfinished_recordings_notice.py` (the row text).
  - Model, engine or vocabulary changed between two queued recordings do reload
    the cached runtime when the second job acquires it (their identity differs);
    only language never does.
- **A save reloads the model only when the model changed**:
  `_transcriber_identity(settings)` describes what `create_transcriber`
  bakes in; reset only when it differs, preload
  (`_local_model_preload_needed`) only when the cache lacks it (a failed
  preload retries every save).
  - It lists every constructor argument and only those, per engine and for
    `local` per runtime, mirroring `_create_local_transcriber` (e.g.
    `silence_gate_enabled` / `silence_gate_threshold` are arguments;
    `keep_onnx_model_loaded` only the Node runtime's)
    (`_LOCAL_RUNTIME_FIELDS` in `tests/test_controller.py`; parametrized
    reload/no-reload test with non-default values). Remote:
    `_ENGINE_MODEL_FIELDS[engine]`, `_ENGINE_KEY_FLAGS[engine]` indexed
    strictly, both covered for every engine by a test.
  - Keys are not in `AppSettings` (`has_*_key` flips only on add/remove;
    a changed provider shows in `settings.engine`): both save paths (incl.
    the key-failure arm) emit `provider_keys_changed` with the provider
    names before `settings_changed`; `main` routes it to
    `controller.invalidate_transcriber_credentials`, scoped to those
    providers. Emit order, not `connect` order, orders them. The identity is
    a `NamedTuple` (`_TranscriberIdentity.engine`).
- **Worker `finally` bookkeeping cannot skip the release**
  (`_transcribe_worker`: logged bookkeeping, release in an inner `finally`).
- **Evict the cached transcriber before closing it**
  (`_reset_transcriber_cache_locked`,
  `_reset_resume_sensitive_transcriber_cache`), so a raising `close()`
  leaves nothing dead cached.
- **Never close an in-use runtime** while `_transcription_runtime_active()`
  (breaks a keep-loaded ONNX subprocess or a live Nemotron stream);
  `reload_settings` defers via `_pending_transcriber_cache_reset`. Work
  acquires a `_TranscriberRuntimeLease`: one owns the shared cache,
  overlapping work gets an isolated close-on-release runtime so Qt never
  waits behind inference; preload waits off-thread for the shared lease;
  only a shared owner applies deferred reset/close. Canceled workers count
  as active until released. Terminal signals follow hook clearing and lease
  release. Shutdown marks closed first, ignores late signals, lets the final
  owner close. Resume uses the same admission lock.
- **A quit is bounded by a native watchdog** (2026-10-03): `main` arms
  `faulthandler.dump_traceback_later(QUIT_WATCHDOG_TIMEOUT_S, exit=True)`
  into `dictation.log` as the first `aboutToQuit` handler, and logs
  `app_exec_returned` with every non-daemon thread still alive. Why: a quit
  once removed the tray icon and kept the process alive until Ctrl+C, with
  nothing in the log; Ctrl+C cannot reach a thread blocked in native code,
  and no Python thread runs once finalization starts. Measured: a thread
  that never ends now exits the process after 15 s with its stack logged.
  The hang's own cause is still unknown; the next one names it in the log.
  The tray's Quit reaches `app.quit` only after the quit window below is
  done, so a deliberate wait is never cut short by this watchdog.
- **The tray's Quit asks first when work is pending** (owner decision
  2026-10-09; `quit_dialog.QuitCoordinator`). `quit_pending_work()`
  (`PendingQuitWork`) counts an open recording, jobs still transcribing,
  finished results held for their paste (window, recording, pace, a held
  re-paste) and the not-inserted rows; aborting jobs do not count. Nothing
  pending: quit as before. Otherwise a fixed-size tool window (480x278 at
  96 dpi, every state) offers Wait and insert / Quit now, keep the
  recordings / Don't quit. Wait calls `hold_for_quit()` on every 250 ms
  poll: it stops the open recording (transcribed and inserted as usual). A
  new recording during the wait calls the quit off (owner decision
  2026-10-09; it was refused until "Don't quit" before): `start_recording`,
  past its capture and finalize guards, clears the hold first -- so the next
  poll cannot stop the new recording --, emits `quit_canceled_by_recording`
  (the coordinator closes the window as for "Don't quit") and sends "Quit
  canceled by the request to record" to the tray (worded so it stays true
  when the start is refused); a start refused further down leaves the quit called
  off as well. The coordinator quits once `can_wait` is
  false -- which includes `paste_settling`: the last paste's restore window
  (`_paste_pace_wait_s`) and its target check must be over, or the quit
  flushes the restore under a late-reading target (review of 3ee1e23); a
  settling paste alone does not make Quit ask -- unless a paste or a transcription failed during the wait -- then
  it says so and waits for Quit or Don't quit. Don't quit, Esc and the
  title bar's close call `release_quit_hold()`. A tool window, because
  `window_focus` never picks one of ours as a paste target: a wait's paste
  into "the current window" reaches the user's window, not the dialog.
  SIGINT/SIGTERM still quit at once.
- **A quit keeps every recording that has no transcript**
  (`_keep_unfinished_recordings`, from `shutdown` before the jobs are marked
  aborting): the open capture, each job still transcribing (its request
  audio), the Retry slot and `_older_failed_audio`, written to the
  `UnfinishedRecordingStore` under the job's recording id ("" gets a fresh
  uuid) and its `created_at` (Retry entries through
  `_recorded_at_by_recording_id`, filled at job registration). Left out: a
  finished result waiting for its paste (its text is in history) and an
  aborting job (the user stopped it). One recording reached twice (a retry
  of the slot's bytes) is written once. Why: the managed last recording is
  one slot, so a quit with a queue lost every older recording.
- **The WebGPU runner exits on `shutdown`** (`process.exit(0)` in
  `webgpu_asr_runner.mjs`): leaving the request loop did not end Node,
  because ONNX Runtime's WebGPU device keeps its event loop alive, so every
  quit waited out the 2 s grace and then killed it. Quit with a loaded
  Cohere runtime: 2.2 s before, 0.4 s after (2026-10-03).
- **`LastRecordingStore.selectable_path()` alone picks "Use last
  recording"**: newest managed/archive WAV; a recoverable managed one wins.
- **Selected local models are strict**: transcription waits off the Qt
  thread for the exact snapshot's preload; never use a fallback model.
  Preload results and cancel are generation-scoped. Batch may use an
  isolated same-settings runtime while a stream leases the shared one.
- **`_TranscriberRuntimeLease.release()` always hands back, swallowing
  (incl. `BaseException`) a failed close and a failed deferred reset**:
  `_close_cached_transcriber` catches only `Exception` and `_released` is
  set first, so an escape stranded `_transcriber_runtime_lock` and the
  terminal signals of `_transcribe_worker`, `_finalize_stream_worker` and
  `_preload_model_worker`. `_release_transcriber_runtime`'s `finally`
  returns the lock and use count; a failed reset leaves
  `_pending_transcriber_cache_reset` for the next release.
- **Clear an owner flag before disposing** (`orphan = None` before the close
  in `_acquire_transcriber_runtime`'s shutdown branch; else a double close
  replaces `TranscriptionCanceled`).
- **Workers emitting after `finally` need a last-resort `except
  BaseException`** that reports, not re-raises (`_transcribe_worker`,
  `_finalize_stream_worker`, `_preload_model_worker`); else the overlay
  sticks in Processing, `_streaming_recording` stays True, or
  `_preload_phase` goes stale. `_acquire_transcriber_runtime`'s two cleanup
  arms are `BaseException` but re-raise (the lease is still `None`).
- **Error-path cleanup must not raise, nor precede its release**:
  `_teardown_pending_stream_connect` is exception-tight and both arms
  release in a `finally` (`Thread.start` once skipped
  `runtime_lease.release()`). Re-derive an arm's guarantees when adding to it.
- **Copying an exception arm copies its preconditions**: the preload's
  `except BaseException` starts with `_preload_generation_was_canceled`,
  or a cancel lands in `_preload_results` and `toggle_recording` re-raises it.
- **Transcription stays threaded, not a subprocess** (CTranslate2/ORT
  release the GIL; a subprocess breaks preload latency and streaming).
- **Local streaming/runtime state is generation-scoped**: workers own
  immutable sessions; Nemotron keeps native objects until retired workers
  exit. The ONNX Node parent serializes lifecycle/stdin with bounded reader
  state and absolute deadlines and kills a timed-out or poisoned child; the
  JS server serializes requests and rejects oversized lines/bad WAVs early.
- **Completion tone** (`completion_beep_enabled`, `completion_beep_tone`,
  default off/chime): after a successful insert (foreground, queued
  background, re-paste) via `_play_tone` on a short-lived thread (only the
  start beep is synchronous). Never for streaming appends, history-only or
  failed inserts. `Thread.start` is guarded (any `Exception`, so a `MemoryError` too): a
  `RuntimeError` there once reported a landed paste as failed and armed a
  duplicate Insert. One tone
  per coalesced paste, and one for a re-paste during a session. With a
  paste target check configured the tone waits for its answer
  (`_on_paste_target_checked`, at most `PASTE_TARGET_CHECK_TIMEOUT_MS`) and
  is not played when it says "not a text field"
  (`docs/agents/text-insertion.md`); a check that cannot start plays it at
  once. It is skipped when a recording other than the one open at the
  paste started before the answer (`_recording_started_since`, 2026-10-03
  review): the delayed tone would reach that recording's microphone. A
  queued paste mid-recording keeps its tone, as before the check.
