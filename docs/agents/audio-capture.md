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
    fails the recording -- never record from another device.
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
- **First audio callback watchdog**: a bounded Qt timer after capture start.
  A timeout is an abort: late bytes are kept for Retry, never submitted; only
  late bytes write the retry slot (persisted, marked failed under the id the
  persist returned); without them the Error offers no Retry. Snapshot
  diagnostics before `capture.stop()`.
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
