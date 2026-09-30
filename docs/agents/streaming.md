# Streaming: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
`streaming_text.py`, the rolling-window merge, the stream handshake/finalize and the remote streaming providers. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

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
  recording after the overlay already showed Error. **A finalize that returns
  nothing is the third road to the same loss, and it is closed the same way**:
  `_on_transcription_ready` rescues `_current_streaming_partial_text()` when a
  streaming result is empty, before `_reset_streaming_state()` wipes it. Both
  AssemblyAI and Deepgram can return an empty string from `stop_stream()`
  after a socket problem, which is exactly when the live text is the only copy
  left; without the rescue the overlay said "No speech detected", history got
  no entry, and the dictation survived only as the part already pasted into
  the document. A session that really said nothing has no live text either, so
  it still reports that. Reading `job.mode` instead of `_active_session_mode` in
  `_on_transcription_ready` was tried and reverted: every writer of
  `_active_session_mode = "batch"` also resets the streaming text state, so
  `committed_text` is already empty by then and the delivery is identical
  either way — it only relabelled history and suppressed the completion beep.
- **Two rolling windows that share no audio append, they never replace**:
  the decode takes the trailing `stream_partial_window_s`, so a decode that
  takes about as long as that window -- a large model on a slow machine, RTF
  near 1 -- advances the buffer further than the window is wide and the next
  window is disjoint from the last. `merge_rolling_window` then finds no seam
  and falls through to its replace branch, and with continuous speech
  `silent_seconds` never accumulates, so no pause has ever pinned
  `segment_floor` and the replace is unbounded. Measured with an 8 s window
  and 9 s between decodes: `'erster teil der nachricht'` became `'und dann kam
  etwas ganz anderes'`, i.e. only the last window survived however long the
  dictation had been, and the fast finalizer lost everything in one step.
  `_transcribe_current_stream_buffer` therefore records the byte range it
  decoded (`last_window_start` / `last_window_end`) and
  `_window_shares_no_audio_with_the_last` compares the next window's start
  against the previous end. Three properties are load-bearing:
  - **The offsets come from the decode, not from the caller.** The capture
    thread keeps appending, so a length read *before* the call gives a window
    start earlier than the real one -- the direction that reports an overlap
    where there is none.
  - **Disjoint is proven, not measured.** Nothing already transcribed can be
    revised by a window that shares none of its audio, so pinning the floor
    and appending is correct rather than heuristic. It is the pause case
    reached by the other road; `new_segment` only skips an alignment search
    that cannot succeed -- and that search does sometimes succeed by
    coincidence, on words the two windows happen to share, which swallows the
    window instead of appending it. A test whose two fake transcripts shared
    three trailing words hit exactly that and passed for the wrong reason.
  - **The speech in the gap was never decoded and is lost either way.** This
    only stops that loss from taking the rest of the transcript with it, and
    logs `streaming_decode_slower_than_window` once per session.
- **The stream worker records why it stopped, always**: only the decode inside
  `_maybe_emit_partial` and the finalization were guarded, so an exception in
  the energy meters, the merge or the buffer append simply ended the thread.
  `stop_stream` then joined a dead worker, found no error and an empty
  `final_text`, and the whole dictation reached the user as "No speech
  detected" -- and a windowed build has no stderr, so `threading.excepthook`
  printed the traceback nowhere. `_stream_worker` wraps `_run_stream_worker`
  in `except BaseException`, records the error (keeping the *first* one, so a
  failure raised on the way out cannot replace the one that explains the
  session) and does not re-raise, because at the top of a worker thread
  re-raising only writes to that same missing stderr.
- **A streaming window is copied, not the whole recording**:
  `_trailing_window` returns the window plus the byte range it covers, reading
  the end once so the two agree. `bytes(pcm_buffer)` grows with the dictation
  -- measured at 16 kHz mono, 0.21 ms for one minute of audio, 0.93 ms for
  five and 3.14 ms for fifteen, against a flat 0.10 ms -- on a path that runs
  every ~350 ms and took up to three such copies when the pause branch also
  measured the window.
- **AssemblyAI Universal-3.5 Pro realtime**: the legacy v2 realtime and earlier
  Universal-Streaming model are retired paths and must not be reintroduced.
  Streaming uses `assemblyai.streaming.v3.StreamingClient` with the
  explicit `universal-3-5-pro` model and optional `keyterms_prompt`; its native
  18-language code switching needs no legacy `language_detection` or
  `format_turns` parameter. The batch selector does not alter realtime routing.
  Turn text is keyed by `turn_order` because later events can refine the same
  turn. Bound SDK `disconnect` joins with a helper thread; they can hang on dead
  connections. **The stop budget contains the text-bearing teardown and
  deliberately not the websocket close**: `disconnect(terminate=True)` waits
  `terminate_timeout` (pinned to 5 s, not inherited), joins its two 1 s-loop
  threads, and then calls `websockets.sync`'s `close()`, which waits for the
  peer's close handshake up to a `close_timeout` the SDK never overrides --
  10 s by default, measured 9.02 s against a loopback peer that never
  acknowledges the close frame. Nothing is dispatched after the read thread
  is joined and the turn is stored before the joins begin, so the 8 s join
  stops short of that stage on purpose; the helper thread outlives
  `stop_stream` by up to ~9 s on a daemon thread holding no app lock. An
  earlier comment modelled the teardown as `terminate_timeout + 2 s` and
  called that the whole of it. A test pins that the SDK source still leaves
  `close_timeout` to the library, because the model above depends on it.
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
  Deepgram's sender queue is bounded; on the PortAudio callback
  `push_audio_chunk` uses only `put_nowait`, so saturation fails the stream
  rather than dropping audio or blocking the callback, and only a caller
  that passes `block_timeout_s` -- the controller's preconnect flush, on its
  own worker thread -- waits for room, with `_stream_lock` released so a
  stop or abort from the Qt thread is never held behind that wait (F09,
  2026-09-16). Normal stop first drains queued binary audio through a
  sender barrier, then sends `Finalize`, waits best-effort for the optional
  `from_finalize` response, and sends the documented `CloseStream` command.
  Control sends and all waits are bounded; a failed drain/control path closes
  the socket without allowing control frames to overtake queued audio.
- **Deepgram streaming language**: the live WebSocket API rejects
  `detect_language`; auto maps to `language=multi` (nova-2/nova-3
  multilingual code-switching). Batch keeps `detect_language=true`.
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
- **A seam is scored by what it explains minus what it discards
  (`overlap - skip`), and the floor's splice searches deeper than
  `_WINDOW_BOUNDARY_SKIP_WORDS`.** Two ways the merge duplicated text into the
  user's document, both measured deterministically:
  - `_join_at_seam` returned on the *first* skip whose overlap cleared the
    threshold, so a coincidental two-word match at skip 1 beat the real
    six-word seam at skip 3 and the six words between them were emitted twice
    -- as `aligned=True`, which is the flag the caller pins the floor from, so
    the duplicate became permanent.
  - The floor branch's splice used the same 3-word bound as the alignment
    above it, and past the fourth junk word fell through to
    `stream_join_text(protected, current)` -- a blind weld of the whole
    window, which re-emits the floor's tail. Measured on a floor ending "ist
    noch nicht fertig": 11 words for up to three junk words, 19 for four. The
    bound describes one word cut in half at a window boundary; the floor
    branch is reached only after `previous` was discarded as unalignable, so
    the window's head can hold several words of that drift.

  **"Widening it is safe there and not above" was wrong**, and this entry
  claimed it for one round. Widening the search without also bounding the
  discard inverts the first defect: taking the *longest* overlap lets a deep
  coincidence beat a shallow real seam, which dropped "ein neuer gedanke" (a
  3-word match at skip 7 against the real 2-word seam at skip 2), and a 2-word
  coincidence at skip 5 discarded an entire window of speech (floor "und und
  und", window "dann dann dann dann dann und und", result "und und und"). The
  floor being preserved verbatim bounds the damage to one window; it does not
  prevent it.

  `overlap - skip` is what gets both directions: 6-3=3 beats 2-1=1 in the
  first case, 2-2=0 beats 3-7=-4 in the second. Past
  `_WINDOW_BOUNDARY_SKIP_WORDS` a candidate must additionally score at least
  zero, because inside that bound the discard is already capped at three words
  and requiring it there would only raise skip 3's bar from two words of
  overlap to three -- and a refused alignment falls through to a replace,
  which without a floor loses the whole dictation. Past the bound nothing caps
  it, which is what the skip-5 case exploited. A candidate that fails the
  score is refused and the window is welded on whole, junk and all: bounded
  duplication is the safe side of this bound.

  Rule evaluation over 20000 randomised German merges (40-word vocabulary),
  same merges under each rule: shipped-old 17 words lost and 74129 duplicated,
  longest-overlap-wins 29 and 16712, `overlap - skip` **2 and 62893**. Better
  than the original on *both* axes, which is why this rather than a revert to
  the bound. Note what this measures: which candidate each rule picks on
  synthetic seams, not accuracy on real audio.

  **The "mean extra words 7.5 -> 2.1 ... mean lost words 21.2 -> 21.3" figures
  this entry used to give are withdrawn.** They came from a harness that was
  never committed and could not be reproduced, and the conclusion drawn from
  them -- "it does not buy the reduction with lost speech" -- is false for the
  rule they described: longest-overlap-wins loses 29 words where the original
  loses 17.
- **Punctuation is not corroboration.** `_stream_word_key` strips
  `.,;:!?)]}`, so "...", "!", "?!" and ":" all key to the empty string and
  match each other -- two of them cleared a two-word overlap threshold with no
  lexical agreement at all, and a cleared threshold is what the merge reads as
  "two overlapping windows agreed on the seam". `_substantive_word_count`
  counts only non-empty keys.
  Counted rather than refused in `_stream_words_match`: a genuine seam may
  contain a standalone mark ("hallo ... welt"), and making the mark itself
  non-matching breaks that three-word overlap down to nothing, because
  `_suffix_prefix_overlap_len` needs every word of the slice to match.

  **The gate is "at least one real word", not "enough real words to clear the
  threshold".** Applying the threshold to the substantive count -- which this
  entry described as the rule for one round -- is stricter than the threshold
  has ever been, and the extra strictness falls on real seams: an overlap of
  "praktisch ..." counts one, fails a threshold of two, and the merge then
  falls through to a *replace*. Before the first measured pause there is no
  `protected_prefix` to bound that, so it is not one window that is lost but
  the whole dictation so far. Measured on a 13-word dictation whose 8-word
  window re-heard "praktisch ...": the merge returns the window alone, so 11
  words are gone and the two in the overlap are the only survivors. (The
  commit message and an earlier version of this entry said "13 words lost".
  13 is the size of the dictation; the loss is 11.) The raw token count
  still has to clear the threshold; the substantive count only has to be
  non-zero, which is exactly what rules out a seam made of nothing but marks.
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
  (`STREAMING_SPEECH_RUN_WINDOW_MS`), and `measure_longest_speech_run_s` takes
  that bucket as a required keyword argument. It used to *default* to
  `SILENCE_GATE_WINDOW_MS` (100) -- the value this very entry explains is
  wrong for this measurement -- so the safe number was opt-in and the
  documented-wrong one was what a caller got by omission. The single
  production caller passes 20 explicitly, so it was latent rather than live;
  it is required now because this repository has already carried one threshold
  across a bucket-size change without rederiving it (the 0.15 in the table
  below), and naming the bucket at every call site makes that impossible to do
  silently. It takes the longest *unbroken* run
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
  partial. **Two failure paths run *after* the paste keystroke** on the
  synchronous WM_PASTE road — the post-paste clipboard-contention check
  and "text pasted but clipboard restore failed" — and rolling those back
  pastes the same words twice, up to the retry limit. They therefore raise
  `TextMayHaveBeenPastedError`, which the retry refuses to act on. On the
  SendInput road the restore is deferred past the call (the paste
  transaction entry), so a restore that fails there is logged rather than
  raised: the insert had already returned success. **Classification is driven by one `paste_sent`
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
  the completion signal. Each push of that flush carries
  `STREAMING_PRECONNECT_FLUSH_PUT_TIMEOUT_S` (5 s) as a wait budget, which a
  provider without a bounded queue ignores (F09 of the 2026-09-12 review):
  Deepgram's send queue holds 32 chunks, 3.2 s of audio, against the 62.5 s
  this buffer may hold, and a burst of nonblocking pushes -- measured, 33
  `put_nowait` calls in about 45 us, a hundredth of CPython's 5 ms switch
  interval -- never let the sender thread run, so chunk 33 was rejected and
  the dictation failed on a socket that had just connected. The flush
  re-checks the generation between chunks, so a retired session stops
  within one chunk's budget. The finalize joins the connect thread for at
  most `STREAMING_CONNECT_JOIN_TIMEOUT_S` (15 s), and a thread still
  running past that -- still connecting, or still handing audio to a
  sender that stopped draining -- ends the session as a failure (wave 11):
  `_await_stream_connect` answers False, the worker aborts the stream
  (which is what unblocks a provider parked in its connect wait) and
  raises a failure naming the bound, so the recording stays kept as the
  last recording, the text so far is saved, and the retired thread pushes
  nothing more (its next generation check refuses, or its next push finds
  the queue gone, within one chunk's budget). Before that the worker
  called `stop_stream()` beside the flush: the provider was stopped with
  part of the recording never handed over, answered with the text it had,
  and the dictation ended in "Done" with a transcript missing speech
  recorded before the stop, the only trace a warning in the log; on a
  handshake still running it ended in "Streaming session is not active",
  which names nothing the user can act on. The overlay says "Connecting
  to the speech service. You
  can speak now." until the stream is live, because the microphone
  genuinely is open. Consequence for tests: a stream-start failure now
  arrives through a queued signal, not on the caller's stack.
- **Stopping or aborting must retire an in-flight handshake, never race it**:
  `stop_stream()` called while `start_stream()` is still running is not a
  no-op. The provider rejects the stop because the session is not active
  *yet*, the handshake then publishes a socket nobody owns and marks it
  active, and every later dictation fails with "Streaming session already
  active" until the app restarts — from one hotkey press inside the 8 s
  window, or from a single microphone failure. `_submit_stream_finalize`
  therefore hands the connect thread to the job and
  `_finalize_stream_worker` joins it (bounded by
  `STREAMING_CONNECT_JOIN_TIMEOUT_S`, off the Qt thread; a bound that runs
  out ends the session as a failure rather than stopping beside the
  thread -- the entry above) before stopping;
  the capture-start failure path uses `_teardown_pending_stream_connect` for
  the same reason. Only the abort and capture-failure roads also *retire*
  the handshake there -- bump `_stream_connect_generation` and clear the
  buffer -- because those sessions are being abandoned; a normal stop does
  not (next entry). The bump is what keeps a late flush out of the next
  session. A stale flush must **refuse to push without clearing the
  buffer** — clearing it destroyed the live session's buffer and killed
  the new dictation.
- **A normal stop hands the handshake to the finalize, and the finalize
  reports its failure** (2026-09-16, F03 of the external review).
  `_submit_stream_finalize` used to retire the handshake as well, and that
  retired the very session being finalized: the connect thread's own
  `_flush_preconnect_buffer` failed its generation check and pushed
  nothing, and the finalize worker, which joins that thread before
  `stop_stream()`, finalized a provider that had been handed no audio.
  Measured on the real Deepgram provider with a fake socket: 20 buffered
  chunks, 0 binary frames sent, an empty transcript -- and an empty
  transcript is the silent-success branch, so the user saw "Done / No
  speech detected" with the recording marked completed and, with both
  retention settings at their default off, deleted. The stop now leaves
  the generation and the buffer alone; `_reset_streaming_state`, which
  every terminal path runs, retires them. Three consequences, each
  measured:
  - **`_stream_finalize_pending`** (set by the submit, cleared by the
    reset) mutes the connect signal's success arm: with the generation
    kept, the late `ok=True` signal passes the generation check while
    `_streaming_recording` is still True, and its "Streaming active. Speak
    now, press hotkey to finalize." replaced "Finalizing streaming
    transcript..." on a dictation the user had ended. The flag is per
    session, so the reset clears it -- left set, the *next* dictation's
    handshake landed muted and the overlay kept "Connecting to the speech
    service. You can speak now." on a stream long since connected.
  - **A handshake that fails after the stop is reported once, by the
    finalize worker.** The connect thread records the cause in
    `_stream_connect_failures` (by generation, under
    `_stream_preconnect_lock`) before it emits, and the finalize worker --
    which joins that thread first, so the join orders the record ahead of
    the read -- consumes it through `job.connect_generation`, tears the
    provider down best-effort (`_abort_stream_after_failed_connect`: a
    session whose flush failed is published, and every provider refuses a
    second one) and raises it as the finalize's own failure. The connect
    signal's `not ok` arm returns while the flag is set. Routed to
    `_on_stream_runtime_failed` as before, it tore the session down under
    the worker, whose `stop_stream()` on a provider that never started
    then arrived as a second report: "Streaming session is not active"
    painted over the invalid key, and a second failure mark on the
    recording (measured: two Error paints and two `mark_failed` calls for
    one dictation). A flush that fails after the stop takes the same road
    with the push failure as the cause. **The record is the handshake's,
    not the session's** (wave 12): a cancel during the pending finalize
    -- the hotkey or the queue row's X -- runs `_reset_streaming_state`
    while the worker is still parked in its join, and for one round that
    reset cleared the record and bumped the generation the record was
    gated on, so a failure arriving after the cancel was refused and one
    recorded before it was wiped; the worker then found nothing, called
    `stop_stream()` on a session never published, and the user read
    "Streaming session is not active" for a handshake that had failed on
    an invalid key (measured through both cancel roads). The record is
    written unconditionally under its own generation, consumed only by
    the finalize that joined that handshake, and pruned at the next
    handshake's begin to what a registered job can still read -- after
    the cancel the user may dictate again while the first finalize is
    still parked, and the next handshake may fail as well, which is why
    it is a dict and not a slot.
  - **Cancel and the capture-failure road still retire the handshake**
    (`_abort_streaming_session`, `_teardown_pending_stream_connect`): the
    session is being abandoned, and handing its audio to a provider nobody
    listens to would be the defect. The fix did not widen "the flush
    survives a stop" into "the flush survives anything", and a test pins
    that.
  - **A runtime failure that arrives after the stop is the finalize's as
    well** (wave 11). The stop does not retire the session's error
    callback -- the provider keeps it wired until its own `stop_stream()`
    takes the lock, and before that the worker is still queued or still
    joining the flush -- so a socket that died in that window passed both
    gates of `_on_stream_runtime_failed` (the token is the live session's,
    and `_streaming_recording` stays True until the result is delivered),
    and `_on_transcription_failed` painted Error, marked the recording
    failed and reset the session under the worker, whose `stop_stream()`
    then delivered its transcript as a second, competing success -- Done
    over Error, both marks on one recording -- or its own failure as a
    second Error. The slot returns while `_stream_finalize_pending` is set
    (`stream_runtime_failed_after_stop`, INFO): every provider's
    `stop_stream()` answers a dead socket with the text it has or, having
    none, with the failure it recorded, so the worker's terminal signal
    carries it exactly once.
- **Remote stream finalizes have their own worker**: `_executor` stays
  `max_workers=1` so two local models never load at once, but a remote finalize
  loads nothing — `stop_stream()` drains a socket. Sharing that queue meant
  pressing stop on an AssemblyAI or Deepgram dictation left it "Processing"
  until an unrelated local batch transcription ahead of it had finished.
  `_stream_finalize_executor_for` routes by engine; local streaming still
  finalizes on the shared worker because it really does re-transcribe audio.
- **Every arm that abandons a stream start must tear the handshake down, not
  just release the lease.** `_begin_stream_connect` has already spawned it, so
  releasing alone lets `start_stream` publish a session nobody owns; every
  provider then refuses the next one with "Streaming session already active"
  and a remote socket stays open and billed until restart. All three arms of
  `_start_streaming_recording` do this now, teardown before release and unable
  to raise past it.
- **A streaming runtime failure names the session it belongs to, and one
  from a retired session is ignored** (2026-09-16, F04 of the external
  review). `stream_runtime_failed` carries `(token, text)`: every
  `start_stream` gets an `on_error` closure holding its own handshake's
  `connect_token`, `_stream_session_token` is that same object for the
  live session -- set by `_begin_stream_connect`, cleared by every path
  that ends a session (`_reset_streaming_state`,
  `_teardown_pending_stream_connect`) -- and `_on_stream_runtime_failed`
  drops a token that is not the live one before its activity test. That
  test (`_audio_capture is not None or ...`) cannot tell whose failure it
  is: an ordinary batch recording satisfies the first disjunct, so a
  provider that fired `on_error` after its session was cancelled stopped
  the microphone of the recording the user had started since and painted
  Error over Listening; the real Nemotron worker produced exactly that
  callback for an aborted run. Three details:
  - **The token is captured by the closure, not read when the callback
    fires**, or a retired provider would be answered with the current
    session's identity -- the same defect one level down.
  - **`_stream_connect_token` and `_stream_session_token` are one object
    under two names with different lifetimes**: the first answers "has a
    newer handshake replaced mine", which a detached aborter still asks
    after its session was torn down, so only `_begin_stream_connect`
    writes it; the second answers "is the session that produced this
    event still the live one", so every session end clears it.
  - **Retiring a failed handshake is one guarded write**
    (`_retire_failed_stream_connect`): `_stream_chunk_error_reported` used
    to be set with no generation check while the buffer drop beside it
    had one, so session A's late handshake failure set the flag for
    session B, which then dropped every audio chunk for the whole
    dictation and delivered the one chunk it had as a successful "Done".
  The activity test is kept behind the identity test, not replaced by it:
  the token is set before the capture exists, and the arm that handles a
  failing `_build_audio_capture` returns without a reset, so a token can
  be current while no session runs. And **Nemotron's stream worker does
  not call `on_error` for a run that was aborted** -- its two normal exits
  already returned silently for one, the `except` arm did not; the failure
  stays on `run.result.error` for whoever still holds the run.
- **A swallowed streaming partial callback is logged once per session.**
  That callback is what puts live text on screen and into the document, so
  a bare `pass` made a dead live-insertion path indistinguishable from a
  user who had simply stopped talking. It must stay swallowed -- one lost
  delivery costs nothing, the next partial carries the whole merged text
  again -- and it must stay latched behind `partial_callback_failed`, the
  same shape as `noise_floor_warned`, because it runs about every 350 ms.
  **The same rule now covers the AssemblyAI and Deepgram providers**, which
  had six bare `except Exception: pass` arms between them
  (`_stream_partial_callback_failed`, cleared in
  `_reset_stream_state_locked`). The error callback needs no latch --
  `_stream_error_reported` already bounds it to once per session -- but needs
  the log most, being the only path a stream failure takes to the user. Two
  teardown arms are logged for a less obvious reason: a `disconnect` that
  raises leaves the AssemblyAI SDK's reader threads running against a dead
  socket, a refused `ws.close()` leaves a Deepgram connection open and
  billed, and the bounded joins around them cannot tell either apart from a
  clean shutdown. A Deepgram frame that will not parse as JSON gets its own
  latch, because it may have carried a transcript. The lock those arms take
  is the one the enclosing handler already takes on its normal path, so it
  adds no new ordering.
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
