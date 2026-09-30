# Audio capture and devices: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
`audio_capture.py`, `audio_devices.py`, `audio_device_listener.py`, the warm stream and the silence gate. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **Warm microphone stream (`keep_microphone_warm`, default off)**: one shared
  PortAudio input stream stays open (`WarmMicrophoneStream`); a recording
  attaches as its consumer, so capture start is instant even where opening
  the microphone takes seconds (EDR/GPO-hooked audio stacks) and the first
  words were cut off. The controller owns its lifecycle (settings change,
  system resume, shutdown); a capture falls back to a cold stream when the
  warm one is not running. `recording_start_timing` logs beep and
  capture-start durations and warns above 500 ms.
  Warm-device opening happens outside its state lock so recording start never
  blocks behind an in-progress background open. Each capture installs a
  generation-scoped callback; callbacks retained by PortAudio after detach are
  ignored and cannot append audio to the next recording. Stream cleanup must
  always attempt `close()` even when `stop()` fails. The overlay must not show
  the ready-to-speak instruction until `capture.start()` has succeeded; its
  preceding wait message keeps slow cold opens from inviting speech that cannot
  yet be recorded. A streaming controller session is published before
  `capture.start()` so a first callback delivered from inside that call reaches
  the active transcriber instead of being discarded.
- **Microphone selection and audio device changes**: `input_device_name`
  (Audio-tab picker; empty = system default) is resolved to a PortAudio index
  only at stream open via `audio_devices.resolve_input_device`; a selected but
  missing microphone fails the recording with an actionable error — never
  silently record from another device. Explicit selections resolve to WASAPI
  device indices, and WASAPI shared mode rejects the app's 16 kHz capture
  rate with paInvalidSampleRate (-9997) when the endpoint mix format differs
  (typically 48 kHz) — the MME sound mapper behind the default path resamples
  transparently, which is why only explicit selections failed. Every
  `sd.InputStream` open therefore passes
  `audio_devices.input_stream_extra_settings(device_index)`, which returns
  `WasapiSettings(auto_convert=True)` for WASAPI devices so PortAudio
  resamples like the default path; do not open input streams without it. PortAudio freezes its device list at
  initialization, so `audio_devices.try_refresh_input_devices` re-initializes
  PortAudio; a shared open-lock plus live-stream registry makes re-enumeration
  impossible while any stream is open (it is refused and retried, never allowed
  to invalidate a running capture). `audio_device_listener.py` registers an
  `IMMNotificationClient` (comtypes, event-driven — no polling) so a Windows
  default-capture switch or hot-plug immediately triggers the controller's
  coalesced reaction: close the idle warm stream, re-enumerate, reopen. While
  a recording is active the refresh defers and resumes on the stop/abort
  paths. The warm stream owns its lifecycle races: `request_restart` /
  `request_close` defer while a consumer is attached and execute on detach, so
  disabling the setting, a resume, or a device event can never cut off a
  running recording's audio source; `attach` additionally requires the warm
  stream's `opened_device_key` to match the recording's selected device --
  inside `attach`, under the lock that publishes the stream, so the check
  cannot be separated from the attach it guards. It used to sit in
  `AudioCapture.start`, one lock acquisition earlier, which is a gap of
  microseconds against a device open of milliseconds to seconds; nothing was
  observed going through it, and it moved because an invariant documented as
  belonging to a function that does not hold it is the shape a later refactor
  drops. `expected_device_key` is a required positional argument for the same
  reason -- the system default is `SYSTEM_DEFAULT_INPUT_DEVICE` (the empty
  string), not `None`.
  **`close_if_idle` waits for an open or a close in flight, then closes
  the stream itself, and only an attached consumer makes it answer False.**
  Two ways it used to answer True with the registry still holding a stream,
  and in both the caller (`_refresh_audio_devices_worker`) then ran
  `try_refresh_input_devices`, which found the stream registered and
  refused -- and a refused refresh is only retried on the next recording stop
  or abort, so a hot-plugged or newly defaulted microphone stayed invisible
  until the user recorded once or pressed Refresh:
  - **An open in flight**: `_starting` True, `_stream` still None. The open
    holds `portaudio_guard()` across construct/start/register, so the refresh
    blocked on that lock and then found the stream. Answering False here (the
    first fix) only deferred the same refresh to the same moment.
  - **A close handed to a helper thread**: `request_restart`, `request_close`
    and a deferred `detach` null `_stream` under the lock and close outside
    it, so the stream was open and registered while `_stream` already said
    idle. Routine, not exotic: a recording stop runs `detach` (a deferred
    restart) and then arms exactly this refresh. Measured:
    `close_if_idle() -> True`, `live_stream_count() -> 1`,
    `try_refresh_input_devices() -> False`.
  It now waits on a `Condition` for `_starting` and `_closes_in_flight` to
  clear (bounded by `_CLOSE_WAIT_S`), bumps the generation **before** the
  wait, and then closes whatever is left. The order is load-bearing: a
  restart helper that finishes its close while this waits reaches its reopen
  on its own lock acquisition, and the notify does not hand the waiter the
  lock first -- measured, the helper reopened, the waiter then waited for
  that open and closed the new stream, and the test timed out inside the
  second close. Bumped first, the helper's `ensure_started(generation=...)`
  is refused whichever thread wins. A wait that runs out answers False, logs
  `warm_microphone_stream_busy`, and leaves the stream closed until the
  deferred refresh reopens it -- honest for an audio stack that has not
  finished a close in ten seconds.
  **A retired stream stays reachable until something has closed it**
  (`_retiring`, drained by `_close_retiring`; whoever gets there first
  closes). `request_restart` used to hand the stream to the helper's closure
  and nowhere else, so a `Thread.start` that raised left a PortAudio stream
  open and registered for the process lifetime with `close`,
  `close_if_idle` and `request_close` all finding `_stream` None -- and,
  reached through `detach` inside `AudioCapture.stop`, the `RuntimeError`
  escaped before the chunks were drained, so the whole recording was lost.
  `_spawn_or_run` now runs the close on the calling thread when no helper
  can start, and never reopens there. `close` drains `_retiring` too and
  bumps the generation, which is what cancels a restart helper's reopen:
  disabling `keep_microphone_warm` during an in-flight restart used to let
  the helper open a fresh stream *after* the controller had dropped its
  reference -- a microphone open for the process lifetime, invisible to
  shutdown, every re-enumeration refused.
  **A microphone change saved while the warm stream is opening restarts
  it** (`WarmMicrophoneStream.is_opening`): `opened_device_key` is None
  during an open exactly as it is after a failed one, and the save took the
  retry branch, whose `ensure_started` no-ops on the `_starting` guard -- so
  the open finished on the previous device and nothing ever restarted it;
  `attach` then refused the stream and every recording cold-opened, i.e. the
  feature was silently dead, and the losing case is the slow open the
  feature exists for. A restart requested mid-open makes the open discard
  its stream and re-resolve, at worst one extra open.
  **A superseded open retires its stream through the accounting**, and
  **`close_if_idle` cancels the reopen a pending restart scheduled.** The
  open that a restart superseded closed its stream on its own thread after
  the lock was released, so `close_if_idle` -- which waits only for
  `_starting` and `_closes_in_flight` -- answered True while that stream was
  still registered, and the refresh it arms found it and refused (measured:
  `close_if_idle() -> True`, `live_stream_count() -> 1`). The discarded
  stream now goes into `_retiring` under the lock and is closed by
  `_close_retiring`, i.e. the same road every other retired stream takes.
  And `close_if_idle` cleared nothing but the generation: a `_pending_restart`
  left by a `request_restart` during a capture made the `detach` that followed
  reopen the stream the refresh had just closed, so the re-enumeration was
  refused on the next stop as well. It clears the flag with the bump.
  **The controller restarts an in-flight open only when the device differs**
  (`WarmMicrophoneStream.opening_device_key`, the selected key while
  `_starting`, else None): the first fix restarted every open in flight,
  so an unrelated save during the seconds a locked-down stack takes to open
  -- an opacity change, a hotkey -- cost one extra open each time. And
  `AudioCapture.start()` re-arms `_callback_failed`, which is what makes the
  once-per-capture callback log once per *capture* rather than once per
  process; the latch used to survive into the next recording. A
  first-callback watchdog timeout on a warm capture restarts the warm stream
  automatically (self-heal) instead of only suggesting to disable the feature.
  Without COM/comtypes the listener is inert and the Settings "Refresh" button
  plus watchdog self-heal remain the manual/backstop paths.
- **A warm-stream open queues behind the device refresh, and `close_if_idle`
  counts its own closes.** Four things the second wave on the round-24 fixes
  measured, each closed at the place the invariant lives:
  - **`ensure_started` takes the PortAudio guard before its gate**, so an
    open arriving during the re-enumeration -- which holds the guard inside
    `try_refresh_input_devices` -- waits and then opens on the fresh device
    list. With the gate outside the guard it passed first and opened on the
    list about to be replaced. **The refresh worker does not hold the guard
    across the close.** It did for one round (`d5e4f2a`), and that froze
    the Qt thread: a cold recording start takes the guard on the Qt thread,
    a close on a locked-down stack is bounded by nothing, and the hotkey
    press played the start beep and then hung the whole UI for the length
    of the close (measured: 3.0 s for a 3 s close, 30.0 s for one past
    `_CLOSE_WAIT_S`, 0.000 s with the close outside). `close_if_idle`'s own
    loop re-reads `_stream` after each close it performs, so a stream that
    opens *during* the close is retired before it answers True; the gap
    between its answer and the re-enumeration gets one more round in the
    worker, which asks `close_if_idle` again and re-enumerates once more,
    while a recording's stream is left alone and the refresh deferred to
    its stop. That second round is not gated on `is_running`: a stream is
    registered while `_stream` is already None in exactly the states
    `close_if_idle` waits for -- a helper closing it, an open in flight, a
    superseded open's retirement -- and the gate skipped the round for
    those (measured: a save's reopen in the gap handed to a restart
    helper, one refused round, the refresh deferred to the next recording
    stop, i.e. the symptom the docstring records as fixed). Note what the
    generation bump refuses and what it
    does not: a restart helper's reopen carries a generation and is refused;
    the settings save's retry and the worker's own reopen carry none and are
    not, which is why the second round exists. The workers queue on a lock
    of their own (`_audio_device_refresh_lock`), so several device
    notifications more than the settle interval apart still run one after
    the other. `_CLOSE_WAIT_S` bounds one wait of `close_if_idle` for
    *another thread's* open or close; the closes it performs itself run to
    completion (measured: a 2.4 s close against a 0.6 s budget answers True
    after 2.41 s, no busy log), bounding them would mean answering True
    with a stream still closing, and each own close re-arms the budget:
    taken once at entry, a 1.2 s own close against a 0.6 s budget left
    nothing for the wait after it, so an open in flight that finished
    0.3 s later was not waited for, the worker deferred the re-enumeration
    and the log blamed a stack that had finished its close. *Own* close:
    `_close_retiring` returns how many streams the call closed, and a
    pass in which a helper popped the retired stream first re-arms
    nothing -- that is the helper's close, another thread's work, inside
    the budget. Re-armed after every pass, a second actor handing a
    stream over every budget-minus-epsilon kept the call from answering
    (forced schedule: True after 2.59 s against a 0.4 s budget, no busy
    line). The bump refuses only a reopen carrying a generation captured
    *before* it: a `request_restart` issued after the bump reopens with
    the bumped generation, this call retires and closes that stream
    itself, and an own close re-arms the budget -- so the half in which
    this call closes the handed-over stream is still unbounded (forced
    schedule, wave 7: 25 restarts 0.1 s apart against a 0.4 s budget
    answered True after 3.14 s, no busy line). Its producers are a
    microphone change saved in Settings and a system resume, both
    serialized on `_audio_device_refresh_lock`, so one restart costs one
    extra open and close on the refresh worker thread, off Qt; recorded
    under Known limitations rather than closed. (This entry said "no
    natural producer at HEAD, since the bump refuses every
    generation-bearing reopen" for one round; the docstring three lines
    below it already said the opposite.) And a worker discharges the owed
    refresh
    (`_pending_audio_device_refresh`) as it starts, its refusals re-arm
    it. Before this the Qt-thread slot cleared the flag *before* spawning
    the worker and nothing cleared it when the worker finished (three
    sites arm it and `_maybe_resume_pending_audio_device_refresh` clears
    it as well; this entry said "nothing else touched it" for one round),
    so a worker scheduled while
    its predecessor still queued on `_audio_device_refresh_lock` found
    the flag clear, the predecessor then refused and re-armed it, the
    newer one succeeded and left it armed -- and the following recording
    stop paid a close, a re-enumeration and a reopen for a refresh that
    had already happened. (This entry said "cleared at the end of the
    worker" for one round; nothing ever cleared it there.) Clearing at
    the end instead would discharge a device event deferred on the Qt
    thread during the run. **A worker that cannot start leaves the
    refresh owed**: the slot clears the flag and then calls
    `Thread.start`, which raises when the interpreter cannot create
    another thread -- the refresh was discharged with no worker to run
    it, the microphone stayed invisible until the next device event, and
    the exception escaped the Qt slot. The flag is re-armed and logged,
    and the next recording stop retries.
  - **`close` is terminal** (`_closed`, checked in the gate under the lock).
    The controller drops its reference to the stream it closes and builds a
    fresh one when the feature returns, so an open that passed its gate
    after `close` was a microphone nothing referenced. With the gate behind
    the guard, an open parked there behind another thread's hold -- an
    earlier open still constructing its stream, or the re-enumeration --
    while `close` ran opened afterwards: three Settings saves a second
    apart during a slow *open*, off/on/off, and `shutdown()` found nothing
    to close (measured: `_warm_mic_stream` None, one stream registered
    after shutdown, every later re-enumeration refused). Not during a slow
    close, which the first version of this entry said: `close` takes the
    stream's own lock and never the PortAudio guard, so nothing parks
    behind it (measured on the pre-fix tree: the same three saves during a
    3 s close leave no orphan; during a 3 s open they do). The
    pre-`d5e4f2a` gate claimed
    `_starting` before parking and observed the bump in its `finally`; the
    guard-first gate claims nothing while parked, and the bump refuses only
    a generation-bearing reopen (above).
  - **`close_if_idle` closes through `_close_retiring`**, inside
    `_closes_in_flight`, and answers True only once nothing is in flight
    *after* its own closes. Closed outside the accounting, a second
    `close_if_idle` found nothing to wait for and answered True while the
    first was still inside `stream.close()`.
  - **`detach` runs the restart's bookkeeping under the hold that released
    the consumer** (`_restart_locked`, shared with `request_restart`). With
    the lock released in between, a `close_if_idle` on the refresh thread
    bumped and closed everything and the restart then bumped again on its
    own acquisition, so its helper's reopen carried the current generation
    and opened behind the caller's re-enumeration -- a forced schedule, 0
    hits in 400 natural trials, closed because the shape allowed it.
  - **`opening_device_key` is published from the selected key the open reads
    *before* resolving the device** (`selected_key_provider`, wired by the
    controller). Published from the resolved key it was None for the whole
    device query -- milliseconds to seconds on the stacks this feature
    exists for -- so a microphone change saved inside that query asked for
    no restart and the open finished on the old device, which `attach` then
    refused for the rest of the session. The controller reads
    `device_state()`, the three fields under one lock hold; three property
    reads had gaps an open could finish in.
  A stream built without `selected_key_provider` keeps the old gap: the
  controller is the one constructor call, and
  `tests/test_controller_audio_devices.py` drives that wiring with the real
  stream and a blocking device query.
- **Nothing may escape the PortAudio callback, and the VAD auto-stop latch
  is set after its thread exists.** sounddevice's wrapper catches only
  `CallbackStop`/`CallbackAbort`; any other exception reaches the cffi
  callback built with `error=paAbort`, which ends the stream at once, so a
  cold-stream recording went silently deaf with the traceback on a stderr a
  windowed build does not have (a warm stream's `_dispatch` swallowed it, so
  audio kept flowing there). `_process_audio` wraps the whole body and logs
  once. The auto-stop set `_auto_stop_fired = True` *before*
  `Thread(...).start()`, so a start that raised latched auto-stop off for the
  rest of the recording; the flag is reset on that failure and the next block
  tries again.
- **The microphone picker's system-default entry names the device Windows
  uses** (`audio_devices.system_default_input_name`, 2026-09-27), e.g.
  "System default: Microphone (HyperX QuadCast S)", and reads "System
  default (follow Windows)" when that name cannot be read.
- **The microphone picker says "(device list unavailable)" when PortAudio
  did not answer, and "(not connected)" only when it did.** The refresh
  worker holds `portaudio_guard` across terminate/initialize, which can take
  seconds on a locked-down stack, and `query_input_devices` takes no lock
  (deliberately: it runs on the Qt thread), so a populate in that window gets
  no list at all -- and called the user's plugged-in microphone "(not
  connected)". The item data is the same either way, so a Save in the window
  keeps the selection; the repopulate timer corrects the label.
- **First audio callback watchdog**: after a successful capture start, a bounded
  Qt timer verifies that PortAudio actually delivered a callback. A timeout is
  an abort, never a normal stop/transcription: a callback can race just after
  the timeout check, so any late bytes are retained for Retry but are not
  submitted automatically while an Error is shown. Only late bytes write
  the retry slot, persisted and marked failed under the id the persist
  handed back: a timeout with none used to empty an older failure's only
  copy, and that Error offers no Retry, because the button would have
  transcribed the older recording under an Error about this one (wave
  16). Snapshot warm-stream and callback-count diagnostics before
  `capture.stop()` mutates them.
- **Silence gate (`silence_gate_enabled` + `silence_gate_threshold`, default
  on/0.004)**: batch recordings whose loudest 100 ms window stays below the
  threshold skip transcription entirely (speech models hallucinate words from
  silence). It defaults to on because it is the only guard that covers *every*
  engine: the Cohere/Granite runtime exposes no VAD and no no-speech
  probability at all, and faster-whisper's `vad_filter` is tied to the
  auto-stop checkbox, which is off by default. The threshold is ~-48 dBFS on
  the *loudest* window (`measure_peak_windowed_rms`), which a whisper clears
  with room to spare (measured: -40 dBFS whisper → 0.0071 vs. 0.0040 gate,
  while room tone at -54 dBFS is blocked). Every batch stop logs
  `recording_peak_level` for tuning, and gated audio stays available as the
  last recording -- when the stop road's persist wrote it: the gate's
  canceled mark is keyed by the id that write handed back and skipped when
  the write failed, and its text then says the recording could not be kept,
  where before the unkeyed mark relabelled the previous recording -- one
  still transcribing -- canceled with the gate's text and the text called it
  kept (the wave-17 reach lens, on the real store). Unmeasurable audio
  returns `None` and must never be gated —
  undecodable bytes are a failure to surface, not silence. Schema 22 turns the
  gate on once for older settings files: every file written before the default
  flip carries "off", so a stored "off" could not be told apart from a
  deliberate choice; an "off" saved at schema >= 22 is kept. Field data from
  one session backs the threshold: 26 silent/hallucinated recordings measured
  0.0006-0.0034 and 7 real utterances 0.0075-0.0290, so 0.0040 separates them
  with 1.9x margin below and 1.2x above.
