# Streaming Mode Implementation Notes

This document explains how streaming mode is implemented in this project, how it differs from batch mode, and when to prefer each mode.

## 1) Current implementation status

- `Batch` mode: stable default.
- `Streaming` mode: implemented for local provider (`faster-whisper`) as experimental.
- Remote provider streaming remains placeholder (not implemented yet).

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
4. Controller inserts incremental text deltas at the current caret during streaming.
5. On stop hotkey, stream is finalized and only the remaining tail (if any) is inserted.
6. If target focus changes during streaming, session auto-aborts and plays a short alert beep.

Implementation detail:
- focus guard uses a focus signature `(foreground_window, focused_child_control, caret_window)` captured at stream start.
- a 25ms poll timer checks for focus/cursor drift even between audio chunk callbacks.
- abort beep is triggered immediately when abort is requested; teardown continues right after.

Characteristics:
- live partial feedback and live insertion during dictation,
- higher CPU usage than batch (periodic re-transcription),
- partial text can be revised as more context arrives.

## 3) Technical architecture in this repo

### Controller

- `DictationController.start_recording()` branches by `settings.mode`.
- For `streaming`, it calls `transcriber.start_stream(...)` and starts capture with a chunk callback.
- Partial updates are emitted via `transcription_partial` Qt signal, shown in overlay, and incrementally inserted at caret.
- Live insertion uses a stable-prefix rule with a trailing-word guard before committing text; suffix/prefix overlap reconciliation is used when partials diverge.
- Stop action triggers `transcriber.stop_stream()` in background worker and only inserts remaining text delta.
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
  - final transcription on stop.

## 4) Why this design (and alternatives)

Implemented design is pragmatic for MVP:
- minimal external dependencies,
- reuses existing local `faster-whisper` pipeline,
- keeps module boundaries testable.

Alternatives (future):
1. Sliding-window streaming decoder with token-level stitching.
2. Provider-native streaming APIs (OpenAI/Azure/Deepgram).
3. VAD-segmented incremental commit strategy.
4. Hybrid: low-latency partial model + high-accuracy final model.

## 5) Quality impact

Streaming does not necessarily mean lower final quality, but in this implementation:
- partial text may be less stable (revisions happen),
- if revisions differ from already live-inserted text, only append-only reconciliation is possible (already inserted words are not deleted),
- finalize tail uses last partial as fallback when final pass diverges from already committed live text,
- CPU contention on slower machines can indirectly affect responsiveness,
- final text quality is typically close to batch for the same model when enough context is captured.

## 6) Recommended default

Keep `Batch` as default unless live feedback is required.

Recommended:
- `Batch` for reliability and lower CPU.
- `Streaming` for interactive dictation UX where partial text visibility matters.

## 7) Models in streaming mode

Streaming mode uses the same local model choices as batch mode:
- `tiny`, `base`, `small`, `medium`, `large-v3`, `large-v3-turbo`, `distil-large-v3.5`

Larger models in streaming mode increase partial update cost.

## 8) Tuning points

Streaming behavior can be tuned via config:
- `STREAMING_PARTIAL_INTERVAL_S`
- `STREAMING_PARTIAL_MIN_AUDIO_S`
- `STREAMING_PARTIAL_WINDOW_S`
- `STREAMING_STABLE_WORD_GUARD`
- `STREAMING_FOCUS_POLL_MS`
- `STREAMING_ABORT_BEEP_HZ`
- `STREAMING_ABORT_BEEP_DURATION_MS`
- `STREAMING_OVERLAY_MAX_CHARS`
- `STREAMING_LIVE_INSERT_ENABLED`
- `STREAMING_ABORT_ON_FOCUS_CHANGE`
- `STREAMING_BEEP_ON_ABORT`

These defaults are set in `src/tts_app/config.py`.
