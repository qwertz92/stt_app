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
  - Not covered: touch and pen taps (a digitizer, not the mouse usage page;
    unmeasured) and typing without a click, which leaves the overlay above.
- **The Language and microphone menus are `_RebuildableMenu`s, rebuilt only
  while hidden** (2026-10-03). `QMenu.clear()` deletes the actions under an
  open popup -- and one the user has chosen whose `triggered` has not run
  yet -- and `audio_devices_refreshed` or a settings load can land at any
  moment. A request while the popup is visible is kept and runs on the
  event-loop turn after `aboutToHide` (Qt hides first, triggers after, so an
  immediate rebuild would still delete the chosen action). The button caption
  and enabled state update at once; only the item list waits.
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
- **The queue panel has two row kinds** (2026-10-01): `(token, label)` or
  `(token, label, kind)`, kind `QUEUE_ROW_KIND_TRANSCRIPTION` (Cancel) or
  `QUEUE_ROW_KIND_UNDELIVERED` (Dismiss, a transcript that was not
  inserted). Every row button is fitted for both captions
  (`_QUEUE_ROW_BUTTON_CAPTIONS`), so kinds never differ in button width;
  every row label is one line (`ElidingLabel`, the whole label in its
  tooltip), and the controller puts each row's status ("Pending insert",
  "Not inserted", "Possibly inserted, check the window") before the
  provider, model or preview, so eliding never hides it; a transcription
  row names the language its recording was made in (`EN`, `DE`, `Auto`: the
  job's own snapshot, not the current selection) as a short code right after
  the time, before the provider (2026-10-09); a label that
  changes in place never changes a row's height
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
  (`#ffb300`, dark bold text), apart from every state colour, and in the
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
- **No delayed writer paints over Done or Error** (they hold the transcript
  and the only Retry/Insert recovery). `_on_preload_progress_poll` (600 ms)
  reads `OverlayUI.state`; `FakeOverlay` in `tests/conftest.py` mirrors it.
  One exception, scoped to its own result: the paste target check's
  doubtful report (`_report_paste_outside_text_field`) replaces the "Done"
  its own paste painted, or an idle overlay, and only while that exact
  `(state, detail)` is still shown (`_PasteCheck.overlay_shown`); another
  job's Done or Error -- a queued paste paints nothing, a re-paste may keep
  another text's offer -- is never painted over, and the tray carries it.
