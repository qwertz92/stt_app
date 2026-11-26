# Settings dialog: design decisions

Binding rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30. Read before changing the `settings_dialog*.py` modules, their
tabs, sizing, save/merge and unsaved-changes logic. Entry order is kept, so
"the entry above/below" refers to this file; "Known limitations" is
`docs/agents/known-limitations.md`. History and full measurements:
`docs/learning-log.md` and git history.

Verbatim pre-condensation text: `git show e608f86:docs/agents/settings-dialog.md` (original AGENTS.md: `df2642a`).

- **Owner's clarification of the stability rules (2026-11-24)**: "nothing may
  jump" was never meant strictly. The goal is best-practice UI/UX: prevent
  UNEXPECTED movement; small shifts and movement the user caused are fine;
  stability is never bought with empty reserved space or with forced
  over-engineering; when stability and usability pull apart, usability wins
  and the trade-off is named. Read every "never moves", "reserves" and
  "retains" rule below in that light. Reservations that exist today stay until
  the code around them is touched for another reason; when one is touched,
  first ask whether what appears is below what the user is pointing at (then
  it needs no reserved room) and whether the reserved area is visible empty
  space on every visit (then it is paid for more often than the shift it
  prevents). The Models tab's group was the first one measured against this.
- **The Models tab's group is as tall as its content** (2026-11-24, owner:
  "there is a scrollbar although everything would fit; the 'Local models' area
  is too tall and has empty space"). Measured on a copy of his data at the
  default 860 x 960: the page scrolled (content 864 px, viewport 829) and the
  group, 577 px, had about 100 px of empty space under its buttons. Cause: the
  group had `Expanding` policy and stretch 1 and the list stretched inside it,
  and the status line (two reserved lines), the progress bar and the queue line
  kept their size while hidden. Now: the three status widgets take no room while
  hidden; the group (`ContentCappedGroupBox`) is no taller than its content (its
  maximum is its preferred height with the list at all its rows, recomputed on
  every layout request); and **the list is the flexible part**
  (`MinimumHeightHintListWidget`): minimum six rows (fewer if the roster is
  shorter), maximum all rows, preferred height = the minimum, stretch 1 in the
  page. The preferred height has to be the minimum because a settings page is
  a `QScrollArea` whose content never gets less than its layout's preferred
  height (wrapped labels make the layout height-for-width): a list preferring
  all rows made the page scroll before the list gave up any. Measured on his
  data: idle at 860 x 960 the group is 481 px with no page scroll bar; a
  download's status line, bar and queue line (+47 px) and a four-line error path
  (+63 px) shrink the list instead (860 x 900: 306 -> 260 -> 244 px, the list
  scrolls inside itself) and create no page scroll bar, so the page keeps its
  834 px width; only below six rows does the page scroll (860 x 700: 822 px
  wide). Nothing depends on 15 rows. **The button row (Refresh, Download
  Selected, Download All Missing, Cancel Downloads, Delete Selected) sits above
  the list** (2026-11-25): the status line, bar and queue line below the list take
  their height from the list, so only the list's lower edge moves and nothing
  clickable does. With the buttons below the list they moved 25-111 px under a
  resting pointer (860 x 960: 788 -> 738 with a four-line error; 40 rows: 849 ->
  802 -> 738; back when the area collapsed). Measured after, in idle, download,
  four-line-error and collapsed states (15 and 40 rows at 860 x 960, 15 rows at
  1600 x 1200): button row at y = 478 / 478 / 448 in every state, the Local
  runtime group at 174 in every state. Arrow keys on a page too short for the list
  also scroll the page to the current row (`_keep_current_model_row_in_the_page`;
  without it the selection moved out of sight at 801 x 400). Tests:
  `test_a_download_gives_the_list_the_height_it_needs_not_the_page_a_scroll_bar`,
  `test_arrow_keys_keep_the_current_model_row_in_view_on_a_short_page`,
  `test_the_download_area_moves_nothing_above_it_from_start_to_finish`. The first
  version (list fixed at all its rows) scrolled the page as soon as a download
  started in a dialog with little spare height: a page scroll bar narrows the
  page and reflows it sideways, which is the movement worth preventing. To
  revert the whole group: `git revert` the commits "fix(settings): size the
  Models tab's group to its content" and its follow-up.

- **`SettingsDialog` is a mixin facade** composed in `settings_dialog.py` from
  `_GeneralTabMixin`, `_AudioTabMixin`, `_LocalModelsMixin`, `_BenchmarkMixin`,
  `_RemoteProvidersMixin`, `_HistoryTabMixin`, `_ImportTabMixin`,
  `_PersistenceMixin` plus `settings_dialog_helpers.py`. Qt `Signal`s stay on
  `SettingsDialog`; mixins use `self` for everything. Public names stay
  patchable as `stt_app.settings_dialog.<name>` (re-exported under
  `__all__`). The six patched externals (`run_benchmark_cases`,
  `_scan_cached_models`, `start_model_download_process`,
  `delete_cached_model`, `estimate_cached_model_bytes`,
  `cleanup_incomplete_model_download`) are called as `_facade().<name>(...)`;
  `_facade()` imports lazily to avoid a cycle.
- **The Transcription tab holds only daily-use settings** (it scrolled at
  1342 px; 654 px at 9 pt now). Capture groups live on Audio
  (`settings_dialog_audio.py`), Hotkeys and Display on Hotkeys && Display
  (`settings_dialog_hotkeys.py`), ONNX Device and "Keep ONNX model loaded" in
  the Models tab's "Local runtime" group, "History Time" in the History tab
  ("Time Zone", whose entry count is an `ElidingLabel`). Widget attribute
  names are unchanged. Build order `_build_general_tab`,
  `_build_hotkeys_tab`, `_build_audio_tab`; the last applies the shared label
  column over `_general_forms`, `_hotkeys_forms` and its own. Eight tabs use
  775 of 840 px at 9 pt (pinned by `tests/test_settings_dialog_general_ux.py`);
  a ninth needs scroll arrows.
- **Tab titles say what the tab is for** (2026-09-20): Transcription, Hotkeys
  && Display, Audio, Models, Providers, History, Import Audio, Benchmark
  ("API Keys" became "Providers" on 2026-10-03: the tab also holds regions,
  Azure's endpoint and the custom endpoint). Module/attribute names keep the
  old ones (`settings_dialog_general.py`, `settings_dialog_remote.py`,
  `_local_tab_index`). Notes point to "the Models tab" / "the Providers tab",
  and every provider's missing-key error says "Settings -> Providers";
  `settings_timing` logs visible titles (`tab=Models`). A test pins the tab
  bar width.
- **The vocabulary note says when the model ignores it** (2026-09-20):
  `config.supports_custom_vocabulary(engine, model)` (from
  `LOCAL_MODEL_RUNTIME`, `CUSTOM_VOCABULARY_ENGINES`) is the one answer,
  pinned by a test that it equals "the constructor received
  `custom_vocabulary`" for every engine and local model. The note is reserved
  (42 px), amber when ignored; the field stays editable.
- **`_configure_combo_popups` runs after the root layout is built** (before,
  `findChildren` missed most combos). Every combo gets `maxVisibleItems(12)`
  and `ScrollPerPixel`; with defaults a separator left a blank strip of up to
  28 px. Not `setUniformItemSizes(True)` (separator becomes a full row).
- **An Audio-tab value read only while its checkbox is on is disabled while
  off** (VAD threshold, start and completion tone): `toggled` -> `setEnabled`,
  synced once at build (`setChecked(False)` emits nothing). **The
  silence-gate threshold is not linked**: streaming reads it with the gate
  off. The reason is on screen (2026-11-24): the VAD hint turns into "Used
  only while auto-stop is on."; each tone picker sits beside its checkbox, so a
  greyed picker explains itself and the tab needs no scrollbar at 860x960 or
  801 wide (scroll range 41 and 71 px with the hints on their own rows, 0 now).
- **Notes say facts in the neutral grey, problems in red** (2026-11-24): the
  Transcription tab's model note is red only for the `unavailable_reason`
  branch; "Batch mode only ..." (WebGPU) and the Nemotron chunk note are
  information. The Providers tab says in visible text what "Save API Keys"
  stores (keys, endpoints, regions of that tab) and that the other tabs still
  need Save. The Import Audio tab says once, under Language, that service,
  model and language apply to the imported file only; the local engine note
  is hidden, and the tab's content is top-aligned so the note's coming
  and going moves only what lies below it. The benchmark case caption names what Run Benchmark still waits
  for ("choose an audio file", "tick a model").
- **One name for the history limit, one empty state** (2026-11-24): "History
  limit" on both History surfaces (`HISTORY_LIMIT_LABEL`); an empty list shows
  `HISTORY_EMPTY_TEXT` through `ui_feedback.EmptyListNotice`, a label on the
  view's viewport, not a row or span the refresh logic must know about.
- **Mode is the first row of "Engine && Mode" and filters Engine and Model**
  (2026-11-24): in Streaming, engines and local models that cannot stream
  are hidden (`QListView.setRowHidden`) and disabled (a hidden row is still
  reached by the arrow keys on a closed combo), except the current
  selection, which stays and is flagged in red in the Model row's reserved
  note. Never flip the mode or the model to make a combination valid. The
  Mode hint's two texts (`_MODE_HINTS`) are kept about as long so a switch
  does not change the row height. Rule and reasons:
  `docs/agents/streaming.md`, "In Settings the mode is chosen first".
- **The Mode picker says "Experimental" only where it is true** (2026-11-26):
  `config.streaming_is_experimental(engine, model)` is the one decision (not
  experimental only for local + Nemotron, whose streaming reached its batch
  word error rate on 2026-11-25), `settings_dialog_helpers.mode_label` turns it
  into the picker text and `_update_mode_labels` re-applies it on every engine,
  model or mode change and on populate. Only the text changes; the stored
  value stays `streaming`. The combo is as wide as its form column (673 px in
  the test dialog in both states), so a text change moves nothing.
  Figures: `docs/agents/streaming.md`, "Which streaming is experimental".
- **Model selection is on the Transcription tab; Models only manages.** One
  "Model" row hosts `model_selector_stack`: page 0 `model_combo` +
  `local_model_runtime_warning_label`, page 1 `remote_model_combo` /
  `remote_model_note_label`, flipped by
  `_update_remote_model_selector` via `_update_model_selector_page`. The
  stack's size hint is its largest page, so rows below never shift.
- **Dialog feedback and refresh state**:
  - Transient button captions reserve width for all states (`ui_feedback.py`);
    refreshes preserve selection, current item and scroll (shared helper).
  - Session-stable default size and `QScrollArea` `AdjustIgnored` avoid
    tab-switch jitter. Tab selection changes colour/border, never font weight
    or width. Action rows use explicit spacing.
  - Inline field buttons use `_match_field_button_height`, which sets the
    `inlineFieldButton` property so the base QPushButton QSS box cannot beat
    the fixed height.
  - Transcription, Hotkeys && Display and Audio share one label column
    (applied by `_build_audio_tab`).
  - Save with no effective setting or key change must not emit
    `settings_changed` (it reloads models).
  - The Benchmark tab is the viewing side: header row ("Run Benchmark..." and
    a fixed-height status label) over the History/Results splitter. The run
    side lives only in the non-modal `benchmark_window` (860x880 bounded to
    the screen, owned by the dialog); `_open_benchmark_window` raises an
    existing one and refreshes its model list. `_set_benchmark_status` feeds
    both status labels. Completed and partial canceled runs save to History
    automatically; Export only shares. faster-whisper results store
    CTranslate2's resolved device, not `auto`.
- **The minimum width is the widest of tab bar and every page (the Benchmark
  page included, a scroll area like the rest since 2026-10-09), measured on the
  tab widget, never on the dialog, never past the screen.** `_pin_content_minimum_width` runs at construction and 0 ms
  after every show and tab switch, only raising the minimum: the Benchmark
  page reported 555 px until painted and 585 after, less since its History
  row lost "Load Selected" on 2026-10-03 (layout caches refresh only
  via a visible parent), and `QTabWidget.minimumSizeHint` covers all pages.
  - A `QScrollArea` reports a fixed 58 px minimum, so
    `_content_minimum_width` adds each page's content minimum plus scrollbar
    and frame, and the tab bar hint: below 771 px at 9 pt the bar hides tabs
    behind scroll arrows, and the tab a note sends the user to may be the
    hidden one. 2026-09-27: dialog minimum 797 px at 9 pt, 907 at 11.25, 1020
    at 13.5; 2026-10-03, with the longer "Providers" title: 801 / 912 / 1026
    (still the tab bar; the Providers page needs 583 / 674 / 757 since the
  fields below the key column were narrowed to it).
  - The tab widget's pane frame is asked of the style
    (`sizeFromContents(CT_TabWidget)`: 6 px), not derived as the tab widget's
    minimum minus the page stack's. That difference was 6 only while a
    non-scroll page (the Benchmark page) made the stack wide; once every page
    scrolled it read 61 px (the scrolling bar's 133 px minus the stack's 72)
    and pinned the dialog 55 px too wide.
  - It reads `self.tabs.minimumSizeHint()` plus root margins, never the
    dialog hint: a long failed-save message on the root status line once
    pinned 3077 px for the app's life (test: 400-character text on the status
    and engine lines leaves the pin unmoved).
  - It stops at `_available_dialog_size().width()`, lowering a minimum a
    wider screen allowed (`_apply_initial_dialog_size` runs first).
  - The Benchmark test's 640 px budget is a 9 pt number; at other fonts the
    test skips and reports the need.
  - `_let_wrapped_labels_narrow` (right after `_build_ui`) gives every
    wrapped label a 1 px minimum width, since a wrapped `QLabel` reports its
    longest word (a long path gave 1538 px). The Import path line and the
    `WrappedStatusLabel`s (Models action line, key-storage line,
    connection-test result) carry their text as tooltip. Labels built after
    `_build_ui` are not covered.
- **The Benchmark header's status label and progress bar take the button's
  rendered height**: `_pin_benchmark_header_row_height`, called from
  `_reserve_feedback_button_widths`, because an unpolished
  `open_benchmark_window_button.sizeHint()` is 26 px against 34 rendered.
- **Unsaved changes are tracked by a fingerprint; a close asks** (2026-09-27,
  `settings_dialog_unsaved.py`). It covers every settings input, pending
  remote models and key removals, the custom endpoint's listed models
  (`custom_models`, keyed by name, not `id()`: the tuple is replaced on every
  Refresh), and two History inputs. Save is enabled
  and "Unsaved changes" shows in amber only while it differs from the clean
  state. Close, Esc, title-bar X ask Save / Discard / Cancel; programmatic
  close and quit never ask. Clean state is recorded after `_populate` and
  every successful or no-change save; a key-only save cleans what it saved;
  the History import moving the limit spin box is no edit. It decides only
  prompt and Save button; the save baseline is `_populated_settings`.
  - No slider is an input (scroll bars and combo popups are sliders).
  - A save that writes no settings still moves `_populated_settings`.
  - A busy Discard (`_discard_unsaved_edits_while_busy`) runs
    `_populate_setting_widgets` from `_populated_settings`, since
    `reload_from_store` waits during work; `_populate_views` (connection-test
    target, Import pickers, local inventory views, history lists) runs only
    on a full reload. A restored Model Dir is set with signals blocked, then
    `_on_model_dir_changed` runs.
  - Discarding typed keys refreshes the Import tab's credential note.
- **The dialog persists for the app lifetime**; closing hides it, since it
  owns downloads, benchmarks, imports, scans and checks. Reopening reloads
  settings into the same object (dropping unsaved key edits), deferred while
  dialog-owned work runs. Every hide path, `QDialog.reject()` included,
  hides the benchmark `Qt.Window`s. Shutdown calls `SettingsDialog.shutdown()`
  before the controller's (children canceled, bounded cleanup), then
  `QCoreApplication.sendPostedEvents(self)` on the main thread so a run
  cancelled by quitting is saved (`aboutToQuit` runs after `exec()`). That
  delivery must start and show nothing: `_request_local_model_scan` and
  `_start_local_model_download` refuse after `_shutdown_started`, and
  `_on_update_check_finished` shows no modal then.
  - It is built ahead, `main._SETTINGS_PREBUILD_DELAY_MS` (2.5 s) after the
    tray, on the Qt thread: `build_ui` took 2.4-3.3 s in the owner's log
    (0.9 s once), during which a hotkey press, a finished transcription and
    its paste wait. Since 2026-11-24 the prebuild waits, 2.5 s at a time,
    while `controller.has_active_session()` (a recording or its foreground
    transcription); opening Settings meanwhile still builds at once. Not
    done: building the tabs over several event-loop turns, which would
    remove the window entirely (~3-4 h, every tab mixin's build order).
- **No settings-dialog stylesheet sets `font-size`** (ignores Windows' "Text
  size"); hints use `settings_dialog_helpers.hint_font()` (0.92 of the app
  font).
- **Field hints have explicit ownership**: `_field_with_hint`, 2 px gap,
  10 px row gap; model/language notes reserve two lines so engine
  switches never move fields; delayed paint/prewarm uses dialog-owned
  `QTimer`s.
- **A changing status line is reserved or elided, never left to grow**
  (growing lines moved buttons under the cursor). Exception since 2026-11-24: a
  line below the controls it reports on (the Models tab's status line) takes no
  room while empty (`CollapsingStatusLabel`), because nothing above it moves
  when it appears.
  - Wrapping lines (the Providers tab's shared connection-test line, the
    Run Benchmark window status) use the two-line
    `_reserve_dynamic_hint_height` with the message as tooltip.
  - Non-wrapping lines (Benchmark tab label, the bottom status line) are
    `settings_dialog_helpers.ElidingLabel`: horizontal policy `Ignored`,
    re-elided on resize, `text()` and tooltip keep the full message, shown as
    one line (whitespace folded). The bottom line takes leftover row space
    with stretch 1 (not beside an `addStretch(1)`), right-aligned. Ctrl+C and
    the context-menu Copy go through `copy_message`, yielding the full
    message unless a part is selected.
- **A widget that appears mid-interaction keeps its space while hidden only
  when its appearance would move something the user is pointing at**
  (`retainSizeWhenHidden`). The Models tab's download bar, queue line and
  status line are below the buttons and no longer keep any (2026-11-24, see
  "The Models tab's group is as tall as its content" at the top).
- **Multi-select lists use ExtendedSelection**, never `MultiSelection`.
  Exception: the Run Benchmark model list has checkboxes and no selection;
  Space toggles; a rebuild keeps check states (first fill all checked, later
  models unchecked). A click anywhere on a row toggles it
  (`_RowToggleListWidget`, 2026-10-03): the delegate toggles only on the
  check indicator, so the list toggles when press and release both lie off
  the indicator rectangle and leaves an on-indicator click to the delegate,
  or it would toggle twice and change nothing.
- **A setting without a widget is a defect waiting for a Save** (2026-10-01).
  `_construct_settings_from_widgets` builds a whole `AppSettings`, so a field
  it does not name takes the dataclass default, differs from the baseline,
  counts as an edit and is written over the stored value: the region and
  Speechmatics/Mistral fields added without UI reset a hand-set
  `"deepgram_region": "eu"` on an untouched Save. Every field is built from
  a widget or carried from `_populated_settings` (`schema_version`,
  `onnx_auto_preferred_devices`); the field guard in
  `tests/test_settings_dialog_connection.py` fails for a new one, and
  `tests/test_settings_dialog_regions.py` pins the round trip.
- **The Providers tab is one compact grid per group** (2026-10-03, UX
  review item 5). Columns: name, key field (the only stretch, minimum
  140 px), Test, Remove, a fixed-width last-test mark, the key-source badge.
  "Cloud providers" holds the eleven key providers (Meta and OpenRouter
  joined 2026-11-26; the OpenRouter row's tooltip says that audio goes to
  OpenRouter, which forwards it to the model's vendor); a provider's region
  (`_REMOTE_REGION_CHOICES`, each choice's vendor guarantee as an item
  tooltip) or Azure's endpoint is an indented sub-row right under it.
  **One width rule (2026-10-03): a field in a grid starts and ends where the
  key fields do** -- Azure's endpoint and the custom endpoint's Base URL,
  API Style and Key Command take the key field's column only (they used to
  span to the badge: 622 px against the key field's 342 at the minimum
  width, 9 pt; now 342 / 390 / 447 px at 9 / 11.25 / 13.5 pt, the
  longest placeholder needs 252 / 310 / 372). The API Style combo's minimum
  contents length is 8, not 24, or it would raise the column. The region
  combo is left-aligned at its own width, always visible and enabled, so a
  pick moves nothing; it raises the page minimum by at most its own width
  (Providers page 583 / 674 / 757 px, was 573 / 636 / 693; the dialog
  minimum 801 / 912 / 1026 is the tab bar and did not move). "Custom
  endpoint (OpenAI-compatible)" holds Base URL, API Style, Key Command and
  the endpoint's own key row; both groups use `_new_provider_grid` and the
  same captions, so their columns line up (a test pins it). Test is enabled
  only with something to test (a typed key, a usable stored key not marked
  for removal, or the custom key command) and no test running; Remove only
  with something to remove; placeholders say "Stored; type a new key to
  replace it" / "API key" / "Removed on Save". The former connection-target
  combo is gone (a row's Test, or "Test All Configured"). The former visible
  hints (region note, Azure endpoint, "status badges") moved into tooltips.
  Measured: content height 1338 -> 818 px at 9 pt (the tab scrolled 219 px,
  now not at all; 1764 -> 1016 px at 13.5 pt). With Meta and OpenRouter the
  content is 877 px (9 pt) against the 829 px viewport of a 960 px dialog, so
  the tab scrolled 48 px and cut the Save API Keys row; **the default dialog
  height is now 1010 px** (`_DEFAULT_SETTINGS_DIALOG_SIZE`, bounded by the
  screen as before; on a screen with under about 1060 px of available height
  the tab scrolls a little; the nine-row version did so below about 950 px).
  Measured on the real platform plugin (not offscreen) on 2026-11-26, 2560 x
  1392 screen, Segoe UI 9 pt: at 960 px content 877 against viewport 829
  (scroll 48 px); at 1010 px content and viewport are both 879 px, no
  scrollbar. Pinned by
  `test_providers_tab_fits_the_default_dialog_height_without_scrolling`.
  **Not built**: moving each region picker into its provider's own row would
  save about 90 px and let the default go back to 960; it was left because it
  changes the row grid (region pickers are indented sub-rows by design).
- **Connection test results persist in `provider_connection_tests.json`**,
  not `settings.json`; restored on open, overwritten only for tested
  providers, cleared when that provider's key is saved or deleted. Each row
  shows its result as a mark (the text in its tooltip); the one shared line
  under the groups (`test_conn_result`, two lines reserved) reports the test
  just run, or -- on opening and after a clear -- the most recent stored
  result, naming the provider. The per-row "Last test" lines it replaced
  reserved two lines each. The summary of a multi-provider test names only
  the failures, by short row titles (`0/12 provider tests passed. Failed:
  AssemblyAI, ...`); the old `Name: Fail | ...` over the long labels needed
  three lines against the two reserved. Twelve failures (the ten above plus
  Meta and OpenRouter) need 42 of 42 px at 9 pt at the minimum width
  (measured 2026-11-26; the 11.25 / 13.5 pt figures were not re-measured).
- **One model-list fetch serves the custom endpoint and OpenRouter**
  (2026-11-26). The Transcription tab's Refresh button (`fetch_models_button`,
  `_run_models_fetch`) asks the selected engine's list endpoint; the result is
  kept per engine (`custom_models` / `openrouter_models`, `listed_models`) and
  the status note per engine (`_model_list_notes`). OpenRouter's list is
  public, so its Refresh needs no key (`listing_only`). **OpenRouter's model
  note starts with where the audio goes** ("Audio is sent to OpenRouter, which
  forwards it to the vendor of the model you pick.",
  `OPENROUTER_AUDIO_NOTE`, also the first sentence of the Providers row's
  tooltip), so it is read where the engine is chosen and not only on hover
  (2026-11-26, review). It stays inside the note's two reserved lines at the
  minimum width and the row has the height of any other engine's, so nothing
  below it moves and the tab gets no scrollbar (pinned by
  `test_the_openrouter_note_says_where_the_audio_goes`). A Refresh result or
  error replaces the model text but keeps the sentence; a red setup issue
  (no key) replaces everything, as for every engine.
- **A remote engine that cannot dictate says so on the Transcription tab**
  (2026-10-03, UX review item 6d). `_remote_engine_setup_issue` (remote
  mixin) names the first gap: the custom endpoint's base URL, then the key
  (typed, or stored with a usable source and not marked for removal; a
  plain-text key is usable while the insecure-fallback checkbox is
  **checked, saved or not** -- `_key_source_after_save`, which the
  key-source badge shares, since the store hands the key out only once Save
  has applied the box. The Test button differs on purpose: a test runs now,
  against the store's present state. For the custom endpoint a key command counts, as
  `CustomEndpointTranscriber` requires one of the two), then Azure's
  endpoint (Speechmatics Melia 1 had a region rule until 2026-11-24, now
  removed). `_update_remote_model_note` shows it in red in place of the
  model note, inside the same two reserved lines, so nothing moves; it is
  re-run on an engine/mode/model change, every key-row refresh of the
  selected engine, a region change, and edits of the Azure endpoint, base
  URL and key command.
  Measured at the dialog minimum: the longest warning needs 30 of 40 px at
  9 pt and 45 of 56 px at 13.5 pt.
- **Saves are explicit and failure-safe**: the insecure-storage checkbox is
  pending until Save/Save API Keys; a failed key operation keeps the typed
  value or pending delete and stops unrelated mutations; a provider changed
  before a later failure still emits `settings_changed` (backends are not
  transactional); settings are persisted before history is trimmed.
- **Error text must be selectable.** One app-wide event filter,
  `dialog_style.install_selectable_message_text`, is the only way to reach
  the `QMessageBox.critical`-style statics; keep it unless every static is
  migrated. Inline labels use `dialog_style.make_label_selectable`. OR the
  flags onto existing ones (keeps `LinksAccessibleByMouse`). The body of
  `make_message_text_selectable` is guarded (runs in an event filter).
- **A reservation is measured, not predicted**: `retranscribe_dialog`
  reserves `heightForWidth` by measuring every candidate through the polished
  label at the width in question; neither `key=len` nor
  `key=horizontalAdvance` orders wrapped height (the latter under-reserved
  15 px for `granite-speech-4.1-2b-nar`). Do not measure before polish.
- **`QLabel.heightForWidth` is floored by `minimumHeight()`** (and the
  argument by `minimumWidth()`), so `_reserve_note_height` clears the floor
  before measuring, or it only ever grows. Any test measuring a reservation
  must remove the previous one first, or it reads it back.
- **A dialog worker thread that will not start rolls back its busy marker**
  (seven `Thread.start()` sites, each catching `_THREAD_START_ERRORS` --
  `RuntimeError` and `MemoryError`, reported through `exception_reason`
  because `str(MemoryError())` is empty; otherwise
  `_background_work_active()` stayed
  true and `reload_from_store()` was deferred forever). Each arm undoes its
  own site and reports the user's own action: connection test and update
  check re-enable controls; the scan routes through
  `_on_local_model_scan_finished` with a non-list payload (pops
  `_local_model_scan_started_at_by_token`, writes its own line, never
  `_set_local_models_action_text`); the benchmark drops its cancel event and
  restores the Details overview; the import calls
  `_finish_import_transcription`; the download queue reuses its crash-arm
  teardown (returns the coordinator's explicit interest).
- **A save checks whether the file would change and applies only the user's
  edits onto it.** The overlay (`overlay_opacity_percent`,
  `overlay_always_on_top`, `language_mode`, `input_device_name`) and
  `history_dialog._persist_limit` (`history_max_items`) write to the store
  while Settings is open. The change check compares with
  `self._settings_store.load()`; `_dialog_edits_over_stored` applies only
  fields differing from the baseline onto a fresh load, in both save paths;
  the trim uses the limit actually saved (writing the snapshot back once
  deleted 201 transcripts). A key replacement leaves `AppSettings` (and
  `has_*_key`) unchanged, writes nothing, but emits `provider_keys_changed`.
  **The baseline answers "what did the user change", the file "what is true
  now"; never put one question to the other.**
- **That baseline is `_populated_settings`, not `_loaded_settings`** (which
  absorbed merged values, so the next save wrote stale widget values).
  Written only by `_populate`, the History tab's programmatic spin-box move,
  and the end of each save (including no-write saves); the save must
  re-record it or an edit cannot be undone (`tray_middle_click_toggle`
  test). Only `_save` resets `language_mode` to the combo's value when
  recording (the one field `_construct_settings_from_widgets` reads from
  disk); the key-save path must not. `_language_mode_for_save`'s own read is
  masked by the diff (see its docstring). The key path uses one object for
  write and baseline.
- **Three `settings_store.load()` calls decide one save**
  (`_overlay_owned_settings`, `_stored_language_mode`, the change check),
  safe only because nothing between them pumps the event loop: all writers
  are main-thread slots (`controller.set_overlay_opacity_percent`,
  `set_overlay_always_on_top`, `set_language_mode`, the History limit
  writers), and `_construct_settings_from_widgets` has no `exec`,
  `processEvents`, `QMessageBox` or `QFileDialog`. Never add one between the
  reads; the history-trim prompt stays before all three.
- **"Use last recording" starts the Import language from the recording's own**
  (owner decision 2026-10-17: correcting a wrong language is "open Import,
  change the language, transcribe"). `_preselect_recorded_import_language`
  reads `LastRecordingStore.load().language_mode` and, when the selected
  import engine and model offer it (`_import_language_scope`, the one place
  that asks `language_modes_for_selection(engine, model, "batch")` for the
  tab), sets the picker through `_update_import_language_selector(preferred_mode=)`;
  the status line says which language, or that the model does not offer it
  and the choice stands. It does nothing for an archived file (its name
  carries no language), a state without a language, or after the user picked a
  language on this tab: `_import_language_chosen` is set by
  `_on_import_language_changed`, which only the user's own pick reaches
  (the selector blocks its signals), and cleared by `_populate_views`, so a
  reopened dialog is a new visit. It acts at the click only: changing the
  Import service or model afterwards rebuilds the picker as before, from what
  was last used for that pair or the Transcription tab's language. Tests:
  `test_use_last_recording_*` and `test_a_preselected_language_*` in
  `tests/test_settings_dialog_mode.py`.
