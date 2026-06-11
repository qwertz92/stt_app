# Streaming Mode Implementation Notes

This document explains how streaming mode is implemented in this project, how it differs from batch mode, and when to prefer each mode.

## 1) Current implementation status

- `Batch` mode: stable default.
- `Streaming` mode: implemented for local provider (`faster-whisper` and
  Nemotron 3.5), AssemblyAI (Universal-Streaming v3 `StreamingClient`), and
  Deepgram (WebSocket API).
- Supported streaming engines are defined in `config.py` as `STREAMING_ENGINES`.
- OpenAI and Groq are batch-only in this app. ElevenLabs offers real-time STT publicly, but the current integration is still batch-only.

## 2) Batch vs Streaming: behavioral difference

### Batch mode

Flow:

1. Record full utterance until stop hotkey (or VAD auto-stop).
2. Run one transcription pass on the final WAV.
3. Insert final text once.

Characteristics:

- lower CPU load during recording,
- usually highest consistency for a final sentence,
- higher perceived latency (you get text at the end).

### Streaming mode (current local implementation)

Flow:

1. Start stream session in transcriber.
2. Audio callback pushes PCM chunks continuously.
3. Transcriber periodically re-transcribes only a trailing audio window and emits partial text.
4. Streaming text state reconciles rolling windows by safe word overlap.
5. Controller inserts only stable append-only text deltas at the current caret.
6. On stop hotkey, stream is finalized and only a safe remaining tail is appended.
7. If target focus changes during streaming, session auto-aborts and plays a short alert beep.

Implementation detail:

- focus guard uses a focus signature `(foreground_window, focused_child_control, caret_window)` captured at stream start.
- a 25ms poll timer checks for focus/cursor drift even between audio chunk callbacks.
- abort beep is triggered immediately when abort is requested; teardown continues right after.

Characteristics:

- live partial feedback and append-only live insertion during dictation,
- higher CPU usage than batch (periodic re-transcription),
- partial text can be revised in the overlay, but already inserted text is not
  rewritten.

## 3) Technical architecture in this repo

### Controller

- `DictationController.start_recording()` branches by `settings.mode`.
- For `streaming`, it calls `transcriber.start_stream(...)` and starts capture with a chunk callback.
- Partial updates are emitted via `transcription_partial` Qt signal, shown in
  overlay, and appended at caret only after text is stable enough.
- Local faster-whisper partials can come from a rolling audio window. The pure
  streaming text state only merges those windows when a safe word overlap is
  present, and still exposes only append-only insertions to the controller.
- Runtime stream/provider errors now fail fast: the controller stops capture, aborts the active stream, preserves the current recording for retry, and surfaces an error immediately instead of waiting for the user to press Stop.
- Live insertion is append-only. The controller may delay insertion until a
  stable prefix is observed, but it must not select, replace, or delete text
  already inserted into the target application.
- Stop action triggers `transcriber.stop_stream()` in background worker and
  only appends a remaining safe text delta.
- Focus-signature guard aborts streaming when target focus/cursor changes.
- Abort path uses fast stream cancellation (no heavy final re-transcription) to keep abort beep low-latency.

### AudioCapture

- `AudioCapture` now supports optional `chunk_callback`.
- Each incoming audio block is converted to PCM16 bytes and forwarded to the streaming transcriber.

### Local transcriber

- `LocalFasterWhisperTranscriber` now implements:
  - `start_stream(on_partial=...)`
  - `push_audio_chunk(chunk)`
  - `stop_stream()`
- Internally it uses:
  - queue + worker thread for stream chunk handling,
  - in-memory PCM16 buffer,
  - periodic partial re-transcription,
  - finalization on stop: by default only the trailing partial window is
    transcribed and merged into the accumulated live text; the full
    re-transcription of the whole recording is opt-in via the
    `streaming_full_final_transcript` setting (slower stop, highest-quality
    history entry).

`LocalNemotronTranscriber` uses a different local streaming contract:

- ONNX Runtime GenAI keeps the FastConformer/RNNT encoder state between chunks,
- each published INT4 ONNX chunk contains 8,960 samples, or 560 ms at 16 kHz,
- audio callback work remains lightweight because inference runs on a queue
  worker,
- tokens are emitted incrementally without repeatedly transcribing an older
  rolling audio window,
- the same cache-aware core processes imported/batch WAV files.

### AssemblyAI transcriber

- `AssemblyAITranscriber` implements streaming via the Universal-Streaming v3
  `assemblyai.streaming.v3.StreamingClient`; the legacy v2 realtime API was
  retired by AssemblyAI.
- `start_stream()` connects with the `universal-streaming-multilingual`
  model, language detection, and formatted turns.
- `push_audio_chunk()` enqueues raw PCM16 through `client.stream()` (the SDK
  sends from its own writer thread).
- Turn events carry the finalized words of one turn; text is keyed by
  `turn_order` because the formatted end-of-turn transcript arrives as a
  second event for the same turn.
- Accumulated text = all turns joined in order, delivered to `on_partial`.
- `stop_stream()` terminates the session (bounded join) and returns the full
  accumulated text; `abort_stream()` terminates and discards all text.

### Deepgram transcriber

- `DeepgramTranscriber` supports streaming via Deepgram's WebSocket `listen` endpoint.
- Audio chunks are queued by `push_audio_chunk` and sent as binary
  `linear16` from a dedicated sender thread, so the PortAudio callback never
  blocks on socket writes.
- With language `auto`, streaming uses `language=multi` (multilingual
  code-switching); the live API does not support `detect_language`.
- On stop, the sender queue is drained first, then the client sends
  `Finalize` and gives the socket a short quiet-period drain window before
  closing, reducing cases where the last final transcript tokens are lost.

## 4) Quality impact

Streaming does not necessarily mean lower final quality, but in this implementation:

- partial text may be less stable in the overlay,
- inserted text is append-only and is not revised in place,
- finalization uses the final transcript as source of truth when present and
  never deletes previously inserted target text,
- CPU contention on slower machines can indirectly affect responsiveness,
- final text quality is typically close to batch for the same model when enough context is captured.

## 5) Recommended default

Keep `Batch` as default unless live feedback is required.

Recommended:

- `Batch` for reliability and lower CPU.
- `Streaming` for interactive dictation UX where partial text visibility matters.

## 6) Models in streaming mode

Local rolling-window streaming supports the faster-whisper/CTranslate2 model choices:

- `tiny`, `base`, `small`, `medium`, `large-v3`, `large-v3-turbo`, `distil-large-v3.5`

Larger models in local streaming increase partial update cost.
Experimental ONNX/WebGPU local models are batch-only in this app.

Nemotron `nemotron-3.5-asr-streaming-0.6b-int4` provides true cache-aware local
streaming. Its current ONNX export is fixed to 560 ms latency. The app's normal
dependency lock currently provides CPU execution; DirectML is attempted when a
compatible runtime becomes available.

AssemblyAI streaming uses the Universal-Streaming multilingual model (the
batch model selection does not apply to streaming).
Deepgram streaming uses the selected Deepgram model.

## 7) Tuning points

Streaming behavior can be tuned via config:

- `STREAMING_PARTIAL_INTERVAL_S`
- `STREAMING_PARTIAL_MIN_AUDIO_S`
- `STREAMING_PARTIAL_WINDOW_S`
- `STREAMING_STABLE_WORD_GUARD`
- `STREAMING_REVISION_WORD_WINDOW`
- `STREAMING_FOCUS_POLL_MS`
- `STREAMING_ABORT_BEEP_HZ`
- `STREAMING_ABORT_BEEP_DURATION_MS`
- `STREAMING_OVERLAY_MAX_CHARS`
- `STREAMING_LIVE_INSERT_ENABLED`
- `STREAMING_ABORT_ON_FOCUS_CHANGE`
- `STREAMING_BEEP_ON_ABORT`

These defaults are set in `src/stt_app/config.py`.
