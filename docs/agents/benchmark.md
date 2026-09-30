# Benchmark: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
`local_benchmark.py`, `benchmark_*.py`, `scripts/benchmark_local.py` and the Benchmark tab/windows. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **The benchmark Results view is one widget, used in the tab and in pop-out
  windows.** `BenchmarkResultsPanel` owns the results table, the
  `_BenchmarkDetailsView` and the splitter between them, plus the row height,
  stylesheet and header configuration; the Benchmark tab embeds one instance
  and keeps `benchmark_results_table`, `benchmark_summary_text`,
  `benchmark_transcripts_table`, `benchmark_transcript_text` and
  `benchmark_results_splitter` as **aliases to the panel's widgets**, so every
  existing seam still addresses the same object and no widget exists twice;
  the mixin's old `_populate_benchmark_results` is gone, because a method
  whose only body is a call the panel already exposes is dead code.
  Consequences to keep intact:
  - **The results box is the panel's parent**, not the splitter's, so a test
    that walks up from `benchmark_results_splitter` reaches the panel and has
    to go one level further for the group box in the main splitter.
  - **The panel is Expanding.** The group box hands its extra height to
    whichever child asks for it, which the bare splitter did; a Preferred
    wrapper pinned the results area to its minimum.
  - `compact_table_row_height` and `configure_button_row` moved from
    `SettingsDialog` to `settings_dialog_helpers` because the panel and the
    results window need them and are not dialogs;
    `SettingsDialog._configure_button_row` is that same function, so all
    16 `self._configure_button_row(...)` call sites are unchanged.
- **Results sorting is three-state and hand-written.** Clicking a column header
  sorts ascending, then descending, then restores the run order, which the
  leading `#` column names for every case. `QTableWidget.setSortingEnabled` is
  deliberately not used: its sort mutates the rows and cannot restore the run
  order, and a proxy model is not an option either because the table is a
  `QTableWidget` that tests read through `item(row, col)`. Four properties:
  - **The stored cases are always in run order**; the sort state only decides
    the layout, so a live run appending a case and a loaded history entry both
    keep the reader's ordering, and only the third click resets it. Anything
    that empties the table therefore goes through the panel rather than
    `setRowCount(0)`, or the cleared result comes back on the next click.
  - **`sorted` is stable, so ties keep the run order** with no extra key.
  - **The three measured columns keep the rows with no measurement last in
    both directions.** `reverse=True` would pull them to the front, so the
    descending pass negates the finite value instead and leaves the non-finite
    group's rank where the ascending pass put it.
  - **The sort indicator stays switched on and the run-order state uses
    section -1**, which paints no arrow (verified on PySide6 6.11.1: the
    grabbed header image is identical to the never-sorted one).
    `setSortIndicatorShown(False)` re-measures every `ResizeToContents` column
    by the width the arrow would need -- measured on the `#` column, 56 -> 32 px
    -- so toggling it would move the table on every third click.
- **A stored run can be opened in a window of its own.** `Open in Window` in
  the Results and History action rows opens a `BenchmarkResultsWindow`: a
  non-modal `Qt.Window` parented to the settings dialog, holding one
  `BenchmarkResultsPanel` plus Export.../Close. Several can be open at once so
  runs can be read side by side, and each sorts independently. Rules:
  - **One entry, one window**, keyed by `BenchmarkHistoryEntry.identity_key()`
    in `_benchmark_result_windows`; reopening raises the window it already has.
  - **`finished` drops the key and only then schedules the deletion**, so the
    registry can never hold a dead wrapper. Hiding emits no `finished`, which
    is what lets a hidden window be raised again.
  - **`_hide_benchmark_window` hides every results window too.** They are
    `Qt.Window` children like `benchmark_window`, so every dismissal path of
    the settings dialog has to hide them explicitly.
  - **Deleting an entry closes its window and clearing the history closes all
    of them**: the entry behind such a window is gone and could not be
    reopened from History.
  - **Both buttons stay enabled while a benchmark runs**, unlike their
    neighbours: a pop-out reads its stored entry and never touches
    `_current_benchmark_cases`, which is what the Load/Export/Delete actions
    are disabled for. Export goes through an injected callback into the
    dialog's own export flow rather than a second copy of it.
  - **A hidden pop-out is forgotten by the caller that closes it.**
    `QDialog.close()` runs `reject()` -- and so emits `finished` -- only
    while the window is visible, and a pop-out hidden with the settings
    dialog closes silently: Delete Selected and Clear History left its key
    and the window in the registry for the life of the app.
    `_close_benchmark_results_window` forgets the key itself after the
    close; for a visible window `finished` has already done it and the
    forget is a no-op the second time.
  - **The action row reads the selection, not `currentRow()`.** A
    Ctrl+click on the selected row of the SingleSelection history table
    deselects it and leaves it current, so Open in Window and Delete
    Selected stayed enabled for a row nothing showed as selected.
  - **A delete or clear that finds nothing refreshes the list.** The store
    re-reads the file for either action, and a file another program damaged
    while Settings was open is quarantined on that read -- so the store was
    empty while the table kept its rows for the rest of the session, under
    a line saying only that the entry was not found (measured by the
    wave-10 reach lens). Both early returns refresh the list and the action
    row first.
- **A minimise of the settings dialog is not a dismissal.** Qt sends a
  hideEvent for it too (`QWidget::event` on the WindowStateChange), and the
  dialog's hideEvent hid the Run Benchmark window and every pop-out there
  exactly as on a Close; nothing re-showed them on the restore, so
  minimising Settings once took them off screen for the rest of the
  session. Inside that hideEvent the dialog is still visible and already
  minimised, which no dismissal is -- `hide()` on a minimised dialog reads
  `isVisible()` False there -- so exactly that state skips the hide. The
  `Qt.Window` children follow the OS minimise on their own.
- **The case list is redrawn only when the plan changes.** The list rebuild
  in `_refresh_benchmark_model_list` runs when the Run Benchmark window is
  reopened and when an inventory scan lands after a run, and its refresh
  redrew every row to Pending, wiping the Done and Skipped states of a run
  that had just ended. `_set_benchmark_plan_rows` records the sequence it
  drew (model, device target, compute type) in `_benchmark_plan_sequence`,
  and `_refresh_benchmark_plan_from_widgets` skips an equal plan. Note what
  that key is not: the statuses, which are the thing being kept.
- **A cancel ends the child first and then reads its output to the end.**
  `_stream_benchmark_process` broke out on the cancel check without looking
  at the queue again, so a case the reader thread had already queued was
  discarded although the child had measured it, and the case list labelled
  it Skipped. The first fix drained the queue with `get_nowait`, which
  closed only half of it: a case the child had *written* but the reader had
  not queued yet was still lost -- 58-63% of the time at the coincident
  instant over 400 trials on a real pipe, and end to end a `large-v3` case
  of 61 s read Skipped with its RTF gone. The cancel branch now calls
  `_terminate_process_tree` first, so the pipe closes and the reader
  reaches EOF once every line the child wrote is queued, then
  `_deliver_reported_cases` reads to that EOF (bounded by
  `_CANCEL_DRAIN_SECONDS` for a handle that outlives the kill; the `finally`
  arm's second terminate is a no-op on a dead child). Two more rules of
  that drain: **an error event met on the way wins over the cancel** -- the
  run had already failed when the user pressed Cancel, and the first fix
  walked past the event, so the dialog showed a clean cancel, stored the run
  as canceled and put the worker's reason nowhere; `RuntimeError` is raised
  before `BenchmarkCancelled` at the end of the stream. And **a `case`
  event without a case is logged and skipped** (`_case_from_event`,
  `benchmark_case_event_malformed`): `item.get("case") or {}` handed a
  number to the parser, which died on `.get` and took every case the child
  reported after it with it. The case a child finishes *after* the kill is
  gone by design; that is what the cancel is for. Two more of its readers:
  **an error event without a message reads as the fallback text** --
  `str(None)` is the word None, a non-empty string, so the `or "Benchmark
  failed."` behind it never fell through, in the main loop and in the
  drain alike (`text_or_empty` at both). And **the drain reads what is
  already queued before it looks at the clock**: with the deadline checked
  first, a deadline already past discarded a case and the EOF sitting in
  the queue, which cost nothing to read; unreachable through the real call
  site, where the deadline is set the statement before, and closed because
  the function's contract is every case the child reported.
  **The first error the child reports is the one raised, on both roads**:
  the main loop kept the last error event and the drain the first, so the
  message for one input depended on whether a cancel had landed before the
  second event, and neither logged the one it dropped
  (`_error_message_to_keep`, `benchmark_error_event_after_the_first`). The
  worker emits one error event and exits, so two is not a shape it
  produces; closed because two branches of one function disagreed. And **a
  child that survives every arm of the kill is logged**
  (`benchmark_worker_survived_termination`): each arm of
  `_terminate_process_tree` swallows its failure, so a kill a policy refused
  reported a clean cancel over a worker still running. The reader threads
  are daemon threads and end with the pipe; what the child computes after
  that is gone by design.
- **A stored benchmark run is read by declared type.** `_run_from_dict`
  coerces every `BenchmarkRun` field to its annotation with NaN, 0 or "" as
  the empty value: a hand-edited `null` raised `TypeError` in every reader
  (`avg_rtf` summed it, the history list formatted it) and the first of
  them sits in `SettingsDialog.__init__`. The same class in two more
  places: a `Win32_Processor.Name` WMI could not read arrives as JSON null,
  and `str()` of that recorded the CPU as the word None (a non-string name
  is no name, and the caller falls back to `platform.processor()`); and
  `_benchmark_created_label` catches the `OSError` that `astimezone`
  raises for every stamp the C library's `localtime` refuses -- on this
  Windows machine everything before 1970 and from 19 January 3001 on
  (epoch second 32,536,800,000), about nine thousand of the ten thousand
  representable years (measured: 8968 of 9999 refused at a mid-year
  instant), not "the ends of the range" as this entry said for one round
  and not "past 3001, about eight thousand" as it said for another -- and
  the `OverflowError` CPython raises when the epoch seconds do not fit
  `time_t`; the split is `time_t` width, not the operating system, and
  with a 64-bit `time_t` every representable datetime fits, so that arm
  is unreachable on this build. Four more
  shapes the first version let through, each one line of JSON: **`float()`
  of an int past the double range raises `OverflowError`** rather than
  answering inf, so a 401-digit `seconds` escaped the reader again -- the
  float branch answers NaN for it, and an int field past a signed 64-bit
  integer (`_INT_FIELD_LIMIT`) is 0, because a count that size is not a
  count; a test pins that every `BenchmarkRun` annotation has an entry in
  `_RUN_FIELD_EMPTY`, since a field of a fourth type would be handed through
  untouched. **A case's `error` is text or nothing** (`_error_text`): the
  results table hands it to a tooltip, which takes a string only, so a
  hand-edited `42` raised `TypeError` inside `show_entry` -- Load Selected
  and Open in Window alike; a non-text value keeps the case failed as its
  text, an empty one is no error. **The XLSX writer puts a number a `<v>`
  cannot hold in as text** (`_fits_a_numeric_cell`): `math.isfinite` itself
  raises `OverflowError` for such an int, and a hand-edited core count of
  that size died the export (the atomic writer had kept the previous file).
  An int past 2**53 goes in as text as well: a `<v>` is a double, which
  keeps only every second integer past that, so a spreadsheet read
  9007199254740993 from a number cell as ...992.
  And **the fastest case and the best real-time factor are taken over
  measured cases only** (`_best_case`, and the history list's Best RTF
  column): `min` over a NaN keeps whichever case comes first, so a stored
  run whose first case had no numbers read "-" while its second had
  measured 0.500.
- **One wrong value in `benchmark_history.json` costs that value, never
  the file.** Three readers, each fixed once. `raw.get("model_names", [])`
  defaults a missing key only, so a `null` there, in `webgpu_devices` or in
  an entry's `cases` raised `TypeError`, which `_load_from_path` answered by
  quarantining the file -- every recorded run gone from the app for one
  hand-edited value, and for good, because the backup holds the same entry
  and every later open re-fails the same way (measured: three good runs, 0
  rows, a `.corrupt.*` beside the `.bak`); `_text_items` reads a list and
  nothing else, and an entry whose `cases` is not a list is dropped like one
  with none. `int(inf)` raises `OverflowError`, which neither the int reader
  nor that backstop caught, so `1e400` -- valid JSON syntax -- in
  `environment.logical_cpus`, `physical_cores`, `options.runs`, `beam_size`
  or `threads` left `SettingsDialog.__init__` through the tray's slot, and
  Settings could never be opened again with nothing saying why; `safe_int`
  lives once, in `benchmark_environment`, catches it, and the two core
  counts are clamped at 0 because -1 exported as a measured count. And
  every text field outside `BenchmarkRun` read `str(raw.get(...))`, which
  renders a `null` as the word None and a container as its Python repr, in
  the History list's Recorded and Status cells and the results table's
  Model column; `text_or_empty` is the one rule for all of them, the run
  reader's `str` branch included. **The environment's own text fields were
  a fourth reader** of the same shape (`os`, `python`, `cpu`, the clock,
  cache, memory and Node strings, the GPU list and the framework map),
  rendering a null as None and a list as its repr in the Details overview
  and every export; they read through `text_or_empty` too, which lives
  beside `safe_int` in `benchmark_environment` -- the leaf module, so
  `local_benchmark` and `benchmark_history` import it without a cycle.
  **And a boolean is not a number in any of the three numeric readers**:
  `int(True)` is 1 and `float(True)` 1.0, so `beam_size: true` read as a
  beam of 1, `logical_cpus: true` as one core and `download_seconds: true`
  as a download of 1.00 s that nobody measured, while `_coerce_run_field`
  refused the same shape in a run's fields; `safe_int` and `_safe_float`
  refuse it now. **A history cap that is not a number keeps the default
  cap**: `_normalize_limit` fell back to 1, and `add_entry` truncates the
  stored file to the cap, so a `max_items` of NaN, None or `True` would
  have deleted every run but the newest (measured: 5 -> 1); no caller
  passes one today, closed because the fallback was the destructive value.
- **A failed export says so on the status line.** The `except` arm showed
  the warning box and returned, so after dismissing "Export failed" the
  Benchmark tab still read "Benchmark exported to ..." naming the previous
  export's file. While a benchmark runs (a pop-out's Export stays enabled),
  the run's next progress line replaces it within a poll interval
  (measured: 63 ms), and the warning box -- its text selectable, as every
  message box's is -- is the failure's report there; the status line is
  the run's while one runs.
- **The benchmark tables do not take Tab.** `tabKeyNavigation` cycles a
  table's cells forever, so a keyboard user who had selected a History row
  with the arrows could not reach the five buttons that selection enables
  (measured: 26 Tab presses, all inside the table), and Tab off "Clear
  History" was trapped in the Results table. All five benchmark tables set
  it off, and Tab moves to the next widget.
- **A run that measured nothing says so, and claims no save.**
  `_on_benchmark_finished` skips the history write for an empty case list
  and painted "Benchmark finished and saved to history." over a store it
  had never written (measured: the line painted, 0 entries, no file). The
  state is reachable through the slot alone -- the Run Benchmark window
  cannot select no model, and a model the runner refuses yields an error
  case, not none -- and the line now reads "Benchmark finished with no
  cases. Nothing was saved."
- **`planned_benchmark_cases` is the single source of the case sequence.**
  `run_benchmark_cases` iterates the list it returns, so the emitted
  `[Case i/N]` texts, the case total and the displayed compute type have one
  description. The Run Benchmark window's always-visible "Cases" table asks the
  same function what the current selection would measure, and reads the
  runner's own `[Case i/N]` progress line to mark a case `Running...` -- there
  is no second kind of event. Load-bearing details:
  - **The table is fixed at six rows and never appears or disappears.** The
    Run/Cancel row sits directly under it, and a group that came and went, or a
    height that followed the selection, would move those buttons under the
    cursor.
  - **A run's plan comes from the options snapshotted at its start.** The three
    controls are disabled during a run, but `_refresh_benchmark_model_list`
    still repopulates the model list when the inventory changes, and redrawing
    then would wipe the Running/Done states on screen. The refresh-from-widgets
    path returns early while `_active_benchmark_thread` is set, and is called
    from the list rebuild because that rebuild blocks the list's signals.
  - **At the end of every run each row that never delivered a result reads
    `Skipped`**, the one that was `Running...` when a cancel or a failure
    landed included: it delivered no result either, and leaving it as running
    would claim work that had stopped. And **a finished case is marked on the
    row the runner announced** (its `[Case i/N]` line, kept in
    `_benchmark_plan_running_index`), not on the nth delivered row: a case
    event the parent cannot read is logged and skipped while the runner's
    numbering moves on, so counting deliveries put the next result on the
    dropped case's row -- Done with another model's numbers -- and the skip
    pass, counting rows past the delivered cases, marked the model that was
    measured Skipped.
  - **The tab's progress bar keeps its space while hidden**
    (`setRetainSizeWhenHidden(True)`, fixed width and a height taken from the
    header button like the status label's), so the status label beside it
    never moves or re-elides when a run starts and ends. A total of zero is its
    hidden state, and `_set_benchmark_progress` is its only writer, as
    `_set_benchmark_plan_rows` / `_mark_benchmark_plan_case` are the plan's and
    `_set_benchmark_status` remains the two status labels'.
    `_clear_benchmark_results` must not touch the plan: that describes the run
    setup, not the loaded result.
  - **The header button's two captions are reserved once, from
    `_reserve_feedback_button_widths`.** Measured any earlier -- even right
    after its tab is added -- the button is not yet a polished child of the
    styled dialog and reports 109 px against the 115 px it renders at, so the
    reservation would be too small and the caption swap would move the button
    and the status label beside it.
- **Every benchmark export neutralizes what it cannot carry, and none of
  them writes into the file the user picked.** XML 1.0
  permits #x9, #xA, #xD, #x20-#xD7FF, #xE000-#xFFFD and #x10000-#x10FFFF and
  nothing else -- not even escaped -- while `saxutils.escape` only rewrites
  `&`, `<` and `>`. So one control byte anywhere in a benchmark row produced a
  worksheet that will not parse, inside a `.xlsx` written without error that
  Excel then refuses to open. Verified with `ElementTree`: NUL, BEL, vertical
  tab and a lone surrogate each fail; tab, newline and an emoji are fine. The
  route is the text nobody types -- `runtime_details` built from a runtime's
  own error output, the environment strings read off the system, and a
  transcript returned by a remote provider. `_export_safe_text` replaces
  exactly those characters with U+FFFD and nothing else: stripping non-ASCII
  instead would trade an unopenable file for a silently mangled German
  transcript, and the test compares the round-tripped cell text rather than
  only asserting that the file parses. `_cell_xml` is the single place user
  text enters XML; every other part of the workbook is static.
  **The same set is what the other two formats need**, which is why the
  function is no longer XML-only: a lone surrogate cannot be encoded as UTF-8
  at all, so the CSV and Markdown writers raised `UnicodeEncodeError`
  part-way through -- and one such character survives the history store
  untouched, because `json.dumps(ensure_ascii=True)` escapes it and
  `json.loads` decodes it straight back. In `_csv_cell` the type check is
  load-bearing (`spreadsheet_safe_cell` passes numbers through unchanged);
  the order is not, since U+FFFD is neither a formula prefix nor a space.
  **And all three build the bytes first and hand them to
  `atomic_write_bytes`.** The target is a path chosen in a Save dialog, so it
  is routinely a file that already exists; opening it directly truncates it
  before the first row is produced, and `zipfile.ZipFile(path, "w")` does the
  same. Measured before the fix: a transcript with one U+D800 left a 638-byte
  CSV fragment and a 0-byte Markdown file where the user's own file had been.
  Two properties are tested separately, because one test cannot see both: an
  export whose row builder raises must leave the previous bytes intact, and
  the atomic writer must be the *only* thing that touches the destination.
- **Benchmark surface stylesheets must stay scoped to widget types**: an
  unscoped property block is inherited by every child, so a bare
  `border: 1px; border-radius: 4px` on a `QTableWidget` gave each header
  section and the corner button its own rounded box.
  `_BENCHMARK_RESULT_SURFACE_STYLESHEET` and `_BENCHMARK_DETAILS_STYLESHEET`
  therefore scope every rule (`QTableWidget`, `QHeaderView::section`,
  `QTableCornerButton::section`, `QTabWidget::pane`, `QTabBar::tab`). Inside
  the details tabs the pane draws the frame, so the views in it are
  `NoFrame` and their content is wrapped with a margin instead of sitting
  flush against the border. The Benchmark History and Results tables share the
  one surface stylesheet so the tab reads as a single design.
- **Benchmark environment metadata**: benchmark summaries and exports include a
  best-effort system context from `benchmark_environment.py`. Keep hardware,
  OS, Python, Node.js, and local runtime/framework version collection there so
  Settings, history exports, and the CLI benchmark do not drift. ONNX benchmark
  cases also persist concise runtime fallback details so a CPU result explains
  why WebGPU or DirectML was rejected.
- **Benchmark environment records clock, cache and memory-module facts**: the
  CPU name, the logical core count and the total RAM say nothing about how
  fast that memory or that CPU is, and at batch size 1 the autoregressive
  decoders (Whisper's text decoder, Granite's LLM decoder) re-read their whole
  weight set for every generated token, so they are bounded by memory
  bandwidth while the encoders are bounded by compute -- two machines with the
  same CPU name and the same 32 GB can differ in either. `BenchmarkEnvironment`
  therefore also carries `physical_cores`, `cpu_clock`, `cpu_cache` and
  `memory_modules`.
  - **One PowerShell call answers for both WMI classes.** The CIM query is
    the expensive part, not the launch -- measured on one Windows 11 machine
    in two sessions, a bare `powershell` launch takes 0.12-0.16 s, the
    shorter video-controller query 0.29-0.42 s and this whole query
    1.35-1.60 s -- so
    `_HARDWARE_QUERY` asks `Win32_Processor` and `Win32_PhysicalMemory`
    together, the CPU name comes out of that same payload instead of the
    query of its own it used to be (1.32-1.40 s), and the memory question
    costs 0.00-0.04 s on top (the second session's seven runs put it within
    the noise). (This entry said "the launch is the expensive
    part" for one round; its own pair of numbers refutes that, since a
    launch-dominated cost would make the two queries roughly equal.) It runs
    on the benchmark worker thread, never on the Qt thread -- keep it that
    way. Both queries are wrapped in `@(...)` so a single-socket /
    single-module machine still yields JSON arrays: without the wrapper a
    single CIM object is a bare object, not a one-element array (measured
    on 5.1 and 7.6.5). What keeps that array from being enumerated on its
    way to `ConvertTo-Json` is the hashtable property it sits in, not the
    wrapper -- `@(1) | ConvertTo-Json` prints `1` -- which this entry had
    the other way round for one round. `_payload_entries`
    accepts a bare object as well, for a query edited to drop the wrapper
    and for shells older than this machine can run; neither PowerShell 5.1
    nor 7.6 unwraps a one-element array that sits in a hashtable property
    (measured), which this entry attributed to "older PowerShell" for one
    round.
  - **Every value is best-effort and empty on failure.** A 6 s timeout, a
    non-zero exit, output that will not parse, or any non-Windows machine
    leaves all four fields at their defaults, and the CPU name falls back to
    `platform.processor()` and `/proc/cpuinfo` exactly as before. The four
    fields default to empty in the dataclass and are read tolerantly in
    `from_dict`, so every history entry written before this change still
    loads.
  - **"Nominal" is in the clock label because turbo is not in the number.**
    `MaxClockSpeed` is the base frequency Windows reports and WMI exposes no
    boost ceiling, so calling it "max" in the UI would claim a limit the part
    does not have.
  - **The bandwidth clause says "per channel" and never multiplies.** It is
    `MT/s * 8 bytes / 1000`, i.e. one 64-bit channel; WMI does not say how
    many channels are populated, so a system total would be a guess. The rated
    speed beside the configured one was meant to show a kit sold as DDR5-6000
    that runs at 4800 because XMP/EXPO was never enabled, and on the one
    machine measured -- which is exactly that case, a G.Skill F5-6000 kit at
    4800 -- it cannot: with the profile off the BIOS fills SMBIOS `Speed`
    (offset 15h, "maximum capable speed" per DSP0134) with the JEDEC profile
    as well, so both fields read 4800 and the label reads like a DDR5-4800
    kit's. The "rated N MT/s, running at M MT/s" clause has no known
    producer; it stays because it costs nothing and a BIOS reporting the
    profile's rating as the maximum would make it fire. The part number
    (`F5-6000J3636F16G`) is what tells the kits apart and is not collected.
    Microsoft's own page documents `Win32_PhysicalMemory.Speed` in
    nanoseconds; the SMBIOS field it mirrors is MT/s and the measured 4800
    is MT/s, so the code is right and the page is not.
  - **The memory type comes from the SMBIOS table, not from WMI's own.**
    `_SMBIOS_MEMORY_TYPES` transcribes the DMTF "Memory Device -- Type" table
    (structure 17, offset 12h) as implemented by dmidecode's
    `dmi_memory_device_type` and by smbios-lib, which is what
    `SMBIOSMemoryType` reports verbatim: DDR is 18, DDR2 19, DDR2 FB-DIMM 20.
    The `Win32_PhysicalMemory.MemoryType` enumeration is a *different* table
    where DDR is 20 and DDR2 21, and the two agree only from DDR3 (24)
    upwards, so reading one with the other's numbers mislabels exactly the
    pre-DDR3 machines. An unrecognised code reads as "RAM" rather than as a
    guessed generation.
  - **Cache sizes are MiB written as "MB"**, the way every OS tool writes a
    cache size, and are deliberately neither `_format_bytes` (whose 1024
    ladder keeps one decimal, for byte totals) nor the decimal megabytes of
    `MODEL_ESTIMATED_SIZE_MB`. Clock and cache describe one processor package;
    only `physical_cores` sums over sockets, because two identical sockets do
    not share one cache.
  - **The export columns are inserted, so later positions shift.** The three
    CPU columns go after `environment_logical_cpus` and
    `environment_memory_modules` after `environment_memory`, in both
    `benchmark_history._export_headers` and `local_benchmark._write_csv`. That
    keeps each new column beside the fact it belongs to, and it moves the
    columns after `environment_logical_cpus` three or four places right: in
    the history CSV `environment_memory` goes from index 19 to 22 (three,
    since `environment_memory_modules` is inserted after it),
    `environment_node` from 22 to 26 and `row_type` from 23 to 27, with the
    same shift from `environment_memory` onwards in the CLI CSV. A reader that addresses
    columns by header name is unaffected; one that hard-codes an index is not,
    and no claim of positional stability holds here. `_write_csv`'s
    `fieldnames` list must be kept in step with `_environment_csv_values` --
    `csv.DictWriter` raises on a key the field list does not name.
  - **An unknown count renders as "" and never as 0.** `summary_details()`
    passes both core counts through `or ""` and the two export helpers do the
    same, because three of the four consumers of `summary_details()`
    (`_BenchmarkDetailsView.set_entry`, `format_benchmark_summary`,
    `benchmark_history._context_rows`) drop a value only when it is empty and
    would print a bare "0" for a machine whose count could not be read; only
    the CLI's `_print_environment` drops a 0 itself. `logical_cpus` is
    `os.cpu_count() or 0`, so its 0 is the same unknown -- and for one round
    the two export helpers passed only `physical_cores` through `or ""` and
    wrote the logical count's 0 as a measured value.
- **Benchmark runs out-of-process**: the Settings benchmark loads
  faster-whisper/ONNX models back-to-back; model loading does not release the
  Python GIL reliably, so running it in a background *thread* still froze the Qt
  UI. `benchmark_process.run_benchmark_cases` therefore launches
  `benchmark_worker` (a child process running the pure
  `local_benchmark.run_benchmark_cases`) and streams `progress`/`case`/`done`
  events as `@@STTBENCH@@`-prefixed JSON lines on stdout; the parent translates
  them back into the same `progress_callback`/`case_callback` and returns the
  same `list[BenchmarkCase]`. The settings-dialog facade re-exports this under
  the name `run_benchmark_cases`, so the Qt-facing benchmark code and the test
  seam (`stt_app.settings_dialog.run_benchmark_cases`) are unchanged. Cancel
  terminates the child process tree (`taskkill /T` on Windows) and raises
  `BenchmarkCancelled`; cases finished before the cancel are already streamed
  and kept. Keep the pure in-process function for the CLI and the worker; only
  the settings dialog goes through the process path. Wire new worker args into
  the frozen entry point (`main.py`) and the PyInstaller `hiddenimports`.
- **Every ONNX-device choice must be measurable**: the Transcription tab pins
  `local_onnx_device` for Cohere/Granite *and* Nemotron, so the benchmark has
  to be able to compare the same targets. It used to expand
  `webgpu_device_targets` only for the `onnx-webgpu` runtime and run Nemotron
  on the hardcoded `device="auto"`, so "All explicit targets" silently
  measured nothing for it. `local_benchmark.benchmark_device_targets` is the
  one place that maps a runtime plus the requested targets onto the cases to
  run, and both the case loop and `total_cases` go through it. For Nemotron it
  renames each target to the provider it actually resolves to
  (`nemotron_provider_order`) and drops duplicates: ORT GenAI has no WebGPU
  provider, so `webgpu`, `gpu` and `dml` are one configuration and reporting
  it three times under three names would be worse than not offering it.
- **Canary needs an explicit language, and the benchmark says so before the
  run**: `run_benchmark_cases` refuses Canary without a language, because
  onnx-asr hardcodes `<|en|>` and the model would *translate* German instead of
  transcribing it. That refusal happens per model, i.e. only when that model's
  turn comes, so with several models selected the whole run finished before the
  single failure was visible. `_run_local_benchmark` now rejects the
  combination up front, mirroring the existing German/English-only guard.
  **That German/English-only guard has a runner half too** (2026-09-19):
  the window refuses German with an English-only model before the run, but
  the CLI and every other caller of `run_benchmark_cases` went straight to
  the model. The Granite CTC graph takes no language input, fell back to
  Auto with a log line, decoded English -- and the stored run said `de`,
  because the ONNX runner records the language that was *asked for*
  (measured by the review through the real model). An English-only model
  asked for anything but Auto or English is an error case now, for distil
  as well, whose German token only produces nonsense. What is still true
  for every other ONNX model: `detected_language` holds the requested
  mode, not a detection -- Parakeet asked for `de` records `de` while
  detecting on its own. **And the runner reads the language once: blank is
  none.** The CLI's `--language " "` is a truthy string, so every
  `language or default` kept it: the English-only guard refused its model
  for "the language '  '", and Canary's `not language` check let it
  through to a runtime that maps an unknown code onto a trained one -- the
  translation that check exists to prevent. `run_benchmark_cases` strips
  the value at entry and turns an empty one into `None`, so both guards and
  every runner see one value (found by the review of the guard itself;
  the window only ever sends Auto, German or English).
- **Benchmark transcripts are first-class results**: every measured run stores
  and exports its complete transcript. The Benchmark tab renders History as a
  column table and compares each model/device run with run 1; keep all runs
  because GPU/runtime numeric differences can occasionally change decoded
  text. Legacy entries without transcript text remain readable.
- **The benchmark CLI cancels by thread, the app cancels by killing the
  process**: `scripts/benchmark_local.py --isolated-case` (the default) and
  the Settings benchmark both terminate the child process, which is why
  `run_benchmark_cases`' `cancel_check` had no production caller. The
  `--no-isolated-case` path ran the case on the main thread, where Python
  cannot run a signal handler while the process sits inside
  `InferenceSession.run` -- Ctrl+C was invisible until the call returned
  (4.46 s for one Canary run, times `--runs`). `_run_case_threaded` runs the
  case on a worker thread and keeps the main thread in a *poll* loop, and the
  flag Ctrl+C sets is handed to the model as `cancel_check`, which ONNX
  Runtime honours mid-run. The case-level `except Exception` must keep
  re-raising `BenchmarkCancelled` first, or a cancel is recorded as a failed
  case.
  Every wait in this script polls at `_CASE_POLL_INTERVAL_S`; none of them is
  a single long `join`, and that holds for the child process as much as for
  the thread. Measured: `Process.join(6.0)` delivered an interrupt raised at
  0.5 s only after 6.01 s, the poll after 0.62 s. `_join_case_worker` is the
  one helper both use.
  **The parent reads the child's queue while it is still running**
  (`_collect_worker_payload`), never after waiting for it to exit. A
  `multiprocessing.Queue.put` returns immediately and a feeder thread writes
  the pickled payload into an OS pipe, so the child blocks at exit until the
  parent drains it -- waiting for the exit first is a deadlock as soon as the
  payload outgrows the pipe buffer. Measured with the real classes: 8 KB
  completed, 16 KB hung forever, and the wait had no budget, so the CLI hung
  with no output. The payload is `asdict(case)`, i.e. every run's full
  transcript, so a few minutes of audio or a short clip at `--runs 3` reaches
  it; the repository's own 24 s sample is why it went unnoticed. The shipped
  app is not affected and must stay that way: `benchmark_process.py` uses
  `subprocess` with a dedicated stdout reader thread that drains the pipe
  concurrently, and `src/` uses no `multiprocessing` at all.
- **The benchmark CLI's `--isolated-case` payload is read while the child
  runs**: see the CLI entry above. The shipped app must stay on the
  `subprocess`-plus-reader-thread pattern in `benchmark_process.py`; `src/` uses
  no `multiprocessing` at all, and that is what keeps the same deadlock out of
  the app.
- **The benchmark report's agreement column cannot rank the leading cluster,
  and must never be quoted as if it could.** It is a `difflib` ratio of word
  tokens against `large-v3`, and re-running it with each working transcript as
  the reference moves Parakeet between 1st and 8th -- the differences are one
  or two tokens out of 52, and on the deciding token the reference itself is
  wrong (`transkriptiere` is not a German word). What survives every choice of
  reference, and is all it may be used for: Plus last of 12, NAR 11th-12th
  (neither transcribed the recording), `tiny` 10th-11th. Between any two
  models that both worked it supports nothing -- even Parakeet against
  `small`, 98.1% to 91.3%, reverses under five of the thirteen references,
  because models that agree with each other are not thereby correct. Quote it
  with
  `autojunk=False` and the argument order the report states: `difflib` discards
  popular elements of its *second* sequence past 200 items, which is why one
  transcript scored 1.4% one way round and 2.8% the other.
- **The supportable claim for the default is "the fastest local model that
  transcribed the recording", never "fastest" and never "most accurate".**
  `tiny` is genuinely quicker (0.033 against 0.043). Both earlier versions of
  this claim were reached by finding a number that supported the conclusion
  instead of asking what would refute it.
- **A retraction is a claim and needs the same search.** Two figures were
  withdrawn as having "no source" after checking only `benchmark_history.json`;
  both were in `docs/learning-log.md`, from a manual session on a different
  clip. "Not comparable" and "unsourced" are different sentences.
- **A stored benchmark run cannot be opened while one is running, and a
  finished case does not move the reader.** Loading a history entry replaces
  `_current_benchmark_cases`, which is the list `_on_benchmark_case_finished`
  appends to, so the table double-click needed the same busy gate `Load
  Selected` already carried -- without it the stored run's cases and the live
  one's landed in one results table and one live summary. Separately,
  `set_live_results` runs once per finished case and `_set_transcript_rows`
  ended in `selectRow(0)`, so every completed case threw a reader back to run
  1; the selection is restored by the row's `model / device / run` identity
  (not by index -- a finished case can insert rows above it), with row 0 as
  the fallback when the opened row is gone.
