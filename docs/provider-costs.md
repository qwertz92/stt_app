# Provider Cost Overview

This document compares transcription costs for providers currently available in `tts_app`.

- Last verified: **2026-02-21**
- Billing and model prices can change at any time. Always confirm on vendor pricing pages before production rollout.

---

## 1) Quick comparison (models used by this app)

| Engine | App mode(s) | Model(s) in app | Public price | Normalized cost |
|--------|-------------|-----------------|--------------|-----------------|
| Local (`faster-whisper`) | Batch + Streaming | `tiny`..`distil-large-v3.5` | No API fee | $0 variable API cost (hardware/power only) |
| AssemblyAI | Batch | Universal-3 Pro (primary), Universal-2 fallback | `Universal-3 Pro`: $0.21/hour, `Universal-2`: $0.27/hour | $0.21-$0.27/hour |
| AssemblyAI | Streaming | Universal Streaming | $0.15/hour | $0.15/hour |
| OpenAI | Batch | `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, `whisper-1` | Mini: est. $0.003/min, 4o: est. $0.006/min, Whisper: $0.006/min | $0.18/hour, $0.36/hour, $0.36/hour |
| Groq | Batch | `whisper-large-v3`, `whisper-large-v3-turbo` | v3: $0.111/hour, turbo: $0.04/hour | $0.111/hour, $0.04/hour |
| Deepgram | Batch | `nova-3` | Monolingual: $0.0043/min, Multilingual: $0.0052/min | $0.258/hour, $0.312/hour |
| Deepgram | Streaming | `nova-3` | Monolingual: $0.0077/min, Multilingual: $0.0092/min | $0.462/hour, $0.552/hour |

Notes:

- OpenAI `gpt-4o*` transcription is token-priced; the per-minute values above are OpenAI's own estimates.
- Deepgram pricing depends on monolingual vs multilingual usage.
- In this app, Deepgram with `language_mode="auto"` uses `detect_language=true`; validate in usage reports whether your traffic is billed as multilingual.

---

## 2) Billing behavior that can surprise teams

### AssemblyAI

- Pricing is metered per second.
- Multi-channel audio is billed per second **per channel**.

### Groq

- Minimum billed length is **10 seconds** per request.
- Very short clips can cost more than expected if you call the API frequently.

### OpenAI

- `gpt-4o-transcribe` and `gpt-4o-mini-transcribe` are token-based.
- Effective per-minute cost can vary with audio characteristics and tokenization behavior.

### Deepgram

- Pricing differs between streaming and pre-recorded paths.
- Multi-channel audio can multiply billed duration.

---

## 3) Example monthly cost (for rough planning)

Assuming 50 hours of audio/month:

| Provider / Model | Estimated monthly cost |
|------------------|------------------------|
| Groq `whisper-large-v3-turbo` | $2.00 |
| Groq `whisper-large-v3` | $5.55 |
| AssemblyAI Streaming | $7.50 |
| OpenAI `gpt-4o-mini-transcribe` | $9.00 |
| AssemblyAI Universal-3 Pro | $10.50 |
| Deepgram Nova-3 Batch (mono) | $12.90 |
| Deepgram Nova-3 Batch (multi) | $15.60 |
| AssemblyAI Universal-2 | $13.50 |
| OpenAI `gpt-4o-transcribe` | $18.00 |
| OpenAI `whisper-1` | $18.00 |
| Deepgram Nova-3 Streaming (mono) | $23.10 |
| Deepgram Nova-3 Streaming (multi) | $27.60 |

This is intentionally simplified and excludes taxes, rounding behavior, free-tier credits, and enterprise discounts.

---

## 4) Sources

- AssemblyAI pricing: <https://www.assemblyai.com/pricing>
- OpenAI pricing: <https://platform.openai.com/docs/pricing>
- Groq speech-to-text docs: <https://console.groq.com/docs/speech-to-text>
- Groq pricing: <https://console.groq.com/docs/pricing>
- Deepgram pricing: <https://deepgram.com/pricing>
