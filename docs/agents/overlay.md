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
  `_apply_window_flags` calls `setWindowFlags` only when pinning flags change
  (it recreates the window); reveals use `_apply_native_z_order`
  (`HWND_TOPMOST` / `HWND_NOTOPMOST`), falling back to a temporary
  `WindowStaysOnTopHint`; a recording start confirms `ensure_compact_size()`
  before and after its event drain. The Language button owns its `QMenu`
  popup and a centred chevron; never `QPushButton.setMenu()` (misaligned
  indicator; a pixel test checks the chevron). The Transcription-tab model
  runtime note reserves three lines (gray note for faster-whisper).
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
  tooltip), so a label that changes in place never changes a row's height
  (wrapped, the long "Possibly inserted, check the window" label grew rows
  from 32 to 40 px and the overlay from 230 to 246 px). The undelivered row
  differs by colour only
  (`QLabel[queueRowKind="undelivered"]`). The title counts the two apart
  (`_queue_title`: "Transcribing N files", "N transcripts not inserted", or
  both joined by " · "). Both buttons emit `queue_cancel_requested`.
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
