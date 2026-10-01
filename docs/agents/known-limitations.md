# Known limitations

Accepted, recorded defects and gaps, condensed from the list moved out of
`AGENTS.md` on 2026-09-30; the measurements and history behind each entry are
in `docs/learning-log.md` and git history. Record a new one here, not in
`docs/ROADMAP.md`.

Verbatim pre-condensation text: `git show e608f86:docs/agents/known-limitations.md` (original AGENTS.md: `df2642a`).

- **A signal between "resource held" and "flag set" can strand it.**
  `incremented = True` in `_acquire_transcriber_runtime`, `acquired = True` in
  `_download_local_model_in_subprocess`, and the `try:` after
  `coordinator.acquire(...)` in `_download_model_for_preload` /
  `run_coordinated_download`. Only a `KeyboardInterrupt` in a console run hits
  it; every fix reshapes locking and moves the window. Only the cross-process
  download lock was closed (its loser is every other process).
- **`_teardown_pending_stream_connect` swallows a `KeyboardInterrupt`**
  (`except BaseException` by design, best-effort cleanup). Console runs only.
- **The download slot is not enforced across Windows user accounts**: the lock
  lives under each user's `%APPDATA%` (`appdata_root() / "locks"`); two
  accounts sharing one Model Dir can corrupt it. Not an offered configuration;
  `_download_lock_dir`'s docstring says why the lock is not in the cache.
- **With a custom Model Dir, a faster-whisper copy only in the default HF
  cache cannot be deleted from the Models tab**: the inventory answers
  "loadable from Model Dir" (`WhisperModel(download_root=...)` reads one
  root). `cached_model_paths` / `delete_cached_model` still reach it. Fix needs
  per-model paths in the scan subprocess protocol.
- **Cancel reaches a download only while it waits for the slot**, not during
  the transfer (`run_coordinated_download` passes `cancel_check` only into
  `acquire()`): `snapshot_download` has no cancel hook and the ModelScope loop
  no poll. With `keep_onnx_model_loaded` off, a Cohere/Granite load-path
  download cannot be cancelled and holds the `max_workers=1` worker. Proper
  fix: route that download through the worker process the Local tab uses.
- **Two input devices with the same name are one entry**:
  `resolve_input_device` opens the first matching index. Names survive
  re-enumeration and reboot; PortAudio indices do not.
- Streaming inserts are append-only; focus-change detection is polled, so a
  very brief switch can be missed.
- **The post-pause append gate is energy, not a VAD; it blocks silence and
  nothing else.** A ~400 ms cough or knock after a pause can append one
  hallucinated window; a word under 80 ms voiced is dropped. "Bitte."
  (0.085 s) vs a key clack (0.080 s): no threshold separates them; the damage
  is bounded by `protected_prefix`.
- **The pause mechanism is inert in a room above the silence gate**: noise
  over `silence_gate_threshold` means `silent_seconds` never accumulates,
  `new_segment` never fires, `segment_floor` is never set. Logged once per
  session as `streaming_noise_floor_above_gate` after 20 s of above-gate audio
  (rolling; cannot tell a loud room from 20 s of pause-free speech).
- **Every number behind that gate is synthetic**: `samples/benchmark_sample.wav`
  (from `scripts/generate_sample_audio.py`) is sine tones. Do not move the
  threshold on synthetic evidence; a knock vs a short word needs a real VAD.
- **Remote batch parts are independent**: a sentence across a cut is split,
  language detection runs per part, no previous-part prompt (vocabulary goes
  with every part). A cancel between parts discards finished parts (audio
  stays reachable via Import/recovery, not Retry); a request in flight runs on.
- **A gap marker can stand for a stretch without words.** An empty part is
  judged by its loudest 100 ms window against the user's silence-gate
  threshold, so a part holding only noise or a click -- a last part can be
  20 ms, e.g. a hotkey click after 180.4 s of `gpt-4o-transcribe` -- leaves
  `[no text returned for ...]` in the transcript if the provider answers it
  with nothing. Deliberate: the error direction is a marker to delete, not
  lost speech (docs/agents/remote-providers.md). A real speech detector
  (the Silero work) would be the better judge.
- **Splitting a long import holds ~5x its file size**:
  `local_onnx_asr._read_wav_float32` keeps raw bytes plus two float32 copies
  (1,325 MB for 265 MB; ~7 GB for two hours of 48 kHz stereo). A `MemoryError`
  sends the file whole. Block-wise mono decode: P3, ~1 h (2026-09-27).
- **An Azure part of up to an hour has a 120 s socket timeout**: the factory
  passes no `request_timeout_s`. Not observed; no Azure resource (2026-09-27).
- **Non-16 kHz WAV is resampled linearly** (`_pcm_audio.resample_linear`,
  Nemotron and Granite CTC): no anti-aliasing, and float64 position arrays
  cost ~3.7 GB per hour of 48 kHz import (2026-09-19). App recordings are
  16 kHz; only imports and benchmark samples reach it.
- ARM CPUs: not supported (CTranslate2 requires x86 AVX/SSE).
- **Clipboard restore is not lossless.** Every HGLOBAL format is restored, but
  not: GDI-handle/owner-drawn formats (`CF_BITMAP` is resynthesized from
  `CF_DIB`; `CF_METAFILEPICT`, `CF_PALETTE`, `CF_ENHMETAFILE`,
  `CF_OWNERDISPLAY`, `CF_DSPBITMAP`, `CF_DSPMETAFILEPICT`, `CF_DSPENHMETAFILE`
  are lost); `CF_PRIVATEFIRST..CF_PRIVATELAST`, `CF_GDIOBJFIRST..CF_GDIOBJLAST`;
  `DataObject` / `Ole Private Data`; unrendered delayed formats; clipboards
  over the size caps (text only); unreadable blocks; the owner window.
  `CF_DSPTEXT` comes back without its `CF_OWNERDISPLAY` (harmless).
- The NVIDIA *NeMo* runtime is intentionally unimplemented (Parakeet via
  onnx-asr, Nemotron via ORT GenAI); see
  `docs/local-asr-model-candidates-2026.md`.
- **One insert offer at a time; a later failure replaces an earlier one.** In
  a flush, an earlier pre-keystroke failure (Insert useful) is replaced by a
  later post-keystroke one (Insert withheld); the earlier text is in history
  and the tray. A tray re-paste of the whole dictation failing before its
  keystroke replaces a streaming tail's offer, whose Insert then re-pastes the
  already-streamed prefix.
- **`close_if_idle` is not bounded against its own closes**: a
  `request_restart` after its generation bump reopens and each own close
  re-arms the budget (25 restarts: 3.14 s on a 0.4 s budget). Producers
  serialize on `_audio_device_refresh_lock`, off Qt.
- **`Thread.start` guards catch `RuntimeError` only**
  (`fd1b50a`/`c98f57e`, settings dialog's six); a `MemoryError` escapes.
- **`WarmMicrophoneStream.close()` does not wait for a helper's close in
  flight** (drains `_retiring` and returns). Only `shutdown()` calls it;
  `close_if_idle` is the call that waits.
- **Copy yields the pending offer after an edit made while one is pending**,
  and Edit stays disabled until the offer is retired. Offer semantics.
- **The benchmark's 6 s environment query can be outlived by a grandchild**
  holding stdout; `Get-CimInstance` spawns none, so unreachable at HEAD.
- **Two benchmark runs saved within one second share
  `BenchmarkHistoryEntry.identity_key()`** (`created_at` has second
  resolution) and open one pop-out.
- **The benchmark environment (median 2.2 s PowerShell) runs before the first
  cancel check**; a shutdown joins for 2.5 s: a worker it outlasts saves
  nothing, one it ends is saved as canceled.
- **`_model_cache_dirs` does not fold a `\\?\`-prefixed Model Dir** with its
  plain spelling (`realpath`), so a held partial counts twice. The app writes
  no such path.
- **`_unlink_partial` leaves a partial writable** when its retry is refused
  for another reason after clearing read-only. Harmless.
- **Three recorded properties of the `_unlink_partial` 10 ms retry**
  (measured, not changed): `removed_bytes` credits the size read before the
  first attempt; the 10 ms is paid serially per refused file (50 held
  partials: 0.53 s) on the queue worker, the preload worker or the script,
  never on Qt; two concurrent cleanups over one tree over-count
  `removed_files` (84 of 4,000), because Windows accepts a second delete of a
  file whose delete is in flight.
- **A minimised Run Benchmark window or pop-out returns with the settings
  dialog** (Windows restores `Qt.Window` owned windows with their owner; the
  owner relation keeps them above it).
- **A model leaving the inventory mid-run resets the plan to Pending at the
  next Run Benchmark click**: `_refresh_benchmark_plan_from_widgets` returns
  before recording the sequence while a run owns the plan. Results stay in
  Results and History.
- **`benchmark_history.json` holds literal `NaN`** for unknown numbers
  (`json.dumps`); not RFC 8259, but the file is the app's, not an export.
- **After a cancel the benchmark stdout drain stops at
  `_CANCEL_DRAIN_SECONDS`** if a grandchild keeps the pipe open. A child
  surviving every kill arm is logged (`benchmark_worker_survived_termination`)
  and left running.
- **Two history entries equal in every field are one entry to Edit and
  Delete**: `update_entry` / `delete_entries` use `list.index` on the
  `TranscriptHistoryEntry` dataclass. The app never writes such a pair (every
  recording has an id); fixing needs an id in the schema.
- **The readiness probe cannot see a browser renderer's delay**: for
  `Chrome_RenderWidgetHostHWND` the `WM_NULL` round trip answers for the UI
  thread, not the renderer. `CLIPBOARD_RESTORE_DELAY_S` (1.5 s) bounds it;
  `keep_transcript_in_clipboard` closes it. Not measured on a live browser.
- **The retry slot holds one failure**: a queued job Q failing during a retry
  of W replaces `_last_failed_wav_bytes`; a second Retry stops W's retry (W
  stays in the store, canceled) and transcribes Q. Holding both needs a
  failure queue.
- **Two Retry presses can write two history entries**: a remote provider runs
  the stopped first retry to completion and it is kept (a finished
  transcription is never discarded). Local engines cancel cooperatively.
- **Two recordings the store never received are told apart by bytes alone**:
  with `source_recording_id` "" on both, identical audio cross-retires the
  retry slot. Needs two refused writes and byte-identical audio.
- **A benchmark's device decision is per run, with no "forget" button**:
  `measured_fastest_devices` reads one run; fewer than two measured devices
  keeps the old entry. Remove an entry by re-running, pinning a device, or
  deleting `onnx_auto_preferred_devices` from `%APPDATA%\stt_app\settings.json`
  with the app closed. A stale entry costs speed, never correctness
  (2026-09-19).
- **A benchmark model row toggles only on its checkbox** (checkable items, no
  selection). Whole-row toggling must test the click against the indicator
  rectangle or it toggles twice. P4, ~20 min (2026-09-27).
