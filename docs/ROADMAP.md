# Roadmap

Ideas kept for later. Open defects do not belong here: they are in
`docs/agents/known-limitations.md`. Every entry carries the date it was written.

## Future ideas (not planned)

Ideas that could be worth building once the current features are finished, but not
now. Each says what it would give the user and why it waits. Building one means
taking it out of this list; deleting one needs no ceremony.

- **A read receipt that works inside Chrome and Edge** (2026-11-25). The receipt
  that tells a paste nobody fetched from one that arrived
  (`docs/agents/text-insertion.md`, "A restoring paste has a read receipt") says
  nothing in those two browsers: something fetches the clipboard text 4-8 ms after
  every Ctrl+V, with the focus in a text field or on a button alike, so a paste that
  missed still reads as fetched. Gain: "not inserted" would be reported for a miss
  on a web page too, where today only the caret check can call it doubtful. Why it
  waits: the one approach tried (refuse the first fetch, serve the second) works in
  Edge, half the time in Chrome, and empties every text-field paste in Electron;
  his log of 2026-10-24..11-24 has 1018 of 1040 pastes in `Chrome_WidgetWin_1`
  windows, which Electron apps share with the browsers, so the split between the
  two is unknown. Worth another look when a browser exposes which element took the
  paste (UI Automation's focused element after the keystroke is the untried road).

- **Remove a kept copy together with its History entry** (2026-11-25). The
  `kept_*.wav` audio of a gap or fallback transcript is never deleted
  automatically (`docs/agents/known-limitations.md`, "Kept copies are never
  deleted automatically"), so the recordings folder grows. Deleting a History
  entry, or clearing History, could offer to delete the kept copy it links.
  Gain: the folder shrinks with the user's own decision about the transcript,
  never behind it. Why it waits: the owner's rule is that no not fully
  transcribed audio is deleted unasked, and the offer needs a dialog in both
  History views.

- **Vocabulary profiles** (2026-09-27). Several named custom-vocabulary sets
  (for example "work", "private", "programming") with a quick switch, instead of the
  one list on the Transcription tab. Gain: domain terms without a list so long that
  it biases every dictation. Why it waits: one list covers today's use, the
  providers cap or ignore vocabulary differently (`supports_custom_vocabulary`), and
  a switch needs a place in the overlay, which is already dense.
- **Voxtral as a local model** (2026-09-27). Mistral publishes open weights under
  Apache-2.0 (Voxtral Mini 4B Realtime, Voxtral Small; docs.mistral.ai models
  overview), and community ONNX exports exist (not tested here). Gain: a local
  model from a European vendor with native streaming. Why it waits: the model card
  of `mistralai/Voxtral-Mini-4B-Realtime-2602` gives 4B parameters (a 0.6B encoder
  and a 3.4B decoder) and asks for a GPU with at least 16 GB for its BF16 weights,
  against 0.6B for Parakeet on a plain CPU; a quantized export's CPU speed is
  unmeasured, and Parakeet and Granite Speech 5.0 already cover fast local
  transcription. Measure one export through the Node runner before deciding.
- **Mistral realtime streaming** (2026-09-27). `voxtral-mini-transcribe-realtime-2602`
  at $0.006 per minute (mistral.ai/pricing/api, read that day) would give the
  Mistral engine live text like AssemblyAI and Deepgram. Why it waits: the batch
  engine comes first, and a streaming provider is a second WebSocket client with its
  own session and teardown rules.
- **Meta Muse Voice streaming** (2026-11-26). `wss://api.meta.ai/v1/asr/realtime` would give the
  Meta engine live text. Protocol, from https://dev.meta.ai/docs/speech-to-text: the first text
  frame, within 10 s of connecting, is a JSON handshake (`audioEncoding` `PCM_16KHZ` or
  `PCM_24KHZ`, `model`, `mode`, `partialMode`, `languageBias`, `keywords`, and the key as
  `authorization.accessToken` with the `Bearer` prefix; the HTTP `Authorization` header is
  ignored); the server acknowledges with `{"sessionId": ...}` (no `type` field); then raw signed
  16-bit little-endian mono PCM in binary frames, paced near real time and never more than 5 s
  ahead; events `speechStart`, `transcript`, `speechEnd`, `speechComplete`, `audioProgress`,
  `error`; the client ends with `{"type": "endStream"}` and reads until the server closes with
  code 1000. Sessions last up to 60 minutes. Price is the batch price, $0.18 per hour. Effort:
  medium (about the size of the Deepgram streaming client: a WebSocket thread, the handshake,
  partial/final merging into the app's append-only insertion, teardown). Why it waits: none of it
  can be checked without a key, and the batch engine comes first.
- **A GPU path for Granite Speech 5.0** (2026-09-27, measured 2026-09-19). DirectML
  runs the FP32 graph in 0.107 s against 0.28 s for the INT8 graph on the CPU, for a
  29.4 s clip. Gain: about 0.2 s per half-minute dictation. Why it waits: it needs a
  second download of 0.95-1.89 GB, a raw-graph Node runtime and a second feature
  extractor in JavaScript, for a gain nobody notices at dictation length.
- **A Linux port, Hyprland first** (2026-10-03). Run the app on the owner's NixOS notebook
  (Hyprland, a Wayland compositor with no desktop environment), later on GNOME/KDE and X11. Gain:
  the same dictation on the machine used most. Why it waits: a Wayland app cannot grab a global key
  or type into another window by itself, so the hotkey becomes a line in the Hyprland config that
  runs `stt-app ctl toggle`, and typing goes through `wtype`; both are verifiable only on a real
  Hyprland. Estimate about 63-87 agent-hours for the MVP (groundwork plus Hyprland), 135-205 for
  everything. Inventory, per-feature options, architecture, phases and risks are in
  [`docs/linux-port.md`](linux-port.md).
- **A cheaper Settings dialog construction** (2026-10-01). Building one
  `SettingsDialog` takes about 0.19 s in the test suite (profile of
  `test_settings_dialog_mode.py`: 33 of 43 s inside `SettingsDialog.__init__`,
  mostly `addWidget` and `addTab`) and 949 ms in the running app
  (`settings_timing event=dialog_init`, 2026-10-01 log, populated settings and
  model inventory); the suite builds it about 420 times, roughly 80 of its
  245 s. Gain: a test suite about a third faster. Why it waits: the app builds
  the dialog in the background at startup, so opening Settings does not wait for
  it, and the only real gain is test time; building tabs lazily changes when
  widgets exist, which the save/merge baseline and the unsaved-changes
  fingerprint rely on. A cProfile around `SettingsDialog(...)` alone would size
  what is achievable.
