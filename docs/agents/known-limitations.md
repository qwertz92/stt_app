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
  `run_coordinated_download`. Only a `KeyboardInterrupt` hits it, and the GUI app
  cannot raise one (its download script `scripts/download_model.py` and a
  download worker child may): `main._install_signal_handlers` replaces Python's SIGINT
  handler with one that only calls `app.quit()`, and nothing else raises
  asynchronously (checked 2026-10-03). Kept as depth: every fix reshapes
  locking and moves the window (~3 h, no gain). Only the cross-process
  download lock was closed (its loser is every other process).
- **`_teardown_pending_stream_connect` swallows a `KeyboardInterrupt`**
  (`except BaseException` by design, best-effort cleanup). Unreachable for the
  reason above. Kept 2026-10-03: the swallow is what keeps the arms'
  `_reset_streaming_state` reachable; re-raising after each arm finishes is
  ~2 h over three call sites for no user-visible gain.
- **The download slot is not enforced across Windows user accounts**: the lock
  lives under each user's `%APPDATA%` (`appdata_root() / "locks"`); two
  accounts sharing one Model Dir can corrupt it. Not an offered configuration;
  `_download_lock_dir`'s docstring says why the lock is not in the cache.
  Kept (2026-10-03, owner decision needed): a machine-wide lock location
  (`%PROGRAMDATA%`) needs a permissions design for a configuration nobody
  uses; about 2 h plus the decision.
- **With a custom Model Dir, a faster-whisper copy only in the default HF
  cache cannot be deleted from the Models tab**: the inventory answers
  "loadable from Model Dir" (`WhisperModel(download_root=...)` reads one
  root). `cached_model_paths` / `delete_cached_model` still reach it. Fix needs
  per-model paths in the scan subprocess protocol. Kept (2026-10-03): the
  scan side is about an hour, but the Models tab needs a row state "in the
  default cache, not used" (`settings_dialog_local.py`, another owner's file),
  about 3 h with its layout tests.
- **The post-pause append gate is energy plus a speech check that admits
  most knocks.** With the silence gate on, Silero refuses most thumps and
  fast typing after a pause, but a knuckle knock still passes 47 times in 50
  (0.064-0.227 against the 0.08 cut) and can append one hallucinated window;
  the damage stays bounded by `protected_prefix`. With the silence gate off
  the speech check does not run and the gate is energy alone, as before. A
  word under 80 ms voiced is still dropped by the energy run. Kept
  (2026-10-03): needs recorded knocks and typing, which only the owner's
  microphone can supply; half a day to measure and recalibrate the cut.
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
  start, or after turning the gate on, is affected. Kept (2026-10-03, by
  design): a stop inside that window needs a recording shorter than the
  graph's load (0.1-0.3 s); waiting for it would freeze the Qt thread.
- **The pause mechanism is inert in a room above the silence gate**: noise
  over `silence_gate_threshold` means `silent_seconds` never accumulates,
  `new_segment` never fires, `segment_floor` is never set. Logged once per
  session as `streaming_noise_floor_above_gate` after 20 s of above-gate audio
  (rolling; cannot tell a loud room from 20 s of pause-free speech). Kept
  (2026-10-03): a pause threshold relative to the measured noise floor needs
  noisy-room recordings to calibrate; about a day with the owner's samples.
- **The energy gate's numbers are synthetic; the speech check's noise side
  is too**: `samples/benchmark_sample.wav` (from
  `scripts/generate_sample_audio.py`) is sine tones, so do not move the
  energy threshold on synthetic evidence. The Silero cuts were calibrated on
  real speech (six LibriSpeech excerpts in `tests/data`, 25 clips and the
  owner's recordings, aggregates only) but on SYNTHETIC non-speech only: no
  recorded cough, fan, room or keyboard was measured. Kept (2026-10-03,
  needs the owner's recordings): nothing here can be fixed without them.
- **Remote batch parts are independent**: a sentence across a cut is split,
  language detection runs per part, no previous-part prompt (vocabulary goes
  with every part). A cancel between parts discards finished parts (audio
  stays reachable via Import/recovery, not Retry); a request in flight runs on.
  By design: a previous-part prompt differs per provider and is not offered
  by all of them; the cut sits at the quietest point to keep splits rare.
- **A gap marker can still stand for a stretch without words.** An empty
  part is judged by its loudest 100 ms window against the user's silence-gate
  threshold, then by the Silero check; noise the batch speech check lets
  through (see above) -- room tone, a thump -- leaves
  `[no text returned for ...]` if the provider answers the part with nothing.
  Deliberate: the error direction is a marker to delete, not lost speech
  (docs/agents/remote-providers.md).
- **Non-16 kHz WAV is resampled linearly** (`_pcm_audio.resample_linear`,
  Nemotron and Granite CTC): no anti-aliasing (2026-09-19). Not added on
  2026-10-03: a low-pass filter changes the samples every imported file feeds
  the models, with no word-error-rate measurement behind it. App recordings are
  16 kHz; only imports and benchmark samples reach it. Kept (2026-10-03,
  owner decision needed): the filter is an hour of work, the word-error-rate
  comparison on resampled imports that would justify it about half a day.
- **Windows on ARM64 runs the app, but not the faster-whisper models.**
  CTranslate2 has no `win_arm64` wheel in any release (and no sdist for 4.8.2),
  so the seven Whisper models (`tiny` to `distil-large-v3.5`) cannot run in a
  native ARM64 Python; the picker marks them `[unavailable]`, the Models tab
  skips them in every download, and a transcription refuses with the reason
  before fetching anything (`local_runtime_support.py`). Everything else has an
  ARM64 build: Parakeet, Canary, Granite CTC, Nemotron, Cohere/Granite via
  Node, the Silero speech check and every cloud engine. The earlier wording
  "CTranslate2 requires x86 AVX/SSE" was wrong: its x86-64 wheels need SSE4.1
  and pick AVX/AVX2/AVX512 at run time. The x64 installer already installs on
  Windows 11 ARM64 (Inno Setup `x64compatible`) and runs the whole app,
  Whisper included, under Prism emulation. Nothing here has run on ARM64
  hardware (2026-10-09, none was available); see `docs/learning-log.md`
  (2026-10-09) for what was checked and what stays unverified. No native
  ARM64 installer is built.
- **Clipboard restore is not lossless.** Every HGLOBAL format is restored, but
  not: GDI-handle/owner-drawn formats (`CF_BITMAP` is resynthesized from
  `CF_DIB`; `CF_METAFILEPICT`, `CF_PALETTE`, `CF_ENHMETAFILE`,
  `CF_OWNERDISPLAY`, `CF_DSPBITMAP`, `CF_DSPMETAFILEPICT`, `CF_DSPENHMETAFILE`
  are lost); `CF_PRIVATEFIRST..CF_PRIVATELAST`, `CF_GDIOBJFIRST..CF_GDIOBJLAST`;
  `DataObject` / `Ole Private Data`; unrendered delayed formats; clipboards
  over the size caps (text only); unreadable blocks; the owner window.
  `CF_DSPTEXT` comes back without its `CF_OWNERDISPLAY` (harmless).
  Kept 2026-10-03 (by design, cost vs effect): a GDI handle is not bytes;
  duplicating the metafile and palette handles (`CopyEnhMetaFile` and kin)
  would help vector copies from Office or Visio only, ~4 h, measurable only
  on the owner's own clipboard.
- The NVIDIA *NeMo* runtime is intentionally unimplemented (Parakeet via
  onnx-asr, Nemotron via ORT GenAI); see
  `docs/local-asr-model-candidates-2026.md`. By design.
- **One insert offer at a time; a later failure replaces an earlier one.** In
  a flush, an earlier pre-keystroke failure (Insert useful) is replaced by a
  later post-keystroke one (Insert withheld); the earlier text is in history
  and the tray, and since 2026-10-01 stays listed as a queue-panel row that
  the re-paste inserts. A tray re-paste of the whole dictation failing before
  its keystroke replaces a streaming tail's offer, whose Insert then re-pastes
  the already-streamed prefix.
  Kept 2026-10-03 (by design): the rows keep the earlier text listed and the
  re-paste inserts it; and the failed whole-dictation re-paste is the paste
  the user asked for, so its Insert retries exactly that. Keeping the tail's
  Insert instead is ~1 h if the owner prefers it.
- **A paced Insert of a row-less offer keeps the text it was pressed for**
  (2026-10-09, P4, from code): an Insert pressed inside the previous
  paste's restore window is held as `_PendingRepaste`, and
  `_run_pending_repaste` rebuilds the text from rows when it runs -- F10's
  waiting rows and an Insert built from rows -- but not for an offer
  without rows (a streaming tail, a failed re-paste of a text that has no
  row). An edit saved within those up to 1.5 s moves such an offer, but
  the held Insert still pastes the old text. Kept: the held Insert runs at
  most 1.5 s after the press, so an edit would have to be saved within
  that time; a fix (remember that the request was the offer's and read the
  offer again when it runs) is ~30 min with its tests.
- **The paste target check sees only Chromium windows and Win32 carets**
  (2026-10-03). A paste into a non-text element of any other application
  -- a Win32 button, a Qt or Java window, a terminal that draws its own
  cursor -- is "unknown" and still reported as inserted: "no caret" there
  does not mean "no text field" (Windows Terminal answers exactly that while
  its prompt takes the paste). Inside Chromium a page that edits through
  EditContext without reporting selection bounds may read as "not a text
  field" (two of four runs of a bare EditContext div did, 2026-10-03), so
  its pastes are reported as doubtful; real editors report bounds (the
  bounds variant read as a text field every time). VS Code and the new
  Notepad were not measured (VS Code refused a second instance while an
  update was pending; the new Notepad would have opened a tab in the
  owner's own window); the new Notepad is not a Chromium window, so it
  cannot be reported as doubtful. Streaming live inserts and the finalize
  tail are not checked. An Electron app's prompt may read as "not a text
  field": a review's read-only probe got that verdict 20 of 20 times on a
  foreground `Chrome_WidgetWin_1` window, most likely the Claude desktop
  app, focus location unknown; its pastes would then be reported as
  doubtful. Unmeasured until the owner runs `scripts/diagnose_paste_target.py`
  in the apps he dictates into. Kept 2026-10-03 (owner measurement needed):
  widening the check beyond Chromium needs per-application evidence (UI
  Automation was rejected, Windows Terminal answers wrongly); ~2-3 h per
  application family once the data exists.
- **A true-miss "not in a text field" row ends with the next paste**
  (2026-10-03, deliberate). Any later successful paste -- into any window
  -- drops it (`_drop_superseded_doubtful_rows`), so a paste that really
  missed is no longer listed or reachable by F10 once the user dictated
  elsewhere. Accepted because the report was shown when it was made
  (overlay with Insert, or the tray) and the text stays in history.
  Rejected alternative: dropping only on a later paste into the same
  window. An always-doubtful window (an Electron prompt may be one) would
  still collect a row per dictation whenever the user also works in a
  second window, and F10 would be left choosing among stale rows whose
  verdicts may all be false. Kept 2026-10-03 (the owner's rule).
- **A hung paste target holds the check's one worker**: the MSAA call is a
  cross-process `WM_GETOBJECT`; while it waits, later pastes are not
  checked (refused at once, "unknown") and the completion tone of the
  timed-out one plays after `PASTE_TARGET_CHECK_TIMEOUT_MS` (1 s). Kept
  2026-10-03 (cost vs effect): a second worker would block on the same hung
  target, and the refusal ("unknown", behaves as before the check existed) is
  the designed fallback; ~2 h for a capped second worker, effect nil.
- **The paste pace is target-agnostic**: a queued paste into another window
  also waits up to `CLIPBOARD_RESTORE_DELAY_S` after the previous keystroke
  (one clipboard). Only the last SendInput keystroke is tracked. Kept
  2026-10-03 (by design): a late reader of the previous paste reads whatever
  the one clipboard holds then, whichever window the next paste is aimed at.
- **Not-inserted rows are not carried into the next session** (2026-10-09):
  the tray's Quit now asks first and can wait for held pastes
  (`quit_dialog.py`), and it lists the rows as "in History", but a row
  itself is gone after the quit; only its text in history remains. A quit
  that skips the window (SIGINT/SIGTERM, Windows ending the session -- the
  latter not checked) drops held pastes as before; unfinished recordings
  are still kept by `shutdown`. A streaming finalize tail whose insert
  fails gets an Insert offer but no row, so the overlay's not-inserted
  badge does not count it either, and the next recording retires that
  offer (2026-10-09). Kept: a row store is ~4 h and the
  owner chose the quit window (2026-10-09). The tail row is not just a
  missing call: rows keep stripped text and a tail's leading space is what
  separates it from the streamed words, and F10 would have to order a tail
  row against failed batch rows (~4 h once decided).
- **Rare gaps of the quit window** (2026-10-09, review of 3ee1e23, P3/P4):
  a finished result held for its paste whose history write was refused (an
  unreadable history) exists only in memory, so Quit now drops it although
  the window says every finished transcript is in History; a recording the
  quit fails to write to the unfinished store (disk full, locked folder) is
  only logged (`unfinished_recordings_kept ... failed=N`), because nothing
  is left on screen to report it; the failure count behind "N
  transcriptions failed" is the Retry slot plus at most two older failures,
  so a failure that pushes out the oldest is not named in the wait's last
  message (the tray still reports it, the audio is kept); the quit after
  launching an update installer (`update_ui.py`) skips the window. A
  transcript with a gap marker leaves its file in the unfinished folder,
  linked from its history entry, and nothing lists or cleans those files;
  the same holds for a transcribed unfinished recording whose move to the
  recordings folder failed, and the `unfinished_*.wav` files "Keep last
  recording after successful transcription" moves to the recordings folder
  are never pruned (2026-10-09; the archive's count deletes only
  `recording_*.wav`, and these are a few files per quit with pending work).
  Kept: each needs a store or history failure, or is cosmetic.
- **A crash keeps only the newest unfinished recording** (2026-10-09): the
  quit writes every queued recording to the unfinished store, but a crash
  or a killed process runs no `shutdown`, so only the managed last
  recording survives, as before. A transcription the Import tab or the
  startup notice runs when the app quits is not tracked as a job: the quit
  window does not count it, and its file stays where it was (offered again
  at the next start for the notice). Writing every recording to the store
  at stop time would close the crash case; ~3 h, not asked for.
- **The 1418 race is survived, not closed**: pywin32 opens the clipboard with
  a NULL owner, so a clipboard manager can still close it under us; three
  reopens cost up to about 0.33 s on the Qt thread per clipboard operation,
  and one paste runs up to four (capture, write, read-back, changed-after-set
  read), so about 1.3 s before it reports contention. Kept 2026-10-03 (by
  design): an owner window would close it but blocks every other program's
  `EmptyClipboard` without a message pump (rejected in
  `docs/agents/text-insertion.md`).
- **A close lost between the text write and the Win+V exclusion formats
  publishes the transcript without them**: a clipboard manager that closes
  our open right after `SetClipboardText` sees the clipboard with the text
  alone, and Windows may list it in Win+V history; the exclusion sets that
  follow fail and log `clipboard_history_exclusion_partial`. Not closed, and
  the race is the 1418 one above. Kept 2026-10-03 (cost vs effect): setting
  the formats between `EmptyClipboard` and the text is possible, but a close
  lost after them then leaves our own formats on the clipboard, which
  `_refuse_if_written_since_our_empty` would read as a foreign write and
  refuse the retried paste -- trading a cosmetic Win+V entry for a failed
  paste; teaching that check our own formats is ~3 h. Separately and on purpose, the
  `copy_on_error` fallback (`QGuiApplication.clipboard().setText`) leaves a
  failed paste's transcript on the clipboard as an ordinary copy, so it is
  in Win+V: the user is meant to paste it by hand.
- **A streaming tail behind live text is not paced** (2026-10-09; the order
  part of the old entry is resolved, `docs/agents/text-insertion.md`). A
  finalize whose dictation already inserted live text pastes its tail at
  once, even inside the restore window of a paste the finalize's own flush
  just made for another window's queued result, or of a re-paste the user
  sent into another window during the stream (2026-10-09; the stream's live
  inserts wait for that one, the tail does not); a late reader of that paste
  can then read the tail. For the stream's own window no earlier result can
  be waiting at that point (it would have held the first live insert), so
  order is not affected. Kept (cost vs effect): pacing the tail means
  holding the rest of a streamed dictation behind another window's paste,
  through the paste queue's coalescing, for a race only a renderer that
  reads the clipboard late loses; about 2 h.
- **A streaming dictation shows no live text while an earlier result for
  its window is still transcribing** (2026-10-09, by design: the owner's
  order rule). A slow remote batch job keeps the stream's words off the
  window until it is pasted, fails or is stopped; the overlay still shows
  them live and nothing is lost. When that result is pasted during the
  stream, its completion tone (if enabled) plays into the stream's
  microphone, as for an immediate-mode paste during a batch recording.
- **A transcript left on the clipboard after an abandoned restore**
  (`abandoned_busy`) is not in Win+V history, though it is on the clipboard;
  restoring the user's own content may add their copy to Win+V again, as
  before. Kept 2026-10-03: writing it again without the exclusion formats
  empties the clipboard first, so the late read by the busy target that the
  abandon protects could see an empty clipboard; ~1 h, and it raises the
  risk the abandon exists to avoid.
- **Two benchmark runs saved within one second share
  `BenchmarkHistoryEntry.identity_key()`** (`created_at` has second
  resolution) and open one pop-out. Only with equal status and summary too,
  and every Settings run spends the environment query (median 2.2 s) before
  it can save, so two saves within a second do not come from the app.
  Since 2026-10-03 the History selection also skips reloading an equal key.
- **The benchmark environment (median 2.2 s PowerShell) runs before the first
  cancel check**; a shutdown joins for 2.5 s: a worker it outlasts saves
  nothing, one it ends is saved as canceled.
- **Two recorded properties of the partial-removal retry** (measured, not
  changed; the per-file pause and the writable leftover were fixed 2026-10-03):
  `removed_bytes` credits the size read before the first attempt; two
  concurrent cleanups over one tree over-count `removed_files` (84 of 4,000),
  because Windows accepts a second delete of a file whose delete is in flight.
  Both only skew a count in a message; the second needs two cleanups at once.
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
  recording has an id); fixing needs an id in the schema. Left on cost
  versus effect (2026-10-03, about 4 hours: schema field, migration of the
  stored file, every dialog and store call): two entries equal in every field
  are indistinguishable on screen. Read from the code on 2026-10-09: a delete
  of either (or both) leaves the same list, so only an Edit of the second row
  differs -- the edited text lands at the first one's position.
- **The readiness probe cannot see a browser renderer's delay**: for
  `Chrome_RenderWidgetHostHWND` the `WM_NULL` round trip answers for the UI
  thread, not the renderer. `CLIPBOARD_RESTORE_DELAY_S` (1.5 s) bounds it;
  `keep_transcript_in_clipboard` closes it. Not measured on a live browser.
  Kept 2026-10-03: no API says a renderer read the clipboard (rejected
  alternatives in `docs/agents/text-insertion.md`).
- **Retry reaches the newest failed recording first, and only three are held**
  (2026-10-03): a failure pushes the slot's holder behind it
  (`_older_failed_audio`, `RETRY_OLDER_FAILURES_MAX`), the oldest of more than
  three is dropped (`retry_failure_dropped` in the log; their audio is only in
  memory), and the overlay and tray offer no way to pick an older one: it
  comes forward once the newer is resolved. A second Retry while W's retry
  runs still stops it (W's late result is kept in history), so W stays
  behind the slot until a Retry resolves it.
- **Two Retry presses can write two history entries**: a remote provider runs
  the stopped first retry to completion and it is kept (a finished
  transcription is never discarded). Local engines cancel cooperatively.
  Kept 2026-10-03 (by design): the restart is how a hung request is re-run
  with changed settings; ignoring a second press with unchanged settings is
  an owner decision (~1 h).
- **Two recordings the store never received are told apart by bytes alone**:
  with `source_recording_id` "" on both, identical audio cross-retires the
  retry slot. Needs two refused writes and byte-identical audio. Kept
  2026-10-03 (cost vs effect): closing it needs a per-failure identity on the
  jobs (~3 h, about 80 references to the slot in the tests); the trigger does not
  occur with microphone audio.
- **A benchmark's device decision is per run, with no "forget" button**:
  `measured_fastest_devices` reads one run; fewer than two measured devices
  keeps the old entry. Remove an entry by re-running, pinning a device, or
  deleting `onnx_auto_preferred_devices` from `%APPDATA%\stt_app\settings.json`
  with the app closed. A stale entry costs speed, never correctness
  (2026-09-19).
- **Custom endpoint trade-offs kept after the 2026-10-03 review** (all P4):
  - A gateway that masks the key itself (LiteLLM-style "sk-...1234" plus a
    key hash in a 401 reason) is shown as sent: only the full stored key and
    the key command's token are recognised and scrubbed. Kept: mask formats
    differ per gateway, what shows is the key's first and last few characters,
    not the key, and a guessed pattern would give false assurance.
  - Scrubbing replaces every occurrence of a credential of 8+ characters, so
    a placeholder key that is an ordinary word (`localhost`) would cut that
    word out of an error message. Kept on purpose: a cut word is cosmetic, a
    real key that happens to be alphabetic and left in a log is not; a
    "dictionary word" test could not tell the two apart.
  - Arguments to a `.cmd`/`.bat` key command containing `& | < > ^ %` are
    refused, because cmd.exe interprets them whatever the quoting (e.g. an
    `az --query "accessToken | [0]"`); put such a call into a script. By
    design: no quoting from the caller is safe against cmd.exe.
- **Touch, pen and precision-touchpad taps may not reach the floating
  overlay's click watch** (2026-10-10, unmeasured): it registers mouse raw
  input only (`raw_mouse_input`), and such taps may arrive from another device
  class, so a tap into the active editor can leave the overlay above it.
- **A slow editor may end above the floating overlay after an overlay menu
  closes** (2026-10-10, unmeasured): `_after_menu_closed` re-raises the overlay
  one event-loop turn after `aboutToHide`; an editor process that handles its
  re-activation later than that (a laptop at 100% CPU) raises itself above the
  overlay again although the user never clicked into it.
- **Audio capture under a starved callback thread** (2026-10-10,
  `docs/agents/audio-capture.md`):
  - A stall longer than `AUDIO_INPUT_BUFFER_S` (20 s) still loses audio;
    MME then drops the whole stall, not just the overflow (measured: 10 s
    stall with the 0.18 s default, 10.1 s lost).
  - A warm stream whose callback thread is stalled when the hotkey comes
    puts the audio from its last block up to the attach into the recording
    (up to 20 s of what was said before the hotkey). PortAudio's timestamps
    cannot place it, and cutting by the attach time would cut words spoken
    between the hotkey and the attach. Recognisable by
    `warm_attach_gap_ms` in `audio_capture_stats`. Fixing it needs the
    hotkey's own message time (`MSG.time`) carried to the capture (~2-3 h).
  - A starved stream that never delivers waits 12 s (the hard limit), not
    2 s, before its Error, unless PortAudio reports it inactive.
  - A stop during a stall can hold the Qt thread for up to
    `AUDIO_STOP_DRAIN_MAX_S` (3 s) while the backlog arrives -- up to 12 s
    while a slow burst is still catching up -- and a stop
    before the first block of a running stream up to the watchdog's 12 s
    hard limit. Not waiting loses the recording; not blocking would need a
    two-phase stop in the controller (~4-6 h with its re-entrancy cases).
  - Loss and backlog are told apart only by a steady second of real-time
    arrivals (the settled deficit). A stop within that second after a
    permanent loss waits until it passes, and a burst that drains at
    0.9-1.1x real time looks like loss, so the rest of it is not waited
    for. Streaming already forwarded the blocks a settling wait drops
    again (at most about a second spoken after the stop).
  - Unverified (review F5): Deepgram closes a streaming socket that gets
    no audio for about 10 s, and the app sends no KeepAlive. A start stall
    that the watchdog now holds for up to 12 s may therefore lose the
    Deepgram session before the burst arrives. Not reproduced; sending
    `{"type": "KeepAlive"}` while no audio was pushed for 5 s would cover it
    (~1-2 h with tests).
