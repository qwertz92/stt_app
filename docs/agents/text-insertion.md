# Text insertion and focus: design decisions

Binding project rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30. Read this file before changing `text_inserter.py`,
`window_focus.py`, the insert offer and queued/deferred inserts. Entries keep
their order, so "the entry above/below" refers to this file; "Known
limitations" is `docs/agents/known-limitations.md`. Measurements, rejected
alternatives and history are in `docs/learning-log.md` and git history.

Verbatim pre-condensation text: `git show e608f86:docs/agents/text-insertion.md` (original AGENTS.md: `df2642a`).

- **`GUITHREADINFO` is defined in both modules on purpose** (self-contained).
- **A paste is a transaction with a deferred, guarded restore** (F01/F02/F07).
  `TextInserter._paste_text_with_options` always logs one `paste_transaction
  id=... mode=<requested>/<actual> target_hwnd=... foreground=... chars=...
  marker=... outcome=... restore=...` line; `_run_paste_transaction` works.
  - *The restore is not part of the call.* A SendInput paste returns at the
    keystroke and hands a `_PendingRestore` to `schedule_fn` (default daemon
    `threading.Timer`). After `CLIPBOARD_RESTORE_DELAY_S` (1.5 s) the timer
    runs `wait_for_paste_target_ready` outside `_insert_lock`, then under it
    restores only while the clipboard still holds the transcript (content,
    never the counter alone), reschedules while busy, and gives up at
    `CLIPBOARD_RESTORE_MAX_WAIT_S` (10 s) leaving the transcript. Logged as
    `clipboard_restore id=... outcome=restored|skipped_changed|superseded|
    superseded_changed|busy_rescheduled|abandoned_busy|failed_retrying|
    busy_retrying|busy_refused|resumed|failed delay_ms=...`; failure is WARNING, never raised. Why: a fixed 160 ms Qt-thread sleep
    (`SENDINPUT_RESTORE_DELAY_S`) lost pastes into Electron
    (`probe_wm_null_order.py`: WM_NULL answers before queued input). Raising
    the delay was rejected: on the Qt thread a longer sleep froze the UI
    during a streaming dictation, and any fixed delay only moves the race.
  - A paste during a pending restore takes the record over, keeping the
    *original* previous state while the clipboard is still ours by the rule
    below (streaming pastes every ~350 ms). A clipboard that cannot be read
    is inherited only while the sequence counter is unmoved since our last
    write; with a moved counter the paste is refused as contention before it
    writes anything (`busy_refused`) and the record stays pending, because
    inheriting restored the older state over a copy the user had just made
    (review of a404479). A paste that then never touches the clipboard hands
    the record back
    (`_resume_pending_restore`), so the user's content is not lost with it.
    Each schedule and each cancel bumps `_PendingRestore.chain`, and a timer
    acts only while the number it was scheduled with is current: a timer
    already inside its readiness probe when the record was taken over and
    resumed finds a newer number under the lock and stops, so one record
    never has two chains rescheduling and spending its retry attempts
    (2026-10-03).
    `flush_pending_restore`
    restores at once (content check kept); `DictationController.shutdown`
    calls it first. A scheduler that cannot start a thread leaves the record
    pending, never reports a landed paste as failed.
  - WM_PASTE stays synchronous (contention check, raising restore failure).
  - *A restore that fails is written again, then reported* (2026-10-01).
    `restore_clipboard_state` runs through `_with_reopen` with
    `reopen_on_lost_close` (its ctypes `SetClipboardData` answers NULL on a
    closed clipboard without raising), and raises `ClipboardEmptiedError`
    when it emptied the clipboard and wrote none of what it had to. A
    reattempt never empties a foreign write: before our `EmptyClipboard` the
    counter must be unchanged since the call began, after it the clipboard
    may hold only our own partial restore (`_holds_only_our_restore`: every
    format byte-equal to a captured one, trailing NULs aside, or synthesized
    from one; empty counts as ours there, within the call's ~0.3 s reopen
    budget only). Either refusal raises `_ClipboardRestoreRefusedError`,
    which ends the restore as `skipped_changed`. A restore that still fails
    -- the immediate one after a failed write, or the deferred one -- stays
    the pending record and is retried `CLIPBOARD_RESTORE_RETRY_ATTEMPTS` (3)
    times `CLIPBOARD_RESTORE_RETRY_DELAY_S` (1.0 s) apart on the scheduler
    from the captured state; then it is reported once through
    `set_restore_failure_handler` -> `clipboard_restore_failed` -> the tray
    ("Clipboard not restored"). A check that cannot read the clipboard
    counts as one of those attempts (`busy_retrying`): another program holds
    it, which is never evidence of a change -- answering "changed" there
    dropped the record silently and lost the user's content (2026-10-01
    review, P1). Why the retry exists at all: after a reopen failure or a
    manager closing the restore's open the user's clipboard stayed empty,
    and nothing tried again. A write whose close was lost and whose
    read-back could not open (`_ClipboardWriteUnconfirmedError`, contention:
    nothing pastes) arms the same retry with the counter read before the
    read-back, because the transcript most likely replaced the user's
    content. `flush_pending_restore` at shutdown does not retry.
  - *Whether the clipboard is still ours goes by Windows' sequence counter*
    (`TextInserter._restore_check`, owner's decision 2026-10-01). The record
    keeps `GetClipboardSequenceNumber` read right after our own last write:
    the transcript write's marker, or the counter read after a restore
    attempt that failed past its `EmptyClipboard` (the record then forgets
    the transcript: `text=None`). A record whose clipboard was emptied or
    holds a partial restore of ours is ours only while the counter is
    unchanged. A record that left the transcript there is ours while the
    clipboard reads exactly that text, even when the counter moved (a
    clipboard manager may set the same content again), and changed when it
    reads anything else. Changed means `skipped_changed`, no report. So an
    empty clipboard is never evidence on its own -- a Win+V "Clear all" or a
    password manager's clear moves the counter and is left alone -- and a
    user's re-copy of the same plain text no longer gets the old HTML
    formats written next to it. Why: the content heuristic
    (`_holds_only_our_restore`) took both for our partial restore (2026-10-01
    review, P3). Residuals: the user copying our exact transcript back out
    of the document while a retry is pending looks like ours; a foreign
    write between our last write or failed attempt and the counter read is
    adopted as ours (the same microseconds `ClipboardMarker` describes).
  - *The marker is read inside the write*: `set_clipboard_text` returns a
    `ClipboardMarker` read before it returns; `_clipboard_changed_after_set`
    compares content when readable, the counter only otherwise (each open may
    raise retryable `ClipboardContentionError`).
  - *A 1418 inside our open clipboard reopens and retries* (2026-10-01,
    `Win32ClipboardBackend._with_reopen`, 3 attempts). pywin32 opens with a
    NULL owner, so a clipboard manager's `CloseClipboard` closes ours and the
    next call fails with "Thread does not have a clipboard open". Spent
    attempts raise `ClipboardContentionError`. A close lost after a finished
    write is read back (`_confirm_write_landed`: counter first, then exact
    text), and a retried write never empties a clipboard someone wrote to
    since. A capture whose close was lost is read again
    (`reopen_on_lost_close`): its ctypes `GetClipboardData` answers NULL on a
    closed clipboard without raising, so a screenshot-only clipboard came
    back as an empty state and the restore wiped it. An open that fails on a
    later attempt raises the operation's `exhausted_error` (after our
    `EmptyClipboard`, `ClipboardEmptiedError`, so the transaction restores);
    a failed first open stays the plain error. Worst case on the Qt thread:
    about 0.33 s per operation, and one paste runs up to four (capture,
    write, the read-back after a lost close, the changed-after-set read), so
    about 1.3 s. This survives the race; an owner window would close it and
    is not built (see the rejected alternatives below).
  - *A transcript the transaction will restore, and the restore itself, stay
    out of Win+V and the cloud clipboard* (2026-10-01; the restore since
    2026-10-13). `set_clipboard_text(exclude_from_history=
    restore_clipboard)` sets ExcludeClipboardContentFromMonitorProcessing,
    CanIncludeInClipboardHistory and CanUploadToCloudClipboard (DWORD 0,
    `RegisterClipboardFormat`) inside the same open (Microsoft, "Clipboard
    Formats", section "Cloud Clipboard and Clipboard History Formats");
    `restore_clipboard_state` sets each of them the restored content does not
    already carry, inside its open, after the content, and only when it
    wrote something (an empty clipboard stays empty). Best-effort: a miss
    logs `clipboard_history_exclusion_partial op=write|restore failed=N` and
    never fails the paste or the restore. With `keep_transcript_in_clipboard`
    the transcript is an ordinary entry. The capture keeps the formats like
    any other, so a password manager's excluded copy comes back excluded
    with its own payload, never overwritten by ours.
    *The three formats are markers, never content* (2026-10-13 review):
    after one marked restore the next capture carries ours as formats, so
    `wrote_anything` and the "nothing written" error leave the marker ids
    out (a restore whose screenshot could not be set but whose 4-byte
    markers could passed as `restored`: no retry, no report), a clipboard
    of markers alone is restored as it was without a failure, and
    `_holds_only_our_restore` never takes a marker for its match (a
    password manager's secret marked CanInclude=0 / CanUpload=0 over our
    kept markers was emptied away by the reattempt); a marker holding our
    DWORD 0 or its captured payload, or one that cannot be read, is ours.
    Why the restore is marked (owner's report 2026-10-13: Win+V full of
    screenshots he did not take that often): Windows lists every write
    that stands for a moment, so every restore was one more entry of the
    user's copy -- 16 of 23 entries on his machine coincided with a logged
    restore to the second, 10 of them one screenshot, pushing real entries
    out of the 25-entry history. Windows skipped a restored text equal to
    the newest entry but listed a restored picture every time. The
    2026-10-01 rule "the restore never adds the formats" rested on the
    premise in its test's docstring (`55a380a`): marking would hide the
    user's own copy. It does not: the copy is listed from when the user made
    it. Measured on the real history: a plain picture copy is listed, an
    unmarked restore of it added one more Bitmap entry, a marked one none;
    content pasted from Win+V (`SetHistoryItemAsContent`: carries
    ClipboardHistoryItemId and Windows' own Exclude marker, which the
    restore keeps and to which it adds the two Can* formats) came back with
    no new entry and the history unchanged -- measured once by hand, not in
    part G; before, such a restore surfaced as the top entry (log
    correlation, 00:25:34). `scripts/release_check_clipboard_paste.py` part
    G repeats the plain-copy case (the transcript stands for
    `CLIPBOARD_RESTORE_DELAY_S` as in a real paste: a clipboard standing for
    milliseconds is not listed at all, so a back-to-back write would prove
    nothing about the transcript) and fails on an unmarked restore. Not
    measured: that a transcript kept with `keep_transcript_in_clipboard` is
    listed.
  - `TextInserter.paste_pace_remaining_s` is the time left until the last
    SendInput keystroke's restore window ends (0 after a WM_PASTE or before
    any paste); the controller paces queued pastes by it (below).
  - *The foreground is re-read immediately before `send_paste_with_mode`*
    (SendInput goes to the focus, not `target_hwnd`); a change from the
    snapshot raises retryable `TextInsertionError` and restores at once; an
    unreadable (None) foreground is no change. WM_PASTE skips it.
  - `wait_for_modifier_release` (bounded poll) runs before the clipboard is
    touched, or a hotkey's held Ctrl/Alt turns Ctrl+V into a no-op. WM_PASTE
    skips it.
  - *The modifier keys are read once more immediately before the keystroke*
    (2026-11-25, `_hold_the_keystroke_while_a_modifier_is_down`). The first
    wait ends before the write, the settle sleep and the check of the write,
    about 25 ms before the keystroke. A Ctrl+Alt pressed inside that gap --
    the record hotkey of the next dictation, and results arrive exactly
    while recordings are started and stopped -- made the target see
    Ctrl+Alt+V, which pastes nothing, under a transaction that logged
    `outcome=pasted`. A key that is down is waited for again, then the
    clipboard and the foreground are checked again (the key may have been
    the Ctrl of a Ctrl+C or the Alt of an Alt+Tab);
    `PASTE_MODIFIER_RECHECK_ROUNDS` (2) such waits, then a retryable
    `TextInsertionError` ("kept being pressed"). With no key down it costs
    one `GetAsyncKeyState` round and no sleep. WM_PASTE skips it. The
    owner's log (2026-10-24..11-24) shows first waits of 200-400 ms because
    a hotkey was still held when a result arrived (`press_to_handle_ms=219`
    next to one), so keys do move around pastes; no lost paste could be
    pinned on exactly this gap.
  - No Windows API says "the target inserted it". What exists is the read
    receipt below ("A restoring paste has a read receipt"): it proves that
    nobody fetched the transcript, never that an element took it. Rejected:
    a UI Automation read-back (heavy and per application); an owner window
    without a pump (blocks every other program's `EmptyClipboard`).
    `keep_transcript_in_clipboard` skips the restore. The target check
    below answers a narrower question afterwards: whether the focus showed
    a caret.
- **A restoring paste has a read receipt** (2026-11-25,
  `clipboard_owner.ClipboardOwner`, wired in `main.py`; a `TextInserter()`
  or `Win32ClipboardBackend()` without an owner writes plain data as before,
  which the test doubles and the release checks get).
  - *Mechanism.* `offer_clipboard_text` opens the clipboard for the owner's
    message-only window, empties it and sets `CF_UNICODETEXT` with a NULL
    handle (delayed rendering); the Win+V exclusion formats are data as
    before. Windows sends that window `WM_RENDERFORMAT` when a program calls
    `GetClipboardData`; the procedure answers with the transcript and
    records the time and the reader (`GetOpenClipboardWindow` -> pid ->
    image name). The window has a thread of its own because the request is
    a *sent* message: the reader blocks until the procedure returns, and a
    WM_PASTE sent from the Qt thread would wait for a request that thread
    cannot answer. `self._api` is set before `CreateWindowEx` (Windows calls
    the procedure from inside it; answered with an exception, the first real
    window was never created), and every window registers a class of its own
    (a class keeps the procedure it was registered with).
  - *Verdicts* (`PasteReceipt`): `unread` -- nobody fetched it; `read` --
    the first fetch came after the keystroke from a process of the paste's
    window (its own pid and its child windows'); `unknown` -- anything
    else: fetched before the keystroke, by another or an unidentifiable
    program, by this app's own read (`own_read`), or the transcript stays
    on the clipboard (`transcript_stays`). Once served the data is ordinary
    clipboard content and later reads are invisible. **Only `unread` changes
    what the user is told.**
  - *What it proves, measured 2026-11-25 with the shipped `TextInserter` on
    real windows* (scratch probes, not in the repository). EDIT window: 19
    of 20 pastes `read reason=target` a median 5 ms after the keystroke
    (max 8) and landed; the twentieth did not land and read `unread`. Edge
    and Chrome, page focused: textarea (20 + 20) and contenteditable
    (10 + 10) all landed, every receipt `unknown
    reason=unidentified_reader`, fetched a median 4-8 ms after the
    keystroke (max 42) -- Chromium opens the clipboard without naming a
    window, so its reader has no pid. Focus on a button and on the page
    body (6 + 6 each; the page saw every paste event): nothing was inserted
    and the text was fetched all the same, a median 4-5 ms after the
    keystroke (max 15). A page whose main thread was busy for 2.2 s:
    fetched 2-5 ms after the keystroke, and the field received the *old*
    clipboard text after the 1.5 s restore (Edge with the receipt and
    without; Chrome with it, the run ended before the plain one). So in
    Chrome and Edge something fetches the text on every Ctrl+V whatever has
    the focus, and the read that feeds the paste comes later, unseen.
    *Electron is different* (a throwaway VS Code instance, Electron 43.8.9 /
    Chromium 150, a textarea, a contenteditable and a button added to its
    workbench page): textarea 8 of 8 and contenteditable 5 of 5 landed with
    one fetch 7-39 ms after the keystroke (once 200 ms, right after the
    window came to the front); focus on the button and on the page body
    (5 + 5, the page saw every paste event): **no request at all**, so the
    receipt reads `unread`; a page busy for 2.2 s fetched the text after
    2.04 s and the field received the *transcript*, because the unfetched
    transcript waited (next item). Consequences: `read` and `unknown` never
    claim "inserted" and never overrule the target check; in an Electron
    window a paste that reached no text field is `unread` and reported; in
    Chrome and Edge the receipt catches only a keystroke that reached nobody
    (`docs/agents/known-limitations.md`). No false alarm: every one of the
    114 landed pastes had a fetch (19 EDIT, 60 + 22 browser, 13 Electron).
    The Claude desktop app is an Electron app and was not driven itself
    (it is the owner's running app); its `paste_receipt` lines will show
    whether it behaves like the VS Code window.
  - *Refusing the browser's first fetch was tried and is not shipped*
    (2026-11-25, scratch code). Edge: with the first request after the
    keystroke refused, 13 of 13 pastes landed through a second request 2-26
    ms later, button and body (5 + 5) raised no second request, and the busy
    page fetched after 2.06 s and received the transcript. Chrome: 13 of 13
    landed, but with the focus on the body a second request came 220-267 ms
    later all the same and the busy page still received the old text.
    Electron: the textarea received nothing in 8 of 8, because its one
    request is the paste itself (the contenteditable asked a second time
    within a millisecond and landed 5 of 5). A rule that helps one browser,
    half-helps another and breaks text fields in every Electron app is not
    one to ship. Why Chrome and Edge make the extra request was not
    established.
  - *`unread` is evidence, not a guess*: the transcript left the clipboard
    without any program having fetched it, so it is in no window, whatever
    the reason (focus on an element that takes no paste, a swallowed chord,
    an elevated window that never got the keystroke). The controller makes
    it a failed paste (below, "An unread receipt is a failed paste").
  - *An unfetched transcript waits.* When the restore is due
    (`CLIPBOARD_RESTORE_DELAY_S`, 1.5 s) and the receipt is still `unread`,
    the restore is put off until `PASTE_READ_RECEIPT_WAIT_S` (3 s) after the
    keystroke (`clipboard_restore outcome=unread_rescheduled`), and
    `paste_pace_remaining_s` lasts as long, so a queued paste does not
    replace a transcript whose reader is still to come. Not while the offer
    is no longer intact (the user copied something else). A target still
    busy at `CLIPBOARD_RESTORE_MAX_WAIT_S` keeps the transcript as before;
    its receipt is then `unknown reason=transcript_stays` and the offer is
    released.
  - *An announced transcript is never read back.* The pre-keystroke check,
    the take-over and the restore check ask `offer_intact` instead: the
    owner window still owns the clipboard and has had no
    `WM_DESTROYCLIPBOARD`. A read would be served by our own window and use
    the receipt up. Once that fails, what is there is someone else's and is
    read like any content.
  - *Clipboard sync programs are refused* (`CLIPBOARD_SYNC_PROGRAMS`:
    `msrdc.exe`). WSLg's `msrdc.exe` asked for the text 1-3 ms after every
    write on the owner's PC, before any keystroke; served, every receipt
    read "before the keystroke". A refused request leaves the format
    unrendered and the next reader is asked again (measured: the refused
    reader got no data, the next one all of it; `msrdc.exe` did not ask
    again within 4 s). Not refused while it is the paste's target (a Linux
    GUI window), and not for a released offer. Windows' own clipboard
    history did not ask within 3 s with the exclusion formats set. Any other
    manager that fetches first makes the receipt `unknown`: no evidence,
    never a false alarm.
  - *One report per paste* (`set_paste_receipt_handler`, `paste_receipt
    id= verdict= reason= reader= read_ms= refused= after_ms=`, never text):
    when the restore ran or found the clipboard changed, when the next paste
    replaced the transcript (asked after that paste's own write; an ended
    offer keeps its receipt, `_OFFERS_KEPT`), when the transcript stays, and
    at the shutdown flush. "Keep transcript in clipboard" writes plain data
    and has no receipt: that text must outlive the app. A WM_PASTE's offer
    is served but not reported.
  - *Cost* (measured): the announced write a median 1.29 ms against 1.16 ms
    plain (n=40); `offer_intact` 0.02 ms; `insert_text_with_options` a
    median 31 ms either way (EDIT, n=20). The target's fetch waits for the
    owner thread to get the interpreter: the text was in the EDIT a median
    12.7 ms after the keystroke with the receipt against 3.5 ms without (max
    18 against 4 ms).
  - *Risk taken.* While a transcript is announced (1.5-3 s per paste) any
    program that pastes waits in `GetClipboardData` until the owner thread
    answers; a process that cannot run Python for that long makes that
    program wait. At shutdown `TextInserter.close` (after the controller's
    flush) ends the window, and `WM_RENDERALLFORMATS` writes out a
    transcript it still owes. `main` registers the same `close` with
    `atexit`, for an exit that never reaches `aboutToQuit` (an exception
    out of `run`); the second call is a no-op. A killed process -- the
    quit watchdog's exit included -- runs neither, and the announced text
    is then gone from the clipboard (known limitation).
  - *Text.* The owner hands out `clipboard_text_payload(text)`: UTF-16
    with `surrogatepass`, so a lone surrogate goes out as its code unit
    like in the plain write. Encoded strictly it raised in the window
    procedure: the reader got nothing and the receipt read `read`.
- **The app never touches the clipboard through Qt from Python**
  (2026-11-25, review P2; `clipboard_copy.copy_text`, the one function for
  "copy this text"). Every Copy button, the tray's copy actions,
  "Keep transcript in clipboard" and the copy-on-error fallback call it; its
  writer is `TextInserter.copy_text`, installed once in `main.py`. Why:
  PySide keeps the interpreter lock for the whole of `QClipboard.setText`,
  and while a transcript is announced (entry above) that call's
  `EmptyClipboard` sends the owner window `WM_DESTROYCLIPBOARD`, whose
  procedure is a Python callback on another thread. Measured with
  `scripts/diagnose_clipboard_copy_stall.py`: the Qt write returned after
  5012 ms (the reviewer: 5004, 5006, 5017 ms; a `QClipboard.text()` in that
  state did not return within 40 s), `TextInserter.copy_text` after 1.1 ms
  during a live offer and 0.8 ms without one. The backend's Win32 calls
  release the lock, so the owner thread answers.
  - `TextInserter.copy_text` writes plain text and settles a paste whose
    restore is still pending: the clipboard captured before that paste is
    dropped instead of being put back, and the paste's receipt is reported
    as it stands. Before, a copy of the pasted transcript itself (tray,
    "Copy last transcript" right after a paste) was undone 1.5 s later: the
    restore found its own text on the clipboard and took it for its own
    write. A write that fails leaves the pending restore alone.
  - `copy_text` answers False instead of raising, and a caller shows
    "Copied" only for True (the clipboard can be held by another program).
  - Gate: `tests/test_clipboard_copy.py` parses every module of the package
    and fails on an attribute, name or import called `clipboard` or
    `QClipboard`. Qt's own C++ reads (a Ctrl+V in a text field of ours) are
    not Python-level and are covered by the own-window rule below.
- **A paste into a window of this app is written plainly, without a
  receipt** (2026-11-25, lead's decision after the review;
  `Win32ClipboardBackend.offer_clipboard_text`: the target window's
  process, or a child window's, is this one). Qt reads the clipboard on
  the Qt thread and the owner window answers from a Python callback on
  its own thread, so a field whose key or paste handling runs through a
  Python override (`keyPressEvent`, `event`, `insertFromMimeData`) holds
  the interpreter lock while it waits for that answer. Measured with
  `scripts/diagnose_own_window_paste.py`, one real Ctrl+V per field,
  announced (before) against plain (after), time until the field showed
  the text: QLineEdit 44 against 27 ms, QPlainTextEdit 68 against 70 ms,
  the Settings spin box 24 against 24 ms, the double spin box 24
  against 23 ms (one paste each, so 2 ms is noise); a QLineEdit with a
  Python `keyPressEvent` never showed
  it and the Qt thread stood still for 35.2 s, against 25 ms after. The
  app has no such field today; the rule is what keeps a future one from
  freezing it. Such a paste has no receipt (verdict unknown, as in
  v0.13.0); the own-window check above still refuses a focus that takes
  no text before the clipboard is touched.
- **No transcript is announced while an in-process ONNX runtime is in
  use** (2026-11-25, review P3; `TextInserter.set_offer_gate`,
  `DictationController._paste_may_be_announced`,
  `config.LOCAL_INTERPRETER_HOLDING_*_MODEL_SIZES`). The owner window hands
  the text out from a Python callback, so a program that fetches it waits
  as long as another thread keeps the interpreter lock in one piece. The
  reviewer measured 2316 ms for another program's `GetClipboardData`
  during one native call of 2.9 s. What the app's own work does, measured
  with `scripts/diagnose_interpreter_stall.py` (a pure-Python thread
  sleeping 1 ms; longest time between two wake-ups; 30 s of English
  speech on the CPU; other agents' test runs were active, so the figures
  are noisy upwards):

  | Work | Longest stall |
  | ---- | ------------- |
  | nothing (the ticker alone) | 2 ms |
  | faster-whisper tiny: load / transcribe | 62 ms / 18 ms |
  | Parakeet TDT 0.6B v3 (onnx-asr): load / transcribe | 1694 ms / 10 ms |
  | Granite 5.0 470M TurboCTC: load / transcribe | 1047 ms / 4 ms |
  | Nemotron 3.5 INT4 (ONNX Runtime GenAI): load / transcribe | 1916 ms / 827 ms |
  | Silero speech check: build the session | 33 ms |
  | Settings dialog: build and show / close | 225 ms / 4 ms |

  So creating an ONNX session keeps the lock (ONNX Runtime releases it in
  `run`, not in the constructor) and Nemotron's decoding does too. Rule:
  a runtime lease for onnx-asr, Granite CTC or Nemotron is tracked from
  acquisition to release, whether a preload (the post-resume one
  included), a batch job or a stream holds it. While such a lease's model
  is not loaded yet (`is_model_loaded`; the load is lazy and may start at
  any moment) -- and for Nemotron while the lease lives at all -- a paste
  is the plain write with no receipt (`paste_receipt_skipped
  reason=interpreter_busy`). A loaded Parakeet, Canary or Granite CTC
  model decoding the next queued recording does not cost the paste of
  the previous result its receipt. faster-whisper, the Node runner
  (another process)
  and the remote engines (they wait on a socket; not measured) keep the
  receipt. Not covered, accepted: a load that *begins* in the 1.5-3 s
  after an announced paste, and work on the Qt thread in that window
  (opening Settings), delay a program that fetches the text late by the
  figures above; the targets measured fetched within 18 ms of the
  keystroke (EDIT) and a median 4-8 ms (Chrome, Edge).
- **The clipboard is put back with every format it held** (F12;
  `Win32ClipboardBackend.capture_clipboard_state` /
  `restore_clipboard_state`). `ClipboardState.formats` = `(format id,
  registered name or "", raw bytes)` in enumeration order; `has_text` /
  `text` unchanged. Why: text-only restore flattened screenshots, file
  selections and HTML (user's clipboard: 1 of 9 formats came back).
  - Capture (one open): one `EnumClipboardFormats` pass first; skip
    `CF_BITMAP`, `CF_METAFILEPICT`, `CF_PALETTE`, `CF_ENHMETAFILE`,
    `CF_OWNERDISPLAY`, the three `CF_DSP*` handle formats, `0x0200-0x02FF`,
    `0x0300-0x03FF`, and `DataObject` / `Ole Private Data` by name; copy
    via `user32.GetClipboardData` -> `kernel32.GlobalSize` -> `GlobalLock`
    -> `ctypes.string_at` -> `GlobalUnlock`, never pywin32 (it decodes).
    Unreadable formats are skipped (`clipboard_capture_unreadable
    formats=<n>`, INFO); over `CLIPBOARD_CAPTURE_MAX_FORMAT_BYTES` (128 MiB)
    or `CLIPBOARD_CAPTURE_MAX_TOTAL_BYTES` (256 MiB) falls back to text
    only (`clipboard_capture_truncated formats=<n> bytes=<total>`, WARNING).
    A failed enumeration returns what it has, never raises.
  - Restore (one open): empty, set each format in a fresh
    `GlobalAlloc(GMEM_MOVEABLE)` block (freed only if `SetClipboardData`
    refuses), re-register a name whose id changed, count refusals
    (`clipboard_restore_partial failed=<n>`, WARNING) and continue; write
    text separately only if `CF_UNICODETEXT` was not set.
  - Evidence: tests drive the real backend against fake Win32 modules with
    real ctypes buffers; `scripts/release_check_clipboard_paste.py` checked
    the real clipboard (2026-09-18: text, HTML, `CF_DIB`, `CF_HDROP` came
    back byte-identical; Explorer cut/copy still worked). Capture costs
    24 ms at the 256 MiB cap, on the Qt thread.
- **Never assume a Win32 timeout throttles a loop.** `SMTO_ABORTIFHUNG`
  makes `SendMessageTimeoutW` return at once for a hung target (953,446
  probes per budget), so `wait_for_paste_target_ready` polls at
  `PASTE_TARGET_RESPONSIVE_POLL_INTERVAL_S` and stops for a handle that is
  no longer a window.
- **A short `SendInput` past the key-down may already have pasted.**
  `_send_input_batch` takes `committed_after` and raises
  `TextMayHaveBeenPastedError` once that many of `[Ctrl down, V down, V up,
  Ctrl up]` went out, so `send_paste_with_mode`'s auto path never falls
  through to `WM_PASTE` for a second paste. Every arm seeing it leaves the
  clipboard alone, and a re-raise carries `allow_clipboard_fallback` across
  (inside `insert_text` a post-keystroke failure becomes a constructed
  `ClipboardContentionError`; a fresh exception defaulted to permissive).
- **WM_PASTE is never sent to a Chromium window** (2026-10-03,
  `window_focus.CHROMIUM_WINDOW_CLASSES`: `Chrome_WidgetWin_1`,
  `Chrome_RenderWidgetHostHWND`). `_send_wm_paste` raises
  `_WmPasteIgnoredError` before sending: in `wm_paste` mode that is the
  paste's error, plus the advice to choose Auto; in `auto` mode after a
  failed SendInput it is folded into "Auto paste failed" with the reason
  only, since that user is in Auto already. Streaming live inserts that
  keep failing end in "Streaming aborted: the target window kept rejecting
  inserted text." followed by the last failure's reason, so the wm_paste
  advice reaches the screen there too. Both are
  pre-keystroke failures: the clipboard is put back and Insert is offered.
  Why: into an Edge --app textarea `SendMessageTimeout(WM_PASTE)` succeeded,
  the page saw no paste event and nothing landed, so the transaction
  reported a paste that never happened (r27 B2, 2026-09-27; re-measured
  2026-10-03 for the top-level window and its render-widget child alike;
  Electron apps are the same window class). Reporting it as "may not have
  been inserted" was rejected: that withholds Insert, while the text
  demonstrably did not land.
- **Deferred queue inserts are coalesced**: `_flush_deferred_background_results`
  groups token-ordered results by captured target, one paste per group
  (each paste is a clipboard race window). Only this flush joins texts,
  with `_join_transcripts` (one space); every other insert keeps its text
  exactly -- never add whitespace across separate pastes.
- **Queued pastes are paced by the restore window** (2026-10-01). An insert
  that would start within `CLIPBOARD_RESTORE_DELAY_S` of the previous
  SendInput keystroke is held (`paste_paced`) and the single-shot
  `_paste_pace_timer` flushes at keystroke + delay, so everything held goes
  out as one coalesced paste per target (`paste_paced wait_ms=... joined=N`).
  Why: a second paste inside the window overwrote a clipboard the target may
  not have read yet. The pace is global (one clipboard); grouping stays per
  target. A foreground batch result joins the deferred group of its own
  target (`hold_key`) or goes through the queue while the window is open
  (`_deliver_foreground_through_paste_queue`: Done, history and the
  last-recording mark at once, the paste after). Streaming live inserts and
  the finalize tail are exempt, except the first live insert of a dictation,
  which waits for the window (`_stream_live_insert_held`), and a streaming
  result none of which was inserted live, which is pasted like a batch
  result (2026-10-09; order entry below). Never sleep on the Qt thread for
  it.
  A result only the pace holds (`pace_held`) is not listed in the queue
  panel and Clear queue or Cancel cannot drop it: it is pasted within the
  delay, and listed it flashed the panel open and shut while the overlay
  already showed it as Done. A recording or a window that defers it again
  lists it as "Pending insert" as before. Re-paste and the overlay's Insert
  go through the pace too (below).
- **Results for one window are inserted in recording order** (owner's rule
  2026-10-09; for different windows order does not matter, and nothing for
  another window waits). "Recording order" is submission (token) order:
  recordings never overlap, and a retry is ordered as the new submission
  it is -- its recording failed, which released everything behind it.
  "One window" is the top-level handle (`_same_order_window`), every
  result with `insert_target=current_window`. Changed: a streaming
  dictation held its live inserts for nothing, so an earlier batch result
  for its window still transcribing or held by a recording landed after or
  inside the streamed words (the finalize's flush, or the pace holding it
  while the tail pasted at once). Now the stream's live inserts wait while
  `_earlier_result_waits_for` its window (a batch result, or an earlier
  stream's result held in the paste queue); such a result that is done may
  paste during the capture while nothing of the stream is in the document
  and its window is in front (`_stream_lets_earlier_result_go_first`; the
  partial handler retries the flush); the first live insert waits for that
  paste's restore window; and a stream with nothing inserted goes through
  the paste queue at its finalize, behind a held earlier result. Already
  held, unchanged: the deferred queue (token-ordered, coalesced per target,
  one deferral answer for every result of a window; a group held by the
  pace holds the later groups too), the single FIFO executor for batch and
  local finalizes, the remote finalize lane's `_has_undelivered_older_job`,
  a foreground result joining its window's deferred group (`hold_key`),
  and F10's join of failed rows, which are recorded in failure order and
  so in token order per window. An F10 or Insert on a failed row may land
  after a later result for that window: the failure released the order,
  and the re-paste is a new request at the current caret.
- **`immediate_background_insert` (default off)**: a finished queued result
  inserts into its captured window at once, even during another
  transcription or a **batch** recording (safe because of the
  modifier-release wait), through the deferred queue, so the pace applies.
  Never during a streaming capture or a recording
  start/stop. Decided per job (`_can_insert_during_active_recording`). UI:
  `insert_immediate` in `_CONCURRENT_MODE_UI_CHOICES` ("While busy" combo);
  stored as `concurrent_transcription_mode` + `immediate_background_insert`.
- **`insert_target`**: `recording_window` (default, snapshot at recording
  start) or `current_window` (focus when ready; flushes coalesce to one
  paste). The caret is always the one at insert time.
- **`get_foreground_window` never answers with one of our own windows**
  (`None`, reported by the insert path). The old `or hwnd` fallback returned
  the tray's hidden host window, which `restore_target_window`'s
  `ShowWindow(SW_SHOW)` then made visible and cached. `main.on_tray_activated`
  calls `note_foreground_window()` first (before the menu takes focus).
- **Our own popups are never a target**: `Win32WindowFocusHelper` returns
  the last foreign foreground window while our `WS_EX_TOOLWINDOW` windows
  (tray menu, overlay) are in front; Settings stays a valid target.
- **A window of this app takes a paste only in a text field** (2026-11-25,
  `own_window_focus.own_window_takes_pasted_text`, asked in
  `_insert_text_at_target` before the clipboard is touched). The owner's
  log, 2026-11-24 22:48: a dictation whose target was the Settings window
  (`Qt6120QWindowIcon`) logged `outcome=pasted`, its target check answered
  `unknown`, the overlay said Done, and the same text was pasted by hand
  into another window 12 s later. For our own windows nothing has to be
  guessed: Qt knows the widget the window gives its focus to
  (`QWidget.find(hwnd).window().focusWidget()`, which does not depend on Qt
  having processed the activation). An enabled, writable `QLineEdit`,
  `QTextEdit`, `QPlainTextEdit` or spin box and an editable combo box take
  text; everything else does not, and a `QKeySequenceEdit` never does (it
  would record Ctrl+V as the new hotkey). "Does not" raises
  `TextInsertionError` -- the ordinary "Not inserted" report with Insert, a
  row and history, no clipboard write and no keystroke. A foreign window
  answers None and nothing changes.
- **Cancel, Retry and Insert share one fixed-size overlay slot.** A
  successful transcription whose insert failed uses
  `error_action=OVERLAY_ERROR_ACTION_INSERT` (`insert_again_requested` ->
  `insert_failed_text`) and is painted as the burnt-orange "Not inserted"
  warning, not the red Error (below, `docs/agents/overlay.md`). An Error
  whose failure kept no audio of its
  own uses `OVERLAY_ERROR_ACTION_NONE` (a Retry would transcribe the previous
  failure's audio); the tray's "Retry transcription" still names the slot.
- **A queued transcript that failed to paste is never silent**: it emits
  `background_insertion_failed` (tray, always); with nothing newer on the
  overlay it shows the "Not inserted" warning (an Error state with
  `OVERLAY_ERROR_ACTION_INSERT`) and takes over `_last_transcript`. `_foreground_delivery_pending` suppresses that inside
  `_on_transcription_ready` just before the foreground result paints.
- **A failed queued paste stays listed until inserted or dismissed**
  (owner's field report 2026-10-01: six queued pastes, two silently
  missing). Each failed or possibly-landed queued or foreground batch paste
  becomes an `_UndeliveredInsert` row (negative id) in the overlay queue
  panel, sent as `QUEUE_ROW_KIND_UNDELIVERED`: "Not inserted", "Not in a
  text field" (the target check below) or "Possibly
  inserted, check the window", amber, with a Dismiss button (both row
  captions share one reserved width) and a title that counts transcriptions
  and waiting inserts apart. Later results never replace a row; Dismiss
  (`queue_cancel_requested` with the row id) and Clear queue drop it; a
  successful paste built from the row retires it -- by identity, never by
  text (`_retire_undelivered(entries)`): two dictations of "okay." are two
  rows, and the overlay's Insert on one retired both (2026-10-01 review).
  The offer knows the rows its text was built from (`_insert_action_rows`):
  set by the two paths that record a failure and paint the offer, and by
  `_repaste` when its failure paints the offer again -- a failed re-paste of
  waiting rows or a failed Insert hands it the same rows, so the next
  successful Insert retires exactly them (before, it carried none, the row
  stayed listed and F10 pasted the text a second time; 2026-10-01 review).
  The tray report appends
  `_undelivered_hint`: the count and how to insert them -- the re-paste
  hotkey only while registered, else the tray's "Insert transcript again".
  The overlay's amber badge counts every insertable row and names the
  re-paste hotkey (`_update_not_inserted_badge`, run by every
  `_update_queue_overlay`, every hotkey registration -- save, startup,
  resume -- and a reload without one; owner's request
  2026-10-09; `docs/agents/overlay.md`); a "possibly inserted" row is not
  counted, and an Insert offer without a row (a streaming tail, a failed
  re-paste of a text that has no row) is not either -- it is retired when
  the next recording starts, so a count that included it would drop at
  that moment (Known limitations: the tail row).
  A transcript the startup notice of unfinished recordings saved to history
  becomes such a row too (`_list_unfinished_transcript`, 2026-10-09, owner's
  idea): never pasted on its own, pasted by the re-paste at the current
  caret like any failed row, counted by the badge, and listed with the
  recording's time as "From earlier session" (`_UndeliveredInsert.
  from_earlier_session`; owner's decision 2026-10-17 -- it read "Not
  inserted" although no insert was ever attempted). A status that the row
  earns later wins: a failed re-paste whose keystroke went out marks it
  "Possibly inserted, check the window" as any other row.
- **A paste that reports success has its target checked, report-only**
  (2026-10-03, `paste_target_check.PasteTargetCheck`, wired in `main.py`;
  the controller's `paste_target_check` argument defaults to None = no
  check, which every test that does not pass one gets). After a foreground
  batch paste, a queued paste and a re-paste (not streaming live inserts or
  the finalize tail) `_check_paste_target` hands a `_PasteCheck` to the
  worker thread (MTA COM, one check at a time; a second request while one
  runs is refused, never queued) and returns; the answer comes back through
  `paste_target_checked`, or `PASTE_TARGET_CHECK_TIMEOUT_MS` (1 s) answers
  "unknown". Verdicts: a Win32 caret (`GetGUIThreadInfo.hwndCaret`) in any
  window is a text field; in a Chromium window (`Chrome_WidgetWin_1`,
  `Chrome_RenderWidgetHostHWND`) MSAA `OBJID_CARET` on the focus window
  invisible or 0 wide is "not a text field", read again after
  `PASTE_TARGET_CHECK_RECHECK_DELAYS_S` (0.1, 0.25 s) and standing only when
  every reading on the same foreground agrees; everything else -- other
  window classes, an MSAA error, a changed foreground, a refused or
  timed-out check -- is "unknown", which behaves exactly as before. "Not a
  text field": no completion tone; an insertable `_UndeliveredInsert` row
  with `outside_text_field`; the tray (`background_insertion_failed`) for
  a queued paste or when the overlay moved on; on the overlay, while it
  still shows what the paste painted (`_PasteCheck.overlay_shown`: only the
  paste's own "Done" or Idle, never a session and never another job's Done
  or Error -- a queued paste paints nothing, and painting over a failed
  paste's or a streaming tail's offer took that Insert away, 2026-10-03
  review), an Error "pasted, but the focused element does not look
  like a text field. If nothing appeared, click into the field and press
  Insert" with the Insert offer, painted as the "Not inserted" warning
  (`_paint_insert_offer`, shared with the failed queued paste). A re-paste or Insert that then pastes it retires
  the row by identity like any other (owner's rule 2026-10-03: a failed or
  doubtful insert is shown briefly and stops being listed once a re-paste
  inserted exactly it); if that paste is doubtful again, it gets a new row.
  F10 (`_repaste_rows`) joins only the failed rows; a doubtful row is pasted
  only when no failed row waits, and the paste of a later dictation whose
  keystroke goes out (any `_insert_text_at_target` that succeeded or may
  have landed; "later" as defined below) drops it
  (`_drop_superseded_doubtful_rows`; a keystroke
  that went out and a cleanup that failed supersedes it as much as a clean
  paste, 2026-10-03, else F10 pasted the stale text next to it; the rows
  that paste is built from stay, `keep_rows`) -- except a row a paced re-paste
  (`_pending_repaste`) names, which the queued paste the pace lets go first
  used to drop, so the F10 pasted nothing and said nothing; a paced
  re-paste whose rows are gone now says so on the overlay or tray
  (2026-10-03 second review); a verdict arriving after the paste of a
  later dictation lists no row and goes to the tray. *Later*
  (2026-11-25, lead's decision): the pasted dictation's recording started
  after the doubtful paste went out (`_TranscriptionJob.recorded_after_paste`
  against the row's `doubt_serial`, both counts of `_paste_serial`; a
  joined paste counts as its newest job), or the paste is made now (a
  re-paste, an Insert, live streaming text). The count is taken when
  the recording starts, not when its job is submitted at the stop: a
  recording that was running while the doubt arose began before it, and
  the overlay showed Listening, so the report was most likely not seen
  (second review, 2026-11-25; a Retry has no recording of its own and
  counts from its submission). A result that was already recorded when
  the doubt arose supersedes nothing: with several results
  queued the next paste went out within a second and took the row with
  it, or kept it from being listed at all (the owner, 2026-11-24: "the
  not-inserted notice vanished when several results were queued").
  Several doubtful rows can therefore wait, and F10 pastes them joined in
  order when no failed row waits. Why a later dictation still drops them: a false verdict leaves a row for
  text that landed, and F10 joined it to the next failed paste, pasting the
  landed text again (2026-10-03 review); a window that always reads
  doubtful would also collect a row per dictation. A later "text field"
  verdict in the same window was rejected as proof of a false one: clicking
  into the field after pasting onto a button is the case the report exists
  for, and in an always-doubtful window it never comes. A new paste of the
  same text into the same window drops a check still waiting for it once
  its own check has started (the earlier verdict was read before the
  repeat). A re-paste whose own check is refused -- the earlier one still
  holds the worker -- plays no tone (`tone_if_refused`; a check that
  raised is not a refusal and plays it) and leaves the
  earlier check in place, which then decides: "text field" plays one tone,
  "not a text field" goes to the tray without a row, since the re-paste
  superseded it. Before (2026-10-03 reviews), the refused re-paste played
  the tone as confirmed, and dropping the earlier check as well left
  neither paste with a verdict. Either way two pastes of the same text into
  one window inside one check window get one verdict and at most one tone.
  Why: a SendInput Ctrl+V into a focused button, list item or page body of
  a Chromium page reported success and inserted nothing (r27 B1). Measured
  on 2026-10-03 with the production reader against real windows (four
  runs; `scripts/release_check_clipboard_paste.py` part F repeats the EDIT
  and Edge cases): Win32 EDIT, read-only EDIT, RichEdit 5.0 text field
  (0.05-0.5 ms first reading); Edge input, textarea, contenteditable, an
  EditContext element that reports its selection bounds, and Edge's own
  address and find bars text field; Edge
  read-only input, button, focusable list item, body "not a text field"
  (2-4 ms warm, 15-33 ms for the first reading in the process); each stable
  over 30 readings in 1.5 s (a bare EditContext element was not: Known
  limitations). A textarea's caret answered 6-29 ms after Edge was brought
  to the front (two runs; the foreground lock refused the others), but at
  least one of 143 readings in the first run said "no caret" at a moment
  the probe did not record: the rechecks keep a short-lived one from deciding.
  Windows Terminal draws its own cursor and answers MSAA with an invisible
  caret of width 0 while its prompt takes the paste, which is why "no
  caret" counts only in Chromium windows; it and the Win32 BUTTON are
  "unknown". UI Automation is not used: a fresh Edge's first answer named
  the wrong element, it calls a contenteditable a Group and the body an
  editable Document, and a UIA client may switch an Electron app into its
  screen-reader mode.
  - **Every check logs one INFO line, and a script reads any window**
    (2026-10-03): `paste_target_check id= verdict= window_class=
    focus_class= gui_caret= msaa_caret= width= rechecks= [note=]
    background= chars=` (`CaretReading.evidence()`: class names and caret
    answers, never a title or text; `note=` names why there is no reading,
    e.g. `not_started`, `timeout`, `foreground_changed`).
    `scripts/diagnose_paste_target.py` counts down 5 s, then runs the
    production `PasteTargetCheck` 10 times against whatever has the focus
    and prints the same fields. Why: a review's read-only probe got "not a
    text field" 20 of 20 times on a foreground `Chrome_WidgetWin_1` window,
    most likely the Claude desktop app the owner dictates into, with the
    focus location unknown; real use and the script now produce the data
    to tell which Electron prompts misread. Sample: an Edge --app textarea
    `text_field ... msaa_caret=visible width=1 rechecks=0` in 2-4 ms (18 ms
    first), its button `not_text_field ... msaa_caret=invisible width=0
    rechecks=2` in about 360 ms.
- **An unread receipt is a failed paste** (2026-11-25). `_paste_check`
  records the inserter's transaction id (`_PasteCheck.receipt_id`,
  `TextInserter.pending_receipt_id`); the paste stays in `_paste_checks`
  after the target check answered (`caret_verdict`), or without any check,
  until `paste_receipt_ready` reaches `_on_paste_receipt`. That connection
  is queued even on the Qt thread: the receipt of a paste the next one
  replaced is emitted inside that next paste's call. `unread` ->
  `_report_paste_never_fetched`: a *failed* row, not a doubtful one, so it
  stays listed whatever is pasted afterwards and the re-paste joins it;
  the overlay shows it with Insert while it still shows that paste's own
  Done, otherwise (and for every queued paste) the tray carries it; and it
  is reported even when a later paste went out first. Its keystroke went
  out and it is offered again all the same: a paste whose keystroke may
  have gone out is never re-offered because its text may be in the
  document, and a transcript nobody fetched is in none. Nothing pastes it
  by itself; the row waits for Insert or the re-paste hotkey. A row the
  target check had already listed for the same paste becomes that failed
  row without a second report. `read` and `unknown` change nothing (see the
  receipt entry: a fetched transcript says nothing about the element). A
  re-paste drops waiting receipts of the same text
  (`_drop_receipts_of_repeated_text`): the user is dealing with it. The
  same words dictated again into the same window drop an earlier target
  check only while it still runs (`caret_verdict is None`); an entry that
  waits for its receipt alone stays, because that receipt is about the
  earlier paste's own clipboard write. The
  completion tone keeps its timing (it follows the target check), so an
  `unread` report can come up to 3 s after a tone. `quit_pending_work`
  counts only running target checks as `paste_settling`; the pace covers
  the receipt wait. Streaming live inserts and the finalize tail get a
  `paste_receipt` log line and no report.
- **The overlay's Insert pastes the text that failed** (a streaming
  finalize's tail past `committed_text`, not all of `_last_transcript`).
  `_insert_action_text` is written by both paths that paint the Insert action
  (`_insert_text_at_target`'s error arm and
  `_report_background_insertion_failure`), read by `insert_failed_text`
  (wired in `main._connect_overlay_actions`), cleared by a successful
  re-paste and at recording start, falling back to `_last_transcript`.
  Tray/hotkey keep `repaste_last_transcript`.
  - **One writer paints every offer** (owner's decision 2026-10-17): a
    transcript that exists but is not (surely) in its window is a warning,
    not the red Error, so `_paint_not_inserted` is the only call that paints
    it, from four call sites: `_paint_insert_offer`, `_paint_status_keeping_offer`
    (one call; its wordings are "Still not inserted:" and "Possibly inserted
    already") and the two arms of the failed paste in
    `_insert_text_at_target`. It takes the offer's own
    `may_have_pasted` and derives both the label word and the action from
    it -- "Not inserted" with Insert, or "Check window" without -- so the
    two cannot disagree; the state stays Error for every control and check
    (`docs/agents/overlay.md`). A failed transcription (foreground or
    queued) and a microphone failure are still the red Error; so is an
    aborted streaming session, although its partial text is in history --
    it is a failed session, not an insert that did not land (open question
    to the owner, 2026-10-17) --, and so is a refusal painted while no offer
    is pending. **Severity wins** (lead's decision 2026-10-17): a real
    failure that arrives while an offer is pending -- a hotkey that did not
    register, a failed model preload, a setting that was not saved, a refused
    recording start -- repaints the offer as the red Error
    (`_paint_status_keeping_offer` passes `failure=True`, `warning=None`),
    with the same "Still not inserted" detail, transcript and Insert: the
    orange hid that something failed, and the red costs the transcript
    nothing. Informational lines and the insert path's own errors
    (`show_overlay_error(..., about_insert=True)`) keep the orange; a new
    error raised from the insert or re-paste path must pass it. A new writer
    of an Insert offer goes through the helper; `FakeOverlay` refuses one
    that does not, and one that passes `warning=None` itself fails
    `test_only_the_one_writer_names_the_insert_action`, which allows
    `OVERLAY_ERROR_ACTION_INSERT` in `_paint_not_inserted` alone. Another
    job's failure on an idle overlay (`_report_background_failure`) is a
    real failure too: with an offer pending it is painted over it as above,
    Insert taking the action slot from Retry, and its text names the tray's
    Retry transcription.
  - **A refused recording start keeps the offer**: the clear sits past every
    refusal; `_refuse_recording_start` repaints it ("Still not inserted:")
    with `copy_text`/`error_action` on the offer (the stripped transcript).
  - **Every non-session status writer paints through
    `_paint_status_keeping_offer`**: `show_idle_status` lines, "Nothing to
    cancel.", the "Transcription canceled." writers, the preload's lines,
    `show_overlay_notice`, Edit/Retry/re-paste refusals and the Edit
    confirmation. Session states stay plain. While an offer is pending Copy
    yields it; Edit is enabled on it when it edits the offer (next entry).
  - **An edit reaches a result that was not inserted** (owner's rule
    2026-10-09). A result is normally inserted right after it is
    transcribed; only while it was not -- the Insert offer, a waiting-insert
    row, a result still held in the paste queue -- can an edit change what
    is pasted, and then it does: `_follow_transcript_edit` rewrites the
    rows whose `parts` hold the edited entry (a coalesced row keeps one
    `(entry, text)` part per result and rejoins them), the queued result's
    text, `_delivered_after_shown`, the shown pair (Copy, Edit, the
    re-paste fallback; a shown coalesced row's joined text, which has no
    entry, takes the rejoined row text so Copy yields the edit, while Edit
    still refuses there) and the offer -- rejoined from its rows, or for a
    row-less offer `streaming_text.retarget_tail` against its own entry
    (`_insert_action_entry`: the dictation whose streaming tail it is, or
    the one a failed re-paste pasted whole): the words in front of the tail
    are in the document, so an edit can only move the tail; an edit that
    removes it retires the offer, one that changes those words keeps the
    offer and says so. Matched against the shown transcript instead, a
    failed F10 of another dictation's "okay." counted as the tail of the
    shown "Alles okay.", and its edit "Alles gut." made Insert paste
    " gut." (2026-10-09 second review). Entries match by value, as `update_entry` does. Both entry
    points: the overlay's Edit (`edit_last_transcript`), enabled on an
    Error when `_edit_reaches` its text (the shown pair has an entry, the
    text is it or its tail, and when the text is built from listed rows the
    shown entry is one of theirs, and without rows the offer's own entry
    is the shown one -- by text alone a failed F10 of another dictation's
    equal row enabled an Edit that went to the shown entry;
    `OverlayUI.set_state(editable=)`), and the
    two history editors through `history_ui_actions.notify_history_edit`
    -> `on_history_entry_edited`, which repaints only an overlay that still
    shows the old offer or transcript and never a session. Nothing pastes
    because of an edit: a "possibly inserted" row takes the edited text for
    its label and stays unpastable, and the pair is written past the
    `_last_transcript` setter, which forgot `_shown_transcript_row` and
    `_shown_transcript_token` -- with Edit enabled on a possibly-inserted
    result, the re-paste fallback would then paste it a second time (a test
    calling Edit there measured it). A paced re-paste rebuilds its text
    from its rows when it runs, F10's rows and an Insert's rows alike (an
    Insert of a row-less offer keeps its text: Known limitations). A paste
    whose target check still runs (`_paste_checks`) takes the edit as well, so a
    "not a text field" verdict lists the edited text (2026-10-09 review;
    built from the check's copy, that row kept the old text for good); a
    re-paste's check keeps each pasted row's `(entry, text)` parts, so the
    row a joined F10 leaves behind is reachable too.
  - **The offer carries its own action**: after a post-keystroke failure (six
    `TextMayHaveBeenPastedError` raise sites in `text_inserter.py`, two via
    the `combined_error` alias and `_ClipboardContentionAfterPaste`) Insert
    is withheld, decided by the offer's own `_insert_offer_may_have_pasted`,
    never the per-attempt `_last_insert_may_have_pasted` a later paste in the
    same flush resets.
  - **A paste that carries the offer marks it** (`_repaste_carries_offer`,
    asked only by `_repaste`, before the paste, and handed to the insert as
    `carries_offer`). Carried means the offer's own dictation was pasted: a
    paste of waiting rows carries an offer built from rows when every one of
    its rows still listed is among them, by identity, and never a row-less
    offer (a streaming tail); otherwise the offer's own text
    (whitespace-folded equal) carries it, and for a row-less offer so does
    the tray's re-paste of the shown transcript it is the tail of
    (`streaming_text.tail_prefix`: ends with it at a word boundary, a
    punctuation tail included; a substring test marked "Wochenende" for
    " ende"). A re-paste retires only an offer it carried. Why identity
    (2026-10-09 review): matched by text, F10 on a failed "Ich komme
    morgen." row carried a streaming tail "." or " morgen." and retired it,
    and F10 on rows "B C" did not carry B's own offer, whose Insert then
    pasted B a second time.
  - **Clear retires the offer it dismissed** (`OverlayUI.detail_cleared` ->
    `on_overlay_detail_cleared`), not one hidden behind a later Error.
  - **The preload progress poll** repaints only the painter's own
    informational Error (`_offer_painted` = `(state, detail)` vs
    `OverlayUI.state` / `OverlayUI.detail`, recorded for the orange offer and
    not for a red failure painted over it, which stays until something
    replaces it; a preload start line is informational and is refreshed),
    skips a foreground transcription in flight
    (`_overlay_session_active`), and leaves the offer and its own progress
    line alone while `OverlayUI.detail_is_being_read`. With an offer pending,
    all five progress lines name the cancel hotkey (`_preload_abort_hint`)
    or the tray's `TRAY_CANCEL_ACTION_LABEL`.
  - **The detail rest position survives a relayout**
    (`OverlayUI._on_detail_range_changed`; `batched_update` defers geometry
    and Qt clamped a stale maximum): `rangeChanged` re-asserts rest or user
    position, `actionTriggered` (never fired by `setValue`) records
    `sliderPosition()`, back at rest releases the hold, a paint wins.
- **Re-paste**: `controller.repaste_last_transcript` (tray
  `TRAY_REPASTE_ACTION_LABEL` "Insert transcript again"; optional
  `repaste_hotkey`, default empty) pastes into the focus via
  `_insert_text_at_target`, in this order: every insertable waiting row,
  joined as one paste; else the last background-delivered text
  (`_delivered_after_shown`); else `_last_transcript`. No history entry.
  It runs while a foreground transcription is in flight (2026-10-01, was
  refused since wave 13) and then reports through the tray, never over the
  overlay. It also runs during an open batch capture (owner's decision,
  2026-10-01): through the pace, into the current focus with its own target,
  leaving the recording's target snapshot and its overlay session alone, and
  reporting through the tray. Refused with the reason only during a
  recording start or stop (the start or stop takes the target snapshot, and
  a paste then races it; a fraction of a second). During a streaming
  recording or its pending finalize (owner's request 2026-10-09; it was
  refused, and the user had to press it again afterwards) the paste's
  window decides (`_streaming_window_has_focus`, top-level window as the
  order rule counts it; unknown counts as the stream's window, and with
  `current_window` insertion every window is): into another window it goes
  out at once -- the stream writes only into its own window, a focus change
  suspends its live inserts --, and the stream's next live insert then
  waits for that paste's restore window (`_stream_waits_for_paste_pace`);
  into the stream's own window, where live inserts write at the caret and
  the finalize still pastes its tail, it is held as a `_PendingRepaste`
  with `after_stream` (a later request replaces it), the tray says it will
  be inserted when the stream has finished, a pace tick during the stream
  leaves it held, and the stream's end starts the pace timer
  (`_release_stream_held_repaste`, from `_reset_streaming_state` and from
  `_finish_transcription_job`): a runtime failure that leaves the finalize
  in flight (`keep_session_text=True`, `_stream_kept_finalize_token`) is not
  the end -- that finalize still pastes the tail, so the hold lasts until
  its job ends (`_stream_still_delivering`). It then runs after the stream's end, rebuilt from its rows like any held
  re-paste, at whatever has the focus then. A failed or doubtful insert still shows briefly in the tray and
  stays as a row; a re-paste that inserts that row retires it.
  A failed re-paste whose keystroke went out marks its rows possibly
  inserted, never pasted again. The `_last_transcript` fallback
  (`_repaste_last_unless_possibly_inserted`) refuses, with the reason shown,
  only when the shown transcript's own row is possibly inserted
  (`_shown_transcript_row`, by identity): another dictation with the same
  text is a different paste. Nothing pastes a possibly-inserted row, so
  only Dismiss or Clear queue take it away. The no-window refusal
  reveals the overlay once (`show_overlay_error` reveals only when it
  paints). The hotkey may not equal the recording, cancel or overlay one.
  Inside the previous paste's restore window a re-paste or the overlay's
  Insert is held as one `_PendingRepaste` (a later request replaces it) and
  `_on_paste_pace_timeout` runs it after the held results, re-checking the
  refusals and dropping rows dismissed meanwhile (`repaste_paced`). The
  shown transcript while its own job is still in the paste queue
  (`_shown_transcript_token`, by identity) is not pasted by the fallback --
  it was pasted twice, once by F10 and once by the timer -- and the tray
  says it is about to be inserted (`repaste_skipped reason=queued`). Rows
  and the Insert offer are never skipped for a held result with the same
  text: those are other dictations.
- **`retry_last_transcription` refuses during a recording start, stop or open
  capture** (via the tray), since it paints Processing and takes the
  overlay. A pending streaming finalize is refused too, with its own text
  ("The streaming transcript is still finalizing. Retry the last failed
  transcription when it has finished."): the finalize owns the session and only
  its own result clears it, so a retry that took the overlay's token turned
  that result into a background one and left the session open for good, and
  stopping it diverted the newest dictation to history only. The retry stops
  only the jobs of its own recording (`_live_jobs_of_the_slot`), never another
  dictation's.
