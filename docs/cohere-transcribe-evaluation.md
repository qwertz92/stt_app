# Cohere Transcribe — Evaluation for stt_app

This document summarizes our evaluation of Cohere's current speech-to-text
offering for potential use in `stt_app`.

## Current project status

- **Status:** Not implemented.
- **Decision:** Keep this as a documented option for now; do not add runtime
  integration yet.
- **Reason:** The current public information is strong enough to document the
  model accurately, but not strong enough to justify immediate implementation
  over the already supported hosted providers.

## Summary

**Verdict: Document, but defer implementation for now.** Cohere currently lists
`cohere-transcribe-03-2026` as its dedicated audio transcription model and
describes it as an open source research release. In practice, the documented
product surface today is still Cohere-hosted via the Audio Transcriptions
endpoint, with a 25 MB file limit and no published desktop-focused runtime or
Windows packaging guidance. That means it does not solve the same product gap
as the historical NVIDIA Parakeet evaluation, and it also does not yet make a
strong enough hosted-provider case to outrank the cloud providers already
supported in this app.

---

## What is Cohere Transcribe?

Cohere now lists **Cohere Transcribe** as a new speech recognition offering for
producing text transcripts from audio. The official model overview identifies
`cohere-transcribe-03-2026` as a live audio model, describes it as an open
source research release focused on high-accuracy multilingual ASR, and routes
it through the `Audio Transcriptions` endpoint with a documented maximum file
size of 25 MB.

What we could verify publicly:

- Cohere exposes a dedicated audio model:
  `cohere-transcribe-03-2026`.
- The model is described by Cohere as an **open source research release**
  focused on multilingual transcription quality.
- The currently documented usage path is still the hosted `Audio
  Transcriptions` endpoint on Cohere's platform.
- The current model overview documents a maximum file size of **25 MB**.
- Cohere's public platform pricing continues to mention free trial API keys and
  general pay-as-you-go billing.

What we could **not** verify from current official sources:

- a supported Windows desktop packaging/inference path for this app,
- a public WER table comparable to the current `docs/provider-costs.md`,
- a clear public production price for the transcription model itself,
- a stable local runtime story that matches the app's current faster-whisper
  deployment model.

---

## Comparison vs NVIDIA Parakeet

Parakeet and Cohere Transcribe solve different problems in this project.

| Factor | Cohere Transcribe | NVIDIA Parakeet |
|--------|-------------------|-----------------|
| Delivery model | Hosted endpoint today | Local model / framework integration |
| Runtime impact on this app | Low local runtime burden, network required | High local runtime burden, GPU-heavy |
| Main integration type | Another remote provider | New local engine / new framework |
| Primary latency tradeoff | Upload + network + remote queue | Local decode speed on NVIDIA hardware |
| Primary product tradeoff | Provider overlap | Dependency, hardware, and deployment complexity |

**Conclusion:** Cohere Transcribe should currently be treated as another remote
provider, not as a Parakeet-style local-engine candidate. It might feel faster
than Parakeet on machines without an NVIDIA GPU because the heavy compute runs
off-device, but it is not a fair direct latency substitute for a strong local
GPU path because upload time and network variability dominate the result.

---

## Implications for stt_app

### Why integration would be technically easy

The repo already isolates hosted providers behind the existing remote-provider
pattern:

- `config.py` for provider/model constants
- `settings_store.py` for persisted engine/model selection
- `settings_dialog.py` for provider-specific key/model UI
- `transcriber/factory.py` for provider construction
- `transcriber/*_provider.py` for API-specific runtime code

From a code-architecture perspective, Cohere Transcribe would fit the same
pattern as OpenAI, Groq, Deepgram, and ElevenLabs.

### Why it is still deferred

- The app already has multiple hosted providers with different cost/quality
  tradeoffs.
- The public Cohere material currently gives weaker speech-specific evidence
  than the repo already documents for the supported providers.
- The currently documented 25 MB request limit is a practical constraint for
  longer recordings unless chunking/upload logic is added.
- It does not improve the local/offline story, which remains the app's most
  distinctive user value.

---

## Recommendation

| Factor | Assessment |
|--------|-----------|
| Architecture fit | Good as a hosted provider |
| Benefit over current remote set | Unclear from current public evidence |
| Local/offline relevance | None today |
| Documentation value | High |
| Immediate implementation priority | Low |

**Recommended action:** keep this evaluation on file, revisit later, and only
integrate Cohere Transcribe if one of these becomes true:

- Cohere publishes stronger speech benchmark evidence,
- pricing or quotas become materially better than current providers,
- users explicitly request Cohere as a cloud provider,
- the product needs Cohere for enterprise/vendor-standardization reasons.

If those conditions change, the implementation should follow the same pattern
used for the hosted providers already in this repo: a provider module,
settings/model wiring, connection testing, and updated cost-quality
documentation.

---

## Sources

- [Cohere models overview](https://docs.cohere.com/docs/models)
- [Cohere pricing](https://cohere.com/pricing)
- [How Cohere pricing works](https://docs.cohere.com/docs/how-does-cohere-pricing-work)
- [Cohere blog](https://cohere.com/blog)
