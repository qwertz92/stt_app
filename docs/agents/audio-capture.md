# Audio capture and devices: design decisions

Binding project rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30. Read this file before changing `audio_capture.py`,
`audio_devices.py`, `audio_device_listener.py`, the warm stream and the
silence gate. Entries keep their order, so "the entry above/below" refers to
this file; "Known limitations" is `docs/agents/known-limitations.md`.
Measurements, forced schedules and history are in `docs/learning-log.md` and
git history.

Verbatim pre-condensation text: `git show e608f86:docs/agents/audio-capture.md` (original AGENTS.md: `df2642a`).

- **Warm microphone stream (`keep_microphone_warm`, default off)**: one shared
  PortAudio stream (`WarmMicrophoneStream`) stays open and recordings attach
  as consumers, so capture start is instant on EDR/GPO-hooked stacks where an
  open takes seconds. The controller owns its lifecycle; a capture falls back
  to a cold stream when it is not running. `recording_start_timing` warns
  above 500 ms. Warm opening happens outside the state lock; callbacks are
  generation-scoped (a retained one cannot feed the next recording); cleanup
  always attempts `close()` even when `stop()` fails. The ready-to-speak text
  waits for `capture.start()` to succeed; a streaming session is published
  before it so a first callback inside it is kept.
- **Device selection and changes**:
  - `input_device_name` (empty = default) resolves to an index only at open
    (`audio_devices.resolve_input_device`); a missing selected microphone
    fails the recording -- never record from another device. Devices sharing
    a name are listed as `Name`, `Name (#2)`, `Name (#3)` in PortAudio's
    enumeration order (`_distinct_name`; the first keeps the plain name so
    old selections still resolve, a number never reuses a real device's name).
    The numbers follow enumeration order, which Windows does not keep: after
    a reboot or a replug the twins can swap. A stored `Name (#k)` that no
    longer exists resolves to the remaining `Name` device and logs
    `audio_input_twin_fallback` (a device really named like that wins), so
    recording continues on the same model instead of failing. Windows usually
    makes endpoint names unique itself, so this is for the rare twin.
  - **Every `sd.InputStream` open passes
    `audio_devices.input_stream_extra_settings(device_index)`**
    (`WasapiSettings(auto_convert=True)`): WASAPI shared mode rejects 16 kHz
    with paInvalidSampleRate (-9997) on a 48 kHz endpoint.
  - `audio_devices.try_refresh_input_devices` re-initializes PortAudio; an
    open-lock plus live-stream registry refuses it while any stream is open.
  - `audio_device_listener.py` (comtypes `IMMNotificationClient`, no polling)
    triggers the coalesced close-idle / re-enumerate / reopen; during a
    recording it defers to stop/abort. Without COM: Settings "Refresh" and
    the watchdog self-heal.
  - `request_restart` / `request_close` defer while a consumer is attached.
  - **`attach` checks `opened_device_key` inside `attach`, under the lock
    that publishes the stream**; `expected_device_key` is required and the
    default is `SYSTEM_DEFAULT_INPUT_DEVICE` (""), not `None`.
- **`close_if_idle` waits for any open or close in flight, then closes the
  stream itself; only an attached consumer makes it answer False.** Its caller
  `_refresh_audio_devices_worker` then runs `try_refresh_input_devices`, which
  refuses while a stream is registered (retried only at the next stop). A
  stream is registered with `_stream` None during an open (`_starting`,
  holding `portaudio_guard()`) and during a helper's close
  (`request_restart`, `request_close`, deferred `detach` -- routine).
  - It bumps the generation **before** waiting on a `Condition` for
    `_starting` / `_closes_in_flight` (bounded by `_CLOSE_WAIT_S`), so a
    helper's `ensure_started(generation=...)` is refused; a timeout answers
    False and logs `warm_microphone_stream_busy`. It clears
    `_pending_restart` with the bump.
  - It closes via `_close_retiring` inside `_closes_in_flight`, re-reads
    `_stream` after each close, and answers True only with nothing in flight
    after its own closes. `_CLOSE_WAIT_S` bounds waits on *other threads*;
    its own closes run to completion and re-arm the budget only when
    `_close_retiring` reports it closed something. A restart issued after the
    bump is closed by this call too, but at most `_MAX_OWN_CLOSES` (4) streams
    in all: past that it answers False and logs `warm_microphone_stream_busy
    restarts_after_close=N`, and the deferred refresh reopens (measured before
    the cap: 25 restarts kept it closing for 3.14 s against a 0.4 s budget).
  - **`close` also waits for a helper's close in flight**, bounded by
    `_CLOSE_JOIN_S` (2.5 s, not `_CLOSE_WAIT_S`: `shutdown()` calls it on the Qt
    thread), so the live-stream registry is clear when it returns; a wait that
    runs out logs `warm_microphone_stream_close_unfinished`.
- **A retired stream stays reachable until closed** (`_retiring`, drained by
  `_close_retiring`, first comer closes; a superseded open's stream too).
  `_spawn_or_run` closes on the calling thread when no helper can start and
  never reopens there (a raising `Thread.start` in `detach` lost a whole
  recording). `close` drains `_retiring` and bumps the generation, cancelling
  a helper's reopen.
- **A microphone change saved during a warm open restarts it only if the
  device differs** (`WarmMicrophoneStream.is_opening`, `opening_device_key`
  = selected key while `_starting`): otherwise the open finished on the old
  device and `attach` refused it all session. `opening_device_key` comes from
  `selected_key_provider` before the device is resolved; the controller reads
  `device_state()` (one lock hold). Tested with the real stream in
  `tests/test_controller_audio_devices.py`.
- **`AudioCapture.start()` re-arms `_callback_failed`** (log once per
  capture). A first-callback watchdog timeout on a warm capture restarts the
  warm stream.
- **A warm open queues behind the device refresh**:
  - **`ensure_started` takes the PortAudio guard before its gate** (opens on
    the fresh list).
  - **The refresh worker must not hold the guard across the close**: a cold
    start takes it on the Qt thread and hung the UI for the close (`d5e4f2a`).
  - **The worker runs a second `close_if_idle` + re-enumeration round, not
    gated on `is_running`**: the save's retry and the worker's reopen carry no
    generation, and `_stream` is None in exactly the in-flight states.
  - Workers serialize on `_audio_device_refresh_lock`.
  - **`_pending_audio_device_refresh` is discharged by the worker as it
    starts and re-armed by its refusals** (cleared by
    `_maybe_resume_pending_audio_device_refresh` too). A worker whose
    `Thread.start` raises re-arms and logs it.
  - **`close` is terminal** (`_closed`, checked in the gate under the lock),
    or an open parked behind the guard opened an unreferenced microphone.
  - **`detach` does the restart bookkeeping under the hold that released the
    consumer** (`_restart_locked`, shared with `request_restart`).
- **Nothing may escape the PortAudio callback**: sounddevice catches only
  `CallbackStop`/`CallbackAbort`; anything else hits cffi `error=paAbort` and
  silently ends a cold stream. `_process_audio` wraps its body, logs once.
  **`_auto_stop_fired` is reset when the auto-stop `Thread(...).start()`
  raises**, so the next block retries.
- **Both microphone pickers offer `audio_devices.input_device_choices`**
  (2026-10-03): the Settings Audio combo and the overlay's microphone menu.
  The default entry names Windows' device
  (`audio_devices.system_default_input_name`), else "System default (follow
  Windows)".
- **A picker says "(device list unavailable)" when PortAudio did not
  answer**, "(not connected)" only when it did: `query_input_devices` takes
  no lock (Qt thread) while the worker holds `portaudio_guard`. The value is
  unchanged, so a Save keeps the selection.
- **The overlay's microphone menu writes the setting like the Lang menu**
  (2026-10-03, `controller.set_input_device_name`): saved straight to the
  store (a refusal reported through `_report_unsaved_overlay_setting`), then
  `_sync_warm_microphone_stream` retargets a warm stream on another device
  (deferred while attached, as for a settings save). Refused while a
  recording starts, runs or stops (`_capture_owns_the_microphone`, shared
  with the device-change deferral): the overlay disables the button while
  Listening, and this covers a menu left open when the hotkey started one.
  The controller refreshes the menu's choices on every settings load, when
  the menu is about to open (`microphone_menu_requested`) and after a
  successful re-enumeration (`audio_devices_refreshed`, emitted by the
  refresh worker, queued to the Qt thread), so the caption names today's
  default device. A Settings save does not undo an overlay pick: the
  dialog's combo is diffed against `_populated_settings`.
- **First audio callback watchdog** (two stages since 2026-10-10): a Qt timer
  armed at capture start. At `AUDIO_CAPTURE_FIRST_CALLBACK_TIMEOUT_MS` (2 s)
  with no block, a stream PortAudio does not report stopped
  (`AudioCapture.stream_is_active() is not False`) is starved, not dead: it
  logs `audio_capture_callback_slow` and re-arms up to
  `AUDIO_CAPTURE_FIRST_CALLBACK_HARD_TIMEOUT_MS` (12 s, below the input
  buffer, so a burst just before it still holds everything). The 2 s abort
  was the field report "a recording stops on its own right away and a red
  error appears" on a laptop at 100% CPU. A stream reported inactive aborts
  at once. The overlay keeps "Speak now" (the audio is buffered). The abort
  itself is unchanged: late bytes are kept for Retry, never submitted; only
  late bytes write the retry slot (persisted, marked failed under the id the
  persist returned); without them the Error offers no Retry. Snapshot
  diagnostics before `capture.stop()`.
- **Every microphone stream asks for a 20 s PortAudio input buffer**
  (`AUDIO_INPUT_BUFFER_S`, passed as `latency` by `_open_input_stream`, the
  one constructor both the cold capture and the warm stream use;
  2026-10-10). While the callback thread is not scheduled (CPU load, an
  endpoint scanner, the GIL held elsewhere) the device keeps capturing into
  this buffer, and what does not fit is lost. Measured on HomeBase with the
  callback blocked for 10 s: the device default ("high": 0.18 s MME, 0.01 s
  WASAPI) lost 10.1 s (MME) / 9.9 s (WASAPI -- with no overflow flag at
  all); 12 s lost nothing. Unstalled, "high" through 30 s gave identical
  frames and first-callback delays (110-175 ms) on all ten MME/WASAPI inputs
  there; MME open/start/close cost +4/+5/+8 ms at 20 s. A driver that
  refuses the buffer is reopened with its default (`audio_input_buffer_refused`).
  Consumers of the chunk callback must take a 20 s burst: Deepgram's send
  queue is sized for it (`docs/agents/streaming.md`); AssemblyAI's SDK queue
  and the local streaming queues are unbounded.
- **`stop` collects a backlog the callback thread still owes** (2026-10-10):
  stopping the stream discards what PortAudio buffered but has not delivered
  (measured: a stop 2 s in, during a 3 s stall, kept 1.1 s; with the wait,
  as much as an unstalled run). When the captured audio is behind the wall
  clock (since the warm attach, or since a cold `start()` returned) by more
  than `AUDIO_BACKLOG_TOLERANCE_S` + `AUDIO_BACKLOG_DRIFT_PER_S` x length,
  `stop` waits on `_audio_arrived` until the audio reaches the stop moment
  within one block, at most `AUDIO_STOP_DRAIN_MAX_S` (3 s, on the Qt thread)
  -- longer, up to the 12 s hard limit, while blocks keep coming (one
  within the last 2.5 block lengths, or the last second's audio at over
  1.25x real time, `AUDIO_STOP_DRAIN_CATCH_UP_RATE`; review round 2: under
  contention MME drains a burst at about 2x, under CPU load plus a busy
  Python thread it first sent seconds at only real time, and a loss not yet
  settled needs its steady second); on a stream PortAudio reports running,
  up to the 12 s hard limit counted from the stall's start (the last
  block), like the zero-block case below (review round 3 F2: a 10 s stall
  from 1 s in, stop at 4 s, gave up 3 s after the stop and kept 1.0 of
  4.0 s on the real MME and WASAPI microphone) -- or until PortAudio
  reports the stream stopped; blocks past the stop moment
  are refused (`_drain_cutoff_frames`). No "silent for a while" exit: a
  starved thread delivers nothing for seconds, then everything. A capture
  with zero blocks is waited for only while the first-callback watchdog would
  wait -- PortAudio reports the stream active and the 12 s hard limit has not
  passed -- and then until that limit (review F1: a 4-10 s start stall and a
  3.5 s dictation kept nothing, and streaming then finalized an empty stream
  as "No speech detected"). A streaming stop with no audio and no live text
  is now an Error ("No audio captured"), never a finalize. The frame count
  cannot place the stop moment after a permanent loss (stall longer than the
  buffer, a refused buffer, WASAPI's silent drop): the audio stays behind
  while the stream is back to real time, and every stop waited 3 s and kept
  ~3 s said after it (review F2, real MME microphone, 0.01 s buffer, 5 s
  stall). The **settled deficit** separates loss from backlog
  (`_CaptureTiming.settled_deficit_s`, review round 2): the deficit (audio
  the wall clock owes minus audio received) is measured once the last
  `AUDIO_STEADY_PACE_GAPS` (10) gaps between blocks spanned the audio they
  carried within 0.9-1.1, none longer than 2.5 blocks, and taken as loss
  (plus the stream's own latency) only **with evidence** (`_settle`): a
  lower deficit always (the stream caught up); a higher one after evidence
  since it last rose -- an input-overflow flag (MME raises one when it drops
  audio: the 0.01 s-buffer runs had `overflows=1`) or a gap between blocks
  of at least `AUDIO_BUFFER_EVIDENCE_RATIO` (0.85) of the buffer (review
  round 3 F1: WASAPI drops without the flag) -- otherwise only the part the
  buffer cannot hold (all of it when the 20 s buffer was refused --
  `_open_input_stream` reports 0.0).
  Pace alone is no evidence: on the real MME microphone under 16 CPU
  burners plus a busy Python thread, a 4 s stall was followed by 1-2 s of
  blocks at exactly real-time pace, the deficit flat at 4 s, before the
  backlog came as a burst (timeline probe, 4/4 runs); settled on pace, 4 of
  5 stops kept 1.0-1.1 of 3.0 s. PortAudio does not report the buffer it
  gave (`stream.latency` 0.100 s for every request on MME, 20.1 on
  WASAPI), and what it gives can be less than requested: WASAPI on the
  real HyperX microphone held about 18 s of the 20 s request (an 18 s stall
  lost 0.10 s, a 24 s stall 6.0 s, no flag; review round 3). So the buffer
  is relied on for 0.85 of what was requested (`effective_buffer_s`), which
  also caps the pre-attach audio counted towards a stop. Without the gap
  evidence a 24 s WASAPI stall never settled, and a stop at 29 s waited
  5.99 s and kept 6 s said after it. The stop waits only for the deficit beyond it,
  and its stop moment is the audio owed minus it; when the deficit settles
  during the wait (the stream caught up and runs on in real time), blocks
  taken past the new stop moment are dropped again
  (`_keep_first_frames_locked`). A single gap decides nothing: the first
  design ended the wait on one gap of 0.5-1.5 block lengths, and under CPU
  load or a busy Python thread a burst's blocks come 46-130 ms apart (31 ms
  idle, real MME microphone), so it refused the rest of the burst -- stall
  1-5 s, stop at 3 s: 1.1-1.7 s kept of 3.0 (5/5 runs); a 100 ms pause
  mid-burst kept 2.00 of 3.00 s. Judged over ten gaps, a device delivering
  blocks in pairs (gaps ~0 and ~200 ms, review P4) settles like any other
  (`test_a_device_delivering_blocks_in_pairs_settles_a_permanent_loss`).
  Cost: a stop within a second after a loss waits until it settles. For a warm capture the
  audio owed includes `warm_attach_gap` (`_CaptureTiming.pre_attach_s`): the
  burst of a stall spanning hotkey and stop carries the seconds before the
  attach too, and a cutoff counted from the attach refused the last ones
  before the stop (review F3: 4.0 of 5.0 s kept). The blocks the wait
  collects in streaming reach the transcriber: `stop_recording` keeps the
  capture in `_stopping_capture` across `capture.stop()`, which
  `_on_stream_audio_chunk` accepts (review F4: with `_audio_capture` already
  cleared they were dropped while the Qt thread waited). A healthy stream is behind
  by the first-callback delay plus one block (about 0.1-0.3 s), so its stop
  never waits (`test_stop_on_a_healthy_stream_neither_waits_nor_changes_the_audio`).
  A stop that will wait (`AudioCapture.backlog_wait_expected`, the same
  decision as the wait's) first paints "Processing" -- "Collecting the
  microphone's delayed audio..." -- with `OverlayUI.paint_now` (a direct
  repaint; `processEvents` would deliver queued signals such as a stream
  abort into the stop), and record-hotkey presses made during the wait are
  dropped (`docs/agents/windows-platform.md`; review round 2 P2: the overlay
  said "Speak now" through the wait, and a second press started a new
  recording once it ended).
  Only the user's stop (`stop_recording`) waits: a cancel, an abort
  (`_abort_streaming_session`, `_stop_active_capture`) and the quit call
  `stop(drain=False)` and keep what arrived -- for the canceled recording,
  the Retry slot or the unfinished store -- without holding the Qt thread
  (review round 2 P3).
- **`audio_capture_stats`, one line per recording** (logged by
  `AudioCapture.stop`; WARNING when the audio fell behind, a gap exceeded
  0.5 s or an overflow flag came): first-callback delay, callbacks, audio
  against wall seconds and the deficit (counting a warm burst's pre-attach
  audio, as the stop does), the settled deficit, longest gap, overflow and status
  counts, audio arrived by 1/2/3 s wall (`audio_by_1s_2s_3s`, about
  0.9/1.9/2.9 when healthy), `warm_attach_gap_ms` (time since the warm
  stream's previous callback at attach) and the stop's wait. The per-block
  "Audio stream status" warning is written once per recording: it runs on
  the PortAudio thread. A refused warm attach logs
  `warm_microphone_attach_refused reason=...` (the recording cold-opens).
  WASAPI never sets the overflow flag for a starved callback (measured), so
  the deficit and the gaps are the evidence, not `overflows`.
- **No pre-roll and no trim at a warm attach** (2026-10-10). PortAudio's
  per-block timestamps cannot place audio in time (MME reports 0, WASAPI
  times in the future, measured after a stall), so audio before the attach
  cannot be told from audio after it. A callback thread stalled across the
  hotkey therefore delivers the seconds since its last block, the part before
  the attach included; they are kept (cutting at the attach would also cut
  what was said between the hotkey and the attach) and `warm_attach_gap_ms` records
  it (`docs/agents/known-limitations.md`). The gap is the current stream's:
  `_last_callback_at` is cleared when a warm stream is retired or accepted
  (review round 2 P3: kept across a reopen, an attach before the new
  stream's first callback read a 60 s gap, counted 20 s of pre-attach audio
  towards the stop moment and kept up to 20 s said after the stop). A deliberate pre-roll is not
  added: on a healthy stream the warm path loses only the short "Starting"
  phase (a 25 ms event drain, plus the start tone when it is on), and a
  pre-roll would record that tone, which plays right before the attach.
- **Silence gate (`silence_gate_enabled` + `silence_gate_threshold`, default
  on/0.004)**: a batch recording whose loudest 100 ms window
  (`measure_peak_windowed_rms`, ~-48 dBFS) stays below the threshold is not
  transcribed. Default on: it is the only guard for every engine
  (Cohere/Granite have no VAD; faster-whisper's `vad_filter` follows
  auto-stop). Evidence: 26 silent/hallucinated recordings 0.0006-0.0034, 7
  utterances 0.0075-0.0290, so 0.0040 sits 1.2x above the loudest silent one
  and the quietest utterance is 1.9x above it; a -40 dBFS whisper measures
  0.0071 (passes), room tone at -54 dBFS is blocked. Logs
  `recording_peak_level`. The gate's canceled
  mark is keyed by the id the stop's persist returned and skipped (text says
  not kept) when it failed. Unmeasurable audio (`None`) is never gated. Schema
  22 turns the gate on once for older files; "off" saved at >= 22 is kept.
- **Behind the level gate, a speech check (2026-10-01, `silero_vad.py`)**: the
  Silero VAD v6 graph faster-whisper ships (MIT; found by `find_spec`, never
  imported; `stt_app.spec` collects it -- no PyInstaller hook does) runs on
  the CPU, one thread. It runs only when the gate is on and the level gate has
  passed the recording -- otherwise its answer could not skip anything and
  would only cost a scan on the Qt thread (about 8 ms warm for a speechless 3
  s stop, both scans; measured 2026-10-01). The session (123-275 ms cold,
  mostly importing ONNX Runtime) is built on a daemon thread at controller
  start and on a settings reload with the gate on; a stop that finds it not
  yet built answers unmeasured (`loading`), never builds it and never waits
  for a build in progress: `start_loading` takes its own `_loader_lock`, not
  the lock the build holds (a stop that waited on it froze the Qt thread for
  the whole build). A failed load is retried after `SILERO_LOAD_RETRY_S` (60
  s), so a file a scanner holds for a moment does not switch the check off
  until restart. A recording the level gate admits is skipped (reason
  `speech_check`, same canceled mark and overlay shape) only when the gate is
  on and `SpeechCheck.no_speech` holds: a COMPLETE scan as recorded AND a
  complete scan of a copy amplified by `SILERO_BATCH_QUIET_SPEECH_GAIN` (8,
  clipped) both stay below `SILERO_BATCH_MIN_PROBABILITY` (0.15). The
  amplified scan is not optional: Silero scores by level, and speech at the
  lowest settable threshold that a keystroke lifted over the level gate scored
  as low as 0.025 as recorded and 0.246 or more amplified, while
  faster-whisper still transcribed it. A scan cut by `SILERO_BATCH_MAX_SCAN_S`
  or stopped by `SILERO_BATCH_STOP_AFTER_SPEECH_S` is incomplete and never
  skips; a graph that will not load, a scan that raises and audio that is not
  16 kHz 16-bit mono answer `None` and never skip. Every stop that ran the
  check logs both scores on `recording_peak_level`
  (`silero_amplified_max_probability=not_run` when the first scan settled it);
  otherwise the line says `silero_speech_seconds=not_run`, `loading` or
  `unavailable`. No setting of its own: it follows `silence_gate_enabled`. Its
  figures, ranges over seeds and grid offsets because non-speech scores move
  with both, are in the `config.py` comment; do not quote one value for a
  noise. `silero_vad.check_speech_pcm16` / `check_speech_wav` are the one
  answer to "does this audio hold no speech" for any other caller.
