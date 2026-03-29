# Cohere Transcribe — Evaluation for stt_app

This document summarizes our evaluation of Cohere's current speech-to-text
offering for potential use in `stt_app`, with separate attention to the
potential **local/open-weights** path and the **hosted API** path.

## Current project status

- **Status:** Not implemented.
- **Decision:** Keep this as a documented option for now; do not add runtime
  integration yet.
- **Reason:** Cohere's public materials are strong enough to evaluate the model
  directionally, but not strong enough to justify immediate implementation as
  either a local engine or a hosted provider in this app.

## Summary

**Verdict: Document, but defer implementation for now.** Cohere currently lists
`cohere-transcribe-03-2026` as its dedicated audio transcription model and
describes it as an open source research release. However, the public material
we could verify still leaves important product questions unanswered:

- For a **local engine**, there is not enough official public information to
  justify implementation over better-defined local candidates such as NVIDIA
  Parakeet or the current faster-whisper stack.
- For a **hosted provider**, trial access is easy enough to obtain, but the
  public production pricing for transcription is not yet explicit enough to
  justify another provider integration.

So the right decision today is still: document it, track it, and defer.

---

## What is Cohere Transcribe?

Cohere now lists **Cohere Transcribe** as a speech recognition offering for
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
- The currently documented usage path is the hosted `Audio Transcriptions`
  endpoint on Cohere's platform.
- The current model overview documents a maximum file size of **25 MB**.
- Cohere states that **trial API key usage is free, but limited**, and that a
  developer can get trial access by simply signing up for a Cohere account.
- Cohere's FAQ also states that their **open weights** default to
  **non-commercial** licensing, and that on-prem/self-deployment questions are
  routed through sales.

What we could **not** verify from current official sources:

- a public, official download/inference guide for running this transcription
  model locally inside a Windows desktop app,
- a clearly documented local runtime stack comparable to `faster-whisper` or
  NVIDIA's NeMo/Parakeet path,
- a public WER table comparable to the current `docs/provider-costs.md`,
- a clear public production price for the transcription model itself,
- a public statement that a normal end-user desktop workflow should treat this
  model as a ready-made self-hosted ASR option.

---

## Local-engine comparison vs NVIDIA Parakeet

This is the comparison that matters if the goal is: "Should we add another
local/offline transcription engine to `stt_app`?"

| Factor | Cohere Transcribe | NVIDIA Parakeet |
|--------|-------------------|-----------------|
| Public local-runtime clarity | Weak today | Strong |
| Public self-hosting guidance | Weak today | Strong |
| Windows desktop integration certainty | Low | Medium |
| GPU requirement | Unknown / undocumented for the public local path | NVIDIA GPU required |
| Licensing clarity for self-deploy | Weak for commercial use | Clearer OSS-style model distribution |
| Current app-fit confidence | Low | Medium, but still rejected |

Why Parakeet is still the clearer local comparison:

- NVIDIA publishes public Parakeet checkpoints, a technical report, and a known
  runtime stack through NeMo.
- That path is heavy and unattractive for this app, but it is at least
  technically concrete.
- Cohere's public docs do not currently give the same level of local-runtime
  specificity for `cohere-transcribe-03-2026`.

**Conclusion:** if the evaluation criterion is "best candidate for a new local
engine," Parakeet is still the better-defined candidate, even though we already
rejected it on practicality grounds. Cohere Transcribe may eventually become a
real local option, but the public evidence is currently too thin to treat it as
an implementation-ready offline engine.

### Would Cohere be faster than Parakeet locally?

We cannot answer that responsibly from the current official material.

- For **true local inference**, there is not enough official public information
  about Cohere's runtime path, hardware profile, or benchmark methodology.
- For the **hosted API**, Cohere could feel faster than Parakeet on machines
  without an NVIDIA GPU, but that is not a local-vs-local comparison. It is a
  remote-service comparison dominated by upload time and network conditions.

So the answer today is: **not enough verified information to claim that Cohere
is a better local runtime than Parakeet**.

---

## Hosted provider path

If we ignore the local-engine angle and treat Cohere purely as another remote
provider, the picture is different.

### Access and account friction

For hosted API access, the official docs are comparatively friendly:

- Cohere says trial API usage is free but limited.
- A developer can access all Cohere models and APIs with a trial key by simply
  signing up for a Cohere account.
- Their FAQ says production keys can be created in the dashboard by the admin
  of the organization.

That means **hosted Cohere is not obviously enterprise-only**. For prototyping,
an end-user can sign up and experiment without first going through sales.

### What still blocks a provider integration

- The public pricing docs we could verify do **not** clearly spell out a
  dedicated transcription price for Cohere Transcribe.
- The documented `25 MB` file limit would require chunking or clearer UX for
  longer recordings.
- The public docs provide weaker speech-specific quality evidence than the
  providers already integrated into this repo.

**Conclusion:** the hosted provider path is more plausible than the local path,
but it is still not a strong enough product addition yet.

---

## Implications for stt_app

### Why local integration is not recommended now

- The current app's local value proposition is "works offline on ordinary
  Windows machines."
- A new local engine is only worth the added complexity if its runtime story is
  well-defined and broadly usable.
- Cohere does not currently clear that bar from the public material we could
  verify.

### Why hosted integration would be technically easy

The repo already isolates hosted providers behind the existing remote-provider
pattern:

- `config.py` for provider/model constants
- `settings_store.py` for persisted engine/model selection
- `settings_dialog.py` for provider-specific key/model UI
- `transcriber/factory.py` for provider construction
- `transcriber/*_provider.py` for API-specific runtime code

From a code-architecture perspective, Cohere Transcribe would fit the same
pattern as OpenAI, Groq, Deepgram, and ElevenLabs.

### Why even the hosted path is still deferred

- The app already has multiple hosted providers with different cost/quality
  tradeoffs.
- The public Cohere material currently gives weaker speech-specific evidence
  than the repo already documents for the supported providers.
- The currently documented 25 MB request limit is a practical constraint for
  longer recordings unless chunking/upload logic is added.
- The public production pricing for transcription is not explicit enough to add
  a trustworthy cost comparison to `docs/provider-costs.md`.

---

## Recommendation

| Factor | Assessment |
|--------|-----------|
| Local-engine readiness | Low |
| Hosted-provider readiness | Medium-low |
| Benefit over current remote set | Unclear from current public evidence |
| Local/offline relevance | Not verified strongly enough |
| Documentation value | High |
| Immediate implementation priority | Low |

**Recommended action:** keep this evaluation on file and revisit later.

For **local integration**, revisit only if Cohere publishes:

- an official public local-runtime path,
- clear weights/download instructions,
- concrete hardware/runtime guidance,
- speech benchmarks that justify the added complexity.

For **hosted integration**, revisit only if one of these becomes true:

- Cohere publishes explicit transcription pricing and quotas,
- pricing or quotas become materially better than current providers,
- users explicitly request Cohere as a cloud provider,
- the product needs Cohere for enterprise/vendor-standardization reasons.

If the hosted path becomes attractive later, implementation should follow the
same pattern used for the existing remote providers in this repo: a provider
module, settings/model wiring, connection testing, and updated cost-quality
documentation. If the local path becomes real later, it should be re-evaluated
directly against Parakeet and the current faster-whisper stack.

---

## Sources

- [Cohere models overview](https://docs.cohere.com/docs/models)
- [Cohere pricing](https://cohere.com/pricing)
- [How Cohere pricing works](https://docs.cohere.com/docs/how-does-cohere-pricing-work)
- [Cohere blog](https://cohere.com/blog)
