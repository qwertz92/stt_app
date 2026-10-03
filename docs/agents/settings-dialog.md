# Settings dialog: design decisions

Binding rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30. Read before changing the `settings_dialog*.py` modules, their
tabs, sizing, save/merge and unsaved-changes logic. Entry order is kept, so
"the entry above/below" refers to this file; "Known limitations" is
`docs/agents/known-limitations.md`. History and full measurements:
`docs/learning-log.md` and git history.

Verbatim pre-condensation text: `git show e608f86:docs/agents/settings-dialog.md` (original AGENTS.md: `df2642a`).

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
  off.
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
- **The minimum width is the widest of tab bar, every settings page and the
  Benchmark page, measured on the tab widget, never on the dialog, never past
  the screen.** `_pin_content_minimum_width` runs at construction and 0 ms
  after every show and tab switch, only raising the minimum: the Benchmark
  page reports 555 px until painted and 585 after (layout caches refresh only
  via a visible parent), and `QTabWidget.minimumSizeHint` covers all pages.
  - A `QScrollArea` reports a fixed 58 px minimum, so
    `_content_minimum_width` adds each page's content minimum plus scrollbar
    and frame, and the tab bar hint: below 771 px at 9 pt the bar hides tabs
    behind scroll arrows, and the tab a note sends the user to may be the
    hidden one. 2026-09-27: dialog minimum 797 px at 9 pt, 907 at 11.25, 1020
    at 13.5; 2026-10-03, with the longer "Providers" title: 801 / 912 / 1026
    (still the tab bar; the Providers page needs 583 / 674 / 757 since the
  fields below the key column were narrowed to it).
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
- **No settings-dialog stylesheet sets `font-size`** (ignores Windows' "Text
  size"); hints use `settings_dialog_helpers.hint_font()` (0.92 of the app
  font).
- **Field hints have explicit ownership**: `_field_with_hint`, 2 px gap,
  10 px row gap; model/language notes reserve two lines so engine
  switches never move fields; delayed paint/prewarm uses dialog-owned
  `QTimer`s.
- **A changing status line is reserved or elided, never left to grow**
  (growing lines moved buttons under the cursor).
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
- **A widget that appears mid-interaction keeps its space while hidden**
  (`retainSizeWhenHidden`, e.g. the Models download bar).
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
  "Cloud providers" holds the nine key providers; a provider's region
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
  now not at all; 1764 -> 1016 px at 13.5 pt).
- **Connection test results persist in `provider_connection_tests.json`**,
  not `settings.json`; restored on open, overwritten only for tested
  providers, cleared when that provider's key is saved or deleted. Each row
  shows its result as a mark (the text in its tooltip); the one shared line
  under the groups (`test_conn_result`, two lines reserved) reports the test
  just run, or -- on opening and after a clear -- the most recent stored
  result, naming the provider. The per-row "Last test" lines it replaced
  reserved two lines each.
- **A remote engine that cannot dictate says so on the Transcription tab**
  (2026-10-03, UX review item 6d). `_remote_engine_setup_issue` (remote
  mixin) names the first gap: the custom endpoint's base URL, then the key
  (typed, or stored with a usable source and not marked for removal -- the
  Test button's judgement; for the custom endpoint a key command counts, as
  `CustomEndpointTranscriber` requires one of the two), then Azure's
  endpoint, then Speechmatics Melia 1 outside eu1/us1
  (`config.speechmatics_model_available_in`, the transcriber's own check). `_update_remote_model_note` shows it in red in place of the
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
  (six `Thread.start()` sites; otherwise `_background_work_active()` stayed
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
