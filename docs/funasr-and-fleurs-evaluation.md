# FLEURS and Fun-ASR — Evaluation for stt_app

This note records the research and decision behind two names that came up while
evaluating Azure LLM Speech: **FLEURS** and **Fun-ASR** (Alibaba). It follows
the same decision-record style as
[parakeet-evaluation.md](parakeet-evaluation.md) and
[cohere-transcribe-evaluation.md](cohere-transcribe-evaluation.md).

## TL;DR

| Name | What it actually is | Decision |
|------|---------------------|----------|
| **FLEURS** | A **benchmark dataset**, not a model | Cannot be "implemented" as an engine. Documented here so nobody tries. |
| **Fun-ASR** | A real ASR model family (Alibaba), open + hosted | **Deferred** — poor fit for this German/English dictation app today. Feasible later as a remote engine if the fit improves. |

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

## 2) Fun-ASR (Alibaba / Tongyi) — deferred

### What it is

Fun-ASR is Alibaba's end-to-end, LLM-based speech-recognition family
(FunAudioLLM / Tongyi Fun team), the successor line to the older FunASR /
Paraformer toolkit.

- **Architecture / size:** full model = 0.7B audio encoder + 7B LLM decoder
  (~7.7B params); `Fun-ASR-nano` ≈ 0.8B.
- **Open weights:** yes, on Hugging Face and ModelScope, **Apache-2.0** (e.g.
  `Fun-ASR-Nano-2512`, `Fun-ASR-MLT-Nano-2512`).
- **Hosted:** `fun-asr-realtime` / `fun-asr-flash-*` on **Alibaba Cloud Model
  Studio (DashScope)**, with an international (Singapore) endpoint and a free
  trial quota.
- **Benchmark standing:** the hosted **Fun-Realtime-ASR-preview** currently tops
  the *Artificial Analysis* speech-to-text leaderboard at **~1.7% WER** — ahead
  of ElevenLabs Scribe v2 and Microsoft MAI-Transcribe-1.5. This is what made it
  worth evaluating.
- **Languages:** **31 languages**, including Chinese (+ 7 dialects), English,
  Japanese, Korean, Vietnamese, Thai, plus a set of European languages
  (Dutch, Danish, Swedish, Polish, Portuguese, Czech, …).

### Why it is deferred for this app

1. **German is not a documented supported language.** Both the Fun-ASR technical
   report and the GitHub model card list 31 languages and **do not include
   German**. This app's primary use case is **German + English** dictation, so a
   headline engine that (per its own docs) cannot do German is a footgun: a user
   would select it, dictate German, and get poor or wrong output.
2. **The benchmark-winning model is hosted-only.** The 1.7% WER result is the
   hosted `fun-asr-realtime` *preview* on Alibaba Cloud Model Studio. Using it
   means an Alibaba Cloud account, a Singapore-region API key, and data
   processed on Alibaba infrastructure — meaningfully more onboarding friction
   than the providers already integrated, and a data-residency consideration.
3. **The open weights don't fit the local runtime.** The full model is ~7.7B
   (too large for this app's local path), and the 0.8B `nano` has **no published
   ONNX export** and uses an LLM-decoder runtime distinct from the app's existing
   Transformers.js/WebGPU and ORT-GenAI paths. Integrating it would be a new
   runtime, not a drop-in — and nano likely lacks German too.
4. **The file API shape differs.** Model Studio's batch "recording file
   recognition" is an **asynchronous task API** (submit job → poll for result),
   unlike the single synchronous POST used by the other remote providers and by
   Azure LLM Speech.

### What would change the decision

- Official, tested **German** support in a Fun-ASR model.
- An **ONNX export** of `Fun-ASR-nano` (or a small variant) that includes German
  and fits the existing local runtime.
- A user who primarily dictates **English or East/Southeast-Asian languages**
  and is fine with Alibaba Cloud onboarding — in that case the hosted remote
  path below is worth adding.

### If we do add it later (remote sketch)

The lowest-effort path is a **remote batch provider** mirroring
`azure_provider.py`:

- Endpoint: Alibaba Cloud Model Studio (DashScope), **Singapore/international**
  base URL; key from a Singapore-region API key.
- Use the async recording-file-recognition task API: submit the audio (or a
  URL), poll the task until it completes, read the transcript.
- Wire it in exactly like Azure (`config.py` engine + models + languages,
  `settings_store.py` key/endpoint, `factory.py`, `settings_dialog.py`),
  defaulting language coverage to the documented 31 (no `de`).

This stays a **deferral, not a hard no**: the remote path is feasible whenever
the language fit or user need justifies the Alibaba onboarding.

---

## Sources

- Fun-ASR (FunAudioLLM): <https://github.com/FunAudioLLM/Fun-ASR>
- Fun-ASR technical report: <https://arxiv.org/abs/2509.12508>
- FunASR toolkit: <https://github.com/modelscope/FunASR>
- Alibaba Cloud Model Studio — real-time speech recognition (Fun-ASR):
  <https://www.alibabacloud.com/help/en/model-studio/real-time-speech-recognition>
- Alibaba Cloud Model Studio — recording file recognition (Fun-ASR/Paraformer):
  <https://www.alibabacloud.com/help/en/model-studio/recording-file-recognition>
- Artificial Analysis speech-to-text leaderboard:
  <https://artificialanalysis.ai/speech-to-text>
- FLEURS dataset: <https://huggingface.co/datasets/google/fleurs>
