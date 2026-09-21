# Provider Cost and Quality Overview

This document compares pricing, free-tier availability, and quality signals for providers currently available in `stt_app`.

- Pricing and model availability last verified: **2026-09-21**
- Prices and limits can change at any time. Confirm on official pricing pages before production use.
- Every price and limit below was re-read from the vendor's own page on the
  verification date unless marked otherwise; the link is next to the figure
  and again in [Sources](#7-sources).

---

## 1) Price comparison (models used by this app)

| Engine | App mode(s) | Model(s) in app | Public price | Normalized cost |
|--------|-------------|-----------------|--------------|-----------------|
| Local (`faster-whisper`, Parakeet, Canary, Cohere, Granite, Nemotron) | Batch + Streaming (model-dependent) | see [models.md](models.md) | No API fee | $0 API cost (hardware/power only) |
| AssemblyAI | Batch | Universal-3.5 Pro or Universal-2 (explicit selection; no fallback) | U3.5 Pro: $0.21/hour, U2: $0.15/hour ([pricing](https://www.assemblyai.com/pricing), checked 2026-09-21) | $0.15-$0.21/hour |
| AssemblyAI | Streaming | Universal-3.5 Pro Realtime | $0.45/hour (same source) | $0.45/hour |
| OpenAI | Batch | `gpt-transcribe` (new default; replaces `gpt-4o-mini-transcribe`), `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `whisper-1` | `gpt-transcribe`: $0.0045/min, `gpt-4o-transcribe`: $0.006/min, `gpt-4o-mini-transcribe`: $0.003/min, `whisper-1`: $0.006/min ([pricing](https://developers.openai.com/api/docs/pricing), checked 2026-09-21) | $0.27/hour, $0.36/hour, $0.18/hour, $0.36/hour |
| Groq | Batch | `whisper-large-v3`, `whisper-large-v3-turbo` | v3: $0.111/hour, turbo: $0.040/hour ([pricing](https://groq.com/pricing), checked 2026-09-21) | $0.111/hour, $0.040/hour |
| Deepgram | Batch | `nova-3` | Mono: $0.0043/min, Multi: $0.0052/min ([pricing](https://deepgram.com/pricing), checked 2026-09-21) | $0.258/hour, $0.312/hour |
| Deepgram | Streaming | `nova-3` | Mono: $0.0048/min, Multi: $0.0058/min — **limited-time promotional rate**; the regular, non-promotional rate shown alongside it is Mono $0.0077/min, Multi $0.0092/min (same source) | $0.288/hour, $0.348/hour (promo); $0.462/hour, $0.552/hour (regular) |
| ElevenLabs | Batch | `scribe_v2` | $3.67 per 1,000 minutes on the [Artificial Analysis leaderboard](https://artificialanalysis.ai/speech-to-text) (read 2026-09-21) — consistent with the pay-as-you-go credit cost below | $0.22/hour |
| Azure LLM Speech | Batch | `mai-transcribe-2`, `mai-transcribe-1.5`, `mai-transcribe-1` (deprecated) | MAI-Transcribe-2: $0.10/hour "as a limited-time offer until the end of the year" ([announcement](https://microsoft.ai/news/mai-transcribe-2-is-the-fastest-most-accurate-and-cheapest-speech-recognition-model-in-the-world/), 2026, price afterwards not announced); MAI-Transcribe-1.5: $0.36/hour | $0.10/hour (2) / $0.36/hour (1.5) |
| Fun-ASR (Alibaba) | Batch | `fun-asr-realtime` | $0.000047/second, Beijing region rate — the only region this model's own pricing page lists ([fun-asr-realtime pricing](https://www.alibabacloud.com/help/en/model-studio/fun-asr-realtime), checked 2026-09-21) | ~$0.169/hour |

Notes:

- **OpenAI is changing its default transcription model to `gpt-transcribe`** (this
  document assumes that change has shipped; see the
  [OpenAI section](#openai) below for the deprecation timeline of the other
  three ids). `gpt-transcribe` and `gpt-4o*` transcription is priced per
  minute of audio on the pricing page above, not per token as earlier
  `gpt-4o*` documentation implied.
- **Deepgram's streaming price is currently a promotion, not the list price.**
  Deepgram's own pricing page shows the promotional rate next to a
  crossed-out regular rate and does not say when the promotion ends. Budget
  for the regular rate if this matters for a production decision.
- **The Fun-ASR price above replaces an earlier, incorrect figure.** The app
  sends the literal model id `fun-asr-realtime` in its API request (see
  `src/stt_app/transcriber/funasr_provider.py`), and that model's own pricing
  page states $0.000047/second for the Beijing region only; no separate price
  is published for the Singapore/international endpoint the app actually
  calls (`FUNASR_WS_URL_INTL`), only a separate, higher rate limit (1,200
  RPM). Alibaba's page for the plain `fun-asr` (non-realtime) model instead
  lists $0.000032/second (Beijing) and $0.000035/second (Singapore) — a
  different, non-streaming model this app does not use. Treat the
  Fun-ASR-realtime hourly figure as the best available estimate, not a
  confirmed Singapore-region price.
- In this app, Deepgram with `language_mode="auto"` uses `detect_language=true`; validate whether your account bills this as multilingual.
- ElevenLabs also offers `scribe_v2_realtime` publicly, but the current app integration remains batch-only.
- Azure LLM Speech (enhanced mode, backed by the MAI-Transcribe models) is a synchronous file/"fast transcription" API and is **batch-only** in this app. It is in **public preview** (no SLA). It needs both a resource key *and* a per-resource endpoint, and the resource region must support LLM Speech.
- Fun-ASR (Alibaba) is driven over the DashScope **real-time WebSocket** API in a batch fashion (the batch file API requires an OSS public URL). Key-only (Singapore region). **No German support.**

---

## 2) Free tier and free credits

All figures below were re-read from the vendor's own pricing page on
2026-09-21; two entries **correct figures this document carried before**
(ElevenLabs, Azure — see the notes under the table).

| Engine | Free tier status | Current free allocation (public) |
|--------|------------------|-----------------------------------|
| Local (all local models) | Yes | Unlimited local usage after model download |
| AssemblyAI | Yes | $50 in free credits on signup, no credit card required ([pricing](https://www.assemblyai.com/pricing)) |
| OpenAI | Limited / account-dependent | No standing free quota documented for transcription on the [pricing page](https://developers.openai.com/api/docs/pricing) |
| Groq | Yes | Free plan; speech model rate limits (for `whisper-large-v3` and `-turbo`) are 20 RPM, 2,000 requests/day, 7,200 audio-seconds/hour, 28,800 audio-seconds/day ([rate limits](https://console.groq.com/docs/rate-limits)) |
| Deepgram | Yes | $200 free credit, no credit card required ([pricing](https://deepgram.com/pricing)) |
| ElevenLabs | Yes | **Corrected 2026-09-21**: the Free plan gives 10,000 credits/month, and Speech to Text costs 330 credits per minute of audio — about **30 minutes/month**, not the "2 hours 30 minutes" this document said before ([pricing](https://elevenlabs.io/pricing)) |
| Azure LLM Speech | **No** | **Corrected 2026-09-21**: Microsoft's own quotas page lists "Not applicable" in the Free (F0) column for every LLM-speech and fast-transcription row — the F0 tier does not cover the API this app uses at all. A Standard (S0) resource is required from the start; see [Azure LLM Speech Setup](azure-llm-speech.md#cost-and-free-tier) |
| Fun-ASR (Alibaba) | Likely yes, amount **not verified** | Alibaba grants new users a free quota per model, valid 90 days, and only for models in the Singapore region with the International deployment scope, which is the endpoint this app uses ([free-quota page](https://www.alibabacloud.com/help/en/model-studio/new-free-quota), read 2026-09-21); that page does not state the amount for Fun-ASR specifically. The "36,000 audio seconds (10 hours)" this document stated before could not be re-confirmed this session; check the Model Studio console for the current figure before relying on it |

OpenAI caveat:

- OpenAI prepaid billing still references possible promotional/free credits on some accounts, but there is no fixed public "always-on" free STT quota.

### Recommendation for light personal use

For someone dictating a little every day, the free options are worth using
before any paid one:

- **A local model costs nothing per use, ever**, and several of the app's
  local models (Parakeet, the Whisper sizes, Granite Speech 5.0 for English)
  are fast enough on an ordinary CPU that there is no speed reason to pay for
  a cloud engine. See [models.md](models.md) for which one fits your
  language and hardware.
- **Groq's free plan** (20 requests/minute, 7,200 audio-seconds/hour, no
  stated card requirement found this session) covers a normal dictation
  workload without ever touching a paid tier — 7,200 audio-seconds/hour is
  two hours of audio per hour of wall-clock time, far more than one person
  dictates.
- If a cloud engine's free allowance runs out and a paid rate is wanted,
  **MAI-Transcribe-2 at $0.10/hour** and **Groq's paid `whisper-large-v3-turbo`
  at $0.040/hour** are the two cheapest per-hour rates in the table above —
  note that MAI-Transcribe-2's price is explicitly a limited-time offer, and
  Deepgram's streaming price above is a promotion too, so re-check before
  budgeting on either past 2026.
- Free plans, free credits, and promotional prices are the parts of this
  document most likely to be stale by the time you read it. As read on
  2026-09-21, only two of them renew: Groq's free plan (limits per minute,
  hour and day) and ElevenLabs' monthly credits. AssemblyAI's $50 and
  Deepgram's $200 are one-time signup credits, Azure's is a 30-day account
  credit, and a "limited-time" price ends when the vendor says so.

---

## 3) Quality comparison (published signals)

No single apples-to-apples benchmark is maintained by all providers under identical settings. The table below shows the strongest public signals currently available, plus a fresh read of the [Artificial Analysis Speech-to-Text leaderboard](https://artificialanalysis.ai/speech-to-text) (checked 2026-09-21). **The Artificial Analysis figures are English-only, the leaderboard order moves as new models are added, and the site was read once on the date given — treat the ranks as a snapshot, not a fact that stays true.**

| Provider | Models used in this app | Public quality signal | Interpretation |
|----------|--------------------------|------------------------|----------------|
| AssemblyAI | Universal-3.5 Pro / Universal-2 | AssemblyAI reports Universal-3.5 Pro as its current 18-language flagship for async and realtime, with native code switching. On Artificial Analysis (checked 2026-09-21), "AssemblyAI Universal-3 Pro" reads 3.1% WER at $3.50 per 1,000 minutes | Strong current multilingual option; validate on your own audio |
| OpenAI | `gpt-transcribe` (new default), `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `whisper-1` | OpenAI reports `gpt-4o-transcribe` has lower WER than Whisper v2/v3 across FLEURS and competitive multilingual performance. On Artificial Analysis (checked 2026-09-21), "OpenAI GPT-4o Transcribe" reads 4.0% WER at $6.00 per 1,000 minutes; `gpt-transcribe` was not listed there under that name | Mid-table on the third-party leaderboard; OpenAI does not publish one global WER number per model on its own pricing page |
| Groq | `whisper-large-v3`, `whisper-large-v3-turbo` | Groq speech docs list WER: **10.3%** (v3) and **12%** (v3-turbo). On Artificial Analysis (checked 2026-09-21), "Groq Whisper Large v3 Turbo" reads 4.6% WER at $0.67 per 1,000 minutes — a different (lower) WER than Groq's own docs, evidence the two are not measured the same way | Cheapest per-hour cloud option in this app either way; useful baseline, not a single agreed number |
| Deepgram | `nova-3` | Deepgram Nova-3 changelog reports median WER **5.26** (batch) and **6.84** (streaming) in its benchmark setup. On Artificial Analysis (checked 2026-09-21), "Deepgram Nova-3" reads 5.2% WER at $4.30 per 1,000 minutes | Good signal for Nova-3; vendor-run benchmark, corroborated within about a point by a third party |
| ElevenLabs | `scribe_v2` | ElevenLabs positions Scribe v2 as its most accurate STT model. On Artificial Analysis (checked 2026-09-21), "Scribe v2" reads 2.2% WER at $3.67 per 1,000 minutes, ranked 4th of the models read that day | One of the strongest accuracy results among this app's cloud providers, on a third-party leaderboard as well as vendor claims |
| Azure LLM Speech | `mai-transcribe-2`, `mai-transcribe-1.5`, `mai-transcribe-1` (deprecated) | Microsoft's announcement of MAI-Transcribe-2 (2026-09-03) says it "ranks second on the Artificial Analysis Word-Error-Rate leaderboard" (an English benchmark). Read fresh on 2026-09-21, Artificial Analysis places "MAI-Transcribe-2" 3rd at 2.0% WER / $1.67 per 1,000 minutes (behind "Fun-Realtime-ASR-preview" and "StepAudio 3 ASR", both 1.7%), and "MAI-Transcribe-1.5" further down at 2.4% WER / $6.00 per 1,000 minutes | Top-tier accuracy **and** currently the cheapest of the highly-ranked models on that leaderboard (see the price comparison in section 1). The rank moved between Microsoft's announcement and this reading, which is the "order moves" caveat in practice. This app has not measured either model itself (no Azure resource was available). Parameter count is **not disclosed** by Microsoft |
| Fun-ASR (Alibaba) | `fun-asr-realtime` | Read fresh on 2026-09-21, Artificial Analysis places "Fun-Realtime-ASR-preview" 1st at 1.7% WER (price not listed on the leaderboard for this entry) | Best published accuracy among the app's integrated providers, but **no German**; strongest fit is Chinese (incl. dialects) and East/SE-Asian languages. See [funasr-and-fleurs-evaluation.md](funasr-and-fleurs-evaluation.md) |

### Independent benchmarks

- **[Artificial Analysis Speech-to-Text leaderboard](https://artificialanalysis.ai/speech-to-text)** — the cross-vendor
  accuracy/price leaderboard cited throughout this document. English-only WER
  figures, re-run periodically by Artificial Analysis on its own test set; the
  numbers above are a single reading taken 2026-09-21 and will drift.
- **[Hugging Face Open ASR Leaderboard](https://huggingface.co/spaces/hf-audio/open_asr_leaderboard)**
  — the leaderboard `docs/models.md` and `docs/local-asr-model-candidates-2026.md`
  cite for the app's own local GPU/ONNX models (Cohere Transcribe, Granite
  Speech, Parakeet, Canary). Its methodology paper is
  [arXiv:2510.06961](https://arxiv.org/abs/2510.06961). The leaderboard added a
  multilingual track in late 2025 covering a handful of languages; this
  document could not confirm this session whether German is one of them or
  read current per-model German WER figures from it — the leaderboard is an
  interactive page that does not render through an automated fetch, so check
  it directly rather than trust a number repeated here.
- **No vendor-neutral German-specific leaderboard was found and confirmed
  this session.** The two leaderboards above are the best available
  cross-vendor signals, and both are English-first; every German-specific
  number in this document (FLEURS figures in [models.md](models.md), vendor
  claims in the table above) comes from a single vendor's own report.
- Voice Writer STT leaderboard (cross-provider snapshot, includes OpenAI/AssemblyAI/Deepgram):  
  <https://voicewriter.io/speech-to-text-api-leaderboard/>
- AssemblyAI benchmark hub (frequently updated vendor benchmark, many models/providers):  
  <https://www.assemblyai.com/benchmarks>
- Deepgram Nova-3 benchmark notes and methodology context:  
  <https://developers.deepgram.com/changelog/speech-to-text-api-nova-3>
- OpenAI audio model announcement and quality claims (predates `gpt-transcribe`):  
  <https://openai.com/index/introducing-our-next-generation-audio-models/>

Recommendation:

- Use public benchmarks for shortlisting.
- Run a private bake-off on your own audio (your language mix, microphones, speaking style, and domain jargon matter more than leaderboard averages).

---

## 4) Billing behaviors that can surprise teams

### AssemblyAI

- Pricing is metered per second.
- Multi-channel audio is billed per second per channel.

### Groq

- Minimum billed length is 10 seconds per request.
- Very short clips can cost more than expected when called frequently.

### OpenAI

- `gpt-transcribe`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, and `whisper-1`
  are all billed per minute of audio on the current
  [pricing page](https://developers.openai.com/api/docs/pricing) (checked
  2026-09-21), not per token as earlier `gpt-4o*` documentation suggested.
- OpenAI announced on 2026-08-26 that `whisper-1`, `gpt-4o-transcribe`,
  `gpt-4o-mini-transcribe`, and `gpt-4o-transcribe-diarize` are deprecated and
  will be removed from the API on **2027-02-26**, replaced by
  `gpt-transcribe` (and `gpt-live-transcribe` for realtime use) — see the
  [deprecations page](https://developers.openai.com/api/docs/deprecations).
  The three older ids stay selectable in this app's Settings until then.
- Paid usage requires prepaid credits (minimum top-up applies).

### Deepgram

- Different rates for streaming vs pre-recorded.
- Multi-channel audio can multiply billed duration.

### ElevenLabs

- `scribe_v2_realtime` is priced separately from batch transcription.
- Keyterm prompting adds `20%` cost, and entity detection adds `30%` cost.

### Azure LLM Speech (MAI-Transcribe)

- Currently in **public preview** — no SLA; behavior and pricing can change.
- "Azure LLM Speech" is the service and MAI-Transcribe the model behind it:
  one engine in this app. Microsoft offers no MAI API outside Azure.
- MAI-Transcribe-2 (the app's default since 2026-09-19): Microsoft's
  announcement of 2026-09-03 prices it at "$0.10 per hour as a limited-time
  offer until the end of the year"; what it costs from 2027 on is not
  announced. MAI-Transcribe-1.5 stays at $0.36/hour, i.e. 3.6 times the
  offer price. MAI-Transcribe-1 was deprecated by Microsoft on 2026-08-20.
- Requires a Speech / Foundry resource in a region that supports LLM Speech
  with MAI-Transcribe -- six when read on 2026-09-19 (`centralindia`,
  `eastus`, `northeurope`, `southeastasia`, `westus`, `westus2`), of which
  `northeurope` (Ireland) is the only one in Europe -- plus the per-resource
  endpoint (not just a key).
- **Not verified against the live service**: no Azure resource was available
  when the integration was written or when MAI-Transcribe-2 was added. The
  request follows Microsoft's documented contract.
- **Corrected 2026-09-21: there is no Free (F0) tier for this feature.**
  Microsoft's [quotas and limits page](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits)
  (`ms.date` 2026-09-09) lists "Not applicable" in the Free (F0) column for
  every row of "LLM speech quotas and limits" and "Fast transcription" — the
  engine this app uses needs a **Standard (S0)** resource from the start.
  This document previously said F0 gives "5 audio hours/month"; that was
  wrong for this API (it may describe a different, older Speech feature) and
  has been removed. A general new-account Azure credit (currently $200 for
  30 days, <https://azure.microsoft.com/free/>) can cover early testing on an
  S0 resource, but it is a whole-account credit, not a Speech-specific free
  tier.
- **Two Microsoft pages disagree on the audio limit, and both are current as
  of 2026-09-21**: the quotas page above and the
  [LLM Speech how-to page](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/llm-speech)
  (`updated_at` 2026-06-05) both say the audio must be **"< 500 MB"** and
  **"< 5 hours per file"**; the
  [REST reference for the `transcribe` endpoint](https://learn.microsoft.com/rest/api/speechtotext/transcriptions/transcribe)
  (`updated_at` 2025-10-30) instead documents the `audio` parameter itself as
  "shorter than 2 hours in audio duration and smaller than 250 MB in size."
  This document cannot resolve which one the service actually enforces
  without a live Azure resource; treat 250 MB / 2 hours as the safer planning
  number until someone tests the larger one against the real API.
- This is a cloud-only model. There is **no local / ONNX runtime** for it, and
  Microsoft does not publish the model size (parameter count).

---

## 5) Hosted candidates not integrated

The table above only covers remote providers currently implemented in
`stt_app`. Local ONNX models are documented in `docs/models.md`; Cohere
Transcribe is available there as a local model, but the hosted Cohere API is not
implemented as a remote engine.

| Candidate | Public access signal | Pricing clarity | Local/offline fit | Current status |
|-----------|----------------------|-----------------|-------------------|----------------|
| Cohere hosted Transcribe API | Trial API access is publicly documented as available via normal Cohere account signup | Public transcription pricing is not explicit enough yet for a trustworthy cost comparison | Local/offline usage is covered by the integrated ONNX model, not by the hosted API | Hosted provider not integrated |
| Alibaba Fun-ASR — **local** weights | Open weights Apache-2.0 on HF/ModelScope | n/a (self-hosted) | 7.7B (too big) or 0.8B nano (no ONNX export, different runtime) | Local path not integrated; the **hosted** Fun-ASR is integrated as a remote engine. See [funasr-and-fleurs-evaluation.md](funasr-and-fleurs-evaluation.md) |
| Soniox API | Hosted, key-based, publicly documented | Read on Artificial Analysis (checked 2026-09-21): "Soniox v5 Async" at 3.8% WER / $1.66 per 1,000 minutes | Not local; would be a new remote engine | Not integrated — see the note below |
| Mistral Voxtral API | Hosted, key-based. Voxtral Small/Mini also ship as open weights (Apache 2.0), but no ONNX or CTranslate2 export was found this session, so they would need a new local runtime, not the app's existing ones | Read on Artificial Analysis (checked 2026-09-21): "Voxtral Small" at 2.8% WER / $4.00 per 1,000 minutes | Open weights exist but no ONNX path; not a drop-in for this app's local runtimes | Not integrated — see the note below |

**Why Soniox and Voxtral were not added as remote providers now**: on the
same Artificial Analysis reading (2026-09-21), neither is *both* more
accurate and cheaper than what the app already offers. Soniox v5 Async is
cheaper than ElevenLabs Scribe v2 ($1.66 vs. $3.67 per 1,000 minutes) but
less accurate (3.8% vs. 2.2% WER); it is also more expensive and less
accurate than Azure's MAI-Transcribe-2 (2.0% WER, $1.67 per 1,000 minutes).
Voxtral Small is both more expensive and less accurate than MAI-Transcribe-2
and Scribe v2 alike. This app's own leaders on that leaderboard —
MAI-Transcribe-2 on price and near the top on accuracy, ElevenLabs Scribe v2
on accuracy — are therefore not beaten by either candidate on the date this
was checked. Re-check before deciding differently; both prices and rankings
move.

Recommendation:

- Revisit the hosted path if Cohere publishes explicit STT pricing and quotas.
- Benchmark the local ONNX path on the target machine before making it the
  default local model.

---

## 6) Recurring free-tier services not integrated

Verified against each vendor's own page (checked 2026-09-21), for context —
none of these are wired into `stt_app`:

- **Speechmatics** — $100 in free credit to start, no credit card required
  ([pricing](https://www.speechmatics.com/pricing)). The vendor's own page
  states a credit amount, not a fixed minutes/month allowance (some
  third-party aggregators describe a monthly minute cap instead; this
  document uses the vendor's own wording).
- **Cloudflare Workers AI (Whisper)** — 10,000 free "Neurons" per day, reset
  at 00:00 UTC; `@cf/openai/whisper` and `@cf/openai/whisper-large-v3-turbo`
  both cost about $0.0005 per audio minute in Neurons, so the free daily
  allowance covers roughly 200+ minutes of transcription per day
  ([pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)).
- **Google Cloud Speech-to-Text** — 60 minutes of audio free every month, on
  an ongoing basis, plus a separate one-time $300 new-customer credit
  ([pricing](https://cloud.google.com/speech-to-text/pricing)).

---

## 7) Sources

- AssemblyAI pricing: <https://www.assemblyai.com/pricing>
- AssemblyAI benchmarks: <https://www.assemblyai.com/benchmarks>
- Cohere models overview: <https://docs.cohere.com/docs/models>
- Cohere pricing: <https://cohere.com/pricing>
- Cohere pricing docs: <https://docs.cohere.com/docs/how-does-cohere-pricing-work>
- Cohere FAQs: <https://docs.cohere.com/v1/docs/cohere-faqs>
- OpenAI pricing (checked 2026-09-21, current location): <https://developers.openai.com/api/docs/pricing>
- OpenAI deprecations (checked 2026-09-21; `gpt-transcribe` timeline): <https://developers.openai.com/api/docs/deprecations>
- OpenAI pricing (older/legacy location, may redirect): <https://platform.openai.com/docs/pricing>
- OpenAI audio models announcement: <https://openai.com/index/introducing-our-next-generation-audio-models/>
- OpenAI model pages (predate `gpt-transcribe`):  
  <https://platform.openai.com/docs/models/gpt-4o-transcribe>  
  <https://platform.openai.com/docs/models/gpt-4o-mini-transcribe>
- OpenAI prepaid billing help: <https://help.openai.com/en/articles/8264644-how-can-i-set-up-prepaid-billing>
- Groq speech-to-text docs: <https://console.groq.com/docs/speech-to-text>
- Groq rate limits: <https://console.groq.com/docs/rate-limits>
- Groq pricing: <https://groq.com/pricing>
- Deepgram pricing: <https://deepgram.com/pricing>
- Deepgram Nova-3 changelog: <https://developers.deepgram.com/changelog/speech-to-text-api-nova-3>
- ElevenLabs STT overview: <https://elevenlabs.io/speech-to-text/>
- ElevenLabs model reference: <https://elevenlabs.io/docs/overview/models>
- ElevenLabs STT API reference and model deprecation notice: <https://elevenlabs.io/docs/api-reference/speech-to-text/convert>
- ElevenLabs API pricing: <https://elevenlabs.io/pricing/api/>
- Azure LLM Speech API: <https://learn.microsoft.com/azure/ai-services/speech-service/llm-speech>
- Azure MAI-Transcribe model: <https://learn.microsoft.com/azure/ai-services/speech-service/mai-transcribe>
- Azure Speech pricing: <https://azure.microsoft.com/pricing/details/speech/>
- Alibaba Model Studio pricing: <https://www.alibabacloud.com/help/en/model-studio/model-pricing>
- MAI-Transcribe-2 announcement (price, leaderboard claim): <https://microsoft.ai/news/mai-transcribe-2-is-the-fastest-most-accurate-and-cheapest-speech-recognition-model-in-the-world/>
- MAI-Transcribe-1.5 announcement: <https://microsoft.ai/news/mai-transcribe-1-5more-accurate-context-aware-and-built-for-production/>
- Azure Speech regions with LLM Speech: <https://learn.microsoft.com/azure/ai-services/speech-service/regions?tabs=llmspeech>
- Azure Speech quotas and limits (F0/S0 correction, checked 2026-09-21): <https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits>
- Azure `transcribe` REST reference (250 MB / 2 h limit, checked 2026-09-21): <https://learn.microsoft.com/rest/api/speechtotext/transcriptions/transcribe>
- Fun-ASR-realtime pricing (checked 2026-09-21): <https://www.alibabacloud.com/help/en/model-studio/fun-asr-realtime>
- Fun-ASR (non-realtime) pricing, for comparison: <https://www.alibabacloud.com/help/en/model-studio/fun-asr>
- Alibaba Model Studio free-quota policy (checked 2026-09-21): <https://www.alibabacloud.com/help/en/model-studio/new-free-quota>
- Groq free-plan rate limits (checked 2026-09-21): <https://console.groq.com/docs/rate-limits>
- ElevenLabs pricing (Free plan, credits per minute, checked 2026-09-21): <https://elevenlabs.io/pricing>
- Deepgram pricing, incl. promotional streaming rates (checked 2026-09-21): <https://deepgram.com/pricing>
- Speechmatics pricing (checked 2026-09-21): <https://www.speechmatics.com/pricing>
- Cloudflare Workers AI pricing (checked 2026-09-21): <https://developers.cloudflare.com/workers-ai/platform/pricing/>
- Google Cloud Speech-to-Text pricing (checked 2026-09-21): <https://cloud.google.com/speech-to-text/pricing>
- Artificial Analysis speech-to-text leaderboard (checked 2026-09-21): <https://artificialanalysis.ai/speech-to-text>
- Hugging Face Open ASR Leaderboard: <https://huggingface.co/spaces/hf-audio/open_asr_leaderboard>
- Open ASR Leaderboard methodology paper: <https://arxiv.org/abs/2510.06961>
- Voice Writer STT leaderboard: <https://voicewriter.io/speech-to-text-api-leaderboard/>
