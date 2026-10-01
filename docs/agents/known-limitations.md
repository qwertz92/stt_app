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
- **The post-pause append gate is energy plus a speech check that admits
  most knocks.** With the silence gate on, Silero refuses most thumps and
  fast typing after a pause, but a knuckle knock still passes 47 times in 50
  (0.064-0.227 against the 0.08 cut) and can append one hallucinated window;
  the damage stays bounded by `protected_prefix`. With the silence gate off
  the speech check does not run and the gate is energy alone, as before. A
  word under 80 ms voiced is still dropped by the energy run.
- **The batch speech check lets much noise through, by design.** With its
  amplified second scan, the SYNTHETIC calibration skips a knock 1 time in
  50, typing at 160 wpm never, a fan rarely, a thump 32 times and room tone
  just over the gate about 40 times in 50; the second scan is the price of
  never skipping quiet speech. A speechless recording longer than
  `SILERO_BATCH_MAX_SCAN_S` (30 s) is never skipped (incomplete scan).
- **In a noisy room the batch speech check can skip one short quiet word**
  (P3, accepted by the owner 2026-10-01). With fan-like noise at 0 dB SNR, a
  single word of 140 ms near the lowest settable threshold (0.0005) scored
  0.072 as recorded and 0.135 amplified -- under the 0.15 cut -- and was
  skipped in 1 of 50 seeds (review of 4298c8a). The calibration holds no
  noisy-room speech, so the cut was never set against it. Lowering the cut
  lets more noise through; a noisy-room speech set is what would settle it.
- **The batch speech check is off for a stop that comes before its model
  loaded**: the stop is transcribed as before (`silero_speech_seconds=loading`)
  rather than waiting on the Qt thread. Only the first second or so after
  start, or after turning the gate on, is affected.
- **The pause mechanism is inert in a room above the silence gate**: noise
  over `silence_gate_threshold` means `silent_seconds` never accumulates,
  `new_segment` never fires, `segment_floor` is never set. Logged once per
  session as `streaming_noise_floor_above_gate` after 20 s of above-gate audio
  (rolling; cannot tell a loud room from 20 s of pause-free speech).
- **The energy gate's numbers are synthetic; the speech check's noise side
  is too**: `samples/benchmark_sample.wav` (from
  `scripts/generate_sample_audio.py`) is sine tones, so do not move the
  energy threshold on synthetic evidence. The Silero cuts were calibrated on
  real speech (six LibriSpeech excerpts in `tests/data`, 25 clips and the
  owner's recordings, aggregates only) but on SYNTHETIC non-speech only: no
  recorded cough, fan, room or keyboard was measured.
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
  and the tray, and since 2026-10-01 stays listed as a queue-panel row that
  the re-paste inserts. A tray re-paste of the whole dictation failing before
  its keystroke replaces a streaming tail's offer, whose Insert then re-pastes
  the already-streamed prefix.
- **The paste pace is target-agnostic**: a queued paste into another window
  also waits up to `CLIPBOARD_RESTORE_DELAY_S` after the previous keystroke
  (one clipboard). Only the last SendInput keystroke is tracked.
- **Paced pastes and waiting-insert rows end with the app**: shutdown drops
  them (the texts stay in history), and a streaming finalize tail whose
  insert fails gets an Insert offer but no row. A foreground result without
  a job is not paced.
- **The 1418 race is survived, not closed**: pywin32 opens the clipboard with
  a NULL owner, so a clipboard manager can still close it under us; three
  reopens cost up to about 0.33 s on the Qt thread per clipboard operation,
  and one paste runs up to four (capture, write, read-back, changed-after-set
  read), so about 1.3 s before it reports contention.
- **A close lost between the text write and the Win+V exclusion formats
  publishes the transcript without them**: a clipboard manager that closes
  our open right after `SetClipboardText` sees the clipboard with the text
  alone, and Windows may list it in Win+V history; the exclusion sets that
  follow fail and log `clipboard_history_exclusion_partial`. Not closed: the
  formats cannot be set before the text (`EmptyClipboard` would drop them),
  and the race is the 1418 one above. Separately and on purpose, the
  `copy_on_error` fallback (`QGuiApplication.clipboard().setText`) leaves a
  failed paste's transcript on the clipboard as an ordinary copy, so it is
  in Win+V: the user is meant to paste it by hand.
- **The streaming finalize tail is not paced** (from code reading,
  2026-10-01, not reproduced in a test): it pastes at once in
  `_on_transcription_ready`. Trigger: a batch result queued before a switch
  to streaming finishes during the streaming recording, waits for it, and
  reaches the finalize's flush. Inside the last live insert's restore window
  it is held by the pace and pasted after the tail (token order inverted);
  outside it, it pastes and the tail follows inside its restore window.
  Holding the tail would route the append-only finalize through the paste
  queue, a larger change; the inverted order keeps the streamed dictation in
  one piece.
- **A transcript left on the clipboard after an abandoned restore**
  (`abandoned_busy`) is not in Win+V history, though it is on the clipboard;
  restoring the user's own content may add their copy to Win+V again, as
  before.
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
- **A key command whose wrapper does not wait for its tool can lose the
  token** (P3, 2026-10-01, review of dedcde4). `run_bounded` ends the job
  0.5 s after the direct child exits while a descendant still holds the
  pipes, so a `.cmd` that runs `start /b` (or PowerShell `Start-Process`
  without `-Wait`) and exits before its tool prints gets "printed no token".
  Telling a late token from a forgotten grandchild needs reader threads that
  see partial output instead of `communicate`. Workaround: make the wrapper
  wait (`start /wait`, `-Wait`). When the job cannot be assigned (a nested
  job that forbids it), the taskkill fallback cannot reach an orphan whose
  parent exited, so such a run times out even though the token arrived.
- **A resumed restore can run two timer chains for one record** (P4,
  2026-10-01, review of a404479). When the deferred timer sits in its
  readiness probe (outside the lock) while a new paste takes the record over,
  fails without touching the clipboard and resumes it, both the old run and
  the new timer act on the record. It is never restored twice (the first
  success clears it), but the retry budget runs out about a second early. A
  per-record chain generation checked under the lock would close it.
