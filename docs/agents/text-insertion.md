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
    superseded_changed|busy_rescheduled|abandoned_busy|failed delay_ms=...`;
    failure is WARNING, never raised. Why: a fixed 160 ms Qt-thread sleep
    (`SENDINPUT_RESTORE_DELAY_S`) lost pastes into Electron
    (`probe_wm_null_order.py`: WM_NULL answers before queued input). Raising
    the delay was rejected: on the Qt thread a longer sleep froze the UI
    during a streaming dictation, and any fixed delay only moves the race.
  - A paste during a pending restore takes the record over, keeping the
    *original* previous state while the clipboard still holds the previous
    transcript (streaming pastes every ~350 ms). `flush_pending_restore`
    restores at once (content check kept); `DictationController.shutdown`
    calls it first. A scheduler that cannot start a thread leaves the record
    pending, never reports a landed paste as failed.
  - WM_PASTE stays synchronous (contention check, raising restore failure).
  - *The marker is read inside the write*: `set_clipboard_text` returns a
    `ClipboardMarker` read before it returns; `_clipboard_changed_after_set`
    compares content when readable, the counter only otherwise (each open may
    raise retryable `ClipboardContentionError`).
  - *A 1418 inside our open clipboard reopens and retries* (2026-10-01,
    `Win32ClipboardBackend._with_reopen`, 3 attempts, about 0.33 s worst case
    on the Qt thread). pywin32 opens with a NULL owner, so a clipboard
    manager's `CloseClipboard` closes ours and the next call fails with
    "Thread does not have a clipboard open". Spent attempts raise
    `ClipboardContentionError`. A close lost after a finished write is read
    back (`_confirm_write_landed`: counter first, then exact text), and a
    retried write never empties a clipboard someone wrote to since. This
    survives the race; an owner window would close it and is not built (see
    the rejected alternatives below).
  - *A transcript the transaction will restore stays out of Win+V and the
    cloud clipboard* (2026-10-01). `set_clipboard_text(exclude_from_history=
    restore_clipboard)` sets ExcludeClipboardContentFromMonitorProcessing,
    CanIncludeInClipboardHistory and CanUploadToCloudClipboard (DWORD 0,
    `RegisterClipboardFormat`) inside the same open (Microsoft, "Clipboard
    Formats", section "Cloud Clipboard and Clipboard History Formats").
    Best-effort: a miss logs `clipboard_history_exclusion_partial failed=N`
    and never fails the paste. With `keep_transcript_in_clipboard` the
    transcript is an ordinary entry. The restore never adds the formats, and
    the capture keeps them like any other format, so a password manager's
    excluded copy comes back excluded. Unit-tested against the fake clipboard
    only: `scripts/release_check_clipboard_paste.py` should gain a check that
    a transcript is absent from Win+V history and a kept one present.
  - `TextInserter.paste_pace_remaining_s` is the time left until the last
    SendInput keystroke's restore window ends (0 after a WM_PASTE or before
    any paste); the controller paces queued pastes by it (below).
  - *The foreground is re-read immediately before `send_paste_with_mode`*
    (SendInput goes to the focus, not `target_hwnd`); a change from the
    snapshot raises retryable `TextInsertionError` and restores at once; an
    unreadable (None) foreground is no change. WM_PASTE skips it.
  - `wait_for_modifier_release` (bounded poll) runs before injecting, or a
    hotkey's held Ctrl/Alt turns Ctrl+V into a no-op. WM_PASTE skips it.
  - No Windows API says "the target read it". Rejected: `WM_RENDERFORMAT`
    delayed rendering (needs an owner window with a message pump, and
    clipboard history and clipboard managers consume it); a UI Automation
    read-back (heavy and per application); an owner window without a pump
    (blocks every other program's `EmptyClipboard`).
    `keep_transcript_in_clipboard` skips the restore.
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
  the finalize tail are exempt. Never sleep on the Qt thread for it.
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
- **Cancel, Retry and Insert share one fixed-size overlay slot.** A
  successful transcription whose insert failed uses
  `error_action=OVERLAY_ERROR_ACTION_INSERT` (`insert_again_requested` ->
  `insert_failed_text`). An Error whose failure kept no audio of its
  own uses `OVERLAY_ERROR_ACTION_NONE` (a Retry would transcribe the previous
  failure's audio); the tray's "Retry transcription" still names the slot.
- **A queued transcript that failed to paste is never silent**: it emits
  `background_insertion_failed` (tray, always); with nothing newer on the
  overlay it shows Error + `OVERLAY_ERROR_ACTION_INSERT` and takes over
  `_last_transcript`. `_foreground_delivery_pending` suppresses that inside
  `_on_transcription_ready` just before the foreground result paints.
- **A failed queued paste stays listed until inserted or dismissed**
  (owner's field report 2026-10-01: six queued pastes, two silently
  missing). Each failed or possibly-landed queued or foreground batch paste
  becomes an `_UndeliveredInsert` row (negative id) in the overlay queue
  panel, sent as `QUEUE_ROW_KIND_UNDELIVERED`: "Not inserted" or "Possibly
  inserted, check the window", amber, with a Dismiss button (both row
  captions share one reserved width) and a title that counts transcriptions
  and waiting inserts apart. Later results never replace a row; Dismiss
  (`queue_cancel_requested` with the row id) and Clear queue drop it; a
  successful insert of its text retires it. The tray report appends
  `_undelivered_hint`: the count and how to insert them -- the re-paste
  hotkey only while registered, else the tray's "Insert transcript again".
- **The overlay's Insert pastes the text that failed** (a streaming
  finalize's tail past `committed_text`, not all of `_last_transcript`).
  `_insert_action_text` is written by both paths that paint the Insert action
  (`_insert_text_at_target`'s error arm and
  `_report_background_insertion_failure`), read by `insert_failed_text`
  (wired in `main._connect_overlay_actions`), cleared by a successful
  re-paste and at recording start, falling back to `_last_transcript`.
  Tray/hotkey keep `repaste_last_transcript`.
  - **A refused recording start keeps the offer**: the clear sits past every
    refusal; `_refuse_recording_start` repaints it ("Still not inserted:")
    with `copy_text`/`error_action` on the offer (the stripped transcript).
  - **Every non-session status writer paints through
    `_paint_status_keeping_offer`**: `show_idle_status` lines, "Nothing to
    cancel.", the "Transcription canceled." writers, the preload's lines,
    `show_overlay_notice`, Edit/Retry/re-paste refusals and the Edit
    confirmation. Session states stay plain. While an offer is pending Copy
    yields it and Edit is disabled -- intended.
  - **The offer carries its own action**: after a post-keystroke failure (six
    `TextMayHaveBeenPastedError` raise sites in `text_inserter.py`, two via
    the `combined_error` alias and `_ClipboardContentionAfterPaste`) Insert
    is withheld, decided by the offer's own `_insert_offer_may_have_pasted`,
    never the per-attempt `_last_insert_may_have_pasted` a later paste in the
    same flush resets.
  - **A paste that carries the offer marks it** (`_paste_carried_the_offer`:
    whitespace-folded equal, or ends with it at a word boundary; asked only
    by `_repaste` via `may_carry_offer` -- a substring test marked
    "Wochenende" for " ende"). A re-paste retires only an offer it carried.
  - **Clear retires the offer it dismissed** (`OverlayUI.detail_cleared` ->
    `on_overlay_detail_cleared`), not one hidden behind a later Error.
  - **The preload progress poll** repaints only the painter's own Error
    (`_offer_painted` = `(state, detail)` vs `OverlayUI.state` /
    `OverlayUI.detail`), skips a foreground transcription in flight
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
  overlay. Refused with the reason during a capture or a recording
  start/stop (kept from before: a paste then races the recording's target
  snapshot; whether to allow it during a batch recording is open with the
  owner) and during a streaming finalize, whose tail would land behind the
  paste.
  A failed re-paste whose keystroke went out marks its rows possibly
  inserted, never pasted again. The no-window refusal reveals the overlay
  once (`show_overlay_error` reveals only when it paints). The hotkey may
  not equal the recording, cancel or overlay one.
- **`retry_last_transcription` refuses during a recording start, stop or open
  capture** (via the tray), since it stops the running transcription and
  paints Processing. A pending streaming finalize is stopped by design, so
  `_streaming_recording` is not in the guard.
