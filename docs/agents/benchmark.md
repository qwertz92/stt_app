# Benchmark: design decisions

Binding rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30. Read before changing `local_benchmark.py`, `benchmark_*.py`,
`scripts/benchmark_local.py` and the Benchmark tab/windows. Entry order is
kept, so "the entry above/below" refers to this file; "Known limitations" is
`docs/agents/known-limitations.md`. History and full measurements:
`docs/learning-log.md` and git history.

Verbatim pre-condensation text: `git show e608f86:docs/agents/benchmark.md` (original AGENTS.md: `df2642a`).

- **The Results view is one widget, `BenchmarkResultsPanel`**, in the tab and
  in pop-outs: results table, `_BenchmarkDetailsView`, splitter, row height,
  stylesheet, headers. The tab's `benchmark_results_table`,
  `benchmark_summary_text`, `benchmark_transcripts_table`,
  `benchmark_transcript_text`, `benchmark_results_splitter` are aliases to it
  (no widget twice). The results box is the panel's parent (tests walking up
  from the splitter hit the panel first); the panel is Expanding (Preferred
  pins it to its minimum). `compact_table_row_height` and
  `configure_button_row` live in `settings_dialog_helpers`;
  `SettingsDialog._configure_button_row` is the same function.
- **Results sorting is three-state and hand-written**: ascending, descending,
  run order (the `#` column). Not `QTableWidget.setSortingEnabled` (cannot
  restore run order), not a proxy (tests read `item(row, col)`). Stored cases
  stay in run order, so emptying the table goes through the panel, never
  `setRowCount(0)`. `sorted` is stable (ties keep run order). Unmeasured rows
  stay last both ways: descending negates the value, no `reverse=True`. Run
  order uses indicator section -1 with the indicator left on:
  `setSortIndicatorShown(False)` re-measures `ResizeToContents` columns and
  moves the table.
- **A stored run can open in a `BenchmarkResultsWindow`** (`Open in Window`,
  non-modal `Qt.Window` owned by the dialog, several at once, each sorting
  independently).
  - One window per entry, keyed by `BenchmarkHistoryEntry.identity_key()` in
    `_benchmark_result_windows`; reopening raises it. `finished` drops the key
    before scheduling deletion; hiding emits no `finished`.
  - `_hide_benchmark_window` hides all results windows too.
  - Deleting an entry closes its window, clearing history closes all;
    `_close_benchmark_results_window` forgets the key itself, since
    `QDialog.close()` emits `finished` only for a visible window.
  - Open in Window and pop-out Export stay enabled during a run (they never
    touch `_current_benchmark_cases`); Export calls the dialog's one flow.
  - The action row reads the selection, not `currentRow()`.
  - A delete or clear that finds nothing refreshes list and action row (the
    re-read may have quarantined a damaged file).
- **A minimise of the settings dialog is not a dismissal**: its hideEvent,
  seen while still visible and minimised, skips hiding the Run Benchmark
  window and pop-outs (they otherwise vanished for the session).
- **The case list is redrawn only when the plan changes**:
  `_set_benchmark_plan_rows` records `_benchmark_plan_sequence` (model,
  device target, compute type, not statuses) and
  `_refresh_benchmark_plan_from_widgets` skips an equal plan, so rebuilds in
  `_refresh_benchmark_model_list` keep Done/Skipped.
- **A cancel kills the child, then reads its output to EOF.**
  `_stream_benchmark_process` calls `_terminate_process_tree`, then
  `_deliver_reported_cases` reads to EOF within `_CANCEL_DRAIN_SECONDS`;
  anything else lost cases the child had written (a 61 s `large-v3` case read
  Skipped). Cases finished after the kill are gone by design. In the drain:
  an error event wins (`RuntimeError` before `BenchmarkCancelled`); a `case`
  event without a case is logged and skipped (`_case_from_event`,
  `benchmark_case_event_malformed`); a message-less error reads the fallback
  (`text_or_empty`); queued items are read before the deadline check; the
  first error is raised on both roads (`_error_message_to_keep`,
  `benchmark_error_event_after_the_first`); a child surviving the kill is
  logged (`benchmark_worker_survived_termination`).
- **A stored run is read by declared type.** `_run_from_dict` coerces each
  `BenchmarkRun` field to its annotation, NaN/0/"" as empty (a `null` broke
  `SettingsDialog.__init__`); a test pins that every annotation is in
  `_RUN_FIELD_EMPTY`. A float-overflowing int is NaN, an int field past
  signed 64-bit (`_INT_FIELD_LIMIT`) is 0. A non-string
  `Win32_Processor.Name` falls back to `platform.processor()`.
  `_benchmark_created_label` catches `OSError` from `astimezone` (Windows
  refuses pre-1970 and from 3001-01-19) and `OverflowError`.
  A case's `error` is text or nothing (`_error_text`; tooltips take strings).
  XLSX writes numbers a `<v>` cannot hold, including ints past 2**53, as text
  (`_fits_a_numeric_cell`). Fastest case and Best RTF use measured cases only
  (`_best_case`).
- **One wrong value in `benchmark_history.json` costs that value, never the
  file** (a `TypeError` made `_load_from_path` quarantine it, and the backup
  held the same value). List fields (`model_names`, `webgpu_devices`,
  `cases`) read via `_text_items`/list checks; an entry whose `cases` is not a
  list is dropped. `safe_int` catches `OverflowError` (`1e400` in
  `environment.logical_cpus`, `options.runs`, `beam_size`, `threads` made
  Settings unopenable) and clamps core counts at 0. All text fields, the
  environment's included, read via `text_or_empty`; both helpers live in the
  leaf module `benchmark_environment` (no import cycle). Booleans are not
  numbers in `safe_int`, `_safe_float`, `_coerce_run_field`.
  `_normalize_limit` keeps the default cap for a non-number (falling back to
  1 let `add_entry` truncate history to one run).
- **A failed export says so on the status line** as well as the warning box
  (during a run the next progress line replaces it).
- **Benchmark tables do not take Tab** (`tabKeyNavigation` off on all five).
- **A run that measured nothing claims no save**: `_on_benchmark_finished`
  shows "Benchmark finished with no cases. Nothing was saved."
- **`planned_benchmark_cases` is the single source of the case sequence**:
  `run_benchmark_cases` iterates it, and the Run Benchmark window's "Cases"
  table asks it and marks `Running...` from the runner's `[Case i/N]` line.
  - The table is fixed at six rows so the window's content never jumps
    as models are checked.
  - Run/Cancel sit in a fixed footer outside the scroll area, under the
    two-line status (2026-10-03): inside it, "Show Run Options" pushed Run
    295 px down out of the 812 px viewport, a 13th model moved it 20 px,
    and with twelve models it needed a scroll at the default size.
  - The audio line is derived from the field on every edit
    (`_update_benchmark_audio_status`: none / "File not found" / "Selected"),
    two lines reserved, minimum width 1 px like the window status (a long
    file name otherwise widened the content to 1464 px).
  - A run's plan comes from options snapshotted at its start; the
    refresh-from-widgets path returns early while `_active_benchmark_thread`
    is set.
  - At run end each row without a result reads `Skipped`, including the one
    `Running...` at a cancel/failure; results go on the row the runner
    announced (`_benchmark_plan_running_index`), not the nth delivery.
  - The progress bar keeps its space hidden (`setRetainSizeWhenHidden(True)`,
    height from the header button; total zero = hidden). Single writers:
    `_set_benchmark_progress`, `_set_benchmark_plan_rows` /
    `_mark_benchmark_plan_case`, `_set_benchmark_status`.
    `_clear_benchmark_results` must not touch the plan.
  - The header button's two captions are reserved once, from
    `_reserve_feedback_button_widths` (earlier it is unpolished: 109 vs
    115 px).
- **Exports neutralize what they cannot carry and never write into the
  user's file directly.** `_export_safe_text` replaces exactly the
  characters XML 1.0 forbids (control bytes, lone surrogates) with U+FFFD for
  XLSX, CSV and Markdown; never strip non-ASCII. Sources: `runtime_details`,
  environment strings, remote transcripts. `_cell_xml` is the only entry of
  user text into XML; in `_csv_cell` the type check matters
  (`spreadsheet_safe_cell` passes numbers). All formats build bytes and call
  `atomic_write_bytes` (opening the path truncated the old file). Tests: a
  raising row builder keeps the old bytes; the atomic writer is the only
  thing touching the destination.
- **Benchmark stylesheets are scoped to widget types**
  (`_BENCHMARK_RESULT_SURFACE_STYLESHEET`, `_BENCHMARK_DETAILS_STYLESHEET`:
  `QTableWidget`, `QHeaderView::section`, `QTableCornerButton::section`,
  `QTabWidget::pane`, `QTabBar::tab`); unscoped blocks are inherited by every
  header section. Details views are `NoFrame` with a margin; History and
  Results share the surface stylesheet.
- **Environment metadata is collected only in `benchmark_environment.py`**
  (hardware, OS, Python, Node.js, framework versions) so Settings, exports
  and CLI agree. ONNX cases persist runtime fallback details.
- **The environment records `physical_cores`, `cpu_clock`, `cpu_cache`,
  `memory_modules`** on `BenchmarkEnvironment`: decoders at batch 1 are
  memory-bandwidth bound, encoders compute bound.
  - One PowerShell call, `_HARDWARE_QUERY`, asks `Win32_Processor` and
    `Win32_PhysicalMemory` (the CIM query, ~1.4 s, is the cost, not the
    launch), on the worker thread, never Qt. Both wrapped in `@(...)`;
    `_payload_entries` also accepts a bare object.
  - Best-effort: timeout 6 s, failure or non-Windows leaves fields empty; CPU
    name falls back to `platform.processor()` and `/proc/cpuinfo`; old
    entries load via tolerant `from_dict`.
  - Clock label says "nominal" (`MaxClockSpeed` is base clock).
  - Bandwidth is "per channel" (`MT/s * 8 bytes / 1000`), never multiplied.
    The "rated N, running at M" clause rarely fires (with XMP off SMBIOS
    reports JEDEC speed too). `Win32_PhysicalMemory.Speed` is MT/s despite
    Microsoft's docs.
  - Memory type uses `_SMBIOS_MEMORY_TYPES` (DMTF structure 17 offset 12h, as
    `SMBIOSMemoryType`: DDR 18, DDR2 19), not
    `Win32_PhysicalMemory.MemoryType` (DDR 20, DDR2 21). Unknown reads "RAM".
  - Cache sizes are MiB written "MB" (not `_format_bytes`, not
    `MODEL_ESTIMATED_SIZE_MB`); only `physical_cores` sums over sockets.
  - New export columns were inserted after `environment_logical_cpus` and
    `environment_memory` (`environment_memory_modules`) in
    `benchmark_history._export_headers` and `local_benchmark._write_csv`;
    indexes are not stable. Keep `fieldnames` in step with
    `_environment_csv_values` (`csv.DictWriter` raises on unknown keys).
  - An unknown count renders "" never 0: `summary_details()` and both export
    helpers pass core counts through `or ""` (`logical_cpus` is
    `os.cpu_count() or 0`); `_BenchmarkDetailsView.set_entry`,
    `format_benchmark_summary`, `benchmark_history._context_rows` drop only
    empty values; `_print_environment` drops 0 itself.
- **The Settings benchmark runs out-of-process** (model loads hold the GIL
  and froze the UI in a thread). `benchmark_process.run_benchmark_cases`
  launches `benchmark_worker` (pure `local_benchmark.run_benchmark_cases`),
  streaming `progress`/`case`/`done` as `@@STTBENCH@@` JSON lines back into
  `progress_callback`/`case_callback` and `list[BenchmarkCase]`. The facade
  re-exports it (test seam `stt_app.settings_dialog.run_benchmark_cases`).
  Cancel kills the tree (`taskkill /T`) and raises `BenchmarkCancelled`. CLI
  and worker keep the in-process function. Wire new worker args into
  `main.py` and PyInstaller `hiddenimports`.
- **Every ONNX-device choice must be measurable**:
  `local_benchmark.benchmark_device_targets` is the one map from runtime plus
  `webgpu_device_targets` to cases (case loop and `total_cases`), covering
  every `local_onnx_device` choice. For Nemotron it renames targets to the
  resolved provider (`nemotron_provider_order`) and dedupes (`webgpu`,
  `gpu`, `dml` are one configuration without a WebGPU provider).
- **Language guards run before the model.** Canary without a language is
  refused up front in `_run_local_benchmark` and the runner (onnx-asr
  hardcodes `<|en|>` and would translate). The runner makes an English-only
  model asked for anything but Auto/English an error case (2026-09-19;
  otherwise English was stored as `de`). Elsewhere `detected_language` is
  the requested mode. `run_benchmark_cases` strips the language and turns
  blank into `None` (`--language " "` defeated both guards).
- **Transcripts are first-class results**: every run stores and exports its
  transcript; History compares each run with run 1; keep all runs (GPU
  numerics can change text).
- **The CLI cancels by thread and polls; the app kills the process.** With
  `--no-isolated-case`, `_run_case_threaded` runs the case on a worker while
  the main thread polls and passes Ctrl+C's flag as `cancel_check` (Ctrl+C
  cannot interrupt `InferenceSession.run` on the main thread). The case-level
  `except Exception` must re-raise `BenchmarkCancelled` first. Every wait
  polls at `_CASE_POLL_INTERVAL_S` via `_join_case_worker`, never one long
  join. The parent reads the `--isolated-case` child's queue while it runs
  (`_collect_worker_payload`): a `multiprocessing.Queue` child blocks at exit
  until drained, so waiting first deadlocks once `asdict(case)` outgrows the
  pipe (16 KB hung). The app stays on `subprocess` plus a reader thread in
  `benchmark_process.py`; `src/` uses no `multiprocessing`.
- **The report's agreement column cannot rank the leading cluster, and must
  never be quoted as if it could.** It is a `difflib` word-token ratio
  against `large-v3`; with each working transcript as reference Parakeet
  moves between 1st and 8th (one or two tokens of 52 differ, and the
  reference is wrong on the deciding token, `transkriptiere`). Only what
  survives every reference may be used: Plus last of 12, NAR 11th-12th
  (neither transcribed the recording), `tiny` 10th-11th. Between two models
  that both worked it supports nothing (Parakeet vs `small`, 98.1% vs 91.3%,
  reverses under five of thirteen references). Quote it with
  `autojunk=False` and the report's argument order (`difflib` junks popular
  items of the second sequence past 200: 1.4% vs 2.8%).
- **The supportable claim for the default is "the fastest local model that
  transcribed the recording", never "fastest" and never "most accurate".**
  `tiny` is quicker (RTF 0.033 vs 0.043).
- **A retraction is a claim and needs the same search**: check
  `docs/learning-log.md` as well as `benchmark_history.json` before calling a
  figure unsourced; "not comparable" differs from "unsourced".
- **A stored run cannot be opened while one runs, and a finished case does
  not move the reader.** The table double-click has Load Selected's busy
  gate (loading replaces `_current_benchmark_cases`, which
  `_on_benchmark_case_finished` appends to). `set_live_results` runs per
  case; `_set_transcript_rows` restores the selection by
  `model / device / run` identity, falling back to row 0.
