# Roadmap

Ideas kept for later. Open defects do not belong here: they are in
`docs/agents/known-limitations.md`. Every entry carries the date it was written.

## Future ideas (not planned)

Ideas that could be worth building once the current features are finished, but not
now. Each says what it would give the user and why it waits. Building one means
taking it out of this list; deleting one needs no ceremony.

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
- **A GPU path for Granite Speech 5.0** (2026-09-27, measured 2026-09-19). DirectML
  runs the FP32 graph in 0.107 s against 0.28 s for the INT8 graph on the CPU, for a
  29.4 s clip. Gain: about 0.2 s per half-minute dictation. Why it waits: it needs a
  second download of 0.95-1.89 GB, a raw-graph Node runtime and a second feature
  extractor in JavaScript, for a gain nobody notices at dictation length.
