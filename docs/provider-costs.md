# Provider Cost and Quality Overview

This document compares pricing, free-tier availability, and quality signals for providers currently available in `stt_app`.

- Last verified: **2026-06-17**
- Prices and limits can change at any time. Confirm on official pricing pages before production use.

---

## 1) Price comparison (models used by this app)

| Engine | App mode(s) | Model(s) in app | Public price | Normalized cost |
|--------|-------------|-----------------|--------------|-----------------|
| Local (`faster-whisper`) | Batch + Streaming | `tiny`..`distil-large-v3.5` | No API fee | $0 API cost (hardware/power only) |
| Local experimental ONNX | Batch | `cohere-transcribe-03-2026`, `granite-4.0-1b-speech`, Granite Speech 4.1 variants | No API fee | $0 API cost (hardware/power only) |
| AssemblyAI | Batch | Universal-3 Pro (primary), Universal-2 fallback | U3 Pro: $0.21/hour, U2: $0.15/hour | $0.15-$0.21/hour |
| AssemblyAI | Streaming | Universal Streaming | $0.15/hour | $0.15/hour |
| OpenAI | Batch | `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, `whisper-1` | Mini: est. $0.003/min, 4o: est. $0.006/min, Whisper: $0.006/min | $0.18/hour, $0.36/hour, $0.36/hour |
| Groq | Batch | `whisper-large-v3`, `whisper-large-v3-turbo` | v3: $0.111/hour, turbo: $0.040/hour | $0.111/hour, $0.040/hour |
| Deepgram | Batch | `nova-3` | Mono: $0.0043/min, Multi: $0.0052/min | $0.258/hour, $0.312/hour |
| Deepgram | Streaming | `nova-3` | Mono: $0.0077/min, Multi: $0.0092/min | $0.462/hour, $0.552/hour |
| ElevenLabs | Batch | `scribe_v2`, `scribe_v1` | Scribe v1/v2: $0.22/hour | $0.22/hour |
| Azure LLM Speech | Batch | `mai-transcribe-1.5`, `mai-transcribe-1` | Fast transcription: $0.36/hour | $0.36/hour |
| Fun-ASR (Alibaba) | Batch | `fun-asr-realtime` | DashScope pay-as-you-go (per-second; see Model Studio pricing) | Pay-as-you-go after free trial |

Notes:

- OpenAI `gpt-4o*` transcription is token-priced; minute values above are OpenAI estimates.
- In this app, Deepgram with `language_mode="auto"` uses `detect_language=true`; validate whether your account bills this as multilingual.
- ElevenLabs also offers `scribe_v2_realtime` publicly at $0.39/hour, but the current app integration remains batch-only.
- Azure LLM Speech (enhanced mode, backed by the MAI-Transcribe models) is a synchronous file/"fast transcription" API and is **batch-only** in this app. It is in **public preview** (no SLA). It needs both a resource key *and* a per-resource endpoint, and the resource region must support LLM Speech.
- Fun-ASR (Alibaba) is driven over the DashScope **real-time WebSocket** API in a batch fashion (the batch file API requires an OSS public URL). Key-only (Singapore region). **No German support.** Confirm the current per-second rate on the Model Studio pricing page.

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
| Azure LLM Speech | Yes | Free (F0) tier: **5 audio hours/month** for speech to text (hard cap, not adjustable) |
| Fun-ASR (Alibaba) | Yes | DashScope free trial quota for new accounts (Singapore region; ~1M tokens per model, ~90 days), then pay-as-you-go |

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
| Azure LLM Speech | `mai-transcribe-1.5`, `mai-transcribe-1` | Microsoft reports MAI-Transcribe-1.5 at **2.4% WER** on Artificial Analysis (ranked #3 there, behind Alibaba Fun-Realtime-ASR and ElevenLabs Scribe v2) and **best-in-class FLEURS** accuracy across 42-43 languages, "leading the accuracy-speed Pareto frontier" | Top-tier accuracy with strong multilingual coverage. Note: it is *not* currently the #1 entry on the Hugging Face Open ASR Leaderboard (which is led by open models such as Granite Speech / Canary-Qwen / Cohere Transcribe). Parameter count is **not disclosed** by Microsoft |
| Fun-ASR (Alibaba) | `fun-asr-realtime` | The hosted **Fun-Realtime-ASR-preview** currently **tops the Artificial Analysis leaderboard at ~1.7% WER** (ahead of ElevenLabs Scribe v2 and MAI-Transcribe-1.5) | Best published accuracy of the integrated providers, but **no German**; strongest fit is Chinese (incl. dialects) and East/SE-Asian languages. See [funasr-and-fleurs-evaluation.md](funasr-and-fleurs-evaluation.md) |

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

### Azure LLM Speech (MAI-Transcribe)

- Currently in **public preview** — no SLA; behavior and pricing can change.
- Requires a Speech / Foundry resource in a region that supports LLM Speech,
  plus the per-resource endpoint (not just a key).
- The F0 free tier's 5 audio hours/month is a hard monthly cap that cannot be
  raised; beyond that, pay-as-you-go (Standard, S0) billing applies.
- This is a cloud-only model. There is **no local / ONNX runtime** for it, and
  Microsoft does not publish the model size (parameter count).

---

## 6) Hosted candidates not integrated

The table above only covers remote providers currently implemented in
`stt_app`. Local ONNX models are documented in `docs/models.md`; Cohere
Transcribe is available there as a local model, but the hosted Cohere API is not
implemented as a remote engine.

| Candidate | Public access signal | Pricing clarity | Local/offline fit | Current status |
|-----------|----------------------|-----------------|-------------------|----------------|
| Cohere hosted Transcribe API | Trial API access is publicly documented as available via normal Cohere account signup | Public transcription pricing is not explicit enough yet for a trustworthy cost comparison | Local/offline usage is covered by the integrated ONNX model, not by the hosted API | Hosted provider not integrated |
| Alibaba Fun-ASR — **local** weights | Open weights Apache-2.0 on HF/ModelScope | n/a (self-hosted) | 7.7B (too big) or 0.8B nano (no ONNX export, different runtime) | Local path not integrated; the **hosted** Fun-ASR is integrated as a remote engine. See [funasr-and-fleurs-evaluation.md](funasr-and-fleurs-evaluation.md) |

Recommendation:

- Revisit the hosted path if Cohere publishes explicit STT pricing and quotas.
- Benchmark the local ONNX path on the target machine before making it the
  default local model.

---

## 7) Sources

- AssemblyAI pricing: <https://www.assemblyai.com/pricing>
- AssemblyAI benchmarks: <https://www.assemblyai.com/benchmarks>
- Cohere models overview: <https://docs.cohere.com/docs/models>
- Cohere pricing: <https://cohere.com/pricing>
- Cohere pricing docs: <https://docs.cohere.com/docs/how-does-cohere-pricing-work>
- Cohere FAQs: <https://docs.cohere.com/v1/docs/cohere-faqs>
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
- Azure LLM Speech API: <https://learn.microsoft.com/azure/ai-services/speech-service/llm-speech>
- Azure MAI-Transcribe model: <https://learn.microsoft.com/azure/ai-services/speech-service/mai-transcribe>
- Azure Speech pricing: <https://azure.microsoft.com/pricing/details/speech/>
- MAI-Transcribe-1.5 announcement: <https://microsoft.ai/news/mai-transcribe-1-5more-accurate-context-aware-and-built-for-production/>
- Artificial Analysis speech-to-text leaderboard: <https://artificialanalysis.ai/speech-to-text>
- Voice Writer STT leaderboard: <https://voicewriter.io/speech-to-text-api-leaderboard/>
