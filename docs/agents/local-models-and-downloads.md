# Local models and downloads: design decisions

Binding project rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30. Read before changing local runtimes (faster-whisper, onnx-asr,
Granite CTC, Nemotron, Node/ONNX), the model inventory, the download slot or
download progress. Each entry gives the rule, the reason and the decisive
evidence; earlier versions and full measurements are in `docs/learning-log.md`
and git history. Entry order is kept ("above/below" refers to this file);
"Known limitations" is `docs/agents/known-limitations.md`.

Verbatim pre-condensation text: `git show e608f86:docs/agents/local-models-and-downloads.md` (original AGENTS.md: `df2642a`).

- **Temp files for audio**: `transcribe_batch` writes a temp WAV because
  `WhisperModel.transcribe()` is most reliable with file paths.
- **Inventories live in their own JSON cache, not `settings.json`**, so the
  Models/Benchmark tabs render at once without mutating settings; disk
  verification follows after paint and at startup. The scan runs in a
  subprocess so it cannot stall Qt. Timings are logged as `settings_timing`;
  the lists keep `AdjustToContents` (check the log before changing that).
  The tray prepares a hidden settings dialog after startup; it reloads
  settings before showing.
- **Every caller parked in the download slot is registered**
  (`ModelDownloadCoordinator.has_waiting_download`), and a cancelled Local-tab
  download keeps partials while one exists: `has_explicit_interest` cannot see
  a parked preload, whose resume bytes a cancel otherwise deleted. Register
  unconditionally, give back in a `finally`; counting only when the slot looks
  busy races with `release` clearing `_active`.
- **A preload queued behind another download says so**
  (`_preload_waits_for_another_model`): `_download_model_for_preload` sets
  phase `download` before blocking in `acquire`, so detail and phase word must
  both ask, or the overlay shows a frozen percentage of an idle directory.
- **Local download queue**: one worker process, serial (predictable cache
  writes, safe kill). Waiting rows show `Queued, 2 of 3`. No parallel
  downloads: same bandwidth, more processes and cleanup bookkeeping. Cancel
  clears the queue and removes unusable `*.incomplete`, keeping complete files.
  - A Download pressed while the worker drains a cancel runs (it was queued
    after the cancel): the worker consumes the event and continues, exiting
    only on an empty queue.
  - A drain that saw a Cancel reports it: `_cancel_local_model_downloads`
    records discards in `_local_model_download_removed_by_cancel`, consumed
    with the event by `_consume_cancel_locked`; `_canceled_drain_summary`
    renders ordered groups (downloaded, failed, canceled, removed), later ones
    starting "Then". "Download canceled." only when something was killed or
    removed. A drain that ran nothing says "No downloads ran." (success False,
    warning colour), never "Downloaded: ".
  - `_start_local_model_download` refuses after `_shutdown_started`, or the
    `aboutToQuit` drain starts a fresh download.
  - The cleanup sentence reports a state: `_CleanupOutcome` carries
    `_CLEANUP_NONE` / `_CLEANUP_RAN` / `_CLEANUP_KEPT` / `_CLEANUP_SKIPPED`
    through `_run_download_worker`, because zero counts also arise from
    `_cleanup_unless_awaited` keeping partials for a waiter, the
    `ModelDownloadCanceled` arm of `_download_local_model_in_subprocess`, and
    a Cancel that interrupted nothing. Sentences name their model (`cleanups`
    holds `(model, outcome)`); "No incomplete files remained." speaks only
    when nothing else does.
  - `cleanup_incomplete_model_download` counts after a successful unlink and
    returns `IncompleteCleanup(removed_files, removed_bytes, left_files)`;
    `_CleanupOutcome.left_files` yields "could not be removed: still in use"
    (`scripts/download_model.py` has its own). `_remove_partials_under` tries
    every partial once (`_unlink_once`), clears read-only on the refused ones,
    pauses 10 ms once for the whole sweep, then retries them
    (`_retry_unlink`) and asks `exists()`: a file another program is deleting
    refuses, then vanishes. A file that stays gets its read-only attribute
    back.
  - `_model_cache_dirs` dedupes roots by `_same_directory_key` (`realpath`
    with an extended-length `\\?\` prefix folded away), not `normpath` (8.3
    short names and the prefixed spelling counted a held partial twice);
    returned paths keep user spelling.
  - Preloads report it too: `_note_preload_cleanup` (by generation), appended
    by `_on_model_preload_done` for an explicit cancel and for a save leaving
    the local engine (no `_preload_cancel_requested`; failure arm).
  - The crash arm clears `_local_model_download_removed_by_cancel` too.
- **Download progress is the downloader's own byte count, not directory
  growth** (2026-09-20). `estimate_cached_model_bytes` sums `st_size`, the
  highest offset, and Xet writes up to 60 ranges out of order (36.4% claimed,
  12.1% present). The worker (`local_model_download_worker.py`) installs
  `snapshot_download(tqdm_class=hub_progress_tqdm_class(...))` and streams
  `@@STTDL@@{"event":"bytes",...}`; a daemon reader
  (`local_model_download._pump_progress`) must drain it or the child blocks in
  hf_xet's callback. `model_download_process_error` uses `wait()`, not
  `communicate()` (second reader).
  - Select the bar by exact name `huggingface_hub.snapshot_download`; 1.32.0
    adds `...snapshot_download.transfer` (Xet-deduplicated) and `thread_map`
    a files bar, so selecting by `unit` double-counts.
  - Total is `max(reported, MODEL_ESTIMATED_SIZE_MB)` (the Hub grows totals
    per file; an early 1.1 MB total read 99%); "approx." drops once reported
    reaches the table. Percent is monotonic. Rates need a >= 1 s span.
  - Unrecognised input is ignored and the parent falls back to directory
    growth, logged once as `model_download_progress_absent`. The ModelScope
    mirror has no hook; the worker sends `DOWNLOAD_PROGRESS_UNKNOWN` so the
    display does not freeze.
  - Progress bar and "Next: ..." line keep their space while hidden
    (`setRetainSizeWhenHidden`; buttons moved 25 px without it).
  - Already-complete files are invisible to the hook:
    `completed_download_bytes` measures a baseline first and
    `offset_progress_hook` adds it, excluding `*.incomplete`, `*.ms-part`
    (reported via `initial=`), symlinks and `<local_dir>/.cache`, capped at
    `MODEL_ESTIMATED_SIZE_MB`. A blob hub copied into `snapshots/` (no
    symlinks, `_create_symlink(..., new_blob=False)`) counts once: a same-size
    real file there is that blob's copy. A source switch keeps the high-water
    mark and clears only the rate samples.
  - A reader that outlives its join owns the pipe: on Windows closing a pipe
    under a blocked read blocks (16.56 s with a grandchild holding stdout).
    `_close_progress_reader` logs `model_download_progress_reader_still_reading`
    and leaves the close to the reader. `release_model_download_process` waits
    for nothing (mostly Qt-thread callers); the spooled error log holds a lock
    across read-and-close and release, or the only error text read as "". The
    malformed-event latch lives on the child's `_ProgressState`.
  - huggingface_hub 1.32.0 never resumes partials (upstream PR #4228:
    process-unique `<etag>.<8 hex>.incomplete`, deleted in a `finally` a
    killed child never runs). `remove_orphaned_hub_partials` clears
    `*.incomplete` (never `*.ms-part`, which the mirror resumes) under
    `download_destination_dir` only, not following junctions or symlinks
    (`_partials_below`), never raising; it runs in
    `start_model_download_process` before the child starts and in both
    snapshot functions (load-path downloads and `scripts/download_model.py`
    bypass the launcher). A cancelled large file restarts from zero; stay on
    the newest hub anyway (Xet repos never resumed).
  - Settle hub's symlink probe before threads start (`_settle_symlink_probe`,
    keyed by the `commonpath` of blob and pointer): `are_symlinks_supported`
    caches `True` before testing, so without the privilege a second thread
    dies with `OSError [WinError 1314]`, not the `PermissionError`
    `_create_symlink` catches (11 of 12 cold runs failed, 0 after). Only
    `models--<repo>` layouts; guarded by `except Exception`.
  - The child runs with `HF_HUB_DISABLE_SYMLINKS_WARNING=1`, or the warning's
    `warnings.warn(message)` line becomes a failed download's reason.
  Verified against a local stand-in only, not Xet or the real Hub; such probes
  must set `STT_APP_DISABLE_MODELSCOPE=1` or they fall back to the mirror.
- **Exactly one download slot, across every process of one Windows user**
  (`model_download_coordinator`), because separate preload and queue workers
  on one cache caused hidden downloads, 0%-forever bars and deleted partials.
  - Every download goes through it, including load-path downloads
    (`_ensure_snapshot` / `_resolve_model_path` via `run_coordinated_download`;
    the only download Cohere/Granite have with `keep_onnx_model_loaded` off).
    `WhisperModel(...)` downloads in its constructor, so `_ensure_model`
    fetches via the slot first, gated on `download_destination_dir` +
    `_has_valid_model_snapshot`; `find_cached_models` is too broad.
  - `acquire()` returns `ACQUIRE_JOINED` if the same model finished while it
    waited (idempotent).
  - Explicit (Local-tab) interest registers at enqueue; the preload checks
    `has_explicit_interest` before deleting partials. Abandoning the queue
    gives claims back (`_discard_queued_downloads_locked`); every exit from
    `_download_local_model_in_subprocess` clears
    `_local_model_download_claimed`, or the model is stranded for the session.
  - `claimed` is not `active`: list, pending set and duplicate check see a
    claimed entry; the bar reads `_local_model_download_active` only.
  - `_poll_preload_download_state` drives
    `_refresh_local_model_download_progress`; the bar tracks its own
    shown-state (children of the hidden dialog report invisible).
  - Nothing waits for the slot on the Qt thread (Nemotron loads in its stream
    worker, not `start_stream`). `main` connects `request_download_shutdown`
    to `aboutToQuit` before teardown, or a waiter starts a download the
    interpreter joins at exit. The queue worker is wrapped in
    `try/except BaseException`.
  - Two layers: an in-process `threading.Condition` (join, interest) and a
    kernel lock (`file_lock.CrossProcessLock`: `msvcrt.locking` /
    `fcntl.flock`) for the benchmark worker and `scripts/download_model.py`
    (a second app is refused by the single-instance guard in `main.py`). A
    kernel lock, not a PID file: the OS drops it on any exit. It does not
    reach another Windows user (`appdata_root() / "locks"`; Known
    limitations).
  - Keyed on the cache directory, not the model (shared blob/ref trees); an
    empty Model Dir is one identity. Taken after the in-process slot and
    outside the condition (waits can take minutes); every exit hands the slot
    back; a filesystem that cannot lock warns and degrades to process-local.
  - The gap between holding the OS lock and storing it in `self._cache_lock`
    (read by `_release_cache_lock`) must not raise: publication releases and
    re-raises; `CrossProcessLock.release()` is idempotent, swallows `OSError`.
  - The app's own download subprocess takes no lock (the parent holds the slot
    for `_run_download_worker`); `scripts/download_model.py` takes it.
- **Progress measures the download destination, never a candidate copy**:
  `local_faster_whisper.download_destination_dir` is the single source (ONNX:
  `local_webgpu_asr.webgpu_download_destination`, the flat `local_dir` of
  `download_webgpu_model_snapshot`; faster-whisper: `models--<repo>` under
  `cache_dir`). No `max()` over `_model_cache_dirs`, which exists for
  detection/delete/cleanup (a foreign 9.4 GB fp32 copy pinned a download at
  100%). A parametrized test pins `webgpu_download_destination` to the real
  `local_dir`; symlinked snapshot entries are skipped. One fallback: while the
  destination is absent, `_complete_cached_model_root` may size a root with a
  complete, loadable snapshot (`resolve_cached_webgpu_model_root`,
  `_has_valid_model_snapshot`), so a legacy copy the loader uses (Cohere here)
  is not shown at 0%.
- **ModelScope mirror downloads are transactional and path-contained**:
  listing paths are untrusted; only normalized POSIX-relative paths inside the
  destination (no absolute, drive, traversal or backslash paths); HTTPS
  endpoint and redirects. Write only to `*.incomplete`; resume only on a
  matching HTTP 206 and `Content-Range`, else restart. Publish after flush,
  sync, exact-size check and atomic replace; never a partial at a final name.
- **Manual imports are transactional**: `scripts/import_model.py` hashes
  files, stages a complete snapshot under a temporary name, repairs legacy
  partials, renames atomically, then updates `refs/main`; a failed copy leaves
  no final snapshot and no reference to it.
- **Local ONNX ASR (Node)**: Cohere Transcribe, Granite Speech 4.0 and 4.1 via
  `transcriber/local_webgpu_asr.py`, batch-only, Node.js required; supported
  daily-use models, never "experimental". All are q4 through the
  Transformers.js `GraniteSpeechForConditionalGeneration` pipeline; 4.1 2B is
  `onnx-community/granite-speech-4.1-2b-ONNX`.
  - Granite 4.1 Plus and NAR were retired 2026-08-26 with the raw
    `onnxruntime-node` graph runtime (RTF 0.447 / 4.149 on CPU vs 0.099 for
    base on WebGPU; broken output). Cause in
    `docs/granite-speech-4.1-onnx-variants.md` (5-D `Einsum` and `MatMul`
    nodes). Before re-adding a raw-graph model read it and check every
    required file against the real repo listing (a test covers the
    allow-patterns); never relabel a Plus build as `granite_speech`.
  - Quote RTF as the mean of a case's runs.
  - `keep_onnx_model_loaded` defaults on (else every dictation pays the Node +
    ONNX load); off means no preload and close after each batch.
  - `DEFAULT_MODEL_SIZE` is `parakeet-tdt-0.6b-v3` (2026-08-27): RTF 0.043 vs
    `small` 0.154 on a 24.3 s German clip (Ryzen 5 7600X CPU), 25 languages,
    pure Python. Batch-only, matching `DEFAULT_MODE` `batch`. First-run only
    (`SettingsStore.load` defaults absent keys).
    `DEFAULT_FASTER_WHISPER_MODEL_SIZE` (`small`) stays the in-runtime default
    (`LocalFasterWhisperTranscriber`, benchmark CLI).
  - Keep `granite-4.0-1b-speech` selectable until benchmarks justify removal.
- **Nemotron 3.5 true streaming**: `nemotron-3.5-asr-streaming-0.6b-int4`
  (793 MB ORT GenAI export, `transcriber/local_nemotron.py`) reuses the
  encoder cache and emits RNNT tokens per fixed 560 ms chunk. The CPU ORT
  GenAI package ships; DirectML is tried first when present (its GenAI package
  needed an unpublished `onnxruntime-directml>=1.26.0` as of 2026-06-08). RTF
  0.21, 1.78 s cold load (CPU, 2026-08-25); never quote figures from the
  synthetic `samples/benchmark_sample.wav`. Stays preloaded; its VAD follows
  the app's; the language UI shows only transcription-ready and
  broad-coverage official prompt IDs.
- **`onnxruntime-node` is not a direct dependency; do not add it back.** A
  second declaration puts two native ORTs in one Node process. The pipeline
  uses Transformers.js's pinned copy, replaced by an `overrides` entry;
  `npm ls onnxruntime-node` must show one entry under
  `@huggingface/transformers`, marked `overridden`. A blocked `postinstall`
  (npm 12 script policy) is harmless: binaries are bundled
  (`bin/napi-v6/win32/x64`), `listSupportedBackends()` reports cpu/dml/webgpu.
- **Transformers.js 4.3.0 (2026-09-18); the one `overrides` entry pins
  `onnxruntime-node` 1.29.0 because it is faster** (2026-09-27). 4.3.0's own
  `sharp: ^0.35.4` and patched `adm-zip` 0.6.1 retired the old overrides;
  `npm audit --omit=dev --package-lock-only` is clean. `npm install
  --package-lock-only` keeps a lock entry that still fits a range; `npm audit
  fix --package-lock-only` moves it.
  - Evidence: installs differing only in the override, through
    `LocalOnnxWebGpuTranscriber` (`runner_path`), Arc A750: 1.29.0 faster on
    WebGPU for every model and clip (Granite 4.1 2B 1.69 vs 3.01 s), same
    transcripts. 1.27.0 needs a vulnerable `adm-zip ^0.5.16`. Re-measure and
    drop the override when Transformers.js pins a newer ORT or 1.31 ships.
  - Owner preference: newest release with fewest known vulnerabilities; pin
    lower only with a measured reason recorded here.
  - The override nests the package under
    `node_modules/@huggingface/transformers/node_modules/`, which Node resolves
    first; `benchmark_environment._node_package_version` reads there first.
  - A running source-tree app holds its Node child's native DLLs; run
    `npm ci --omit=dev` only with the app closed.
- **Python dependencies are exact pins, updated 2026-10-03 and measured
  through their code** (locked: onnxruntime 1.30.0, onnxruntime-genai 0.17.1,
  huggingface-hub 1.33.0, hf-xet 1.6.0, ctranslate2 4.8.2, av 18.1.0; direct
  pins are in `pyproject.toml`).
  `uv lock --upgrade` moves only transitive packages; `uv tree --outdated
  --depth 1` shows stale direct pins; `requirements-win.txt` /
  `requirements-dev-win.txt` mirror them (a test compares). Evidence: suite,
  `scripts/release_check_providers.py`, one real model per runtime. `uv audit`
  and `pip-audit` report no known vulnerability on the lock.
  - **`av` is a direct pin on the last 18.x, below the newest (19.0.1,
    2026-10-03), because faster-whisper 1.2.1 breaks on 19.** It requires only
    `av>=11` but calls `av.open(..., metadata_errors="ignore")`, which av 19
    rejects with `TypeError`; every file-path transcription through
    faster-whisper then fails (measured: `LocalFasterWhisperTranscriber.
    transcribe_batch` on av 19.0.1, fine on 18.1.0;
    `test_installed_av_decodes_a_file_the_way_faster_whisper_opens_it` fails on
    19). Lift the pin when a faster-whisper release newer than 1.2.1 supports
    av 19, and re-run that test.
  - **`huggingface-hub` stays 1.x because `tokenizers` 0.23.2 requires
    `<2.0`** (2.1.1 exists); nothing to measure until tokenizers lifts it. The
    1.32.0 download claims below (no partial resume, process-unique temp
    files) were re-checked on 1.33.0: its diff touches no resume or temp-file
    path, and the download/progress suites pass.
  - ORT stays 1.30.0 (newest); genai 0.17.1 needs `onnxruntime>=1.30.0`.
    Nemotron on 0.17.1 gave text identical to 0.16.0 on six clips in batch and
    streaming, at equal or better speed (2026-10-03, CPU).
- **onnx-asr engine (Parakeet TDT 0.6B v3, Canary 1B v2)** in
  `transcriber/local_onnx_asr.py`: pure Python, no Node.js, no extra ORT
  (`onnx-asr[cpu,hub]`). Download, detection, sizing and deletion reuse
  `_OnnxModelLayout` in `local_webgpu_asr` (int8 allow-patterns only).
  - Canary has no benchmark RTF; quote none. Parakeet is "the fastest local
    model that transcribed the recording", never "fastest" (`tiny` 0.033) or
    "most accurate"; 1.9x faster than the best GPU case
    (`cohere-transcribe-03-2026`, 0.083).
  - Never add `onnxruntime-directml`: it overwrites the `onnxruntime/`
    package (620 of 625 files, `pip check` silent) and breaks
    `onnxruntime-genai` ("The requested API version [26] is not available"),
    losing Nemotron. `onnxruntime-webgpu==1.27.0` coexists but was slower
    than CPU. A GPU path needs an isolated subprocess environment.
  - Canary never exposes `auto` (onnx-asr hardcodes `<|en|>` and translates):
    it is in `LOCAL_EXPLICIT_LANGUAGE_MODELS`, and `_normalize_language_mode`
    maps unsupported codes to trained ones (else `KeyError`). Parakeet ignores
    `language=`, so it offers only `auto` and sends none.
  - Both batch-only, excluded from the ONNX Device picker (CPU-only).
  - PyInstaller needs `collect_all('onnx_asr')` (mel/resampler graphs are
    package data via `importlib.resources`).
- **Parakeet TDT 0.6B v3 Ultra is a pinned community export** (2026-09-27):
  `Olicorne/parakeet-tdt-0.6b-v3-ultra-onnx` (CC-BY-4.0, Moondream
  post-train), run as `nemo-parakeet-tdt-0.6b-v3`, Auto only.
  - Pinned (`_OnnxModelLayout.revision`, commit `dd20322`), because a
    community upload can change under one name; pinned layouts never use the
    ModelScope mirror; `scripts/download_model.py` gives the commit.
  - `prepare_onnx_inference_dir` atomically copies root `vocab.txt` and
    `config.json` into `int8/` (onnx-asr 0.12.0 reads one folder); a failed
    write passes only if a re-read matches. Allow-patterns name the four files
    exactly (fnmatch `*` crosses `/`; the repo holds fp32/fp16/w4a8, `.zst`
    and a 2.5 GB `.nemo`).
  - v3 stays default: Ultra is faster (RTF 0.030 vs 0.044) but had higher WER
    (2.9% vs 2.2%, `docs/models.md`). The label omits the Moondream credit so
    the retranscribe combo does not clip; attribution is in `docs/models.md`.
- **Granite Speech 5.0 470M TurboCTC: one INT8 graph on the CPU in pure
  Python** (`transcriber/local_granite_ctc.py`, 2026-09-19), fifth runtime
  (`LOCAL_MODEL_RUNTIME` `granite-ctc`), owning its `InferenceSession`:
  `input_features` float32 `[1, frames, 320]` -> `logits`
  `[1, frames // 4, 16384]`, numpy log-mel, greedy CTC (blank 0),
  `tokenizers` BPE decode; no new dependency. Weights
  `qwertz92/granite-speech-5.0-470m-turboctc-onnx` rev `e6e3b4d`, nine files,
  552,442,697 bytes (`snapshot_download(dry_run=True)`); `onnx/model_int8.onnx`
  only (`onnx/*.onnx` would add fp32 and fp16).
  - No GPU path: DirectML saves 0.10-0.18 s per 29.4 s clip over INT8 CPU
    for a 1-2 GB download and a raw-graph Node runtime. Not built.
  - The extractor is a numpy port of `GraniteSpeech5FeatureExtractor` (which
    needs torch): 80 HTK mels, hop 160, `n_fft` 512, deltas, two frames
    stacked to 320. `tests/data/granite_ctc_reference.npz` holds real
    processor output; the test bounds 1e-4 (measured 5.1e-6).
  - `preprocessor_config.json` is required and compared with the hard-coded
    parameters at load, so a different re-export fails instead of emitting
    garbage.
  - Passes of at most `_MAX_PASS_SECONDS` (180 s), cut at the quietest 20 ms
    frame of each window's last 15 s (one pass of 563 s peaked at 2.39 GB).
  - Lower case without punctuation is the model; do not post-process.
  - English only, no language input: `LOCAL_ENGLISH_ONLY_MODELS` gives
    `("auto", "en")`; code that assumed `distil-large-v3.5` was the only one
    reads the set.
  - Cancel reuses `_RunAbortHandle` / `_CancelWatchdog` from `local_onnx_asr`
    with its own `RunOptions`, checked before load, between passes and
    mid-run (0.46 s). The load itself (tokenizer, `InferenceSession`, ~1.4 s)
    is not interruptible; same for onnx-asr.
  - Shared, not copied: `resolve_or_download_onnx_model` (module level,
    imports inside so monkeypatch targets resolve), `_read_wav_float32`
    (refuses header rate 0), `_pcm_audio.resample_linear`.
  - `_read_wav_float32` decodes block-wise (`_WAV_BLOCK_FRAMES`) straight into
    one preallocated mono float32 buffer sized by what the file can hold, not
    by the header's frame count. Peak is the result plus one block: 1.01x the
    file for a 480 MB stereo 48 kHz WAV, where decoding the whole data chunk
    and converting it twice peaked at 5.0x (measured 2026-10-03 with
    tracemalloc). The output is byte-identical to the whole-file decode.
  - `resample_linear` interpolates in blocks of 65,536 target samples
    (`_RESAMPLE_BLOCK`), bit-identical to the former whole-array version:
    `np.interp` only reads the two samples around a position, so each block
    gets its own slice of the integer grid. Peak is the float32 result plus
    about 4 MB (30.9 MB for a 26.7 MB result); the whole-array version held
    float64 position arrays, 427 MB for 416 s of 48 kHz audio (3.7 GB an
    hour). No anti-aliasing: see known-limitations.
  - `resample_linear` refuses rates below `MIN_SOURCE_SAMPLE_RATE_HZ` (8 kHz,
    also onnx-asr's `WrongSampleRateError` limit): a 1 Hz header turned 3 KB
    into 25.6 M samples. The readers' `<= 0` guards stay; `transcribe_batch`
    wraps the decode so an oversized recording is not a raw `MemoryError`.
  - Not device-aware (absent from `DEVICE_AWARE_LOCAL_MODELS`): the ONNX
    Device row is disabled, `benchmark_device_targets` yields one `auto` case,
    `onnx_auto_preferred_devices` never reaches it, identity reads onnx-asr's
    four fields.
  - Models tab row suffix is one rule (`LOCAL_ONNX_MODEL_RUNTIME_LABELS` +
    `supports_streaming`); every row has its full text as tooltip (rows elide
    past the 788 px viewport).
  - Sentences name models as the screen does (`local_model_short_label`).
  - Fastest local model on English (RTF 0.0135 vs `tiny` 0.0245); the
    default stays Parakeet for languages and pasteable text.
  `scripts/release_check_frozen_bundle.py --model-dir` prefers this model;
  the real 552 MB transfer and non-English audio are unverified.
- **Local ONNX device (`local_onnx_device`, default `auto`, schema 23)**: the
  "ONNX Device" row (Models tab, "Local runtime" group) feeds
  `LOCAL_WEBGPU_DEVICE_POLICIES` into `LocalOnnxWebGpuTranscriber` with the
  benchmark's wording (before, `factory.py` passed none). Re-add
  `LOCAL_ONNX_AUTO_CPU_MODELS` only for a model that loads on GPU and fails
  at inference. The device is in the transcriber cache key and the preload
  key (unlike `language_mode`); unknown values fall back via
  `normalize_local_onnx_device`. Nemotron gets the policy through
  `config.nemotron_provider_order` (factory and benchmark; GPU policies mean
  DirectML). The row is never hidden, only disabled (a test pins the next
  checkbox's y-position).
- **`auto` starts with the benchmark's fastest device**
  (`onnx_auto_preferred_devices`, 2026-09-18, no schema bump): a finished run
  measuring a Cohere, Granite or Nemotron model on two or more devices stores
  the device to try first. `settings_store.preferred_onnx_device(settings)` is
  the single reader.
  - It lives in `AppSettings` so `reload_settings`' `_transcriber_identity`
    comparison sees it; never mutate it in place (`dataclasses.replace`).
  - `normalize_onnx_auto_preferred_devices` keeps keys in
    `DEVICE_AWARE_LOCAL_MODELS` with values in `ONNX_MEASURABLE_DEVICES`; an
    older build drops the key (costs only the measurement).
  - The save path carries it from `_populated_settings` (like
    `schema_version`), not from `_construct_settings_from_widgets`: a
    widgetless default counts as an edit in `_dialog_edits_over_stored` and
    erased it. Any widgetless field needs this.
  - The benchmark writes the store, then both dialog snapshots, then emits
    `settings_changed`, or controller setters write the old map back. Only
    `completed` / `completed_with_errors` runs decide; an unchanged map writes
    nothing.
  - `measured_fastest_devices`: error-free cases, finite positive mean RTF,
    resolved device in `onnx_auto_device_order`; at least two devices; a
    challenger must beat the incumbent (the device `auto` starts with today)
    by `MEASURED_DEVICE_MIN_GAIN` (10%; runs differ by a median 4%).
  - `effective_preferred_device` answers "" for a pinned policy, an
    unreachable or already-leading device, so
    `_TranscriberIdentity.onnx_preferred_device` (and a reload) moves only on
    a real change; `auto_first_onnx_device` before/after picks "keeps WebGPU
    first" vs "now starts with".
  - The Node runner keeps policy `auto` plus an optional `--prefer`, sent only
    when non-empty; `webgpu_asr_runner.mjs` must accept no `--prefer`.
    `resolveDevice(requested, preferred, platform)` rotates a known device
    first for `auto` only; `transcribeWithFallback` unchanged; Nemotron uses
    `nemotron_provider_order(policy, preferred)`. The benchmark never applies
    the preference.
  - A measured CPU is not a fallback: `runtime_status_text` names the
    benchmark, `_set_runtime_status` clears `runtime_warning`,
    `_should_restart_after_cpu_fallback` stays False.
  - A one-device comparison says so (`uncomparable_device_models`); never
    store "cpu" from it (one GPU failure would pin CPU).
  - Notes say "your benchmark" (the map merges runs) and never restate the
    device order, which the ONNX Device row owns.
  Not measured: Nemotron on DirectML, a machine where the CPU wins.
- **`MODEL_ESTIMATED_SIZE_MB` is measured decimal megabytes**: verify entries
  against the repo with allow-patterns applied (`distil-large-v3.5` was 756 vs
  a real 1513 MB). `model_download_progress` multiplies by `1_000_000`;
  renderers divide by 1000, and tests must not share a wrong unit (past
  offenders: `scripts/benchmark_local.py` `_bytes_to_human`, `update_ui.py`).
- **Download parallelism is a non-lever**: `snapshot_download` `max_workers`
  splits by file and each ONNX model is one dominant file (2 vs 8 workers
  identical). No change without a measurement; no parallel model downloads.
- **`stt_app/transcriber/__init__.py` resolves names lazily (PEP 562)** so
  workers importing one submodule skip the providers (0.232 s -> 0.114 s).
  `__getattr__` caches in `globals()`, `__all__` comes from the map, and a
  test pins the surface and the `TYPE_CHECKING` / `_LAZY_ATTRIBUTES`
  agreement. Keep the `if TYPE_CHECKING` block: it is PyInstaller's only
  static link to `factory`, the providers and `local_granite_ctc`
  (modulegraph reads `IMPORT_NAME` opcodes, not strings). Submodules are not
  attributes until resolved (`stt_app.transcriber.base` raises
  `AttributeError` fresh; mocks use `import_module`); never `getattr` one.
- **Picker labels never hand-write sizes**: `LOCAL_MODEL_LABELS` derives them
  from `MODEL_ESTIMATED_SIZE_MB`; a test rejects labels more than 5% off.
- **The delete confirmation names every folder it removes** (a row can mean
  the Model Dir or the shared default cache).
- **An extensible WAV is decoded by its SubFormat, not its format tag** (F08):
  `decodeWavFile` in `webgpu_asr_runner.mjs` read 0xFFFE
  (`WAVE_FORMAT_EXTENSIBLE`) as PCM, so float files decoded as int32.
  `readExtensibleFormatCode` requires a 40-byte `fmt` chunk and `cbSize` >=
  22, reads the SubFormat GUID's first four bytes (1 PCM, 3 float; tail
  `0000-0010-8000-00AA00389B71`), and rejects anything else as "Invalid WAV
  file". `cbSize` is not checked against the chunk size (Windows accepts
  that). Fixtures are synthetic.
- **Record a temp file's path before writing it**:
  `NamedTemporaryFile(delete=False)` creates the file, so faster-whisper,
  Cohere/Granite ONNX, AssemblyAI and Groq leaked it when the write failed
  with `temp_path` still `None`.
- **A CPU fallback triggers at most one restart**: `resolveDevice("auto")`
  returns `["webgpu", "dml", "cpu"]`, so GPU-less machines always report
  `fallbackErrors` and reloaded per dictation. `_MAX_CPU_FALLBACK_RESTARTS`
  bounds it; the counter resets when an accelerated device comes up.
- **The ONNX child's stdout queue is bounded and drops the oldest line**
  (`_push_bounded`): only `_read_json_message` drains it, so a chatty or
  discarded child parked the reader in `Queue.put`, leaking `Popen` and pipes
  per restart. A lost response surfaces as the request timeout.
- **The inventory answers from the directory the loader resolves**: for
  faster-whisper `download_destination_dir(name, model_dir)` only
  (`WhisperModel` uses `snapshot_download(repo_id, cache_dir=download_root)`),
  the same gate as `_coordinated_download_if_missing`; searching other roots
  claimed "cached" for models re-fetched on dictation. `_snapshot_is_complete`
  requires every file `WhisperModel` reads: `config.json`, `model.bin`,
  `tokenizer.json`, a `vocabulary.*` file, and `preprocessor_config.json` for
  `PREPROCESSOR_CONFIG_MODELS` (large-v3, large-v3-turbo, distil-large-v3.5),
  else the constructor fetches past the slot or keeps 80 mel bins where 128
  are needed. `scripts/import_model.py` validates the same set. ONNX stays in
  `find_cached_webgpu_models`, which accepts other roots and the legacy layout
  because `resolve_cached_webgpu_model_root` loads from them.
- **The default Hugging Face cache is the library's answer, in one place**
  (F13): `local_webgpu_asr.default_hf_cache_dir()` returns
  `huggingface_hub.constants.HF_HUB_CACHE` (imported inside the function).
  Inventory, `download_destination_dir`, the ModelScope fallback, the lock
  identity in `model_download_coordinator` and `scripts/import_model.py` use
  it. Hand-rolled copies got the precedence wrong (`HF_HUB_CACHE`, then
  `HUGGINGFACE_HUB_CACHE`, then `HF_HOME` or `XDG_CACHE_HOME` plus `hub`), so
  downloads and lookups disagreed. `local_faster_whisper` imports it at module
  scope, so `stt_app.transcriber.local_faster_whisper.default_hf_cache_dir`
  is a separate patch target from the webgpu module's. The value is fixed at
  hub's first import; `tests/conftest.py` sets the variables in
  `pytest_configure` first.
  `tests/test_hf_cache_dir.py` compares app and library per environment in a
  fresh child interpreter.
