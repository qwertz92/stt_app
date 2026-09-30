# Settings dialog: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
the `settings_dialog*.py` modules, their tabs, sizing, save/merge and unsaved-changes logic. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **Settings dialog is a mixin facade**: `settings_dialog.py` composes
  `SettingsDialog` from per-tab mixins in `settings_dialog_*.py`
  (`_GeneralTabMixin`, `_AudioTabMixin`, `_LocalModelsMixin`, `_BenchmarkMixin`,
  `_RemoteProvidersMixin`, `_HistoryTabMixin`, `_ImportTabMixin`,
  `_PersistenceMixin`) plus shared code in `settings_dialog_helpers.py`. Rules to
  keep intact: Qt `Signal`s stay on the `QObject`-derived `SettingsDialog`
  (mixins are plain classes and only use `self.<signal>`); every method reaches
  peers/attributes through `self`, so scattering across mixins is safe. The
  module's public names must remain importable/patchable as
  `stt_app.settings_dialog.<name>` — tests monkeypatch there — so the facade
  re-exports them (guarded by `__all__`). The six external functions the tests
  patch (`run_benchmark_cases`, `_scan_cached_models`,
  `start_model_download_process`, `delete_cached_model`,
  `estimate_cached_model_bytes`, `cleanup_incomplete_model_download`) are called
  through a lazy `_facade()` accessor (`_facade().<name>(...)`) in the
  local/benchmark mixins so the patch target still resolves after the split. The
  accessor imports the facade lazily (not at module scope) so a mixin can be
  imported directly without an import cycle.
- **Transcription tab hosts daily-use settings; capture setup lives on Audio**: the Transcription tab kept growing until it needed its own scroll
  marathon, so the set-and-forget capture groups ("Audio && Voice Detection"
  and "Recordings") moved to a dedicated Audio tab
  (`settings_dialog_audio.py`). That split was not enough: with Hotkeys,
  Display, Engine && Mode and Text Insertion, the tab still needed 1342 px
  and scrolled by 283 px on a 1392 px screen (measured 2026-09-18; it was the
  only tab that scrolled, and a smaller screen scrolls by more). Hotkeys and
  Display therefore moved to a Hotkeys && Display tab
  directly after it (`settings_dialog_hotkeys.py`), which left the tab
  at 781 px: Engine && Mode and Text Insertion, what actually changes during
  daily dictation. On 2026-09-27 the ONNX Device row and "Keep ONNX model
  loaded" moved to a "Local runtime" group on the Models tab and the static
  hints under several fields became tooltips or placeholders, which took the
  content to 654 px at 9 pt (738 at 11.25 pt, 819 at 13.5 pt). "History Time" left the Display group for the History
  tab's top row ("Time Zone"), because it changes nothing but how that list
  prints its timestamps; the entry count beside it became an `ElidingLabel`,
  since as a plain label its full text width put the row at 689 px against
  the 585 px viewport the dialog's 611 px minimum width leaves (measured:
  560 px content minimum and no horizontal scrollbar after). Widget
  attribute names are unchanged, so persistence and the controller are
  unaffected. Build order:
  `_build_general_tab`, `_build_hotkeys_tab`, `_build_audio_tab` -- the last
  one applies the shared label column across all three form tabs
  (`_general_forms`, `_hotkeys_forms` and its own two). An eighth tab takes
  the tab bar from 660 to 797 px of the 840 px the default dialog width
  gives it at 9 pt; a ninth would not fit without scroll arrows.
- **Tab titles say what the tab is for** (2026-09-20): Transcription (was
  General), Hotkeys && Display, Audio (was Audio && Recording), Models (was
  Local), API Keys (was Remote), History, Import Audio, Benchmark. The
  model that runs is chosen on the first one, while Local only managed
  downloads and Remote only held keys, which the old names hid. Module,
  mixin and attribute names are unchanged (`settings_dialog_general.py`,
  `_local_tab_index`), this file uses the new titles throughout, and
  `docs/learning-log.md` keeps the names of its day. The tab bar needs
  771 of the 840 px the default width gives it at 9 pt (pinned by a test).
  The local runtime note ends with "Download or remove local models on the
  Models tab." and the remote model note with "The API key is set on the
  API Keys tab.", both inside height they already reserved. The
  `settings_timing` log names a tab by its visible title, so it reads
  `tab=Models` now.
- **The vocabulary field says when the selected model ignores it**
  (2026-09-20). `config.supports_custom_vocabulary(engine, model)` is the
  single answer, derived from `LOCAL_MODEL_RUNTIME` and
  `CUSTOM_VOCABULARY_ENGINES`, and pinned against the factory: a recorder
  stands in for every transcriber class and the test asserts the answer
  equals "the constructor received `custom_vocabulary`" for every engine
  and every local model. The note under the field is reserved (42 px,
  worst case 30 px at the minimum width), amber when the model ignores the
  vocabulary and names it the way the screen does; the field stays
  editable, because the user may switch models later. Cost, measured: the
  Transcription tab's content was 960 px at the default width (916 px
  without the note), so it scrolled on a screen whose available height is
  below about 1139 px; 654 px since the 2026-09-27 move described above.
- **`_configure_combo_popups` runs after the root layout is built.**
  `findChildren` walks the parent tree, and before `self.tabs` was parented
  it reached 3 of the 21 combos, none of them `model_combo`. With Qt's
  defaults the popup is sized by summing the first `maxVisibleItems`
  *entries*, a separator counts as one and is 2 px tall, so a separator
  inside that window made the popup up to 28 px short of its rows, and
  `ScrollPerItem` left that remainder blank under the last model (field
  report: "an empty white strip"). Measured by the number of downloaded
  models: 2 px for 1-4, 28 px for 10-13, 0 otherwise. Every combo gets
  `maxVisibleItems(12)` and `ScrollPerPixel`; `setUniformItemSizes(True)`
  was rejected because it makes the separator a full row.
- **An Audio-tab value that is only read while its checkbox is on is
  disabled while it is off** (VAD threshold, start tone, completion tone):
  `toggled` is connected to `setEnabled` and the state is synced once at
  build time, because populating an unchecked box with `setChecked(False)`
  emits nothing. **The silence-gate threshold is deliberately not linked**:
  streaming reads it for its pause handling even with the gate switched off.
- **Model selection is unified on the Transcription tab; Models tab is management-only**:
  "what do I use" (engine, model, language, mode) all live in the Transcription tab's
  "Engine && Mode" group box. A single "Model" form row hosts a
  `model_selector_stack` `QStackedWidget` with page 0 (`model_combo` plus
  `local_model_runtime_warning_label`) for the local engine and page 1
  (`remote_model_provider_label`/`remote_model_combo`/`remote_model_note_label`)
  for remote engines; `_update_remote_model_selector` flips the page via
  `_update_model_selector_page` whenever the engine changes.
  `QStackedWidget.sizeHint()` already reflects the largest page regardless of
  the current index, so switching pages never shifts the rows below. The Local
  tab keeps Model Dir, cached-model inventory, scan/refresh, download queue,
  and delete only, with a short gray note pointing to the Transcription tab for the
  active model.
- **Qt dialog feedback and refresh state**: transient button text such as
  "Copied" must reserve enough width for all feedback states via
  `ui_feedback.py` so layouts do not jump. Dialog/list refreshes should preserve
  selection, current item, and scroll position when the same entry still exists;
  use the shared scroll helper instead of rebuilding lists in a way that resets
  the user's place. Settings tabs use a session-stable default dialog size and
  `QScrollArea` `AdjustIgnored` to avoid small tab-switch resize jitter. Inline
  field buttons match the corresponding input height via
  `_match_field_button_height`, which also tags them with the
  `inlineFieldButton` stylesheet property: the dialog-level base QPushButton
  rule has a larger QSS box (min-height + padding) that would otherwise beat
  the fixed height once the button is reparented into the styled dialog and
  render it taller than its field or clipped at the bottom. Action rows keep
  explicit spacing rather than relying on platform defaults. Settings tab selection must
  not change tab font weight or measured tab width; use color/border changes for
  the selected state. Transcription, Hotkeys && Display and Audio form
  sections share one measured label column (applied by `_build_audio_tab`
  after all three tabs exist)
  so fields align across group boxes and when switching between the two tabs. Pressing Save with no effective setting or
  API-key changes must not emit `settings_changed`; otherwise the controller can
  reload or preload local models unnecessarily. The Benchmark tab hosts the
  *viewing* side directly (viewing results/history is frequent, running a
  benchmark is rare): a compact header row ("Run Benchmark..." button plus a
  fixed-height live status label) above the History/Results vertical splitter.
  The *run* side (audio sample picker, installed-model list with one compact
  row of small Select all/Deselect all/Refresh buttons, collapsible Run
  Options, Run/Cancel controls) lives only in the resizable, non-modal
  `benchmark_window` ("Run Benchmark", 860x880 default bounded to the
  available screen so expanding Run Options keeps the model list usable,
  owned by the settings dialog so it hides when the dialog closes). Re-clicking the button raises/activates
  the existing window rather than creating a second one via
  `_open_benchmark_window`, which also refreshes the model list. Status is set
  through the single `_set_benchmark_status`, which feeds both the tab label
  and the window's own status line. Benchmark Results tables use per-pixel
  scroll modes. The Local Models group and its inventory list expand into
  available vertical space instead of leaving an unusable blank area below the
  group. All benchmark widget attribute names are unchanged; only
  containers moved. Completed and partial canceled runs are saved to Benchmark
  History automatically; Export only creates a shareable file. New
  faster-whisper results store CTranslate2's resolved device instead of `auto`.
- **The settings dialog's minimum width is the widest of the tab bar, every
  settings page and the Benchmark page -- measured on the tab widget and its
  pages, never on the dialog, and never past the screen.**
  The explicit 520 px minimum predates the Benchmark tab's third History
  action button, which took that tab's minimum to 611 px; at 520 every
  caption in that row was clipped, and the last one clears only at 611.
  Two Qt facts decide where the pin runs. `QTabWidget.minimumSizeHint` is the widest of *all* its pages
  whichever is current (measured: a bare tab widget with 100 and 700 px
  pages answers 706 on either), so the 581 the dialog answers before the
  Benchmark tab has been painted is that page's unpainted 555 plus the tab
  widget's frame and the root margins, and every other page's 72 px never
  counts -- this entry said "the tab bar" and "follows the current page
  only" for one round, and both were wrong. And the Benchmark page reports
  555 px until it has been painted on screen and 585 afterwards -- two
  stale caches, not the splitters. The two action rows' `QBoxLayout`
  minimums were computed before their buttons were polished, 6 px per
  button short (the Results row 306 -> 324 with three buttons, the History
  row 511 -> 541 with five), and `QLayout::invalidate()` alone refreshes
  them with the dialog still hidden; the page layout's `QWidgetItem` for
  each splitter caches the old 535 until the splitter's own
  `updateGeometry()`, and a splitter has no layout of its own, so that
  arrives only through the `LayoutRequest` Qt posts to a *visible* parent
  -- which is why the show moves the number. "A splitter counts visible
  children only" was this entry's guess for one round: it does, and none
  is ever hidden here (measured `isHidden()` False in both states). So the
  Benchmark page's share cannot be measured at construction:
  `_pin_content_minimum_width` runs at construction for the tab bar and the
  scroll pages, and again 0 ms after every show and every tab switch; it only
  ever raises the minimum, down to the screen. Both roads are needed: a tab
  made current while the dialog is hidden measures the unpainted page. Five
  properties, each wrong once:
  - **A `QScrollArea` answers a fixed 58 px as its minimum whatever it
    holds**, so the tab widget's hint never saw the seven scroll pages: at
    the old 611 px minimum Transcription scrolled sideways by 38 px, Hotkeys
    && Display and Audio by 36 and Models by 124 (9 pt).
    `_content_minimum_width` takes each page's content minimum plus its
    vertical scrollbar and frame, and the tab bar's size hint, because below
    771 px at 9 pt the bar hides tabs behind scroll arrows and the tab a note
    sends the user to may be the hidden one. Measured after (2026-09-27): a
    minimum of 797 px at 9 pt, 907 at 11.25 and 1020 at 13.5, with no page
    scrolling sideways at any of them; at the two larger sizes the default
    width follows the minimum.
  - **It measures `self.tabs.minimumSizeHint()` plus the root layout's
    margins, never the dialog's own hint.** The root layout also holds the
    bottom status line, whose text after a failed save is the whole
    exception message, and the first version read the dialog's hint while
    such a message showed: a tab switch or a close-and-reopen inside the
    three seconds the message stays pinned its width for the life of the
    app -- 3077 px for a real failed save on a 2560 px screen, 1360 for a
    172-character `WinError 5` -- with the message long gone and no way to
    drag the dialog back. (That line is an `ElidingLabel` as well now; see
    the status-line entry.) The engine line is the other root-level label;
    a test sets a 400-character text on both and expects the pin unmoved.
  - **It stops at `_available_dialog_size().width()`.** A minimum the
    screen cannot host puts Save and Close past its edge with no way back
    short of restarting the app, and `setMinimumWidth` holds whatever
    `_apply_initial_dialog_size` fitted before it. That includes a minimum
    an earlier, wider screen allowed, which it lowers: the dialog lives as
    long as the app and may be shown on another monitor.
  - **The 640 px budget the test bounds the need with is a 9 pt number.**
    Windows' "Text size" raises the application font without the DPI:
    measured 720 px at 11.25 pt, 813 at 13.5, 917 at 15.75 and 1025 at 18,
    so off the 9 pt font the test skips and names the need it measured.
  - **No word-wrapped label may set it** (`_let_wrapped_labels_narrow`,
    right after `_build_ui`). A wrapped `QLabel` reports its longest word as
    its minimum width, so once the scroll pages counted, one unbroken token
    in a status line -- a path in the Import tab's "Selected:" line, a URL
    in a provider's error, a folder in a download failure -- raised the
    minimum for the life of the app (1538 px for a 346-character path; the
    probe found 25 such labels on seven pages, not only on History and
    Import). Every wrapped label on every page gets an explicit minimum
    width of 1 px, which replaces the hint in every layout
    (`qSmartMinSize`), so such a token is cut off at the label's edge
    instead of moving anything; ordinary words never reach that edge, and
    the page minimums were measured unchanged at 9 and 13.5 pt. The Import
    tab's path line carries the path as its tooltip, since its end is the
    file name, and so do the three status lines that report paths and
    provider errors (`WrappedStatusLabel`: the Models tab's action line,
    the key-storage line and the connection-test result). A label built
    after `_build_ui` is not covered.
  At 9 pt the tab bar now sets the minimum (797 px) above the Benchmark
  page's 611, so opening that tab no longer widens the dialog; the 640 px
  budget in the Benchmark test bounds that page's own need (one label once
  took the layout's minimum to 1109 px, which is what the budget is for).
- **The Benchmark tab's status label and progress bar take their height
  from the button as it renders.** The build measured
  `open_benchmark_window_button.sizeHint()` before it was a polished child
  of the styled dialog, where its QSS box (min-height plus padding) is not
  applied yet: 26 px against the 34 it renders at, so both sat 8 px short
  beside it. `_pin_benchmark_header_row_height` re-pins them from
  `_reserve_feedback_button_widths`, the place that already re-measures
  that button for the same reason.
- **Unsaved changes are tracked by a fingerprint, and a close asks**
  (2026-09-27, `settings_dialog_unsaved.py`). The fingerprint covers every
  input on the settings pages, two pending states (remote models chosen for
  other providers, keys marked for removal) and two History inputs. Save is
  enabled only while it differs from the clean state, and the bottom line
  then reads "Unsaved changes" in amber. Close, Esc and the title-bar X ask
  Save / Discard / Cancel; a programmatic close and quitting the app never
  ask. The clean state is recorded after `_populate` and after every
  successful or no-change save, a key-only save cleans only what it saved,
  and the History import that moves the limit spin box is not an edit. It
  is not the baseline a save diffs against, which stays
  `_populated_settings` (the save-merge entries below): the fingerprint
  decides only the prompt and the Save button. Four rules the review of
  2026-09-27 added, each with a test that fails without it:
  - **No slider is an input.** None of these pages has one as a setting,
    and every scroll bar is one -- each page's own and the popup list of
    every combo box -- so scrolling a page read as an edit.
  - **A save that writes no settings still moves `_populated_settings`**
    (the no-change branch and a key-only save). Left behind, a value typed
    to match another window's write counted as an edit on the next save and
    was written back over whatever that window wrote in between (measured:
    800 over the History dialog's 300).
  - **A Discard while dialog-owned work runs puts the setting widgets back**
    (`_discard_unsaved_edits_while_busy`). It called `reload_from_store`,
    which waits while such work runs (next entry), so the edits stayed and
    the next Save wrote them. `_populate` is now two halves:
    `_populate_setting_widgets` sets only what the user could set by hand
    while the work runs, and the busy Discard runs it from
    `_populated_settings`; `_populate_views` holds what the work owns (the
    connection-test target and labels, the Import tab's pickers, the local
    inventory views, both history lists) and runs only in a full reload.
    The model combo is rebuilt from the inventory already known for the
    Model Dir. A Model Dir that the Discard put back is restored with its
    signals blocked, so `_on_model_dir_changed` then runs as typing the
    folder back would; without it the Models tab went on describing the
    discarded folder.
  - **Discarding typed keys refreshes the Import tab's credential note**,
    because the key fields are cleared with their signals blocked.
- **Settings dialog persists for the app lifetime**: closing Settings hides the
  existing dialog instead of deleting it. The dialog owns background model
  downloads, benchmark work, imports, scans, and connection/update checks, so
  recreating it while an old worker was alive could start overlapping work and
  discard the only UI tracking that worker. Reopening reloads stored settings
  into the same object before showing it. Every hide path, including
  `QDialog.reject()`, also hides the independent `Qt.Window` benchmark dialog.
  Reopening while idle reloads stored settings and discards unsaved provider-key
  edits; while dialog-owned work is active, that reload is deferred so the
  operation's snapshotted controls and busy state stay intact. Application
  shutdown calls `SettingsDialog.shutdown()` before controller shutdown so
  active model-download and benchmark child-process work is canceled and given
  a bounded cleanup window -- and then delivers the events posted to the
  dialog (`QCoreApplication.sendPostedEvents(self)`, on the main thread
  only): `shutdown()` runs from `aboutToQuit`, after `exec()` has returned,
  so the queued signals through which the joined benchmark worker hands over
  its cases and its outcome had no loop left to deliver them, and a run
  cancelled by quitting saved nothing (measured: 0 history entries) while
  the dialog still believed it active. What the join did not see -- a
  worker still running after its 2.5 s -- is still not saved.
  **And that delivery starts nothing and shows nothing.** `sendPostedEvents`
  hands over every slot posted to the dialog, not the benchmark's alone. A
  finished download's slot refreshes the inventory, and the scan that asked
  for started a worker thread and a child process from `aboutToQuit`, with
  nothing left to join either (measured: the scan subprocess launched after
  `shutdown()` had returned); and the update check's slot -- its thread is
  not one the join covers -- ends in a modal `QMessageBox.exec()`, which held
  `aboutToQuit` until the box was closed (measured: 0.42 s with a timer
  closing it; for a user, the quit waits on a dialog about updates).
  `_request_local_model_scan` refuses after `_shutdown_started`, as
  `_start_local_model_download` already did, and `_on_update_check_finished`
  shows no dialog then. Both roads were opened by the delivery itself
  (`6cf8cbd`, wave 9).
- **No stylesheet in the settings dialog sets `font-size`** (2026-09-27): a
  pixel size ignores Windows' "Text size", which raises the application
  font without the DPI, so the hints stayed at 11 px while everything else
  grew. Hints and notes use `settings_dialog_helpers.hint_font()`, 0.92 of
  the application font (the same 15 px line at 9 pt).
- **Transcription/Audio-tab field hints have explicit visual ownership**: a control
  and its descriptive hint use `_field_with_hint` with a 2 px internal gap;
  these forms use a 10 px row gap before the next setting. Changing model/language
  notes reserve two fixed lines so engine switches never move later fields.
  Delayed paint/prewarm callbacks use dialog-owned `QTimer`s and must disappear
  with the dialog instead of invoking deleted Qt objects.
- **A changing status line is reserved or elided, never left to grow.**
  Three of them were not, and each moved something the user was pointing at:
  every API Keys-tab provider row's word-wrapped "Last test" label reserved one
  line where a failure message is two, so one failing provider moved 43 of 50
  widgets by 15 px and all seven pushed the "Run Connection Test" button
  105 px down; the Run Benchmark window's status label sits under the scroll
  area holding Run/Cancel, so each wrapped line lifted both buttons 16 px.
  Both now use the same two-line `_reserve_dynamic_hint_height` reservation as
  every other changing note, with the full message in the tooltip because two
  lines cannot hold a 300-character provider body. The Benchmark *tab's* label
  cannot wrap -- it shares a fixed-height row with a button -- and a plain
  `QLabel` then reports the full text width as its `minimumSizeHint`, which
  raised the settings dialog's own minimum width from 492 px to 1109 px while
  showing the leading 77% of the message with no ellipsis and no tooltip.
  `settings_dialog_helpers.ElidingLabel` is the answer to that shape: size
  policy `Ignored` horizontally so the layout never widens for it, elided to
  whatever width it is given and re-elided on resize, `text()` still returning
  the full string so callers and tests read what was set, and the whole
  message in the tooltip. **The dialog's own bottom status line is the
  fourth**: a failed save writes its whole exception message there, a plain
  label reported that message's width as its minimum hint, and the
  minimum-width pin read that hint (above). It is an `ElidingLabel` that
  takes the button row's leftover space with a stretch factor of 1 -- with
  the `addStretch(1)` it replaced still in the row, an `Ignored` width
  policy gets nothing and the text vanishes -- right-aligned so a short
  message still ends beside Save. **And it is one line whatever the text
  holds**: `ElidingLabel` elides the single-line form of its text
  (whitespace runs folded to one space), because a failed save's exception
  message can span several lines, and shown as such the label grew taller
  than the 34 px Save and Close buttons and moved both up -- 7 px at three
  lines and 8 px more per line after (measured on the shown dialog).
  `text()` and the tooltip keep the message as written. **And copying it
  yields the message as written**: `QLabel` copies from its own text
  control, which holds the elided text, so Ctrl+C on a failed save's
  message gave its first 37 characters and an ellipsis (measured) while the
  tooltip holding the rest cannot be copied. `ElidingLabel` handles Ctrl+C
  and its own context menu's Copy through `copy_message`: the whole painted
  text, or no selection, copies the message as written, and a part of it
  copies as selected.
- **A widget that appears mid-interaction keeps its space while hidden.**
  The Models tab's download progress bar appears the instant a download starts,
  and without `retainSizeWhenHidden` its 28 px left the layout: pressing
  Download pulled Download/Cancel/Delete up under the cursor -- with Cancel
  sliding into the place the pointer was on -- and pushed them back down on
  completion, shrinking the model list twice per download.
- **Multi-select lists use ExtendedSelection**: Shift selects ranges, Ctrl
  toggles, matching the file explorer. Do not reintroduce `MultiSelection`.
  The Run Benchmark window's model list is the one exception (2026-09-27): a
  checkbox per model and no selection, since a plan of cases needs a set
  that survives a click elsewhere; Space toggles the current row, and a
  rebuild keeps each model's check state (on the first fill every model is
  checked, a model that appears later starts unchecked).
- **Remote connection test persistence**: last-known provider connection test
  results live in `provider_connection_tests.json`, not `settings.json`, because
  they are diagnostic UI state rather than configuration. The API Keys tab should
  restore these labels on open and overwrite only the providers tested. Saving a
  new provider key or deleting a provider key must clear that provider's stored
  test result because the old result no longer describes the active credential.
- **Settings and credential saves are explicit and failure-safe**: toggling the
  insecure-storage checkbox changes only its pending UI until Save/Save API
  Keys. Failed key operations retain the typed value or pending delete and must
  stop unrelated settings/history mutations. Because credential backends are
  not transactional, any provider changed before a later failure still emits
  `settings_changed` to invalidate cached clients. Persist the settings file
  before trimming history; a failed settings write must never delete history.
- **Error text must be selectable**: Qt hands a `QMessageBox` only
  `LinksAccessibleByMouse`, so its text could be captured only by retyping it
  or screenshotting it. `dialog_style.install_selectable_message_text` installs
  one application-wide event filter that marks every message box selectable as
  it is shown. That is the only place that reaches the `QMessageBox.critical`
  and friends convenience statics, which build *and* show the box in a single
  call and give the caller no chance to configure it — do not "simplify" this
  into per-call-site changes unless every static is migrated first. Inline
  status/error labels use `dialog_style.make_label_selectable`, including the
  update dialog's status and details labels; the overlay detail label already
  carried the flags. Always OR the flags onto the existing ones: replacing them
  strips `LinksAccessibleByMouse` and makes the links in the update dialogs
  dead. The whole body of `make_message_text_selectable` is guarded, because it
  runs from an event filter and an exception there escapes the caller's
  `show()`.
- **A reservation is measured, not predicted**: `retranscribe_dialog` reserves
  a `heightForWidth`, so the candidate is chosen by measuring every candidate
  through the polished label. `key=len` is character count, not drawn width
  (`W` x29 draws 319 px against 159 px for the 30-character longest label);
  `key=horizontalAdvance` is drawn width, and what is reserved is a wrapped
  height, which a width ordering does not order. **This is live, not
  hypothetical**: for a `granite-speech-4.1-2b-nar` entry the advance key
  under-reserves by 15 px at label widths 594-606, i.e. dialog widths 678-690,
  well inside the shipped 560 px minimum. The band is narrow -- the candidates
  differ at 134 of 841 reachable widths there, 100-102 for other entries --
  which is why three hand-picked width lists missed it and one review round
  concluded it could not happen. Measuring at construction time is wrong
  regardless: `setStyleSheet` alone does not apply the font, so the label
  reports 9 pt and a 16 px line height until it is polished, 11 px and 15 px
  after. (That does not flip the advance key's *winner* for today's names,
  only the ordering below it.)
- **`QLabel.heightForWidth` is floored by `minimumHeight()`, and the
  reservation *is* that minimum.** `QLabelPrivate::sizeForWidth` ends in
  `.expandedTo(minimumSize())`, so reading through a label that already
  carries a reservation returns the reservation. Measured on a bare label,
  one identical call: 15 px at `minimumHeight() == 0`, 400 px at 400, 15 px
  again at 0. Two consequences, and both bit:
  - `_reserve_note_height` was a one-way ratchet. `max(...)` over readings
    that cannot fall below the installed floor can only grow, so narrowing
    the dialog and widening it again kept the taller note -- 6 px for `small`,
    30 px for a 63-char imported id, taken off the transcript view -- while
    `resizeEvent` documented the reservation as correct at every size. It now
    clears the floor before measuring.
  - **Every wrong claim this pair of entries has carried came from measuring
    that way.** "The identical call returned 60 px at label width 556 and
    90 px at 476" was the dialog's own reservation at those two widths, not
    impurity; a later sweep reporting all 15 candidates agreeing at every
    reachable width was one installed floor read 841 times; and the
    corresponding test asserted `needed <= reserved` against a `needed` that
    was `reserved`, which is why an advance-key shortcut survived mutation.
  `heightForWidth(w)` itself *is* pure in `w`, verified with the argument held
  fixed while the label's width varied. The two clamps around it are not:
  `minimumWidth()` raises the argument (`heightForWidth(200)` returns 120 at
  minimum width 0 and 60 at 600), `minimumHeight()` raises the result. The
  rule that survived all four versions of this entry: measure through the
  real, polished widget, at the width in question, with the previous
  reservation removed.
- **A settings-dialog worker thread that will not start rolls back what it
  claimed.** All six `Thread.start()` calls in the dialog write their busy
  marker first, and `RuntimeError` from a starved interpreter arrives after
  that; nothing clears the marker but the completion signal the thread will
  never send. Because `_background_work_active()` reads exactly those markers,
  the control stayed disabled *and* `reload_from_store()` was deferred
  silently for the life of the app -- the dialog is never recreated. Each arm
  undoes its own site: the connection test and the update check re-enable
  their controls, the model scan clears its marker, the benchmark drops its
  cancel event, the import routes through `_finish_import_transcription`, and
  the download queue reuses the teardown its own crash arm performs, which is
  what hands the coordinator's explicit interest back so partial files stay
  cleanable.
- **A save asks whether the file would change, not whether the snapshot
  differs.** The overlay writes `overlay_opacity_percent`,
  `overlay_always_on_top` and `language_mode` straight to the store while
  Settings is open, and both save paths deliberately read exactly those three
  back from the store -- so comparing the result against `_loaded_settings`,
  the dialog-open snapshot, reported a change this dialog had not made. The
  file was rewritten with the bytes already in it, the status claimed
  "Settings saved", and `settings_changed` cost four global hotkey
  unregister/re-register cycles. Both paths now compare against
  `self._settings_store.load()`. Note what this must *not* break: a key
  replacement leaves `AppSettings` byte-identical (the `has_*_key` flags do not
  move), so it writes no settings and still emits `provider_keys_changed`.
- **A rollback arm must undo the state it is rolling back, and say the thing
  the user's own action was about.** Three `Thread.start()` guards were each
  wrong in a different way, and all three only appear when the interpreter
  cannot create another thread:
  - The model scan's arm repeated half of `_on_local_model_scan_finished`
    instead of calling it, so it left the
    `_local_model_scan_started_at_by_token` entry (never popped again for that
    token) and the "Checking local model availability in the background."
    line, describing a scan that never began. It now routes through the
    completion slot with a non-list payload, which is that slot's own "did not
    finish" branch.
  - That arm also wrote `_set_local_models_action_text`, which belongs to the
    download -- and a download whose thread also failed finishes by refreshing
    the inventory, which starts the scan. So a user who pressed Download was
    told a *model scan* could not be started. The scan writes its own status
    line now, and the two labels have exactly one writer each.
  - The benchmark's arm left the Details overview reading the running summary,
    because `setPlainText` had already put it in the Status row a few lines
    above the start -- next to a status line saying the run never began.
- **A save applies the user's edits onto the file, it does not write its
  snapshot back.** The comparison above was only half of it: the dialog also
  *wrote* every field of its dialog-open snapshot, so a value another window
  had raised meanwhile was reverted. `history_dialog._persist_limit` does
  exactly that -- it writes `history_max_items` straight to the store and
  notifies only the controller, so Settings' spin box and snapshot keep the old
  number -- and an untouched Save then wrote it back *and* ran the follow-on
  trim at it. Measured: disk 800, spin box 500, 700 entries held, one Save with
  nothing touched deleted 201 transcripts. `_dialog_edits_over_stored` diffs
  the constructed object against `_loaded_settings` and applies only the fields
  that differ onto `self._settings_store.load()`, in both save paths, and the
  trim is keyed on the limit that was actually saved. This entry and the one
  above are the two halves of one rule: **the baseline answers "what did the
  user change", the file answers "what is true now", and neither question may
  be put to the other.**
- **That baseline is `_populated_settings`, and the whole merge above held for
  exactly one save until it existed.** `_loaded_settings` was answering both
  questions: "what the widgets were populated from", which the diff needs, and
  "what was last written", which the save assigns -- and a save assigns the
  *merged* object, so the snapshot absorbed the History dialog's 800 while the
  spin box went on showing 500. The next save read that 500 as a genuine edit
  and wrote it. Measured on the real `SettingsStore`: 700 entries, two saves
  with nothing touched between them, 200 transcripts deleted behind a prompt
  naming a limit the user never chose -- and with no prompt at all whenever
  the history was smaller than the limit. `_language_mode_for_save` has the
  same shape (its own snapshot read is now consistent but not independently
  observable; see its docstring).
  Four properties, each of which was wrong in some version of the fix:
  - **It is written in exactly three places, and each is a moment at which
    what the widgets show becomes settled**: `_populate`, the History tab's
    programmatic move of the limit spin box, and the end of a successful save.
    The first two are "a widget moved without the user"; miss either and the
    diff describes widgets that no longer exist.
  - **The save must re-record it.** Freezing it at the dialog-open snapshot
    fixes the revert and breaks the opposite direction: an edit could then
    never be taken back within one session, because the widget agrees with the
    dialog-open value again and counts as no edit. Measured on
    `tray_middle_click_toggle` -- set, save, set back, save, nothing written.
  - **`language_mode` is put back to what the combo shows when recording**, and
    only in `_save`. It is the one field `_construct_settings_from_widgets`
    fills from disk while a widget for it exists. The key-save path must *not*
    make that correction: it never reads the combo, so claiming the disk value
    as the baseline there swallows a pick the user had made but not yet saved.
  - **The key path lists its widget reads once**, and uses that one object for
    both the write and the baseline, so "a field this path did not touch is
    not an edit" holds by construction. Built on `_loaded_settings` instead it
    carries a previous save's merged values into a third window's newer ones.
  Nine mutations cover these; the one survivor is `_language_mode_for_save`'s
  own snapshot read, which the diff masks, and that is stated in its docstring
  rather than left to be rediscovered.
- **Three `settings_store.load()` calls decide one save, and that is safe only
  because nothing between them pumps the Qt event loop.**
  `_overlay_owned_settings`, `_stored_language_mode` and the change comparison
  each read the file. Every writer of those three fields is a direct-connected
  Qt slot on the main thread (`controller.set_overlay_opacity_percent`,
  `set_overlay_always_on_top`, `set_language_mode`, and both History views'
  limit writers), the single-instance guard rules out a second process, and
  `_construct_settings_from_widgets` contains no `exec`, `processEvents`,
  `QMessageBox` or `QFileDialog` -- so the three reads are one uninterrupted
  run and cannot see a mixture. An earlier version of this entry recorded that
  mixture as an accepted race; it is not reachable. What *would* make it real
  is adding any modal or `processEvents` between the reads, which is why the
  invariant is written down rather than the reads collapsed. The history-trim
  prompt is a modal and sits deliberately *before* all three.
