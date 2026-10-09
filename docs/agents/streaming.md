# Streaming: design decisions

Binding rules for `streaming_text.py`, the rolling-window merge, the stream
handshake/finalize and the remote streaming providers. Read before changing
that area. Condensed on 2026-09-30; measurements, earlier versions and
review history are in `docs/learning-log.md` and git history. Entry order is
kept, so "above/below" refers to this file; "Known limitations" is
`docs/agents/known-limitations.md`.

Verbatim pre-condensation text: `git show e608f86:docs/agents/streaming.md` (original AGENTS.md: `df2642a`).

- **No streaming road loses transcribed text**; all read the one reader
  `_current_streaming_partial_text()` before `_reset_streaming_state()`
  wipes it. Abort (`_abort_streaming_session`): history, last transcript for
  Copy, shown, overlay revealed. Dying runtime (`_on_transcription_failed`):
  history with the retained audio path, `_last_transcript`, the Error's
  `copy_text`. Empty finalize (`_on_transcription_ready`; AssemblyAI and
  Deepgram can return "" from `stop_stream()` after a socket error): rescued;
  a silent session has no live text and still reads "No speech detected".
  (Keying that on `job.mode` instead of `_active_session_mode` only
  relabelled history and muted the beep.)
- **Shutdown aborts a live stream, never stops it**: `shutdown()` runs on the
  Qt thread (`app.aboutToQuit`) and `stop_stream()` joins without timeout;
  teardowns prefer `abort_stream()`. Only the partial's history write is
  skipped while `_has_pending_streaming_job()` (excludes `aborting`), since
  the finalize delivers it; `_teardown_active_stream_runtime` itself always
  runs, or the mic keeps recording under an Error.
- **Two rolling windows sharing no audio append, never replace**: a decode
  as slow as `stream_partial_window_s` makes windows disjoint, and without a
  pause no `segment_floor` bounds the replace (only the last window
  survived). `_transcribe_current_stream_buffer` records the decoded range
  (`last_window_start` / `last_window_end`, from the decode, not the caller)
  and `_window_shares_no_audio_with_the_last` compares it; disjoint is
  proven, so pin and append. The gap's speech is lost; log
  `streaming_decode_slower_than_window` once per session.
- **The stream worker always records why it stopped**: `_stream_worker`
  wraps `_run_stream_worker` in `except BaseException`, keeps the first
  error, does not re-raise (no stderr in a windowed build). Otherwise an
  exception outside `_maybe_emit_partial` ended the thread silently and the
  dictation read "No speech detected".
- **Copy the window, not the recording**: `_trailing_window` returns window
  and byte range from one read; `bytes(pcm_buffer)` grew with the dictation
  on a ~350 ms path.
- **AssemblyAI realtime (v3)**: never reintroduce v2 realtime or
  earlier Universal-Streaming. `assemblyai.streaming.v3.StreamingClient`,
  model `ASSEMBLYAI_STREAMING_MODEL` (`universal-3-6-pro` since 2026-10-01:
  streaming-only, same price and parameters as 3.5 Pro, 32 languages; batch
  stays on `universal-3-5-pro`), optional `keyterms_prompt`, no
  `language_detection` / `format_turns`; the batch selector does not affect
  it. Turn text keyed by `turn_order`. SDK `disconnect` runs on a helper
  thread with bounded joins; the 8 s budget covers the text-bearing teardown
  (`disconnect(terminate=True)`, `terminate_timeout` pinned to 5 s, two 1 s
  threads) and deliberately not `websockets.sync` `close()`, which waits the
  library's `close_timeout` (10 s; measured 9.02 s). The daemon helper may
  outlive `stop_stream` by ~9 s, holding no app lock. A test pins that the
  SDK leaves `close_timeout` to the library.
- **Provider sends never block the audio callback**: `push_audio_chunk` runs
  on the PortAudio thread and only enqueues.
- **Remote streaming sessions are generation-scoped**: AssemblyAI events and
  Deepgram callbacks must match the generation and the exact client/socket
  before touching state; starting/retiring are explicit states. Deepgram's
  sender queue is bounded: on the callback only `put_nowait` (saturation
  fails the stream); only a caller passing `block_timeout_s` (the preconnect
  flush) waits, with `_stream_lock` released (F09). Stop drains audio
  through a sender barrier, sends `Finalize`, waits best-effort for
  `from_finalize`, sends `CloseStream`; all bounded, and a failed path
  closes the socket without control frames overtaking audio.
- **Deepgram streaming language**: the WebSocket rejects `detect_language`;
  auto maps to `language=multi`. Batch keeps `detect_language=true`.
- **Streaming availability**: `config.supports_streaming()` is the single
  source. Cohere/Granite ONNX/WebGPU are batch-only; Nemotron streams; a
  local model never disables AssemblyAI/Deepgram streaming.
- **Streaming text state**: reconciliation lives in `streaming_text.py`; the
  controller only orchestrates. Insertion is append-only.
  - Local faster-whisper uses `merge_rolling_window_transcript`, not
    `append_only_stream_partial_candidate` (a window is not a revision; an
    empty window once wiped the final transcript). Alignment
    (`_suffix_prefix_overlap_len`) re-anchors up to
    `_WINDOW_BOUNDARY_SKIP_WORDS` words in, past a boundary-cut word.
  - **An unalignable window replaces, never appends**, unless the post-pause
    speech check below passed (caller: `vad.measure_longest_speech_run_s`
    against `silence_gate_threshold`, then `new_segment=True`).
    Unconditional append pasted 896 junk words during 2 min of silence. The
    merge must not become more permissive.
  - **`StreamingTextState` never joins a candidate that lost the
    `committed_text` prefix** (`compute_stream_locked_prefix` then stays
    frozen): joining re-pasted the dictation on a provider revision (86
    words for 48).
- **Streaming finalization**: full re-transcription at stop is opt-in
  (`streaming_full_final_transcript`, default off); otherwise only the
  trailing window is merged, so stop is fast and history matches.
- **A seam is scored `overlap - skip`, and the floor's splice searches past
  `_WINDOW_BOUNDARY_SKIP_WORDS`**: `_join_at_seam` must take neither the
  first skip clearing the threshold (duplicated six words with
  `aligned=True`) nor the longest overlap (a deep coincidence dropped a real
  seam); the floor branch must not fall back to
  `stream_join_text(protected, current)` after 3 words (re-emits the floor's
  tail). Past the bound a candidate also needs score >= 0; a refused one
  welds the whole window (bounded duplication is the safe side). Evidence,
  20000 synthetic German merges, lost/duplicated words: old 17/74129,
  longest 29/16712, `overlap - skip` 2/62893.
- **Punctuation is not corroboration**: `_stream_word_key` strips
  `.,;:!?)]}`, so marks match each other. A seam needs at least one real
  word (`_substantive_word_count` > 0) while the raw count clears the
  threshold. Do not make marks non-matching in `_stream_words_match`
  ("hallo ... welt"), nor apply the threshold to the substantive count
  ("praktisch ..." then failed and a replace lost 11 of 13 words).
- **Streaming decodes nothing during silence, at either end**: the partial
  skips an increment below `silence_gate_threshold`; the finalizer checks its
  own window (`_stream_tail_window_is_silent`). Unmeasurable (`None`) is
  never silence. The partial gate sees the increment, not the decoded window.
- **A quiet microphone can gate a streaming dictation to nothing**, as in
  batch; it surfaces as "No speech detected".
- **A focus change suspends live insertion, it does not abort**:
  `_on_stream_focus_poll` sets `_stream_insertion_suspended`; the tail past
  `committed_text` is inserted when the target returns or at stop (with
  `insert_target=current_window` into whatever is focused; a final not
  extending `committed_text` stays only in history). Arm the poll timer
  unconditionally; keep updating `live_text` (preferred by
  `_current_streaming_partial_text`). `STREAMING_ABORT_ON_FOCUS_CHANGE`
  still selects the hard abort; its tests opt in. A partial runs the same
  check itself before it pastes (`_on_transcription_partial` calls
  `_on_stream_focus_poll`): with the 25 ms timer alone a partial landing
  between a switch and the next tick pasted into the new window (measured in
  a test with no tick, 2026-10-03). What remains is the few milliseconds
  between that check and the keystroke, where the inserter's own foreground
  re-read (`docs/agents/text-insertion.md`) stands guard.
- **Live insertion waits for an earlier result for its own window**
  (2026-10-09, owner's rule: results for one window in recording order).
  `_stream_live_insert_held`, checked before each live insert like the
  focus suspension: while `_earlier_result_waits_for` the stream's window
  (a batch job recorded before it that will still paste, or an earlier
  stream's result waiting in the paste queue, `insertion_deferred`:
  skipped as "streaming", it let the next stream's live words in ahead
  of it when they arrived between the restore window's end and the pace
  timer, 2026-10-09 review), and for the
  first live insert while the previous paste's restore window is open.
  Such a result that is done pastes during the capture as long as nothing
  of the stream is in the document and its window is in front; the partial
  handler retries that flush. `live_text` stays current, so nothing is
  lost: the next allowed insert or the finalize carries it. A finalize with
  nothing inserted (`committed_text` empty) is an ordinary paste and goes
  through the paste queue when an earlier result for its window waits or
  the pace is open (`_deliver_foreground_through_paste_queue(shown=)`); a
  finalize with live text keeps the exempt direct tail -- then no earlier
  result for its window can be waiting, since one would have held the
  first live insert. Detail and the cases that already held:
  `docs/agents/text-insertion.md`.
- **The post-pause speech measurement buckets at 20 ms**
  (`STREAMING_SPEECH_RUN_WINDOW_MS`, a required keyword of
  `measure_longest_speech_run_s`, never defaulted to
  `SILENCE_GATE_WINDOW_MS`) and takes the longest unbroken run: at 100 ms,
  120 wpm typing read 1.5 s; at 20 ms a 150 ms word reads 0.16 s against
  0.02 s for typing. Name the bucket at every call site.
- **Inline locale markers are stripped by the app's own codes**
  (`transcriber/base.py:strip_language_tags`; Nemotron emits "<de-DE>",
  "<|en|>"): region = two uppercase letters or three digits, script = four
  letters; never a bare `<xx>` (`<tr>`, `<br>`, `<td>`); "<xx-anything>" ate
  `<my-widget>`, `<el-button>`, `<dom-if>`, `<log-2026>`. Never trim (it runs
  per chunk). Nemotron also strips the accumulated text.
- **An unalignable window replaces only the current segment**
  (`merge_rolling_window_transcript(protected_prefix=...)`, floor =
  `segment_floor`). The floor check accepts a raw OR word prefix
  (`stream_join_text` welds punctuation). The floor advances only when a
  window ALIGNED and ADDED text, as reported by `merge_rolling_window`, never
  via `startswith` (growth stops the identical-repeat ratchet: 53 words from
  4). Pin on alignment, not only at pauses (alignment fails at 7.2-8.0 s of
  silence, `new_segment` fires at 8.0 s). A replace loses at most one
  growing window (`STREAMING_PARTIAL_WINDOW_S`); a hallucination pinned
  before a pause stays (accepted).
- **A window after a pause is appended only when shown to hold speech**:
  `_StreamResult.silent_seconds` accumulates skipped audio even with the gate
  off (reset only in the "slice carried sound" branch). After more than
  `stream_partial_window_s` of silence, the window to be decoded needs a run
  of `STREAMING_NEW_SEGMENT_MIN_SPEECH_S` (**0.08 s**; `config.py` and its
  comment are authoritative); a peak test passes a 5 ms click. Earlier 0.35
  and 0.18 deleted "Bitte." (0.085 s) / "Stopp." (0.100 s); a key clack
  measures 0.080 s. Energy cannot separate them, so bound damage with
  `protected_prefix` and do not move the number on energy or sliced-excerpt
  evidence. A failing window is not decoded; the finalizer applies the same
  rule; never fall back to `silent_seconds` alone or unconditional append.
  **With the silence gate on, the window must also reach
  `SILERO_STREAM_MIN_PROBABILITY` (0.08) in the Silero speech check, as
  recorded** (`_stream_window_has_speech(confirm_with_speech_model=...)`,
  2026-10-01). It runs only where a refusal means "skip this window" (the
  pause route) or "drop the finalizer's tail" -- never where a refusal would
  turn an append into a replace -- and a detector that is unavailable never
  refuses. 0.08 is placed for the speech side: of 2868 owner utterances it
  refuses 4.5 on average over grid offsets, 1.1 of them heard as words by one
  of two hearer models and none by both; it refuses most thumps and fast
  typing and only 3 knocks in 50 (`config.py` has the table). No amplified
  copy here, unlike the batch check: the quietest words scored 0.51.
- **The partial callback carries the merged transcript**
  (`session.result.merged_text`); the controller must not re-merge.
- **A failed live insert gives its words back, unless it may have landed**:
  `apply_partial_append_only` commits on hand-off; on failure call
  `StreamingTextState.rollback_commit(previous)` and retry next partial.
  Post-keystroke failures (WM_PASTE contention check, failed restore) raise
  `TextMayHaveBeenPastedError`: never rolled back, no Insert. SendInput's
  deferred restore only logs (`docs/agents/text-insertion.md`). One
  `paste_sent` flag classifies, not per-site classes.
  `STREAMING_LIVE_INSERT_RETRY_LIMIT` = 3 (each try blocks Qt up to 1.5 s).
- **A remote stream handshake never runs on the Qt thread** (Deepgram
  `connected.wait(timeout=8.0)`): `_begin_stream_connect` opens the mic,
  connects on a worker, buffers audio in `_stream_preconnect_chunks`
  (`STREAMING_PRECONNECT_BUFFER_MAX_BYTES`, newest dropped with a warning)
  and flushes it in order (`_flush_preconnect_buffer`) before signalling.
  Overlay: "Connecting to the speech service. You can speak now." Start
  failures arrive via a queued signal.
  - Each flush push waits up to `STREAMING_PRECONNECT_FLUSH_PUT_TIMEOUT_S`
    (5 s; F09): Deepgram's queue holds 32 chunks and a burst of
    `put_nowait` never let the sender run. The flush re-checks the
    generation between chunks.
  - The finalize joins the connect thread for at most
    `STREAMING_CONNECT_JOIN_TIMEOUT_S` (15 s); past it
    `_await_stream_connect` answers False, the worker aborts and raises a
    failure naming the bound (recording kept, text saved), never
    `stop_stream()` beside a live flush.
- **Stop/abort never race an in-flight handshake** (a stop refused mid
  `start_stream()` left an orphan session: "Streaming session already
  active"): `_submit_stream_finalize` hands the connect thread to the job and
  `_finalize_stream_worker` joins it first. Only abort and capture failure
  (`_teardown_pending_stream_connect`) retire the handshake (bump
  `_stream_connect_generation`, clear the buffer). A stale flush refuses to
  push without clearing the buffer.
- **A normal stop hands the handshake to the finalize, which reports its
  failure** (F03): the stop leaves generation and buffer alone (retiring
  them sent 0 of 20 chunks and deleted the recording as "No speech");
  `_reset_streaming_state` retires them on every terminal path.
  - `_stream_finalize_pending` (set by the submit, cleared by the reset)
    mutes the connect signal's `ok=True` arm while `_streaming_recording` is
    still True, so "Finalizing..." is not overwritten; clear it per session.
  - A post-stop handshake or flush failure is recorded by the connect thread
    in `_stream_connect_failures` (dict by generation, under
    `_stream_preconnect_lock`) before emitting; the joining worker consumes
    it via `job.connect_generation`, tears down
    (`_abort_stream_after_failed_connect`) and raises it; the `not ok` arm
    returns while the flag is set. The record is the handshake's: written
    unconditionally, pruned at the next handshake's begin to what a
    registered job can read; a cancel's reset must not clear it.
  - Cancel and capture failure still retire the handshake
    (`_abort_streaming_session`, `_teardown_pending_stream_connect`; a test
    pins it).
  - A runtime failure after the stop belongs to the finalize:
    `_on_stream_runtime_failed` returns while `_stream_finalize_pending`
    (`stream_runtime_failed_after_stop`, INFO); `stop_stream()` returns the
    text or recorded failure, so it is reported once.
- **Remote stream finalizes have their own worker**
  (`_stream_finalize_executor_for`): `_executor` stays `max_workers=1` for
  local models; a socket drain must not wait behind a local batch. Local
  streaming finalizes on the shared worker.
- **Every arm abandoning a stream start tears down the handshake, not just
  the lease**: all three arms of `_start_streaming_recording` (incl. a
  failing `_build_audio_capture` and `AudioCaptureError`) call
  `_teardown_pending_stream_connect` before releasing, unable to raise past
  it. Nothing in `_build_audio_capture` is known to raise (`AppSettings`
  clamps `vad_energy_threshold`; `EnergyVad` / `AudioCapture` constructors
  assign); the guards are depth.
- **A runtime failure names its session; a retired one is ignored** (F04):
  `stream_runtime_failed` carries `(token, text)`; each `start_stream` gets
  an `on_error` closure capturing its `connect_token`.
  `_on_stream_runtime_failed` drops a token that is not
  `_stream_session_token` before its activity test
  (`_audio_capture is not None or ...`, which a batch recording satisfies).
  `_stream_connect_token` (written only by `_begin_stream_connect`) and
  `_stream_session_token` (cleared by `_reset_streaming_state`,
  `_teardown_pending_stream_connect`) are one object, two lifetimes.
  `_retire_failed_stream_connect` sets `_stream_chunk_error_reported` and
  drops the buffer under one generation check. Nemotron's stream worker
  does not call `on_error` for an aborted run (`run.result.error` keeps it).
- **A swallowed partial callback is logged once per session**
  (`partial_callback_failed`, like `noise_floor_warned`; AssemblyAI/Deepgram
  `_stream_partial_callback_failed`, cleared in
  `_reset_stream_state_locked`); still swallowed. Their error callback
  (bounded by `_stream_error_reported`), a raising AssemblyAI `disconnect`
  and a refused Deepgram `ws.close()` are logged; an unparseable Deepgram
  frame has its own latch. No new lock ordering.
