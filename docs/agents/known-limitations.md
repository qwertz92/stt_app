# Known limitations

Accepted, recorded defects and gaps, moved verbatim from `AGENTS.md` on
2026-09-30. Record a new one here, not in `docs/ROADMAP.md`.

- **A signal landing between "the resource is held" and "the flag says so"
  can strand it, and this is accepted rather than fixed.** Several places set
  a bookkeeping flag on the statement after the acquiring call returns --
  `incremented = True` in both branches of `_acquire_transcriber_runtime`,
  `acquired = True` in `_download_local_model_in_subprocess`, and the
  `try:` that follows `coordinator.acquire(...)` in
  `_download_model_for_preload` and `run_coordinated_download`. An exception
  delivered *between* those two statements leaves the resource held with the
  cleanup arm believing it was not. CPython only delivers signals to the main
  thread between bytecodes, and a windowed run has no SIGINT source at all, so
  the window is a `KeyboardInterrupt` in a console-launched process. It is
  recorded rather than closed because every plausible fix restructures locking
  on paths where restructuring has itself introduced defects three rounds
  running, and the same window reappears one statement further in whatever the
  new shape is. The one case that *was* closed is the cross-process download
  lock, because there the loser is every other process on the machine, not
  just this one.
- **`_teardown_pending_stream_connect` swallows a `KeyboardInterrupt`.** Its
  guard is `except BaseException` by design -- it is best-effort cleanup on an
  error path that still has a lease to release and an overlay to update -- so
  a Ctrl+C landing inside `thread.join()` or the provider call is logged and
  dropped. Only reachable in a console-launched process.

- **The download slot is not enforced across Windows user accounts.** The
  cross-process lock file lives under the calling user's `%APPDATA%`
  (`appdata_root() / "locks"`), so two accounts on one machine that point at
  the same Model Dir can download into it at the same time and corrupt each
  other's blob and ref trees. Sharing a Model Dir between accounts is not a
  configuration the app offers or documents; a lock beside the shared
  directory would need write access for every account and the docstring on
  `_download_lock_dir` says why it is not inside the cache. Recorded rather
  than fixed.
- **With a custom Model Dir, a faster-whisper copy that lives only in the
  default Hugging Face cache can no longer be deleted from the Models tab.**
  Delete is gated on the inventory, and the inventory now answers "loadable
  from the configured Model Dir" -- which that copy is not, because
  `WhisperModel(download_root=...)` reads one cache root. `cached_model_paths`
  and `delete_cached_model` still reach it, so nothing about the copy changed;
  only the row's Delete button is disabled. This is the price of the inventory
  no longer claiming such a model is installed and then silently
  re-downloading gigabytes of it, which is the worse failure of the two, and
  it affects nobody who leaves Model Dir empty -- then the destination *is* the
  default cache. Restoring the capability means the scan reporting per-model
  path information rather than names, which the subprocess protocol does not
  carry today.
- **Cancel reaches a download only while it is *waiting* for the slot, not
  while it is transferring.** `run_coordinated_download` passes `cancel_check`
  into `acquire()`, so with the slot free -- the ordinary single-user case --
  the check is polled zero times and the transfer runs to completion.
  `snapshot_download` exposes no cancel callback (its `tqdm_class` reports
  progress only), and the
  ModelScope fallback reads its response in a plain loop with no poll. The
  visible consequence: with `keep_onnx_model_loaded` off, a Cohere/Granite
  model's only download is the one its transcriber starts from its own load
  path, and pressing Cancel during a multi-gigabyte fetch does nothing while
  that job holds the single `max_workers=1` transcription worker. The Local
  tab's own queue is unaffected -- it downloads in a child process and Cancel
  kills it. Closing this properly means routing the transcriber's load-path
  download through that same worker process; a poll inside the mirror loop
  would only cover the fallback and would make Cancel look like it works.
- **Two input devices with the same name are one entry.** The microphone
  picker stores and resolves the device by *name*, so two identical USB
  microphones show as one row and `resolve_input_device` opens the first
  PortAudio index that carries the name -- every time, whichever the user
  meant. Keying the selection on an index instead would break the property
  the name buys: a selection that survives a re-enumeration and a reboot,
  where PortAudio's indices do not. Recorded, not fixed.
- Streaming: inserted text is append-only and never rewritten. A focus
  change suspends live insertion rather than aborting the session; the
  detection is best-effort polling, so a very brief switch can be missed.
- The post-pause append gate is an energy measurement, not a real VAD. A
  sustained non-speech sound after a long pause (a ~400 ms cough or chair
  scrape, or a heavy desk knock) can still authorise appending one
  hallucinated window, and a word whose longest voiced piece is under 80 ms
  is dropped. **The gate blocks silence, and nothing else.** Digital silence
  and room tone measure 0.000 s, which is the case that once grew a
  transcript to 896 invented words; a single key clack measures exactly the
  cut and typing above ~130 wpm reports seconds of "speech" because the decay
  tails bridge the gaps. "Bitte." (0.085 s) and a key clack (0.080 s) are 0.005 s
  apart, so no threshold separates them. Everything louder than
  silence passes and is bounded by `protected_prefix` rather than prevented.
- **The whole pause mechanism is inert in a room above the silence gate.**
  With a noise floor over `silence_gate_threshold` no slice is ever quiet,
  so `silent_seconds` never accumulates, `new_segment` never fires and
  `segment_floor` is never set — the protection is simply absent, and an
  unalignable window destroys the whole transcript again. A fan, an open
  window, or Windows microphone boost is enough. Nothing in the UI shows it,
  so the transcriber logs `streaming_noise_floor_above_gate` once per
  session after 20 s of unbroken above-gate audio. The condition is rolling,
  so a fan that starts mid-dictation is reported; a latching flag never
  reported it, because every session begins with a moment of silence. It
  cannot tell a loud room from 20 s of speech without a pause, which the
  message says.
- **Every number behind that gate is synthetic.** `samples/benchmark_sample.wav`
  is generated by `scripts/generate_sample_audio.py` and contains sine tones,
  not speech, so the repository has no recorded audio to calibrate against.
  A threshold was once derived from it and read back the generator's own
  parameters as if they were speech statistics. Separating a knock from a
  short word needs spectral features (a real VAD); until then, do not move
  the threshold on synthetic evidence alone.
- **A long remote batch recording is transcribed part by part, and a part
  knows nothing of the one before it.** Each request is independent: a
  sentence running across a cut is transcribed in two halves, automatic
  language detection runs per part, and no prompt carries the previous
  part's text (the custom vocabulary goes with every part). The cut sits at
  the quietest 20 ms frame of its search window, usually a pause. A cancel
  between parts discards the parts already transcribed -- the audio stays
  reachable through Import and the recovery prompt, as for any cancel, not
  through Retry, which holds failures only -- and a request in flight runs
  to its end, as a single request always has. Recorded.
- **An empty part fails the recording when it holds sound, and "sound" is
  a level, not speech.** `_audio_parts._holds_sound` compares a part's
  loudest 100 ms window with the silence gate's *default* threshold
  (0.004), not the one the user set. Two shapes in which a recording
  fails for a stretch that holds no words (found by the review of
  2026-09-27; not observed in the field, and whether a provider answers
  such a part with nothing rather than an invented word is unverified):
  the remainder after the last cut can be as short as 20 ms, so a
  `gpt-4o-transcribe` dictation of 180.4 s whose last 0.5 s holds only the
  hotkey's click fails on every Retry if the provider returns nothing for
  that tail; and a user who raised the threshold for a noisy room gets a
  failure for a stretch the gate would call silent. Fixing it is a choice
  between losing a short word silently and failing visibly: judge the part
  by its longest speech run (`vad.measure_longest_speech_run_s`, 20 ms
  buckets) against the configured threshold -- which needs the threshold
  passed through the factory to OpenAI, Groq and Azure and added to their
  runtime identity -- or keep the splitter from leaving a last part
  shorter than a few seconds, which Granite CTC's passes would share. P3,
  one to two hours with tests; waits for the owner's choice.
- **Splitting a long import holds about five times its file size in
  memory.** The shared WAV reader (`local_onnx_asr._read_wav_float32`)
  keeps the raw bytes and two float32 copies of every channel: measured
  576 MB for a 115 MB 48 kHz stereo file and 1,325 MB for 265 MB, so a
  two-hour 48 kHz stereo import sent to Azure would need about 7 GB. Only a
  WAV past its engine's limit is decoded; a `MemoryError` sends the file
  whole, where the provider refuses it. Decoding block by block into one
  mono array would bring it to about one file size for stereo (P3, about an
  hour with tests, and the two local runtimes that share the reader gain
  the same). Recorded 2026-09-27.
- **An Azure part of up to an hour is held to a 120 s socket timeout.** The
  factory passes no `request_timeout_s`, so the constructor's default of
  120 s applies to every socket read, and a service that stays silent for
  longer while it transcribes an hour of audio fails the part; the same
  held before the split for a whole recording of up to two hours. Not
  observed (no Azure resource here). Recorded 2026-09-27.
- **A WAV that is not 16 kHz is resampled by linear interpolation** in the
  Nemotron and Granite CTC runtimes (`_pcm_audio.resample_linear`), which has
  no anti-aliasing filter: content above 8 kHz in a 44.1 or 48 kHz file folds
  back into the band the model hears. The app's own recordings are 16 kHz, so
  only an imported file or a benchmark sample reaches it; a proper resampler
  means a low-pass filter before the decimation, which neither runtime has.
  It also holds float64 position arrays for the input and the output:
  measured on 2026-09-19, ten minutes of 48 kHz audio cost 614 MB beyond
  the 115 MB input (269 MB for 8 kHz), i.e. about 3.7 GB per hour of a
  48 kHz import. Recorded.
- ARM CPUs: not supported (CTranslate2 requires x86 AVX/SSE).
- **Clipboard restore is not lossless.** Every HGLOBAL format is captured
  and put back (F12 of the 2026-09-12 review); what still is not: the
  GDI-handle and owner-drawn formats (`CF_BITMAP` -- no practical loss,
  Windows synthesizes it again from a restored `CF_DIB` --
  `CF_METAFILEPICT`, `CF_PALETTE`, `CF_ENHMETAFILE`, `CF_OWNERDISPLAY`,
  `CF_DSPBITMAP`, `CF_DSPMETAFILEPICT`, `CF_DSPENHMETAFILE`, so a
  clipboard carrying only a metafile comes back without it); the private
  ranges `CF_PRIVATEFIRST..CF_PRIVATELAST` and
  `CF_GDIOBJFIRST..CF_GDIOBJLAST`, which the copying application frees
  itself; the OLE bookkeeping formats `DataObject` and `Ole Private
  Data`, so an application that reads the clipboard only through
  `IDataObject` may see less after a dictation than before (the formats
  such an object advertises are normally on the clipboard as bytes
  beside it, and those are restored); a delayed-rendered format whose
  owner has not rendered it (a NULL handle is skipped -- the restore
  writes bytes, never a promise); a clipboard over the size caps, which
  falls back to text only; a single format whose block cannot be read;
  and the clipboard's owner window, which becomes this app's as before.
  `CF_DSPTEXT` is restored without the `CF_OWNERDISPLAY` that gives it
  meaning (harmless: ordinary bytes).
- The NVIDIA *NeMo* runtime remains intentionally unimplemented. Parakeet itself
  ships through the pure-Python onnx-asr path and Nemotron through ONNX Runtime
  GenAI, so no NeMo/PyTorch stack is needed. See
  `docs/local-asr-model-candidates-2026.md` for rationale.
- **One insert offer at a time, and a later failure replaces an earlier
  one.** A deferred flush pastes several queued transcripts; when an
  earlier one fails before its keystroke (Insert useful) and a later one
  fails after it (Insert withheld), the offer ends up holding the later
  text and the earlier one is only in history and the tray notification
  (the wave-6 concurrency lens's flush matrix). Keeping the insertable
  one instead would hide the other's "possibly inserted" report;
  recorded rather than changed. The tray's re-paste of the whole
  dictation failing *before* its keystroke is the same shape from the
  other side: it replaces a streaming tail's offer with the whole
  dictation, whose Insert then pastes the prefix the live stream already
  put in the document a second time (the wave-7 reach lens).
- **`close_if_idle` is bounded only against another thread's hand-over,
  not against its own closes.** Its generation bump refuses a reopen
  carrying a generation captured before it; a `request_restart` issued
  after the bump reopens with the bumped generation, the call retires and
  closes that stream itself, and each own close re-arms the budget
  (forced schedule: 25 restarts 0.1 s apart against a 0.4 s budget
  answered True after 3.14 s). The producers -- a microphone change saved
  in Settings, a system resume -- serialize on
  `_audio_device_refresh_lock`, so a restart costs one extra open and
  close on the refresh worker, off Qt. Recorded rather than closed: the
  bound would have to refuse a restart's reopen for the length of the
  call, and every reshaping of that lock has cost a round.
- **The `Thread.start` guards catch `RuntimeError` only.** The two
  guards of `fd1b50a`/`c98f57e` (and the settings dialog's six) name the
  exception a starved interpreter raises; `_thread.start_new_thread` can
  raise `MemoryError` as well, and that escapes exactly as before the
  guards (the wave-7 boundaries lens). A process that cannot allocate a
  thread stack is past what a guard can recover; recorded.
- **`WarmMicrophoneStream.close()` does not wait for a helper's close in
  flight.** It drains `_retiring` and returns, so a stream a helper is
  still closing stays registered for the length of that close (measured:
  `close()` returned after 0.000 s with one live stream, which the helper
  then closed). Its only production caller is `shutdown()`, and the
  helper finishes on its own; `close_if_idle` is the call that waits.
- **Copy yields the pending offer after an edit made while one is
  pending**: the overlay's Edit is enabled for Done only, so the offer
  has to appear while the edit dialog is open; the confirmation then
  goes through the offer painter, Copy yields the un-inserted text
  rather than the edited one, and Edit is disabled until the offer is
  retired. The offer's semantics; recorded.
- **The benchmark worker's 6 s environment query can be outlived by a
  grandchild.** `subprocess.run(timeout=6)` kills the PowerShell it
  started, but a process that PowerShell spawned and that still holds the
  stdout pipe keeps the read open past the budget. `Get-CimInstance` does
  not spawn one, so nothing at HEAD reaches it; recorded (wave 8).
- **Two benchmark runs saved within one second share an identity.**
  `BenchmarkHistoryEntry.identity_key()` is `(created_at, status, summary)`
  and `created_at` has second resolution, so two runs finished in the same
  second with the same status and summary open one pop-out. A benchmark
  run takes longer than a second; recorded.
- **The benchmark environment is collected before the first cancel check**
  (1.8-3.9 s of PowerShell over eleven runs in three sessions on this
  machine, median 2.2 s), so a cancel pressed at once is honoured only
  after it, and a shutdown joins the worker for up to 2.5 s -- shorter
  than the query in about half of those runs; a worker the join outlasts
  saves nothing, one it ends is saved as canceled. Recorded.
- **Two spellings of one incomplete-file directory are not folded.**
  `_model_cache_dirs` dedupes by `realpath`, which does not fold the
  `\\?\` prefix, so a Model Dir spelled that way beside its plain spelling
  lists the directory twice and counts a held partial as two. Nothing in
  the app writes such a path; recorded.
- **A read-only partial the cleanup then cannot remove has lost its
  attribute.** `_unlink_partial` clears the bit before its retry, and a
  retry refused for another reason leaves the file writable. The next
  cleanup or resume treats it like any other partial; recorded.
- **A Run Benchmark window or pop-out the user minimised comes back with
  the settings dialog.** They are `Qt.Window` children owned by the dialog;
  Windows minimises them with their owner and restores them with it, so a
  pop-out minimised on its own, followed by a minimise and restore of the
  dialog, is back on screen (measured through the window manager). The
  owner relation is what keeps them above the dialog; recorded.
- **A model that leaves the inventory during a run resets the plan at the
  next Run Benchmark click.** `_refresh_benchmark_plan_from_widgets` returns
  before it records the sequence while a run owns the plan, so a scan that
  removed a selected model mid-run leaves the stored sequence and the
  widgets apart; the run finishes with its Done/Skipped states intact, and
  the first reopen afterwards redraws the new, shorter plan to Pending with
  nobody having changed a selection. The plan describes what the current
  selection would run, and the run's own results are in the Results table
  and in History; recorded.
- **`benchmark_history.json` holds `NaN` where a number is unknown.** The
  reader answers NaN for a null, a string or a boolean in a numeric field
  and `json.dumps` writes that back as the literal `NaN`, which RFC 8259
  does not allow; Python's own parser reads it, another tool's may not.
  The file is the app's, not an export; recorded.
- **The benchmark worker's stdout is read to its end after a cancel, within
  `_CANCEL_DRAIN_SECONDS`.** A grandchild that inherited the pipe and
  outlives the kill of the process tree keeps it open, and the drain then
  returns on its bound with whatever had arrived. `taskkill /T` reaches
  the tree the worker spawned; recorded. A child that survives every arm
  of the kill itself is logged (`benchmark_worker_survived_termination`)
  and left running; recorded.
- **Two history entries equal in every field are one entry to Edit and
  Delete.** `TranscriptHistoryEntry` is a dataclass compared by value, and
  `update_entry` / `delete_entries` find their target with `list.index`,
  the first equal row -- so an edit of the second of two rows sharing the
  same second, text, engine, model, mode and empty recording id and audio
  path lands on the first (the wave-11 boundaries lens, with a probe on
  the real store). The two rows are indistinguishable to the user as well,
  so what shows is one of two identical rows changed rather than the
  other; the app never writes such a pair itself, since every recording
  carries its own id, and telling them apart would need an id in the
  schema. Recorded.
- **The readiness probe cannot see a browser renderer's delay.** For a
  Chromium or Electron target the focused window is a
  `Chrome_RenderWidgetHostHWND`, pumped by the browser process's UI thread,
  and the deferred restore's `WM_NULL` round trip answers for that thread
  -- not for the renderer whose paste handler asks for the clipboard
  later. A renderer that reads more than `CLIPBOARD_RESTORE_DELAY_S` after
  the keystroke while the UI thread already answers can still find the
  restored content (the wave-11 reach lens, with a fake backend that
  separates the two; not measured against a live browser). The delay
  bounds that window at 1.5 s where the predecessor had 160 ms, and
  `keep_transcript_in_clipboard` closes it. Recorded.
- **The retry slot holds one failure, and a background failure landing
  during a retry replaces it.** `_last_failed_wav_bytes` is the most recent
  failure with audio: a queued job Q failing while a retry of W is in
  flight promotes Q's bytes over W's, and a second Retry press then stops
  the retry of W -- its recording stays in the store, marked canceled, so
  the recovery prompt and Import still reach it -- and transcribes Q under
  Q's id (the wave-15 concurrency lens, on the real store and executor).
  W's retry succeeding leaves Q retryable since wave 15; holding both
  would need a queue of failures where the tray's "Retry transcription"
  names one. Recorded.
- **Two Retry presses on one failure can write two history entries.** The
  second press stops the first retry, which a remote provider runs to
  completion regardless; its transcript is then kept in history as every
  finished transcription is, and the second retry's beside it, both with
  the recording's id (the wave-15 concurrency lens). The rule that a
  finished transcription is never discarded is the one this keeps; a
  local engine's cooperative cancel ends the first retry instead.
  Recorded.
- **Two recordings the store never received are told apart by their bytes
  alone.** A job whose `source_recording_id` is "" -- its persist was
  refused -- carries no identity, so the retire of the retry slot compares
  "" with "" and falls back to the bytes: two such recordings with
  identical audio cross-retire, as every pair did before wave 16. It needs
  two refused store writes in one session and byte-identical recordings;
  recorded.
- **A benchmark's device decision is per run, and there is no button to
  forget one.** `measured_fastest_devices` reads one finished run; runs are
  never combined. A run that could not measure the stored device (it errored
  that time) decides among the devices it did measure and replaces the entry,
  and a run that measured fewer than two devices for a model leaves that
  model's entry as it was -- so a machine whose GPU targets all fail never
  stores anything and `auto` keeps its default order there. An entry is
  removed by running the comparison again, by pinning a device (which always
  wins), or by deleting `onnx_auto_preferred_devices` from
  `%APPDATA%\stt_app\settings.json` with the app closed. Windows' driver and
  the app's runtimes change between a measurement and the day it is used; a
  stale entry costs speed, never correctness, because the rest of the chain
  is still tried after the preferred device. Recorded (2026-09-19).
- **A benchmark model row toggles only on its checkbox.** The Run
  Benchmark window's model list holds checkable items and takes no
  selection, so a click on a model's name does nothing -- Qt's default for
  a checkable item -- while the checkbox and Space on the current row
  toggle it. Making the whole row the target needs a test of the click
  position against the style's indicator rectangle, because a click on
  the indicator already toggles and would toggle twice (P4, about 20
  minutes with a test). Recorded 2026-09-27.
