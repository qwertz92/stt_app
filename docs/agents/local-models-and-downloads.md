# Local models and downloads: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
local runtimes (faster-whisper, onnx-asr, Granite CTC, Nemotron, Node/ONNX), the model inventory, the download slot and download progress. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **Temp files for audio**: `transcribe_batch` writes WAV to temp file because `WhisperModel.transcribe()` is most reliable with file paths.
- **Local model inventory cache**: last-known local model lists are stored in a dedicated JSON cache file, not `settings.json`, so the Models tab can render immediately without silently mutating user settings.
  Cached inventories are used for initial Models/Benchmark tab rendering, then
  disk verification starts automatically after the tab has had a chance to
  paint. App startup also refreshes the persistent inventory in the background.
  Source-tree and packaged runs isolate that scan in a subprocess so Python
  filesystem work cannot stall the Qt UI thread.
  Settings dialog lifecycle, tab paint, inventory render, and inventory scan
  timings are logged as `settings_timing` diagnostics for later troubleshooting.
  Models/Benchmark list widgets intentionally keep `AdjustToContents`; if first
  paint regresses again, use the timing diagnostics before changing this policy.
  The tray schedules a hidden settings-dialog preparation after startup so the
  first visible open and first Models tab paint avoid lazy Qt layout work. A
  hidden prepared dialog reloads settings from disk before it is shown.
- **Every caller parked in the download slot is registered**
  (`ModelDownloadCoordinator.has_waiting_download`), and a cancelled Local-tab
  download keeps the partial files while one exists. The mirror-image guard on
  the preload path checks `has_explicit_interest`, and the waiter it must not
  rob is typically a *preload*, whose interest is implicit and therefore
  invisible to that check -- so cancelling deleted the bytes the preload was
  parked to resume from and it restarted the multi-gigabyte fetch from zero.
  (Since huggingface_hub 1.32.0 that resume exists for the mirror's
  `*.ms-part` only; a hub `*.incomplete` is never read back and is removed
  when the parked download starts. See the download-progress entry.)
  The registration is unconditional and given back in a `finally`:
  incrementing only when the slot looks busy is a race, because `release`
  clears `_active` and notifies, and a caller that slips in before the parked
  thread wakes would otherwise decrement *its* registration to zero. Nothing
  can observe the transient count of a caller that takes the slot immediately,
  since every reader holds the same condition.
- **A preload queued behind another model's download says so**
  (`_preload_waits_for_another_model`): the phase is already `download` there,
  because `_download_model_for_preload` sets it and *then* blocks inside
  `acquire`, so both the progress detail and the phase word have to ask.
  Before this the overlay showed a frozen "approx. 60% (919/1531 MB),
  measuring speed" -- a percentage measured from a directory nothing was
  writing to -- for as long as the other download took.
- **Local model download queue**: Settings downloads run serially through one
  worker process so Hugging Face cache writes and network usage remain
  predictable and the active download can be terminated safely. Additional
  models can be queued while a download is active, and each waiting row shows
  its place (`Queued, 2 of 3`) because only one download runs at a time and
  several rows reading just "Queued" hid the order. Parallel downloads are
  deliberately not offered: the total bandwidth is the same, while every extra
  writer needs its own worker process, its own progress row, and its own share
  of the cancel/partial-cleanup bookkeeping. Cancel clears the queue and
  removes unusable `*.incomplete` files while preserving completed files for a
  later resume. **A Download pressed while the old worker is still draining
  its cancel runs.** A Cancel clears the queue under the download lock, so
  anything the worker finds queued when it observes the cancel event was
  queued *after* it -- the user pressed Download again and the tab already
  said "Queued for download" -- and the worker used to discard exactly that,
  silently. It now consumes the event and continues with the newer entries,
  both at the top of its loop and when the canceled subprocess reports back,
  and exits only when nothing is queued. **A drain that saw a Cancel reports
  it as one.** The first version of that rule reported the drain's *last*
  outcome: with `medium` canceled and a later `small` downloaded the summary
  said "Downloaded: small" and the row of the canceled model said nothing,
  and with a Cancel after the first model finished the run read as a plain
  success. The second version split the lists at one "before the cancel"
  snapshot, which two Cancels in a drain overwrote (a model downloaded
  between them read as finished before either), and named only the download
  a Cancel *killed*: the queued entries it removed were never mentioned, and
  a Cancel that emptied the queue before the worker's first iteration was
  not a cancellation at all -- the drain reported "Downloaded: " with no
  model, in the success colour. `_cancel_local_model_downloads` therefore
  records what it discards (`_local_model_download_removed_by_cancel`), the
  drain consumes that list together with the cancel event at both sites
  (`_consume_cancel_locked`), and `_canceled_drain_summary` renders an
  ordered event list -- downloaded, failed, canceled, removed -- grouping
  consecutive events of one kind, every group after the first starting with
  "Then". The headline "Download canceled." is kept for a Cancel that killed
  a download or removed queued entries; one that found nothing queued and
  nothing running did nothing the drain has to report, and what the user
  queues afterwards is an ordinary result. And `_start_local_model_download`
  refuses after
  `_shutdown_started`: the worker exits when the queue is empty, so an entry
  queued during shutdown would have started a fresh download from the
  `aboutToQuit` handler's own drain. **The drain's cleanup sentence reports
  a state, not a file count.** "No incomplete files remained." is a
  statement about a disk somebody read, and three zeros reached it having
  read nothing: `_cleanup_unless_awaited` keeps the partials on purpose when
  another caller is parked to resume them; the `ModelDownloadCanceled` arm
  of `_download_local_model_in_subprocess` never gets to the disk because
  the entry was still waiting for the slot; and a Cancel that only emptied
  the queue, or one after a download that simply finished, interrupted
  nothing at all. `_CleanupOutcome` carries `_CLEANUP_NONE` / `_CLEANUP_RAN`
  / `_CLEANUP_KEPT` / `_CLEANUP_SKIPPED` through `_run_download_worker` to
  the drain: the first says nothing, the kept and skipped ones say why the
  files were left alone, and the removal sentence is independent of all of
  them because one drain can remove for one model and keep for another.
  **And a partial the cleanup could not remove is reported as still
  there**: `cleanup_incomplete_model_download` added the size before the
  `unlink` and counted the file after it, so a partial another program
  still held -- the killed download child in a kernel read, a virus
  scanner -- was reported as removed by size and as gone by count
  ("Removed 1 incomplete file (0.5 MB)" for 1,000 bytes removed and
  500,000 left, and "No incomplete files remained." with the file on the
  disk). It counts after a successful unlink and returns
  `IncompleteCleanup(removed_files, removed_bytes, left_files)`;
  `_CleanupOutcome.left_files` carries the count to the drain, which says
  "N incomplete files of <model> could not be removed: still in use." and
  keeps "No incomplete files remained." for a disk with nothing left on
  it, and `scripts/download_model.py` appends its own sentence to its
  cancel line ("N incomplete file(s) could not be removed (still in
  use).", no model name). **Left means still there**: a file another
  program is deleting -- a scanner quarantining it, the killed download
  child tearing down -- refuses the unlink with "access denied" and is
  gone a moment later, and was counted as still in use (a racing
  deleter: 343 files "could not be removed" on an empty disk -- a
  run-dependent count; the wave-7 facts lens measured 162 with the same
  probe).
  `_unlink_partial` retries a refused unlink once after 10 ms: a held
  file is refused again, a file being deleted is not found, a transient
  lock lets the retry remove it, and `exists()` decides what is refused
  twice -- alone, `exists()` still counted two of 2,441 inside the
  delete-pending moment (0, 1 and 2 in three later runs of that build).
  **A read-only partial is not "in use"**: the unlink is refused for
  good -- a backup tool restored it, a copy carried the attribute over
  -- and it was reported as still in use on every cleanup while nothing
  held it; `_unlink_partial` clears the attribute before its retry,
  because the resume could not append to it either. `_model_cache_dirs`
  dedupes its search roots by `realpath`: `normpath` folds `..`, so a
  Model Dir spelled through the default cache with one no longer lists
  the same directory twice, but it is lexical and does not fold an 8.3
  short name (`AVERYL~1` beside `a very long name`), which listed one
  directory twice again and counted a held partial as two; the returned
  paths keep the user's spelling, and a missing directory stays as
  spelled. Three properties of the retry, measured by the wave-7
  concurrency lens and recorded rather than changed: `removed_bytes`
  credits the size read before the first attempt, now across a 10 ms
  window rather than a stat-to-unlink gap; the 10 ms is paid serially
  per refused file (50 held partials: 0.53 s) on the queue worker, the
  preload worker or the script, never on the Qt thread; and two cleanups
  running over one tree at once over-count `removed_files` (84 of 4,000
  at HEAD, 1,170 before 01adf24), because Windows accepts a second delete
  of a file whose delete is in flight.
  **The preload road reports the same count**: its cancel arm ignored
  the triple and painted "Model preload canceled." over gigabytes a
  scanner still held; `_note_preload_cleanup` records the sentence by
  generation and `_on_model_preload_done` appends it on both arms that
  tell the user the preload was canceled -- the explicit cancel (hotkey,
  tray) and a Settings save that leaves the local engine, which cancels
  the generation without bumping it and without
  `_preload_cancel_requested`, so its "Model download canceled." arrived
  through the failure arm and dropped the note (measured on both roads,
  one flag apart). A model switched while it downloads retires the
  generation; that completion paints nothing, because the new preload's
  progress line owns the overlay, and the count is logged as a warning
  instead of discarded; shutdown returns before either. Generation-keyed
  so a retired worker's note cannot describe the current cancel. (This
  entry said "the preload road" for one round while one of its four
  roads reported the count.)
  The kept and skipped sentences name their models (`cleanups` is one
  `(model, outcome)` per entry): two Cancels in one drain, the first killing
  a download and the second hitting an entry still waiting for the slot,
  read "Removed 3 incomplete files" beside "Incomplete files were left in
  place" as a contradiction. "No incomplete files remained." speaks only
  when nothing else does. And "No downloads ran." is painted in the
  warning colour like the other Cancel-shaped outcomes, not in the failure
  colour.
  **A drain that downloaded nothing does not report an empty success**:
  with nothing downloaded, nothing failed and no Cancel of its own, the
  summary fell through to "Downloaded: " naming no model in the success
  colour; not reachable through the UI at HEAD, but it now says "No
  downloads ran." with success False. **And the crash arm clears
  `_local_model_download_removed_by_cancel`** along with the four fields it
  already reset: only `_consume_cancel_locked` empties that list, so the
  next drain's first Cancel reported the crashed drain's removed models as
  its own (drain 2 queued `medium` and `large-v3`; its summary named `base`
  and `small` before them). Progress and its transfer rate come from the
  download worker's own byte count; cache growth and the estimated sizes in
  `MODEL_ESTIMATED_SIZE_MB` are the fallback (next entry).
- **Download progress is the downloader's own byte count, not directory
  growth** (2026-09-20, field report). A steady 100 Mbit/s download of
  Granite Speech 5.0 showed "measuring speed", one absurd rate and a jump
  from 0% to about 30%. `estimate_cached_model_bytes` sums `st_size`, which
  for a file written out of order is the highest offset written, not the
  bytes present, and a Xet-backed repository is reconstructed from up to 60
  concurrent ranges (the user's hf_xet log: 552 MB in 43 s). Measured with
  a synthetic writer placing 64 MB blocks out of order: `st_size` claims
  36.4% with 12.1% present; the 5 s speed window then saw one step and
  nothing between steps. A stale Windows `stat` was the other suspect and
  is refuted: a file another process was writing answered its current size
  every time. The worker (`local_model_download_worker.py`) therefore
  installs `snapshot_download(tqdm_class=hub_progress_tqdm_class(...))` and
  streams `@@STTDL@@{"event":"bytes",...}` lines on stdout, which a daemon
  reader thread in the parent (`local_model_download._pump_progress`)
  drains -- a pipe nobody reads blocks the child from inside hf_xet's
  callback, so the reader is part of the contract, and
  `model_download_process_error` waits with `wait()` because
  `communicate()` would read the same pipe from a second thread. Rules:
  - **The bar is selected by its exact name**,
    `huggingface_hub.snapshot_download`. huggingface_hub 1.32.0 adds a
    second `unit="B"` bar, `...snapshot_download.transfer`, whose
    Xet-deduplicated count never reaches the file size, and `thread_map`
    builds a files-count bar; a selector by `unit` double-counts.
  - **The total is `max(reported, MODEL_ESTIMATED_SIZE_MB)`**: the Hub grows
    its total per file as metadata arrives, and an early 1.1 MB total with
    1.1 MB fetched read 99%. "approx." is dropped only when the reported
    total reaches the table's figure. Percent is monotonic per download.
  - **The rate needs a span of at least one second**, and "measuring
    speed" is shown only until the first real sample pair exists.
  - **Anything unrecognised is ignored** and the parent falls back to
    directory growth, logged once as `model_download_progress_absent`: an
    older worker, a future Hub version that renames the bar, a transcriber's
    own in-process load-path download. The ModelScope mirror writes
    sequentially and has no hook; the worker sends
    `DOWNLOAD_PROGRESS_UNKNOWN` so the display returns to directory growth
    instead of freezing.
  - **The Models tab's progress bar and its "Next: ..." queue line keep
    their space while hidden.** Negative control: without
    `setRetainSizeWhenHidden` on the queue line, Download/Cancel/Delete
    moved 25 px and the model list shrank by 25 px.
  - **A file already complete is invisible to the download hook.**
    huggingface_hub returns it before it builds any progress bar, so its bytes
    are in neither the reported `done` nor the reported `total`: resuming a
    nearly finished download read 551 of 552 MB from directory growth and then
    0 of 552 MB at the worker's first event (found by the review, reproduced
    with the real classes). `completed_download_bytes` measures that baseline
    before `snapshot_download` starts and `offset_progress_hook` adds it to
    both numbers -- excluding `*.incomplete` and `*.ms-part` (the Hub reports
    those itself through `initial=`, so counting them double-counts),
    symlinks, and `<local_dir>/.cache` matched *below* the destination, since
    the default cache root is itself `~/.cache/huggingface/hub`. Capped at
    `MODEL_ESTIMATED_SIZE_MB`, because a blob cache keeps every revision it
    ever fetched. With both sources counting the same bytes, the speed tracker
    keeps its high-water mark across a source switch and clears only the
    sample history, so the rate is never timed across the switch.
  - **A reader thread that outlives its join owns the pipe.** On Windows,
    closing a pipe another thread is reading blocks until that read returns:
    with a grandchild holding the child's stdout handle, `join(2.0)` returned
    with the reader still in `readline()` and `stdout.close()` then blocked
    16.56 s on the download queue worker thread, which holds the in-process
    and the machine-wide download slot (0.00 s with no grandchild; after the
    fix the whole call takes 2.01 s). `_close_progress_reader` logs
    `model_download_progress_reader_still_reading` and leaves the stream to
    the reader's own `finally`. The preload cancel reaps its pipe and spooled
    log through `release_model_download_process`, which waits for nothing --
    six of its seven call sites are the Qt thread (the first version of
    this entry and of the docstring said five of six; the second review
    counted them). The seventh is the preload's own download thread, which
    is also the one that reads the failure text, so the spooled error log
    carries a lock held across read-and-close and across the release: a
    release landing between `seek(0)` and `read()` returned "" for the
    download's only error message (reproduced with two real threads). The
    malformed-event latch lives on the child's `_ProgressState`, not on the
    module.
  - **huggingface_hub 1.32.0 never resumes a partial file, so an orphan is
    removed before the next download starts.** Since upstream PR #4228 a
    file is downloaded to a process-unique `<etag>.<8 hex>.incomplete` that
    hub deletes in a `finally`; 1.8.0, the version before the 2026-09-21
    upgrade, resumed `<etag>.incomplete`. The app cancels by killing the
    child, so the `finally` never runs and nothing ever reads the file
    again. Measured by driving the real worker against a throttled HTTP
    stand-in for the Hub on 127.0.0.1: after a kill at 33.7 MB the next run
    asked for the file with no `Range` header, while the parent's first
    directory sample still read 34 MB, and the kept high-water mark held
    the line at "34 of 78 MB (approx. 43%), measuring speed" until the
    restart had caught up. `remove_orphaned_hub_partials` clears
    `*.incomplete` -- never `*.ms-part`, which the ModelScope mirror does
    resume -- under `download_destination_dir` only, without pruning
    directories and without walking through a junction or a symbolic link
    (`_partials_below`; `rglob` followed a junction out of the folder,
    review round 3), and never raises. It runs in
    `start_model_download_process` before the child exists (the parent
    samples the directory before the child's first event) and in both
    snapshot functions before `snapshot_download` (the transcribers' own
    load-path download and `scripts/download_model.py` never go through
    the launcher). After the fix the same probe restarts at 3% (the
    completed files), moves with the first new bytes and leaves no orphan.
    Consequences: a cancelled download of one large file starts that file
    again from zero -- completed files are still skipped -- and the cleanup
    arms that keep partials for a parked waiter now matter for the
    mirror's `*.ms-part` only. Stay on the newest hub: pinning below it for
    the resume would also give up its fixes, and most model repositories
    are Xet-backed, where a partial file was never resumable.
  - **hub's symlink probe is settled before the download threads start.**
    `are_symlinks_supported` writes `True` into its per-directory cache
    *before* it runs its test. On Windows without the symlink privilege a
    second download thread that asks inside that window reads `True`, calls
    `os.symlink` and dies with `OSError [WinError 1314]`, which is not the
    `PermissionError` `_create_symlink` catches; `snapshot_download` fails
    and the app falls back to the mirror or reports a failed download.
    Measured against the zero-latency stand-in, three small files, a fresh
    cache per run: 11 of 12 runs failed cold, 0 of 12 after one serial call
    with the storage folder (`_settle_symlink_probe`; the key is the
    `commonpath` of a blob and its pointer). The rate over a real network
    is lower and unknown. Only the `models--<repo>` layout is affected; a
    flat `local_dir` creates no symlinks. The guard is `except Exception`:
    an upstream rename costs the mitigation, not the download.
  - **The child runs with `HF_HUB_DISABLE_SYMLINKS_WARNING=1`.** The last
    stderr line is what a failed download shows as its reason, and hub's
    once-per-process symlink warning ends in the source line
    `warnings.warn(message)` -- which is what a child that died without a
    reason of its own reported.
  - **The baseline counts a blob hub copied into the snapshot once.** On
    Windows without symlinks `_create_symlink(..., new_blob=False)` copies
    (a blob whose pointer is missing, or content two files share), so
    `blobs/<etag>` and `snapshots/<sha>/<file>` are two real files with the
    same bytes (the second review: 3,000,000 bytes on disk, 6,000,000
    returned). A real file under `snapshots/` whose size equals a counted
    blob's is that blob's copy, each blob absorbing one; a same-size
    collision under-counts one small file, the safe direction. The test
    that would have caught it needs a symlink and skips on this machine.
  The overlay's preload line uses the same numbers and the on-screen model
  name. **Verified on 2026-09-21 against a local stand-in, not against the
  Hub**: the real worker and the real huggingface_hub 1.32.0 over plain HTTP
  from a server on 127.0.0.1 throttled to 12 MB/s reported 12.7 and
  11.6 MB/s, a monotonic percentage, a kill at 43% and a restart to exit 0
  with a complete snapshot. **Not verified: the Xet transfer path and the
  real Hub**, which need a real download. A probe of this kind must set
  `STT_APP_DISABLE_MODELSCOPE=1`: the first run's stand-in answered the
  tree endpoint wrongly, the worker fell back to the real mirror and
  fetched the real `tiny` model into the sandbox.
- **There is exactly one download slot, and it is enforced across every
  process of the same Windows user**
  (`model_download_coordinator`). It exists because the controller's preload
  path and the Models tab's queue each used to spawn a worker against the same
  cache directory, which the user hit as three failures in one sitting:
  selecting an uncached model and pressing Save downloaded it while the Local
  tab showed nothing; starting that same model from the Models tab then sat at
  0% forever, because progress is directory growth and the other process owned
  the directory; and switching model terminated the preload download *and*
  deleted its partial files, so a multi-gigabyte model restarted from a few
  hundred megabytes.
  - **Every** download goes through it, including the ones a transcriber starts
    from its own load path (`_ensure_snapshot` / `_resolve_model_path`) via
    `run_coordinated_download`. Not an edge case: with `keep_onnx_model_loaded`
    off the Cohere/Granite family never preloads, so the transcriber's own
    download is the only one it has. **faster-whisper is the easiest to miss** —
    `WhisperModel(...)` downloads inside its own constructor through
    `huggingface_hub`, which no grep of this repo reveals, so `_ensure_model`
    fetches through the slot first. That pre-fetch gates on
    `download_destination_dir` + `_has_valid_model_snapshot`, i.e. the directory
    the constructor actually resolves; `find_cached_models` is too broad (it
    also accepts the default cache and a flat layout) and let a custom Model Dir
    bypass the slot.
  - `acquire()` blocks until the slot is free and returns `ACQUIRE_JOINED` when
    the very same model finished while the caller waited, so the second caller
    never re-downloads. It is idempotent: several waiters on one finished model
    all join.
  - **Explicit (Local-tab) requests register interest at *enqueue***, not when
    the entry reaches the slot, and the preload path checks
    `has_explicit_interest` before deleting partials — otherwise a model still
    queued behind another download loses the bytes it is about to resume from.
    Every path that abandons the queue must give that claim back
    (`_discard_queued_downloads_locked`), and every exit from
    `_download_local_model_in_subprocess` must clear
    `_local_model_download_claimed`; leaving either behind blocks partial
    cleanup for the process lifetime and strands the entry so the model can
    never be queued again that session.
  - **`claimed` and `active` are different things.** A popped entry waiting for
    the slot is `claimed`: the model list, the pending set and the duplicate
    check must see it, but the progress bar must not — it measures directory
    growth, so pointing it at a model nothing is writing to invents a
    percentage. The bar reads `_local_model_download_active` only.
  - The Models tab renders a controller-started download in both the list and
    the progress bar, so `_poll_preload_download_state` drives
    `_refresh_local_model_download_progress` too; without that the progress
    branch is unreachable. The bar tracks its own shown-state rather than asking
    the widget, because Qt reports a child of a hidden dialog as invisible and
    this dialog persists hidden for the app lifetime.
  - **Nothing may wait for the slot on the Qt thread.** Nemotron's `start_stream`
    used to load the model there; once loads could queue behind an unrelated
    download that froze the whole UI with no progress and no way out, so it
    loads in its stream worker instead. `main` connects
    `request_download_shutdown` to `aboutToQuit` *before* the dialog and
    controller teardown, because the dialog shutdown releases the slot and a
    waiter would otherwise start a fresh multi-gigabyte download on a
    non-daemon executor thread that the interpreter joins at exit.
  - The download queue worker is wrapped in `try/except BaseException`: an
    exception used to
    kill the thread holding the queue, leaving interest registered, the running
    flag set and the tab's controls disabled with no way back.
  - **The slot has two layers, and both are load-bearing.** Inside the process
    a `threading.Condition` serializes callers and provides the join and
    explicit-interest behaviour the Models tab depends on. Across processes an
    OS-level lock (`file_lock.CrossProcessLock`, `msvcrt.locking` on Windows /
    `fcntl.flock` elsewhere) covers the out-of-process benchmark worker and
    `scripts/download_model.py` (a second copy of the app is separately
    refused by the single-instance guard in `main.py`) — neither of which
    the in-process half can even see. **It does not reach a second Windows
    user**: the lock files live in `appdata_root() / "locks"`, i.e. under
    the calling user's `%APPDATA%`, so two accounts sharing one Model Dir
    hold two different locks (this entry claimed otherwise until
    2026-09-04; see Known limitations). It is a real kernel lock rather than a
    PID file on purpose: the OS drops it when the owner exits for any reason,
    so there is no stale-lock detection, no heartbeat, and no timeout that
    guesses whether the other side is alive — the three things a PID file gets
    wrong, each of which leaves downloading permanently broken.
  - The cross-process lock is keyed on the **cache directory**, not the model:
    two writers corrupt each other through the shared blob and ref trees even
    when fetching different models, and directory-growth progress becomes
    meaningless for both. An empty Model Dir maps onto one shared identity
    because every such caller uses the default Hugging Face cache.
  - It is taken **after** the in-process slot and **outside** the condition —
    waiting for another process can take minutes, and holding the condition
    would freeze every observer (`active()`, the progress poll,
    `has_explicit_interest`) with it. Every exit from that wait must hand the
    in-process slot back, or a cancel strands it for the process lifetime and
    no download can ever start again. A filesystem that cannot lock (some
    network shares) logs a warning and degrades to process-local serialization
    rather than making downloads impossible.
  - **Holding the OS lock and storing it are two steps, and the gap between
    them must not be able to raise.** `acquire()` gives the in-process slot
    back on any exception, but it cannot see the `CrossProcessLock` object, and
    `_release_cache_lock` finds it through `self._cache_lock` -- so a raise
    after `lock.acquire()` returned True and before the assignment strands a
    real kernel lock with no reference to it. Because the lock is keyed on the
    cache directory and shared by every process of this user, that blocks the
    benchmark worker and `scripts/download_model.py` until this process
    exits, not just this process. The publication therefore
    releases the lock and re-raises. `CrossProcessLock.release()` is idempotent
    and swallows `OSError`, so the defensive call costs nothing.
  - The app's own download subprocess needs no lock of its own: the parent
    holds the slot for the whole life of `_run_download_worker`.
    `scripts/download_model.py` does take it, so running the script while the
    app is downloading now waits instead of racing — the standalone script was
    the one path the process-wide lock could never cover.
- **Download progress measures the download *destination*, never a candidate
  copy**: because progress is cache growth, `estimate_cached_model_bytes` must
  watch exactly the directory the downloader writes into.
  `local_faster_whisper.download_destination_dir` is that single source of
  truth: local ONNX models resolve through
  `local_webgpu_asr.webgpu_download_destination` (the flat `local_dir` that
  `download_webgpu_model_snapshot` passes to `snapshot_download`),
  faster-whisper models to `models--<repo>` under the configured `cache_dir`.
  Do not reintroduce a `max()` over the *candidate* layouts and cache roots in
  `_model_cache_dirs` — that exists for detection/delete/cleanup and
  legitimately includes both the flat and `models--<repo>` layouts plus the
  default cache. Sizing those made a foreign directory masquerade as the
  download: a conversion script pulled a repo's fp32 weights with `cache_dir=`
  (9.4 GB in `models--smcleod--…-nar-onnx`, since retired), so that download
  reported a fixed `10078/2490 MB, approx. 100%, measuring speed` while the real
  flat destination was still filling. A parametrized test pins
  `webgpu_download_destination` to the `local_dir` actually downloaded into so
  the two cannot drift. Snapshot entries that are symlinks are skipped when
  summing, because `stat()` follows them into an already-counted blob and would
  report 100% at half a download.
  **The one permitted fallback**: while the destination directory does not
  exist, `_complete_cached_model_root` may size a cache root that holds a
  *complete, loadable* snapshot — validated by
  `resolve_cached_webgpu_model_root` for local ONNX models and by
  `_has_valid_model_snapshot` for faster-whisper. Without it a model cached in
  the legacy `models--<repo>` layout — which the loader still resolves and
  uses, as Cohere does here — showed a 0% "Downloading" bar during every
  preload. Requiring a *valid* snapshot is what separates this from the bug
  above: the fp32 conversion copy carries none of the required `int8/*` files
  and can never qualify, and an in-flight download has no valid snapshot
  anywhere, so it correctly starts at 0%. A complete copy in another root is
  reported at 100% on purpose — the app would load that copy rather than
  download anything.
- **ModelScope mirror downloads are transactional and path-contained**:
  Treat every path in the remote file listing as untrusted. Only normalized
  POSIX-relative repository paths contained by the requested destination are
  accepted; absolute, drive-qualified, traversal, and backslash paths are
  rejected. The endpoint and redirects stay on HTTPS. Downloads and resumes
  write only to `*.incomplete`; a resume appends only after a matching HTTP 206
  and `Content-Range`, while an ignored range restarts the incomplete file.
  Publish a model file only after flushing, syncing, exact-size validation, and
  atomic replacement. Never expose a partial download at its final filename.
- **Manual model imports are transactional**: `scripts/import_model.py` hashes
  every imported model file, stages a complete snapshot under a temporary name,
  repairs legacy partial snapshots, publishes by atomic rename, and only then
  atomically updates `refs/main`. Copy failures must leave neither a final
  snapshot nor a reference to incomplete content.
- **Local ONNX ASR**: Cohere Transcribe, IBM Granite Speech 4.0,
  and IBM Granite Speech 4.1 are selectable local models through
  `transcriber/local_webgpu_asr.py`. They are batch-only and require Node.js.
  These are supported daily-use models, not experimental trials; do not
  reintroduce "experimental" framing in UI labels or user-facing model docs.
  Cohere, Granite 4.0, and Granite 4.1 2B use q4 ONNX snapshots through the
  high-level Transformers.js `GraniteSpeechForConditionalGeneration` pipeline.
  Granite 4.1 2B points at `onnx-community/granite-speech-4.1-2b-ONNX` (verified
  on WebGPU / Arc A750 on 2026-06-17: correct de/en/fr, no `Einsum` crash).
  **Granite 4.1 Plus and NAR were retired on 2026-08-26**, and the raw
  `onnxruntime-node` graph runtime that served them was removed with them, so
  there is exactly one ONNX inference path here and `onnxruntime-node` is no
  longer a top-level npm dependency (Transformers.js keeps its own nested pin).
  They were removed on measurement, not preference: in the 2026-08-25 run the
  base 4.1 2B ran at mean RTF 0.099 on WebGPU while NAR managed 0.447 and Plus
  4.149 -- both CPU-only, NAR with merged and dropped German words (63.2%
  word-sequence agreement with `large-v3`, 43 words against 52), Plus looping
  one clause to the token limit (2.8% agreement, 378 words), which is also what
  makes its RTF so bad: it is autoregressive and kept generating. **Every RTF
  in this file is the mean of that case's runs**, which is the convention the
  benchmark report uses; quoting a single run instead is how the same
  measurement came to be published as three different values -- 0.098, 0.100
  and 0.099 -- across four places in this repository. The graph-level cause is
  recorded in `docs/granite-speech-4.1-onnx-variants.md`: their encoders carry
  16 `Einsum` nodes each (`b m h c d, c r d -> b m h c r`, a 5-D contraction the
  WebGPU EP has no shader for), plus the 5-D attention `MatMul`s DirectML
  cannot execute, while the
  `onnx-community` export of the base model writes the same attention as
  Reshape/Transpose/MatMul and has none. Before re-adding any raw-graph model,
  read that document and verify every required file against the actual repo
  listing rather than copying a sibling's list -- a required file the repo does
  not ship is unrecoverable at runtime, and a test asserts each required file is
  covered by the download allow-patterns. Do not relabel a Plus build as base
  `granite_speech` to force it onto the pipeline path either: that produces
  broken English (verified with the valoomba build).
  `keep_onnx_model_loaded` now defaults to **on**: the flag only takes effect
  once such a model is selected, and without it every single dictation pays the
  full Node + ONNX load while faster-whisper and Nemotron stay warm. With it on
  the runtime is preloaded and kept like the other local engines; turning it
  off restores the old behavior (no preload, closed after each batch) for
  machines where RAM/VRAM pressure matters more. Existing settings files keep
  whatever they stored.
  The resolved runtime device is reported through transcriber progress messages
  so the overlay/import UI can show whether WebGPU, DirectML, or CPU was used.
  **`DEFAULT_MODEL_SIZE` is `parakeet-tdt-0.6b-v3`, not a Whisper model**
  (changed 2026-08-27). The earlier note here said to keep faster-whisper
  "until real target-hardware benchmarks justify switching"; those benchmarks
  now exist and say the opposite. On one 24.3 s German recording on a Ryzen 5
  7600X, both at `device=cpu`, Parakeet measured mean RTF 0.043 against
  `small`'s 0.154 -- 3.6x faster -- for 670 MB against 486 MB, with the 25 European
  languages its model card lists and its own
  language detection. It keeps everything that made `small` the default: pure
  Python, CPU only, no GPU, no Node.js. The one capability it drops is
  streaming (`onnx-asr` is batch-only), and `DEFAULT_MODE` is `batch`, so the
  out-of-the-box combination is consistent; a user who switches to streaming
  mode is told to pick a streaming model. Changing this constant does not
  touch an existing install -- `SettingsStore.load` falls back to the default
  only when the key is absent -- so it is strictly a first-run decision.
  `DEFAULT_FASTER_WHISPER_MODEL_SIZE` (`small`) stays the default *within*
  that runtime, which is what `LocalFasterWhisperTranscriber` and the
  benchmark CLI use.
  Keep `granite-4.0-1b-speech` selectable as a smaller q4 option until real
  benchmarks justify removing it.
- **Nemotron 3.5 true streaming**:
  `nemotron-3.5-asr-streaming-0.6b-int4` uses the published 793 MB multilingual
  ONNX Runtime GenAI export through `transcriber/local_nemotron.py`. It reuses
  the model's encoder cache and emits incremental RNNT tokens every fixed
  560 ms chunk instead of re-transcribing a rolling window. The published ONNX
  graph is fixed to 560 ms even though the original NeMo model supports other
  latency profiles. The app ships the installable CPU ORT GenAI package and
  tries DirectML first when a compatible DirectML runtime is present. As of
  2026-06-08, Microsoft's DirectML GenAI package depends on an unpublished
  `onnxruntime-directml>=1.26.0`, so reproducible installs fall back to CPU.
  Two Ryzen 5 7600X CPU runs measured it: RTF 0.21 with a 1.78 s cold load on
  a 24.3 s recording (2026-08-25) and RTF 0.24 with a 1.90 s load on a 28.1 s
  one (2026-07-10). The "0.229 RTF, 0.81 s cold load on the repository sample"
  this entry carried until 2026-08-28 matches neither run, and that sample is
  2.1 s of synthetic sine tones that no benchmark run used. Nemotron stays preloaded and cached like faster-whisper so
  pressing the recording hotkey does not block on model loading. Its internal
  runtime VAD follows the app's VAD setting. The language UI exposes only the
  transcription-ready and broad-coverage official prompt IDs.
- **`onnxruntime-node` is no longer a direct dependency**: it was only ever
  needed by the raw Granite 4.1 Plus/NAR graph sessions, which were retired on
  2026-08-26. The pipeline models run on the copy Transformers.js pins itself
  (exactly 1.24.3 across 4.0-4.2, exactly 1.30.0 in 4.3.0), which an
  `overrides` entry replaces with 1.29.0 since 2026-09-27 (next entry), so
  `npm ls onnxruntime-node` must show exactly one entry, under
  `@huggingface/transformers` and marked `overridden`. An override replaces
  that one copy; a dependency adds a second. **Do not add it back.** Declaring
  a newer version alongside makes npm install two different native ORT runtimes
  into one Node process (observed API-version mismatch warnings), and nothing
  in the app would use the newer copy. A 2026-07-21 benchmark found Transformers.js
  4.1->4.2 and CTranslate2 4.7.1->4.8.1 performance-neutral on AMD hardware
  (CT2 4.8.0's int8 PACKED_GEMM speedup is Intel-MKL-only). Re-checked on
  2026-08-11 against Transformers.js 4.2.0: the nested pin is still exactly
  1.24.3. `onnxruntime-node`'s
  `postinstall` being blocked by npm 12's install-script policy is harmless —
  the package ships its native binaries bundled and reports cpu/dml/webgpu
  (re-checked for 1.30.0 on 2026-09-18: `bin/napi-v6/win32/x64` holds
  `onnxruntime.dll`, `DirectML.dll`, `dxil.dll`, `dxcompiler.dll` and the
  binding, and `listSupportedBackends()` answers all three as bundled).
- **Transformers.js is 4.3.0, and `package.json`'s one `overrides` entry
  pins `onnxruntime-node` to 1.29.0 because it is faster** (2026-09-18, pin
  2026-09-27). 4.2.0 declared `sharp: ^0.34.5` and pinned an
  `onnxruntime-node` whose `adm-zip` range stopped at the vulnerable 0.6.0, so
  the tree needed two `overrides` (`sharp: ^0.35.0` against GHSA-f88m-g3jw-g9cj,
  `adm-zip: ^0.6.0`) and the audit job still went red again when the advisory
  database moved past them (sharp < 0.35.4, GHSA-rgj7-g3m4-5g8c; adm-zip
  0.5.9-0.6.0, GHSA-vwc7-r8mq-g2x9). 4.3.0, published 2026-09-16, declares
  `sharp: ^0.35.4` and `onnxruntime-node: 1.30.0` (`adm-zip: ^0.6.0`, locked at
  the patched 0.6.1) itself, so both overrides are gone and
  `npm audit --omit=dev --package-lock-only` reports nothing. An existing lock
  entry that still satisfies the new range is kept by
  `npm install --package-lock-only`, which is how `adm-zip` stayed at 0.6.0
  after the bump: `npm audit fix --package-lock-only` moves it. The jump from
  ONNX Runtime 1.24.3 to 1.30.0 was measured before it was taken, through the
  app's own `LocalOnnxWebGpuTranscriber` with `runner_path` pointing at a copy
  of the runner beside a throwaway 4.3.0 install: all three models
  (`cohere-transcribe-03-2026`, `granite-speech-4.1-2b`,
  `granite-4.0-1b-speech`) on `webgpu` and on `cpu`, a German and an English
  clip each -- twelve transcripts byte-identical to 4.2.0's, the requested
  device reached in every case with no fallback, and the warm real-time
  factors within run-to-run noise in both directions (one warm run per case,
  with another Cohere instance sharing the GPU, so no speed claim either way).
  The owner's standing preference is the newest release with the fewest known
  vulnerabilities as long as it runs; pin below the newest only with a
  measured reason written here. The repository's own `node_modules` is not
  touched by a lockfile change: a source-tree app that is running keeps the
  native binaries of its Node child loaded, so `npm ci` has to wait until the
  app is closed.
  **Why 1.29.0 rather than 4.3.0's own 1.30.0** (2026-09-27, Arc A750, Ryzen 5
  7600X, Node 24.21.0): measured through `LocalOnnxWebGpuTranscriber` against
  two throwaway 4.3.0 installs that differ only in the override, interleaved
  and repeated in reverse order, three warm runs per clip after a warm-up, on
  a 28.6 s German and a 29.4 s English clip. On WebGPU 1.29.0 was faster for
  every model, clip and order; fastest run, German clip: Cohere 1.03 and
  1.17 s against 1.67 and 1.73 s, Granite 4.1 2B 1.69 and 1.72 s against 3.01
  and 3.04 s, Granite 4.0 1B 1.55 s against 2.83 s; the English clip alike.
  The transcripts were byte-identical between the versions for every model
  and clip, the CPU target was within noise (Granite 4.0: 8.8 against 9.6 s),
  DirectML fails for Cohere on both (the known `MultiHeadAttention` failure),
  and `npm audit` reports nothing for either tree. The cause is not
  identified; 1.30's release notes change the WebGPU subgroup-matrix
  MatMul/Gemm subgroup size, which is a candidate, unverified. npm publishes
  no 1.28, and 1.27.0 was not measured: it declares `adm-zip ^0.5.16`, inside
  the vulnerable range above, so it would need a second override. Re-measure,
  and drop the override when it stops winning, whenever Transformers.js pins
  a newer ORT or `onnxruntime-node` 1.31 is published. With the override npm
  nests the package under `node_modules/@huggingface/transformers/node_modules/`,
  where Node resolves it first, so `benchmark_environment._node_package_version`
  reads that location before the root one; the version that runs is in that
  folder's `package.json` and in the lock entry of the same path.
- **Every Python dependency was brought to its newest release on 2026-09-21,
  and each was measured through the code that uses it.** Direct pins: PySide6
  6.11.2, sounddevice 0.5.6, numpy 2.5.3, onnxruntime-genai 0.16.0, assemblyai
  1.5.5 (from 0.64.33, a major version), groq 1.7.0, websocket-client 1.9.2,
  pyinstaller 6.22.3, ruff 0.16.8; through the lock onnxruntime 1.30.0,
  huggingface-hub 1.32.0, hf-xet 1.6.0, ctranslate2 4.8.2, tokenizers 0.23.2.
  Measured: the full suite; `scripts/release_check_providers.py` against the
  live services (AssemblyAI batch and realtime, Groq batch, a quit during the
  AssemblyAI poll, 4/4); one real model per local runtime from the source tree
  (`small`, Parakeet, Granite Speech 5.0, Nemotron on the CPU, Cohere on
  WebGPU). The pins are exact on purpose (a reproducible release build), so
  `uv lock --upgrade` alone moves only the transitive tree: `uv tree
  --outdated --depth 1` is what shows a direct pin that fell behind, and
  `requirements-win.txt` / `requirements-dev-win.txt` mirror the pins (a test
  compares them). ruff 0.16.8 added ISC004 (twenty implicit concatenations
  inside collections, each read, all intended, now parenthesized) and LOG004
  (one narrow suppression with its reason in the benchmark worker's
  `finally`). `node_modules` was reinstalled with `npm ci --omit=dev` the same
  day: it had still held the pre-4.3.0 tree (sharp 0.35.3), because a running
  source-tree app keeps seven native files of its Node child loaded
  (`onnxruntime_binding.node`, `onnxruntime.dll`, `dxil.dll`,
  `dxcompiler.dll`, sharp's `.node` and two libvips DLLs).
- **onnx-asr engine (Parakeet TDT 0.6B v3, Canary 1B v2)**: a third local ONNX
  path in `transcriber/local_onnx_asr.py`, separate from the Cohere/Granite Node
  runtime and from Nemotron's ORT GenAI path. It is **pure Python and needs no
  Node.js**, and it adds no ONNX Runtime: `onnx-asr[cpu,hub]` resolves the exact
  `onnxruntime` distribution `onnxruntime-genai` already requires. Only
  inference lives in that module — download, cache detection, size estimation
  and deletion reuse the shared `_OnnxModelLayout` entries in
  `local_webgpu_asr`, whose allow-patterns fetch only the int8 tier (both repos
  also ship fp32 graphs worth 2.4 GB and 3.3 GB).
  Measured on a Ryzen 5 7600X, CPU only: Parakeet 670 MB at **mean RTF 0.043**
  on a 24.3 s German recording (no English run was retained; earlier text here
  said 0.046 EN / 0.043 DE and neither figure is in the benchmark history),
  and Canary at 1029 MB. **Canary has no RTF in the benchmark history**: its
  only case there errored ("Canary cannot detect the language"), so the
  "0.134 / 0.135" this entry used to give came from the same retracted note as
  the Parakeet figures above and must not be requoted without a real run --
  nor may any ratio derived from it, which is how "~3x slower than Parakeet"
  survived one round longer than the number it came from.
  **Parakeet is not the fastest case in that run, and the qualifier is load-
  bearing**: `tiny` measured 0.033, 1.29x quicker. What separates them is the
  report's agreement column, and only in one direction. `tiny` is the weakest
  of the models that transcribed the recording -- 82.7%, and 10th or 11th of
  12 whichever transcript is taken as the reference. Parakeet is in the
  leading cluster, and **that cluster cannot be ordered by this measure**:
  Parakeet's own rank moves between 1st and 8th depending on the reference,
  because the differences are one or two tokens out of 52 and `large-v3` is
  the one that is wrong on the deciding token. So the supportable claim is
  "the fastest of the models that transcribed the recording" -- never
  "fastest" flat, and never "the most accurate". Saying it matched
  `large-v3` best *was* the second version of this defect: an unsourced
  superlative was replaced by a sourced number that does not mean what the
  sentence around it claimed.
  Against the GPU models it needs no qualifier: the quickest GPU case in that
  run is `cohere-transcribe-03-2026` at 0.083 on WebGPU, so Parakeet on plain
  CPU is **1.9x** faster than the best local GPU result and no GPU path is
  needed for the best local latency. (Granite Speech 4.1 2B at 0.099 is the
  *slowest* of the three GPU cases; comparing against it gave the 2.3x this
  entry used to state. An earlier "six times" compared against a stale Granite
  figure.)
  **Never add `onnxruntime-directml`.** It installs happily beside
  `onnxruntime` — `pip check` reports nothing wrong — but both distributions
  own the same `onnxruntime/` package directory (620 of 625 files), so the
  DirectML wheel silently overwrites the CPU build and downgrades the reported
  version. `onnxruntime-genai` then dies with "The requested API version [26]
  is not available" and a DLL init failure, i.e. it would trade a Parakeet
  speedup measured at roughly 1.9x -- from a manual DirectML run recorded in
  `docs/learning-log.md`, not from any benchmark case, so treat it as
  indicative only -- for the whole Nemotron engine. `onnxruntime-webgpu==1.27.0` is the one
  GPU distribution that coexists, but it measured *slower* than CPU here. If a
  GPU path is ever wanted it must be an isolated subprocess environment, like
  the Node runner already is.
  **Canary must never expose `auto`.** onnx-asr hardcodes the `<|en|>` source
  and target token, so with no explicit language it *translates* German into
  English instead of transcribing it (observed: "The automatic speaker
  recognition wandels spoken language..."). It is therefore in
  `LOCAL_EXPLICIT_LANGUAGE_MODELS` alongside Cohere, and
  `_normalize_language_mode` maps any unsupported code onto a trained one
  because an untrained ISO code raises `KeyError` deep inside the runtime.
  Parakeet is the mirror image: it accepts `language=` and *ignores* it (a
  bogus code yields byte-identical output), so it exposes only `auto` and sends
  no language at all rather than faking control. Both are batch-only, and both
  are excluded from the ONNX Device picker because they are CPU-only and would
  otherwise let the UI claim a setting that does nothing. PyInstaller needs
  `collect_all('onnx_asr')`, not just a hidden import: the mel/resampler graphs
  are package *data* loaded via `importlib.resources`, and without them every
  model fails while constructing its preprocessor.
- **Parakeet TDT 0.6B v3 Ultra is a pinned community export on the same
  runtime (2026-09-27).** Moondream post-trained Parakeet v3 and Olicorne
  exported it (`Olicorne/parakeet-tdt-0.6b-v3-ultra-onnx`, CC-BY-4.0); onnx-asr
  runs it as `nemo-parakeet-tdt-0.6b-v3`, Auto only and no language sent, like
  v3. Three rules:
  - **The download is pinned** (`_OnnxModelLayout.revision`, commit
    `dd20322`) because a community upload can replace its files under the
    same name, and the size and the measurements describe that commit; a
    pinned layout never asks the ModelScope mirror, which serves whatever it
    holds now, and `scripts/download_model.py` gives the commit in its manual
    clone and browser steps.
  - **The repository keeps each precision in a folder of its own** beside a
    shared `vocab.txt` and `config.json`, and onnx-asr 0.12.0 looks for all of
    them in the one folder it is handed. `prepare_onnx_inference_dir` copies
    the two root files into `int8/` at load, atomically, leaves a matching copy
    alone and rewrites a missing or changed one; a failed write is accepted
    only when a re-read finds the same bytes, so a Model Dir the app cannot
    write fails the load by name. The allow-patterns name the four files
    exactly, because fnmatch's `*` crosses `/` and the repository also holds
    `fp32/`, `fp16/`, `w4a8/`, `.zst` copies and a 2.5 GB `.nemo`.
  - **v3 stays the default.** Measured by the agent that added it: RTF 0.030
    against 0.044 on the 28.6 s German clip with an identical transcript, and
    0.031 against 0.042 on LibriSpeech excerpts, where its word error rate was
    2.9% against 2.2% (13 against 10 errors in 448 words, see
    `docs/models.md`). The picker label leaves out "post-trained by Moondream"
    because the retranscribe dialog's model combo would clip it; the
    attribution is in `docs/models.md`.
- **Granite Speech 5.0 470M TurboCTC is one INT8 graph on the CPU, in pure
  Python (`transcriber/local_granite_ctc.py`, 2026-09-19)**: a fifth local
  runtime (`LOCAL_MODEL_RUNTIME` value `granite-ctc`) and the only one that
  owns its `InferenceSession`: the graph is a single CTC encoder
  (`input_features` float32 `[1, frames, 320]` -> `logits`
  `[1, frames // 4, 16384]`), so the module supplies what a runtime library
  would -- log-mel features in numpy, a greedy CTC decode (blank id 0) and the
  `tokenizers` byte-level BPE decode. numpy, `onnxruntime` (CPU build) and
  `tokenizers` were all installed already; no dependency, no Node.js, no
  torch. The weights are the user's own public export,
  `qwertz92/granite-speech-5.0-470m-turboctc-onnx` (revision `e6e3b4d`), and
  the layout fetches nine files, 552,442,697 bytes, of which
  `onnx/model_int8.onnx` is 551,294,349 -- verified on 2026-09-19 with
  `snapshot_download(dry_run=True)` and the layout's real allow-patterns. The
  same repository holds an fp32 (1.89 GB) and an fp16 (947 MB) graph, and
  `onnx/*.onnx` would have fetched all three.
  - **Why one variant and no GPU path.** The export branch measured all three
    on one 29.4 s clip (`reports/benchmark_*.json` in that repository): CPU,
    12 threads, INT8 0.28 s, FP32 0.97 s, FP16 8.50 s; DirectML on an Arc
    A750 through onnxruntime-node 1.24.3, FP32 0.107 s, INT8 0.137 s, FP16
    0.179 s. FP16 is unusable on a CPU and the slowest of the three on that
    GPU, and the whole gain of any GPU variant over INT8 on the CPU is
    0.10-0.18 s per half-minute recording. A GPU path costs a second download
    of 0.95-1.89 GB, a raw-graph Node runtime (the Python ONNX Runtime is the
    CPU build and `onnxruntime-directml` must never be installed; the
    raw-graph Node path was removed on 2026-08-26) and a second feature
    extractor in JavaScript. Not built; it is the user's decision, not a
    default.
  - **The feature extractor is a numpy port of
    `GraniteSpeech5FeatureExtractor`**, which lives in `transformers` and
    pulls torch and torchaudio: 16 kHz, `n_fft` 512 with a periodic 400-sample
    Hann window zero-padded to the centre, hop 160, `center=True` with reflect
    padding, power 2, 80 HTK mel bins without normalisation,
    `log10(max(mel, 1e-10))`, a floor 8.0 below the clip's maximum, `/ 4 + 1`,
    deltas of window 3 (a centred difference with replicated edges), then two
    frames stacked to 320 values; the waveform is right-padded so an odd mel
    frame count fills its pair. `tests/data/granite_ctc_reference.npz` is what
    proves the port: a 7,840-sample int16 waveform (49 mel frames, so the
    padding branch runs, with a stretch of digital silence, so the relative
    floor matters) and the 25 x 320 features the real processor produced from
    it (transformers 5.16.0, torch 2.11.0, torchaudio 2.11.0, in the export
    branch's WSL environment). The test bounds the difference at 1e-4;
    measured 5.1e-6. On the branch's 20 real clips the port agreed with the
    real processor within 2.4e-5 and produced the identical token stream
    20/20; against the PyTorch model's argmax 19/20, and that one difference
    ("adam paintings" for "a paintings") is the INT8 graph's own, present in
    the branch's INT8 report.
  - **`preprocessor_config.json` is compared with the hard-coded parameters
    at load**, and it is a required file of the layout: a re-export with
    another hop or mel count would otherwise be transcribed into garbage
    without one error.
  - **A recording runs in passes of at most 180 s.** One pass allocates
    activations for the whole recording: peak working set measured 1.05 GB
    after the load, 1.42 GB for 188 s and 2.39 GB for 563 s. Past
    `_MAX_PASS_SECONDS` the waveform is cut at the quietest 20 ms frame of the
    last 15 s of each window; the windows share no audio and concatenate back
    to the input exactly. Measured on the real model: 563 s in 8.1 s, the same
    word count as the per-clip transcripts and a word agreement of
    0.9956-0.9978 per 188 s tile.
  - **Lower case without punctuation is the model, not a defect**: no merged
    token of its 16,384-entry vocabulary holds an upper-case letter (only the
    byte-level alphabet's 26 single letters do), and the 452 words of the 20
    reference clips consist of `a-z`, spaces and one digit -- "mister quilter
    is the apostle of the middle classes" -- on the PyTorch model's own argmax
    as well. The picker label, the runtime note and `docs/models.md` say so;
    do not post-process it.
  - **English only, and the graph has no language input**:
    `LOCAL_ENGLISH_ONLY_MODELS` gives `("auto", "en")` and both mean the same
    request. Adding a second English-only model surfaced three sentences that
    named `distil-large-v3.5` as *the* English-only model (the Transcription tab's
    language note, the benchmark's German refusal, `download_model.py
    --list`'s substring test on "distil"); all three read the set now.
  - **Cancel** reuses `_RunAbortHandle` and `_CancelWatchdog` from
    `local_onnx_asr` -- no session wrapping is needed, because this module
    passes its own `RunOptions` -- and checks before the load, between passes
    and mid-run. Measured on the real model: a cancel 0.4 s into the 563 s
    recording returned after 0.46 s and the next transcription was correct.
    **The load itself is not interruptible**: building the tokenizer and the
    `InferenceSession` is one blocking call each, so a cancel that arrives
    during it surfaces when it ends (the review measured 1.4 s with the real
    model, once per session, and the session stays loaded for the next run).
    The same holds for onnx-asr.
  - **Shared rather than copied**: `resolve_or_download_onnx_model` (module
    level in `local_onnx_asr`, imports still inside the function so the
    existing monkeypatch targets resolve), `_read_wav_float32` -- which now
    refuses a header rate of 0, since `wave` validates the channel count and
    the sample width and not the rate, and this runtime's resampler divided by
    it -- and `_pcm_audio.resample_linear`, moved out of Nemotron.
  - **The resampler refuses a source rate below 8 kHz**
    (`MIN_SOURCE_SAMPLE_RATE_HZ`, found by the review of this runtime,
    2026-09-19). The header's rate decides how many samples the
    interpolation makes -- `16000 / rate` times the file's -- so a WAV
    declaring 1 Hz around ordinary PCM turned 3,244 bytes into 25.6 million
    samples, 1,600 s of "audio" and ten graph passes (measured through
    `_waveform_from`; 64 KB works out to 512 million samples, 2 GB as
    float32), on the single transcription worker. Nemotron has called the
    same function since it was added, so the check sits in the function
    and not in a reader: a third caller cannot forget it. 8 kHz is
    telephony's rate, the lowest speech is recorded at and the lowest
    onnx-asr accepts too (`WrongSampleRateError` below it, tried against
    the installed package), so no file a recorder writes is refused and
    the factor is at most two. The two readers' own `<= 0` guards stay:
    they are header validity checks with tests of their own, and onnx-asr
    never reaches the resampler. `transcribe_batch` also wraps the decode
    step, which sits in front of the graph call's `try`: a recording too
    long to hold was a raw `MemoryError`.
  - **It is not device-aware**: absent from `DEVICE_AWARE_LOCAL_MODELS`, so
    the ONNX Device row is disabled with a note of its own,
    `benchmark_device_targets` yields the one `auto` case, a stored
    `onnx_auto_preferred_devices` entry never reaches the constructor, and the
    identity reads the same four fields as onnx-asr.
  - **The Models tab's row suffix is one rule now**
    (`LOCAL_ONNX_MODEL_RUNTIME_LABELS` plus `supports_streaming`): a branch
    per runtime family had left the two onnx-asr rows without "batch only".
    **Every row carries its whole text as a tooltip**: the list elides a
    row wider than its viewport, and at the dialog's 860 px default the
    viewport is 788 px against 810 px for this model's row and 809 px for
    Nemotron's (measured 2026-09-19), so the end of the row -- the part
    that says what the model can do -- was cut off with nowhere to read it.
  - **A sentence names a model the way the screen does**
    (`local_model_short_label`), never by its settings id: the benchmark's
    German refusal said "deselect granite-speech-5.0-470m-turboctc" above a
    list whose row reads "IBM Granite Speech 5.0 470M (...)", and the test
    written with it asserted the id -- a passing test that pinned the
    defect.
  - **It is the fastest local model on English, and its text is the price.**
    The app's own benchmark runner, one process, CPU, the branch's 29.4 s
    clip, three runs after a warm-up (2026-09-19): mean RTF 0.0135, against
    0.0245 for `tiny`, 0.0501 for Parakeet and 0.1593 for `small`. Besides the
    casing and the punctuation there is no apostrophe either, so a possessive
    comes out as "is" ("nor is mister quilter is manner less interesting").
    `DEFAULT_MODEL_SIZE` stays Parakeet: one language against 25, and text a
    dictation can be pasted from.
  Verified with the real model in a sandboxed Model Dir whose seven files are
  byte-identical to the hosted ones (git blob ids and LFS SHA-256): 20/20
  clips equal to the validated prototype, load 1.7 s, RTF 0.016 over 187.7 s
  on the Ryzen 5 7600X (Windows, ONNX Runtime 1.28.0), WAV path, WAV bytes and
  raw PCM identical, and a 20-mutant negative control with no survivor. A
  PyInstaller 6.22.0 bundle built from `0d1c5b2` loads and transcribes with it
  (`scripts/release_check_frozen_bundle.py --model-dir`, which now prefers
  this model as the fifth runtime: load 1.6 s, RTF 0.016, the prototype's
  text). **Not verified**: the real 552 MB transfer through the app (only its
  plan), and a recording that is not English.
- **Local ONNX execution device (`local_onnx_device`, default `auto`, schema
  23)**: the Benchmark tab could always pin a device, but daily dictation
  always ran on `auto` because `factory.py` never passed one. The "ONNX Device"
  row (on the Transcription tab until 2026-09-27, now in the Models tab's
  "Local runtime" group) feeds the same policy (`LOCAL_WEBGPU_DEVICE_POLICIES`)
  into `LocalOnnxWebGpuTranscriber`, with the same wording as the benchmark
  choices so a device proven faster there can be selected for real use.
  `auto` keeps every existing behaviour. (A per-model CPU preference,
  `LOCAL_ONNX_AUTO_CPU_MODELS`, existed for the two retired raw-graph Granite
  variants and was removed with them; re-add it if a model ever again loads on
  a GPU and only fails at inference, which a load-time probe cannot detect.)
  Unlike `language_mode`, the device **is** part of the
  transcriber cache key *and* the preload key: it is baked into the loaded
  runtime, so changing it must reload rather than reuse. An unknown stored
  value falls back to `auto` via `normalize_local_onnx_device` instead of
  failing the load. Nemotron is in the picker and therefore *must* receive the
  policy: `config.nemotron_provider_order` is the shared mapping used by both
  the factory and the benchmark, and because ORT GenAI has no WebGPU provider
  every GPU-flavoured policy resolves to DirectML for it, which its note says. The row is always present and only toggles enabled state
  and note text — hiding it for faster-whisper or a remote engine would shift
  every field below it, which a test pins by asserting the checkbox under it
  keeps its y-position across all four cases.
- **`auto` starts with the device a benchmark measured as fastest
  (`onnx_auto_preferred_devices`, no schema bump)** (2026-09-18). Which device
  is quicker depends on the machine, and until now the only way to act on a
  benchmark was to read its table and pin a device by hand. A finished in-app
  run that measured a Cohere, Granite or Nemotron model on at least two
  devices now stores, per model, the device `auto` should try first.
  `settings_store.preferred_onnx_device(settings)` is the single reader; the
  factory, the controller's runtime identity and the note under the picker all
  go through it. Rules to keep intact:
  - **The preference lives inside `AppSettings`, not in a store of its own.**
    `reload_settings` compares `_transcriber_identity(previous)` with
    `_transcriber_identity(current)`, so a value read from a separate store
    inside the identity could never invalidate the loaded runtime; and the
    factory, the job snapshots, the isolated runtimes, the Import tab and the
    retranscribe dialog already receive `settings`. The dict is never mutated
    in place -- `dataclasses.replace` shares the reference across snapshots.
  - **No schema bump.** An absent key is an empty map, and
    `normalize_onnx_auto_preferred_devices` keeps an entry only when its key
    is in `DEVICE_AWARE_LOCAL_MODELS` and its value in
    `ONNX_MEASURABLE_DEVICES`. An older build drops the key on its next save,
    which costs the measurement and nothing else.
  - **The save path carries the field, it does not construct it.**
    `_construct_settings_from_widgets` names every `AppSettings` field, and a
    field without a widget left at its default differs from
    `_populated_settings`, counts as an edit in `_dialog_edits_over_stored`
    and erased the measurement on every Save -- including the write the
    benchmark had made while the dialog was open. It is carried from
    `_populated_settings`, like `schema_version`. Any future field without a
    widget has this shape.
  - **The benchmark path writes the store first, then both dialog snapshots,
    then emits `settings_changed` -- for every write, also one that reorders
    nothing.** The controller's own setters (overlay opacity, pin, language)
    save their whole snapshot, so a controller that never reloaded would write
    the map back as it was before the run. Only a run that ran to its end
    decides (`completed` / `completed_with_errors`); a canceled or failed run
    does not, and neither does a dialog that is shutting down. An unchanged
    map writes nothing and emits nothing.
  - **The winner rule is conservative** (`measured_fastest_devices`): cases
    with no error, with runs, a finite positive mean RTF and a resolved device
    the model's own chain contains (`onnx_auto_device_order`); fewer than two
    devices is no comparison; another device replaces the incumbent only when
    it was at least `MEASURED_DEVICE_MIN_GAIN` (10%) faster. **The incumbent
    is the device `auto` starts with today**: the stored one when this run
    measured it, else the first measured device of the default order. Judged
    against the default alone (the first version, found by the review), a run
    that measured the stored device 5% ahead -- still ahead, inside the band
    -- moved `auto` back to the device its own numbers called slower,
    reloaded the model for it, and the next run could move it forth again. A
    stored device the run did not measure defends nothing. An error case
    stores the *requested* target and a successful one the *resolved* runtime
    device, which is why only successful cases count. The mean RTF is used
    because it is the number the results table shows. Measured on the six
    benchmark runs recorded on the development machine: the runs of one case
    differ by a median of 4% (31 cases, up to 36% where the first run carried
    the warm-up), so 10% is a design value above that noise, not an optimum.
  - **An identity slot of its own, and only when it says something.**
    `effective_preferred_device` answers "" for a pinned policy, for a device
    the chain cannot reach and for one that already leads it, so
    `_TranscriberIdentity.onnx_preferred_device` moves -- and the model
    reloads -- only when the selected model's first device really changes. On
    the development machine the usual outcome is WebGPU measured and WebGPU
    kept: the map gains an entry, the note says so, nothing reloads, and the
    run's status line says "keeps WebGPU first" rather than "now starts with"
    (`auto_first_onnx_device` before and after the write decides which).
  - **The Node runner keeps the policy string `auto` and takes the device as
    an optional `--prefer`**, sent only when non-empty, so the command line
    without a measurement is byte-identical to the one every earlier build
    sent. A running app re-reads `webgpu_asr_runner.mjs` whenever it starts a
    child, so the file must keep accepting a caller that sends no `--prefer`.
    `resolveDevice(requested, preferred, platform)` rotates a known device to
    the front for `auto` only and ignores an unknown one;
    `transcribeWithFallback` is unchanged. Nemotron gets the same rotation
    through `nemotron_provider_order(policy, preferred)`. The benchmark itself
    never applies the preference: its `auto` target measures the default
    order.
  - **A measured CPU is not a fallback.** `runtime_status_text` says "ONNX
    runtime active on CPU (the fastest device for this model in your
    benchmark)." and `_set_runtime_status` clears `runtime_warning`, where the
    fallback sentence would claim a GPU attempt that never happened;
    `_should_restart_after_cpu_fallback` stays False because no fallback error
    was reported.
  - **A comparison that measured one device says so**
    (`uncomparable_device_models`). On a machine whose GPU targets all fail,
    the run the note asks for ends with one error case and one CPU case;
    nothing is stored, rightly, and the status line reads "Auto's device order
    for <model> is unchanged: only CPU could be measured." -- for the selected
    model only, and only when the run tried that model on more than one
    target. Storing "cpu" on that evidence was rejected: a GPU that failed
    once (a driver not back after a resume) would pin the CPU until the next
    benchmark, under a status line calling it "the fastest device".
  - **The notes say "your benchmark", never "your last benchmark"**: the map
    is merged across runs, so an entry can be older than the last run, which
    may have measured other models only.
  - **The notes stopped restating the order.** The runtime note beside the
    model combo said "Auto tries WebGPU, then DirectML, then falls back to
    CPU", which a measurement makes false; the ONNX Device row owns the order
    now, in its two reserved lines with the whole sentence in the tooltip.
  Verified with the real thing in a sandboxed `APPDATA` (2026-09-18, Ryzen 5
  7600X + Arc A750): the OS-reported child command line carries `--prefer cpu`
  for a stored CPU and is unchanged for a stored WebGPU or a pinned policy; a
  real "GPU + CPU comparison" run measured Granite 4.0 1B at 0.097 (WebGPU)
  against 0.441 (CPU) and kept WebGPU, and a Cohere run that started from a
  stored CPU measured 0.115 against 0.180 and moved `auto` back to WebGPU.
  Applied to the recorded 2026-08-25 run, the rule keeps WebGPU for all three
  pipeline models. Not measured: Nemotron on DirectML (the installed ORT GenAI
  build is CPU-only) and a machine on which the CPU wins.
- **Model size estimates are measured, not copied**: `MODEL_ESTIMATED_SIZE_MB`
  drives the download percentage, so a wrong number is directly visible.
  `distil-large-v3.5` was listed at 756 MB against a real 1513 MB `model.bin`,
  so its bar read "approx. 100%" at half the transfer and kept counting. Verify
  a new entry against the repository with the download allow-patterns applied.
- **Download parallelism is a measured non-lever**: `snapshot_download`'s
  `max_workers` parallelizes across *files*, and every local ONNX model is one
  dominant weight file (Parakeet 652 of 671 MB), so it cannot help. Measured on
  a ~70 Mbit/s line: 2 workers 76.7/77.6 s against 8 workers 76.6/76.4 s. Do
  not raise it without a new measurement, and do not add parallel *model*
  downloads: the bandwidth is shared either way while every extra writer needs
  its own worker process, progress row and cancel/cleanup bookkeeping.
- **`stt_app/transcriber/__init__.py` resolves its names lazily (PEP 562)**:
  importing any submodule runs the package first, so
  `import stt_app.transcriber.local_faster_whisper` used to pull in the
  AssemblyAI, Azure, Deepgram, ElevenLabs, Fun-ASR, Groq and OpenAI modules
  with it. The download and inventory-scan worker subprocesses do exactly
  that and paid 0.232 s / 330 modules per launch for provider code they never
  call; they now pay 0.114 s / 234. `__getattr__` caches each resolved name in
  `globals()`, `__all__` is derived from the lazy map, and a test pins both
  the public surface (so a name cannot be dropped by editing the map alone)
  and the agreement between the `TYPE_CHECKING` imports and that map -- a name
  typed for static checkers but missing from the map is an `AttributeError`
  no type checker can see. **Do not delete the `if TYPE_CHECKING` block.** It
  keeps editors and linters working, and it is also the packaged app's only
  static link to `factory` and the providers: PyInstaller's modulegraph scans
  `IMPORT_NAME` opcodes without following control flow, so it walks into that
  block and finds them, while `_LAZY_ATTRIBUTES` is strings it cannot read.
  Verified on PyInstaller 6.22: a graph rooted at
  `from stt_app.transcriber import create_transcriber` contains `factory`, all
  seven providers and all four local runtimes; the fifth, `local_granite_ctc`,
  was confirmed on 2026-09-19 by running a bundle built with the same
  PyInstaller through the frozen-bundle check. The typed/lazy agreement test
  is what keeps the block from being deleted or renamed.
  Note that the package no longer binds its submodules as attributes until
  something resolves a lazy name, so `stt_app.transcriber.base` raises
  `AttributeError` in a fresh interpreter. `unittest.mock` and pytest both fall
  back to `import_module`, so every existing patch target still works; do not
  write new code that reaches a submodule through `getattr` on the package.
- **A picker label never hand-writes a model's size**: `LOCAL_MODEL_LABELS` is
  built from a name-and-notes table plus `MODEL_ESTIMATED_SIZE_MB`, which is
  the table corrected whenever a real download disagrees with it. Written
  twice, the two drifted: `distil-large-v3.5` read "~756 MB" against a measured
  1516 and `large-v3-turbo` "~809 MB" against 1622 -- the two models a user
  picks between by size, both understating themselves by half, while AGENTS.md
  already recorded the 756 MB figure as a *fixed* defect. Nearly every other
  entry was a few percent out from dividing by 1000. A test rejects any label
  stating a size more than 5% from the table.
- **The delete confirmation names every folder it will remove**: the inventory
  searches the Model Dir *and* the default Hugging Face cache, so one row can
  mean a copy in either, and the shared cache holds models other tools put
  there. "This removes downloaded files from disk" did not say which disk.
- **`MODEL_ESTIMATED_SIZE_MB` is decimal megabytes**, it says so, and
  `model_download_progress` converts it with `* 1_000_000`. Anything rendering
  a size from it divides by 1000. Dividing by 1024 and writing "GB" names
  neither unit, and the test that should have caught it divided by 1024 as
  well -- a test that shares the code's misunderstanding is not a check.
  `scripts/benchmark_local.py`'s `_bytes_to_human` was the second instance,
  and worse because it feeds the *same table column* as the
  `MODEL_ESTIMATED_SIZE_MB` branch beside it: one model reported two
  different numbers under the same unit depending on `--show-sizes`
  (Parakeet 670.00 against 638.96). `update_ui.py`'s download label was the
  third: it divided by 1024 squared and wrote "MB".
- **An extensible WAV is decoded by its SubFormat, never by its format
  tag** (F08 of the 2026-09-12 review). `decodeWavFile` in
  `webgpu_asr_runner.mjs` read format tag 0xFFFE (`WAVE_FORMAT_EXTENSIBLE`)
  as PCM and never looked at the 22-byte extension, so an extensible
  32-bit IEEE-float file -- what recorders and editors write for anything
  past two channels or 16 bits -- decoded as int32 (measured with the real
  function in a Node process: 0.001 came back as 0.4571250081062317, the
  largest error 0.503), while extensible PCM16 happened to decode exactly.
  The app's own recordings are classic PCM; the Import Audio tab and the
  benchmark hand the user's file to this decoder unvalidated.
  `readExtensibleFormatCode` requires a 40-byte `fmt` chunk and a `cbSize`
  of at least 22, reads the SubFormat GUID's first four bytes as the
  format code (1 PCM, 3 float; the GUID's tail
  `0000-0010-8000-00AA00389B71` is the same for both) and rejects any
  other SubFormat, a short chunk or a short `cbSize` in the module's
  "Invalid WAV file" wording -- before this the reverted SubFormat check
  reported `Unsupported WAV encoding: 65534`, which names nothing the
  user can act on. It does not check `cbSize` against the chunk's real
  size: a `cbSize` of 65535 inside a 40-byte chunk is accepted, because
  only the first 22 extension bytes are read and they are there, and
  Windows accepts such files. The test fixtures are built from raw bytes
  (`wave` cannot write the extensible header) and are synthetic; no file
  from a real recorder was decoded.
- **A temp file's path is recorded before the write, not after.**
  `NamedTemporaryFile(delete=False)` has already created the file when it
  returns, so the four batch paths that spool audio to `%TEMP%`
  (faster-whisper, the Cohere/Granite ONNX runtime, AssemblyAI, Groq) left
  `temp_path` at `None` whenever the write itself failed -- a full disk, a
  quota -- and their cleanup then skipped a file that really existed, once per
  failed dictation, forever.
- **A CPU fallback is the normal answer on a machine without a GPU, so it must
  not trigger a restart every time.** `resolveDevice("auto")` on Windows
  returns `["webgpu", "dml", "cpu"]`, so a CPU-only machine always reports two
  `fallbackErrors` plus `device: cpu` -- and the restart-on-fallback path
  therefore paid a full Node + ONNX model load on *every* dictation, forever.
  `_MAX_CPU_FALLBACK_RESTARTS` bounds it to one attempt, and the counter is
  reset whenever the runtime does come up on an accelerated device, so a
  transient GPU failure still gets its one retry later.
- **The ONNX child's stdout queue is bounded, and drops the oldest line.**
  Only `_read_json_message` drains it, i.e. only while a request is in flight,
  so a child that keeps writing between requests -- or one discarded after a
  protocol timeout with lines still in the pipe -- parked the reader thread
  inside a plain `Queue.put` for the rest of the process's life, holding its
  `Popen` and all three pipe handles, and every restart leaked another set.
  `_push_bounded` evicts the oldest entry rather than refusing the new one,
  because the newest protocol line is the one a caller is waiting for; a
  response genuinely lost that way surfaces as the request timeout that
  already discards the child.
- **The local inventory answers from the directory the loader resolves.** For
  a faster-whisper model that is `download_destination_dir(name, model_dir)`
  and nothing else: the app always passes a size name, so `WhisperModel` calls
  `snapshot_download(repo_id, cache_dir=download_root)`, which reads that one
  cache root and the `models--<repo>` layout alone. Searching the default
  Hugging Face cache as well, or accepting a flat folder, reported "cached"
  for a model the Models tab then refused to offer a Download button for while
  the next dictation fetched it again -- and offline mode could not load it at
  all. It is also the gate `_coordinated_download_if_missing` already used, so
  the inventory and the load path cannot disagree. **A snapshot counts only
  with every file `WhisperModel` reads** (`_snapshot_is_complete`, one rule for
  both): `config.json`, `model.bin`, `tokenizer.json`, a `vocabulary.*` file,
  and `preprocessor_config.json` for the three repositories that ship one
  (`PREPROCESSOR_CONFIG_MODELS`: large-v3, large-v3-turbo, distil-large-v3.5,
  from their file listings on 2026-09-27). With only the first two the gate
  answered "valid" and the constructor fetched the rest itself, past the
  download slot, the orphan removal and the settled symlink probe (review
  round 3, reproduced 2026-09-21); without the tokenizer it asks the Hub for
  openai/whisper-tiny's, and without the preprocessor config it keeps 80 mel
  bins where those three expect 128. `scripts/import_model.py` validates an
  import against the same set, or an import would land as a model the Models
  tab calls missing. The ONNX half stays
  delegated to `find_cached_webgpu_models` and deliberately does accept the
  other roots and the legacy layout, because `resolve_cached_webgpu_model_root`
  loads from them.
- **The default Hugging Face cache directory is the library's own answer,
  resolved in exactly one place** (F13 of the 2026-09-12 review).
  `local_webgpu_asr.default_hf_cache_dir()` returns
  `huggingface_hub.constants.HF_HUB_CACHE` (imported inside the function:
  the inventory-scan and download worker subprocesses import the module,
  and the import measured +2 modules and 0.002 s on top of the module's
  own), and the inventory, `download_destination_dir`, the ModelScope
  fallback, the download slot's lock identity in
  `model_download_coordinator` and `scripts/import_model.py` all ask it.
  Three byte-identical private copies used to read `HF_HOME` before
  `HF_HUB_CACHE` and ignore `XDG_CACHE_HOME`, while the installed
  `huggingface_hub` 1.8.0 lets `HF_HUB_CACHE` win outright, then
  `HUGGINGFACE_HUB_CACHE`, then `HF_HOME` (itself `$XDG_CACHE_HOME/
  huggingface` or `~/.cache/huggingface`) plus `hub`. With both
  variables set the faster-whisper download -- `snapshot_download`
  without `cache_dir` for an empty Model Dir, i.e. the library's own
  resolution -- landed in `$HF_HUB_CACHE` while
  `_coordinated_download_if_missing` looked in `$HF_HOME/hub`, so the
  model was never found and every dictation re-entered the download path;
  with `XDG_CACHE_HOME` alone the two disagreed the same way. Two
  properties to keep: `local_faster_whisper` imports the function at
  module scope, so `stt_app.transcriber.local_faster_whisper.default_hf_cache_dir`
  stays a patchable module attribute (about 25 tests patch it there and
  one patches the webgpu module's; patching one does not patch the
  other), and the answer is the constant the library computes at its
  first import, so a variable changed afterwards moves neither the app's
  answer nor the download -- nothing in `src/` writes those variables at
  runtime, and `tests/conftest.py`'s isolation holds because nothing it
  imports pulls `huggingface_hub` in before `pytest_configure` has set
  them (measured: not in `sys.modules` when the first test runs).
  `tests/test_hf_cache_dir.py` runs one child interpreter per environment
  combination with an environment built from scratch and asserts the
  app's answer equals the library's in that child.
