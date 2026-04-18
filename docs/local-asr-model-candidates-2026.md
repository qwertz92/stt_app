# Local ASR Model Candidates - 2026 Re-evaluation

Date: 2026-04-18

This document is the canonical evaluation for adding non-Whisper local speech
recognition models to `stt_app`. It supersedes the older standalone Cohere and
Parakeet notes and adds IBM Granite plus adjacent 2026 candidates.

## Decision

Do not promote Cohere Transcribe, NVIDIA Parakeet, or IBM Granite Speech as the
production local default yet.

Status after the 2026-04-18 implementation pass:

- Cohere Transcribe and IBM Granite Speech are available as experimental
  selectable local models through the q4 ONNX/WebGPU runtime.
- They are batch-only, require the JavaScript runtime, and show a CPU fallback
  warning in Settings.
- NVIDIA Parakeet remains unimplemented because the official NeMo path is still
  a heavyweight, NVIDIA-oriented runtime.
- Qwen3-ASR 0.6B and 1.7B were evaluated, but are not implemented because the
  official path uses a separate `qwen-asr`/PyTorch or vLLM stack and the
  available community ONNX/GGUF packages require custom runtime code rather
  than the shared Transformers.js ASR pipeline used for Cohere and Granite.

The next step is an on-device benchmark on the target Intel GPU. That benchmark
should compare:

- current `faster-whisper` models through CTranslate2 on CPU,
- `CohereLabs/cohere-transcribe-03-2026` through ONNX/WebGPU,
- `ibm-granite/granite-4.0-1b-speech` through ONNX/WebGPU,
- optionally Parakeet or Qwen3-ASR through a separate community runtime
  experiment if the ONNX/WebGPU candidates do not win clearly.

Only promote a candidate from experimental to recommended/default if it wins on
the user's real Windows hardware in warm latency, total dictation latency,
quality for German and English dictation, memory use, and packaging reliability.

## Why this changed

The older Cohere note treated Cohere mainly as an unclear hosted-provider
candidate. That is no longer accurate.

Cohere now publishes open weights for `cohere-transcribe-03-2026` under
Apache 2.0, documents local `transformers` usage, and has an ONNX/WebGPU model
variant usable through Transformers.js. That makes Cohere a real local candidate.

The decision still remains conservative because none of the new candidates are
drop-in CTranslate2 models, and the app's local runtime is intentionally simple:
Python, PySide6, `faster-whisper`, and CTranslate2.

## Current app baseline

The current local engine is `faster-whisper`, which wraps CTranslate2 Whisper
models. The app assumes CTranslate2 model layout and behavior in several places:

- `config.py` owns `MODEL_REPO_MAP` and local model identifiers.
- `transcriber/local_faster_whisper.py` loads, preloads, downloads, inventories,
  and streams CTranslate2-compatible Whisper models.
- `scripts/download_model.py` and `scripts/import_model.py` manage Hugging Face
  cache-compatible CTranslate2 model files.
- Settings and fallback logic assume that all "local models" are compatible
  with the same transcriber implementation.

Adding Cohere, Parakeet, or Granite as "just another local model" would be
incorrect. Each needs a separate runtime family and model-management path.

## Hardware reality

For this app, GPU support only matters if it helps ordinary Windows laptops and
especially Intel GPUs. An NVIDIA-only path is not enough.

### CTranslate2

CTranslate2 remains the best production path for CPU-first local dictation. Its
prebuilt binaries support x86-64 CPU acceleration and NVIDIA CUDA GPU execution.
They do not provide a clean Windows Intel GPU path. AMD HIP can exist in source
builds and ROCm-oriented environments, but that is not a practical Windows
desktop distribution path for this app.

So the current app's local GPU story is:

- CPU: supported and primary.
- NVIDIA CUDA: possible.
- Intel GPU: not supported by the current CTranslate2 app runtime.
- AMD GPU: not supported by the current Windows CTranslate2 app runtime.

### PyTorch native inference

Native `transformers` inference is the most direct Python path for Cohere and
Granite. It is also the least attractive production path for this app:

- It adds a large PyTorch dependency stack to a desktop app that currently has a
  lean local runtime.
- CPU inference for 1B-2B speech models is likely slower than CTranslate2
  Whisper variants on the same laptop.
- CUDA works for NVIDIA, but that does not help the target Intel GPU case.
- Intel XPU, DirectML, and AMD ROCm paths are not a clean, single, well-supported
  Windows packaging story for these ASR model classes.

Native PyTorch is useful for research and correctness checks, not as the first
production integration path.

### ONNX Runtime Python

ONNX Runtime has execution providers that can target CPU, CUDA, DirectML,
OpenVINO, and other backends. In principle, this could support multiple GPU
vendors. In practice, the ASR wrappers matter as much as the raw model graph:
audio preprocessing, chunking, decoder logic, tokenizer logic, language control,
and postprocessing must all match the model.

Using ONNX Runtime directly from Python may become viable, but it is high-risk
until a candidate is proven end-to-end on Windows with Intel GPU acceleration.

### Transformers.js / WebGPU

Transformers.js v4 is the most plausible multi-vendor GPU path today. Hugging
Face describes the v4 WebGPU runtime as usable across browsers and server-side
JavaScript environments, including desktop-style runtimes, and the Cohere and
Granite ONNX model cards show WebGPU examples.

This is promising because WebGPU can run on Intel, AMD, and NVIDIA GPUs through
the operating system and browser/runtime graphics stack.

It is still a new runtime family for `stt_app`:

- likely Node.js, a WebView, or another JavaScript host,
- a separate ONNX model cache,
- an IPC bridge between Python and the JS runtime,
- new startup, preload, cancellation, logging, and crash behavior,
- new packaging/offline-deployment work,
- new tests around process boundaries and model cache handling.

This is the runtime family used by the experimental Cohere/Granite integration.
It still needs a real target-hardware benchmark before it should become the
recommended local path.

## Summary table

| Candidate | Quality signal | Runtime fit | Intel/AMD/NVIDIA GPU fit | App recommendation |
| --- | --- | --- | --- | --- |
| Current `faster-whisper` | Known and already integrated | Excellent | CPU and NVIDIA only in current runtime | Keep as production local engine |
| Cohere Transcribe | Best English Open ASR mean WER in current public data | Medium as WebGPU, poor as native PyTorch production | Plausible via WebGPU only | Experimental selectable model; benchmark before recommending |
| NVIDIA Parakeet v3 | Excellent speed and good WER on supported hardware | Poor as NeMo production path | NVIDIA-only for official path | Do not implement official NeMo path |
| Parakeet community ONNX | Potentially useful | Medium-low, community maintained | Plausible via ONNX/WebGPU only | Optional benchmark, not first priority |
| IBM Granite 4.0 1B Speech | Very close to Cohere on English Open ASR | Medium as WebGPU | Plausible via WebGPU | Experimental selectable model; benchmark alongside Cohere |
| Qwen3-ASR 0.6B / 1.7B | Strong official ASR family, but not ahead of Cohere/Granite on English Open ASR average | Poor for current WebGPU path; possible future GGUF/custom ONNX experiment | Runtime-specific | Watch; do not add without a separate runtime decision |
| Canary / Voxtral / Kyutai | Interesting but less compelling for this app | Unproven | Runtime-specific | Watch only |

## Cohere Transcribe

### What it is

`CohereLabs/cohere-transcribe-03-2026` is a 2B parameter Conformer-based
audio-to-text ASR model with open weights under Apache 2.0. Cohere reports
support for 14 languages, including German and English.

The official model card documents local inference through `transformers`, with
dependencies such as PyTorch, `huggingface_hub`, `soundfile`, `librosa`,
`sentencepiece`, and `protobuf`. The model card also documents long-form
chunking and batched inference.

The ONNX Community model provides a WebGPU example through Transformers.js:

- model: `onnx-community/cohere-transcribe-03-2026-ONNX`
- runtime: `@huggingface/transformers`
- device: `webgpu`
- example dtype: `q4`

### Quality

The strongest public quality signal is the Hugging Face Open ASR Leaderboard.
Current public figures:

| Model | English Open ASR mean WER | RTFx |
| --- | ---: | ---: |
| Cohere Transcribe | 5.42 | 524.88 |
| IBM Granite 4.0 1B Speech | 5.52 | 280.02 |
| NVIDIA Parakeet TDT 0.6B v3 | 6.32 | 3,332.74 |
| OpenAI Whisper Large v3 | 7.44 | not shown in the same app docs here |

Interpretation:

- Cohere is a serious quality candidate.
- The leaderboard is English-focused; it does not replace an app-specific
  German dictation benchmark.
- The RTFx values are leaderboard throughput numbers on high-end NVIDIA A100
  infrastructure. They do not predict a Windows laptop CPU or Intel GPU result.

### Limitations that matter for dictation

Cohere is not a simple Whisper replacement:

- The model requires an explicit language code. It does not provide the same
  automatic language-detection behavior expected from Whisper.
- The ONNX model card warns that it is strongest on single-language audio and
  inconsistent on code-switched audio.
- It does not provide timestamps or diarization.
- The ONNX model card says VAD/noise gating is helpful because silence or
  low-level floor noise can produce hallucinated text.

For this app, the VAD warning is especially relevant. The app already has an
energy-based VAD, but a Cohere path would need stricter testing around silence,
breathing, keyboard noise, and low-volume rooms.

### Speed expectation

Native PyTorch CPU inference is unlikely to be a good production experience on
ordinary Windows laptops. A 2B PyTorch ASR model is likely to be slower and
heavier than the current CTranslate2 `large-v3-turbo` or `large-v3` options on
CPU.

NVIDIA CUDA would probably be fast, but the user explicitly does not benefit
from NVIDIA-only support.

The interesting path is ONNX/WebGPU with `q4` or `q8` weights. That could use
Intel GPU hardware and may be fast enough for short dictation if the model is
preloaded. This must be measured; it should not be assumed.

### Quantization

Hugging Face lists many community quantizations for Cohere, including ONNX,
int8, CoreML, MLX, GGUF, and low-bit variants. For this app the relevant ones
are the ONNX/WebGPU dtypes exposed by Transformers.js.

Likely tradeoff:

- `q4`: best chance of fitting and running quickly on Intel integrated/discrete
  GPUs; highest accuracy risk.
- `q8`: better accuracy expectation, more memory and slower than `q4`.
- `fp16`: better fidelity but may be too heavy for many Intel GPUs.
- native PyTorch BF16/FP16: useful for NVIDIA or research, not a broad Windows
  desktop path.

There is no trustworthy public app-level WER table for these quantized variants.
Quantized Cohere must be tested against the same German and English samples as
the current local models.

### Implementation effort

Hosted Cohere provider:

- Low to medium effort.
- Mostly follows existing remote provider patterns.
- Still limited by 25 MB request size and unclear product value versus existing
  remote providers.

Native local PyTorch provider:

- Medium-high prototype effort.
- High production cost due dependency size, startup time, packaging, memory, and
  GPU-vendor mismatch.
- Not recommended as the first implementation.

ONNX/WebGPU provider:

- Medium prototype effort.
- High production effort.
- Best match for Intel GPU goals.
- Recommended only as an isolated benchmark first.

## NVIDIA Parakeet

### What it is

NVIDIA Parakeet is a FastConformer-TDT ASR family distributed through NVIDIA
NeMo. The two relevant models are:

- `nvidia/parakeet-tdt-0.6b-v2`: English-only, 600M parameters.
- `nvidia/parakeet-tdt-0.6b-v3`: multilingual, 600M parameters, intended for 25
  European languages.

For this app, v3 is the only relevant Parakeet model if German matters.

### Quality and speed

Parakeet v3 currently reports:

- English Open ASR mean WER: 6.32.
- LibriSpeech clean WER: 1.92.
- RTFx: 3,332.74.

That speed is excellent, but it reflects NVIDIA-accelerated benchmark
conditions. It is not evidence that Parakeet will be fast on a Windows Intel
GPU. NVIDIA's official path is explicitly built around NVIDIA GPU-accelerated
systems and NeMo.

### Why the official path is still rejected

The official implementation path is not aligned with the app:

- requires a separate NeMo/PyTorch runtime,
- targets NVIDIA GPU acceleration,
- prefers Linux in model-card guidance,
- adds large dependencies and deployment complexity,
- does not help Intel GPU users.

If NVIDIA-only support is not acceptable, Parakeet via NeMo should remain out of
scope.

### What changed since the older note

There are now many community ONNX, CoreML, MLX, GGUF, and quantized Parakeet
variants. That makes Parakeet more interesting for a research benchmark than it
was before.

However, community ONNX Parakeet is not the same as the official NVIDIA NeMo
path. TDT/RNN-T style decoding and postprocessing are non-trivial, and the app
would inherit compatibility risk from third-party conversions.

Recommendation:

- Do not implement Parakeet through NeMo.
- Optionally include a community ONNX Parakeet model in the same WebGPU
  benchmark harness used for Cohere and Granite.
- Do not prioritize it ahead of Cohere or Granite for this user, because its
  strongest official advantage is NVIDIA speed.

## IBM Granite 4.0 1B Speech

### What it is

`ibm-granite/granite-4.0-1b-speech` is a speech-language model for multilingual
ASR and bidirectional speech translation. Its name is "1B Speech" because it is
built around the `granite-4.0-1b-base` language model, while the Hugging Face
metadata reports 2B total parameters for the speech model package.

It supports multilingual speech input in English, French, German, Spanish,
Portuguese, and Japanese. The ONNX model card also highlights keyword-list
biasing for names and acronyms, which is relevant to dictation quality.

### Quality and speed

The Open ASR results are strong:

- English Open ASR mean WER: 5.52.
- RTFx: 280.02.
- AMI WER: 8.44.
- Earnings22 WER: 8.48.
- LibriSpeech clean WER: 1.42.

This is close enough to Cohere that Granite should be included in a benchmark.
It may be especially interesting if keyword biasing can improve names, product
terms, acronyms, and domain-specific vocabulary.

### Runtime

Granite has a Transformers.js ONNX/WebGPU path with model-specific dtype
controls for embedding, audio encoder, and decoder components. This is a better
fit for Intel GPU experimentation than a native PyTorch production integration.

Because Granite is an LLM-style speech model, it may also have different
failure modes than dedicated ASR:

- prompt sensitivity,
- possible verbosity or instruction-following artifacts,
- hallucination risk when audio is ambiguous,
- token-generation latency after the audio encoder step.

Those risks are not dealbreakers, but they must be tested with real dictation
audio and silence/noise clips.

### Quantization

The ONNX path exposes `q4`, `q8`, `fp16`, and `q4f16`-style choices for
different submodules. This could be useful on Intel GPUs because the audio
encoder and decoder may have different memory/performance bottlenecks.

Tradeoff expectation:

- More aggressive quantization may be necessary for Intel iGPU memory.
- Keyword and punctuation accuracy may degrade before general WER looks bad.
- A mixed quantization plan may beat a single dtype for all submodules.

Granite should be benchmarked in at least two configurations: a low-memory
`q4`-heavy profile and a higher-quality `q8` or `fp16` profile.

## Other watched candidates

### Qwen3-ASR 0.6B and 1.7B

Qwen3-ASR is a serious ASR family and should stay on the watch list. The
official 0.6B and 1.7B model cards describe broad multilingual ASR support,
automatic language identification, and examples through `qwen-asr`,
`transformers`, and vLLM-style Python runtimes.

That is not the same integration class as the Cohere and Granite models added
in this branch. The current app integration depends on a Transformers.js ONNX
pipeline that can select `webgpu`, `dml`, or `cpu` from the same helper process.
Qwen3-ASR has community ONNX and GGUF packages, including int4 ONNX Runtime
exports and CPU-only ONNX pipelines. They are real options, but they imply a
separate runtime family and separate model-management rules.

The current public English comparison does not make Qwen an obvious upgrade:
Cohere reports average WER 5.42, Granite 5.52, and Qwen3-ASR-1.7B 5.76 on the
English Open ASR set. A Japanese RTX 5090 benchmark found Qwen3-ASR-1.7B
excellent for Japanese media audio, but that is not enough to justify adding a
new Windows desktop runtime for German/English dictation.

The dedicated Qwen note is `docs/qwen3-asr-evaluation.md`.

### ElevenLabs Scribe v2

This app already has an ElevenLabs provider with `scribe_v2`. It is relevant as
a remote quality baseline, not a local model. It should stay in provider-cost
and provider-quality comparisons.

### Voxtral Mini Realtime

Voxtral Mini Realtime is interesting because of realtime/WebGPU activity, but it
is larger and more assistant-like. It is not the first local dictation candidate
unless realtime local WebGPU becomes the main product goal.

### NVIDIA Canary Qwen and Kyutai STT

Both are worth watching from the ASR leaderboard perspective, but neither has a
clearer app fit than Cohere or Granite for Intel-GPU Windows dictation.

## Recommended benchmark before implementation

Create an experiment that does not modify the production app:

1. Use fixed German and English WAV samples:
   - short dictation, 3-10 seconds,
   - medium dictation, 30-90 seconds,
   - quiet speech,
   - keyboard/background noise,
   - silence-only,
   - names/acronyms/domain vocabulary,
   - code-switching if the user actually uses it.
2. Measure cold start, warm start, transcription latency, RTF, peak memory, and
   total end-to-end latency.
3. Compare against current `faster-whisper`:
   - `small`,
   - `large-v3-turbo`,
   - `large-v3` if available,
   - current `int8` CPU default.
4. Run on the actual target Windows machine with Intel GPU.
5. Test at least:
   - Cohere ONNX/WebGPU `q4`,
   - Cohere ONNX/WebGPU `q8` or `fp16` if available and memory allows,
   - Granite ONNX/WebGPU `q4` profile,
   - Granite ONNX/WebGPU higher-quality profile.
6. Record transcripts and manually grade dictation usefulness, not only WER.

Promotion criteria:

- German and English quality clearly beat `large-v3-turbo` for the user's audio.
- Warm latency is not worse than the current selected local model.
- Silence/noise hallucinations are controlled by VAD and do not regress UX.
- The runtime works on Intel GPU without fragile local driver setup.
- Offline model caching and packaging are understandable for non-developers.
- Failures produce actionable errors in the app.

## Implementation plan if the benchmark wins

### Phase 1: Experimental runner

Build a standalone runner first. Prefer a small Node/Transformers.js runner over
deep Python ONNX work because the public Cohere and Granite examples are already
there.

The runner should accept:

- model id,
- dtype profile,
- language,
- WAV path,
- output JSON path.

It should return:

- transcript,
- timing breakdown,
- runtime/device info,
- warnings/errors.

### Phase 2: Out-of-process app provider

If the runner wins, add a new provider that launches the runner as a controlled
child process instead of embedding JavaScript in the main PySide process.

Why out-of-process:

- easier crash isolation,
- simpler cancellation,
- simpler logging,
- fewer PySide/WebView interactions,
- easier future replacement with Python ONNX or another runtime.

The first production version should be batch-only. Streaming can come later only
if the runtime supports partial, stable outputs without excessive reprocessing.

### Phase 3: Product integration

Required app changes:

- new engine name, not a new CTranslate2 model name,
- separate model inventory/cache logic,
- download/import support for ONNX model files,
- settings UI for runtime availability and dtype profile,
- language validation because Cohere does not auto-detect language,
- stricter VAD/silence handling,
- packaging of Node/WebGPU or chosen runtime,
- tests for engine selection, provider errors, cancellation, and fallback.

## Effort estimate

| Path | Prototype | Production integration | Risk |
| --- | ---: | ---: | --- |
| Hosted Cohere API | 1-2 days | 2-4 days | Low-medium |
| Cohere native PyTorch local | 3-5 days | 2-3 weeks | High for packaging/performance |
| Cohere ONNX/WebGPU | 3-6 days | 3-6 weeks | Medium-high |
| Parakeet NeMo | 3-5 days | 1-3 weeks | High and NVIDIA-only |
| Parakeet community ONNX/WebGPU | 3-7 days | 3-6 weeks | High |
| Granite ONNX/WebGPU | 3-6 days | 3-6 weeks | Medium-high |
| Qwen3-ASR community GGUF or ONNX CPU | 3-7 days | 3-6 weeks | High |

These estimates assume no major driver/runtime blockers. A single missing ONNX
operator or WebGPU runtime issue can change the estimate materially.

## Final recommendation

Keep Cohere Transcribe and IBM Granite Speech as experimental selectable local
models, not as the production default.

The highest-value validation order is:

1. Cohere ONNX/WebGPU.
2. Granite ONNX/WebGPU.
3. Current `faster-whisper` baselines in the same report.
4. Optional Parakeet or Qwen3-ASR through a separate community runtime
   experiment if the first two do not clearly win.

Do not spend time on the official Parakeet NeMo path unless NVIDIA-only support
becomes acceptable.

Do not spend time on a native PyTorch production provider unless the WebGPU path
fails and a CPU/NVIDIA-only research path is explicitly desired.

Do not add Qwen3-ASR to the normal local model picker until a runtime decision
is made and app-specific benchmark data shows a quality win. A community GGUF
or custom ONNX experiment can be useful, but it should be treated as a new
runtime family, not folded into the Cohere/Granite WebGPU helper.

## Sources

- Cohere Transcribe model card:
  <https://huggingface.co/CohereLabs/cohere-transcribe-03-2026>
- Cohere Audio Transcription quickstart:
  <https://docs.cohere.com/docs/audio-transcription-quickstart>
- Cohere launch/benchmark blog:
  <https://cohere.com/blog/transcribe>
- Cohere ONNX/WebGPU model card:
  <https://huggingface.co/onnx-community/cohere-transcribe-03-2026-ONNX>
- NVIDIA Parakeet v3 model card:
  <https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3>
- NVIDIA Parakeet v2 model card:
  <https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2>
- IBM Granite 4.0 1B Speech model card:
  <https://huggingface.co/ibm-granite/granite-4.0-1b-speech>
- IBM Granite ONNX/WebGPU model card:
  <https://huggingface.co/onnx-community/granite-4.0-1b-speech-ONNX>
- Qwen3-ASR 0.6B model card:
  <https://huggingface.co/Qwen/Qwen3-ASR-0.6B>
- Qwen3-ASR 1.7B model card:
  <https://huggingface.co/Qwen/Qwen3-ASR-1.7B>
- Qwen3-ASR 0.6B ONNX Runtime community package:
  <https://huggingface.co/andrewleech/qwen3-asr-0.6b-onnx>
- Qwen3-ASR 0.6B ONNX CPU community package:
  <https://huggingface.co/wolfofbackstreet/Qwen3-ASR-0.6B-ONNX-CPU>
- Qwen3-ASR 0.6B GGUF community package:
  <https://huggingface.co/cstr/qwen3-asr-0.6b-GGUF>
- Hugging Face Open ASR Leaderboard repository:
  <https://github.com/huggingface/open_asr_leaderboard>
- Transformers.js v4 announcement:
  <https://huggingface.co/blog/transformersjs-v4>
- CTranslate2 hardware support:
  <https://opennmt.net/CTranslate2/hardware_support.html>
- CTranslate2 quantization:
  <https://opennmt.net/CTranslate2/quantization.html>
- CTranslate2 installation/build options:
  <https://opennmt.net/CTranslate2/installation.html>
