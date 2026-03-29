# Provider Cost and Quality Overview

This document compares pricing, free-tier availability, and quality signals for providers currently available in `stt_app`.

- Last verified: **2026-03-29**
- Prices and limits can change at any time. Confirm on official pricing pages before production use.

---

## 1) Price comparison (models used by this app)

| Engine | App mode(s) | Model(s) in app | Public price | Normalized cost |
|--------|-------------|-----------------|--------------|-----------------|
| Local (`faster-whisper`) | Batch + Streaming | `tiny`..`distil-large-v3.5` | No API fee | $0 API cost (hardware/power only) |
| AssemblyAI | Batch | Universal-3 Pro (primary), Universal-2 fallback | U3 Pro: $0.21/hour, U2: $0.15/hour | $0.15-$0.21/hour |
| AssemblyAI | Streaming | Universal Streaming | $0.15/hour | $0.15/hour |
| OpenAI | Batch | `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, `whisper-1` | Mini: est. $0.003/min, 4o: est. $0.006/min, Whisper: $0.006/min | $0.18/hour, $0.36/hour, $0.36/hour |
| Groq | Batch | `whisper-large-v3`, `whisper-large-v3-turbo` | v3: $0.111/hour, turbo: $0.040/hour | $0.111/hour, $0.040/hour |
| Deepgram | Batch | `nova-3` | Mono: $0.0043/min, Multi: $0.0052/min | $0.258/hour, $0.312/hour |
| Deepgram | Streaming | `nova-3` | Mono: $0.0077/min, Multi: $0.0092/min | $0.462/hour, $0.552/hour |
| ElevenLabs | Batch | `scribe_v2`, `scribe_v1` | Scribe v1/v2: $0.22/hour | $0.22/hour |

Notes:

- OpenAI `gpt-4o*` transcription is token-priced; minute values above are OpenAI estimates.
- In this app, Deepgram with `language_mode="auto"` uses `detect_language=true`; validate whether your account bills this as multilingual.
- ElevenLabs also offers `scribe_v2_realtime` publicly at $0.39/hour, but the current app integration remains batch-only.

---

## 2) Free tier and free credits

| Engine | Free tier status | Current free allocation (public) |
|--------|------------------|-----------------------------------|
| Local (`faster-whisper`) | Yes | Unlimited local usage after model download |
| AssemblyAI | Yes | Up to 185 hours pre-recorded or 333 hours streaming on trial |
| OpenAI | Limited / account-dependent | No standing free quota documented for transcription; `gpt-4o(-mini)-transcribe` are marked as not supported on free tier |
| Groq | Yes | Free plan with no card; speech model rate limits (for `whisper-large-v3` and `-turbo`) include 20 RPM, 2,000 requests/day, 7,200 audio-seconds/hour, 28,800 audio-seconds/day |
| Deepgram | Yes | $200 free credit, no credit card required |
| ElevenLabs | Yes | Free plan includes 2 hours 30 minutes of Speech to Text usage |

OpenAI caveat:

- OpenAI prepaid billing still references possible promotional/free credits on some accounts, but there is no fixed public "always-on" free STT quota.

---

## 3) Quality comparison (published signals)

No single apples-to-apples benchmark is maintained by all providers under identical settings. The table below shows the strongest public signals currently available.

| Provider | Models used in this app | Public quality signal | Interpretation |
|----------|--------------------------|------------------------|----------------|
| AssemblyAI | Universal-3 Pro / Universal-2 | AssemblyAI benchmark page reports U3 mean WER: **5.9 (EN)** / **8.7 (multilingual)** | Strong published accuracy, but vendor-run benchmark |
| OpenAI | `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, `whisper-1` | OpenAI reports `gpt-4o-transcribe` has lower WER than Whisper v2/v3 across FLEURS and competitive multilingual performance | Strong qualitative claim; OpenAI does not publish one global WER number per model on pricing page |
| Groq | `whisper-large-v3`, `whisper-large-v3-turbo` | Groq speech docs list WER: **10.3%** (v3) and **12%** (v3-turbo) | Useful baseline; values come from Groq model table |
| Deepgram | `nova-3` | Deepgram Nova-3 changelog reports median WER **5.26** (batch) and **6.84** (streaming) in its benchmark setup | Good signal for Nova-3; vendor-run benchmark |
| ElevenLabs | `scribe_v2`, `scribe_v1` | ElevenLabs positions Scribe v2 as its most accurate STT model and shows a vendor-run realtime comparison where Scribe v2 Realtime outperforms Gemini Flash 2.5, GPT-4o Mini, and Deepgram Nova 3 | Useful directional signal, but still vendor-run and not a published WER table |

---

## 4) Public benchmark links

These are useful if this document is not updated for a while:

1. Voice Writer STT leaderboard (cross-provider snapshot, includes OpenAI/AssemblyAI/Deepgram):  
   <https://voicewriter.io/speech-to-text-api-leaderboard/>
2. AssemblyAI benchmark hub (frequently updated vendor benchmark, many models/providers):  
   <https://www.assemblyai.com/benchmarks>
3. Deepgram Nova-3 benchmark notes and methodology context:  
   <https://developers.deepgram.com/changelog/speech-to-text-api-nova-3>
4. OpenAI audio model announcement and quality claims:  
   <https://openai.com/index/introducing-our-next-generation-audio-models/>

Recommendation:

- Use public benchmarks for shortlisting.
- Run a private bake-off on your own audio (your language mix, microphones, speaking style, and domain jargon matter more than leaderboard averages).

---

## 5) Billing behaviors that can surprise teams

### AssemblyAI

- Pricing is metered per second.
- Multi-channel audio is billed per second per channel.

### Groq

- Minimum billed length is 10 seconds per request.
- Very short clips can cost more than expected when called frequently.

### OpenAI

- Token-based pricing on `gpt-4o*` means effective per-minute cost can vary by transcript/token density.
- Paid usage requires prepaid credits (minimum top-up applies).

### Deepgram

- Different rates for streaming vs pre-recorded.
- Multi-channel audio can multiply billed duration.

### ElevenLabs

- `scribe_v2_realtime` is priced separately from batch transcription.
- Keyterm prompting adds `20%` cost, and entity detection adds `30%` cost.

---

## 6) Sources

- AssemblyAI pricing: <https://www.assemblyai.com/pricing>
- AssemblyAI benchmarks: <https://www.assemblyai.com/benchmarks>
- OpenAI pricing: <https://platform.openai.com/docs/pricing>
- OpenAI audio models announcement: <https://openai.com/index/introducing-our-next-generation-audio-models/>
- OpenAI model pages:  
  <https://platform.openai.com/docs/models/gpt-4o-transcribe>  
  <https://platform.openai.com/docs/models/gpt-4o-mini-transcribe>
- OpenAI prepaid billing help: <https://help.openai.com/en/articles/8264644-how-can-i-set-up-prepaid-billing>
- Groq speech-to-text docs: <https://console.groq.com/docs/speech-to-text>
- Groq rate limits: <https://console.groq.com/docs/rate-limits>
- Groq pricing: <https://console.groq.com/docs/pricing>
- Deepgram pricing: <https://deepgram.com/pricing>
- Deepgram Nova-3 changelog: <https://developers.deepgram.com/changelog/speech-to-text-api-nova-3>
- ElevenLabs STT overview: <https://elevenlabs.io/speech-to-text/>
- ElevenLabs model reference: <https://elevenlabs.io/docs/overview/models>
- ElevenLabs STT API reference: <https://elevenlabs.io/docs/api-reference/speech-to-text/convert>
- ElevenLabs API pricing: <https://elevenlabs.io/pricing/api/>
- Voice Writer STT leaderboard: <https://voicewriter.io/speech-to-text-api-leaderboard/>
