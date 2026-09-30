# Overlay: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
`overlay_ui.py` and every controller path that paints the overlay. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **Overlay changes of one event go through `batched_update`**: most
  transitions touch the queue panel *and* the state text (a finished
  transcription clears its queue row, then publishes the transcript). Applied
  separately the window resized twice — measurably 183 → 137 → 269 px — and the
  frame in between showed the previous content at the already-changed size.
  `OverlayUI.batched_update()` defers the geometry to the end of the block and
  resizes with painting suppressed, then repaints, so size and content land
  together. The controller wraps its Qt slots via `_overlay_batch()`
  (`toggle_recording`, transcription ready/failed). Geometry stays synchronous
  outside a batch, so direct `set_state` calls still resize immediately.
- **`show_idle_status` never overwrites a live session**: the preload
  completion arms `singleShot(..., show_idle_status)` when nothing is running,
  but the timer fires 1.2-1.8 s later — long enough for the user to have
  started dictating. The overlay then showed "Idle" during an active capture,
  and pressing the hotkey again to "start" actually stopped it mid-sentence.
  `show_idle_status` therefore re-checks `_overlay_session_active()` at fire
  time; any new delayed overlay writer must do the same.
- **`show_overlay_error` never paints over a live session** (wave 12). It
  had no session guard, and the wave-11 refusal report of the overlay's
  own controls took that road: the opacity slider and the pin button stay
  enabled while recording, so a save refused by a locked `settings.json`
  mid-dictation replaced "Listening" with "Error" while the microphone
  kept recording underneath, and in batch mode nothing repaints
  "Listening" before the stop -- the same for "Processing" over a
  transcription in flight, whose real result then overwrote the Error a
  moment later (the wave-12 reach lens; the tray's "No transcript
  available to copy yet." and the re-paste refusals painted over a live
  session the same way). While `_overlay_session_active()` the error is
  logged and handed to `busy_overlay_error`, which
  `main._connect_tray_notifications` shows as a tray notification under
  the app's name, beside the two reports that already took that road;
  outside a session the call paints as before, keeping a pending insert
  offer. `show_overlay_notice` keeps dropping its confirmation during a
  session -- a success confirmation is not worth a tray balloon, an
  error is. The wiring lives in its own function so a test pins it by
  emitting the signals at a fake tray.
- **Overlay window resizes go through `_resize_window`**: `QWidget.resize`
  clamps to the widget's *current* minimum size, and that minimum is only
  recomputed when the layout is activated (normally deferred to the next event
  loop pass). Right after shrinking the detail/queue areas the window still
  carried the previous state's larger minimum, so the resize was silently
  swallowed and a short error after a long transcript kept the expanded height.
  `_resize_window` activates both layouts first; never call `self.resize(...)`
  directly for the overlay window. The styled container's stylesheet border
  becomes part of its contents margins, so every size computation adds
  `_container_frame_margins()` and `set_state` applies the state stylesheet
  *before* measuring — without that the computed target was 2 px below the real
  layout minimum and `OVERLAY_MAX_HEIGHT` did not hold.
- **Overlay primary action and shared action slot**: the header starts with the
  Record/Stop button (`record_toggle_requested` → `controller.toggle_recording`)
  so dictation can be started without a keyboard; its caption swaps between two
  fixed-width captions and the recording state is a stylesheet property, so
  neither changes the layout. Its state indicator is a *generated icon*
  (`_OverlayRecordButton`), not a caption glyph: "●"/"■" sit on the font
  baseline, so the dot rendered 1.5 px below the button's middle, the square
  1 px, and the indicator jumped because the glyphs differ in height. Qt lays
  an icon out vertically centred; the icon carries its own trailing gap
  because Qt otherwise places icon and caption almost flush. The button keeps
  the same fill as its neighbours — a lighter fill reads as a permanent hover
  state — and is marked as primary by a brighter border only.
- **The header's two button groups are kept equally wide, and that is what
  centres the status text**: the header is
  `[Record][Pinned] <state label> [Clear][Copy]`, and the label is its only
  stretching item, so Qt gives it exactly the span the four fixed-width
  buttons leave over and `AlignCenter` centres the text in *that span*. That
  span's midpoint is the header's midpoint only while the two groups are
  equally wide, and they were not: 78 + 6 + 74 = 158 px on the left against
  64 + 6 + 64 = 134 px on the right put every status word 12 px right of the
  overlay's centre line in every state — 7 px until the 78 px Record button
  replaced the 68 px History button as the first item, so it was never
  centred and got worse. `_balance_header_flanks` runs once in `__init__`,
  before the header layout is filled, and widens the narrower group's buttons
  until the totals match (Clear and Copy are 76 px each). Measured after:
  0.0 px offset in every state, in both pin modes, with and without the
  queue, and the overlay is still 470 px wide because the header's sizeHint
  stays below the controls row, which is the row `_target_window_width`
  actually takes.
  Three properties are load-bearing:
  - **It measures the width `setFixedWidth` pinned, not `sizeHint()`.** A
    pinned width is often deliberately below the style's natural width, so
    `sizeHint()` would discard the constant. For a button that is *not*
    pinned, `minimumWidth()` is the style minimum instead — near zero — so
    its group measures too narrow and the deficit is spread evenly over its
    members, pinning that button under its own caption. It takes a group of
    two to see that (with one per group the wrong number cancels out), the
    flanks still come out equal, and asserting the precondition afterwards
    cannot catch it either because the balancing itself calls
    `setFixedWidth`. Hence the `sizeHint()` fallback plus a warning rather
    than a silent wrong number, and a test that drives exactly that shape;
    raising instead would trade a clipped caption for an overlay that does
    not open. Any later `setFixedWidth` on Record, Pinned, Clear or Copy
    still reinstates the offset — nothing does today (the runtime syncs
    change only caption, icon, tooltip, enabled state and stylesheet
    properties), and the regression test drives all of them.
  - **Do not replace this with a spacer.** An 18 px spacer between the label
    and Clear yields the identical label span, but the visible clear space
    around the text then reads 28 px left against 52 px right, and the
    constant has to be re-derived by hand on every button-width change.
    Moving Pinned to the controls row is worse still: it puts the text 28 px
    off centre the other way *and* widens the overlay by ~80 px, because the
    controls row is what sets the window width.
  - **Equal flanks centre the text only because the container's horizontal
    margins are symmetric** (1 px frame + 14 px layout margin per side).
    `test_the_status_text_is_centred_on_the_overlay_in_every_state`
    therefore measures the *painted* glyph pixels against the container's
    centre instead of asserting the button arithmetic, and separately pins
    the label's rectangle as identical across every state, error action,
    hover, press, copy-feedback swap, queue change and pin mode — so a
    symmetric reflow that keeps the text centred while moving it still
    fails.
- **Overlay `set_state(copy_text=...)`**: when the detail area shows more than
  the transcript — an insertion error followed by the transcript preview — the
  Copy action must still yield exactly the transcript. A failed insertion shows
  the transcript because it is otherwise invisible until it is inserted again,
  and the Error state scrolls to the top so the reason stays in view.
- **Compact overlay states grow to fit their text**: Idle/Listening/Processing
  used to pin the detail area to `OVERLAY_DETAIL_MIN_HEIGHT`, which silently
  clipped anything longer than two lines — most visibly the startup hotkey
  notice, the one message a user must be able to read. Compact now sizes to its
  content up to `OVERLAY_COMPACT_DETAIL_MAX_HEIGHT` and adds only the overflow
  to the compact window height, so short status text still produces exactly the
  captured compact size `ensure_compact_size()` relies on.
- **Overlay must never re-wrap or blink**: the transcript label wraps at a
  width derived from the target window width (never the live scroll
  viewport, which changes with deferred queue resizes and scrollbar
  visibility) and pre-measures the scrollbar case; `_apply_window_flags`
  calls `setWindowFlags` only when permanent pinning flags actually change
  because it recreates the native window. Temporary foreground reveals first
  use `_apply_native_z_order` (`HWND_TOPMOST` / `HWND_NOTOPMOST`) without
  changing Qt window flags; if that native call fails, use a temporary
  `WindowStaysOnTopHint` fallback rather than leave a floating overlay hidden.
  Successful recording start confirms `ensure_compact_size()` before and after
  its bounded Qt event drain; pending layout work can otherwise leave the
  previous expanded result geometry visible.
  The overlay Language button owns its `QMenu` popup and a centered chevron in
  a reserved right-hand zone. Do not use `QPushButton.setMenu()` here: the
  native Qt/Windows menu indicator can be vertically misaligned under the
  overlay stylesheet. Its regression test renders the button and verifies that
  the chevron pixels remain inside that zone and centered on it.
  The Transcription-tab model runtime note keeps a reserved
  three-line area and shows a neutral gray note for faster-whisper models so
  model switches never shift the layout.
- **Pinned overlay button sizes are measured, not constants**
  (`OverlayUI._fit_buttons_to_font`). Windows' Accessibility > "Text size"
  raises the application font's point size *without* changing the DPI, so
  Qt's device-pixel-ratio does not scale a pixel constant with it. Measured:
  at 11.2 pt Record needs 82 px against its pinned 78 and Reset Pos 80 against
  74; at 13.5 pt nine buttons clip and every one of them is 4 px too short; at
  18 pt Record needs 108x34 against 78x24. At the default 9 pt everything
  already fits, so the shipped layout is unchanged. Two ordering rules:
  - **It runs after the first `set_state`**, because only then does the
    container carry its stylesheet, whose padding and border a button's
    `sizeHint` includes -- measuring in the constructor came out 1-2 px short
    at every scale.
  - **`_balance_header_flanks` runs last, from the sizes that pass sets.**
    Balancing first and then widening one group is exactly the 12 px offset
    that balancing exists to remove.
  Each entry lists every caption its button can ever show, because a caption
  swap must not reflow its row; Clear is deliberately single-caption, and the
  Cancel/Retry/Insert slot is sized for the widest of the three.
- **A dragged overlay is clamped from where the user put it, not from where it
  currently is.** `_reposition_within_current_screen` clamped `self.pos()`, so
  a result tall enough to run off the bottom pushed the window up to keep it
  on screen and every later state started from the pushed-up position:
  measured on a 1392 px screen, an overlay dragged to y=1233 came back at
  y=1097 and stayed there, and the loss is whatever the tallest state ever
  shown costs (up to `OVERLAY_MAX_HEIGHT`, more with a queue). The remembered
  position and the `_manual_positioned` flag are one fact in two fields and
  `_claim_manual_position` is their only writer -- a caller that set just the
  flag left the previous drag's anchor behind. A configured corner is still
  recomputed from the screen every time.
- **Overlay corner vs. dragged position**: after a settings save, apply the
  corner through `OverlayUI.apply_corner_setting`, which repositions only when
  the configured corner changed. Never call `move_to_corner` unconditionally
  on save; it would discard a manually dragged overlay position.
  A drag claims the manual position on its **first movement**, not on mouse
  release, and `_reposition_within_current_screen` returns early while
  `_drag_active`. Startup keeps updating the overlay (preload progress,
  "Model loaded", the idle status) and every such update repositions a
  not-yet-manual overlay back to its configured corner — so with the claim
  deferred to the release, dragging the overlay during startup made it jump
  out from under the cursor.
- **Overlay reveal after a result**: a floating (non-pinned) overlay is a tool
  window (no Alt+Tab) and can hide behind other windows. The controller calls
  `_reveal_overlay_result` after a finished transcription — briefly on success
  (`OVERLAY_RESULT_REVEAL_MS`) and longer on errors/insertion failures
  (`OVERLAY_ERROR_REVEAL_MS`) so the transcript can still be copied. A tray
  "Show overlay" action (`controller.bring_overlay_to_front`) is the manual
  escape hatch. Reveals are best-effort (wrapped so a missing overlay method
  never breaks delivery).
- **No delayed overlay writer paints over a finished result.**
  `show_idle_status` already re-checked `_overlay_session_active()`; the
  preload progress poll did not, and it rewrites the overlay every 600 ms. A
  `Done` carries the transcript and an `Error` carries the reason plus the
  Retry or Insert action that is the only way to recover the recording, so
  neither may be replaced by a loading line. `_on_preload_progress_poll` reads
  `OverlayUI.state` -- the state name last passed to `set_state`, exposed as a
  property for exactly this -- and `tests/conftest.py`'s `FakeOverlay` mirrors
  it, because the controller tests never touch the real overlay.
