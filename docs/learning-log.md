# Learning Log

Project history, decisions, and operational learnings. Referenced by `AGENTS.md`.
Agents and developers: use this as a knowledge base for past issues and solutions.

## 2026-08-30 (rounds thirteen to sixteen)

Everything below was re-verified by running it, not by reading an agent's
report. Where a claim could not be measured, it says so.

### Round thirteen to fifteen

- **`os.replace` onto an existing directory does not raise `FileExistsError`.**
  `scripts/import_model.py` published a staged snapshot with
  `staging_dir.replace(snapshot_dir)` and caught `FileExistsError` to handle a
  second importer that had published the same content first. Measured on this
  machine: Windows raises `PermissionError` (WinError 5) for that call, both
  when the destination is empty and when it is not, and POSIX raises `OSError`
  with `ENOTEMPTY` -- `FileExistsError` never appears. So the guard was
  unreachable and a concurrent import failed with an access-denied traceback.
  The arm is now `except OSError` with an `is_dir()` re-check, and the comment
  says which exception each platform really raises so the next reader does not
  "simplify" it back.

- **A WM_PASTE that times out may still paste.** `_send_wm_paste` treated a
  `SendMessageTimeoutW` failure as a clean "nothing happened" and let the
  caller restore the clipboard, so a busy target processed the message
  afterwards and pasted the *restored old* clipboard. Measured against a real
  Win32 window whose WM_PASTE handler sleeps one second: `GetLastError()` is
  `ERROR_TIMEOUT` (1460) both when the target never received the message and
  when it is mid-handler, so the two are indistinguishable and the safe reading
  is "it may have landed". An invalid handle is 1400 and stays a clean failure.
  The timeout now raises `TextMayHaveBeenPastedError`, the same class the
  SendInput path already uses for exactly this ambiguity.

- **A rolling window that cannot be aligned threw away the part of the floor it
  re-emitted.** `merge_rolling_window_transcript`'s floor fallback appended the
  whole unalignable window after `protected_prefix`, duplicating the words the
  window shared with the floor. The seam search the normal path uses
  (`_join_at_seam`, extracted for this) is now tried against the floor as well
  before falling back to a plain join.

- **The remote providers swallowed every callback failure.** Six bare
  `except Exception: pass` arms across the AssemblyAI and Deepgram providers.
  The worst is the partial callback: it is what puts live text on screen and
  into the document, so a dead one was indistinguishable from a user who had
  stopped talking. Swallowing is still right -- the next partial carries the
  whole combined text again -- so the fix is a log, latched per session the
  same way `partial_callback_failed` already is in the local faster-whisper
  path, because it runs on every turn revision. The error callback needs no
  latch (`_stream_error_reported` already bounds it) but needs the log most: it
  is the only path a stream failure takes to the user. Two teardown arms matter
  for a reason that is not obvious -- a `disconnect` that raises leaves the
  SDK's reader threads running against a dead socket, and a refused
  `ws.close()` leaves a Deepgram connection open and billed, and the bounded
  joins around them cannot tell either apart from a clean shutdown.
  Mutation testing left two survivors, both equivalent mutants: removing the
  latch reset from `start_stream` changes nothing because every write of
  `_stream_state = "idle"` is in `__init__` or `_reset_stream_state_locked`
  (checked by grepping every writer), and `start_stream` refuses to run in any
  other state. The redundant re-initialization is kept because the five
  neighbouring assignments are redundant for the same reason.

- **`HTTPError.reason` is the status phrase, and four providers reported only
  that.** "API returned HTTP 400: Bad Request" throws away the one part that
  says what to change. Azure alone read the body; its private reader is now the
  shared `read_http_error_detail` in `_http_utils`, used by OpenAI, ElevenLabs,
  Deepgram and AssemblyAI too. Capped at 300 characters so a provider cannot
  push an HTML error page into a dialog, and it falls back to the status phrase
  when the body is empty or its read raises.

- **AssemblyAI's connection test was the only REST call in the app without a
  TLS context.** Behind a TLS-intercepting proxy it therefore failed while
  transcription itself worked: the SDK goes through `requests`, which reads
  `REQUESTS_CA_BUNDLE`, and `urllib` does not. Both of its hand-written SSL
  messages told the user to set `REQUESTS_CA_BUNDLE`, which could not fix the
  urllib call that had just failed; they now use the shared
  `format_ssl_error_message`, which names `SSL_CERT_FILE` as well.

- **The AssemblyAI SDK's `wait_for_completion` is `while True:` with no bound of
  any kind.** A job the service leaves in `queued` therefore held the app's
  single `max_workers=1` transcription worker for the rest of the session --
  and blocked process exit with it. That second half is worth stating precisely
  because it is not obvious from the shutdown code: `ThreadPoolExecutor`
  registers an exit handler (`threading._register_atexit`) that *joins* its
  worker threads, and
  `shutdown(wait=False, cancel_futures=True)` does not release one that has
  already started. Measured with a throwaway script: the interpreter never
  exited. In the app that leaves a process holding the single-instance lock, so
  the user cannot even restart it. The provider now submits and polls itself
  against `ASSEMBLYAI_BATCH_MAX_WAIT_S`. Terminal status is the positive test
  rather than `queued`/`processing`, so a status this SDK version does not know
  is waited out instead of mistaken for a finished job; and a submit that comes
  back without a transcript id fails at once rather than spending the whole
  budget on `get_by_id("")`.

- **A `finally` runs before the code after the statement it belongs to.** My own
  fix for the benchmark's terminal signal put the success emit after the whole
  `try/except/finally`, so the `finally`'s last-resort failure emit fired first
  on every successful run. Caught by reading back the applied patch rather than
  by the tests, which is the lesson: the success path belongs in `else:`.

- **A test whose two branches happen to agree tests nothing.** The
  language-mode regression test picked "the first non-`auto` option", which for
  the `small` model is `de` -- exactly the value already in the store -- so the
  mutant and the correct code returned the same answer. It now uses two
  distinct picks.

### Round sixteen

- **A thread that will not start left Settings busy for the rest of the
  session.** Six `Thread.start()` calls in the settings dialog sat outside any
  guard. `RuntimeError` from a starved interpreter arrives *after* the busy
  marker is written, and nothing clears that marker but the completion signal
  the thread will never send -- so the control stayed disabled and, because
  `_background_work_active()` reads those same markers, `reload_from_store()`
  was deferred silently and permanently. The dialog is never recreated, so
  "permanently" means until the app is restarted. Each site now rolls back what
  it had already set; the download queue reuses the teardown its own crash arm
  performs, which is what hands the coordinator's explicit interest back so the
  model's partial files stay cleanable. Two of the six arms in the first draft
  called methods that do not exist (`_abandon_local_model_download_queue`,
  `_on_import_transcription_finished`) -- caught by grepping every name in the
  patch against the source, not by the tests, which never reached those arms.
  Thirteen mutations, all detected.

- **The inventory called a faster-whisper model cached where it cannot be
  loaded.** `find_cached_models(model_dir)` searched the configured Model Dir
  *and* the default Hugging Face cache, and accepted both the `models--<repo>`
  and a flat layout. The app always passes a size name, so `WhisperModel` calls
  `snapshot_download(repo_id, cache_dir=download_root)` -- one cache root, one
  layout. Measured: with `tiny` present only in the default cache and a custom
  Model Dir configured, `find_cached_models` returned `['tiny']` while the
  download destination held no snapshot; the same for a flat folder. The Local
  tab therefore showed the model as installed and disabled its Download button
  while the next dictation silently fetched it again, and offline mode could
  not load it at all. The inventory now answers from
  `download_destination_dir`, which is what the load path's own pre-fetch
  already gates on, so the two cannot disagree. The ONNX half is unchanged and
  deliberately still accepts the other roots -- those models really do load
  from them. Two existing tests pinned the old behaviour and were rewritten;
  four mutations, all detected.

- **Double-clicking a benchmark history row during a run merged the two.**
  `Load Selected` is disabled while `_active_benchmark_thread` is set; the
  table's double-click was not, and loading replaces `_current_benchmark_cases`
  -- the list the next finished case appends to. So the stored run's cases and
  the live one's ended up in one results table and one live summary, and the
  stored entry's environment could reach the saved history entry of the live
  run whenever the worker had none of its own. The handler now carries the same
  gate and says why.

- **Every finished case threw the reader back to run 1.** `set_live_results`
  runs once per completed case and `_set_transcript_rows` ended in
  `selectRow(0)`. Measured with the real widget: reading run 3 of the first
  model, a second case finishing moved the selection to row 0 and replaced the
  transcript pane. The selection is now restored by the row's
  `model / device / run` identity -- not by index, because a finished case can
  insert rows above the selected one -- with row 0 as the fallback when the
  opened row is gone.

- **An overlay change made an untouched Save look like a real one.** The
  overlay writes `overlay_opacity_percent`, `overlay_always_on_top` and
  `language_mode` straight to the store while Settings is open, and the save
  deliberately reads exactly those three back from the store -- but compared
  the result against `_loaded_settings`, the dialog-open snapshot. Measured:
  moving the overlay's opacity slider and then pressing Save without touching
  anything wrote the file with the bytes already in it, reported
  "Settings saved", and emitted `settings_changed`, which costs four global
  hotkey unregister/re-register cycles. Both save paths now compare against
  what is on disk. The two existing tests for this asserted that a write had
  happened *carrying* the overlay's value; they now assert the property itself
  -- that the store still holds it -- which is satisfied by writing nothing.
  One mutation survived: a line adopting the fresh settings as the new snapshot
  after a no-op save. Nothing reads `_loaded_settings` for an overlay-owned
  field (the History tab's own write path already loads fresh), so the line
  defended against nothing and was removed rather than given a test.

### Earlier in the same day, before the rounds above

- **A failed temp-file write leaked the file it had already created.**
  `NamedTemporaryFile(delete=False)` creates the file before it returns, and
  all four batch paths that spool audio to `%TEMP%` recorded the path *after*
  the write -- so a write that failed on a full disk or a quota left the path
  variable `None` and the cleanup skipped a real file, once per failed
  dictation. faster-whisper, the Cohere/Granite ONNX runtime, AssemblyAI and
  Groq all had it.

- **A CPU fallback restarted the Node child on every batch.** The ONNX runtime
  reports `fallbackErrors` plus `device: cpu` on any machine without a usable
  GPU, which is the *normal* answer there, and the restart-on-fallback path
  therefore paid a full model load per dictation forever.
  `_MAX_CPU_FALLBACK_RESTARTS` bounds it to one.

- **The download lock's in-process registration could be stranded.**
  `_close_handle` is the only place that removes a key from
  `_HELD_RESOURCES`, and it finds the key through `self._held_key` -- which
  was assigned *after* the registration. An exception in between left the key
  registered with nothing able to remove it, so every later `acquire` for that
  resource raised `LockHeldInThisProcess` and no download could start again
  until the app restarted. The assignment moved first (the reverse order is
  harmless: `discard` ignores a key that was never added), and `release()`'s
  early return for an already-closed handle now goes through `_close_handle`
  too, since a bare `return` there is one more way to leave a key behind.

- **A delayed writer painted over a finished result.** The preload progress
  poll rewrites the overlay every 600 ms and did not check what was already
  there, so a `Done` carrying the transcript or an `Error` carrying the reason
  plus its Retry or Insert action could be replaced by a loading line. The poll
  now reads `OverlayUI.state`, and the test doubles mirror it.

## 2026-08-30 (twelfth round: three ways a dictation disappeared)

Every defect below was proved before it was fixed, by running the real code
and printing what it produced. Two of the three had already been reported as
"a known gap" in `AGENTS.md`, which is not the same as being harmless.

### A decode as slow as the window replaced the whole transcript

Streaming with faster-whisper decodes the trailing `stream_partial_window_s`
(8 s) of audio on every partial. When the decode itself takes about as long as
that window -- a large model on a slow machine, RTF near 1 -- the buffer
advances further than the window is wide, so two consecutive windows share no
audio at all. `merge_rolling_window` then finds no seam, falls through to its
replace branch, and with continuous speech `silent_seconds` never accumulates,
so no pause has ever pinned `segment_floor` and the replace is unbounded.

Measured with an 8 s window and 9 s between decodes: `'erster teil der
nachricht'` became `'und dann kam etwas ganz anderes'`. Whatever the length of
the dictation, only the last window survived. The fast finalizer had the same
seam and lost everything in one step at the moment the text is handed over.

The decode now reports the byte range it covered (`last_window_start` /
`last_window_end`), and a window that starts at or after the previous window's
end is treated as a new segment: the floor is pinned and the text is appended.
That is provably correct rather than heuristic -- nothing already transcribed
can be revised by a window that shares none of its audio. The speech in the
gap was never decoded and is lost either way; a warning says so once per
session.

Writing the test taught something the fix did not: the first pair of fake
transcripts shared three trailing words, and the merge's re-anchor search
found a seam the audio says cannot exist, so the window was silently swallowed
instead of replacing anything. A different loss, and one that would have made
the test pass for the wrong reason.

### The stream worker could die without saying anything

Only the decode inside `_maybe_emit_partial` and the finalization were
guarded. The energy meters, the merge and the buffer append were not, so an
exception in any of them simply ended the thread. `stop_stream` then joined a
dead worker, found no error and an empty `final_text`, and reported the whole
dictation as "No speech detected". A windowed build has no stderr, so
`threading.excepthook` printed the traceback nowhere at all. Proved by making
`_stream_slice_is_quiet` raise on the worker thread: the worker died and
`stop_stream` returned `''` with no exception.

### An empty finalize threw away the live transcript

An explicit abort and a dying stream runtime both rescue the best-known live
text before the reset wipes it. A finalize that returned nothing did not: the
overlay said "No speech detected", history got no entry, and the dictation
survived only as the part already pasted into the document. Both AssemblyAI
and Deepgram can return an empty string from `stop_stream` after a socket
problem, which is exactly when the live text is the only copy left.

### Clear queue pasted the queue it was clearing

`clear_transcription_queue` cancelled the rows one at a time, and a single
row's cancel deliberately flushes the deferred inserts beside it -- otherwise
the X on one row would strand the finished transcripts on the others. On the
first iteration those others are exactly what is about to be cancelled.
Measured with two finished transcripts and one running job: `transcript B.`
was typed into the focused window, while `transcript A.`, reached before the
flush, was discarded. Which ones survived depended purely on the loop order.
Every job is now stopped before anything is delivered.

### Three smaller ones in the same pass

- `Executor.submit` raises once the pool is shut down and when a worker thread
  cannot be started. Neither submit site handled it, so the exception escaped
  into the Qt slot, the queue row sat at "Processing" for the rest of the
  session, and the streaming job's runtime lease -- which only its worker's
  `finally` releases -- held the shared runtime lock for the process lifetime.
- `_get_or_create_transcriber` closed the cached runtime while it was still
  installed as the cache. `create_transcriber` raises for a missing API key or
  an absent model, and the closed runtime then stayed under its old key, so
  switching the settings back handed it to the next dictation. AGENTS.md
  already stated this rule for two other sites; this was the third.
- A cancelled Local-tab download deleted the partial files a *preload* was
  parked to resume from, because the preload registers implicit interest and
  the guard checked explicit interest only. Downloads now register every
  parked caller.

### The overlay clipped its own buttons above 9 pt

Windows' Accessibility > "Text size" raises the application font's point size
without changing the DPI, so Qt's device-pixel-ratio does not scale a pixel
constant with it. Measured: at 11.2 pt the Record button needs 82 px against
its pinned 78 and Reset Pos 80 against 74; at 13.5 pt nine buttons clip and
every one of them is 4 px too short; at 18 pt Record needs 108x34 against
78x24. At the default 9 pt everything already fits.

The sizes are now measured from every caption a button can show. Two details
mattered: the pass has to run *after* the first `set_state`, because only then
does the container carry the stylesheet whose padding and border a button's
`sizeHint` includes -- measuring in the constructor came out 1-2 px short at
every scale -- and the header flanks have to be balanced from the resulting
sizes rather than before them.

### Four claims that did not survive being checked

- The Parakeet entry in the model picker said "CPU, fastest". This repository
  formally retracted that superlative: `tiny` measured 0.033 RTF against
  Parakeet's 0.043 in the same run.
- A comment said that clearing the orphaned runtime before closing it restores
  the `TranscriptionCanceled` the branch exists to deliver. It does not -- a
  close that raises skips that `raise` either way. What it prevents is the
  double close and the loss of the first failure.
- A comment said the history import catches `UnicodeDecodeError` "before
  `json.JSONDecodeError` would be". It comes from `read_text`, before
  `json.loads` runs at all, and the two are siblings under `ValueError`, so
  clause order decides nothing.
- The smoke test said its loop reaches the backup "precisely when the primary
  raised `OSError`". It also reaches it when the primary is missing, is not
  valid UTF-8 or JSON, or is a JSON value that is not an object -- and in the
  last two cases copying the primary would hand the diagnostic exactly the
  file the app is about to discard.

The 953,446-probe measurement was recorded with three different provenances in
four places. Re-measured: a pure-Python stub of the probe runs the loop at
15.6 M iterations/s, 33x too fast to have produced it, while the real
`SendMessageTimeoutW` against a non-window handle gives 0.99-1.01 M probes in
2.000 s -- the same shape as the recorded 478 k/s. The number is real; the
"fake that always reports hung" sentence around it was not.

### Two tests that could not fail

- `test_a_deleted_primary_does_not_get_the_backup_overwritten` asserted
  `backup unchanged OR read() == saved`, and the recovery its sibling test
  already pins makes the second half true every time. Restated as surviving
  the loss twice.
- A new assertion that a cleared cache key forces a preload passed with or
  without the fix, because `_local_model_preload_needed` returns True on its
  own when no preload result has been recorded yet. The test now records one
  first.

Both were caught by mutation testing, which also found that a test for the
partials cleanup was satisfied by the real cleanup returning `(0, 0)` for a
model with nothing on disk -- it never observed whether the guard held.

## 2026-08-28 (tenth and eleventh rounds: two silent data losses, and a hang)

Two of the four defects in this pair of rounds destroyed user data with no
error message anywhere. Neither was found by reviewing a diff: both were found
by reading a file end to end and asking what happens to each branch when the
input is hostile.

### An unreadable key file was rewritten as an empty one

`KeyringSecretStore`'s insecure fallback (used where the Windows credential
store is blocked by policy) read its JSON through one `try/except` that
returned `{}` for *every* failure. A file that could not be read -- a
permission error, a lock held by a backup tool, a truncated write after a
power loss -- was therefore indistinguishable from a file that does not exist
yet. The next `set_api_key` then took that `{}` as the current contents, added
the one new key, and wrote the result back: every other provider's key gone,
silently, with the UI reporting success.

The reader now returns `(payload, damaged)`. `_set_insecure_api_key` raises a
`RuntimeError` naming the path rather than writing over a damaged file, and
`delete_api_key(provider, strict=True)` does the same; the stale-copy cleanup
that runs after a successful keyring write stays deliberately tolerant,
because failing there would undo a save that already succeeded.

### A settings file that is not UTF-8 killed the app at startup

`persistence.read_json_with_recovery` caught `json.JSONDecodeError` when
reading its backup candidates. `Path.read_text(encoding="utf-8")` does not
raise that -- it raises `UnicodeDecodeError`. Both are `ValueError`s, and only
the JSON one was named, so a settings, history or inventory file containing a
single non-UTF-8 byte propagated out of the store constructor and the app died
before its first window. The recovery path that exists precisely for a damaged
file could not run, because the damage was of the wrong kind.

Widened to `except (OSError, ValueError)` with a comment saying why both
members are meant, in `persistence.py` and in
`local_model_scan.load_scan_cached_models_payload`.
`transcript_history.import_from_file` got its own `UnicodeDecodeError` arm
with a message the user can act on, since there the file is one they chose.

The test fixture for this was itself wrong on the first attempt:
`json.dumps` defaults to `ensure_ascii=True`, so the "non-UTF-8" payload was
pure ASCII and the test passed against the unfixed code. `ensure_ascii=False`
was the difference between a test and a decoration.

### A hung target application froze the app at 100% CPU

`wait_for_paste_target_ready` polls the target window with
`SendMessageTimeoutW(..., SMTO_ABORTIFHUNG, ...)` before restoring the
clipboard. `SMTO_ABORTIFHUNG` makes that call return **immediately** for a
hung target rather than waiting out the timeout -- so the loop had no delay in
it at all. Measured with the real Win32 probe against a handle that names no
window, which returns just as fast: 953,446 probes in one budget window, one
core pinned, the Qt thread unavailable for the whole time. A `poll_interval_s`
(default 10 ms) and an `_is_window` early-out fixed it.

That provenance was recorded here as "a fake that always reports hung", which
does not survive being checked. Re-measured on 2026-08-30 on the same machine:
a pure-Python stub of the probe runs the loop at 15.6 M iterations/s, 33x too
fast to produce 953,446 in a 2 s budget, while the real
`SendMessageTimeoutW` against a non-window handle gives 0.99-1.01 M probes in
2.000 s (494-507 k/s) -- the same shape as the recorded 478 k/s. The number is
real; only the sentence about where it came from was not.

The same file had a second defect one layer down. `SendInput` can return a
short count, and the paste batch is `[Ctrl down, V down, V up, Ctrl up]`:
applications paste on the **key-down**, so two delivered events already mean
the text was pasted. A short send was treated as a clean failure, so
`send_paste_with_mode`'s auto path fell through to `WM_PASTE` and pasted a
second time, and the clipboard restore ran as if nothing had happened.
`_send_input_batch` now takes `committed_after` and raises
`TextMayHaveBeenPastedError` past that point, which the auto path re-raises
instead of retrying and which suppresses the restore.

### Every runtime hand-back is now unskippable

`_transcribe_worker`'s `finally` cleared diagnostics and cancel hooks *before*
releasing the runtime lease, because `AGENTS.md` requires exactly that order.
Anything raising in that bookkeeping therefore skipped the release and
stranded `_transcriber_runtime_lock` for the process lifetime -- after which
every dictation loads its own isolated runtime and a preload waits forever.
The order is kept; the bookkeeping is now wrapped so that its own failure is
logged and the release still runs from an inner `finally`.

Related, same commit: `_reset_transcriber_cache_locked` detaches the cached
transcriber before closing it. Closing first meant a `close()` that raises
left the dead runtime still installed as the cache, and the next dictation
used it.

### The tray menu handed back our own window as the paste target

`get_foreground_window` ended in `return self._remembered_foreign_window() or
hwnd`. The `or hwnd` fires exactly when one of our own tool windows is in
front and nothing foreign has been remembered yet -- which on a fresh session
is every path before the first recording. Starting the first dictation from
the tray menu hits it, because the notification-icon contract requires
`SetForegroundWindow` on the hidden 0x0 host window before `TrackPopupMenu`.

The transcript then went to a window that cannot take text. Worse than the
lost paste: `restore_target_window` calls `ShowWindow(SW_SHOW)` on the target,
which makes that helper window *visible*, so it passes the own-non-target
predicate from then on and is cached as the last foreign window for the rest
of the session.

Returning `None` is the fix -- the insert path reports "no target" rather than
pasting into nothing. The tray's `activated` signal, which fires before the
menu takes the foreground, now calls a new best-effort
`note_foreground_window()` so the window the user was actually working in is
remembered first.

### Two findings were reviewed and deliberately not changed

- The post-paste clipboard-contention warning is still raised with
  `keep_transcript_in_clipboard` enabled. It looks redundant -- nothing is
  restored in that mode -- but a user copy landing inside the window can still
  make the target paste the wrong text, and that is worth reporting.
- `allow_clipboard_fallback` is not honoured on the
  `TextMayHaveBeenPastedError` re-raise. Currently unreachable: no caller
  combines the two. Recorded rather than closed, so the next person to add
  such a caller finds it written down.

### Method notes

Every fix in these two rounds was mutation-checked: the fixed line is reverted
to its broken form and the test suite must go red. Three survivors were found
this way and each was closed with an additional test before moving on -- the
resume-path eviction order, the `single_group` coalescing key, and the two
gaps in the retranscribe reservation guard. A mutation that survives is not a
verdict on the code; it is a verdict on the test that was supposed to protect
it.

One survivor turned out to be an artifact of the harness rather than a gap:
the `-k` selector chosen for the run did not include the test that does catch
the mutation. Widening the selector to the whole file settled it. Check the
selector before believing a survivor.

## 2026-08-28 (ninth round: the measuring instruments were wrong)

Two of this round's findings were not about the code at all. They were about
the tools used to check the code, and both had been quietly producing wrong
answers for several rounds.

### The bytecode cache made mutation testing report random survivors

A batch of four mutations, run one after another, reported exactly one
"SURVIVED" -- and a different one on every run. The mutations were fine. The
harness was not: `spec_from_file_location` and ordinary imports use
`__pycache__`, and a `.pyc` counts as current when the source's mtime **in
whole seconds** and its size both match. Two mutations of one file that happen
to produce the same byte count, written inside the same second, make the second
run execute the *first* one's bytecode.

Measured: the four mutations produced exactly two colliding pairs, 10018 and
10015 bytes. So the run was not flaky in the usual sense -- it was
deterministic given the second boundary, which is why re-running "fixed" it and
moved the bogus result somewhere else.

Every mutation check in this project now runs with `PYTHONDONTWRITEBYTECODE=1`
and deletes the target's `.pyc` first. The direction of the error matters: a
false SURVIVED wastes an investigation, but a false DETECTED -- stale bytecode
from the *previous* mutation breaking the test -- would have signed off a test
that checks nothing. Both were possible.

The harness also has to survive being killed. A two-minute command timeout
killed it mid-case and left a mutated `controller.py` in the working tree with
nothing saying so; it was caught by `git status` before it could be committed.
It now writes a sentinel naming the file it is about to mutate and refuses to
start while one exists -- which promptly earned its keep on the next kill.

Two smaller harness rules came out of the same afternoon. A revert has to be
**one atomic replacement**: splitting the "lease built outside the guard" revert
into two cases produced an `_acquire_transcriber_runtime` that fell through
still holding the shared lock, so the run hung rather than reporting anything.
And scratch filenames need a namespace -- the review subagents share this
session's scratch directory, and one of them wrote its own `mutate.py` over the
harness between two runs.

### `QLabel.heightForWidth(w)` is not a function of `w`

The retranscribe note's reservation was justified by a measured pixel pair:
`IBM Granite Speech 4.1 2B Plus` (159 px) wrapping to 45 px while the narrower
`NVIDIA Nemotron 3.5 ASR 0.6B` (157 px) wrapped to 60. A reviewer could not
reproduce it. Neither could I -- three times, in three different ways, each
disagreeing with the last:

- A bare `QLabel` carrying the same stylesheet: reproduced the 45/60 split, but
  for different names than the real dialog does.
- The real dialog's label, sweeping the *argument* to `heightForWidth`: found a
  15 px shortfall at one width.
- The real dialog's label, **resized to each width before measuring**: found no
  shortfall at any width from 200 to 1100.

The third is the correct one, and the difference is the point:
`heightForWidth(476)` returned 60 px with the label 556 px wide and 90 px with
it 476 px wide. The same call, the same argument, two answers. A sweep that
varies only the argument is reading a cache.

So the comment, its test docstring and the `AGENTS.md` entry had all been
carrying a number produced by an instrument that does not measure what it looks
like it measures. They now say what reproduces, including the part that is
uncomfortable: **no under-reservation from the `horizontalAdvance` shortcut can
be produced at any reachable width with today's names.** The reason to measure
every candidate is not that the shortcut has been caught failing -- it is that
measuring all fifteen costs 1.26 ms on a cache miss and cannot be wrong.

A related correction in the same commit: the delete-prompt comment blamed
dialog *width* ("a long absolute path does not wrap at a space, so the dialog
grows to the longest path"). Measured, the width is constant at 502 px whether
8 folders are listed or 80. The height is what grows -- about 16 px per folder,
931 px at the realistic worst case of 52, crossing a 1080p usable height at 60.
The bound was right; the stated reason was not.

### What actually was a code defect

- A cancel that surfaces as a plain exception (the Cohere/Granite Node runtime
  kills its child, which raises `TranscriptionError`) returned from the preload
  arm *above* the runtime condemnation, so a half-loaded runtime stayed cached
  and was recorded with the success sentinel. The next dictation used it and
  never retried the load. The two arms on either side both condemn.
- The isolated branch of `_acquire_transcriber_runtime` created a transcriber
  and, if the lease constructor raised, dropped it -- a Node child process or
  ONNX session with nothing left able to close it.
- `_start_streaming_recording`'s outer guard still caught only `Exception`,
  one frame above two guards that had been widened to `BaseException` for
  exactly that reason.
- `model_download_coordinator._acquire_cache_lock` holds the machine-wide OS
  lock before storing it. A raise in between strands a real kernel lock with no
  reference, and unlike the in-process slot that blocks *other processes* --
  the benchmark worker, `scripts/download_model.py`, a second user of the same
  Model Dir -- until this process exits.
- The label-size tolerance introduced last round, `max(5 MB, 0.5%)`, accepted
  the very drift its docstring cited as its reason (`tiny` at 75 against 78)
  and was looser than 5 MB for every GB model, reaching 15.5 MB on `large-v3`.

Two of the previous round's fixes also turned out to be untested, found by
re-running its mutations through the corrected harness.
`_start_streaming_recording`
has **two** capture-failure arms and they were widened together, but the test
fails `_build_audio_capture`, which returns before `capture.start()` is ever
reached -- so reverting the second arm's guard left the whole suite green.
Reaching it needs a capture that builds and then refuses to start, which is
what a microphone held by another application actually does. The shared branch
of `_acquire_transcriber_runtime` had the same gap: the isolated branch has a
test for a raising lease constructor, the branch that actually holds the lock
did not.

### The same two statements, four frames apart

A reviewer of the round-9 controller work found that the guard I had just
added to `_acquire_transcriber_runtime` was missing one frame further out, in
`_TranscriberRuntimeLease.release()` -- close, then hand back, no `finally`.
Measured: `lock_reacquired_after_release = False`, and because `_released` is
set before either statement, the retry is a no-op.

That one omission was the root of three separate symptoms, which is what makes
it worth recording. All three workers -- `_transcribe_worker`,
`_finalize_stream_worker`, `_preload_model_worker` -- call `release()` from a
`finally` that sits *outside* their own `except BaseException` arm and emit
their terminal signal *after* it. So a close that raised did not merely fail to
close: it took the transcript with it. Measured on `_transcribe_worker`:
`ready = []`, `failed = []`, exception escaped, overlay stuck in Processing
with no error and no Retry. On `_finalize_stream_worker`: nothing emitted, so
`_streaming_recording` stays True and every later hotkey press is refused.

The fix is one `try/except BaseException/finally` in `release()`, and it makes
that method a true cleanup primitive: it always hands back and never raises.

Three smaller ones from the same review, each a placement rather than a
mechanism:

- the shutdown branch of `_acquire_transcriber_runtime` cleared its `orphan`
  marker *after* the close, so a close that raised let the outer arm close the
  same runtime a second time and the second raise replaced the first;
- the `BaseException` arm added to `_start_streaming_recording` released the
  lease but did not tear the handshake down, which its two sibling arms do
  precisely because `start_stream` otherwise publishes a session nobody owns;
- the justification I wrote beside the preload fix was wrong. It named the
  Cohere/Granite child-kill as the case that reaches the `except Exception`
  arm. Checked: that raises `TranscriptionCanceled`, handled by the arm above,
  and it lives in `transcribe_batch` -- that runtime's `preload_model` is
  `_ensure_process()` with no cancel check at all. The fix is right; the
  sentence next to it was not, which is the third time this session that a
  correct change shipped with unreproducible reasoning attached.

### Escaping is not validating

The XLSX export runs every cell through `saxutils.escape`, which is the right
call and looks like the whole job. It rewrites `&`, `<` and `>`. XML 1.0 also
forbids most control characters outright -- they cannot be escaped, they
simply may not appear -- so a single NUL, BEL or vertical tab produced a
worksheet that will not parse, packed into a `.xlsx` that was written with no
error at all and that Excel refuses to open.

Nothing a user types reaches those cells. What does is `runtime_details` built
from a runtime's own error output, the environment strings read off the
system, and transcripts returned by remote providers, which is why it had not
been seen.

The fix had to be narrow in both directions. Replacing only the forbidden code
points keeps a German transcript and an emoji intact; stripping non-ASCII
"to be safe" would have traded a file that cannot be opened for one that is
quietly wrong, which is worse. The test therefore compares the round-tripped
cell text against an exact expectation rather than asserting that the file
parses -- and the mutation that strips everything above ASCII is caught by the
umlaut case, not by the control-character ones.

### A backup nothing ever reads

Every persisted store here writes a `.bak` beside itself, and
`load_json_with_backup` prefers the primary and falls back to it. Five of them
then opened their load with `if not path.exists(): return <empty>` -- so the
fallback covered a primary that would not *parse* and not a primary that was
*gone*, which is the more likely of the two.

The consequence is not that the data is invisible; it is that the data is
destroyed. Measured on the transcript history: five entries, delete the
primary, load returns 0, and the next dictation saves that empty list over the
backup, leaving 1. `settings_store` does it inside the load itself -- a
missing primary writes defaults and refreshes the `.bak` in the same call.

Nothing was wrong with the backup mechanism, the atomic writes, or the
recovery path; each was correct and tested. The defect was one guard placed
in front of all of it, and it read as an obvious fast path.

Worth noticing about how it was found: not by reviewing a diff. It came from
reading a module nobody had changed, asking what the `.bak` is actually for,
and then testing that instead of testing the code that was in front of me.
Nine rounds of review over recent changes had not touched it, because it was
not a recent change.

### The instrument was the reservation itself

Four versions of one comment, three of them wrong, and every wrong version
came from the same unexamined assumption: that reading
`note.heightForWidth(w)` tells you how tall the text wraps.

`QLabelPrivate::sizeForWidth` ends in `.expandedTo(minimumSize())`. The result
is floored by the label's own minimum height -- which is exactly what the
reservation installs. Measured on a bare label, one identical call: 15 px at
`minimumHeight() == 0`, 400 px at 400, 15 px again back at 0.

So every measurement taken through that label was reading the reservation
back:

- "the identical call returned 60 px at label width 556 and 90 px at 476" was
  the dialog's own reservation at those two widths, published as proof that
  `heightForWidth` is impure;
- the sweep that replaced it, reporting all 15 candidates producing the same
  height at all 841 reachable widths, was one installed floor read 841 times.
  With the floor genuinely cleared the candidates differ at 134 of those
  widths and the advance-key shortcut under-reserves by 15 px at dialog widths
  678-690 -- inside the shipped minimum, on a shipped model name;
- the test asserting `needed <= reserved` was comparing `reserved` with
  itself, which is why an advance-key mutant survived it.

And the code had the same defect as the measurements. `max(...)` over readings
that cannot fall below the installed floor can only grow, so
`_reserve_note_height` was a one-way ratchet: narrowing the dialog and widening
it again kept the taller note (6 px for `small`, 30 px for a 63-char imported
id), taken off the transcript view, while `resizeEvent`'s docstring promised
the reservation was correct at every size.

My second correction of this entry was itself refuted the same way. I cleared
the floor -- and then ran the resize loop that collects the reachable widths,
which fires `resizeEvent`, which puts the floor straight back, before taking a
single reading. Clearing a confound and then re-establishing it before
measuring looks exactly like clearing it.

What generalises: when a measurement and the code under test share a
mechanism, agreement between them is not evidence. Three separate wrong
conclusions here were each internally consistent, reproducible, and confirmed
by a passing test.

### Varying two things and calling the result impurity

For three rounds the retranscribe reservation carried a claim that
`QLabel.heightForWidth(w)` is not a pure function of `w`, "measured" as 60 px
at label width 556 and 90 px at 476. A reviewer could not reproduce it in
4440 comparisons and named the real mechanism instead. Re-measured here with
the argument held fixed while only the label's width moved:
`heightForWidth(500)` returns 60 at every reachable width from 476 to 1316.
Pure.

The original measurement had changed the argument *and* the width together
and attributed the difference to the width. What does move the answer without
the argument changing is `minimumWidth()`, which Qt clamps the argument up to
-- `heightForWidth(200)` returns 120 at `minimumWidth() == 0` and 60 at 600 --
and the dialog reserves a minimum *height*, so it never applies here.

Two neighbouring claims in the same comment were checked at the same time and
both were partly wrong. "No under-reservation at any width" is true only
because the dialog's own 560 px minimum keeps the label at 476 or wider; below
that, at 415-419, both candidate keys under-reserve by 15 px. And "the
unpolished label reports 9 pt, which flips the choice between two shipped
labels" is right in its first half -- `setStyleSheet` alone leaves the label
at 9 pt and a 16 px line height until it is polished -- and wrong in its
second: the winner is the same either way, only the ordering below it differs.

The practical rule survived all three versions and is the only part worth
keeping: measure through the real, polished widget. What did not survive is
any of the mechanisms offered for *why* -- including the one this section
offers. See the section above: the real mechanism is `minimumHeight`, and
this correction was refuted the same day it was written.

### The fix moved the defect one level out, and made it likelier

`smoke_test.py` was rewritten so that asking where the settings live could not
migrate a legacy `tts_app` install. The same commit added a fresh-install
branch that reaches the model check, and loading a model calls
`preload_model` -> `_coordinated_download_if_missing` ->
`run_coordinated_download` -> `acquire` -> `_acquire_cache_lock` ->
`_download_lock_dir` -> `appdata_root()`. Which migrates. So the commit that
closed the side effect reopened it through a call chain that contains none of
the words a reader would grep for, and the new branch made it reachable in
cases where the old code had returned early.

Nothing in the script's own text is wrong about this; the help even says the
check "initializes the app data directory the same way starting the app
would". "Initializes" is the word doing the hiding: it names creation, and the
call also renames.

Two smaller ones from the same review, both in code written to be careful:
the settings reader falls through to the `.bak` when the primary raises
`OSError`, then copied *the primary* -- reading again the file that had just
refused, so the one failure the backup exists for became a crash; and the
"settings are unusable" branch skipped the model check although
`SettingsStore.load` quarantines that file and runs on defaults, which the
script's own message says.

### Three numbers that no build ever printed

The entry above about deriving a size from the wrong table gave the regression
as `2.13 -> 2.08`, `1.84 -> 1.80`, `1.03 -> 1.00`. The buggy formatter was
`f"~{megabytes / 1024:.1f} GB"` -- one decimal -- so it printed `~2.1`, `~1.8`
and `~1.0`. The three "after" values were computed from the description of the
bug rather than read off the code, in an entry whose subject is exactly that.
The source comment beside the fixed code had the opposite half wrong: it still
said six labels and named `large-v3`, which the log had already corrected to
four. Recomputing both formatters over the real table settles it in one
command, and the two texts now say the same thing.

### The second door

The `release()` fix above was checked by mutation, documented, and wrong by
half. Reviewing my own change afterwards -- reading every one of the 14 lease
release sites rather than the one I had edited -- showed that the guarded
statement was only the first of two. `release()` hands back through
`_release_transcriber_runtime`, which applies the deferred cache reset, which
closes the *cached* transcriber through the same `_close_cached_transcriber`
that swallows `Exception` but not `BaseException`. So the exact failure the
new `try` was added to survive still escaped, to the same worker, with the
same symptom.

Two things made it easy to miss. The mutation test proved the guard I wrote
was load-bearing, which is a different claim from "the method now holds", and
I had written the stronger claim into `AGENTS.md` as "never raises" without
reading what the call inside the `finally` does. The invariant is now stated
as what is actually guaranteed -- always hands back, swallows a failed close
and a failed deferred reset -- and explicitly not as "cannot raise", because a
logging handler that throws still escapes and no failure mode stands behind
chasing that.

The generalisable part: after fixing a call, read the callee. A guard placed
around statement A is worth nothing if statement B, one frame down, reaches
the same failure.

### Every run in this repo was hiding its test count

`pyproject.toml` sets `addopts = "-q"` and `AGENTS.md` told every agent to
pass `-q` as well. That is `-qq`, which suppresses pytest's final
`N passed in Xs` line entirely, so a green run printed dots, `[100%]`, and no
count at all. Measured on one module: 59 dots and nothing with the extra
`-q`, `59 passed in 0.26s` without it.

Nothing was broken by it, which is the point -- a run that collected three
tests looked exactly like one that collected the whole suite, and every
"the suite is green" this session rested on an exit code. The documented
command now omits the redundant flag, and the first run under it recorded
`1831 passed, 1 skipped in 102.08s`.

### Two of my own mutations were the thing that was wrong

Both survivors in the first mutation run were harness errors, not surviving
defects. One test used a `close()` that succeeded, so the ordering it existed
to pin could not matter. The other revert was split across two edits again --
the same mistake as the hung run earlier the same day -- leaving `transcriber`
bound to `None` so the mutation was a no-op.

Re-run properly, the second one *still* survives, and that is the honest
result: the teardown's own `except BaseException` catches the
`UnboundLocalError` from evaluating its argument, so pre-binding `transcriber`
changes no observable behaviour. It is kept because without it every such
failure logs a "Failed to tear down the stream connect" traceback that is
really our own unbound local. The comment and the test docstring now say that
rather than implying the line is load-bearing.

### What these say about the process

- **Check the instrument before publishing the measurement.** Three rounds
  quoted numbers from a `heightForWidth` sweep that was reading a cache.
- **A reviewer who contradicts you may still be wrong.** The round-8 reviewer's
  three central numbers for the note reservation disagreed with my first
  re-measurement; it took a third method to find that the reviewer's
  conclusion was right and both of our first measurements were not.
- **Prefer "I could not reproduce a failure" to inventing a justification.**
  The honest version of the reservation comment is weaker than the one it
  replaced, and it is the one that will still be true next year.

## 2026-08-28 (seventh and eighth adversarial rounds: the fixes were the bugs)

Two more rounds of three reviewers each, over the previous round's commits.
Every round so far has found defects in the round before it; these two found
them almost exclusively *in the fixes*, including two cases where a fix made
something reachable that had not been reachable before, and one where a
correction was itself wrong in the same way as the thing it corrected.

### A guard that created the failure it was written to prevent

Round 6 added a teardown call to the streaming capture-failure arm, because
the arm released the runtime lease but left the provider handshake running.
Correct diagnosis, wrong placement: the new call went *in front of*
`runtime_lease.release()`, and it is not exception-tight. It reaches provider
code and starts a thread, and `Thread.start` raises `RuntimeError` when the
process cannot create one -- so a plain `Exception` now stranded
`_transcriber_runtime_lock` for the process lifetime, which the old ordering
made impossible. The escaping exception also left the overlay on "Listening",
i.e. it destroyed the error report the arm exists to produce.

Before the fix that arm could not strand the lock. After it, it could. The
reviewer measured it: `LOCK ACQUIRABLE: False`.

The same round's `except BaseException` on the preload was copied from the
`except Exception` above it without the cancel check that arm *starts* with,
so pressing Cancel persisted "could not be loaded" for that model and the next
dictation re-raised it instead of retrying -- the exact failure the
`TranscriptionCanceled` arm three lines up documents and avoids.

### Deriving a value from the wrong table

The picker labels were changed from hand-written sizes to derived ones,
correctly, and then divided by 1024 and labelled "GB". `MODEL_ESTIMATED_SIZE_MB`
says in its own comment that it is decimal megabytes, and
`model_download_progress` converts it with `* 1_000_000` -- so the fix took the
four labels that were already correct two-decimal decimal GB and made every one
of them wrong. The formatter it introduced was `f"~{megabytes / 1024:.1f} GB"`,
i.e. binary *and* one decimal, so the four went
`cohere-transcribe-03-2026` `~2.13 GB` -> `~2.1 GB`, `granite-4.0-1b-speech`
and `granite-speech-4.1-2b` `~1.84 GB` -> `~1.8 GB`, `canary-1b-v2`
`~1.03 GB` -> `~1.0 GB`. The test could not see it because the test divided by
1024 too: it agreed with the code and the pair was self-consistent.

(A later version of this entry gave those results as 2.08, 1.80 and 1.00.
Those are what a *two*-decimal binary divisor would print, and no build ever
had one -- the numbers were derived from the arithmetic rather than read off
the code, in an entry whose whole subject is deriving a value from the wrong
table. Recomputed from `MODEL_ESTIMATED_SIZE_MB` through both formatters, and
the current `f"~{megabytes / 1000:.2f} GB"` restores all four to their
original strings exactly.)

(The first version of this entry said "six labels" and offered `large-v3` at
"~3.0 GB" as the most visible case. Both are wrong, and checking cost one
command -- `git show 3901440^` lists the pre-derivation labels. `large-v3` read
`~3 GB` before the derivation, so it was never one of the correct entries the
divisor regressed; it was under-stated already and the divisor merely
under-stated it differently.)

Its tolerance was 5%, which would also have accepted the 3.8% drift on `tiny`
that motivated it. Replacing it with `max(5 MB, 0.5%)` was still wrong in both
directions: it *also* accepts `tiny` at 75 against 78, and for every GB model it
is looser than the 5 MB it was meant to express, reaching 15.5 MB on
`large-v3`. The bound now follows the format the label uses -- exact below
1000 MB, where the label restates the table's integer, and 5 MB above it, which
is all two decimals of GB can move. Measured against the current table: every
MB label is exact and the worst GB label is 4 MB out, so both halves bind.

### A diagnostic that moved the user's data folder

Round 6 stopped `smoke_test.py` rewriting the settings file. Round 7 found the
same failure one level up: `settings_path()` goes through `appdata_root()`,
which creates the folder and, when only the legacy `tts_app` one exists,
renames the user's entire data directory onto the current name. So *asking
where the settings live* migrated settings, history and recordings. Measured
with a scratch `APPDATA`; the round-6 verification missed it because it
compared directory contents in states where the directory already existed.

`app_paths` now has read-only resolvers, and the script reports a settings file
that will not parse instead of silently loading defaults from the repaired copy
and calling the default model "the configured local model".

### One glyph at a time is the wrong shape

`import_model.py --validate-only` printed U+2713 on success. `sys.stdout`
becomes cp1252 the moment output is redirected on Windows, so a *complete,
valid* model crashed the script with `UnicodeEncodeError` and exit 1 -- while
19 passing tests said it was fine, because `capsys` swaps in a UTF-8 buffer and
the encoder never runs.

Fixing those two glyphs left three em dashes in the same file, which encode in
cp1252 (0x97) and therefore only *look* wrong -- U+FFFD in any UTF-8 editor,
which is where a captured log gets opened. And a sweep then found
`download_model.py` drawing its SSL-error box with 63 U+2550 characters:
stderr's error handler is `backslashreplace` rather than strict, so that one
never crashed either, it just wrapped the corporate-proxy guidance -- the text
a Zscaler user pastes into a ticket -- in two walls of literal `\u2550`.

Three files, three symptoms, one cause. The assertion is now on the cause: no
non-ASCII in any string literal any script evaluates. Writing that test
immediately found a fourth thing, that two scripts hand `__doc__` to argparse,
so their module docstrings *are* printed and could not be exempt.

### The correction that repeated the mistake

Round 7 corrected "Parakeet is the fastest local model by a wide margin" --
`tiny` measured 0.033 against 0.043 -- and, to justify the default anyway,
computed a word-sequence agreement against `large-v3` from the transcripts the
benchmark already stores. Parakeet came out at 98.1%, the highest in the run,
and that went into the report, `AGENTS.md`, the README and `docs/models.md` as
"the highest of any model measured".

Round 8 took the same data apart. Re-run with each of the 13 working
transcripts as the reference in turn, Parakeet's rank moves between 1st and
8th; it is 1st under `large-v3` and `medium`, 3rd under `large-v3-turbo` and
Cohere, 5th under Granite 4.1. The whole gap is **one token out of 52** -- and
on that token the reference is the one that is wrong, writing `transkriptiere`,
which is not a German word, where Parakeet, turbo and Cohere all write
`transkribiere`. Parakeet's lead came from also reproducing the reference's
doubled "richtig".

So an unsourced superlative had been replaced with a sourced number that did
not mean what the sentence around it claimed. What the measure *does* support
is robust under every reference and is what it is now used for: Plus is last of
12 every time and NAR 11th or 12th -- neither transcribed the recording -- and
`tiny` is 10th or 11th every time.

The published value for Plus was also not reproducible from the published
method: `difflib` discards popular elements of its *second* sequence once it
exceeds 200 items, so the 378-token transcript scored 1.4% in one argument
order and 2.8% in the other. The report now gives the exact call, including
`autojunk=False`.

And the retraction in `granite-speech-4.1-onnx-variants.md` was wrong in the
same way: it withdrew Plus's 0.81 and NAR's 0.49 as having "no source" after
checking only `benchmark_history.json`. Both are in this log, from a manual
session on a 16.9 s English / 13.4 s German clip pair. They are not
unsourced -- they are not comparable, which is a different sentence.

A third pass was still needed after that. The rewritten report said the
Parakeet-versus-`small` comparison "does survive every choice of reference" --
repeated from the reviewer's summary without being checked. It does not: it
reverses under five of the thirteen, including `small` itself. Between two
models that both worked, this measure supports nothing, and the fourth version
of the paragraph says so.

### What these two rounds say about the process

- **A fix inherits the burden of the thing it fixed, and then some.** Round 7
  fixed round 6; round 8 fixed round 7. Two fixes made a failure *newly*
  reachable, which no amount of testing the original bug would have caught.
- **Mutation-test the assertion, and check the test is not short-circuited.**
  Three tests written this round passed under their own mutation. The preload
  one passed because the worker's *pre-acquire* cancel check returned before
  the code under test ran -- the same shape as the round-6 watchdog test, which
  the log already records. The note-height pair each passed under the other's
  mutation, which is why both are kept.
- **A retraction is a claim.** "This figure has no source" needs the same
  search a positive claim needs, and it did not get one.
- **Check a superlative against the thing that would overturn it.** Both
  versions of the Parakeet claim were reached by finding a number that
  supported the conclusion, not by asking what would refute it. Re-running the
  same measure under a different reference took one command.

## 2026-08-28 (sixth adversarial round: a deadlock, an inert fixture, and a destructive smoke test)

Three reviewers again, over the fifth round's commits, plus my own pass over
the two commits no reviewer had been given. The pattern held for the sixth
time, and this round was the largest yet: three HIGH defects in the previous
round's fixes, two HIGH defects in code I had written and mutation-checked,
and roughly thirty documentation claims that were wrong or unsourced.

### The one I found myself: the CLI hung forever above 8 KB

`scripts/benchmark_local.py --isolated-case` -- the default -- waited for the
child process to exit and only then drained its result queue. A
`multiprocessing.Queue.put` returns immediately and a feeder thread writes the
pickled payload into an OS pipe, so the child blocks at exit until the parent
reads it. Waiting for the exit first is therefore a deadlock as soon as the
payload outgrows the pipe buffer. Measured with the real classes: 8 KB
completed, 16 KB hung forever. The payload is `asdict(case)`, i.e. every run's
full transcript, so a few minutes of audio or a short clip at `--runs 3`
reaches it -- the repository's own 24 s sample is why nobody hit it. And the
wait had no budget at all, so the CLI just stopped with no output.

The shipped app was never affected and the contrast is worth keeping:
`benchmark_process.py` uses `subprocess` with a dedicated stdout reader thread
that drains the pipe concurrently, and `src/` uses no `multiprocessing`.

The same function's cancel branch still had the single long `join(2.0)` that
the rest of the file had just removed. Measured: `Process.join(6.0)` held an
interrupt raised at 0.5 s until 6.01 s, the poll loop delivered it at 0.62 s --
the identical deferred-signal defect as the thread case, so `_join_case_thread`
became `_join_case_worker` and serves both.

### The smoke test could carry away the user's configuration

`smoke_test.py --check-model` called `SettingsStore().load()` on the real
settings path. That is not a read: it creates the file and a `.bak` when none
exists, rewrites it whenever the stored payload differs from the normalized
one, and renames both the file and its backup to `*.corrupt.<timestamp>` when
the JSON will not parse. A diagnostic script quarantining a user's settings is
the worst outcome in this session. It now loads a throwaway copy, verified to
leave the directory byte-identical in all four states.

The same step also ran for remote engines, where the transcriber is built
without a secret store and dies with "API key is missing" -- so `--strict`
returned 1 on a perfectly healthy install -- and where no provider implements
`preload_model` at all.

### The suite's cache isolation was inert

The fifth round added a fixture setting `HF_HOME`, `HF_HUB_CACHE` and
`HF_HUB_OFFLINE` to stop the suite downloading models. It did nothing.
`huggingface_hub` computes those constants **at import**, and a test module
imports it at module scope, which pytest does during collection -- before any
fixture runs. Measured after collection: the constants still pointed at the
developer's real `~/.cache/huggingface/hub` with offline False. The only thing
actually preventing downloads was the other half of the fixture, the
`_coordinated_download_if_missing` stub.

It moved to `pytest_configure`, which runs before test modules are imported,
and the isolation is now verified live after collection. That also replaced one
temp directory per test with one per session: `tmp_path_factory` rescans its
base directory on every call, so the per-test version cost ~1750 extra
directories and seconds of pure scanning.

### Rekeying a test dropped the coverage it was rekeyed away from

The fifth round rekeyed the faster-whisper between-segments cancel test from a
call count to decoding progress, for a good reason. But the call count had been
the only thing pinning the check *above* the model load, and the new key cannot
see it: deleting that check passed the entire suite. A job cancelled while it
waited in the single-worker queue would pull a multi-gigabyte model into memory
to throw the result away, holding the shared runtime lease while doing it. A
dedicated test now asserts the model is never constructed.

The lesson generalises: when a test's key changes, ask what the *old* key was
covering that the new one cannot.

### Three workers could never resolve

The fifth round added an `except BaseException` arm to `_transcribe_worker`
because the terminal signal sits after the `finally`. Three sibling workers had
the identical shape and were left alone. Measured consequences:

- `_finalize_stream_worker`: no terminal signal, `_streaming_recording` stays
  True, and every later hotkey press is refused with "Streaming transcript is
  still finalizing" until Cancel or a restart.
- `_preload_model_worker`: no `model_preload_done`, so `_preload_phase` keeps
  describing a preload that ended.
- `_start_batch_recording` built its capture outside any `try`: Qt prints the
  traceback and continues, so the overlay sat on "Listening" forever with no
  error text and every retry reproduced it.

And the headline fix itself was incomplete twice over. Its guard released the
runtime lease but left the handshake running, so `start_stream` published a
session nobody owned and the *next* dictation failed with "Streaming session
already active". Meanwhile `_acquire_transcriber_runtime` -- one frame deeper --
still cleaned up under `except Exception`, so the exact stranded-lock failure
the commit message described was fully reachable through it.

Worth recording plainly: the trigger that commit named,
`float(vad_energy_threshold)` on a corrupt stored value, **cannot happen**.
`AppSettings.from_dict` already coerces and clamps it. The guards are defensive
depth and the comments now say so.

### Sizes drift when they are written twice

The picker labels hand-wrote a download size next to each model name,
duplicating `MODEL_ESTIMATED_SIZE_MB`. That table gets corrected whenever a real
download disagrees with it; the label did not follow. `distil-large-v3.5` read
"~756 MB" against a measured 1516 and `large-v3-turbo` "~809 MB" against 1622 --
the two models a user picks between by size, both understating themselves by
half, and AGENTS.md already recorded the 756 MB figure as a *fixed* defect.
Almost every other entry was a few percent out from dividing by 1000. The label
is now derived, and a test rejects any stated size more than 5% from the table.

### The measurement that justified the default was not in the repository

Every published Parakeet, Canary and Nemotron figure cited a benchmark run that
existed only in `benchmark_history.json` on one machine. It is now committed as
`docs/benchmarks/amd-ryzen-7600x-intel-arc-a750-2026-08-25.md`, without the
private German recording or any transcript.

Checking it against the history retired two more numbers. Nemotron's
long-published "0.229 RTF and 0.81 s cold load on the repository sample"
matches no run: the two real runs are 0.21 with a 1.78 s load and 0.24 with a
1.90 s load, and that sample is 2.1 s of synthetic sine tones no run used.
Canary's "RTF 0.134 / 0.135" has no source at all -- its only benchmark case
errored with "Canary cannot detect the language".

### What this round says about the process

Six rounds, and every one has found defects in the round before it. Three
specific habits earned their keep here:

- **Mutation-check the assertion, not just the fix.** Three tests I wrote this
  round passed under the mutation they were written for. One took three
  attempts: the pre-run cancel check consumed the single raise before the
  watchdog ever polled, and `TranscriptionCanceled` was raised by the post-run
  check either way, so only asserting on the *exit path* discriminated.
- **A fix inherits the burden of the thing it fixed.** Five of this round's
  findings were in fixes made one round earlier, three of them mine.
- **Restart dead agents.** Two of the three round-6 reviewers died on a session
  limit having produced nothing. Both were relaunched; between them they
  produced five of this round's HIGH findings.

## 2026-08-28 (fifth adversarial round: a stranded lock, and a suite that downloaded models)

Three reviewers over the previous round's commits. The pattern held for the
fifth time: the round found defects in the fixes the round before it made,
including two of mine that had been committed with a mutation check.

### The HIGH one: a lease nobody owned

`_build_audio_capture` sat after the block whose three `except` arms release
the streaming runtime lease and before the lease is stored in
`_active_stream_runtime_lease`. Nothing owned it in that window, and the call
can raise -- `float(vad_energy_threshold)` on a corrupt stored value, or the
`AudioCapture` constructor. Measured on a real controller with the call
patched to raise: `runtime_in_use` True, `active_count` 1,
`_active_stream_runtime_lease` None, and a later shared acquire returning
False. That is the process-lifetime failure the *previous* round had written
four lines of comment about while hardening a statement that cannot raise
(`set_cancel_check` assigns two attributes). Every later preload and audio
import would block forever, every dictation would build its own isolated
runtime, and `_transcription_runtime_active()` would stay True so no deferred
cache reset could ever run.

### Both of the previous round's UI fixes were incomplete

- **The retranscribe note's worst case was bounded by the wrong set.** It
  measured the longest entry in `LOCAL_MODEL_LABELS`, but the note interpolates
  `local_model_short_label(self._entry_model)`, which returns an unrecognised
  id verbatim -- and `_entry_model` comes from a history entry, so a History
  import can carry anything. Measured with a 63-character id at the dialog's
  own minimum width: 60 px reserved for a note that takes 75. The reservation
  now takes the longer of the widest known label and this entry's own.
  Rewriting the test around it corrected the invariant too: it had compared
  heights *across* dialogs, which is wrong once each dialog's worst case
  depends on its own entry. What must not change is the height *within* one
  dialog as the user works the model picker, so the test now drives every
  entry in the combo at four widths.
- **The caption tuples were right and untied.** Nothing linked
  `RECORD_BUTTON_CAPTIONS` and friends to the captions the code actually sets,
  and the commit that introduced the constants left the literals in place at
  three call sites. Both fixed, with a test that drives every runtime caption
  path and asserts each observed caption is in the tuple that sizes its button.

### A vacuous assertion that a mutation check had passed

The assertion added to `test_a_download_canceled_during_a_preload_is_not_a_broken_model`
was answered by the cache-key branch, not the condemned-runtime branch its
comment described: `_preloading_controller` never sets
`_transcriber_cache_key`, so the final `cached_key != identity` comparison
already returns True. My earlier mutation run had reported it detected --
because a *different* test I had added in the same batch covers that branch.
Two tests, one mutation, and the attribution was wrong. The fixture now sets
the cache key, and the mutation is detected by the test that claims it.

### My own CLI cancel fix reintroduced the defect it removed

The wind-down after Ctrl+C was one `worker.join(timeout=10.0)`. On Windows
CPython's lock acquire ignores the interrupt flag, so no signal is delivered
until the call returns. Measured on this machine:

| call | interrupt sent | KeyboardInterrupt raised |
| ---- | -------------- | ------------------------ |
| `while alive: join(0.15)` | 0.500 s | 0.630 s |
| `join(6.0)` | 0.500 s | 6.011 s |
| `join()` | 0.500 s | 8.000 s (thread's full life) |

So it is the *timeout* that makes the interrupt arrive, not the join -- the
timeout returns to bytecode, where the pending signal fires. The docstring
had credited the join. Every wait is now a poll against a deadline, and four
smaller holes in the same handler are closed: the worker starts inside the
`try` (an interrupt in the few bytecodes after `start()` escaped without ever
setting the cancel flag), a case that finished in the instant of the interrupt
is kept rather than discarded, a case outliving the wind-down budget is
reported (the thread is a daemon, so `transcriber.close()` never runs and the
Cohere/Granite `node.exe` is orphaned), and the isolated worker no longer
records a `BenchmarkCancelled` as a failed benchmark case.

### The test suite was downloading models

Isolating the Hugging Face cache per test -- so the inventory tests stop
depending on which models the developer happens to have -- immediately turned
one faster-whisper unit test into a real `snapshot_download` that ran for 42 s
and would have written 486 MB. It injects a fake model factory and never
wanted a download; it was only fast because the cache was warm. **That is what
a clean CI runner has been doing on every run.** The fixture now points
`HF_HOME`/`HF_HUB_CACHE` at an empty directory, forces `HF_HUB_OFFLINE`,
disables the ModelScope fallback (plain urllib, it does not read
`HF_HUB_OFFLINE`), and stubs out the pre-fetch itself.

Two attempts before that one were wrong and are worth recording:

- Gating the pre-fetch on "the caller supplied its own model factory" looked
  clean and broke the two tests that exist to assert the pre-fetch happens --
  they inject a factory precisely to observe the ordering.
- Stubbing `_has_valid_model_snapshot` instead of the method above it made
  every Whisper model look installed, because that predicate is also what
  `find_cached_models` detects with.

The files that test the download path take the real method back through a
shared fixture, which is the same seam used deliberately rather than by
accident.

### One cache root on one side, two on the other

The ONNX inventory and loader searched a single root while `delete_cached_model`
always spanned both the Model Dir and the default cache. So a model fetched by
`scripts/download_model.py`, which writes into the default cache, went
invisible the moment a Model Dir was set: the Local tab reported it missing,
the preload downloaded it again, and Delete would then remove the copy the scan
had never listed. Now symmetric. `webgpu_download_destination` is untouched, so
download progress still measures the one directory a download writes into.

### Documentation that contradicted the code, and numbers that were not sourced

Changing the default model reached further than the commit did. `quick-start.md`
still told a new user the first dictation downloads "the `small` Whisper model
(~486 MB)"; `models.md` marked `small` as the default nine lines above the table
that marks Parakeet as the default; `README.md`'s settings table said `small`;
and the candidate evaluation's headline paragraph still said Parakeet "is not
implemented". All corrected.

The numbers were worse, and the fix was to go back to the source.
`benchmark_history.json` holds one run measuring both models on the *same* 24.3 s
German recording at `device=cpu`: Parakeet 0.0428/0.0423, `small` 0.152/0.1553.
So the honest figures are **0.042 against 0.152, a 3.6x difference on one clip**.
What had been published instead was "RTF 0.046 EN / 0.043 DE against 0.151":
0.043 is the German number this repository had corrected to 0.042 two days
earlier and written down as a misquote; 0.151 comes from an older run on a
different clip; and no English Parakeet measurement exists in the history at
all. The "25 European languages" claim turned out to be true and unsourced --
the model card in the downloaded snapshot lists exactly 25 codes -- so it is
now attributed rather than asserted.

Three offline paths had also lost the default model: it has no ModelScope
mirror (the script's docstring promised one for everything), it was missing
from the git-clone list, and the SSL help box matched a substring only the
mirrored error message carries, so an unmirrored model got a bare "Download
failed" where `--model small` got the CA-bundle guidance. `import_model.py`
was the worst of them: it validated files before checking the model, so a
Parakeet folder was told to download `model.bin`, `tokenizer.json` and
`vocabulary.txt` from a repository that contains none of them, and the
accurate "this script imports CTranslate2 models only" message was
unreachable.

## 2026-08-27 (third and fourth adversarial rounds, and the default model)

Two more review rounds over the previous rounds' commits. The pattern from
round two held again: every round found defects in the round before it, and
several of them were in code written to fix a defect.

### Cancel was installed everywhere except where it mattered most

- **The preload never installed a cancel check at all.** Overlay Cancel could
  stop a transcription but not a preload, and a preload's *own* download --
  the one a transcriber starts from its load path while waiting on the
  machine-wide slot -- had nothing to poll. Fixed by installing a
  generation-scoped check on the shared transcriber before `preload_model()`.
- **A canceled preload was recorded as a successful one.**
  `_record_model_preload_result(key, generation, None)` is the *success*
  sentinel, so the cancel branch marked the half-loaded runtime as preloaded
  and the cached key still matched the settings snapshot. Nothing would ever
  retry it. The branch now condemns the runtime
  (`_pending_transcriber_cache_reset`), which `_local_model_preload_needed`
  reads before the cache-key comparison.
- **`self._cancel_check` was passed raw into the download coordinator.** The
  base class has `_is_cancel_requested`, which never raises, logs once and
  latches; the raw attribute does none of that, and the coordinator re-raises
  whatever escapes a check -- so a user check that raised would have failed
  the download instead of the cancel. All four local engines now pass the
  base method.
- **`close()` between the two locks raises `TranscriptionError`, not a
  cancel.** `transcribe_batch` takes `_model_lock` and `_inference_lock`
  sequentially, so a `close()` in the gap unwraps the sessions the run is
  about to use. It is not a user cancel: a settings save and a resume-driven
  reset take that path too, and the controller renders a cancel as a bare
  "canceled" with no text and no Retry -- which would present a runtime the
  user did not stop as one they did, and drop the recording.

### Two of my own fixes were verified wrong, and one report was wrong

- **A precondition assertion that could not fail.** I asserted the four header
  buttons are fixed-width *after* `_balance_header_flanks` ran -- which is the
  function that calls `setFixedWidth`. The mutation check returned VACUOUS.
  Replaced by a `sizeHint()` fallback plus a test that drives the real header
  shape: two buttons per group, one of them unpinned.
- **An invalid mutation, reported as a pass.** To check the retranscribe
  note's `heightForWidth`, I mutated by *adding* `setFixedHeight` rather than
  by reverting the flag, and reported "detected". Challenged, the honest
  mutation came back VACUOUS: `QLabel.setText()` internally restores
  `sizePolicy().setHeightForWidth(wordWrap)`, so the explicit call changes
  nothing. Measured sequence on this label:
  `setWordWrap=False setSizePolicy=False setAlignment=False setText=True`.
  The reviewer's stated mechanism was wrong and their conclusion was right.
- **A pixel test that measured the rectangle instead of the text.**
  `QWidget.render()` defaults include `DrawWindowBackground`, so every pixel
  in the label came back opaque: 1792/1792. Centred and left-aligned both
  reported the span `(0, 111)`. Rendering with `DrawChildren` only, plus an
  assertion that not every pixel is opaque, makes the test detect a
  left-aligned label.
- **A dead `layout().activate()`.** The note reservation called it before
  reading `note.width()`, with a comment asserting that `resizeEvent` fires
  before the layout runs. Instrumented: across five observed calls at dialog
  widths 640/640/600/560/320 the width was identical before and after the
  call. Removed, and the comment replaced with what was measured.

### The status text was never centred, and the flanks are why

The header is `[Record][Pinned] <label> [Clear][Copy]` and the label is the
only stretching item, so Qt gives it the span the four fixed-width buttons
leave over -- whose midpoint is the header's midpoint only while the two
groups are equally wide. They were 158 px against 134 px, putting every status
word 12.0 px right of centre in every state. It had been 7 px until the 78 px
Record button replaced the 68 px History button as the first item, so it was
never centred and got worse. Balancing the flanks at construction measures
0.0 px offset in every state, both pin modes, with and without the queue, and
the overlay is still 470 px wide because the controls row, not the header,
sets the width.

### Round four: what the tests were not testing

- **Three "identity" tests compared a tuple instead of observing an
  outcome.** Converting them to drive `on_settings_changed` and assert on the
  closed runtime and the preload immediately surfaced a real distinction the
  tuple comparison hid: a *remote* runtime change must close the cached
  transcriber and must **not** preload, because there is no model to load.
- **The cancel-check source scan was porous in four ways.** It only looked at
  classes whose bases literally name `ITranscriber`, so a base imported under
  an alias, a subclass of a subclass, an annotated assignment and
  `setattr(self, "_cancel_check", ...)` all walked past it. Replaced by the
  conservative inverse: flag every `self._cancel_check` assignment in the
  package, with a named allow-list for the one helper that legitimately owns a
  field of that name. Each of the four shapes is now a test case.
- **`_local_model_preload_needed`'s condemned-runtime branch was unpinned.** A
  mutation replacing its `return True` with `pass` passed the whole suite,
  because the outcome assertion I had just added was satisfied by a different
  branch. Pinned directly.

### Two performance findings, one of which was not what it looked like

- **`import stt_app.local_benchmark` pulled numpy and 352 modules** because of
  a module-level `TranscriptionCanceled` import; moving it into the function
  took it to 176 and 0.081 s. A round-four reviewer then showed the win does
  not reach the worker subprocesses, which import
  `transcriber.local_faster_whisper` themselves. The real driver was the
  transcriber package's eager provider imports.
- **`stt_app/transcriber/__init__.py` now resolves its names lazily**
  (PEP 562). Importing any submodule runs the package first, so the download
  and inventory-scan workers were each paying for the AssemblyAI, Azure,
  Deepgram, ElevenLabs, Fun-ASR, Groq and OpenAI modules at every launch.
  Measured on the download worker: 0.232 s / 330 modules down to 0.114 s /
  234. What remains is `stt_app.vad` importing numpy at 61 ms, which is a real
  dependency of a module that is actually used.
- **The benchmark CLI could not be interrupted in its non-default mode.**
  `--isolated-case` (the default) terminates the child process, which is why
  the `cancel_check` parameter of `run_benchmark_cases` had no production
  caller at all. `--no-isolated-case` ran the case on the main thread, where
  Python cannot run a signal handler while the process sits inside
  `InferenceSession.run` -- so Ctrl+C was invisible until the call returned,
  4.46 s for one Canary run times `--runs`. The case now runs on a worker
  thread with the main thread in `join()`, and the flag Ctrl+C sets is handed
  to the model as `cancel_check`.

### The default model is now Parakeet

`DEFAULT_MODEL_SIZE` was faster-whisper `small`. The note in `AGENTS.md` said
to keep it "until real target-hardware benchmarks justify switching"; those
benchmarks exist now and say the opposite. On a Ryzen 5 7600X,
`parakeet-tdt-0.6b-v3` measures RTF 0.046 EN / 0.043 DE against 0.151 for
`small` -- about 3.3x faster on the same CPU -- for 670 MB against 484 MB,
over 25 European languages with its own language detection, and it keeps every
property that made `small` the zero-setup choice: pure Python, CPU only, no
GPU, no Node.js.

What it gives up is streaming, because onnx-asr is batch-only. `DEFAULT_MODE`
is `batch`, so the out-of-the-box combination is consistent, and switching to
streaming mode already tells the user to pick a streaming model.
`DEFAULT_FASTER_WHISPER_MODEL_SIZE` (`small`) was added for the default
*within* that runtime, which `LocalFasterWhisperTranscriber` and the benchmark
CLI use -- without it, faster-whisper's own default argument would have become
an onnx-asr model id. Changing the constant does not touch an existing
install: `SettingsStore.load` falls back to the default only when the key is
absent, which is now a test rather than an assumption.

The change broke 26 tests, all of which had been relying on the default being
a streaming-capable, language-selectable Whisper model without saying so.
Naming a model explicitly in those tests is the fix and also documents what
each of them actually needs.

## 2026-08-27 (second adversarial round: the fixes reviewed, and their own defects)

The rule from the previous round -- a fix is a change and inherits the same
burden -- paid for itself: two agents reviewing only the *previous round's
commits* found ten further defects, one of them HIGH.

- **The cancel fix stopped short of the preload.** Every local engine now maps
  a canceled model download onto `TranscriptionCanceled`, but
  `_preload_model_worker` still caught it in its generic branch: it told the
  user the model "could not be loaded" *and persisted that failure for the
  key*, so the next dictation re-raised a stored error for something the user
  had chosen to stop. `run_benchmark_cases` had the same shape one level down,
  where the consequence is a permanent `error` row in benchmark history. Both
  now treat it as the cancel it is.
- **A per-engine cache key that was still too coarse.** Scoping
  `_TranscriberIdentity` per engine fixed the "every save reloads the model"
  defect, but `local` is four runtimes with four constructor signatures. The
  flat local branch made Parakeet reload its 670 MB model when the user typed
  a custom-vocabulary term onnx-asr never receives. Splitting it per runtime
  needed a test that pins *both* directions per field, or the split would be
  the next silent over-scope.
- **A `.get(engine, "")` that could only fail silently.** Reading the API-key
  flag through a defaulted lookup meant a future engine missing from
  `_ENGINE_KEY_FLAGS` would read *no key at all* and quietly share one identity
  with itself. Strict indexing plus a test that both engine maps cover every
  remote engine turns that into a suite failure at the moment the map is
  incomplete.
- **A guard clause in the wrong order.** `show_idle_status` returns early while
  a preload owns the overlay, which is right -- but it sat above the four
  hotkey-registration error branches, so a running preload swallowed the one
  message a user must see. Order matters in a chain of early returns; place a
  cosmetic gate below the error ones.
- **A subclass override that skipped the base setter.** faster-whisper assigned
  `self._cancel_check` directly instead of calling `super().set_cancel_check`,
  so it never re-armed the once-per-check failure log. Because the runtime is
  cached for the app's lifetime, "once per installed check" silently became
  once per process.
- **A lock the class did not depend on -- yet.** `close()` unwrapped the cancel
  hooks under `_model_lock` only. No caller can reach it mid-run today, but a
  class whose correctness rests on its callers' scheduling is one refactor from
  a silent regression, and the failure mode here is the cancel quietly doing
  nothing. Both locks now, in the order `transcribe_batch` acquires them.
- **A reserved height measured against the wrong worst case.** The retranscribe
  language note reserved two dialog lines; the note can carry a retired-model
  substitution *and* the Canary warning at once, which measures 45 px against
  38 px reserved, so the buttons below moved by 7 px. Measured rather than
  estimated, then pinned by a layout test.
- **A vacuous test scanner.** The check that the Node runner's imports match
  `package.json` matched only `from "..."`. A side-effect import, a multi-line
  named import, a re-export and a dynamic `import()` all read as "no
  dependency", so the check passed by finding nothing. All five forms now, with
  the scanner itself under test.
- **Nine of the round's ten fixes were mutation-checked** (revert the fix,
  confirm the paired test fails, restore). The tenth -- strict engine-map
  indexing -- has no mutation because its guard is the map-completeness test
  rather than a behavioural assertion; recorded here rather than left implied.

## 2026-08-26 (adversarial round on the cancel, preload-phase and retirement work)

A review round over the three preceding commits found nine defects worth
recording, several of them in the *fixes themselves* rather than in the code
they touched.

- **A cancel that frees nothing.** `LocalOnnxAsrTranscriber` wraps every ONNX
  session's `run` so `RunOptions.terminate` becomes reachable. The wrapper is
  stored in the session's own `__dict__` and holds the original *bound*
  method, whose `__self__` is that session -- a reference cycle, so `close()`
  freed nothing until a generation-2 collection happened to run. The whole
  point of the cancel was to release the CPU *and the model*. Fixed by
  restoring each session's `run` in `close()`.
  - The first attempt still failed its own test: `_install_cancel_hooks`
    walked the model with a **recursive local function**, and a closure that
    calls itself is a second cycle through its own cell -- which also held the
    list of collected sessions. An explicit work list fixed it. Lesson: when a
    test asserts an object is freed, disable the cyclic collector
    (`gc.disable()`), or refcount-only bugs pass.
- **Pressing Cancel during a download reported a failure.** All four local
  engines surfaced `ModelDownloadCanceled` as "Failed to download ..." -- an
  error dialog for the thing the user had just asked to stop. It is also what
  shutdown raises. One shared context manager in `ITranscriber` now maps it to
  `TranscriptionCanceled`.
- **A cancel check that raises logged a traceback every 0.25 s.** The
  ONNX/WebGPU reader polls it for the whole transcription. Latched to once per
  installed check.
- **A preload key that was not the runtime identity.** `_model_preload_key`
  described fewer fields than `_transcriber_identity`, so a successful preload
  could be credited to a runtime built from different settings. They are now
  the same function.
- **An identity that read fields its engine never touches.** Listing every
  provider's model field unconditionally meant pasting an Azure endpoint
  unloaded a multi-gigabyte *local* model. The identity is now built per
  engine -- and building it that way exposed two fields that had been missing
  entirely (`has_api_key`, `allow_insecure_key_storage`).
- **`invalidate_transcriber_credentials("groq")` invalidated nothing.** A
  string is iterable, so the membership test compared `"groq"` against
  `{"g", "r", "o", "q"}`. Every caller happened to pass a list, but the
  signature accepted the string form.
- **The JavaScript probe outlived its packages.** After the raw Granite paths
  were deleted, `_run_transformers_import_probe` still imported
  `@huggingface/tokenizers` and `onnxruntime-node`. Both resolve today only
  because npm hoists them out of `@huggingface/transformers`; neither is
  declared. Had that hoist changed, the probe would fail, its own `npm
  install` repair could not have fixed it, and every ONNX dictation would end
  in "run npm install" forever. Two tests now pin the probe to the runner's
  imports and to `package.json`.
- **A published number that was never counted.** The retirement write-up said
  the NAR/Plus encoders carry **48 `Einsum` nodes**; that came from
  `grep -c -o`, which counts *lines*, not occurrences. Re-counted three ways
  against the actual graphs -- protobuf `op_type` fields, distinct exporter
  node names, and `equation` attributes -- the answer is **16**, matching the
  repository's own 2026-06-24 record. The raw byte string appears 80 times.
  Corrected in four places. Also corrected: DirectML does not fail on
  `Einsum`. The benchmark's own error text names
  `/encoder/layers.0/attn/MatMul/MatMulScaleFusion/` -- the fused 5-D MatMul.
  Two different nodes, two unrelated reasons, one wrong sentence repeated
  everywhere.
- **Benchmark figures quoted from memory instead of from the file.** Re-read
  from `benchmark_history.json` (2026-08-25, a real 24.3 s German dictation,
  best of two runs): base 2B **0.098** on WebGPU, NAR **0.434**, Plus
  **4.138**, Parakeet **0.042**. The write-up had 0.100/0.460/4.161/0.043 and
  called NAR's output "word salad" -- an overstatement; the transcript is
  degraded German with words merged and dropped, not unrelated to the audio.
  Plus's 4.138 is a *consequence* of a degenerate loop (it repeated one clause
  to the 1024-token cap), not an independent speed measurement, which is what
  reconciles it with the earlier 0.81 on a clip where it terminated normally.
  The retirement decision does not depend on any of this: both encoders are
  GPU-incapable here at the graph level, both are slower than the base model
  even in their most favourable measurement, and Parakeet beats all of them on
  plain CPU. But "roughly six times faster than Granite 2B" in `AGENTS.md` was
  wrong for the same reason and is now **2.3x** (0.042 against 0.098).
- **Retirement leftovers.** The removal missed: `_run_transformers_import_probe`
  (above), a stale `GRANITE_4_1_MODEL_SIZES` used only by a test, present-tense
  prose naming the deleted `loadGranite41NarRuntime`, a "publish to Hugging
  Face" plan that ended, "every selectable ONNX model uses the pipeline" (two
  of the three local ONNX runtimes do not), and two `LOCAL_MODEL_LABELS`
  entries. The labels were **kept on purpose** so a history row recorded with
  a retired model still reads as a name, and are now marked "(removed)"; a
  test pins that split. Two user-visible gaps also came out of it: a stored
  model that no longer exists fell back to the default with no log line, and
  the Retranscribe dialog silently substituted another model for an entry
  recorded with a retired one. Both now say so.
- **~9 GB of orphaned cache with no way to reclaim it.** The Local tab lists
  only models the app currently offers, so a retired model's snapshot becomes
  invisible rather than deletable. Documented with the exact directories and
  measured sizes in `docs/models.md` rather than building an orphan-scanner
  for a one-off.

## 2026-08-23 (rounds five to eight: what repeated review actually caught)

Eight adversarial rounds ran over the streaming work. Every one found a real
defect in the previous round's fixes. The pattern is worth recording, because
it is not "the reviewer was thorough" but a specific, repeatable way of being
wrong.

- **Fixing a theoretical case and breaking the real one.** Round 7 replaced a
  raw prefix check with a word-based one, to handle a re-cased transcript and
  a partial-word match. Neither can occur in the only code path that calls
  the function. What does occur is a window starting with punctuation, which
  `stream_join_text` welds onto the previous word -- so the floor broke on the
  very call that created it, and the whole dictation was replaced by one
  hallucinated sentence. The check now accepts either comparison.
- **Deriving a number from a measurement the code never performs.** Round 6
  set the post-pause threshold from 300 ms excerpts. Production measures the
  longest run in the whole 8 s window, so every excerpt truncated its run at
  the edge and the statistic was an artifact of the slicing.
- **Measuring the wrong file entirely.** `samples/benchmark_sample.wav` is
  generated by `scripts/generate_sample_audio.py` and contains sine tones. A
  threshold was derived from it and read the generator's own duration
  parameters back as speech statistics; a test was even named
  `test_real_recorded_speech_...`. The repository has no recorded audio.
- **Guards that can never fire.** A generation counter re-checked after a
  join, while the caller bumped the same counter one statement later, so the
  stream abort was skipped every time it was needed. Two `segment_floor`
  guards were added against text that only ever grows, so neither could ever
  be false. All three looked protective and did nothing.
- **Fixtures too weak to reach the branch.** A test asserting that typing is
  rejected used undecayed 5 ms clicks measuring 0.020 s, four times below the
  cut. A duplication test used a `previous` shorter than the floor, so the
  merge resolved on the alignment path and never reached the code under test.
  Both were green and both proved nothing.

**Substantive outcome.** An energy gate cannot separate a keystroke from a
short word: "Bitte." measures 0.085 s and a mechanical key clack 0.080 s.
Four thresholds were set as if it could and three of them deleted real words.
The gate now claims only what it delivers -- it blocks silence -- and the
residual risk is handled by bounding the damage: a measured pause closes off
the text before it, so a bad window costs one segment rather than the whole
dictation. Separating the two classes needs spectral features, i.e. a real
VAD.

**Method note.** Reverting a fix and confirming its test fails caught six
tests of my own that proved nothing. A test written alongside a fix is not
evidence until the fix has been taken away -- and the fixture has to be large
enough to reach the branch, which is a separate check.
## 2026-08-23 (four adversarial rounds on the streaming and download work)

The entry below records the fixes. This one records what reviewing them
found, because three separate rounds each turned up defects in the previous
round's fixes -- twice a fix was worse than the bug it replaced.

- **A fix that deleted text.** The post-pause append was gated on 0.35 s of
  measured speech to stop a keyboard click appending an invented sentence.
  Measured afterwards: the meter buckets at 100 ms, so a 5-50 ms click
  reports 0.10 s and a real 150 ms word reports 0.20 s. 0.35 s therefore
  deleted short answers spoken after a pause -- "Ja.", "Stop." -- silently.
  The cut was set to 0.15 s at the time. It moved three times more afterwards (0.08, 0.18, 0.08) and
  now sits at 0.08 -- see the table in AGENTS.md, and treat `config.py` as
  authoritative. A log entry records what was believed on the day; do not
  read a value out of one of these entries and act on it.
- **A fix that pasted twice.** Rolling a failed live insert back is right
  only while the paste keystroke has not gone out. Two failure paths run
  after it, and the first attempt tagged them one exception class at a time
  -- missing the likeliest one of all, a clipboard verification read failing
  because a clipboard manager holds the clipboard open. Classification is
  now driven by a single `paste_sent` flag.
- **A fix that moved a threshold instead of removing it.** Capturing the
  overlay baseline after a *short* line instead of the real initial detail
  looked correct at 9-16 pt and reproduced the bug above 20 pt, because the
  detail minimum is a fixed 42 px. The baseline is now computed structurally
  after the stylesheet is applied, and the test asserts baseline ==
  structural height at five font sizes -- it catches the original bug and
  the incomplete fix.
- **A fix that could leave no hotkey at all.** `HotkeyManager.register`
  unregisters the current binding before trying the new one and does not
  restore it, so the reclaim timer could destroy a working fallback 30 s
  after startup while the overlay still advertised it.
- **A claim that was simply false.** "Silence tracking survives switching
  the gate off" -- the counter was incremented and zeroed on the same call,
  so the pause handling was dead whenever the gate was off.
- **A release that never happened.** v0.8.0 is tagged and has no GitHub
  release: both workflows ran the suite under `QT_QPA_PLATFORM=offscreen`,
  which the project itself documents as producing false layout failures, and
  that gate runs before the build step. The release gate now runs under
  offscreen deliberately with those assertions skipping themselves, quality
  runs without it on pushes, PRs and now tags, and both configurations were
  verified by running the complete suite twice.

**Method note.** Every fix was checked by reverting it and confirming its
test fails. That caught four tests of my own that proved nothing: the
merged-text assertion (the fake windows were nested, so the raw window
passed too), the finalize-executor test (it exercised the selector, not the
submit site), the new_segment "counterpart" (it asserted the unchanged
default path), and the first baseline test (it compared two overlays that
were both wrong). A test written alongside a fix is not evidence until the
fix has been taken away.
## 2026-08-23 (streaming data loss, the Qt-thread freeze, machine-wide download lock)

Five defects in the streaming path, all of which end in the user losing text,
plus the download lock finally made real.

- **Silence overwrote finished dictation, at both ends of the stream.**
  faster-whisper invents words from silence. In the rolling-window path an
  invented window can never be aligned against the accumulated text, so
  `merge_rolling_window_transcript` fell through to its replace fallback and the
  whole transcript became the hallucination. Both the partial path and the fast
  finalizer now measure the audio they are about to decode
  (`measure_peak_windowed_rms_pcm`, the same meter as the batch silence gate)
  and skip it below the threshold. **The finalizer was the worse half**: a
  dictation that simply ended with a few quiet seconds decoded one last
  hallucinated trailing window and lost everything. Reproduced end to end
  through the real worker: speech, then 12 s of near-silence the fake model
  "transcribes" anyway -> before, `stop_stream()` returned
  `"hallucinated subtitle"`; now it returns the real speech and the silence is
  never decoded at all.
- **Speaking again after a pause replaced everything before it.** A window
  arriving after more than one window's worth of silence shares no audio with
  the accumulated text, so the overlap search cannot find a seam and the same
  replace fallback fired. `_StreamResult.silent_seconds` now counts the skipped
  audio and marks such a window `new_segment=True`, which appends instead.
  The append has to stay driven by *measured* silence -- an unconditional
  append is exactly what produced 896 junk words during two minutes of an open
  microphone in the previous round.
- **Live insertion froze silently.** The transcriber emitted the raw rolling
  window to `on_partial`, but the controller's locked prefix compares against
  what it has already pasted, and a raw window does not contain that text. Once
  the window rolled past the committed prefix, `compute_stream_locked_prefix`
  could never advance again: insertion stopped for the rest of the session while
  the overlay kept reporting progress. The transcriber now emits
  `session.result.merged_text` and owns the merge; the controller no longer
  duplicates it.
- **A failed live paste threw its words away.** `apply_partial_append_only`
  commits text the moment it hands it to the inserter, so a paste failure lost
  it permanently -- the locked prefix would never offer it again. Added
  `StreamingTextState.rollback_commit()`; the controller rolls back and retries,
  and aborts the session after `STREAMING_LIVE_INSERT_RETRY_LIMIT` consecutive
  failures rather than dictating into a window that refuses text.
- **The remote handshake froze the whole UI.** Deepgram's `start_stream` waits
  up to 8 s on `connected.wait(timeout=8.0)` and the AssemblyAI SDK connects
  synchronously; both ran on the Qt thread from `_start_streaming_recording`.
  Pressing the hotkey therefore froze the overlay, tray and settings for the
  entire handshake -- at the exact moment the user wanted to start talking.
  Now the microphone is opened first and the handshake runs on a worker thread;
  audio recorded meanwhile is buffered and flushed in order before the
  completion signal, so nothing is lost (it used to be lost anyway, just with a
  frozen window on top). Measured in a test with a blocked fake handshake:
  `start_recording()` returns in well under 2 s instead of holding the thread.
- **Remote stop waited behind unrelated model work.** The single transcription
  worker exists so two local models never load at once, but a remote finalize
  loads nothing -- it drains a socket. Pressing stop on a Deepgram dictation
  while a local batch job was running left it "Processing" until that job
  finished. Remote finalizes got their own single worker; local streaming still
  uses the shared one because it genuinely re-transcribes audio.
- **The download lock is now machine-wide.** The coordinator only ever
  serialized callers inside one process, which says nothing about the
  out-of-process benchmark worker, `scripts/download_model.py`, or a second copy
  of the app -- all of which can write the same Hugging Face cache. Added
  `file_lock.CrossProcessLock` (`msvcrt.locking` on Windows, `fcntl.flock`
  elsewhere), taken after the in-process slot and outside the condition so
  observers are not frozen while waiting. A real kernel lock rather than a PID
  file on purpose: the OS drops it when the owner dies, so there is no stale
  state, no heartbeat, and no liveness timeout to get wrong. Verified with two
  real OS processes: same cache dir serializes strictly, different cache dirs
  still run in parallel, and `kill -9` on the holder frees it instantly.
- **Method note.** Every fix was checked for vacuity by reverting it and
  confirming its test fails. That caught one test of my own that proved nothing:
  the merged-text assertion passed with the raw window too, because the second
  fake window happened to contain the first. Making the windows genuinely
  overlapping rather than nested fixed it.

## 2026-08-18 (empty Parakeet result looked like a skipped queue item)

- **The queued WAV was transcribed.** `recording_20260818_211317_907096.wav`
  (token 11, 1.8 s, peak 0.1178) was submitted and finished with
  `outcome=success` in 1188 ms. Nothing was inserted and nothing was written
  to history because Parakeet returned `""`. The next clip 28 s later was the
  same sentence spoken longer (`Gib mir hierzu bitte noch eine Auskunft.`) and
  worked. Whisper recovers the short clip; Parakeet does not.
- **"No speech detected" as Done hid the miss.** The silence gate had already
  passed. The empty-result path then showed a brief Done state, saved no
  history, cleared retry audio, and overwrote `_last_transcript`. In a queue
  that looks exactly like "this recording was skipped".
- **Do not pad short Parakeet audio.** 0.5 s lead + 1.0 s tail recovered a
  truncated sentence; 6 s of pad invented words. Treat empty batch text as
  `empty_transcript` / Error + Retry instead. Import/retranscribe of the same
  empty result must return failure, not success plus a fake
  "No speech detected." history save.

## 2026-08-03 (tray flyout solved)

- **Solved by replacing the icon registration, not by anything at menu time.**
  Two standalone experiments settled it: an icon registered by hand
  (Electron-style bare `WS_POPUP` host window, `NOTIFYICON_VERSION_4`, native
  `TrackPopupMenu`) keeps the Windows 11 hidden-icons flyout open, and of two
  such icons differing *only* in the menu, only the native menu keeps it open —
  a Qt `QMenu` closes the flyout even on a correctly registered icon. So both
  halves are required. `win_tray_icon.py` implements it with a
  `QSystemTrayIcon`-compatible surface and a fallback, and the context menu
  stays a `QMenu` that is merely rendered natively, so all menu wiring and its
  tests are untouched.
- **Two bugs the unit tests could not have caught, both found by running the
  real thing:**
  - Registering the window class per instance crashed the process: a window
    class is process-wide and keeps the procedure it was registered with, so
    later windows ran the first instance's trampoline and dangled once it was
    collected. One class, one dispatcher, per-HWND handler lookup.
  - `ctypes` without `argtypes`/`restype` assumes `c_int`: the first large
    `LPARAM` raised "int too long to convert" inside the window procedure, and
    window handles would have been truncated on 64-bit. Every call is declared
    now.
- Things Qt used to do for us that a hand-rolled icon must do itself: re-add
  the icon on `TaskbarCreated` after an Explorer restart, and delete it before
  destroying its window (otherwise a dead icon stays until hovered).
- **Native menu width is `longest label + 70 px`, and the 70 px is not ours.**
  Measured by opening the real menu, reading its window rect and comparing it
  with `GetTextExtentPoint32W` in the system menu font: the chrome is exactly
  70 px for a one-word menu and for the full one alike, so it is Windows'
  padding, not something the app reserves. Two levers exist: `MNS_NOCHECK`
  removes the check-mark column while nothing is checkable (233 -> 205 px wide,
  257 -> 224 px tall — the column drives the row height too), and shorter
  labels move the width 1:1 (dropping the redundant "last" from three entries:
  205 -> 184 px). Anything beyond that needs owner-drawn items, i.e. drawing
  hover, disabled and dark-mode states by hand.
- **Removing the icon must be the first shutdown step.** `aboutToQuit` runs its
  slots in connection order, and the controller's shutdown joins worker threads
  and child processes, so with the tray close registered after it the icon
  lingered for a second or two after the windows were gone.

## 2026-08-03 (silence gate field data)

- **Flipping a default is not enough — old settings files keep the old value.**
  After the gate defaulted to on, a real session still logged
  `silence_gate_enabled=False` because the stored `settings.json` carried the
  previous default. Schema 22 adopts the new default once for files below it;
  an "off" saved at schema >= 22 is a real choice and is kept.
- **The threshold is confirmed by field data, not by synthetic tones.** One
  session produced 26 silent recordings that were transcribed into invented
  subtitle-style text ("Herr Präsident", "Vielen Dank", "... Musik ...") and 7
  real utterances. Measured peak levels: hallucinations 0.0006-0.0034, real
  speech 0.0075-0.0290. The 0.0040 gate separates them cleanly — every
  hallucination blocked, every real utterance kept, 1.9x margin to the quietest
  real recording. The invented phrases correlated with music playing on the
  machine, but the levels do not: those recordings were *quieter* than the
  silent ones without music, so it is decoder behaviour, not audio bleed.

## 2026-08-03 (tray flyout measurement)

- **Measured on the affected machine, with the app's own tray menu vs. an
  Electron app's:** both menus become the foreground window, and only ours
  makes the Windows 11 overflow flyout close — about 1.0-1.3 s later, not
  immediately. Timeline excerpt: our popup takes the foreground at 10.196 s,
  flyout hides at 11.462 s; the Electron menu takes the foreground at 15.206 s
  and the flyout stays visible until the user dismisses it at 17.886 s. This
  **refutes** the foreground-steal explanation (and with it the earlier
  `SetForegroundWindow` hypotheses, in both directions). Remaining candidate
  differences, still unverified: our popup has no owner window while a
  Chromium menu widget is owned, and the two windows' extended styles differ.
  `scripts/diagnose_tray_flyout.py` now logs style/exstyle/owner so one more
  run compares the two windows directly.
- **Not resolved after all — the fix was reverted.** With the activation in
  place the app logged `tray_menu_activation hwnd=6291618 accepted=True
  foreground=6291618`, i.e. Windows granted the foreground to our icon window
  exactly as it does for Electron, and the flyout still closed. A 50 ms gap
  before showing the menu (matching the reference app's timing) changed nothing
  either. Both the manual popup and the activation were removed: they carried a
  real cost (a Qt-drawn menu, and moving the foreground on every tray click)
  for no measured benefit. Only the untestable candidate remains — how Qt
  registers the icon (`NOTIFYICONDATA` version/flags; Qt's host window is a
  `WS_CAPTION` overlapped window, Electron's a bare `WS_POPUP`) — which would
  mean replacing `QSystemTrayIcon` with a hand-rolled `Shell_NotifyIcon`
  implementation. Not worth it for a cosmetic issue with a one-click
  workaround (pin the icon).
- **What the second run did show:** the foreground going
  to `Electron_NotifyIconHostWindow` at 14.109 s and only 38 ms later to their
  menu widget — the app activates the window that owns the notification icon
  *before* showing the menu, exactly the documented Q135788 pattern. Our menu
  jumped straight from the flyout to the popup. Both menu windows are
  otherwise identical (`ex=0x88` vs `ex=0x200088`, i.e. toolwindow + topmost,
  no owner in either case), so this ordering is the whole difference.
  That ordering was implemented and measured — and it did not help (see
  above), so activation order is not the trigger either.
- **Consequence handled either way:** a dictation started from the tray menu
  used to capture our own menu as the insert target, because
  `get_foreground_window` returned the raw foreground. It now remembers the
  last foreign window and skips our own tool windows.

## 2026-08-03 (later)

- **Hallucinated text from silent recordings had no guard at all in practice.**
  The user dictates with Cohere Transcribe, whose Node/ONNX runtime exposes no
  VAD, no no-speech probability and no confidence signal — its request carries
  only `id/command/audioPath/language/maxNewTokens`, so a silent recording is
  decoded into fluent invented text ("Hallo Herr Präsident"). faster-whisper's
  `vad_filter` would help there but is wired to the *auto-stop* checkbox, which
  is off by default, and `no_speech_threshold` & friends are left at library
  defaults. The app-level silence gate was the only engine-independent guard
  and was also off. It now defaults to on. Measured against synthetic levels:
  digital silence, mic self-noise (-66 dBFS) and room tone (-54 dBFS) are
  blocked, a faint whisper (-40 dBFS → 0.0071) passes the 0.0040 gate with
  ~1.8x headroom, a normal whisper with 5x. Gated audio stays recoverable.
- **`peak_windowed_rms_from_wav` reported unreadable audio as 0.0**, which was
  harmless while the gate was opt-in and became a silent data-loss path the
  moment it defaulted to on. Split into `measure_peak_windowed_rms` returning
  `None` for undecodable audio; the gate only acts on a real measurement. An
  existing test had been passing for the wrong reason (it fed `b"RIFF"` as
  "silence") and now uses a real silent WAV.
- **Record button indicator:** "●"/"■" in a caption are baseline-aligned, so
  the dot sat 1.5 px below the button's middle, the square 1 px, and the
  indicator jumped between states. Painting it in a reserved zone (like the
  language button's chevron) fixes all three. Measured, not eyeballed: the
  button's border is 1 px on every side for every button — only its brightness
  differs (luminance 194 vs. 122), which is what reads as "thicker".

## 2026-08-03

- **The overlay stuttered because one event caused two resizes.** Finishing a
  transcription clears the queue row and then publishes the transcript;
  measured with a `resizeEvent` trace, the window went 183 → 137 → 269 px, and
  the frame in between showed the old content at the new size (a screenshot
  from the user showed the queue text cut off). `OverlayUI.batched_update()`
  now collects the geometry work of one event, resizes with painting
  suppressed and repaints once: 183 → 269 in a single step. Note for future
  measurements: instrumenting `resize()` is misleading, because activating a
  layout also resizes the window through its minimum size — only `resizeEvent`
  shows the real changes.
- **A delayed "Idle" could stop a running dictation.** `_on_model_preload_done`
  evaluates "is a session active" when it *arms* `singleShot(1800,
  show_idle_status)`. If the user starts dictating inside that window the timer
  fires anyway and the overlay claims Idle while the microphone is recording —
  and pressing the hotkey again to "start" really stopped the capture.
  `show_idle_status` now re-checks at fire time. The repeating preload-progress
  poll already did this correctly; the one-shot did not.
- **Qt's tray menu does not steal the foreground.** Measured on Windows 11:
  `QMenu.popup()` leaves `GetForegroundWindow()` at `Shell_TrayWnd` (Qt shows
  popups with `SW_SHOWNOACTIVATE`), so the earlier hypothesis that our menu
  closes the hidden-icons flyout is wrong — bypassing Qt's `setContextMenu`
  (and its `SetForegroundWindow` call) did not change the behavior for the
  user. Remaining explanation: Explorer dismisses its own overflow flyout when
  a hidden icon is clicked. Practical workaround: pin the icon to the
  always-visible tray area.
- **`diagnostics_text()` only saw the live log file** and only its last 300
  lines, so a copied diagnostic could start minutes after the interesting
  events. It now reads the rotated backups oldest-first, keeps 3000 lines and
  prefixes the log path. A fixed line budget turned out to be the wrong knob
  (300 cut the session, 3000 made the clipboard unusable): the text now starts
  at the last `app_session_started` marker, so it covers exactly the current
  run, with 800 lines only as a safety net. Transcripts are still never
  logged.
- **Cohere/Granite defaults:** `keep_onnx_model_loaded` defaults to on. It only
  applies when such a model is selected, and the previous default made every
  dictation reload several GB while the other local engines stayed warm.

## 2026-08-02

- **Overlay refused to shrink because `resize()` clamps to a stale minimum.**
  After a long transcript the overlay stayed at its expanded height even when
  the state switched to a short error message. Root cause: `QWidget.resize()`
  clamps the requested size to the widget's *current* minimum size, and that
  minimum is only recomputed when the layout is activated (normally deferred to
  the next event-loop pass). Immediately after shrinking the detail area the
  window still carried the previous state's larger minimum, so the resize was
  silently swallowed — growing always worked, shrinking never did. Fix:
  `_resize_window` activates both layouts before resizing. Activating the
  layout also exposed a second, older inaccuracy: the container's stylesheet
  border adds 1 px contents margin per side, which no size formula counted, so
  every computed target was 2 px below the real layout minimum and
  `OVERLAY_MAX_HEIGHT` was quietly exceeded. `set_state` now applies the state
  stylesheet before measuring and all formulas add `_container_frame_margins()`.
- **A language switch used to unload the model.** `language_mode` was part of
  both the transcriber cache key and the preload key, so choosing another
  language tore the loaded runtime down and reloaded it — even though no local
  runtime depends on the language: faster-whisper passes it per
  `transcribe()` call, Nemotron sets `lang_id` per session, the Cohere/Granite
  Node process takes it as a JSON request field, and the remote providers put
  it into request parameters. Consequences in daily use: a mis-clicked language
  blocked the correction behind a full model load (the overlay's language
  button is disabled while "Processing"), and transcribing one import in
  another language evicted the model the next dictation needed. The language is
  now applied to the live instance through `ITranscriber.set_language_mode`
  when a job acquires the runtime; the cache and preload keys ignore it.
- **Retry was the wrong offer after a failed insertion.** A successful
  transcription clears `_last_failed_wav_bytes`, so the Retry button shown in
  the Error state after a failed paste could only answer "No failed
  transcription to retry". The Error state now shows the transcript itself
  (it was invisible exactly when the user needed to read it) and offers Insert
  instead, wired to the existing re-paste path.
- **Qt closes the Windows 11 hidden-icons flyout when the tray menu opens.**
  Qt's Windows tray backend calls `SetForegroundWindow` on its hidden helper
  window before tracking a menu registered via `setContextMenu`, so Explorer's
  overflow flyout loses the foreground and light-dismisses itself. Popping the
  `QMenu` up ourselves from `activated(Context)` skips that call — Qt still
  emits `Context` when no platform menu is set. Note that Electron's tray code
  calls `SetForegroundWindow` too, so the comparison with ChatGPT/Claude
  desktop does not prove a framework-level difference; this needs a real-world
  check on the target machine.
- **No fallback model exists (wording only).** The startup notice
  "Model 'X' is ready. Next transcription uses it." read as if something else
  had been used before. There is no fallback: preload is strict about the
  selected model and a recording waits for it. The message is now plain
  "Model 'X' is ready."

## 2026-07-21

- **Runtime upgrade check: mostly current; no measurable perf win available.**
  Benchmarked on HomeBase (Ryzen 5 7600X, Arc A750, 199 s German dictation,
  runs=2 + warmup, fixed language): Transformers.js 4.1.0 -> 4.2.0 changed
  Cohere/Granite WebGPU inference by -0.4 %/-4 % (within noise, transcripts
  bit-identical); CTranslate2 4.7.1 -> 4.8.1 looked ~6 % slower on
  large-v3-turbo until an A/B/A counter-run showed the machine itself had
  drifted ~8 % slower over the session — verdict: no measurable difference.
  CTranslate2 4.8.0's advertised int8 speedup (PACKED_GEMM, ~+22 %) applies
  only to Intel-MKL CPUs; AMD runs oneDNN and is unaffected. faster-whisper
  1.2.1 and onnxruntime-genai 0.14.1 are the latest releases.
  **Key structural finding:** Transformers.js hard-pins an exact
  `onnxruntime-node` version (1.24.3 across 4.0/4.1/4.2 and main), so bumping
  the top-level `onnxruntime-node` cannot speed up the Cohere/Granite
  pipeline models — npm then nests a private 1.24.3 for Transformers.js and
  two different native ORT runtimes coexist in one Node process (observed
  "requested API version [27] is not available" warning). Only the raw
  Granite Plus/NAR `InferenceSession` paths would use a newer top-level
  runtime. DirectML for GenAI remains blocked upstream
  (`onnxruntime-genai-directml` 0.14.1 requires `onnxruntime-directml>=1.26`;
  PyPI still tops out at 1.24.4). Worthwhile low-risk maintenance for a
  future release: ctranslate2 4.8.1 (model-load heap-overflow security fix)
  and Transformers.js 4.2.0 (verified compatible); neither is urgent.
- **Explicitly selected microphones failed with PortAudio -9997.** Work-machine
  diagnostics showed every warm and cold `sd.InputStream` open failing with
  "Invalid sample rate [PaErrorCode -9997]" as soon as a specific microphone
  was selected, while "System default" kept working. Root cause: explicit
  selections resolve to WASAPI device indices (untruncated names, one entry
  per endpoint), and PortAudio's WASAPI backend opens shared-mode streams
  only at the endpoint's shared mix format (typically 48 kHz) — the app
  captures at 16 kHz. The default path goes through the MME sound mapper,
  which resamples transparently. Fix: `input_stream_extra_settings` returns
  `WasapiSettings(auto_convert=True)` for WASAPI devices and both stream-open
  sites pass it, enabling PortAudio's own sample-rate conversion.
- **Release v0.7.0 never published: npm audit gate.** The tag build failed in
  "Run release quality gates" because a fresh advisory (GHSA-xcpc-8h2w-3j85,
  adm-zip < 0.6.0, high) reaches the production tree via onnxruntime-node ->
  @huggingface/transformers; the Quality workflow's Linux audit went red for
  the same reason. onnxruntime-node uses adm-zip only in its install script
  to unzip vendor archives, and adm-zip 0.6.0 is the API-compatible fix
  release, so a package.json `overrides` entry pins adm-zip to ^0.6.0
  (lockfile-only change). The v0.7.0 tag exists without a GitHub release;
  v0.7.1 ships the fixed tree.

## 2026-07-20

- **Optional show-overlay hotkey.** New `show_overlay_hotkey` setting
  (schema 20, empty default = disabled; e.g. Ctrl+Alt+F11) registers a third
  global hotkey whose only action is `bring_overlay_to_front` — the same
  reveal as the tray "Show overlay" — so a floating overlay can be brought up
  to check the last transcript without the mouse. Empty stays empty (no
  default combo is ever substituted, nothing is registered by default), the
  Save flow validates the combo and rejects conflicts with the recording and
  cancel hotkeys, and registration mirrors the cancel-hotkey model including
  the resume-path refresh and disabled-state unregistration.
- **Inline field buttons rendered taller than their fields or clipped.** The
  reported symptom was the microphone Refresh button "slightly cut off at the
  bottom" and field-adjacent buttons visibly taller than their inputs. Root
  cause: the dialog-level `BUTTON_FEEDBACK_STYLESHEET` gives every QPushButton
  a QSS box of min-height 24 px + 4 px vertical padding + 1 px borders
  (~34 px), while `_match_field_button_height` fixes heights to the native
  input hint (~24 px) *before* the widgets are reparented into the styled
  dialog; after reparenting, the QSS minimum beats the fixed height (button
  taller than its combo) or the style draws past the allocated rect (clipped
  bottom), depending on layout context. Fix: matched buttons are tagged with
  an `inlineFieldButton` stylesheet property whose rule shrinks the QSS box
  (min-height 0, 1 px vertical padding) so the field's native height wins.
  Verified with an offscreen windows11-style rendering; a regression test
  asserts equal actual heights and that the QSS minimum fits the matched
  height for all matched rows.
- **Run Benchmark window opens larger.** 820x720 -> 860x880, bounded to the
  available screen at build time, so expanding "Show Run Options" no longer
  squeezes the installed-models list until the window is resized manually.
- **General tab split: new Audio & Recording tab.** The General tab had grown
  to six group boxes (~25 rows). The set-and-forget capture setup ("Audio &&
  Voice Detection" and "Recordings") moved to a dedicated tab directly after
  General (`settings_dialog_audio.py` mixin); General keeps Hotkeys, Display,
  Engine && Mode, and Text Insertion. Widget attribute names are unchanged so
  persistence, controller wiring, and the test seams kept working; the shared
  form label column now spans both tabs and is applied by `_build_audio_tab`.
- **Overlay hotkey now ships preset (schema 21).** `show_overlay_hotkey`
  defaults to Ctrl+Alt+F11 for an out-of-the-box experience but stays
  clearable: the new `_normalize_optional_hotkey` keeps a stored "" as a
  deliberate disable (only invalid non-empty values fall back to the
  default), and a schema<21 empty value migrates to the default once because
  schema 20 briefly used "" for "never configured".
- **Re-paste last transcript.** `controller.repaste_last_transcript` inserts
  the last transcript into the currently focused window via the normal
  insertion path; reachable from the tray ("Insert last transcript again")
  and an optional `repaste_hotkey` (default empty — a global paste combo is
  riskier than an overlay reveal). Blocked while a recording/stream is
  active; no new history entry.
- **Completion tone.** `completion_beep_enabled`/`completion_beep_tone`
  (default off/chime, shares the start-tone table) plays after successful
  foreground-batch, queued-background, and re-paste inserts on a worker
  thread; streaming appends and history-only delivery stay silent. The
  recording-start beep remains synchronous on purpose (keeps the tone out of
  the microphone).
- **Tray middle-click toggles dictation.** `tray_middle_click_toggle`
  (default on, Display group) makes a middle-click on the tray icon act like
  the recording hotkey; the guard reads live controller settings so the
  checkbox applies without restart.

## 2026-07-18

- **The warm microphone stream now follows device changes.** Root cause of
  "I switched the Windows input device but the new microphone never became
  active": the warm stream binds the default endpoint once at open and was
  only ever reopened on settings toggle, system resume, or app restart — a
  device switch kept recording from the old endpoint (or from a dead stream
  that `is_running` still reported as healthy). New `audio_device_listener.py`
  registers an MMDevice `IMMNotificationClient` via comtypes (event-driven, no
  polling); default-capture switches and hot-plug events funnel into a
  coalesced controller reaction that closes the idle warm stream,
  re-initializes PortAudio (its device list is frozen at init), and reopens
  the warm stream on the fresh list. A first-callback watchdog timeout on a
  warm capture triggers the same refresh as a self-heal.
- **Warm-stream lifecycle races closed.** Disabling `keep_microphone_warm`
  during a recording used to hard-close the stream under the attached capture
  (silent audio loss, since the watchdog had long passed); the resume restart
  checked `_audio_capture` on the Qt thread but closed later on a worker,
  racing a just-starting recording. `WarmMicrophoneStream.request_close` /
  `request_restart` now defer under the stream's own lock while a consumer is
  attached and execute on detach, which removes both races at the source.
- **Microphone picker with strict resolution.** New `input_device_name`
  setting (General tab; default "System default" follows Windows via the
  PortAudio/MME sound mapper). Names are listed WASAPI-first (untruncated, one
  entry per endpoint) and resolved to a PortAudio index only at stream open;
  re-enumeration is guarded by a shared open-lock plus live-stream registry so
  `Pa_Terminate` can never invalidate an open stream. A stored-but-missing
  device stays visible as "(not connected)", and recording with it selected
  fails with an actionable error instead of silently using another device.
  The warm attach path is device-keyed so a stale warm stream on the wrong
  device is bypassed with a cold open on the right one.

## 2026-07-14

- **Persistent history views refresh on activation, not only on navigation.** A
  settings/history dialog can remain open while transcripts arrive elsewhere,
  so switching to its tab was insufficient. Both history surfaces now force a
  reconciliation when the window becomes active; the existing refresh path
  retains the selected entry and scroll position whenever that entry remains.
- **Audio-import choices form an independent job snapshot.** Import now owns a
  model-aware batch-language selector and passes its value through the GUI-thread
  settings snapshot. It never implicitly uses or mutates the General-tab
  language, and constrained models only offer their supported import languages.
- **Capture readiness and the first-callback watchdog are race-safe.** The
  overlay previously said "Speak now" before a slow microphone or streaming
  session had started, which could lose every word spoken during device open.
  It now shows an explicit wait message and publishes the ready-to-speak detail
  only after capture succeeds. Streaming session references are installed
  before `capture.start()` so an immediate callback is forwarded. A callback
  timeout aborts instead of entering the normal transcription path; bytes that
  arrive at the timeout boundary stay available for Retry without producing a
  simultaneous transcript and Error. Stop diagnostics snapshot the warm-stream
  state before capture teardown resets it.
- **Overlay start and reveal preserve visibility without hiding readiness.**
  The explicit wait and ready-to-speak messages share the same state color, so
  changing between them no longer reapplies the full stylesheet. Compact size
  is reasserted after the bounded Qt event drain, because deferred layout work
  could otherwise leave the previous expanded result geometry visible.
  Floating overlays first use a typed native
  `SetWindowPos(HWND_TOPMOST)` call; when Windows rejects that call, a temporary
  `WindowStaysOnTopHint` fallback keeps the overlay visible instead of hidden.
- **The overlay Language control owns its centered chevron.** The native
  `QPushButton.setMenu()` indicator remained visibly misaligned in the target
  Windows overlay. The button now opens its menu explicitly and paints an
  antialiased chevron in a dedicated right-hand zone; Qt applies normal device
  scaling to those logical painter coordinates. Its regression renders the
  button, verifies the arrow pixels stay in that zone and centered, and checks
  the explicit popup. Settings comboboxes retain their native appearance.
- **The Local Models inventory consumes available vertical space.** Changing
  its group to a preferred fixed height left most of a resized Settings window
  blank while the useful model list stayed short. The expanding policy and
  stretch were restored so resizing exposes more inventory rows.

- **Granite Speech 4.1 NAR now prefers its verified CPU path.** The normal
  `auto` policy previously retried WebGPU and DirectML even though the NAR
  encoder is known to fail on both providers, then discarded the CPU fallback
  so the same attempts could recur on the next dictation. NAR now resolves
  `auto` directly to CPU. Explicit WebGPU/DirectML benchmark targets still
  bypass the preference so future runtime or graph improvements remain testable.
  The General-tab note and runtime diagnostics explain this model-specific
  behavior instead of claiming that a GPU fallback failed. Explicit CPU policy
  selections are likewise reported as intentional, not as unavailable GPUs.

## 2026-07-10

- **Benchmark layout inverted after user feedback (design lesson).** The
  slim-launcher tab shipped earlier today had the usage backwards: viewing
  results/history is the frequent action, running a benchmark is rare. The tab
  now hosts history + results directly (with a live status label next to a
  "Run Benchmark..." button), and the pop-out window contains only the run
  side; the model-list actions were compacted into one row of small buttons.
  Lesson: derive UI structure from action frequency, not from "which parts are
  heavy".
- **Immediate insert folded into the "While transcribing" combo and the
  mid-recording rule loosened.** The separate checkbox plus "Queue &" wording
  confused more than it explained — the queue always exists; the options only
  differ in what happens to the older transcription and when results insert.
  The combo now offers finish-insert-when-idle / finish-insert-immediately /
  finish-history-only / cancel (UI value `insert_immediate` maps back to the
  unchanged settings keys). Immediate delivery also pastes during an active
  batch recording again, restoring focus to the job's target — the original
  queue behavior. The user correctly pushed back on the foreground-window
  restriction added a day earlier: the historical "insert near a hotkey press
  fails" bug was the held-modifier Ctrl+V corruption (now fixed at the root),
  not the mid-recording insert itself. Streaming captures still block.
- **Streaming abort no longer loses the partial transcript.** A focus-change
  or cancel abort dropped everything already transcribed from UI and history
  (only the text pasted so far survived in the target window). The abort now
  saves the live transcript to history, keeps it for the overlay Copy action,
  and shows it in the abort message.
- **Custom vocabulary biasing added across providers.**
  `custom_vocabulary` (General tab) is parsed once
  (`config.parse_custom_vocabulary`) and wired per provider: faster-whisper
  `initial_prompt` (batch + rolling-window streaming), OpenAI/Groq `prompt`,
  AssemblyAI batch `word_boost` (the installed streaming v3 SDK exposes no
  biasing parameter), Deepgram repeated `keyterm` (nova-3) / `keywords`
  (nova-2) query params (batch + streaming, `doseq=True`). ElevenLabs, Azure,
  Fun-ASR, Nemotron, and Cohere/Granite ONNX expose no biasing input.
- **Multi-select lists switched to ExtendedSelection** (Shift ranges, Ctrl
  toggles) for Local and Benchmark model lists, matching the History lists
  and the file explorer.
- **Stacked Model row spacing fixed.** The shorter stack page absorbed the
  extra height into its word-wrapped note label, which centers text
  vertically — producing equal empty bands above and below the note. Notes
  are now top-aligned with a trailing stretch and a two-line reserve.

- **Mid-recording insert + insert-target setting.** With
  `immediate_background_insert`, a finished queued result now pastes during an
  active *batch* recording when its captured target is already the foreground
  window — the paste lands where the user is dictating anyway and needs no
  focus steal. A streaming recording never allows it (live inserts + focus
  abort), and a result targeting another window stays deferred (never steal
  focus mid-recording); deferral is decided per job in the flush. The new
  `insert_target` setting chooses between the recording-start window snapshot
  (default) and the window focused when the transcript is ready; the caret
  position inside the target is always the position at insert time because
  Windows cannot paste at a remembered caret offset.
- **Cut-off first words on locked-down machines = slow microphone open.** On a
  GPO/EDR-heavy work PC, `sd.InputStream(...).start()` can take seconds and
  everything spoken before the stream runs is silently lost; the overlay only
  shows "Speak now" after the open finishes, so the app was honest but slow.
  Fix: opt-in `keep_microphone_warm` keeps one shared PortAudio stream open
  (`WarmMicrophoneStream`) and a recording merely attaches as consumer —
  effectively instant start. Detach must compare bound methods with `==`
  (each `self._on_audio` access creates a new object; an `is` check silently
  failed to detach — caught by a test). `recording_start_timing` logs beep +
  capture-start durations and warns above 500 ms.
- **Silence gate against hallucinated words.** Whisper-family models
  hallucinate text from pure silence. Opt-in gate: if the loudest 100 ms
  window of a batch recording stays below a tunable RMS threshold
  (default 0.004, deliberately below whisper level), transcription is skipped
  and the overlay reports the measured level. Windowed peak measurement keeps
  short whispers detectable that full-recording averaging would dilute;
  `recording_peak_level` is logged on every batch stop for tuning.
- **Two real overlay layout-shift bugs fixed.** (1) The transcript label's
  wrap width came from the live scroll viewport, which changes after the
  deferred queue resize and with scrollbar visibility — the same text
  re-wrapped a moment after "Done" and visibly jumped. It now wraps at a
  width derived from the target window width and pre-measures whether the
  scrollbar will appear. (2) `_apply_window_flags` called `setWindowFlags`
  unconditionally; that recreates the native window and blinked the overlay
  on every hotkey reveal. It now only rebuilds when the flags actually
  change. Also, the Local-tab model runtime note keeps a reserved three-line
  area (neutral gray note for faster-whisper models) instead of toggling
  visibility, so model switches no longer shift the widgets below.

- **Unified model selection on the General tab ("General = choose, Local/Remote
  = manage").** The Local tab's "Model Size" row (`model_combo` plus the
  reserved-height `local_model_runtime_warning_label`) moved into the General
  tab's "Engine && Mode" group box, physically joining the existing remote
  model widget under one "Model" form row. Both pages live in a
  `model_selector_stack` `QStackedWidget` (page 0 = local, page 1 = remote);
  `_update_remote_model_selector` now also calls `_update_model_selector_page`
  to flip the page whenever the engine changes. `QStackedWidget.sizeHint()`
  already returns the max size across all of its pages regardless of the
  current index, so the row never resizes on an engine switch without any
  extra padding tricks — the remote note label's minimum height was simply
  raised to match the local page's three-line reserved height. Widget
  attribute names (`model_combo`, `local_model_runtime_warning_label`,
  `remote_model_combo`, etc.) were kept unchanged; only their parent/placement
  moved. The Local tab is now local-model management only (Model Dir,
  inventory, download queue, delete) with a short gray note pointing users to
  the General tab for the active model.

- **Redesigned the Benchmark tab into a slim launcher + pop-out window.** The
  tab was overloaded (model selection, run options, run controls, status,
  results tables, and history all stacked in one scrolling tab), leaving each
  pane tiny. The tab is now a short, non-scrolling page: an explanation label,
  a most-recent-run summary line ("Last run: ... - N models" or "No benchmarks
  yet"), and an "Open Benchmark Window" button. The full benchmark UI (the
  existing History/Results/Run-controls vertical splitter, unchanged) moved
  into a resizable, non-modal `benchmark_window` (~980x720, owned by the
  settings dialog) built by `_build_benchmark_window`; `_open_benchmark_window`
  raises/activates the existing window instead of creating a second one and
  refreshes the history list on open. The window hides together with the
  settings dialog via a new `closeEvent` override. All `_BenchmarkMixin`
  widget attribute names, the `_facade()` patch seam, and the splitter/list
  `AdjustToContents`/per-pixel-scroll conventions were kept unchanged — only
  the container moved from the tab to the window.

## 2026-07-09

- **Root-caused the long-standing intermittent paste failures (wrong clipboard
  content pasted; queued transcripts landing "only in history").** Two distinct
  races, both in the clipboard paste path, explained every reported symptom:
  1. *Held hotkey modifiers corrupt the injected Ctrl+V.* The recording hotkey
     is `Ctrl+Alt+Space` and cancel is `Ctrl+Alt+F12`; every insert triggered
     synchronously from the WM_HOTKEY press (stop, cancel, deferred-queue
     flush) ran while the user still physically held Ctrl+Alt. SendInput adds
     its own Ctrl+V *on top of* the real keyboard state, so the target app
     received Ctrl+Alt+V — AltGr+V on a German layout — which is not a paste
     in most apps. Nothing was inserted, no error was raised anywhere (the
     injection itself succeeds), and the transcript existed only in history.
     With a multi-item queue flush (~230 ms per item) the first ~2-3 items were
     corrupted until the keys were released, matching "only 3 of 6 inserted".
     Fix: `Win32ClipboardBackend.wait_for_modifier_release` polls
     GetAsyncKeyState for Ctrl/Alt/Shift/Win before the clipboard is touched
     (bounded by `PASTE_MODIFIER_RELEASE_TIMEOUT_S`); WM_PASTE mode skips it.
     This was also why "pressing the hotkey while an insert ran" used to break
     insertion — the historical reason immediate queue delivery was removed.
  2. *Late clipboard read loses against the fixed 160 ms restore.* The injected
     Ctrl+V is processed asynchronously by the target's message loop; under
     transcription CPU load (local Whisper pegging all cores — i.e. exactly
     when a queue exists) the target can read the clipboard after
     `SENDINPUT_RESTORE_DELAY_S`, receiving the already-restored *previous*
     clipboard content instead of the transcript. Fix: before starting the
     restore delay, `wait_for_paste_target_ready` waits until the target
     thread answers WM_NULL again (SendMessageTimeout); if it stays
     unresponsive past `PASTE_TARGET_RESPONSIVE_TIMEOUT_S`, the restore is
     skipped so the eventual late paste still reads the transcript. With
     `keep_transcript_in_clipboard` enabled the restore is skipped entirely,
     eliminating this race for that configuration.
  Windows offers no "clipboard was read" signal (delayed rendering exists but
  clipboard history/managers request the data immediately, defeating it), so
  a heuristic delay remains after the responsiveness gate — accepted as
  practically irrelevant after the gates.
- **Deferred queue-insert flush is now coalesced per target window.** Six
  queued results used to flush as six separate set/paste/restore cycles, each
  its own race window (and ~230 ms of Qt-thread blocking each). Same-target
  results are now joined (space-separated, unless a boundary already has
  whitespace) and pasted in one cycle; different targets stay separate pastes.
- **Continuous queue delivery is back as an opt-in setting**
  (`immediate_background_insert`, General tab, default off): a finished queued
  transcription inserts as soon as it completes even while another
  transcription runs. It had been removed because inserts coinciding with a
  hotkey press failed mysteriously — that was race 1 above, now fixed at the
  root. An active recording still always blocks insertion, and the serial
  worker keeps insert order equal to recording order.

## 2026-07-06

- **The overlay now has a model-aware language quick selector.** It uses
  `config.language_modes_for_selection()` like the General settings tab, saves
  changes immediately for the next recording, and safely resets or defers the
  cached transcriber so the new language takes effect. Language changes are
  blocked while listening/processing. Engines or modes with only automatic
  detection show a disabled `Lang: Auto` button instead of a meaningless
  menu. Cohere exposes its 14 explicit languages and no Auto option, matching
  its official model card: the model does not perform automatic language
  detection and performs best with one pre-specified language.

## 2026-07-05

- **Cancel (Ctrl+Alt+F12) left completed pending inserts stuck behind an
  unrelated transcription.** Scenario: msg1 finished and is deferred as
  "Insert Pending", msg2 is still transcribing (`_active_request_token` set),
  and the user cancels the active recording with the cancel hotkey. The cancel
  paths already flushed deferred inserts, but the flush guard
  (`_should_defer_background_insertion`) treated *any* in-flight transcription
  as a blocker, so msg1 was not delivered until msg2 finished — up to a minute
  later, which reads as "deleted, only in history" (the transcript is appended
  to history the moment it is deferred, so it shows in history but is not yet
  pasted). Fix: the guard and `_flush_deferred_background_results` gained an
  `ignore_active_transcription` flag. An active recording/capture (or
  in-progress start/stop) stays a hard blocker — never insert mid-recording —
  but explicit user cancels (`cancel_current_action` incl. its "nothing to
  cancel" fall-through, `cancel_queued_transcription`, `_abort_streaming_session`)
  now flush with `ignore_active_transcription=True`, delivering every completed
  deferred insert immediately into its own captured window. Deferred tokens are
  always older than the active one, so order stays intact and the running
  transcription still delivers itself later with no duplicate. Normal
  (non-cancel) flow is unchanged. This is distinct from the queue-row cancel fix
  below (which only closed the flush gap for `cancel_queued_transcription`);
  here the completed result was being *delayed*, not dropped. Regression test:
  `test_cancel_recording_delivers_deferred_insert_despite_active_transcription`.
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

## 2026-07-11

- **ModelScope fallback download hardening:**
  - Mirror endpoints and redirects are HTTPS-only, and all remotely listed
    paths are validated as normalized POSIX-relative paths whose resolved
    destinations remain inside the requested model directory.
  - Downloads now write and resume exclusively through `*.incomplete`. Resume
    responses require a matching HTTP 206 `Content-Range`; a server that ignores
    `Range` safely restarts the incomplete file instead of appending duplicate
    data.
  - Completed data is flushed, synced, checked against the expected byte size,
    and atomically moved to the final filename. Interrupted data remains only as
    a resumable incomplete artifact.
  - Deterministic tests cover POSIX and Windows traversal forms, HTTP endpoint
    rejection, interrupted/resumed transfers, ignored and mismatched ranges,
    legacy partial-file migration, and successful atomic publication.
- **Shared transcriber runtime ownership hardening:**
  - Batch inference, model preload, and live streaming now hold explicit,
    transferable runtime leases for their complete use of a transcriber.
  - One lease owns the shared cache; overlapping normal work uses an isolated
    close-on-release runtime, while preload waits off-thread so a successful
    preload remains cached without blocking the Qt thread.
  - Cache resets and shutdown closes are deferred until the shared owner exits;
    canceled workers remain active until then. Terminal worker signals now emit
    only after callback cleanup and lease release, and shutdown ignores late
    queued results.
  - Added real-thread barrier regressions for overlap isolation, canceled-worker
    reset deferral, shutdown close ordering, and terminal-signal cleanup order.
- **Microphone stream lifecycle hardening:**
  - Warm-stream opening no longer holds the shared state lock while PortAudio
    may spend seconds opening a device, so an attach attempt immediately falls
    back instead of freezing the UI.
  - Capture callbacks now carry a recording generation. A callback retained
    after detach is rejected even if a new recording has already attached.
  - Cold and warm cleanup always attempts `close()` after a failed `stop()`, and
    partially opened streams are closed when startup fails.
  - Added regression tests for stop/start failures, non-blocking attach, and a
    delayed callback crossing a recording boundary.
- **Settings worker ownership now survives window dismissal:**
  - Settings is a single application-lifetime dialog that is hidden and reused
    instead of deleted and recreated. This preserves the busy state for model
    downloads, benchmarks, imports, scans, and remote checks, preventing a
    reopened dialog from starting duplicate work while an orphan worker from
    the previous instance is still active.
  - Reopening while idle still reloads persisted settings and discards unsaved
    provider-key edits before presentation. Reload is deferred while owned work
    is active so operation inputs and disabled/busy controls remain intact.
    Every hide/reject/close path hides the separate Run Benchmark window.
  - Application shutdown explicitly cancels dialog-owned model-download and
    benchmark process work and briefly joins their coordinator threads for
    cleanup.

- **Repository quality gates now run before release time:**
  - Added a read-only GitHub Actions workflow that installs the locked Python
    environment and runs Ruff plus the complete pytest suite on Windows.
  - Added a separate fast JavaScript lockfile audit so vulnerable WebGPU runtime
    dependencies fail review branches and pull requests without downloading
    model assets or executing package install scripts.
- **JavaScript runtime dependency audit:**
  - `npm audit` found high/moderate advisories in the transitive `protobufjs`
    tree used by `onnxruntime-web`.
  - Refreshed the lockfile to patched `protobufjs` 7.6.5 and
    `@protobufjs/utf8` 1.1.2 without changing the direct runtime versions.
  - The three WebGPU runtime imports still load successfully and the production
    dependency audit reports no known vulnerabilities.
- **Portable Node bootstrap supply-chain hardening:**
  - The bootstrap previously trusted any non-empty archive returned by either
    configured mirror and passed it directly to `ZipFile.extractall`.
  - Node versions are now strict numeric semantic versions, archives are
    checked against the release directory's published SHA-256 list, and every
    ZIP member is verified to remain inside the selected installation folder.
  - Regression tests cover valid downloads, checksum mismatch cleanup, version
    path injection, safe archives, and parent-directory traversal attempts.
- **Remote streaming session and backpressure hardening:**
  - AssemblyAI and Deepgram callbacks now carry a session generation and exact
    client identity, so late reader, error, and close events from a retired
    connection cannot mutate a later recording.
  - Both providers explicitly reserve starting and retiring states. AssemblyAI
    cleans up clients whose connect only partially succeeds, including bounded
    disconnect when connect raises or reports an asynchronous error.
  - Deepgram microphone chunks enter a bounded queue through `put_nowait`; a
    saturated sender fails visibly without blocking PortAudio or silently
    dropping speech.
  - Deepgram stop uses a deterministic sender barrier before the documented
    `Finalize` and `CloseStream` messages. Optional `from_finalize` responses,
    server close, sender drain, and control-send waits are all bounded.
  - Barrier, queue-saturation, partial-connect, and stale-callback regressions
    cover the new lifecycle ordering.
- **Provider, export, and update trust boundaries:**
  - Imported audio now retains its real MIME type across OpenAI, Deepgram,
    ElevenLabs, and Azure requests. Multipart boundaries are randomized and
    field/header injection is rejected.
  - Azure credential-bearing endpoints accept only documented HTTPS Azure host
    suffixes with the expected path shape and reject userinfo, custom ports,
    fragments, queries, and foreign hosts.
  - GitHub release responses are size-bounded, tags use strict numeric SemVer,
    and release links are restricted to this repository. Manual checks promote
    an in-progress startup request so the visible result is not lost.
  - Benchmark CSV exports neutralize spreadsheet formula prefixes in every
    user-controlled cell.
- **Controller, input, and import race hardening:**
  - Clipboard insertion now aborts when modifier release or target focus cannot
    be confirmed; partial `SendInput` failure releases injected keys and never
    replays the paste sequence. Recording cleanup matches only managed filename
    patterns.
  - Imports run in the controller's single inference lane and snapshot managed
    recording bytes plus identity. Conditional state transitions prevent an old
    import from clearing a newer recording, and cleanup errors no longer discard
    successful transcripts.
  - VAD auto-stop is marshaled onto the Qt thread. Background/import history no
    longer changes the foreground Edit target.
  - Failed hotkey unregistration preserves the registered state and blocks an
    unsafe replacement.
- **Persistence and credential transaction hardening:**
  - Persisted booleans use strict JSON parsing, preventing strings such as
    `"false"` from enabling security-sensitive options.
  - One normalized path-lock registry serializes read-modify-write operations
    across history, benchmark, settings, diagnostics, model inventory, last
    recording, and insecure-key store instances.
  - Insecure fallback mode applies only on explicit save. Failed key edits stay
    visible, partial credential success invalidates cached clients, and history
    is trimmed only after settings persistence succeeds.
  - Credential deletion surfaces inaccessible backends and stale plaintext
    cleanup failures instead of reporting false success.
- **Local inference process/session hardening:**
  - faster-whisper and Nemotron stream workers now own immutable generation
    state; timed-out old workers cannot consume or publish into a new stream.
  - Nemotron defers native teardown until retired workers exit. The Node parent
    uses per-process queues, bounded stderr, absolute deadlines, and restarts a
    timed-out or poisoned child.
  - The JS runner serializes stdin requests and validates protocol objects plus
    RIFF/chunk/data/block bounds before typed-array allocation.
  - Long-running model downloads spool diagnostics to a temporary file instead
    of risking deadlock on an unread stderr pipe.
- **Release and offline model workflow hardening:**
  - Manual model imports hash complete content, stage snapshots, repair legacy
    partial directories, publish atomically, and update `refs/main` only after
    success.
  - Release builds install locked Python/JavaScript dependencies and run lint,
    full tests, and dependency audit. Release creation rejects untracked files;
    version updates prevalidate all targets and roll back earlier writes after a
    later failure.
  - ElevenLabs `scribe_v1` was removed from runtime/UI after its 2026-07-09 API
    retirement; legacy settings migrate to `scribe_v2`.
- **AssemblyAI Universal-3.5 Pro correction and migration:**
  - Universal-3.5 Pro exists for both async and realtime transcription under
    the current `universal-3-5-pro` model identifier. The earlier review that
    denied the async model was incorrect and resulted from not verifying the
    newly published AssemblyAI release before changing the integration.
  - Batch and realtime now request Universal-3.5 Pro explicitly. Batch no
    longer appends Universal-2 as a silent fallback, and stored Universal-3 Pro
    selections migrate to the current model.
  - Both paths use the current `keyterms_prompt`; realtime relies on native
    18-language code switching and does not send retired legacy parameters.
- **Consecutive insertion boundaries:**
  - Successful transcript inserts remember the captured target control. A
    later transcript into that same control receives one boundary space while
    punctuation continuations, existing whitespace, different controls, and
    live streaming deltas remain untouched.
  - Queue and immediate-delivery regressions confirm that separation also
    applies when transcriptions finish in different controller paths.
- **General and benchmark UX pass:**
  - General-tab hints now sit 2 px below their field and 10 px before the next
    row. Dynamic model and language notes reserve two lines, keeping every
    later row stationary across engine changes.
  - Vocabulary guidance documents all separators, preserved multi-word
    phrases, and the exact supported/unsupported provider and local-model paths.
    The ambiguous "While transcribing" field is now "New Recording" and every
    choice explicitly describes what happens to the previous job.
  - The Benchmark runner defaults to 820x720, loaded-result actions live beside
    Results, and the UI states that completed/partial results are saved to
    history automatically. New faster-whisper cases record CTranslate2's
    resolved device.
  - The retained July 11 four-run Arc A750 benchmark documents a 32-39% first
    WebGPU-run overhead without warm-up. Warm-up occurs after measured model
    loading and primes inference compilation, pipelines, kernels, and caches.
- **Secure in-app update foundation:**
  - Update/no-update dialogs have explicit contrast-safe normal, hover,
    pressed, disabled, and primary-button colors.
  - Release builds publish a post-build SHA-256 companion. In-app downloads
    require exact repository assets, trusted HTTPS redirects, bounded declared
    size, atomic partial publication, and a matching checksum.
  - Installer launch remains disabled until Windows validates Authenticode and
    its full publisher subject matches an explicit pin. GitHub Verified commits
    are source-history signatures and do not satisfy Windows code signing.
  - Settings prewarm/paint callbacks are now owned timers, eliminating delayed
    calls into already-deleted Qt dialog objects found during focused UI tests.
- **Final dependency audit refresh:**
  - A fresh July 11 audit found newly disclosed advisories in the locked
    `cryptography`, `idna`, `pygments`, and `pytest` versions.
  - The lock now resolves `cryptography` 49.0.0, `idna` 3.18, `pygments`
    2.20.0, and `pytest` 9.0.3. The synchronized Python environment reports no
    known vulnerabilities, and the complete 1,063-test suite remains green.
- **Strict selected-model preload semantics:**
  - Local recordings may begin while the selected model loads, but the worker
    waits for that exact immutable settings snapshot and never substitutes or
    persists a faster-whisper fallback.
  - A second review found and closed a stream/batch lease deadlock, three
    cancellation races, and a preload-completion overlay race. Cancellation and
    completion are generation-scoped, and exact-model isolated runtimes remain
    available when a live stream owns the shared runtime.
- **History audio provenance and retranscription:**
  - New history entries can retain an immutable archive/external source path;
    managed last-recording paths continue to be checked by recording ID so an
    overwritten debug WAV is never associated with an older transcript.
  - History refreshes on tab activation. A selected entry can be reopened on
    Import Audio with its original engine/model preselected or revealed in File
    Explorer when the audio still exists.
- **Benchmark output quality is inspectable:**
  - Every measured run now persists and exports its real transcript. Benchmark
    History uses a structured table, and the Transcripts view shows full text
    plus exact-match/difference status against run 1 for each model/device case.
  - Older benchmark entries remain compatible and clearly show that their
    transcript text was not stored.

## 2026-08-11

- **Reported cancel behavior was not a bug:**
  - The report was "cancelling with the shortcut cancels everything in the
    pipeline". `cancel_current_action` targets only the active recording or,
    when none runs, only `_active_request_token`; `clear_transcription_queue`
    ("Clear queue") is the sole path that cancels every job.
  - What the user actually saw was the documented history-only delivery: a
    transcription that finishes despite a cancel is kept, and the session log
    additionally contained a `Paste canceled because a Ctrl, Alt, Shift, or
    Windows key remained held` insertion failure. Both leave the transcript in
    history but not in the target window, which reads as "canceled".
- **A queued transcript that is produced but never pasted is now reported:**
  - `_insert_background_transcription` inserted with `show_overlay_error=False`
    and logged a failure to the log file only. A failed *transcription* already
    raised a tray notification, so the silent case was the one where the text
    existed and was simply lost — indistinguishable from success.
  - Failures now emit `background_insertion_failed` (tray notification) and,
    when nothing newer owns the overlay, show the Error state with the
    transcript and an Insert action, taking over `_last_transcript` so Copy and
    Insert act on exactly what is displayed.
  - Writing the test surfaced a second defect: the deferred flush runs inside
    `_on_transcription_ready` before the foreground result writes its own
    overlay state, so the Error flashed and was overwritten. The new
    `_foreground_delivery_pending` guard suppresses only the overlay part in
    that gap; the notification always fires.
- **History audio actions moved to the overlay's Recent Transcriptions dialog:**
  - Per-entry Retranscribe... and Show audio file (buttons plus a right-click
    menu) and a Recordings-folder shortcut, so a wrong-language dictation can be
    fixed without opening Settings.
  - `history_audio.py` now owns audio resolution and file-manager reveal for
    both history views, `app_paths.resolve_recordings_dir` owns the
    configured-else-default rule, and `settings_store.apply_engine_model_selection`
    owns the engine-to-model-field mapping that the Settings General tab, audio
    imports, and retranscription all need.
  - The overlay retranscribe dialog exposes only the language on purpose;
    changing engine or model stays with Settings > History, which prefills the
    Import Audio tab. Both paths write a new history entry.

## 2026-08-12

- **Dependency refresh to current latest, verified against the new code:**
  - ctranslate2 4.7.1 -> 4.8.1 (model-load heap overflow), onnxruntime-genai
    0.14.1 -> 0.15.2 (heap overflow, OOB write, use-after-free, arbitrary DLL
    load via JSON injection), plus PySide6 6.11.1, numpy 2.5.2, groq 1.6.0,
    assemblyai 0.64.33, pywin32 312, pyinstaller 6.22.0, pytest 9.1.1,
    Transformers.js 4.2.0.
  - `npm audit` surfaced a separate high-severity advisory that already applied
    to `main`: `sharp < 0.35` inherits four libvips CVEs, and npm could not
    resolve past `@huggingface/transformers`' `^0.34.5`. A `sharp: ^0.35.0`
    entry in `package.json`'s existing `overrides` block fixes it; the audit
    now reports zero vulnerabilities.
  - Ruff 0.16.x was deliberately not adopted: its default rule set grows from
    59 to 413 rules and produced 439 findings against a codebase that is clean
    on 0.15.8. That is a separate triage task, not a dependency bump.
  - Verified on the real Windows platform (not `offscreen`) with the upgraded
    packages: Ruff clean, 1225 tests green, `npm ci` clean, and the ONNX Node
    runtime loads reporting cpu/dml/webgpu.
- **Two test-harness lessons, both self-inflicted:**
  - Two full pytest suites were left running concurrently; both froze. Qt suites
    build real windows on one desktop and must not overlap. Every test run now
    carries a hard `timeout`.
  - The full suite froze twice at ~94% (`test_win_tray_icon.py`, after four
    tests) while other load ran, and passed twice when run exclusively. The file
    alone, and large subsets around it, are green in seconds. Treat this as an
    unexplained intermittent hang under load, not a diagnosed defect; run with
    `-o faulthandler_timeout=...` to capture thread tracebacks if it recurs.
- **`QT_QPA_PLATFORM=offscreen` is not a valid substitute for the documented
  test command**: it makes
  `test_overlay_ui.py::test_overlay_record_button_indicator_stays_centered_in_both_states`
  and
  `test_settings_dialog_general_ux.py::test_bottom_status_does_not_move_the_save_and_close_buttons`
  fail with 1-4 px deltas. Both pass under the normal Windows platform. A report
  of "pre-existing pixel failures" from an offscreen run is an artifact.

- **Desktop side effects in tests are now structurally impossible:**
  - Reported symptom was leftover Explorer windows, some showing an empty
    folder named `recordings` — which reads like a product bug but is a pytest
    `tmp_path` directory of that name.
  - Two autouse fixtures now block `QProcess.startDetached`,
    `QDesktopServices.openUrl`, the `QMessageBox` statics, and the
    `QFileDialog` getters, raising a named error instead of no-opping.
  - The full suite passes with both active, so no current test was leaking:
    they are preventive. The modal-dialog half is the valuable one — an
    unstubbed `QMessageBox.information` does not fail a run, it hangs it
    indefinitely with no output naming the cause, which is expensive to bisect.
  - This also disproves the theory that a modal dialog caused the earlier
    ~94% hang. That hang remains unexplained.
  - The real recordings directory resolved correctly throughout (200 WAV files
    under the configured path); the app's own "Show audio file" and "Recordings
    folder" actions open a window per click by design and must not close it.

## 2026-08-15

- **Retranscribe dialog gained a quick engine/model picker and a resizable window:**
  - The entry's own engine, model, and language stay preselected; all three are
    now changeable, and the pickers are dependent (engine -> models ->
    languages), restoring the entry's model when the user returns to its
    engine.
  - `settings_dialog_helpers.model_choices_for_engine` / `local_model_label`
    now hold the model table that the Settings General/Import tabs used to own
    privately, so every picker labels a model identically.
  - The window is resizable with the two transcript views in a splitter. The
    no-layout-shift guarantee still holds: the status line keeps a fixed
    height and an ignored width policy, verified by rendering progress, a long
    provider error, and a long result at a fixed window size.
- **Ctrl+C in the history views copied only one entry:**
  - Selecting three rows and pressing Ctrl+C produced a single transcript,
    while "Copy selected" produced all three. Both views now install an
    explicit `QKeySequence.Copy` shortcut bound to the same handler.
- **Benchmark tab visuals:**
  - `_BENCHMARK_RESULT_SURFACE_STYLESHEET` was an unscoped property block.
    Qt inherits those into every child, so each header section and the table
    corner button drew its own rounded, bordered box — the reported "ugly
    corners and edges". All rules are now scoped to widget types.
  - The details view drew a second frame inside the tab pane. Its views are
    now `NoFrame` with their content wrapped in a small margin, and the tab bar
    matches the main dialog. Benchmark History and Results share one surface.
  - Verified by rendering the tab to a PNG before and after, not by assertion
    alone.
  - Default settings-dialog width 780 -> 860 px.
- **Overlay jumped back to its corner while dragged during startup:**
  - `_manual_positioned` was set on mouse *release*, so every startup overlay
    update (preload progress, "Model loaded", idle status) repositioned the
    still-"automatic" overlay to its configured corner mid-drag. The drag now
    claims the position on first movement, and repositioning is skipped
    entirely while a drag is active.

## 2026-08-17

- **Model download progress measured a foreign directory (reported as
  "10078/2500 MB, approx. 100%" while the model was still downloading):**
  - `estimate_cached_model_bytes` searched every *candidate* cache layout for a
    model (`models--<repo>` and the flat `local_dir`, in the configured model
    dir and the default cache) and returned the largest. Local ONNX models only
    ever download into the flat `local_dir`, so the largest candidate was not
    the download.
  - On this machine `scripts/convert_granite_nar_q4.py` had pulled the NAR
    repo's fp32 weights with `cache_dir=` — 9.4 GB (a 6.5 GB `fp32/editor
    .onnx_data` and a 2.5 GB `fp32/encoder.onnx_data`) in
    `models--smcleod--ibm-granite-speech-4.1-2b-nar-onnx`. Measured: 10 077 625 970
    bytes, exactly the "10078 MB" on screen. That directory never grows during a
    download, so the percentage was pinned at 100% and the speed tracker never
    saw a delta and stayed on "measuring speed" forever.
  - Fixed by introducing `download_destination_dir` as the single source of
    truth for where a download lands, resolving local ONNX models through the
    new `webgpu_download_destination` that `download_webgpu_model_snapshot`
    itself now uses for its `local_dir`. A parametrized test asserts the two
    agree for every local ONNX model so they cannot drift.
  - The same defect was latent for faster-whisper: with a configured model dir,
    a larger copy in the *default* cache would have been reported as progress.
    The destination is now a single directory rather than a `max()` over roots.
  - Symlinked snapshot entries are now skipped when summing. `stat()` follows a
    symlink into the blob that was already counted, which would report 100% at
    half a download on any platform where the hub links instead of copies.
  - Measured sizes corrected the estimates: Plus 4100 -> 4065 MB, NAR
    2500 -> 2522 MB (base 2B's 1843 MB was already exact).
  - Not a bug: a model that is already complete on disk jumps straight to
    ~100%. `granite-speech-4.1-2b` and Plus were fully cached from earlier
    sessions, which is why re-queueing them finished instantly.

- **Granite Speech 4.1 2B Plus had never run end-to-end and was unusable:**
  - `_GRANITE_4_1_AR_INT8_REQUIRED_FILES` was copied from the NAR list and
    demanded `preprocessor_config.json`. The Plus repo does not ship that file;
    it ships `processor_config.json` with the same mel parameters nested under
    `audio_processor`. With 4.0 GB correctly downloaded,
    `resolve_cached_webgpu_model_path` returned `None`, Plus never appeared in
    `find_cached_webgpu_models()`, and selecting it raised "is not cached
    locally. Disable Offline mode or download it first."
  - The failure was self-perpetuating online: the re-download's allow-pattern
    for a file that does not exist matches nothing, so the same check fails
    again and raises "no complete int8 ONNX snapshot was found".
  - `webgpu_asr_runner.mjs` carried the same assumption independently in
    `loadGranite41ArRuntime`, so fixing only the Python gate would have moved
    the failure to an ENOENT in the runner. `Granite41AudioFrontend` already
    accepted both config shapes, so only the filename lookup needed to tolerate
    both (`readGranite41AudioConfig`).
  - After the fix, verified through the real `LocalOnnxWebGpuTranscriber`:
    loads in 15.6 s and returns the reference transcript verbatim.
  - Same lesson as the NAR CTC bug: "the model is shipped" is not "the path is
    verified". Neither variant had a benchmark entry.
- **Plus paid a doomed WebGPU attempt on every dictation:**
  - Plus shares NAR's conformer encoder and fails on the identical
    `/encoder/layers.0/attn/Einsum` node, but only at *inference* — the WebGPU
    session creates successfully, so the load-time probe cannot reject it.
    `_should_restart_after_cpu_fallback` then tore the runtime down after each
    CPU fallback, so the next dictation repeated the whole cycle: measured 75 s
    (EN) and 110 s (DE) wall clock versus 13.6 s for the same clip under an
    explicit CPU policy. Plus joins NAR in `LOCAL_ONNX_AUTO_CPU_MODELS`.
- **Measured model comparison (Ryzen 5 7600X + Arc A750, 16.9 s EN / 13.4 s DE):**

  | model | device | RTF | English | German |
  | --- | --- | --- | --- | --- |
  | `granite-speech-4.1-2b` (q4) | WebGPU | 0.28 | verbatim | best |
  | `granite-speech-4.1-2b-nar` (INT8) | CPU | 0.49 | verbatim | degraded |
  | `granite-speech-4.1-2b-plus` (INT8) | CPU | 0.81 | verbatim | mid |

  The base 2B q4 is both the fastest and the most accurate because it is the
  only one of the three that reaches the GPU; precision is not the lever here.
  NAR corrupts ordinary German words (`geschrenen`, `auchmllaute`, `korkt`)
  that the other two get right.
- **`samples/benchmark_sample.wav` cannot validate transcripts:** it is 2.09 s
  of synthetic sine tones from `scripts/generate_sample_audio.py`, and the
  working base 2B "transcribes" it as *"the city is the capital of the province
  of the same name."* Benchmark timings from it are meaningful; its stored
  transcripts are pure hallucination and must never be read as accuracy.
- **Streaming review: four text-loss and lifecycle defects.**
  - *An empty rolling window wiped the whole transcript.*
    `append_only_stream_partial_candidate("...", "")` returns `""`. At
    `_stream_worker`'s fast finalization the trailing window is merged into the
    accumulated text, so a last window that decoded to nothing (trailing
    silence) produced an **empty final transcript for the entire dictation**.
    The same call during partials wiped `merged_text` mid-session.
  - *A mistranscribed window-boundary word discarded everything before it.*
    `_suffix_prefix_overlap_len` anchors every candidate alignment at the
    window's first word, and the 8 s window boundary routinely cuts a word in
    half. When that fragment came back wrong no alignment matched, the fallback
    returned the window alone, and the accumulated text was gone.
  - Both are fixed by `merge_rolling_window_transcript`, used only by the
    rolling-window paths: an unalignable window is appended, never substituted,
    and an empty one keeps the accumulated text. The revision semantics of
    `append_only_stream_partial_candidate` are unchanged for providers that
    genuinely send full-text revisions, and its existing tests still pin them.
  - *Losing the committed prefix froze insertion permanently.* Once the
    candidate no longer contained `committed_text`,
    `compute_stream_locked_prefix` returned the committed text unchanged for
    every subsequent partial, so nothing more was ever inserted while the
    overlay still finished with `Done`. `StreamingTextState` now joins a
    contradicting candidate onto the committed text instead.
  - *A dying stream runtime discarded the partial transcript*, though
    `_abort_streaming_session` deliberately keeps it. Both paths now read
    `_current_streaming_partial_text()`.
  - *Shutdown froze the UI mid-dictation*: `shutdown()` runs on the Qt thread
    and called `stop_stream()`, which joins the worker with no timeout through
    a final transcription whose result it then throws away. It aborts now.
  - *A result was delivered under the wrong mode.* `_on_transcription_ready`
    read `_active_session_mode`, which a stream runtime failure resets to
    `batch` without retiring the in-flight finalize job; the batch delivery
    then pasted the whole transcript again on top of the streamed text. It
    reads `job.mode`.
  - Reviewed and found clean (recorded so it is not re-reviewed): the
    PortAudio-callback non-blocking invariant for all four providers,
    generation scoping, Nemotron abort/close lifecycle, the Deepgram stop
    sequence, AssemblyAI `turn_order` keying, and the append-only guarantee.
  - Known and deliberately not changed: live partials run the full blocking
    clipboard paste on the Qt thread, so a modifier key held longer than 1.5 s
    aborts the session instead of skipping one delta; remote stream start
    blocks the Qt thread on the network handshake; and a stream finalize queues
    behind unrelated batch jobs on the single-worker executor.
- **Adversarial review of the same day's fixes — three of them were wrong.**
  Two Opus reviewers were pointed at the three commits with instructions to
  break them. Both produced executable proof; the streaming commit did not
  survive and was corrected before pushing.
  - *The rolling-window append fallback was a worse bug than the one it fixed.*
    A silent microphone makes faster-whisper emit a fresh hallucination on
    every 0.35 s partial, and none of them can ever align, so appending grew
    the transcript without bound — measured 79 words after 10 s of silence,
    896 after 120 s, for 8 words of real speech — and finalization *pasted*
    330 of them into the user's document. The old code lost the transcript;
    this typed junk into whatever had focus. Reverted to replacing.
  - What survives, and is strictly better than the baseline: an empty window no
    longer wipes anything, and the overlap search now re-anchors up to three
    words into the window. The reviewer's own 20-word rolling simulation with a
    garbled boundary went from 31 words (15 duplicated) to the exact 20-word
    ground truth.
  - *The committed-prefix join re-pasted whole dictations.* It does unfreeze
    insertion, but an AssemblyAI turn revision inside the already-pasted region
    made it re-emit everything: 86 pasted words for a 48-word truth, scaling
    with session length. Reverted; the freeze is documented as the lesser evil
    and the real fix (gate windows on audio energy) is recorded in AGENTS.md.
  - *Reading `job.mode` instead of `_active_session_mode` was inert.* Every
    writer of `_active_session_mode = "batch"` also resets the streaming text
    state, so `committed_text` is already empty and the delivery is identical;
    it only relabelled history and suppressed the completion beep. Reverted,
    along with the AGENTS.md bullet that asserted it prevented a double paste.
  - *Partial preservation could write two history entries.* AssemblyAI and
    Deepgram record a socket error and still return text from `stop_stream()`,
    so the failure path and the finalize both stored the dictation. Guarded
    with `_has_pending_streaming_job()`.
  - *Measuring only the download destination regressed the user's own model.*
    Cohere is cached in the legacy `models--<repo>` layout, which is still
    resolved and loaded, so its preload bar dropped from 100% to 0%.
    `estimate_cached_model_bytes` now falls back to the largest existing layout
    while the destination does not exist yet.
  - Estimates corrected again from Hub-authoritative listings rather than the
    local directory: NAR 2522 -> 2490 MB (the measured dir held a 31.5 MB
    orphan `.incomplete`), and the pre-existing `large-v3-turbo` 809 -> 1622 MB,
    whose bar had been reading 100% at half a download.
  - The new allow-pattern guard was vacuous — `int8/encoder.onnx_data` was
    being checked against `int8/*.onnx`, so it passed even with
    `int8/*.onnx_data` removed. It now uses `fnmatch` over every layout, which
    is the check that would have caught the original Plus bug.
  - The Settings note still said "NAR uses CPU by default" for Plus; the
    transcriber's own status text was already model-agnostic.
  - Lesson: a fix in a merge/reconciliation path needs a simulation over long
    realistic input, not just unit assertions on two-line examples. Every
    defect above passed the full test suite.
- **Re-verification round: the corrections themselves had two regressions.**
  A third adversarial pass over `b9ba9ce..HEAD` confirmed the streaming-merge
  work (re-anchoring, bounded silence, the reverted join) holds under a
  600-trial fuzz and a 1005-partial silence run, but found two defects the
  correction commit introduced:
  - *The partial-preservation guard also gated the teardown.* Wrapping
    `_teardown_active_stream_runtime` in `not _has_pending_streaming_job()`
    abandoned a live capture, its transcriber and its runtime lease, so the
    microphone kept recording after the overlay said Error and the leaked lease
    defeated `_pending_transcriber_cache_reset`. Only the history write is
    conditional now.
  - *The 0%-preload fallback reintroduced the original download bug.* Falling
    back to `max(_model_cache_dirs)` when the destination is absent let the
    9.4 GB fp32 conversion copy pose as NAR's progress again — the same
    `10078/2490 MB, 100%` string, and it violated the rule this same series had
    just written into `AGENTS.md`. The fallback now requires a *complete,
    loadable* snapshot (`_complete_cached_model_root`), which the fp32 copy can
    never satisfy because it carries no `int8/*` files, and which an in-flight
    download cannot satisfy either, so it correctly starts at 0%.
  - Lesson, again: the fix for a bug in a fallback path needs a fixture with
    *both* conditions true. The two tests written for the fallback each covered
    one branch (destination absent, or foreign copy present) and neither
    covered the combination that actually reproduces the bug.
  - Fuzz evidence worth keeping: over 600 randomized rolling-window trials with
    1-4-word garbles, the re-anchoring merge lost 4158 truth words against the
    baseline's 23581, and where it did worse than baseline (80/600 trials) the
    maximum delta was 3 words, all of them retained garble tokens rather than
    truncated speech. Through the real stream worker, 1005 hallucinated silence
    partials produced at most 8 words.
  - Known and accepted, unchanged from baseline: a finalize that raises or
    returns empty after a runtime failure still loses the partial, and one
    unalignable window still freezes live insertion for the session. Both wait
    on gating streaming windows by audio energy.
- **The ONNX execution device is now selectable for daily dictation:** the
  Benchmark tab has offered Auto/GPU/CPU/DirectML/WebGPU targets all along, but
  `factory.py` never passed a device to `LocalOnnxWebGpuTranscriber`, so real
  dictation always ran on `auto` and a benchmark result could not be acted on.
  `local_onnx_device` (schema 23) closes that, using the same wording as the
  benchmark choices. It belongs in both the transcriber cache key and the
  preload key — unlike the language, the device is baked into the loaded
  runtime — and the row stays permanently visible so switching to
  faster-whisper or a remote engine cannot shift the fields under it.
- **Added NVIDIA Parakeet TDT 0.6B v3 and Canary 1B v2 through `onnx-asr`.**
  - Measured through the app's own transcriber on a Ryzen 5 7600X, CPU only:
    Parakeet **670.6 MB, RTF 0.046 EN / 0.043 DE**; Canary **1029.3 MB, RTF
    0.134 / 0.135**. Parakeet is about ten times faster than Granite NAR (0.49)
    and six times faster than Granite 2B *on the GPU* (0.28) — a 17 s dictation
    comes back in 0.78 s with no GPU involved at all.
  - The graft is unusually light: `onnx-asr[cpu,hub]` resolves the same
    `onnxruntime` 1.28.0 that `onnxruntime-genai` already required, so no new
    native runtime enters the app. Download, cache detection, progress and
    deletion all reuse the existing `_OnnxModelLayout` machinery; only
    inference is new code.
  - **DirectML was measured and rejected.** It is genuinely the fastest
    (Parakeet EN RTF 0.022 vs 0.043) but `onnxruntime-directml` installs beside
    `onnxruntime` without pip noticing — `pip check` reports no broken
    requirements — while overwriting 620 of 625 shared files. `import
    onnxruntime` then reports 1.24.4 and `onnxruntime-genai` dies with "The
    requested API version [26] is not available" plus a DLL init failure. So it
    would silently trade a 1.9x speedup for the entire Nemotron engine.
    `onnxruntime-webgpu==1.27.0` coexists but is slower than CPU here.
  - **Canary's default silently translates.** With no explicit language,
    onnx-asr's hardcoded `<|en|>` made it return *"The automatic speaker
    recognition wandels spoken language reliably into written text"* for German
    audio. It is now in `LOCAL_EXPLICIT_LANGUAGE_MODELS` and can never select
    Auto. Parakeet is the opposite — it ignores the language argument entirely,
    so it exposes only Auto and sends none.
  - The German gate clip turned out to be a weak discriminator: Parakeet,
    Canary and faster-whisper large-v3 all mangle its three isolated umlaut
    showcase words identically, so that failure is the TTS clip, not the
    models. Everything diagnostic — noun capitalisation, `ä`/`ö`/`ü` inside
    real words, `ß` in "Straße", sentence punctuation — is correct in both.
    A real dictated German sample is still needed to rank German accuracy.
  - Canary is kept despite being 3x slower because published FLEURS-de favours
    it (3.43 vs Parakeet's 4.16) and our clip cannot separate them; it is the
    accuracy option, Parakeet the speed option.
- **Adversarial review of the onnx-asr work found four defects, all mine.**
  - *Benchmarking either new model was impossible.* `LOCAL_MODEL_RUNTIME` gained
    the value `"onnx-asr"`, but `run_benchmark_cases` dispatches on that dict
    and only knew three runtimes, so both models hit the `else: raise` with
    "Benchmark runtime ... is unknown. Restart the app..." — advice that could
    never help — and the branch added inside `_run_onnx_case` was unreachable
    dead code. A test now asserts every `LOCAL_MODEL_RUNTIME` value is
    dispatchable.
  - *The benchmark's `"de"` language default would have made Canary translate*
    an English sample and store the German result as the benchmark transcript.
    It now takes the first mode the model itself declares.
  - *The new ONNX Device picker was enabled for Nemotron but the factory never
    passed it a device* — the exact bug the picker was written to fix, one
    branch above the one it fixed, and a violation of the rule the same commit
    added to AGENTS.md. `config.nemotron_provider_order` is now the shared
    mapping for the factory and the benchmark.
  - *Canary's `auto -> de` substitution was silent.* Closing the "no language"
    hole only moved it: a wrong language is equally destructive because the
    model translates into it. Retranscribing an English history entry (recorded
    with the default `auto`) as Canary produced German prose saved as a new
    entry, and the Language hint underneath still read "Granite supports Auto".
    The substitution is now logged, the retranscribe dialog warns and names the
    dropped language, and the constraint note is per-model.
  - Lesson: adding a value to a lookup table is only half the change — every
    dispatcher keyed on that table has to learn it, and a UI that offers a
    setting has to be traced to the code that consumes it.

## 2026-08-18

- **Two independent model downloaders were merged into one slot.** The
  controller's preload path and the Local tab's queue each spawned their own
  worker process against the same Hugging Face cache. Reported symptoms, all
  from one sitting: an uncached model selected and saved downloaded invisibly
  (the Local tab showed no download); starting the same model from the Local
  tab then sat at 0% because progress is directory growth and the other process
  owned the directory; and switching model killed the preload download *and*
  ran `cleanup_incomplete_model_download`, so a multi-gigabyte download
  restarted from ~100 MB.
  `model_download_coordinator` is now the single slot. A second caller waits
  rather than racing, a caller waiting for the *same* model gets
  `ACQUIRE_JOINED` and skips its own download entirely, and explicit user
  requests register interest while still queued so the implicit path stops
  deleting the partial files they are about to resume from. The remote work had
  already made a controller download visible in the model list; the progress
  bar now follows it too, so the two paths are indistinguishable to the user.
- **Error messages could not be selected or copied.** Qt gives a `QMessageBox`
  only `LinksAccessibleByMouse`, so every error dialog in the app could be
  captured only by retyping it or taking a screenshot. Most of the app's boxes
  come from the `QMessageBox.critical`/`warning`/`information`/`question`
  convenience statics, which construct and show the box in one call, so no
  call-site change can configure them; one application-wide event filter that
  marks each box selectable as it is shown covers all of them, plus any box Qt
  raises itself, with no call-site or test churn. Inline status labels in the
  Settings, benchmark, history, remote, retranscribe and transcript-edit
  surfaces are marked selectable too. Verified that a 216-character provider
  error is wrapped and kept whole rather than elided. The overlay's detail
  label already had the flags, and a probe confirmed a drag over it selects
  text rather than moving the window.
- **A registration audit now guards the model tables.** Two defects in this
  session came from a model being added to some tables but not all: Granite
  Plus demanded a file its repo does not ship (invisible and unusable), and the
  onnx-asr models introduced a `LOCAL_MODEL_RUNTIME` value the benchmark
  dispatcher did not know (benchmarking them always failed).
  `tests/test_model_registration.py` parametrizes over `VALID_MODEL_SIZES` and
  fails if a model is missing from the repo map, the runtime map, the label
  table, the size estimates or the language modes, and over
  `LOCAL_ONNX_MODEL_SIZES` for a layout and a download destination.
- The download coordinator is exercised by a 60-thread concurrency test with
  mixed explicit/implicit callers, random failures and random cancels; it
  asserts peak concurrency of exactly 1, no deadlock, no dangling slot and no
  leaked explicit interest. An ad-hoc 200-thread run behaved identically.
- **Adversarial review of the download/selectable work found 14 issues; the
  premise of the coordinator did not hold.**
  - *Three transcriber load paths downloaded outside the slot.*
    `_ensure_snapshot` (Cohere/Granite), Nemotron's loader and the onnx-asr
    loader all fetched in-process on a cache miss. Worse, with
    `keep_onnx_model_loaded` off the Cohere/Granite family never preloads, so
    that uncoordinated path was the *only* one — the original triple failure
    was fully reachable for exactly the family it was reported on. All three now
    go through `run_coordinated_download`.
  - *The progress bar never followed a controller download.* The branch added
    for it was unreachable: `_poll_preload_download_state` refreshed only the
    list, and the progress timer starts solely on this tab's own queue paths.
  - *A queue entry published itself as active before `acquire()` returned*, so
    while queued behind another download the bar measured a directory nothing
    was writing to and read 0% forever — the exact symptom being fixed. It now
    publishes after acquiring and says "Waiting for the current download".
  - *The Canary benchmark-language fix was a no-op*: "the model's first declared
    mode" is `de` for Canary. Defaulting cannot be right when the sample's
    language is unknown, so the benchmark now refuses without an explicit one.
  - *Two of the three new guard suites were vacuous.* The runtime-dispatch guard
    compared the table against a hardcoded copy of itself, and nothing tested
    the coordinator wiring or the filter installation — reverting either fix
    left the suites green. Both are now driven through the real call sites, and
    each was checked by reverting the fix in a scratch tree and confirming a
    failure.
  - Also fixed: explicit interest is registered at enqueue so a queued model
    keeps its partials; `ACQUIRE_JOINED` is idempotent so several waiters on the
    same finished model all skip their download; selectable flags are ORed so
    update-dialog links keep working; the whole filter body is guarded since an
    exception in an event filter escapes `show()`; the unused listener plumbing
    is gone; a mismatched release now logs instead of silently wedging the slot;
    and the Canary warning no longer clips when the dialog is narrowed.
- **Re-verification round: three release blockers, and four regressions from the
  previous round's own fixes.**
  - *The default engine still bypassed the slot.* `WhisperModel(...)` downloads
    inside its own constructor through `huggingface_hub`, so no grep of this
    repo reveals it and the headline "every download goes through the
    coordinator" was false for the engine most users run. `_ensure_model` now
    fetches through the slot first.
  - *A hotkey press could freeze the UI forever.* Nemotron's `start_stream`
    loaded the model on the Qt thread; once that load could queue behind an
    unrelated download, the freeze became unbounded with no progress and no
    cancel. The worker loads instead, and buffered audio means nothing is lost.
  - *The app could outlive its own exit downloading gigabytes.* No transcriber
    path passed a cancel check, and the shutdown sequence releases the slot
    (dialog first) before the controller stops — so a waiter woke up and started
    a fresh download on a non-daemon executor thread that the interpreter joins
    at exit. `request_download_shutdown` is now connected to `aboutToQuit`
    ahead of both teardowns, and `acquire()` refuses to wait once it is set.
  - Regressions from the previous fixes, all mine: publishing the active entry
    only after `acquire()` made a queued model invisible so Download queued it
    twice; the progress bar could never hide again because the hide branch was
    gated on a value tab re-entry resets; the loop-top break leaked enqueue-time
    interest, which then blocked partial cleanup for the process lifetime *and*
    stranded the entry so the model could never be downloaded again that
    session; and the new "Waiting for the current download" line was overwritten
    two lines later by the progress refresh.
  - Four of seven fixes had shipped with no coverage at all. Each new guard was
    checked by reverting its fix in a scratch tree and confirming a failure.
  - Recorded as a known limitation: the lock is process-wide, so the
    out-of-process benchmark worker and `scripts/download_model.py` can still
    write the same cache directory. Both are deliberate developer actions.
- **Round 3: four more defects, three of them introduced by round 2's fixes.**
  - *Cancelling a queued download stranded its entry for the session.* The
    cancel branch returned before the `finally` that clears
    `_local_model_download_claimed`, so the tab showed the model as downloading
    forever and silently refused to queue it again. One `try/finally` now covers
    every exit, and releases the enqueue-time interest when the slot was never
    held. The precondition became far more common in the same commit, because
    the new faster-whisper pre-fetch takes the slot too.
  - *The progress bar measured a model that was only queued.* The snapshot folds
    a claimed entry in for the list and the duplicate check, and the bar was
    reading that same field — inventing a percentage from another model's
    directory growth, and the 500 ms timer overwrote the accurate "Waiting for
    the current download" line with it. The bar now reads
    `_local_model_download_active` only.
  - *faster-whisper still bypassed the slot with a custom Model Dir.* The gate
    used `find_cached_models`, which also accepts the default cache and a flat
    layout, so it answered "cached" for a model `WhisperModel` would still
    download into the custom dir. It now gates on
    `download_destination_dir` + `_has_valid_model_snapshot`, i.e. exactly the
    directory the constructor resolves.
  - *An exception in the download queue wedged the tab permanently.* The worker
    body had no `try/finally`: interest stayed registered (blocking partial
    cleanup for the process lifetime), `_worker_running` stayed True, and the
    Refresh/Delete/Model-Dir controls stayed disabled with no way back.
  - Also: `_hide_local_model_download_progress` asked the widget whether it was
    visible, and Qt answers False for a child of a hidden dialog — this dialog
    persists hidden for the app lifetime, so a download ending while Settings
    was closed never cleared its bar. It tracks the state itself now.
  - Known and accepted: the coordinator is process-wide, so the out-of-process
    benchmark worker and `scripts/download_model.py` can still write the same
    cache directory; `ModelDownloadCanceled` surfaces as a transcription error
    rather than a clean cancel; and the transcriber's own pre-fetch is not
    reported by `preload_downloading_model()`.
- **Round 4 verification (run by hand after the reviewer hit its usage limit).**
  The reviewer died before delivering findings, so its checklist was executed
  directly. All five round-3 fixes verified by reproduction, plus:
  - The narrowed faster-whisper gate agrees with `find_cached_models` on all
    seven models against the real cache, so it does not re-download anything
    already present; with a custom Model Dir it correctly refuses to skip the
    slot, and it correctly recognises an imported `models--<repo>` snapshot
    inside that dir.
  - Explicit-interest refcounting survives over-drops: the counter clamps at
    zero and a later register still takes effect, so the new `finally` cannot
    push another caller's claim negative.
  - Interest returns to zero on every exit of
    `_download_local_model_in_subprocess` — success, worker failure, worker
    exception, cancel-while-queued and joined — with the claim and the slot
    cleared each time.
  - **One of the new guards was vacuous**: the faster-whisper wiring test
    patched `_has_valid_model_snapshot`, so reverting the gate to
    `find_cached_models` left it green. Replaced with two tests that assert the
    behaviour instead — an empty custom Model Dir still takes the slot, and a
    model already in that dir is not refetched — and both were confirmed to
    fail against the reverted gate.
  - The two remaining uncovered fixes (the bar never measuring a merely claimed
    model; the bar hiding while Settings is closed) now have tests, each
    verified against a revert.
- **Round 5 (release-readiness): verdict releasable, with four small fixes.**
  - *The "Downloaded X" / "Download failed: reason" line was wiped a second
    after it appeared.* `_on_local_model_download_finished` hid the bar without
    clearing the shown-flag, so the Local tab's 1 Hz watchdog then ran the full
    hide body — including blanking the label — exactly when the user was
    watching. One line.
  - *The General tab told the user the four Granite models have no automatic
    language detection while the combo beside it offered Auto.* The round-2
    rewrite moved that falsehood rather than removing it: Cohere and Canary have
    their own branches, so only Granite reaches the fall-through. Worse, the
    guard added with it asserted only that the word "Granite" was absent, which
    cannot detect a wrong claim. Replaced with a test that compares every
    model's hint against its actual picker contents, verified against a revert.
  - The Remote tab's connection-test and per-provider "Last test" labels — where
    a 401, SSL or proxy error actually lands — had been missed by the selectable
    sweep, so the AGENTS.md claim about inline error labels was false for them.
  - `acquire()` returned explicit interest it had never registered: the
    increment was guarded by `interest_already_registered`, both decrements only
    by `explicit`, so the production call path went net −2 on the joined and
    cancelled branches. Latent (the counter clamps at zero) but it disarmed the
    partial-file protection early.
  - Two pre-existing bugs the round surfaced were fixed too: Parakeet and Canary
    were never preloaded although the overlay announced "Model loaded" (the
    isinstance tuple omitted their class; it now asks for `preload_model`), and
    deleting a model kept its name in the session's "completed" set, so the row
    still read "Downloaded" and re-downloading was refused until restart.
  - The intermittently red `test_delete_selected_cached_model_updates_feedback`
    was waiting 3 s for an out-of-process inventory scan; raised to 15 s.
  - Reviewer measurements worth keeping: the selectable-text filter costs
    ~0.74 us per dispatched event (~0.07 % CPU at 1000 events/s), and
    `estimate_cached_model_bytes` takes 1.0-8.3 ms across all 14 cached models,
    so the 1 Hz progress timer is not a concern.

## 2026-08-19

- **The fallback hotkey could never fire, and it overwrote the user's choice.**
  Reported as "Claude Code stole my hotkey and afterwards recording did not
  start". Both halves reproduced:
  - `RegisterHotKey(Ctrl+Alt+Space)` really does fail on this machine with
    error 1409 (`ERROR_HOTKEY_ALREADY_REGISTERED`) while a terminal holds it.
  - The app then fell back to `FALLBACK_HOTKEY = "Ctrl+Win+LShift"`, whose key
    is itself a modifier. `RegisterHotKey` matches the modifier state exactly,
    and pressing LShift necessarily raises the SHIFT bit, so the registered
    `Ctrl+Win`+`VK_LSHIFT` can never match the actual `Ctrl+Win+Shift`
    keystroke. Proven by registering `Ctrl+Win`+`VK_LSHIFT` and
    `Ctrl+Win+Shift`+`VK_LSHIFT` simultaneously — Windows accepts them as two
    different hotkeys. Registration *succeeded*, so nothing was reported and
    the app claimed a working hotkey that silently did nothing. `config.py`'s
    own comment three lines above already said the key must not be a modifier.
  - Worse, the fallback was **persisted** into settings, so the temporary
    condition became permanent: after the other program closed, the app had
    already forgotten the user's preference.
  - Fixed at the root: `parse_hotkey` rejects a modifier as the key (which also
    prevents configuring one in Settings), `FALLBACK_HOTKEYS` is a chain of
    real-key combinations tried in order, the preference is never overwritten,
    and a reclaim timer takes the preferred hotkey back once it is free.
    Verified against the live Win32 API which combinations are actually
    available here: `Ctrl+Alt+Space` and `Ctrl+Win+Space` are taken (the latter
    by Windows itself for language switching), `Ctrl+Alt+F9`,
    `Ctrl+Shift+Space` and `Ctrl+Alt+D` are free.
- **The overlay clipped its own hotkey notice.** Compact states pinned the
  detail area to 42 px while that notice needs 48 px, so the text the user most
  needed to read was the text they could not see. Compact now grows to fit up
  to a bounded cap and adds only the overflow to the window, leaving the
  ordinary short idle overlay at exactly its previous size.

## 2026-08-26

- **Every settings save threw the loaded model away.** Reported as "I only
  changed something small and it reloaded the model". Confirmed at the source
  rather than guessed: `reload_settings` reset the transcriber cache
  unconditionally and `on_settings_changed` then preloaded unconditionally, so
  overlay opacity, a hotkey, a beep tone or the history limit each closed a
  multi-gigabyte local runtime and loaded the identical one again. The
  language-mode exemption fixed earlier was one instance of this; the general
  rule is now `_transcriber_identity(settings)` — the single description of
  what `create_transcriber` bakes in — with the reset and the preload both
  conditional on it.
  Two things the unconditional reset had been hiding:
  - **Three constructor arguments were missing from the cache key**
    (`custom_vocabulary`, `silence_gate_enabled`, `silence_gate_threshold`).
    Harmless only because the cache was discarded on every save anyway;
    making the reset conditional without adding them would have kept a runtime
    with the previous biasing prompt or silence gate. This is the general
    hazard when removing a blunt invalidation: it was masking the precise one.
  - **A replaced API key is invisible in `AppSettings`.** `has_*_key` flips
    only on add/remove, so overwriting a key with a different value leaves the
    settings snapshot byte-identical. Needed its own signal
    (`provider_keys_changed`) rather than a settings comparison.
  A test-authoring trap worth remembering: the first parametrized case for
  `keep_onnx_model_loaded` passed `True`, which is already the default, so it
  changed nothing and the test would have passed against a broken
  implementation. Both parametrized tests now assert up front that the
  parameter really is a change.
- **The remote stream finalize lane could paste a later dictation first.** Its
  own worker exists so that stopping a remote dictation does not queue behind
  unrelated local model loading. The cost, found while answering a question
  about it rather than from a report: everything else runs on one shared FIFO
  worker, so delivery order equals recording order, and a foreground result
  additionally flushes older deferred results before pasting its own — but
  neither protects against a result that does not exist yet. A fast remote
  finalize can finish while an older local transcription is still running.
  Reaching it needs an engine switch between two dictations while the first is
  still transcribing, so it is narrow, but it is not theoretical. The fix keeps
  the fast lane only while no older job is pending; otherwise the finalize
  joins the shared queue, exactly as before the lane existed.
- **A repository sweep, after being asked for "a serious list, or fix it".**
  What it found, in descending order of how badly the docs or the gate had
  drifted from the code:
  - **Ruff had no configuration at all**, so the CI gate checked pyflakes plus a
    few pycodestyle errors and nothing else. With an explicit rule set it found,
    among 247 findings, three that were real: the Import Audio elapsed counter
    measured wall-clock time (a DST or NTP correction would jump or reverse it),
    three `zip()` calls silently truncated on a length mismatch they in fact
    cannot have, and a test bound loop variables through closures. This is the
    concrete case for writing the rule set out instead of inheriting defaults.
  - **`distil-large-v3.5` was listed at 756 MB; its `model.bin` is 1513 MB.**
    Because the download percentage is derived from that table, the bar read
    "approx. 100%" halfway through and then kept counting. Every entry is now
    measured against the repository with the download allow-patterns applied;
    `medium`, `tiny`, `base` and `large-v3` were off by 4-9% as well, and the
    same wrong numbers appeared in four documents.
  - **The docs said Parakeet was "not implemented"** in `models.md` 200 lines
    below a table listing it as the fastest local model, in the README's
    document index, and as a "Status" line in its own evaluation note. All three
    were written before the onnx-asr engine existed and were only ever true of
    the *NeMo* path.
  - **The benchmark could not measure a setting the General tab offers.**
    Nemotron takes an ONNX device policy in daily use, but the benchmark
    expanded device targets only for the `onnx-webgpu` runtime, so "All explicit
    targets" ran it once on `auto`.
  - **Canary + language "Auto" failed only when its turn came.** The runner
    refuses that combination for a good reason (it would translate instead of
    transcribe), but with several models selected the whole run finished before
    the user could see why one of them had failed. Reported from a real
    benchmark run on another machine, and reproduced from the code alone.
- **A credential change must not unload a local model.** The provider-key signal
  added earlier that day invalidated the cached runtime unconditionally, so
  saving an OpenAI key threw away a multi-gigabyte faster-whisper model that
  reads no API key at all. Caught by the user asking why that would ever be
  necessary. The signal now carries the affected provider names and the
  invalidation is scoped to the engine that is actually loaded. Worth
  remembering as a pattern: a blunt invalidation looks harmless while the code
  around it is equally blunt, and only becomes visible once the surrounding
  logic gets precise.
- **Download parallelism was measured rather than argued.** The undocumented
  `max_workers=2` for ONNX snapshots looked like a throttle worth raising. It is
  not: every one of these models is one dominant weight file (Parakeet 652 of
  671 MB), so file-level parallelism has nothing to distribute. Four runs on a
  ~70 Mbit/s line, ABBA-ordered against CDN warming: 2 workers 76.7/77.6 s, 8
  workers 76.6/76.4 s. The value stays at 2, now with the measurement written
  next to it so it is not re-litigated.
- **Cancel did nothing for three of the four local engines.** Reported from a
  real session: an accidentally selected Canary run kept a CPU core at ~50 %
  after Cancel, its model stayed in memory, and one transcription sat in the
  queue forever. Confirmed at the source -- only faster-whisper ever polled
  `set_cancel_check` during compute; onnx-asr, Nemotron and the Cohere/Granite
  Node runtime installed the hook and never read it. The consequences went
  further than the one job: the stuck run holds the single `max_workers=1`
  worker *and* the shared runtime lease, so a preload started afterwards blocks
  on that lease, the overlay reports "still loading" for a model that is
  loaded, and each later dictation quietly builds its own isolated Node
  runtime. Two of the three follow-up symptoms the user reported in the same
  message were this one bug seen from a different angle.
  - onnx-asr was the interesting case, because `recognize()` is a single
    blocking call. ONNX Runtime does support aborting a run in flight --
    `RunOptions.terminate` set from another thread -- but onnx-asr never lets a
    caller supply RunOptions. Measured before building anything: with the
    model's sessions wrapped, a terminate set 1.0 s into a 4.6 s encoder pass
    aborted it in 1.01 s, raising a generic `Fail`, and the *same session was
    fully usable afterwards*. That last measurement is what made the design
    viable -- otherwise a cancel would cost a full model reload.
  - The end-to-end numbers on the real models: Canary 0.67 s to cancel against
    a 4.46 s run, Parakeet 0.66 s against 3.21 s, and both transcribed
    correctly immediately afterwards.
  - Three traps, each found by a test rather than by reading: `TranscriptionCanceled`
    is not a `TranscriptionError`, so the two engines whose `transcribe_batch`
    wrapped `except Exception` relabelled the cancel as a failure; the pre-run
    check has to sit before the *model load*, not only before the run, or a job
    cancelled while queued still pulls gigabytes into memory; and the first
    version of the pre-run test was vacuous, because a second check further
    down kept it passing after the first was removed.
- **"Downloading ... approx. 100%" for a model that was already on disk.** The
  same message also invited recording during what was actually a model load.
  A preload does two things -- fetch, then load -- and only the first has
  measurable progress, because the progress bar is directory growth. Both
  halves reported themselves as a download, so a Cohere/Granite load (a Node
  process plus an ONNX graph, tens of seconds) printed a frozen 100 % download.
  The phase is now tracked as `(generation, phase)`, generation-scoped so a
  retired preload worker cannot describe what the current one is doing.
- **The Import Audio / last-recording interference the user asked about does
  not exist -- and now has a test that says so.** The question was whether
  transcribing the managed last recording can be corrupted by dictating over
  it. It cannot: `save_recording` and `snapshot_managed_recording` take the
  same `lock_for_path` lock and the write is atomic, so the snapshot either
  predates the new recording entirely or sees all of it, and the import then
  works on immutable in-memory bytes. Two tests already covered the
  compare-and-set identity half; the interleaving half was only an argument, so
  it is now a test that pauses inside the write and proves the reader blocks.
- **Granite 4.1 Plus and NAR retired; the raw ONNX graph runtime went with
  them.** Asked to remove NAR after a benchmark, and asked what Plus was still
  for. The measurements answered both at once (user's own run, 2026-08-25,
  German dictation, all device targets): base 4.1 2B **RTF 0.100 on WebGPU**
  with correct German; NAR **0.434, CPU only, German with words merged and
  dropped**; Plus **4.138, CPU only, looping one clause to the 1024-token cap**
  -- slower than real time, 42x the base model. For scale, Parakeet does the
  same job at 0.042 on plain CPU. (Figures re-read from
  `benchmark_history.json` on 2026-08-26, best of the two runs; the first write-up
  quoted 0.100/0.460/4.161/0.043 and called NAR's output "word salad", which
  overstated it -- the transcript is degraded, not unrelated to the audio.
  Plus's 4.138 is a consequence of the loop, not an independent speed number:
  its earlier 0.81 on a 16.9 s English clip is what a normally terminating run
  costs. **Corrected 2026-08-28:** that 0.81 is from the manual session
  recorded earlier in this log, on a different recording, so it is not
  comparable with the 24.3 s figures beside it -- and the numbers in this
  paragraph are best-of-two, while the report and `AGENTS.md` now publish
  means, 0.099/0.447/4.149/0.043. The entry is left as written because this
  file is the historical record.)
  - The graph-level cause was confirmed rather than assumed, by reading the
    exports: the two smcleod encoders contain **16 `Einsum` nodes each**, all
    `b m h c d, c r d -> b m h c r`; the `onnx-community` export of the base
    model contains **zero** and writes the same attention as
    Reshape/Transpose/MatMul. So the answer to "why does 2B run on the GPU and
    Plus does not" is the export, not the model.
  - Removing both (rather than only NAR) is what let the whole raw
    `onnxruntime-node` path go: ~670 lines of the JS runner, the INT8 layouts,
    the per-model auto-CPU preference, and the top-level `onnxruntime-node`
    dependency. `npm ls onnxruntime-node` now shows exactly one nested copy --
    the one Transformers.js pins -- which retires the two-native-runtimes
    hazard the pin note was about. Verified afterwards by transcribing with
    Cohere and Granite 4.1 2B on WebGPU.
  - A settings file still naming a retired model needs no migration:
    `settings_store` already falls back to the default for an unknown
    `model_size`. That is now a test rather than an assumption.
- **A patch helper that truncated a file before it knew what to write.** My
  `edit()` wrapper was `io.open(p, "w").write(fn(s))` -- Python opens (and
  truncates) before evaluating `fn`, so when the substitution assertion failed
  the file was left empty. Recovered from git. Compute the new content first,
  then open for writing.
- **Parakeet on DirectML: still impossible, and it is not a pinning problem.**
  Another agent had suggested it as a ~2x speedup blocked by "pip problems".
  Both halves check out, and PyPI metadata settles it without an experiment:
  `onnxruntime-genai-directml` 0.14.1 requires `onnxruntime-directml>=1.26.0`,
  and the latest published `onnxruntime-directml` is **1.24.4** -- the required
  releases do not exist. Meanwhile `onnxruntime-genai` 0.15.2 (Nemotron)
  requires `onnxruntime>=1.28.0`, which the DirectML distribution cannot
  provide since it shares the same `onnxruntime` package directory. That is the
  mechanical reason the earlier mixed install died with "API version [26] is not
  available". A second isolated Python environment would work, and would save
  ~1.3 s per minute of dictation on the already-fastest engine; not worth a
  second venv, a subprocess protocol and a packaging story.
- **ORT 1.28.0 -> 1.29.0 is worth ~4% for Parakeet.** Measured in a throwaway
  venv against the same 62.7 s clip, three runs each: best RTF 0.05523 vs
  0.05774, and every 1.29 run beat every 1.28 run. `onnxruntime-genai` 0.15.2
  loads and runs Nemotron against 1.29 as well. Small but free; kept out of the
  release commit because it changes the native runtime under three engines.

## Round 17-19 (2026-08-30 / 2026-08-31)

Continuation of the adversarial review loop. Nine defects, each re-measured
independently before being accepted, fixed, and mutation-verified.

### Settings: an untouched Save reverted a limit another window had raised

`2361db7` corrected the *comparison* to ask "would the file change" instead of
"does this differ from my snapshot", but the save still *wrote* every field of
that snapshot. `history_dialog._persist_limit` writes `history_max_items`
straight to the store and notifies only the controller, so Settings kept the
old number. Measured through the real dialog: disk 800, spin box 500, 700
entries held, one Save with nothing touched wrote 500 back and the follow-on
trim deleted 201 transcripts. `_dialog_edits_over_stored` now applies only the
fields the user actually changed onto what is on disk.

The regression surfaced through a test the fix broke:
`test_save_persists_repaste_hotkey_and_new_toggles` cleared an optional hotkey
in a second save and saw the first save's value. The cause was the test fake:
`_FakeSettingsStore.load()` returned the constructor's object no matter what
`save()` had written, which no real store does. Corrected in both fake stores,
and the same sequence re-verified against the real `SettingsStore`.

### Hotkey: a punctuation key bound a completely different Windows key

`_KEY_MAP` already holds every letter and digit, so the `ord(key_name)`
fallback could only ever be reached by characters where `ord()` is not the
virtual-key code. Measured through the Settings field: "Ctrl+Alt+." bound
VK_DELETE, "-" VK_INSERT, "#" VK_END, "'" VK_RIGHT, while ";" and "AE" landed
on codes Windows assigns to nothing. One steals a system shortcut silently,
the other registers a hotkey that can never fire. The fallback is gone and the
rejection message is derived from `_KEY_MAP`, with a test that feeds every
name it prints back through the parser -- the first version of that message
advertised Insert, Delete, Home, End, PageUp and PageDown, none of which the
map holds.

### Streaming: the rolling-window merge emitted text twice

Two independent duplication paths, both reproduced deterministically.

`_join_at_seam` returned on the *first* skip whose overlap cleared the
threshold. On a base ending "sechs sieben acht" against a window "x sieben acht
drei vier fuenf sechs sieben acht neun zehn", skip 1 overlaps 2 words and skip
3 overlaps 6; taking skip 1 repeated six words, as `aligned=True`, which is the
flag the caller pins the floor from.

The floor branch's splice used the same three-word bound as the alignment above
it, and past the fourth junk word fell through to a blind weld of the whole
window. On a floor ending "ist noch nicht fertig": 11 words for up to three
junk words, 19 for four.

Both fixed, and measured over 300 randomised sessions per cell: mean extra
words 7.5 -> 2.1, worst case 38 -> 15, mean lost words 21.2 -> 21.3. The first
run of that measurement reported the two as identical, because the
reconstructed "old" `_join_at_seam` honoured the new call site's wider
`max_skip` -- half of what was being measured.

A third, smaller one: `_stream_word_key` strips punctuation, so "...", "!" and
":" all key to the empty string and match each other, and two of them cleared a
two-word threshold with no lexical agreement. `_substantive_word_count` counts
only non-empty keys. Counted rather than refused in `_stream_words_match`,
because a real seam may contain a standalone mark and refusing it breaks the
whole overlap down to nothing.

The first regression test for the seam rule separated it from neither mutant:
`skip + overlap` is the cut point, so every skip that finds one true seam
yields identical text. Both distinguishing inputs were found by searching the
real `merge_rolling_window` with each mutant installed. They show the rule
sits between two opposite failures -- first-passing duplicates, last-passing
loses speech.

### Tray: a notification icon a few hundred milliseconds early killed the app

`Shell_NotifyIcon(NIM_ADD)` raised out of `show()`, whose only caller is `main`
before `app.exec()`. Explorer also broadcasts `TaskbarCreated` before it will
accept icons, so the re-add after a restart routinely fails once -- and the
arm keyed on `_visible`, which is what the shell has accepted, so a restart
during a pending retry read a hidden icon and did nothing. With `_visible`
stuck False, `showMessage` returned early and every later tray notification
was dropped silently. Now: bounded retries, `_wanted_visible` as the intent,
a generation on each retry, a separate class for "added but version not set",
`RegisterWindowMessageW`'s 0 stored as `None` rather than as `WM_NULL`, and a
log line for a notification that cannot be shown.

### text_inserter: a refusal to touch the clipboard was re-raised as permission

Every re-raise builds a new exception, and a new one starts with
`allow_clipboard_fallback=True`. AGENTS.md recorded this as unreachable
"because no caller combines the two" -- both halves wrong: the combination
happens inside `insert_text`, whose handler *constructs* a
`ClipboardContentionError` when a non-contention failure after the paste
keystroke finds the clipboard changed. Measured: cause flag False, re-raised
flag True, and the controller would then have copied the transcript over the
clipboard the user had just filled.

### Three rollback arms that undid the wrong thing

All only reachable when the interpreter cannot create another thread. The
model scan's arm repeated half of its completion slot instead of calling it
(leaking a timing entry and leaving "Checking ... in the background" over a
scan that never began); it also wrote the *download's* label, so a user who
pressed Download was told a model scan had failed; and the benchmark's arm left
the Details overview reading the running summary it had been given a few lines
before the start.

### Corrections to earlier entries

- The "Three separate `settings_store.load()` calls" limitation recorded a race
  that cannot occur: every writer is a direct-connected Qt slot on the main
  thread and nothing between the reads pumps the event loop. Rewritten as the
  invariant that makes it safe, and what would break it.
- The comment in `find_cached_models` still said the ONNX scan works "exactly
  like the Whisper loop above", which `58c3038` made false.
- `window_focus` and `hotkey` declared no ctypes signatures and used the
  process-wide `ctypes.windll.user32`, where a declaration would leak to every
  other caller. Both now take their own handle. Hardening, not a measured
  defect: a real HWND here is 0x30766 and declared and undeclared calls agree.

### Two process notes

- `pytest ... | tail -3` returns *tail's* exit code. One round reported
  "exit code 0" over a real failure. Use `set -o pipefail`, or read the count.
- The Bash heredoc mangles backslashes in Python string literals. It silently
  did nothing to a mutation script this round, and the run that followed looked
  like a clean result for mutations that were never applied.

## Round 20 (2026-08-31) - the audio review

Three findings from the streaming-text/audio review, each re-measured before
being accepted.

### `close_if_idle()` reported success while an open was still in flight

It checked only `_consumer`, so mid-`ensure_started` -- `_starting` True,
`_stream` still None -- it bumped the generation, closed nothing and returned
True. Reproduced with a stubbed `sd.InputStream` whose `start()` blocks. The
caller reads True as "re-enumeration is safe" and calls
`try_refresh_input_devices`, which blocks on the `portaudio_guard()` the open
holds, then finds the stream that was registered while it waited and refuses.
A refused refresh is only retried on the next recording stop or abort, so a
hot-plugged microphone stayed invisible until the user recorded once. It defers
now, and the open is left to complete rather than cancelled, so the stream the
deferred refresh will close is a real one.

### The speech-run measurement defaulted to the bucket it must not use

`measure_longest_speech_run_s(window_ms=SILENCE_GATE_WINDOW_MS)` -- 100 ms,
which AGENTS.md explains at length is wrong for this measurement, because two
keystrokes 100-150 ms apart land in adjacent buckets and typing measures as
1.5 s of speech. The one production caller passes 20 explicitly, so it was
latent. It is a required keyword argument now: this repository has already
carried one threshold across a bucket-size change without rederiving it.

### The device-key check was documented in the wrong function

AGENTS.md said `attach` requires the warm stream's `opened_device_key` to
match. It did not -- `AudioCapture.start` read the property and then called
`attach`, two acquisitions of the same lock. The gap is microseconds against a
device open of milliseconds to seconds, so nothing was observed going through
it. The check moved into `attach`, under the lock that publishes the stream,
rather than the sentence being reworded: an invariant documented as belonging
to a function that does not hold it is the shape a later refactor drops.

### Areas the review found clean

`compute_stream_locked_prefix` / `apply_partial_append_only` / `rollback_commit`
over 300 randomised sessions with revising providers and 15% paste failures: no
document/`committed_text` divergence, no decreasing committed word count, no
word emitted twice. `stream_join_text` / `stream_insertion_text` across quotes,
brackets, hyphens, umlauts, combining accents, NBSP, zero-width space and the
empty string. Floor containment and branch reporting over 4000 randomised
merges: no lost floor, no `aligned=True` that is not a word-extension. `vad.py`
numerics: no sample/byte confusion, no NaN or division by zero on empty, odd,
sub-bucket or zero-rate input, and `None` honoured as unmeasurable by every
caller. `audio_devices.py` registration ordering. The PortAudio callback path:
nothing blocks or allocates unboundedly per callback.

### One more process note

The Bash heredoc's backslash handling bit twice more this round, once
silently: a mutation script's anchors were rewritten to nothing and the run
that followed reported clean results for mutations that had never been
applied. Anything containing a backslash escape now goes through the Write
tool or a real file, never a heredoc.

## Round 21 (2026-08-31) - the settings, streaming and Win32 round

Three lead-verified findings from the previous round's agents, each reproduced
against the real component before being accepted, and five documentation
claims that later measurement falsified.

### The settings merge held for exactly one save

`_dialog_edits_over_stored` exists so that pressing Save writes the user's
edits onto the file rather than the dialog-open snapshot, which is what stops
an untouched Settings window reverting a limit the History dialog raised. It
diffed against `_loaded_settings` -- and `_save` ends by assigning that same
attribute the *merged* object. So the snapshot absorbed the external 800 while
the spin box kept showing 500, and the second save read the 500 as a genuine
edit.

Reproduced against the real `SettingsStore` with 700 entries: save #1 correct
(disk 800), save #2 wrote 500 and trimmed, 200 transcripts deleted behind a
prompt naming a limit nobody chose. Silently, with no prompt at all, whenever
the history was smaller than the limit.

The attribute was doing two incompatible jobs -- "what the widgets were
populated from" and "what was last written" -- which diverge the moment a
merge happens. `_populated_settings` now answers the first.

Two wrong versions on the way to the fix, both caught by measurement rather
than review:

- **A frozen baseline** (written only where widgets are written) fixes the
  revert and breaks the reverse: set a checkbox, save, change your mind, set
  it back, and the second save finds the box agreeing with the dialog-open
  value, calls it no edit, and writes nothing. The probe matrix caught it on
  `tray_middle_click_toggle`; a probe that had only tested the revert
  direction would have shipped it.
- **Recording the merged language on the key-save path** is the same defect
  one level in. That path writes no language -- it reads the value back off
  disk precisely so an overlay pick survives -- so recording what it wrote
  tells the next settings save that the combo has moved, and the untouched
  combo becomes a deliberate choice again.

Nine mutations, eight detected. The survivor is `_language_mode_for_save`'s
own snapshot read: `_dialog_edits_over_stored` compares its answer against the
same baseline afterwards, so an untouched combo produces no edit whichever
snapshot it consults, and the only caller that bypasses that merge (the Import
tab, via `_build_current_settings`) always passes an override. The change was
kept for consistency and the docstring says plainly that it is not
independently observable -- the alternative was a comment implying a fix that
no test can demonstrate.

A tenth mutation settled a claim rather than a fix: the trim reading the spin
box instead of the saved limit was recorded as "an equivalent change today
that mutation testing cannot tell apart". Once the merge holds across saves
the two genuinely disagree, and the substitution now fails two tests.

### The seam rule: two obvious rules, each fixing the other's defect

The previous round replaced "first skip over the threshold wins" with "longest
overlap wins" and widened the floor branch's search past
`_WINDOW_BOUNDARY_SKIP_WORDS`, recording that "widening it is safe there and
not above". It is not. Widening without bounding the discard inverts the
original defect: a deep coincidence beats a shallow real seam. Two measured
cases -- a 3-word match at skip 7 against the real 2-word seam at skip 2 drops
"ein neuer gedanke"; a 2-word match at skip 5 discards an entire window ("und
und und" + "dann dann dann dann dann und und" -> "und und und").

`overlap - skip` -- words explained minus window words discarded -- gets both,
with a non-negative score additionally required past the boundary bound, where
nothing else caps the discard. Inside the bound the cap is the bound itself,
and requiring it there would only raise skip 3's bar; a refused alignment
falls through to a replace, which before the first measured pause loses the
whole dictation.

Five candidate rules were evaluated over 20000 randomised German merges, the
same merges under each: shipped-old 17 words lost / 74129 duplicated,
longest-wins 29 / 16712, `overlap - skip` 2 / 62893. Better than the original
on both axes. (Published first as 62884, which is the figure for a variant
that applies the net requirement at every depth rather than only past the
boundary bound. The scratch harness that produced the original numbers did
not run as saved -- it passed an argument its own builder did not accept --
so re-deriving it was what surfaced the mix-up. A number that cannot be
re-run is not yet a measurement.) What this measures is which candidate
each rule picks on synthetic seams, not accuracy on real audio.

Two lessons about the tests rather than the code:

- The two parametrized seam tests asserted whatever the then-current rule
  produced on strings a search had found. They pinned a rule, not a behaviour,
  and broke the moment the rule was corrected -- which is the only reason the
  correction was noticed to need re-deriving. Every replacement case has an
  unambiguous right answer.
- Two of my first mutation cases survived because I had mis-specified them:
  they changed the tie-break rather than the first-match rule. A mutation that
  does not revert the property under test proves nothing, and "survived" reads
  identically either way.

### The punctuation gate could cause a replace

Counting substantive words *against* the overlap threshold is stricter than
the threshold has ever been. A seam of "praktisch ..." counts one, fails a
threshold of two, and the merge falls through to a replace with no floor to
bound it: a 13-word dictation becomes the 8-word window, so 11 words are
gone and only the two in the overlap survive. The commit message says "13
words lost", which is the size of the dictation rather than the loss. The
rule is the raw token threshold as before, plus at least one real word --
which is all that is needed to rule out a seam made of nothing but marks.

### `use_last_error=True` moves the error, and silenced every hotkey failure

Giving `window_focus` and `hotkey` their own `WinDLL` handle was recorded as
"hardening, not a fix for anything observed". True of `window_focus`; false of
the change as a whole. `use_last_error=True` saves the Windows error into
ctypes' private per-call slot and *restores* the thread's `GetLastError` to
its previous value, so `Win32HotkeyApi.get_last_error`'s
`ctypes.GetLastError()` answered 0 and every failed registration reported
"Unknown Windows hotkey registration error".

Measured against a real double `RegisterHotKey`: thread reader 0, ctypes slot
1409 -- "another program holds this combination", the code the fallback and
reclaim machinery exists for. The user-facing message is now `Failed to
register hotkey: Ctrl+Alt+F12. Windows reported hotkey already registered
(1409).`

The same entry claimed a 64-bit handle "already fails outright" as though the
undeclared call were the safer one. `wintypes.HWND` *is* `c_void_p`, so
declaring it removes that check rather than keeping it: measured with
0x7FF8_1234_5678, undeclared raises `ArgumentError: int too long to convert`
and declared accepts it and returns 0. The overflow was ctypes refusing a
legal handle, not a guard -- the declaration is still the right change, for
the opposite reason to the one written down.

### Withdrawn: the 7.5 -> 2.1 seam figures

"Mean extra words 7.5 -> 2.1, worst case 38 -> 15, while mean lost words moved
21.2 -> 21.3" came from a harness that was never committed and could not be
reproduced. The conclusion drawn from it -- "it does not buy the reduction
with lost speech" -- is false for the rule it described: longest-overlap-wins
loses 29 words where the original loses 17. Replaced with the rule evaluation
above, which is reproducible from the numbers it states.

### Process notes

- Probes must test both directions of a fix. The frozen-baseline version
  passed every revert case and would have shipped a save that silently
  discarded a change of mind.
- Two probe failures this round were artefacts of the probe, not the code:
  `findData("en")` returned -1 because the default model (Parakeet) exposes
  only `auto`, so the combo has one item. A failing assertion is a claim about
  the code and needs the same check as a passing one.
- The mutation harness prints `[lines containing "passed"/"failed"] or
  lines[-1:]`, and the repository's own `-q` makes that `-qq`, which
  suppresses the count line. So a run with several failures shows only the
  last `FAILED` line. It was settled here by re-running with a single-test
  selector; the harness's reporting should be fixed before it misleads.

## Round 22-23 (2026-08-31 / 2026-09-04) - remote providers, the warm stream, and a field report

Round 22 was cut short by the weekly limit and resumed on 2026-09-04 with four
breakers in parallel (concurrency, external facts against the installed SDK
sources, boundaries and hostile input, and a hunt for a field regression). Every
finding below was reproduced by the lead before it was accepted; the refutations
are listed with the reason, so they are not re-raised.

### The AssemblyAI poll was bounded around the wrong thing

`_wait_for_transcript` put a deadline around `Transcript.get_by_id`, which in
SDK 0.64.33 is `cls(transcript_id=...).wait_for_completion()` -- the SDK's own
unbounded `while True:`. The deadline therefore bounded nothing: measured still
blocked after 12.0 s on a 3.0 s budget, 46 polls inside one call. AGENTS.md had
recorded the defect as fixed for a month. The fetch is now
`api.get_transcript(http_client, id)` wrapped by `Transcript.from_response`,
which is the single request the SDK's own loop is built from; one test runs
that fetch against the installed SDK with a stub that raises on a second poll,
so a fetch that waits fails instead of hanging the suite.

Two later findings on the same loop: a status fetch that raised (a read
timeout, a 5xx) aborted the whole wait and the message omitted the transcript
id, the one thing that would let the job be recovered -- it is now retried
across three consecutive failures one interval apart, with the id in the
message; and the fake-clock guard sat inside the patched `time.sleep`, so a
loop that stops sleeping makes the guard unreachable and hangs pytest instead
of failing it (measured: 23.5 million iterations in 2 s, guard never fired).
The pending fake now caps fetches as well, and a test pins the sleep itself.

### The stop budget modelled the teardown wrongly, twice

The first version put the app's 5.0 s join on top of the SDK's 5.0 s
`terminate_timeout`, so the final Turn was dropped and returned as a silently
shortened transcript. The second version's comment modelled the whole of
`disconnect(terminate=True)` as `terminate_timeout + 2 s`. It is not: the call
ends in `websockets.sync`'s `close()`, which waits for the peer's close
handshake up to a `close_timeout` the SDK never passes -- 10 s by default,
measured 9.02 s against a loopback peer that never acknowledges the close
frame. The 8 s budget deliberately stops short of that stage, because nothing
is dispatched once the read thread is joined and the turn is stored before the
joins begin; the comment now says so, and a test pins that the SDK source still
leaves `close_timeout` to the library.

### Fun-ASR: three ways the receive loop lost text, and one it could spin

No total budget (198 receive calls in 4 s, holding the single transcription
worker and blocking process exit through the executor's exit-handler join); a
server
CLOSE frame -- which `websocket-client` returns as `""` -- read as "keep
waiting"; and every exit other than `task-finished` discarding the sentences
already received. Then, from the boundaries breaker: a `result-generated` with
`sentence_end` true and empty `text` reset the pending partial after an
`if text: append` that did nothing, so the transcript came back truncated *as
a clean success* (partial 'Hallo', empty final, task-finished -> ''). The last
partial is now the sentence's text when the final carries none. And the loop
skipped frames that were not events with no bound but the budget: a real
socket blocks in `recv`, so only a flooding peer can make it spin, but it then
pinned a core for thirty minutes (1.37 million receive calls in 0.31 s against
an instant fake). More than 1000 unusable frames in a row now fail the request
with the text received so far.

A duplicate finalized sentence is deliberately not de-duplicated: nothing shows
the service re-delivers one, and a user can say the same sentence twice.

The external-facts breaker read the vendor's client-events page: `heartbeat`
defaults to false, and the documented contract is that the connection is
closed after a period of continuously silent audio -- which a paused recording
uploaded over the realtime protocol is. The run-task now asks for it. This is
the one change this round that could not be verified against the live service
from here, and it is labelled so in AGENTS.md and the commit.

### The overlay's Insert pasted the whole dictation

A streaming finalize inserts only the tail past `committed_text`; the Error
state's Insert was wired to `repaste_last_transcript`, which pastes the last
transcript. Measured: finalize inserted ' zweiter teil', Insert then pasted
'erster teil zweiter teil' on top of the 'erster teil' already in the document.
The text of the failed insert is recorded where the Error is painted and a new
slot pastes exactly it; the overlay wiring was extracted from `main.run` so it
can be pinned by emitting the signal. The background arm must record it too,
even though it sets `_last_transcript` to the same text: the cancel hotkey's
"nothing to cancel" path flushes a queued job's failing insert without a new
recording in between, and Insert then pasted the previous failure's tail while
the overlay displayed the queued transcript.

### The warm microphone stream and its own re-enumeration

`close_if_idle` answered True while a helper thread was still closing the
stream `request_restart` had handed it, and while an open was in flight. The
device refresh then found a live stream, refused, and was only retried at the
next recording stop -- so a hot-plugged microphone stayed invisible until the
user recorded once. Routine: a recording stop runs `detach` (a deferred
restart) and then arms exactly this refresh.

The fix went through two versions. The first waited on a `Condition` for the
open and the close to finish and then bumped the generation. It lost a race:
the helper that finishes its close reaches its reopen on its own lock
acquisition, and the notify does not hand the waiter the lock first -- the
helper reopened, the waiter waited for that open too, closed the new stream,
and the test timed out inside the second close. Bumping the generation
*before* the wait refuses the helper's reopen whichever thread wins.

Three more from the same review, all measured: a helper thread that could not
be started (`Thread.start` raising) left the microphone open and registered for
the process lifetime with nothing able to close it, and through `detach` inside
`AudioCapture.stop` the error escaped before the chunks were drained, losing the
recording; disabling the feature during an in-flight restart let the helper
open a fresh stream after the controller had dropped its reference; and a
microphone change saved while the stream was opening took a retry branch whose
`ensure_started` no-ops on the `_starting` guard, pinning the stream to the
old device for the session. The retired stream now stays reachable until
closed, `close` cancels the reopen through the generation, and the controller
restarts an in-flight open.

Also from that review: anything escaping the PortAudio callback makes
sounddevice abort the stream (its wrapper catches only its own two exception
types), so a cold-stream recording went silently deaf; and the VAD auto-stop
latch was set before the thread that delivers it existed. The callback is now
exception-tight and the latch is reset when the thread cannot start.

### Smaller ones

- The provider error body was read in full before being capped to 300 chars
  (50,000,000 bytes pulled off the socket for a 300-char detail); the read is
  capped at 64 KiB.
- ElevenLabs' documented error shape nests the message under `detail`, and the
  key loop `str()`-ed the dict; nested objects are now unwrapped.
- `_on_transcription_failed`'s background arm never gave the active token back.
- The microphone picker called a plugged-in microphone "(not connected)" while
  PortAudio was being re-initialized; it now distinguishes "did not answer".

### Refuted or deferred, with the reason

- Deepgram's 32-chunk sender queue and 2 s drain: a tuning decision, not a
  defect; still open for a decision before the release.
- A negative `ASSEMBLYAI_BATCH_MAX_WAIT_S` renders "-1 minutes": the constant
  is not user-configurable; unreachable.
- `Authorization: bearer` in lower case against DashScope's documented
  `Bearer`: RFC 7235 makes the scheme token case-insensitive; unverifiable
  without a live call, left as is.
- `_make_fake_aai`'s transcript starts in a terminal status, unlike a real
  `submit(poll=False)`: a test-design observation; the poll loop's own tests use
  the pending fake.
- `_report_background_failure` raising and skipping the token clear: a
  hypothesis with no raise site; not acted on.

### The field report

On a corporate machine the user could record but every transcription failed
at HEAD, with Parakeet selected, and a checkout from 2026-08-28 worked; no
logs. Not reproducible on HomeBase in three configurations, and the hunt
breaker proved the negative for Parakeet three ways: an AST function diff
(`local_onnx_asr`, `factory`, `base`: zero changed functions), a coverage
differential of a complete dictation (HEAD runs three extra functions, all
no-ops on the batch path), and a cache-layout differential (cached in all four
layouts in both trees). The one confirmed HEAD divergence that produces the
symptom -- the inventory answering only from the configured Model Dir, so a
faster-whisper model that lives elsewhere is downloaded at startup and behind
a blocked proxy every dictation waits and then cancels -- cannot apply to
Parakeet, whose inventory still searches every root. The instrument that
settles it is the work machine's `dictation.log`, and the report to the user
names the lines to grep.

### Process notes

- A retraction was avoided this time by writing the claim before the test: the
  `close_if_idle` fix was tested by the sequence that had been measured, and
  the first version failed that test rather than shipping.
- Two breakers reported the same warm-stream defect from different briefs;
  the lead reproduced it once and refuted nothing twice. Deduplicate findings
  across briefs before reproducing.
- The Bash heredoc failed once on a script containing several triple-quoted
  strings; generating patch scripts as files and running them is the reliable
  path, and it leaves the exact edit on disk.

## Round 24 (2026-09-04) - the second wave on the round-22/23 fixes

Four breakers with fresh contexts and distinct briefs -- concurrency, boundaries
and hostile input, external facts, and reachability of the previous round's
fixes -- against the commits of rounds 22-23 and the two still-open warm-stream
items. Every finding below was reproduced by the lead before it was acted on,
every fix was mutation-tested (a fix whose test does not fail on the old code
is not a fix), and the refutations are listed with the reason so they are not
re-raised.

### The warm stream: the previous fix left two ways to the same refusal

`close_if_idle` waited for `_starting` and `_closes_in_flight` and then closed
what was left. Two streams escaped that accounting:

- The open a restart superseded closed its stream on its own thread *after*
  releasing the lock, and nothing counted that close. `close_if_idle` answered
  True with the stream still registered (measured: `close_if_idle() -> True`,
  `live_stream_count() -> 1`), the refresh it arms found the stream and
  refused, and the microphone the refresh was for stayed invisible until the
  next recording stop. The superseded open now hands its stream to `_retiring`
  under the lock and closes it through `_close_retiring`, the same road every
  other retired stream takes.
- A `request_restart` during a capture leaves `_pending_restart` set, and the
  `detach` at the recording stop honours it. `close_if_idle` bumped the
  generation and cleared nothing else, so the detach reopened the stream the
  refresh had just closed and the refresh was refused on the next stop too. It
  clears the flag with the bump.

The first version of the controller half restarted *every* open in flight on
a settings save, which the reachability breaker measured as one extra device
open per unrelated save -- an opacity change during the seconds a locked-down
audio stack takes to open. `opening_device_key` names the device an open is
resolving, and the controller restarts only when it differs from the saved
one. Also from that review: the once-per-capture callback log was latched
once per *process*, because nothing re-armed `_callback_failed` between
recordings.

Refuted on the same code: the reachability breaker's probes for
`close_if_idle` failed on the fixed tree, and both failures were probe
assumptions -- one fake never released the close gate in the order the fixed
code needs, and one stub predated `opening_device_key`. Neither is a defect;
both are recorded so the next round does not rerun them as findings.

### The download queue's drain summary reported its last outcome

The round-23 rule -- a drain that downloaded something after a Cancel says so
-- was implemented as "report what ran", so with `medium` canceled and a later
`small` downloaded the summary read "Downloaded: small" and the canceled row
said nothing; with a Cancel after the first model finished, the run reported
as a plain success. `_canceled_drain_summary` now leads every drain that
consumed a Cancel with "Download canceled." and lists what completed, failed,
was removed and ran afterwards on its own side of the cut, from a counter
snapshot taken where the event is consumed. The headline is dropped only when
the drain has nothing of its own to say. The concurrency breaker's second
finding here was a hardening: an entry queued during shutdown would have
started a fresh download from the `aboutToQuit` handler's own drain, so the
enqueue refuses after `_shutdown_started`.

### Fun-ASR: the frame bound reset once per event

The round-23 bound on unusable frames lived inside `_recv_event`, which the
transcript loop calls once per *event*. A peer alternating one JSON object
that is not an event with any junk therefore reset the counter every call, and
the spin the bound existed to stop was back (an empty object or an unknown
event name counts as unusable as well). The budget is now created per
transcript, spent on every unusable frame, and reset only by the three event
names the loop acts on. From the boundaries breaker's hostile-input file:
`text` was `str.strip`-ed and `sentence_end` `bool()`-ed without a type check,
so a number or a list in either reached the loop; both are typed before they
are trusted.

### AssemblyAI: the poll outlived the app, and a status-less object was "still waiting"

`ThreadPoolExecutor` joins its worker at exit, and the batch poll had no
reason to stop, so a quit during a job the service never finishes kept the
process alive for the rest of the thirty-minute budget -- holding the
single-instance lock, so the app could not even be restarted. An app-wide
shutdown flag in `transcriber/base.py` is set as the first statement of
`DictationController.shutdown`; the poll reads it at the top of its loop and
between the half-second slices its sleep is cut into, and the Fun-ASR receive
loop reads the same flag. Two smaller ones: a fetch returning an object
without a `status` raised `AttributeError` past the whole wait instead of
counting as a failed fetch, and the docstring's bound was the budget alone
while the true bound is the budget plus one request in flight.

### The controller: a refused start retired the Insert offer

The round-23 Insert fix cleared `_insert_action_text` on the statement after
`_recording_start_in_progress`, before any branch that decides whether a
recording starts. A refused start -- "Model is still loading" right after a
dictation, the common case -- therefore retired the tail of a failed
streaming finalize and repainted the overlay without it, leaving only the
whole-dictation re-paste, which pastes on top of the prefix already in the
document. The clear now sits past every refusal, and a refusal painted while
an offer is pending keeps the text on screen with Copy and Insert acting on
exactly it. Two more from the same file: `refresh_hotkey_registration` was the
one writer of the registration state that did not repaint the idle line, so a
resume that substituted a fallback advertised a key that no longer fired; and
the preload's `Executor.submit` was the third unguarded submit site, leaving
the overlay on "Loading" for good when it raised.

### Smaller ones

- A download queued while the old worker was still draining its cancel was
  discarded silently; the worker now consumes the event and continues.
- The update dialog's download label divided by 1024 squared and wrote "MB":
  the third decimal-megabyte instance in this repository.
- The retranscribe dialog's language-substitution note was Canary-only, while
  any model can decline the selected language; the sentence is hoisted and
  the reservation measured for the worst case.
- The download lock lives under the calling user's `%APPDATA%`, so it covers
  one Windows user's processes, not a second account sharing the Model Dir;
  the table row, the coordinator's docstring and one test docstring said
  "machine-wide" and no longer do.
- The nested-status HTTP test asserted a substring and a length, both of
  which the stringified dict also satisfied; it
  now asserts the unwrapped string.

### Refuted or judged, with the reason

- Two identical USB microphones are one picker entry and the first index
  wins: real, and kept -- the name is what makes a selection survive a
  re-enumeration and a reboot. Recorded under Known limitations.
- The update label showing `downloaded > total`: unreachable, because the
  verified download raises before reporting progress past the declared size.
- A Download refused while `_claimed` still names a dying process: transient
  and pre-existing; the refusal is correct while the claim stands.
- The strip mismatch between the background arm's Copy and Insert: no
  observable difference for any text a transcriber returns, unified anyway
  because two readers of one value should read one value.
- Two test observations from the reachability breaker (structural
  `AttributeError` failures in the stub-based tests): verified by mutation to
  fail on the old code; the stubs are deliberate.
- The 9.02 s measured against the 10.00 s `close_timeout`: a different
  measurement stack, not a contradiction of the SDK source.
- A failed `UnregisterHotKey` message naming the wrong key: unreachable, the
  managers are built thread-bound (`hwnd=None`), so the call cannot fail that
  way.
- AltGr suppression dropping a genuine Ctrl+Alt hotkey while the right Alt is
  held: deliberate, and documented.
- Deepgram's 32-chunk sender queue (about 3.2 s of audio at 100 ms blocks) and
  the 2 s stop drain: a tuning decision for the user, not a defect.

### The field report

Still not reproducible: the Parakeet batch path is byte-identical between
HEAD and the 2026-08-28 checkout on three independent measurements (AST
function diff, coverage differential of a complete dictation, cache-layout
differential), and the one HEAD divergence that produces the symptom applies
to faster-whisper with a custom Model Dir, which Parakeet's inventory does
not share. The instrument is the work machine's `dictation.log`; the report
to the user names the lines to grep. The `aug28` worktree stays until the
log arrives.

### Process notes

- A mutant that "survives" can be a no-op edit: adding a redundant line in
  front of the real assignment changes nothing, so the test cannot see it.
  Check that the mutant changes behaviour before reading a survival as a weak
  test.
- A probe that fails on the fixed tree is a finding about the probe until
  the failure is reproduced against the fix's own claim. Two of four probes
  this round encoded the old code's ordering.
- The Bash heredoc failed again on a script with several triple-quoted
  strings; writing the patch script as a file and running it remains the
  reliable path.
- Restaging mixed working-tree changes into logical commits is worth the
  detour: backing the verified files up, reverting, and re-applying stage by
  stage gave three commits that each say one thing, and a byte comparison
  against the backups proved nothing was lost on the way.
- **The suite went red and was pushed red.** Every per-file run this round
  was green; the full suite had 26 failures, all one cause: the new
  process-wide shutdown flag is set by every controller test's `shutdown()`
  and nothing reset it, so every provider loop that ran after a controller
  test gave up at once. No single file runs a controller test before a
  provider test, which is why the per-file runs could not see it. The
  fixture that resets it is in `tests/conftest.py`. The push happened
  because the docs commit sat in a shell chain that did not gate on the
  suite's count, and the pipeline's exit code was `tail`'s. Both halves are
  now rules in AGENTS.md: process-global state set by `shutdown()` gets an
  autouse reset, and a push waits for the printed `N passed` line.

### Wave 3 (2026-09-04) - the second wave on the round-24 fixes

Four read-only breakers with distinct lenses (concurrency, reach, boundaries,
external facts) on the round-24 commits; every finding below was reproduced
by running the breaker's own probe before anything was changed.

**Confirmed and fixed**

- *Warm stream* (`d5e4f2a`): `close_if_idle` closed outside its own
  accounting, so a second `close_if_idle` answered True during the first's
  `stream.close()`; a bare `ensure_started` during that close opened a
  second stream and the re-enumeration was refused (measured: streams
  constructed 2, `try_refresh_input_devices() -> False`,
  `_pending_audio_device_refresh` True); `detach` restarted through
  `request_restart` after releasing its lock (forced schedule only, 0 hits
  in 400 natural trials); and `opening_device_key` was None for the whole
  device query, so a microphone change saved inside it did not restart the
  open (the setting said Mic B, the stream opened Mic A, `attach` refused
  it). The guard now sits before the gate, the worker holds it across close
  and refresh, closes are counted, the restart runs under one hold, and the
  selected key is published before resolution.
- *Insert offer* (`8506509`): the refusal fix of round 24 re-armed an
  Insert button for a paste that had most likely landed (double paste,
  measured through the overlay's Insert and a queued flush); the
  unconditional resume repaint painted "Idle" over Done and over the Error
  carrying the only Insert button; every other non-result status writer
  hid the button. One painter, the may-have-pasted flag deciding, the
  resume repainting only on a changed registration state.
- *Preload* (`cdb3ca9`): the submit-failure arm left the stale future
  installed, so the save meant to fix the problem found "running" and
  nothing to retry.
- *Drain summary* (`60f0b6c`): a Cancel that only emptied the queue printed
  "Downloaded: " in green; removed entries were never named; two Cancels in
  one drain shared one snapshot. Now a timeline.
- *Fun-ASR* (`5407c85`): the budget was spent before classification, so
  exactly the bound of junk followed by a real `task-finished` failed the
  transcription; heartbeat packets (documented by the vendor as ignorable,
  requested by this provider) were read as sentences; the `task-failed`
  detail had no cap.

**Refuted, with the evidence**

- A bare-string AssemblyAI status "is not recognized as terminal": the
  installed SDK's `TranscriptStatus` is a `str` enum, so
  `'completed' in {TranscriptStatus.completed, ...}` is True.
- A NaN `http_timeout` sleeping forever: an SDK global setting, not
  reachable from this code.
- Hostile `_on_progress` calls and a 300-character language code: unreachable
  from any caller.
- The frame bound "evaded by one real event per 1000 frames": inherent to a
  consecutive-frame bound and equal to the pre-fix worst case, and the
  receive loop still ends on the app's shutdown flag.
- `_callback_failed` read outside the lock: one duplicated log line at
  worst, not a finding.

**Process notes**

- The mutation harness takes one test file per case. Two files in one
  argument reach pytest as a path with a space, the run errors, and the
  harness reports DETECTED with an empty failure list -- a false kill. Every
  DETECTED must name a `FAILED ...` line.
- A Bash heredoc handed `b"\x00\x01"` to Python as real NUL and 0x01 bytes
  inside a test file ("source code string cannot contain null bytes"). Byte
  escapes in a patch go through a script file, not a heredoc.
- The first draft of the drain rule headlined *every* consumed Cancel and
  broke the round-24 test for a Cancel that found nothing queued and nothing
  running: that Cancel did nothing the drain has to report, and headlining
  it put a successful later download in the error colour. Substance is
  "removed queued entries"; a killed download reports itself through its
  own status.
- `ThreadPoolExecutor` registers its exit handler through
  `threading._register_atexit`, not `atexit.register`, so a grep for the
  latter finds nothing. Eight files said "atexit hook" or "atexit join";
  `e8b1456` corrected the three documents and the wave-4 facts review found
  the other five (two source comments, a docstring, three test comments),
  which are corrected in the wave-4 wording commit.
