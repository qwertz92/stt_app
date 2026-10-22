# Overlay: design decisions

Binding rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30; history and full measurements are in `docs/learning-log.md` and
git history. Read before changing `overlay_ui.py` or any controller path that
paints the overlay. "Above/below" refers to this file; "Known limitations" is
`docs/agents/known-limitations.md`.

Verbatim pre-condensation text: `git show e608f86:docs/agents/overlay.md` (original AGENTS.md: `df2642a`).

- **One event's changes go through `OverlayUI.batched_update()`** (geometry
  deferred to the end, resized with painting suppressed); otherwise queue plus
  state resized twice (183 -> 137 -> 269 px) with a stale frame. The
  controller wraps slots via `_overlay_batch()` (`toggle_recording`,
  ready/failed); outside a batch `set_state` resizes at once.
- **`show_idle_status` re-checks `_overlay_session_active()` at fire time**:
  a delayed `singleShot` from the preload painted "Idle" over a live capture,
  and the next hotkey press stopped it. Every delayed writer must re-check.
- **`show_overlay_error` never paints over a live session**: while
  `_overlay_session_active()` it logs and emits `busy_overlay_error`, shown as
  a tray notification by `main._connect_tray_notifications` (a refused
  opacity/pin save had replaced "Listening"). Outside a session it paints,
  keeping a pending insert offer. `show_overlay_notice` drops confirmations
  during a session. The wiring is its own function, tested at a fake tray.
- **Resize only through `_resize_window`, never `self.resize(...)`**: it
  activates both layouts first, because `QWidget.resize` clamps to a stale
  minimum and a short state kept the tall height. Size computations add
  `_container_frame_margins()`, and `set_state` applies the stylesheet before
  measuring, or `OVERLAY_MAX_HEIGHT` does not hold.
- **The wait before "Speak now" is painted "Starting", not in the Listening
  green** (`set_state(..., starting=True)`, colour `OVERLAY_STATE_COLORS
  ["Starting"]`): nothing is recorded until the microphone is open, and the
  owner saw a green overlay while his first seconds were missing (2026-10-09).
  The state stays "Listening" for every control and check; only label and
  colour differ. "Speak now" and the remote "You can speak now" are green.
- **A transcript that exists but was not (surely) inserted is a warning, not
  the red Error** (owner's decision 2026-10-17): `set_state("Error", ...,
  warning=OVERLAY_WARNING_NOT_INSERTED | OVERLAY_WARNING_CHECK_WINDOW)`
  paints burnt orange (`OVERLAY_STATE_COLORS["Warning"]`, `#9a4d00`) under
  "Not inserted" (the paste failed or its target is no text field: Insert
  offered) or "Check window" (the keystroke went out and only the cleanup
  failed: no Insert, never pasted twice). Red and "Error" stay for failures
  that left no transcript (Retry, no action) and for a real failure painted
  over a pending offer (the severity bullet below). Why: nothing failed that
  Retry could redo and the text is safe in history, so the red "Error" over a
  working Insert said more than had happened.
  - **Appearance, not a state**, like "Starting": `state` stays "Error", so
    the controls (Insert, Copy, Edit, Clear), the scroll-to-top, the
    preload poll's `_offer_painted` check, the paste check's `overlay_shown`
    and `FakeOverlay` need no change, and the many tests that read
    `state == "Error"` keep their meaning. Only the label word and the
    colour differ; `warning` is ignored for any other state.
  - **One writer**: `DictationController._paint_not_inserted` picks the word
    and the action from one flag (`may_have_pasted`) and is the only
    `set_state` call that offers Insert. It has four call sites:
    `_paint_insert_offer`, one in `_paint_status_keeping_offer` (it covers
    both of that function's wordings, "Still not inserted" and "Possibly
    inserted already") and the two arms of the failed paste in
    `_insert_text_at_target`. It always passes `warning` -- a word, or None
    for the red Error below -- and `tests/conftest.py`'s `FakeOverlay`
    refuses an Insert offer whose call leaves `warning` out, so a fifth
    writer fails every test that reaches it. That guard cannot see a writer
    that passes `warning=None`, so
    `test_only_the_one_writer_names_the_insert_action` parses `controller.py`
    and fails when `OVERLAY_ERROR_ACTION_INSERT` is named anywhere but
    inside `_paint_not_inserted` (the import is not a use).
  - **Severity wins over the warning** (lead's decision 2026-10-17): the
    orange belongs to the offer's own state. A status painted over a pending
    offer by `_paint_status_keeping_offer` is decided by what it is. A real
    failure -- state "Error": a hotkey that did not register, a failed model
    preload, a setting that was not saved (`show_overlay_error`), a refused
    recording start, a failed Edit or Retry -- is the red "Error" label and
    colour, and the offer rides along unchanged: the "Still not inserted" /
    "Possibly inserted already" detail, the transcript, Copy, Edit and
    Insert (`_paint_not_inserted(failure=True)`, `warning=None`). Without
    this the failure was painted orange "Not inserted" and read as nothing
    but the offer. An informational line (Idle, Done, Processing: the idle
    hotkey line, the preload's progress, "Transcription canceled.") and an
    error about the insert itself (`show_overlay_error(..., about_insert=
    True)`: "No window to insert into", the re-paste refusals, "waiting
    transcripts were dismissed") keep the orange. The next status that is
    not a failure turns it orange again. The overlay needs nothing for it:
    red plus Insert is the Error state it always had, so the controls are
    the same (the warning's own overlay test compares them with and without
    `warning`) and the three-size geometry test includes that case.
    - **The red failure stays until something replaces it.** The preload
      progress poll refreshes only the orange paint it recorded in
      `_offer_painted`; a failure painted over the offer is not recorded, so
      the poll treats it as the result it is and leaves it alone. Recorded
      like the orange, the poll turned a refused start ("Model is still
      loading.") back into the orange progress line within 600 ms.
    - **A background failure keeps the offer too**
      (`_report_background_failure`): another job failing on an idle overlay
      while an offer is pending paints the red Error through
      `_paint_status_keeping_offer`, not a plain Error that took the
      transcript, Copy and Insert off the screen. Insert and Retry share
      the overlay's one action slot, so Insert wins it and the text says
      "use Retry transcription in the tray menu" (the audio is kept either
      way).
  - **Why two words, and why these**: "Not inserted" is true for the offer;
    for a keystroke that went out it would be false (the detail says "most
    likely inserted"), so that one reads "Check window", the queue row's
    own advice. The state label reserves the widest word's width; both new
    words are wider than "Processing" and that reservation now sets the
    header's `sizeHint`, which stays inside the controls row that alone
    decides the overlay's width. Measured with real widgets at 9 / 11.25 /
    13.5 pt: widest word before 59 / 75 / 91 px ("Processing"), after 81 /
    100 / 122 ("Check window"; "Not inserted" 70 / 88 / 107); header
    `sizeHint` 387 / 411 / 459 -> 409 / 436 / 490 px against the controls
    row's 440 / 473 / 528; overlay 470 / 503 / 558 px wide, the state label
    rectangle 112 / 137 / 160 px wide, both before and after, in every
    state including the two warnings; the painted glyphs sit inside the
    label and on the overlay's centre line.
    `test_the_warning_words_move_nothing_in_the_header_at_9_11_and_13_5_pt`
    pins it, and fails when a word is added to the label without the
    reservation. A shorter word ("Insert") was not needed.
  - **Contrast, from painted pixels**
    (`test_the_warning_keeps_its_text_readable_and_the_badge_apart_from_it`):
    white text on `#9a4d00` 6.11:1
    (the red it replaces: 6.57), the light-amber queue row labels 4.52:1
    (red: 4.86; both clear WCAG AA's 4.5), the not-inserted badge's amber
    `#ffb300` against it 3.41:1 (red: 3.66). Both are orange, so only
    lightness tells badge from background: 3:1 needs a background of
    relative luminance 0.145 or less (`#9a4d00` is 0.122). Negative controls:
    `#b35900` failed the row-label check, `#d98200` the white text.
- **A stop that waits for the microphone's backlog paints "Processing" --
  "Collecting the microphone's delayed audio..." first** (2026-10-10), with
  `paint_now()`: a synchronous `repaint()` of the visible overlay, because
  the wait holds the Qt thread and `processEvents` would deliver queued
  signals into the stop (`docs/agents/audio-capture.md`).
- **Record button**: header starts with Record/Stop (`record_toggle_requested`
  -> `controller.toggle_recording`); fixed-width captions and a stylesheet
  property keep the layout still. Its indicator is a generated icon
  (`_OverlayRecordButton`) with its own trailing gap, not a "●"/"■" glyph
  (baseline-aligned, off centre). Same fill as its neighbours; primary by a
  brighter border only.
- **The header's two button groups are kept equally wide; that centres the
  status text** (the label is the only stretch item; 158 vs 134 px flanks put
  it 12 px off). `_balance_header_flanks` widens the narrower group (Clear and
  Copy are 76 px); width stays 470 px since `_target_window_width` follows the
  controls row.
  - **It measures the `setFixedWidth` width**, falling back to `sizeHint()`
    plus a warning for an unpinned button (`minimumWidth()` is near zero).
    Any later `setFixedWidth` on Record, Pinned, Clear or Copy reinstates the
    offset; the regression test drives every runtime sync.
  - **No spacer instead** (asymmetric clear space, hand-derived constant), and
    Pinned stays in the header (moving it widens the overlay ~80 px).
  - **It relies on symmetric container margins** (1 px frame + 14 px).
    `test_the_status_text_is_centred_on_the_overlay_in_every_state` measures
    painted glyphs and pins the label rectangle across states, actions,
    hover, press, copy feedback, queue and pin mode.
- **`set_state(copy_text=...)`**: Copy yields exactly the transcript even
  when the detail also shows an insertion error; Error scrolls to the top.
- **Compact states grow to fit their text** up to
  `OVERLAY_COMPACT_DETAIL_MAX_HEIGHT` (not pinned to
  `OVERLAY_DETAIL_MIN_HEIGHT`, which clipped the hotkey notice), adding only
  the overflow so `ensure_compact_size()` still holds.
- **Never re-wrap or blink**: the label wraps at a width from the target
  window width (never the live viewport), pre-measuring the scrollbar;
  pinning and reveals never call `setWindowFlags` on Windows (next bullet);
  a recording start confirms `ensure_compact_size()`
  before and after its event drain. The Language button owns its `QMenu`
  popup and a centred chevron; never `QPushButton.setMenu()` (misaligned
  indicator; a pixel test checks the chevron). The Transcription-tab model
  runtime note reserves three lines (gray note for faster-whisper).
- **On Windows topmost is SetWindowPos alone; the Qt flag set never changes**
  (2026-10-09). `setWindowFlags` destroys and recreates the native window:
  every Pinned/Floating click measured `Hide`, 2x `WinIdChange`, `Show` -- the
  blink. `_base_window_flags` carries `WindowStaysOnTopHint` on Windows only
  while `_topmost_uses_window_flag` (SetWindowPos failed); `showEvent` sets
  topmost for a pinned overlay, because Qt sets it only at window creation
  (Qt 6.11 `WindowCreationData::initialize`; `raise_sys` is `HWND_TOP`).
  Measured after the change: no events on either click; topmost survives
  hide/show, `raise_()`, state changes, moves and a stylesheet re-apply.
  Other platforms keep the flag (`_always_on_top`).
- **A floating overlay stays on top of the normal band until the user clicks
  into the window below it** (2026-10-10, owner's requirement of
  2026-10-09). Dropping topmost (Floating click, end of a reveal) is
  `HWND_NOTOPMOST`, and a shown floating overlay waits
  (`_waiting_for_click`). Going directly behind the foreground window
  instead (8a72099, `hWndInsertAfter` = that window) made the overlay vanish
  the moment Floating was clicked or a reveal ended. Clicking an inactive
  window raises it natively (activation); a click into the window that is
  *already* active does not, which kept the editor under the overlay until
  minimise/restore. So while waiting, `raw_mouse_input` registers mouse raw
  input (`RIDEV_INPUTSINK`) for the overlay's window; `nativeEvent` reads
  each `WM_INPUT`, and a button press runs `_on_desktop_mouse_press` on the
  next event-loop turn with the message's `pt`. The overlay goes directly
  behind the foreground window (`SWP_NOACTIVATE`) only when the top-level
  window under the press is that window, `_window_to_stay_behind` accepts it
  (not topmost, minimised, a shell surface or the overlay) and the overlay
  is still above it (`_is_above`, a bounded `GW_HWNDPREV` walk); then the
  watch stops. A press on the overlay or an inactive window, an ineligible
  foreground, or a refused call (elevated foreground: access denied) leave
  it waiting; a window activated above it ends the wait. The watch runs only
  while waiting and shown -- every mouse movement on the desktop is a
  `WM_INPUT` then -- and stops on topmost, hide and `shutdown()`
  (`aboutToQuit`). `SWP_SHOWWINDOW` is passed only once the `QWindow` is
  visible: `showEvent` runs before Qt shows the native window.
  - **Why raw input**: no WinEvent reports the click (measured: a plain
    window produced none for a click into it while active; an EDIT control
    produced `EVENT_SYSTEM_CAPTURESTART` only because it captures the
    mouse). A `WH_MOUSE_LL` hook puts a Python callback (the GIL) into
    every click's path, and Windows silently removes a hook that misses
    `LowLevelHooksTimeout`. `WM_INPUT` is a posted copy: the click never
    waits for this process. One mouse registration exists per process;
    nothing else registers one.
  - **Measured** (real overlay, foreign-process EDIT window, `SendInput`):
    before, the overlay was directly below the editor right after the
    Floating click and after a reveal ended; after, directly above it, still
    above after a click on its own label, and directly below after a left
    or right click into the active editor. A click into another, inactive
    window raised that window over the overlay, which stayed above the
    editor. Press handled ~6 ms after it was injected; a `GetRawInputData`
    read costs ~3 us; 0 `WM_INPUT` once the watch stopped.
  - **A closed menu puts a waiting overlay back above the editor**
    (2026-10-10). The Language, microphone and detail context menus are
    activated popups; when one closes, Windows re-activates the editor and
    raises it over the overlay (measured: below the editor already at
    `aboutToHide`, after Esc, an item choice and a click on the Language
    button). `_after_menu_closed`, a zero-timer from each menu's
    `aboutToHide`, puts it back with `HWND_TOP` while it still waits --
    `HWND_NOTOPMOST` has no effect on a window that is already not topmost
    (SetWindowPos docs; measured: it stayed below). After the fix: above the
    editor and still waiting after Esc, an item and the button click; below
    it, wait ended, after a click into the editor that closed the menu. A
    press while a popup is open (`activePopupWidget()`) is kept
    with its top-level window and judged after that, so a click into the
    editor that closes the menu still sends the overlay behind it and a
    click on a menu item does not. A new popup of the overlay connects the
    same way.
  - Not covered: touch, pen and precision-touchpad taps (known
    limitations) and typing without a click, which leaves the overlay above.
- **The Language and microphone menus are `_RebuildableMenu`s, rebuilt only
  while hidden** (2026-10-03). `QMenu.clear()` deletes the actions under an
  open popup -- and one the user has chosen whose `triggered` has not run
  yet -- and `audio_devices_refreshed` or a settings load can land at any
  moment. A request while the popup is visible is kept and runs on the
  event-loop turn after `aboutToHide` (Qt hides first, triggers after, so an
  immediate rebuild would still delete the chosen action). The button caption
  and enabled state update at once; only the item list waits.
- **The Language button is never locked by a running job; a pick is always for
  the next recording** (2026-10-23, owner: "cannot change the language when a
  transcription is active"). It was disabled in Listening and Processing, a
  lock from 2026-07-06 when a pick reset the cached runtime; since then the
  language is a per-request parameter and every job keeps the one in its
  settings snapshot (`controller-and-jobs.md`), so the lock only forced the
  owner to wait for the queue to empty. Now only an engine/model that offers a
  single language disables it. While Listening, the overlay remembers the
  language selected when the state first became Listening
  (`_recording_language_mode`, `_track_recording_language`; any other state
  drops it, and streaming repaints Listening with every partial, so only the
  first counts). When the selection differs, the caption reads `Next: German`
  instead of `Lang: German` and the tooltip says which language this recording
  is made in; once the recording ends the caption is the selection again and
  the queue row names the finished recording's language (`EN`, `DE`).
  - **Rejected: keeping Listening locked, as the microphone is.** A microphone
    pick cannot reach the running capture; a language pick can still be useful
    (the next recording), and a disabled button only says "not now".
    **Rejected: a note in the detail area** (streaming's live text owns it) **or
    in the tray** (the button would keep claiming the new language).
  - **No layout shift**: the button is pinned at the widest `Lang: X` or
    `Next: X` caption (`_widest_language_caption_width`). `Next:` is 1-2 px
    narrower than `Lang:` in Segoe UI at 9 to 18 pt (measured, widest label
    Luxembourgish / Northern Sotho), so today the second caption changes
    nothing; it guards fonts where it would be wider.
    `test_naming_the_next_language_moves_nothing_on_the_overlay` compares every
    button and label rectangle across the swap; measured with real widgets
    the overlay stays 470x144 and the button 152x22 at (303, 39) in Idle,
    Listening, after a pick, and in Processing with a queue row (470x190).
  - The overlay's value equals the controller's snapshot because both are taken
    in `start_recording`, one event drain apart (25 ms at most, excluding user
    input events); only a settings save landing inside that drain could make
    them differ (the label would then name the other language).
- **The footer is [microphone menu][opacity slider][value]** (2026-10-03,
  UX review item 3, variant B). The microphone button
  (`_OverlayMicrophoneButton`, the Lang button's chevron and chrome, text
  left-aligned) is the footer's only stretch; the slider is fixed at 96 px
  and the value label at the width of "100%" (40 px floor), so neither moves
  while the slider is dragged. The "Opacity" caption is gone (the slider and
  value carry it as a tooltip). The button's size hint is its chrome alone and
  its caption elides ("Mic: Default · HyperX QuadCast S"; the role in
  "Microphone (...)" is dropped on the caption only -- the menu and tooltip
  carry the full name -- and kept when another listed device has the same
  device part, as Realtek's "Microphone"/"Stereo Mix"/"Line In" do; a
  " (not connected)" suffix is never elided, only the name before it),
  so a 50-character device name never widens the
  overlay: `_target_window_width` sums the footer's hint, which stays below
  the controls row's. Height is fitted to the Lang button's. Disabled only
  while Listening (Processing may switch: the capture has ended). Measured
  with real widgets, idle: 470x138 -> 470x144 at 9 pt (the 22 px button
  replaces the 16 px slider row), 503x142 -> 503x144 at 11.25, 560x164 ->
  560x166 at 13.5; width and the status label rectangle unchanged in every
  state; the button gets 288 / 321 / 371 px.
- **Button sizes are measured (`OverlayUI._fit_buttons_to_font`)**: Windows
  "Text size" raises the font without the DPI (18 pt Record needs 108x34 vs
  78x24). It runs after the first `set_state` (stylesheet padding counts only
  then), and `_balance_header_flanks` runs after it. Each entry lists every
  caption its button shows; the Cancel/Retry/Insert slot fits the widest.
- **A Cancel click right after a click on Retry is ignored** (review
  2026-10-17, narrowed in round 2). Retry, Cancel and Insert share one slot at
  one geometry (`_sync_action_slot`), so a double click on Retry started the
  retry with its first click -- which paints Processing and puts Cancel under
  the cursor -- and the second click cancelled the retry it had just started.
  `_on_retry_clicked` (connected to `_retry_button.clicked`) arms
  `_cancel_ignored_until` for `QApplication.doubleClickInterval()` (the
  system's double-click time, so it follows the user's own setting) and then
  emits `retry_requested`; `_on_cancel_clicked` drops a click before that,
  logging `overlay_cancel_ignored reason=double_click_on_retry`, and every
  later Cancel works. It is armed by the click, not by the slot leaving Retry:
  the first version armed on any exit of the slot from Retry, so pressing the
  record hotkey from an Error with Retry (-> Listening) dropped a real Cancel
  click within 500 ms, which no double click explains. Arming at the click
  also needs no case for a refused Retry (the overlay stays on Error and the
  click has nothing under it). No layout change: no gap and no second slot, so
  the one-slot sizing above still holds. Disabling Cancel for the interval
  instead was not done: the button would flash grey under the cursor in the
  one moment the user looks at it. Tests:
  `test_a_second_click_on_retry_lands_on_cancel_and_is_ignored` (clock and
  interval set by the test; the clock is the module-local
  `overlay_ui.monotonic`, so no other code that reads the global
  `time.monotonic` sees the patch), its negative control
  `test_cancel_works_at_once_when_retry_was_not_clicked` and
  `test_cancel_works_at_once_when_the_error_with_retry_was_left_without_a_click`.
- **The queue panel has two row kinds** (2026-10-01): `(token, label)` or
  `(token, label, kind)`, kind `QUEUE_ROW_KIND_TRANSCRIPTION` (Cancel) or
  `QUEUE_ROW_KIND_UNDELIVERED` (Dismiss, a transcript that was not
  inserted). Every row button is fitted for both captions
  (`_QUEUE_ROW_BUTTON_CAPTIONS`), so kinds never differ in button width;
  every row label is one line (`ElidingLabel`, the whole label in its
  tooltip), and the controller puts each row's status ("Pending insert",
  "Not inserted", "Not in a text field", "Possibly inserted, check the
  window", and "From earlier session" for a transcript the startup notice
  of unfinished recordings saved -- no insert was ever attempted for it,
  owner's decision 2026-10-17; it keeps the recording's time and counts in
  the badge like the others) before the time and text (an undelivered row
  quotes those only; the language code and model below are a transcription
  row's), so eliding never hides it; a transcription row names the language
  its recording was made in (`EN`, `DE`, `Auto`: the job's own snapshot, not
  the current selection) as a short code right after the time, before the
  provider (2026-10-09); a label that changes in place never changes a row's
  height
  (wrapped, the long "Possibly inserted, check the window" label grew rows
  from 32 to 40 px and the overlay from 230 to 246 px). The undelivered row
  differs by colour only
  (`QLabel[queueRowKind="undelivered"]`). The title counts the two apart
  (`_queue_title`: "Transcribing N recordings" -- it said "files" until
  2026-10-03 (UX review item 6g), though the user handled no file --, "N
  transcripts not inserted", or both joined by " · "). Both buttons emit `queue_cancel_requested`.
- **The not-inserted badge sits in the queue header** (owner's request
  2026-10-09): `set_not_inserted_badge(text, tooltip)`, "" hides it. The
  controller writes "N not inserted · <re-paste hotkey>" ("· tray menu"
  while no hotkey is registered) and the how-to as its tooltip. Amber
  (`#ffb300`, dark bold text), apart from every state colour -- the
  warning's burnt orange included, 3:1 or more in luminance (the bullet on
  the warning above) --, and in the
  queue panel because no `set_state` touches that panel: the next
  recording's Listening cannot paint over it, while its waiting rows keep
  the panel visible. Layout: [title (eliding, the stretch)][badge][Clear
  queue]; the badge is exactly as tall as Clear queue
  (`_fit_buttons_to_font`) and its minimum width is pinned to 1 px, so
  showing it changes neither the header's height nor the overlay's width --
  only the title gets narrower (it may elide at 11.25 pt and up). With the
  badge shown the title leaves the waiting count out (`_queue_title(...,
  badge_shown=True)`). Measured with real widgets, Listening, one
  transcription and two waiting rows, badge "2 not inserted · Ctrl+Alt+F10":
  9 pt overlay 470x234 and header 20 px with and without it, badge 173x20;
  11.25 pt 503x242, header 22, badge 218x22; 13.5 pt 560x268, header 26,
  badge 258x26.
- **A dragged overlay is clamped from where the user put it**, not from
  `self.pos()` (a tall result pushed it up for good). The remembered position
  and `_manual_positioned` have one writer, `_claim_manual_position`. A
  configured corner is recomputed from the screen each time.
- **On settings save use `OverlayUI.apply_corner_setting`** (moves only when
  the corner changed); never `move_to_corner` unconditionally. A drag claims
  the position on first movement, and `_reposition_within_current_screen`
  returns early while `_drag_active`, or startup updates snap it back.
- **Reveal after a result** (`_reveal_overlay_result`): briefly on success
  (`OVERLAY_RESULT_REVEAL_MS`), longer on errors (`OVERLAY_ERROR_REVEAL_MS`),
  best-effort; tray "Show overlay" (`controller.bring_overlay_to_front`) is
  the manual path. A floating overlay is a tool window and can hide.
- **Opening Settings never reveals the overlay** (2026-10-23). Nothing in the
  settings open path (`main.open_settings_dialog`, `_present_dialog`,
  `SettingsDialog`) touches the overlay; what did was the tray's `Trigger`,
  which the shell sends twice around the `DoubleClick` that opens Settings.
  `main._create_tray_icon` now waits out the double-click interval before a
  single click reveals the overlay (`windows-platform.md`). A *pinned*
  overlay is topmost and stays above the dialog where they overlap, by design
  of Pinned, with or without any reveal (measured with the fix, so with no
  reveal call: z-order overlay, dialog, window); only a floating one was moved
  by this. Whether Pinned should step behind a
  dialog the user just opened is an open design question (owner).
- **No delayed writer paints over Done or Error** (they hold the transcript
  and the only Retry/Insert recovery). `_on_preload_progress_poll` (600 ms)
  reads `OverlayUI.state`; `FakeOverlay` in `tests/conftest.py` mirrors it.
  The poll's one repaint of an Error is the orange offer the painter itself
  wrote (`_offer_painted`, informational paints only); a red failure over
  that offer is left alone. One exception, scoped to its own result: the
  paste target check's doubtful report (`_report_paste_outside_text_field`)
  replaces the "Done" its own paste painted, or an idle overlay, and only
  while that exact `(state, detail)` is still shown
  (`_PasteCheck.overlay_shown`); another job's Done or Error -- a queued
  paste paints nothing, a re-paste may keep another text's offer -- is never
  painted over, and the tray carries it.
