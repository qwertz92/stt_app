# FLEURS and Fun-ASR — Evaluation for stt_app

This note records the research and decisions behind two names that came up while
evaluating Azure LLM Speech: **FLEURS** and **Fun-ASR** (Alibaba). It follows
the same decision-record style as
[parakeet-evaluation.md](parakeet-evaluation.md) and
[cohere-transcribe-evaluation.md](cohere-transcribe-evaluation.md).

## TL;DR

| Name | What it actually is | Decision |
|------|---------------------|----------|
| **FLEURS** | A **benchmark dataset**, not a model | Cannot be "implemented" as an engine. Documented here so nobody tries. |
| **Fun-ASR** (hosted) | Alibaba's ASR family, top of the Artificial Analysis leaderboard | **Implemented** as a remote, batch-only engine (`funasr`) for its broad non-German coverage. |
| **Fun-ASR** (local weights) | Open weights, 7.7B / 0.8B nano | **Not** implemented — too big / wrong runtime / no German. |

---

## 1) FLEURS is a benchmark, not a model

FLEURS = **Few-shot Learning Evaluation of Universal Representations of Speech**.
It is an n-way parallel **speech dataset** in 102 languages (~12 hours of
supervision per language), built on top of the FLoRes-101 machine-translation
benchmark. It is used to **measure** ASR / language-ID / speech-translation
quality — for example, Microsoft reports MAI-Transcribe-1.5's multilingual
accuracy "on FLEURS."

You therefore **cannot add FLEURS as a transcription engine**: there is no
FLEURS model to call. "Leading on FLEURS" is a property of a model (e.g.
MAI-Transcribe), evaluated against the FLEURS test set.

Where FLEURS *could* be relevant to this repo is the existing local **benchmark**
feature (`local_benchmark.py` / the Benchmark tab): FLEURS clips could in
principle serve as labeled multilingual test audio. That is an evaluation-data
idea, not a provider, and it is out of scope here.

- Dataset: <https://huggingface.co/datasets/google/fleurs>
- Paper: <https://arxiv.org/abs/2205.12446>

---

## 2) Fun-ASR (Alibaba / Tongyi) — implemented as a remote engine

### What it is

Fun-ASR is Alibaba's end-to-end, LLM-based speech-recognition family
(FunAudioLLM / Tongyi Fun team).

- **Architecture / size:** full model = 0.7B audio encoder + 7B LLM decoder
  (~7.7B params); `Fun-ASR-nano` ≈ 0.8B.
- **Open weights:** yes, on Hugging Face / ModelScope, **Apache-2.0**.
- **Hosted:** `fun-asr-realtime` on **Alibaba Cloud Model Studio (DashScope)**,
  with an international (Singapore) endpoint and a free trial quota.
- **Benchmark standing:** the hosted **Fun-Realtime-ASR-preview** currently tops
  the *Artificial Analysis* speech-to-text leaderboard at **~1.7% WER** — ahead
  of ElevenLabs Scribe v2 and Microsoft MAI-Transcribe-1.5.
- **Languages:** **31 languages**, including Chinese (+ 7 dialects), Cantonese,
  English, Japanese, Korean, Vietnamese, Thai, Indonesian, plus a set of
  European languages — but **not German**.

### Why we implemented the hosted path

This app is general-purpose dictation. Fun-ASR adds **SOTA accuracy for Chinese
(incl. dialects) and East/Southeast-Asian languages** that the existing engines
do not cover as well, letting those language communities use the app. The added
effort is bounded — one more remote provider — so excluding those languages had
no good justification.

### How it is implemented

Fun-ASR's **batch "recording file recognition" API does not fit a local
dictation app**: it is asynchronous and requires a publicly reachable file URL
(OSS upload); it rejects local files and base64. So the integration instead
drives the **real-time WebSocket API in a batch fashion** (`funasr_provider.py`):

- Connect to the Singapore endpoint
  (`wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference/`),
  auth `Authorization: bearer <key>`.
- `run-task` → `task-started` → stream the recorded PCM as binary frames →
  `finish-task` → collect `result-generated` sentences → `task-finished`.
- Key-only (no per-resource endpoint). Batch mode only (not wired into the
  app's live streaming mode, even though the underlying API is realtime).

### Setup

1. Create an **Alibaba Cloud Model Studio (DashScope)** account and an
   **API key in the Singapore region**.
2. In **Settings → Remote Provider API Keys**, paste the key into the **Fun-ASR**
   field and **Save API Keys**.
3. Set **Connection Target → Fun-ASR only** and **Run Connection Test**.
4. Set **Engine → Remote (Fun-ASR / Alibaba)** and pick a language (or `Auto`).
   German is intentionally absent from the language list.

### Caveats

- **No German.** Use Azure LLM Speech or a local model for German.
- Cloud-only; audio is processed on Alibaba infrastructure (Singapore region).
- Implemented against the documented DashScope protocol; validate with a real
  key, since this repo's CI mocks the WebSocket.

### Why the local Fun-ASR weights are NOT implemented

- The full model is **~7.7B** — far too large for this app's local path.
- `Fun-ASR-nano` (0.8B) has **no published ONNX export** and uses an
  LLM-decoder runtime distinct from the app's Transformers.js/WebGPU and
  ORT-GenAI paths — a new runtime, not a drop-in — and likely lacks German too.
- The hosted realtime preview (not the open weights) is the benchmark winner, so
  a local build would not deliver the headline accuracy anyway.

This matches the user's call: skip local unless the small model were
extraordinarily good; it is not the SOTA variant, so there is no such case.

---

## Sources

- Fun-ASR (FunAudioLLM): <https://github.com/FunAudioLLM/Fun-ASR>
- Fun-ASR technical report: <https://arxiv.org/abs/2509.12508>
- Model Studio — real-time speech recognition (Fun-ASR):
  <https://www.alibabacloud.com/help/en/model-studio/real-time-speech-recognition>
- Model Studio — recording file recognition (Fun-ASR/Paraformer):
  <https://www.alibabacloud.com/help/en/model-studio/recording-file-recognition>
- Artificial Analysis speech-to-text leaderboard:
  <https://artificialanalysis.ai/speech-to-text>
- FLEURS dataset: <https://huggingface.co/datasets/google/fleurs>
