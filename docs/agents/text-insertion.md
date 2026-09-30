# Text insertion and focus: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
`text_inserter.py`, `window_focus.py`, the insert offer and queued/deferred inserts. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **GUITHREADINFO duplication**: defined in both `text_inserter.py` and `window_focus.py`. Intentional — modules are self-contained.
- **A paste is a transaction with a deferred, guarded restore** (2026-09-16,
  F01/F02/F07 of the external review). `TextInserter._paste_text_with_options`
  opens the transaction and writes one `paste_transaction id=... mode=<requested>/
  <actual> target_hwnd=... foreground=... chars=... marker=... outcome=...
  restore=...` line whatever happens; `_run_paste_transaction` does the work.
  Four rules, each measured before it was written:
  - *The restore is not part of the call.* The predecessor slept
    `SENDINPUT_RESTORE_DELAY_S` (160 ms) on the Qt thread and then restored,
    and the readiness gate before it (`wait_for_paste_target_ready`, WM_NULL
    through `SendMessageTimeoutW`) proved nothing about the keystroke: sent
    messages are retrieved ahead of posted and input messages, so a
    message-only window whose handler was busy for 300 ms answered WM_NULL
    at 0.302 s and dispatched a keystroke posted before it only afterwards
    (`probe_wm_null_order.py`, 2026-09-16). The 160 ms were the only barrier,
    and the field log shows them lost: a 326-character transcript reported
    `text_insertion outcome=success` 196 ms after the transcription finished,
    into an Electron window (its renderer reads the clipboard asynchronously)
    while a test suite pinned the CPU, and the user pasted the same 326
    characters by hand 4.5 s later. After a SendInput paste the transaction
    now returns at the keystroke and hands a `_PendingRestore` to a scheduler
    (`schedule_fn`, default a daemon `threading.Timer`). After
    `CLIPBOARD_RESTORE_DELAY_S` (1.5 s) the timer thread runs the readiness
    probe *outside* `_insert_lock`, then under the lock restores only while
    the clipboard still holds the transcript (content compared, never the
    counter alone), reschedules while the target is busy, and gives up at
    `CLIPBOARD_RESTORE_MAX_WAIT_S` (10 s) leaving the transcript on the
    clipboard -- a target that slow has not read it yet, and restoring is
    what turns its late paste into the user's old content. Outcomes are
    logged as `clipboard_restore id=... outcome=restored|skipped_changed|
    superseded|superseded_changed|busy_rescheduled|abandoned_busy|failed
    delay_ms=...`, the failure at WARNING and never raised: the call
    returned success long before. A new paste during a pending restore takes
    the record over: while the clipboard still holds the previous transcript
    the *original* previous state carries over (a streaming dictation pastes
    every ~350 ms, and capturing afresh would restore a transcript over the
    user's clipboard at the end), otherwise the user copied something and the
    transaction captures that. `flush_pending_restore` runs the restore at
    once (content check kept, readiness wait skipped) and
    `DictationController.shutdown` calls it first, because exit kills the
    daemon timer. A scheduler that cannot start a thread leaves the record
    pending (the next paste or the shutdown flush settles it) rather than
    reporting a landed paste as a failure, which the streaming retry would
    have pasted again. Raising the delay was rejected: on the Qt thread a
    longer sleep froze the UI during a streaming dictation, and any fixed
    delay only moves the race. The WM_PASTE road is synchronous and
    unchanged, `SendMessageTimeout` returns after the target read the
    clipboard, so its post-keystroke contention check and its raising
    restore failure stay.
  - *The marker is read inside the write.* `set_clipboard_text` returns a
    `ClipboardMarker` read between `CloseClipboard` and the return. The
    caller used to read the counter in a second call, so a foreign write in
    the gap became "our" number and the change check compared a number with
    itself -- the transaction pasted the stranger's content and restored over
    it (measured on the old implementation with a recording backend).
    `_clipboard_changed_after_set` compares content first whenever it can be
    read and falls back to the counter only for a backend without text; the
    price is two clipboard opens per paste that the equal-counter fast path
    used to skip, each of which can raise the retryable
    `ClipboardContentionError` under an aggressive clipboard manager. Whether
    the counter increments on `SetClipboardData` or on `CloseClipboard` is
    undocumented and unmeasured (the user's clipboard was never text-only
    when the probe ran, and the probe refuses to write over other formats);
    "the value right after our close" is true either way.
  - *The foreground is re-read before the keystroke.* `SendInput` addresses
    the focus, not `target_hwnd`, and between the controller's own check and
    the keystroke sit the modifier-release wait (up to 1.5 s) and the settle
    sleep. The window is snapshotted when the transaction opens, before that
    wait, and compared immediately before `send_paste_with_mode`; a change
    raises a retryable `TextInsertionError` with the clipboard restored at
    once. A foreground that cannot be read (None) is no evidence of a change.
    WM_PASTE addresses the handle and skips the check.
  - *Held hotkey modifiers* (2026-07-09): inserts are often triggered
    straight from the WM_HOTKEY press (stop, cancel, queue flush), so the
    user's physical Ctrl/Alt was still down and the injected Ctrl+V reached
    the target as Ctrl+Alt+V (AltGr+V on German layouts), silently pasting
    nothing (the transcript then existed "only in history").
    `wait_for_modifier_release` (GetAsyncKeyState poll, bounded timeout) runs
    before injecting; WM_PASTE skips it because messages ignore keyboard
    state.
  Rejected for the restore: delayed rendering / `WM_RENDERFORMAT` as a "the
  target read it" signal needs an owner window with a message pump and is
  consumed by clipboard history and clipboard managers; a UI Automation
  read-back is heavy and per application; and an owner window without a pump
  blocks every other program's `EmptyClipboard`, so there is none. With
  `keep_transcript_in_clipboard` the restore is skipped entirely, which
  closes the late-read race completely. There is still no Windows API that
  says "the target read the clipboard"; the deferred restore's content check
  and its budget are what replace the guess.
- **The clipboard is put back with every format it held, not its text
  alone** (F12 of the 2026-09-12 review).
  `Win32ClipboardBackend.capture_clipboard_state` kept `CF_UNICODETEXT`
  only and `restore_clipboard_state` emptied the clipboard and wrote that
  text back, so one dictation turned a copied screenshot, a file selection
  or formatted text into plain text -- measured on the user's own
  clipboard, which held nine formats (`DataObject`, `CF_UNICODETEXT`,
  `CF_TEXT`, `HTML Format`, `text/markdown`, an ODT format, `Ole Private
  Data`, `CF_LOCALE`, `CF_OEMTEXT`) of which one came back.
  `ClipboardState.formats` carries `(format id, registered name or "",
  raw bytes)` per format in the clipboard's own enumeration order;
  `has_text` and `text` keep their meaning, so every "still holds our
  transcript" check reads as before. The capture, inside the one
  clipboard open, reads the id list in a single `EnumClipboardFormats`
  pass before touching anything (a `GetClipboardData` for a format
  Windows synthesizes on demand may change the list), skips the
  GDI-handle and owner-drawn standard formats (`CF_BITMAP`,
  `CF_METAFILEPICT`, `CF_PALETTE`, `CF_ENHMETAFILE`, `CF_OWNERDISPLAY`,
  the three `CF_DSP*` handle formats), the private ranges
  `0x0200-0x02FF` and `0x0300-0x03FF`, and `DataObject` / `Ole Private
  Data` by name (they describe the copying application's live
  `IDataObject`), and copies everything else through
  `user32.GetClipboardData` -> `kernel32.GlobalSize` -> `GlobalLock` ->
  `ctypes.string_at` -> `GlobalUnlock` on the module's own handles --
  never pywin32, which decodes what it reads (text as `str`, `CF_HDROP`
  as a tuple of paths). A NULL handle, a zero size or a refused lock
  skips that format and is counted (`clipboard_capture_unreadable
  formats=<n>`, INFO); a format over `CLIPBOARD_CAPTURE_MAX_FORMAT_BYTES`
  (128 MiB) or a total over `CLIPBOARD_CAPTURE_MAX_TOTAL_BYTES` (256 MiB),
  both read off `GlobalSize` before any copy, abandons the collection and
  leaves the text-only state (`clipboard_capture_truncated formats=<n>
  bytes=<total>`, WARNING; the numbers include the format that tripped
  the cap and never its content). A failed enumeration returns the ids
  it has rather than raising: the capture is on the path of every
  dictation and `_run_paste_transaction` turns anything raised out of it
  into a failed insertion, so the failure costs the formats, not the
  transcript. The restore, inside the one open, empties the clipboard
  and sets every captured format in order, each in a fresh
  `GlobalAlloc(GMEM_MOVEABLE)` block that `SetClipboardData` takes
  ownership of on success and that is freed only on failure; a
  registered format's id is checked against its name and re-registered
  when the id no longer answers to it; a refused format is counted
  (`clipboard_restore_partial failed=<n>`, WARNING) and the rest are
  still set; and the text is written separately only when
  `CF_UNICODETEXT` was not among the formats set, so the restore can
  never put back less text than the text-only one did. Measured cost on
  the Qt thread (the capture; the deferred restore's write runs on its
  timer thread except on the WM_PASTE road): 0.4 ms for a 4 MiB
  screenshot, 3.2 ms at 32 MiB, 24 ms at the 256 MiB cap. Every test
  drives the real backend against a fake `win32clipboard`/`user32`/
  `kernel32` whose blocks are real ctypes buffers, so the copy, the sizes
  and the lock pairing are real and the Win32 calls are not. **Measured on
  the real clipboard on 2026-09-18** with a scratch probe: text, `HTML
  Format`, `CF_DIB`, `CF_HDROP` and `Preferred DropEffect`, written
  independently through pywin32, came back byte-identical after capture ->
  transcript -> restore, Windows synthesized `CF_BITMAP` again, and
  Explorer's own paste still MOVED a cut file and COPIED a copied one
  afterwards. Not measured: a real screenshot tool's or Office's clipboard.
- **`SMTO_ABORTIFHUNG` is why the readiness probe needs its own sleep**: that
  flag makes `SendMessageTimeoutW` return *immediately* when the target thread
  is already hung, instead of waiting out the timeout it was given. So
  `wait_for_paste_target_ready`'s loop had no delay in it at all against the
  one case it exists for: measured 953,446 probes inside a single budget
  window, one core pinned and -- while the probe still ran inside the paste
  transaction -- the Qt thread unavailable for the whole time, from nothing
  worse than pasting into a frozen application. The probe has since moved
  to the deferred restore's timer thread, where a spin still pins a core. That was measured
  with the real Win32 probe against a handle that names no window, which
  returns as fast as `SMTO_ABORTIFHUNG` makes a hung target return; a
  pure-Python stub of the probe spins 33x faster still and could not have
  produced it, which is how the "fake that always reports hung" provenance in
  `docs/learning-log.md` was caught. It polls at
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
  clipboard alone. The re-raise carries `allow_clipboard_fallback` across,
  which this entry previously recorded as an unreachable gap "because no
  caller combines the two". Both halves of that were wrong: the combination
  happens inside `insert_text`, not across callers -- a non-contention failure
  after the paste keystroke lands in the handler, which then *constructs* a
  `ClipboardContentionError` when it finds the clipboard changed -- and the
  re-raise built a fresh exception, so the refusal was replaced by the
  permissive default. Measured through `insert_text`: cause flag False,
  re-raised flag True, and the controller would then have copied the
  transcript over the clipboard the user had just filled.
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
  "While busy" combo ("While transcribing" until 2026-09-27) as a fourth
  choice (`insert_immediate` UI value in
  `_CONCURRENT_MODE_UI_CHOICES`); the stored settings stay
  `concurrent_transcription_mode` + `immediate_background_insert`.
- **`insert_target` setting**: `recording_window` (default) pastes into the
  window/control snapshotted at recording start; `current_window` pastes into
  whatever is focused when the transcript is ready. The caret position inside
  the target is always the position at insert time — Windows cannot paste at
  a remembered caret offset. With `current_window`, deferred flushes coalesce
  into a single paste since every result goes to the same target.
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
  only answer "No failed transcription to retry". And an Error whose
  failure kept no audio of its own -- a finalize or a stream that died
  without persisting, an aborted stream, the watchdog's timeout with no
  late bytes, a background failure reported on an idle overlay with its
  audio not kept -- passes `OVERLAY_ERROR_ACTION_NONE` and shows no
  button: a Retry there transcribed the previous failure's audio under an
  Error about this one (wave 16). The tray's "Retry transcription" still
  names the slot.
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
- **The overlay's Insert pastes the text that failed, which is not always the
  last transcript.** A streaming finalize inserts only the tail past
  `committed_text` -- the rest already reached the document live -- and its
  Error state offers Insert for exactly that tail. The button was wired to
  `repaste_last_transcript`, which reads `_last_transcript`, the whole
  dictation: measured, the finalize inserted ' zweiter teil' and Insert then
  pasted 'erster teil zweiter teil' on top of the 'erster teil' already in
  the document. `_insert_action_text` is written by the two paths that paint
  an Error with `OVERLAY_ERROR_ACTION_INSERT` (`_insert_text_at_target`'s
  error arm and `_report_background_insertion_failure`), read by
  `insert_failed_text` -- the slot `main._connect_overlay_actions` wires the
  button to -- cleared by a successful re-paste and at the next recording
  start, and it falls back to `_last_transcript` when nothing failed. The
  tray action and the re-paste hotkey keep `repaste_last_transcript`, because
  there "the last transcript" is what the user asked for. The background arm
  must write it too even though it also sets `_last_transcript` to the same
  text: a stale tail from an earlier failed finalize survives until the next
  recording start, and the cancel hotkey's "nothing to cancel" path flushes a
  queued job's failing insert without one -- Insert then pasted the old tail
  while the overlay displayed the queued transcript. The wiring is pinned by
  emitting the signal at a fake controller, not by reading `run()`.
  **A refused recording start keeps the offer.** The clear sat on the
  statement after `_recording_start_in_progress`, before any branch that
  decides whether a recording starts, so a refusal -- "Model is still
  loading" right after a dictation is the common one -- retired the tail and
  repainted the overlay without it, and the only remaining road to that
  tail was the whole-dictation re-paste. The clear now sits past every
  refusal, and `_refuse_recording_start` paints a refusal that finds a
  pending offer with the text still on screen ("Still not inserted:") and
  `copy_text`/`error_action` acting on exactly it. The background arm's
  `copy_text` is the stripped transcript, the same value Insert reads.
  **Every status writer that is not a session result paints through
  `_paint_status_keeping_offer`**: the idle line and the hotkey-error lines
  of `show_idle_status`, "Nothing to cancel." and the three "Transcription
  canceled." writers (the cancel hotkey, a queue row's X, Clear queue), the
  preload's start line, progress line, "Canceling model download..." and
  four end-of-preload lines, the tray's copy notices
  (`show_overlay_notice`), the three Edit refusals, the Retry refusal, the
  re-paste refusals and the refusal above. Fixing the refusal alone left
  every one of the others hiding the button while the text stayed pending
  -- a save, a resume, a Cancel press, a finished preload, "No window to
  insert into" -- and the first painter round covered five of them and
  left nine, which the wave-4 reach review found. The writers that stay
  plain are session states: "Streaming transcript is still finalizing" and
  "Retrying transcription..." describe a session that owns the overlay, and
  a recording start clears the offer anyway. **The preload progress poll
  repaints the painter's own Error and no other**: the poll skips a Done or
  Error result on screen, the offer is an Error, so with an offer pending
  it froze the progress line for the whole download. The painter records
  `(state, detail)` in `_offer_painted`, and the poll compares that with
  `OverlayUI.state` and `OverlayUI.detail` -- a result replaces the
  painter's text, an untouched offer matches it. **And the
  offer carries its own action**: the insert paths that fail *after* the
  paste keystroke went out (six raise sites of `TextMayHaveBeenPastedError`
  in `text_inserter.py`: four literal, one through the `combined_error`
  alias and one through the `_ClipboardContentionAfterPaste` subclass,
  neither of which a grep for the class name finds) deliberately
  withhold Insert -- the text is most
  likely in the document already -- and a repaint that read the pending text
  alone upgraded that to an Insert button -- pressing it pasted the
  transcript a second time, measured through the overlay's Insert and
  through a queued transcript's flush. The flag that decides in the painter
  is the offer's own, `_insert_offer_may_have_pasted`, recorded beside the
  offer by the paths that create it -- not `_last_insert_may_have_pasted`,
  which is per insert attempt and which the next insert resets: one flush
  pastes several queued transcripts, so a second paste that succeeded
  re-armed the Insert of the first, whose keystroke had gone out (measured:
  all nine newly routed writers offering a duplicate paste), and a
  different transcript's post-paste failure hid the button of a tail that
  had never reached a window. The offer's own re-paste failing after the
  keystroke is the one insert that sets its flag. The text stays readable
  and copyable, the button stays hidden, the wording says which of the two
  it is. **The overlay's Clear retires the offer it dismissed**
  (`OverlayUI.detail_cleared`, carrying the cleared state's copy text, wired
  to `on_overlay_detail_cleared`): the overlay's own clear wrote Idle
  without the controller learning of it, so the offer came straight back --
  600 ms later from the poll during a preload, and from every later status
  writer. Only the offer that was on screen is retired; one hidden behind a
  later Error is kept. **A paste that carries the offer's words marks it**
  (`_paste_carried_the_offer`): the offer's own flag was set on exact
  equality, and the tray's "Insert transcript again" pastes
  `_last_transcript`, the whole dictation, of which a streaming tail's
  offer is the last part -- that re-paste failing after its keystroke
  left the flag False and the next repaint armed Insert for words that
  had just reached the window (measured: the tail reached the inserter
  three times; introduced by `f0f4a77`, whose parent read the
  per-attempt flag and got this one sequence right). Whitespace is
  folded for the comparison, because the tail is inserted with a leading
  space. Carried means the pasted text is the offer or ends with it at a
  word boundary, and only `_repaste` asks (`may_carry_offer`): the
  substring test that first replaced equality was asked by every insert,
  so a queued transcript that merely contained the tail's word ("Milch
  und Brot" for " und", "Wochenende" for " ende") and failed after its
  keystroke marked the tail, and every later repaint hid Insert for
  words that had reached no window (four of six one-word tails on the
  real painter). And the re-paste's success arm retired the offer
  unconditionally: `_last_transcript` moves on when a failed queued
  streaming job rescues its partial, so the tray's re-paste of unrelated
  text took the tail's Insert away while the tail had reached no window;
  an offer the paste did not carry stays pending under the Done line.
  **The poll's other three rules**: it leaves a
  foreground transcription in flight alone (`_overlay_session_active`,
  which sees the request token after `stop_recording` has dropped the
  capture -- a model changed in Settings mid-transcription painted the
  download progress over "Transcribing audio..."); it leaves the offer
  alone while the user has scrolled or selected it
  (`OverlayUI.detail_is_being_read` -- `set_state` scrolls an Error to the
  top and `setText` drops the selection, which the repaint did every
  600 ms for the whole download) -- **and its own progress line too**:
  gated on the offer alone, the ordinary download's Processing line was
  still repainted over the user's selection in it every 600 ms (measured
  on the real overlay: the selection gone after one poll); and with an
  offer pending its line names the cancel *hotkey* rather than a Cancel
  button the action slot no longer holds (`_preload_abort_hint`), or,
  with no hotkey registered, the tray's "Cancel current action"
  (`TRAY_CANCEL_ACTION_LABEL`, shared with the menu) -- dropping the
  sentence left a multi-gigabyte fetch with no way out on screen. All
  five progress lines carry it: the queued line and the load line did
  not, so behind another model's load, and on every preload's load
  phase, the overlay showed neither the button nor a word about the
  hotkey or the tray. The
  overlay Edit button's success confirmation goes through the painter
  like its refusals (there is no tray Edit action; two comments
  `f0f4a77` added said "the tray's", one of them left in `tests/` until
  wave 7 -- neither its message nor any revision of this entry did,
  which this entry claimed for one round), which leaves
  Copy yielding the pending offer rather than the edited text and Edit
  disabled until the offer is retired -- the offer's semantics, not a
  defect. **The rest position `detail_is_being_read` compares against is
  held through a relayout** (`OverlayUI._on_detail_range_changed`):
  `batched_update` defers the geometry, so `set_state` scrolled to a
  stale maximum and Qt clamped the value when the range changed under
  it -- a batched Done showed 392 of 408 px with its last line hidden,
  queue rows appearing did the same (286 of 302), and the property
  answered True for nothing the user had done. `rangeChanged` re-asserts
  the rest position, or the position the user chose, bounded by the
  range; `actionTriggered` fires for the wheel, a drag, a click on the
  track and the keys and never for `setValue` (verified with probes on
  the real overlay; the stylesheet removes the scrollbar's arrow
  buttons), the handler reads `sliderPosition()` there, and scrolling
  back to the rest position hands the hold back. A one-way flag did
  not: back at the bottom, the next relayout clamped the value as
  before (286 of 302) and, because `detail_is_being_read` then answered
  True, the preload poll -- the one writer that would have painted --
  stayed away for the rest of the download. A drag above the
  intermediate maximum was clamped by the queue rows and left there (312
  of 408 came back at 286); and while the rows clamp the user's value
  onto the rest position, `detail_is_being_read` reads the flag, so the
  poll does not paint over a user still reading. A paint supersedes the
  user's scrolling. A
  parametrized test drives every writer except `_refuse_recording_start`,
  which four tests of its own drive, and the poll has tests of its own.
- **Re-paste last transcript**: `controller.repaste_last_transcript` pastes
  `_last_transcript` into the currently focused window through the normal
  `_insert_text_at_target` path (paste mode, clipboard semantics, modifier
  release wait), reachable via the tray action "Insert last transcript
  again" and the optional fourth global hotkey `repaste_hotkey` (default
  empty — a global paste combo is riskier than an overlay reveal, so nothing
  is preset). It is blocked while a recording/stream is active so a paste can
  never interfere with a capture, and while a foreground transcription is in
  flight (wave 13): `_last_transcript` is still the previous dictation then
  and the job's result owns the overlay, so let through it pasted the older
  text and painted "Done" with it over "Processing", and a paste that failed
  there painted "Error" over it through the inserter's own handler. Both
  refusals reach the tray, since a session owns the overlay. And the
  no-window refusal reveals the overlay once: `show_overlay_error` reveals on
  its paint road and must not on its tray road, and a second reveal from
  `_repaste` brought a "Listening" overlay to the front for an error the tray
  carried. It never writes a new history entry.
  **Retry has the same microphone guard** (wave 15):
  `retry_last_transcription` stops the running transcription and paints
  "Processing", and from the tray while a recording was active it did both
  over the live session -- "Listening" replaced with the capture still
  running, and the transcription the recording was about to queue behind
  stopped (the wave-15 reach lens's open item). It refuses during a
  recording start, stop or open capture, through the tray; a pending
  streaming finalize is a transcription in flight, which it stops by
  design, so `_streaming_recording` is not in the guard.
  Save-time validation rejects conflicts with the recording, cancel, and
  overlay hotkeys.
