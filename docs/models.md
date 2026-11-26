# Models & Offline Setup

Everything about model choices, downloading, and configuring models for offline or corporate use.

## Available models

The app has five local runtime families:

- **GPU-accelerated ONNX models** — Cohere Transcribe and IBM Granite Speech, run
  on the GPU through WebGPU via a Node.js helper. These are the highest-accuracy
  local models and, on a machine with a working GPU, usually more accurate than
  the Whisper models. Batch mode only.
- **NVIDIA Nemotron 3.5** (int4, ONNX Runtime GenAI) — the local true cache-aware
  streaming model; also supports batch.
- **onnx-asr models** (Parakeet TDT v3, Fluid Speech Pro, Canary) — pure
  Python, CPU only, no Node.js and no GPU. Parakeet is the fastest local model
  in this app that transcribed the benchmark recording correctly: RTF 0.043 on
  a Ryzen 5 7600X. Fluid Speech Pro is FluidVoice Labs' post-training of the
  same model: a lower word error rate than v3 on the 2026-11-25
  German and English test (German 3.3 % against 6.5 %, English 3.6 %
  against 4.9 %), at about three times the processing time (see
  [onnx-asr models](#onnx-asr-models-parakeet-canary)). Batch mode only.
- **IBM Granite Speech 5.0 470M TurboCTC** (int8, ONNX Runtime) — a CTC encoder
  that runs on the CPU provider the app already ships, with numpy features and
  the `tokenizers` package; no Node.js and no GPU. English only, batch only.
  It is the fastest local model here on English: on one 29.4 s English
  recording (Ryzen 5 7600X, CPU, mean of three runs, measured 2026-09-19) RTF
  0.014, against 0.050 for Parakeet and 0.159 for `small`. The price is the text: lower case, no punctuation and no
  apostrophes, so "Mr. Quilter's manner" comes out as "mister quilter is
  manner". That is how the model was trained, not something the app strips.
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** (CTranslate2) —
  CPU-based, no extra setup, the broad-compatibility baseline; also supports the
  experimental rolling-window streaming mode.

Granite Speech 5.0 2B runs as a q4 Transformers.js ONNX package on the WebGPU
pipeline path, from IBM's own repository
[`ibm-granite/granite-speech-5.0-2b-ONNX`](https://huggingface.co/ibm-granite/granite-speech-5.0-2b-ONNX)
(Apache-2.0). It replaced Granite 4.0 1B and 4.1 2B on 2026-11-25 (the owner's
rule is that only the newest generation is offered): on the same 25 German and
25 English FLEURS clips, 5.0 2B scored 3.5 % (German) and 4.9 % (English, Auto) or 5.3 % (English,
language chosen: the app then adds the language sentence) word error rate, 4.1 2B
as the app shipped it 10.6 % and 13.5 %. The 4.x models were handed an
instruction that makes 5.0 paraphrase or answer in another language, so the
app now sends IBM's punctuation instruction (and, when you pick a language,
the sentence "The speech is in <language>. Transcribe it exactly; do not
translate."). The Plus and NAR variants of 4.1 were retired on 2026-08-26:
their exports cannot use any GPU here, they measured 4.5x and 42x slower than
the base 4.1 2B on WebGPU, and their transcripts were unusable. See
[Granite Speech 4.1 ONNX variants](granite-speech-4.1-onnx-variants.md).

For deeper background on WebGPU, DirectML, CPU fallback, memory behavior, and
language handling, see [Local ONNX Runtime Guide](local-onnx-runtime.md).

| Model | Runtime | Size | Languages | Best for |
|-------|---------|------|-----------|----------|
| `small` | CTranslate2 | ~486 MB | Multilingual | The Whisper that streams on a CPU-only laptop; modest accuracy |
| `large-v3` | CTranslate2 | ~3.09 GB | Multilingual | Best Whisper quality (NVIDIA GPU recommended) |
| `large-v3-turbo` | CTranslate2 | ~1.62 GB | Multilingual | Fast + high quality — pruned version of large-v3 |
| `distil-large-v3.5` | CTranslate2 | ~1.52 GB | **English only** | Fastest high-quality English transcription |
| `cohere-transcribe-03-2026` | ONNX/WebGPU | ~2.13 GB q4 | 14 explicit languages; no Auto | High-quality local ASR, batch mode only |
| `granite-speech-5.0-2b` | ONNX/WebGPU | ~1.73 GB q4 | Auto + the languages IBM lists | IBM's newest speech model (Apache-2.0); German 3.5 % and English 4.9 % in Auto (5.3 % with a language chosen) on the 2026-11-25 test; batch mode only |
| `nemotron-3.5-asr-streaming-0.6b-int4` | ORT GenAI INT4 | ~793 MB | Auto + 28 transcription-ready/broad-coverage languages | True cache-aware local streaming at fixed 560 ms chunks |
| `parakeet-tdt-0.6b-v3` | onnx-asr INT8 (CPU) | ~670 MB | Auto (multilingual, no selection needed) | **Fastest accurate local model** — RTF 0.043 on CPU, no GPU or Node.js needed, batch mode only |
| `fluid-speech-pro` | onnx-asr INT8 (CPU) | ~801 MB | Auto (multilingual, no selection needed) | FluidVoice Labs' post-trained Parakeet v3 (CC-BY-4.0, official ONNX export): on the 2026-11-25 test German 3.25 % against v3's 6.50 % and English 3.57 % against 4.89 %, about 3x slower; pinned to one commit, batch mode only |
| `canary-1b-v2` | onnx-asr INT8 (CPU) | ~1.03 GB | 25 explicit languages; **no Auto** | German 3.7 % against Parakeet's 6.5 % on the 2026-11-25 test, RTF about 0.15; Fluid Speech Pro measured 3.25 % German (within the test's noise of Canary) and 3.57 % English against Canary's 5.45 %; batch mode only |
| `granite-speech-5.0-470m-turboctc` | ONNX Runtime INT8 CTC (CPU) | ~552 MB | **English only** | Fastest local model on English (RTF 0.014 on CPU) and the smallest that is not a Whisper size; writes lower case without punctuation or apostrophes, batch mode only |

### Which model should I use?

Accuracy and speed no longer point at the same model. For **accuracy** with a GPU
and Node.js, start with the GPU/ONNX models. For **speed**, `parakeet-tdt-0.6b-v3`
needs neither: it measured RTF 0.043 on a Ryzen 5 7600X CPU, which beats every
GPU model in the same run on its GPU. The English-only
`granite-speech-5.0-470m-turboctc` is quicker (see above) but writes plain
lower-case text and knows no other language, which is why the default is
Parakeet. Where German or English accuracy matters more than speed,
`fluid-speech-pro` measured German 3.25 % and English 3.57 % against Parakeet's
6.50 % and 4.89 % on the 2026-11-25 test. Between
models that both worked, that benchmark cannot tell you which is more
accurate -- see the
[report](benchmarks/amd-ryzen-7600x-intel-arc-a750-2026-08-25.md) -- so pick on
speed, size and language coverage. The Whisper models remain a
solid, zero-setup CPU baseline with the broadest language coverage. The surest way
to choose is to run the [benchmark](advanced-setup.md#benchmarking) on your own
hardware.

| Situation | Recommendation |
|-----------|---------------|
| Best accuracy without a GPU, German + English | `fluid-speech-pro` (CPU) |
| High accuracy, punctuated text, GPU | `granite-speech-5.0-2b` (GPU) |
| High accuracy, fastest on GPU | `cohere-transcribe-03-2026` (GPU) |
| Lowest-latency live streaming | `nemotron-3.5-asr-streaming-0.6b-int4` |
| Zero setup: fastest accurate local transcription, no GPU and no Node.js | `parakeet-tdt-0.6b-v3` (default, CPU) |
| Whisper on CPU, German + English, supports streaming | `small` |
| Better Whisper quality on CPU | `large-v3-turbo` |
| English only, punctuated text at Whisper quality | `distil-large-v3.5` |
| English only, fastest, plain lower-case text is acceptable, no GPU and no Node.js | `granite-speech-5.0-470m-turboctc` (CPU) |
| Very limited resources | `small` |

> **Real-time factor (RTF)** measures speed: processing time ÷ audio length.
> RTF 0.1 means a 10-second clip transcribes in ~1 second — lower is faster, and
> anything below 1.0 is faster than real time. In the most recent run on the
> tested Ryzen 7600X + Arc A750
> ([2026-08-25](benchmarks/amd-ryzen-7600x-intel-arc-a750-2026-08-25.md)),
> `cohere-transcribe-03-2026` measured RTF 0.083 on WebGPU, faster than `small`
> (0.154) or `large-v3-turbo` (0.376). See
> [Local Benchmark Results](benchmarks/README.md).

### Accuracy reference (Word Error Rate)

Lower is better. These are published benchmark values — your results depend on microphone, accent, and environment.

**German (FLEURS benchmark):**

| Model | WER (%) |
|-------|--------:|
| small | 10.2 |
| large-v3 | ~4.5 (est. from large-v2) |
| large-v3-turbo | similar to large-v3 |

**English (LibriSpeech clean):**

| Model | WER (%) |
|-------|--------:|
| small | 3.3 |
| large-v3 | ~2.5 (est. from large-v2) |
| large-v3-turbo | ~2.5 |
| distil-large-v3.5 | ~2.5 |

Sources: [Whisper paper](https://arxiv.org/abs/2212.04356), [faster-whisper benchmarks](https://github.com/SYSTRAN/faster-whisper).

**GPU/ONNX models (Open ASR Leaderboard):** Cohere and Granite are newer
Conformer-encoder + LLM-decoder systems and generally beat the older Whisper
models on the public
[Open ASR Leaderboard](https://huggingface.co/spaces/hf-audio/open_asr_leaderboard).
Granite Speech 4.1 2B led it at launch (2026-04-29, ~5.33% mean English WER);
see [provider-costs.md](provider-costs.md#3-quality-comparison-published-signals)
for why that rank should not be quoted as current without checking the live
page. Real-world and German quality still depend on your microphone and audio, so benchmark on your own
German quality still depend on your microphone and audio, so benchmark on your own
samples before changing the default.

### ONNX local models

Cohere Transcribe and IBM Granite Speech are selectable under the normal local
model list, but they are not CTranslate2 models. They use a separate Node.js
ONNX runtime and run in **batch mode only**. Cohere and Granite 5.0 2B both use
Transformers.js q4 ONNX packages loaded through the high-level
`GraniteSpeechForConditionalGeneration` pipeline -- there is exactly one ONNX
path here since the raw-graph variants were retired. The helper process is kept
alive only while a transcription or benchmark case is active, so the app does
not keep a large ONNX runtime idling after normal dictation.

Granite 5.0 2B loads three q4 graphs from IBM's package: the audio encoder
(`audio_encoder_q4`) and, in `q4f16`, the token embedding and the merged
decoder. On 2026-11-25 it loaded on an Intel Arc A750 under WebGPU with the
pinned onnxruntime-node 1.29.0 and ran at RTF 0.09 to 0.10.

Community GGUF builds of the older Granite 4.1 exist for the CrispASR/GGUF
runtime and cannot be loaded by the app's ONNX paths.

### Nemotron 3.5 cache-aware streaming

`nemotron-3.5-asr-streaming-0.6b-int4` is a separate local runtime path, not a
Node/WebGPU or faster-whisper model. It uses Microsoft's ONNX Runtime GenAI
`StreamingProcessor` and preserves the model's encoder/RNNT state between new
audio chunks. That avoids the repeated rolling-window transcription used by the
current faster-whisper streaming implementation.

The published multilingual INT4 ONNX export is approximately 793 MB and is
optimized for a fixed 560 ms chunk. NVIDIA's original NeMo model supports
80/160/320/560/1120 ms profiles, but changing the app setting cannot change the
fixed graph contract. Additional latency choices should be exposed only when
compatible ONNX exports exist.

The installable app dependency currently provides CPU execution. The runtime
tries DirectML first and falls back to CPU, but as of June 8, 2026 Microsoft's
`onnxruntime-genai-directml` package depends on an
`onnxruntime-directml>=1.26.0` wheel that is not yet published on PyPI. On the
test Ryzen 5 7600X, two runs measured:

- cold model load: 1.78 s (2026-08-25) and 1.90 s (2026-07-10),
- transcription RTF on CPU: 0.21 on a 24.3 s recording, 0.24 on a 28.1 s one,
- automatic language mode and the DML-to-CPU fallback both loaded correctly.

That CPU result is comfortably faster than real time on the test desktop, but
laptop performance and German dictation quality still need real user samples.

### Custom vocabulary

The Transcription tab's **Custom vocabulary** field biases transcription
toward names, product terms, and jargon you type in — but only some engines
and models read it. It is sent to the Whisper models (`small` through
`distil-large-v3.5`), OpenAI, Groq, AssemblyAI, Deepgram, Speechmatics
Enhanced, Mistral, and a custom endpoint. It is **ignored**
by Parakeet, Canary, Nemotron, Cohere, every Granite model (Granite Speech 5.0
2B and 470M TurboCTC), ElevenLabs, Azure LLM Speech, Fun-ASR, and
Speechmatics Melia 1 — those runtimes, providers and models expose no
biasing input at all. The Transcription tab
shows a note under the field for models that ignore it, so the field is
never silently no-op without a warning.

### Language selection

Settings -> Transcription rebuilds the language list for the selected engine and
model. Auto is selected by default when supported. The app keeps one canonical
language code and adapts it where a provider requires a different code format,
such as ElevenLabs Scribe's three-letter codes.

The overlay uses the same model-aware list in its `Lang` button. A selection is
saved immediately and applies to the next recording; the button is disabled
while a recording or transcription is active.

The model-aware lists are based on the current primary documentation:

- [OpenAI speech-to-text languages](https://platform.openai.com/docs/guides/speech-to-text#supported-languages)
- [Groq speech-to-text](https://console.groq.com/docs/speech-to-text)
- [Deepgram model/language overview](https://developers.deepgram.com/docs/models-languages-overview)
- [AssemblyAI models](https://www.assemblyai.com/docs/getting-started/models)
- [ElevenLabs Scribe](https://elevenlabs.io/docs/models#scribe-v2)
- [Cohere Transcribe model card](https://huggingface.co/CohereLabs/transcribe-03-2026)
- [Granite Speech 5.0 2B model card](https://huggingface.co/ibm-granite/granite-speech-5.0-2b)
- [Nemotron 3.5 INT4 ONNX model card](https://huggingface.co/onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4)
- [Official ORT GenAI Nemotron example and language-ID mapping](https://github.com/microsoft/onnxruntime-genai/blob/main/examples/python/nemotron_speech.py)

The runtime automatically tries an ONNX GPU target first and falls back to CPU
if no compatible GPU runtime loads. Cohere and Granite 5.0 2B try
WebGPU, then DirectML on Windows, then CPU. The app shows a red warning under
the model selector because pure CPU fallback can be much slower than the
CTranslate2 Whisper models.

The app also falls back from DirectML/WebGPU to CPU during transcription when a
model loads on a GPU runtime but the first generation call fails because an ONNX
operator is not supported by that provider. Benchmark `GPU only` may move
between WebGPU and DirectML, but intentionally does not use CPU fallback, so GPU
provider failures remain visible. Benchmark summaries and exports retain the
concise fallback reason. On the tested Intel Arc A750, Cohere and
Granite 5.0 2B use the Transformers.js pipeline graph and work on WebGPU.

Node.js cannot decode arbitrary audio files through `AudioContext`. The ONNX
runner decodes WAV input itself and passes Float32 audio directly to
Transformers.js. Use the app's last recording or another WAV file when
benchmarking Cohere/Granite.

Long audio is handled conservatively. Cohere uses the Transformers.js Cohere
ASR pipeline, which chunks long audio internally. Granite is processed through a
model-specific path, so the app chunks Granite audio at quiet boundaries with a
maximum chunk size of 30 seconds before generation. This keeps long recordings
from being sent as one very large prompt/audio feature block, but it is still
not a replacement for a dedicated long-form transcription pipeline.

Unlike faster-whisper and Nemotron, Cohere and Granite are not preloaded when
the app starts. This avoids expensive background CPU model loading before the
user actually starts a local ONNX transcription. The Models tab has an
expert setting to keep the last Cohere or Granite ONNX model loaded after
dictation when warm latency matters more than RAM/VRAM use.

`wasm` is not a valid device in the Transformers.js Node runtime used by this
app. It appears in the browser/web ONNX bundle, but the app process uses the
Node ONNX runtime where the practical targets are DirectML, WebGPU, and CPU.

NVIDIA Parakeet ships as `parakeet-tdt-0.6b-v3` through the pure-Python
`onnx-asr` runtime (CPU only). What remains unimplemented is the *official
NeMo/PyTorch path*, which would add a second heavyweight Python ML runtime and
does not solve the Intel-GPU requirement cleanly. See
[Local ASR Model Candidates - 2026 Re-evaluation](local-asr-model-candidates-2026.md).

### CPU vs GPU

The CTranslate2/faster-whisper runtime works on **CPU** (default) and
**NVIDIA GPU** (if CUDA is available).

- **Intel/AMD CPU**: works out of the box. Most users run on CPU.
- **NVIDIA GPU**: much faster. Set device to `auto` or `cuda` in the benchmark script.
- **Intel iGPU / AMD GPU**: not supported by the CTranslate2 backend. Use CPU.

The ONNX/WebGPU runtime is designed to be vendor-neutral when
WebGPU or DirectML is available, so Intel, AMD, and NVIDIA GPUs are all valid
targets. If neither GPU runtime can be selected by the JavaScript runtime, the
model uses CPU and will likely be slower than `large-v3-turbo`.

**Choosing the device yourself.** Settings → Transcription → **ONNX Device** applies to
the local ONNX models (Cohere, Granite, Nemotron). It offers the same choices as
the benchmark, so a device that proves faster there can be selected for everyday
dictation:

| Choice | Meaning |
|--------|---------|
| `Auto` | WebGPU, then DirectML, then CPU (the default); starts with the device a benchmark of yours measured as fastest for that model, if there is one |
| `GPU only` | WebGPU then DirectML; fails rather than falling back to CPU |
| `WebGPU only` / `DirectML only` | Pin one backend |
| `CPU only` | Never try the GPU |

Nemotron runs on ONNX Runtime GenAI, which has DirectML and CPU
only, so every GPU choice means DirectML for it. Parakeet and Canary run through
`onnx-asr` on CPU and ignore the setting entirely; the row is disabled while one
of them is selected.

**Letting a benchmark decide what `Auto` starts with.** Which device is
faster depends on the machine. A working GPU path usually wins by a wide margin
(in the development machine's 2026-08-25 run WebGPU measured 1.6x to 4.6x
faster than the CPU for the three Cohere/Granite models), while on a machine
whose GPU cannot run a model the CPU is the quicker road and every GPU attempt
is wasted load time.
To let the app find out, open Settings -> Benchmark -> **Run Benchmark...**,
choose **GPU + CPU comparison** (or **All explicit targets**) under ONNX
Device, and run it. When the run finishes, the app stores, per model, the
device it measured as fastest, and `Auto` tries that device first from then on;
the rest of the chain stays in place as the fallback. The note under
**ONNX Device** on the Transcription tab says which device `Auto` starts with.

The rule is deliberately conservative:

- Only a run that ran to its end counts, not a canceled or a failed one.
- A model must have been measured successfully on at least two devices in that
  one run; a single device is not a comparison.
- The device `Auto` starts with today stays first unless another one was at
  least 10% faster (mean real-time factor of the case). Differences of a few
  percent between two runs are noise, and a reorder costs a model reload.
- On a machine whose GPU targets all fail, the comparison measures one device
  only. Nothing is stored then, and the run's status line says so.
- A pinned device (`CPU only`, `WebGPU only`, ...) always outranks the
  measurement.
- Run the benchmark again after a driver or hardware change; the newest run
  replaces the stored answer for the models it measured. To go back to the
  default order without a new run, close the app and remove the
  `onnx_auto_preferred_devices` entry from `%APPDATA%\stt_app\settings.json`.

The command-line benchmark (`scripts/benchmark_local.py`) measures the same
cases but never writes this setting.

Nemotron currently ships with the reproducibly installable CPU ORT GenAI
runtime. Its DirectML path is already attempted by the app, but cannot be part
of the normal dependency lock until Microsoft publishes the matching DirectML
runtime wheel.

---

## First-time model download

On first use, the app downloads the selected model automatically from HuggingFace Hub. The default `parakeet-tdt-0.6b-v3` (~670 MB) takes a couple of minutes; `small` (~486 MB) takes about a minute. After that, it loads from cache in seconds.

The model is stored in the HuggingFace cache (`%USERPROFILE%\.cache\huggingface\hub\` on Windows) and persists across restarts, reboots, and updates.

The Settings **Models** tab downloads models one at a time. You can select and
queue more models while the current download continues; the row for a queued
model shows its place in line, and a "Next: ..." line under the active
download names what starts after it. The active download shows the
downloader's own byte count and total — for example "212 of 552 MB (38%),
11.8 MB/s (94.4 Mbit/s), about 25 s left" — with a progress bar. Percentage
and speed fall back to estimating from on-disk cache growth only when there
is no worker to ask (a model a transcriber downloads directly from its own
load path outside the queue, or the brief window before a queued download's
first progress event); that fallback is what the "approx." wording, when it
appears, refers to. The speed uses a short rolling window so bursty cache
writes do not immediately display `0.0`, but short pauses or jumps can still
occur while Hugging Face finalizes files.

**Cancel Downloads** stops the active worker and clears the remaining queue.
The app removes unusable `*.incomplete` files left by the interrupted transfer
but preserves files that finished downloading so a later retry can reuse them.
The command-line download script applies the same cleanup after `Ctrl+C`.

For Cohere and Granite, source checkouts use the system Node.js executable.
Before the first Cohere/Granite start of a session, the app compares
`package-lock.json` with what npm recorded as installed
(`node_modules/.package-lock.json`). When they differ -- a fresh clone, or a
`git pull` that changed the lock -- it runs `npm ci --omit=dev` itself in a
separate folder and swaps the result in only once it loads, so nothing has to
be run by hand and a failed install never breaks the packages that worked
before; when they agree it runs no npm at all. `@huggingface/transformers` is
the only declared JavaScript dependency; `onnxruntime-node` comes with it. The packaged Windows
release bundles the JavaScript dependency tree and never runs npm, but it
still needs a Node.js executable unless the distribution bundle adds one
separately. Set `STT_APP_NODE_PATH` if Node.js is installed in a non-standard
location.

If the machine-wide Node.js installer is blocked by corporate policy, use the
no-admin portable install: run `python scripts/setup_node_windows.py` (Windows
Python), which downloads the portable Node.js ZIP and sets `STT_APP_NODE_PATH`
for you. See
[Advanced Setup → Node.js for the GPU/ONNX models](advanced-setup.md#nodejs-for-the-gpuonnx-models-no-admin-install).

---

## Automatic ModelScope fallback

If a Hugging Face download fails for any reason, the app and the download script
**automatically retry against the [ModelScope](https://modelscope.cn) mirror**
(Alibaba's model hub) -- for every model except the five listed below, which
are not mirrored there and have Hugging Face as their only source. ModelScope mirrors the same repository IDs
(`onnx-community/…`, `Systran/…`, etc.) and serves the large LFS weights from its
own CDN instead of redirecting back to Hugging Face, so it usually works even
when a corporate proxy blocks Hugging Face wholesale under a "Generative AI and
ML Applications" category rule (see
[SSL / proxy issues → Category block](advanced-setup.md#category-block-hugging-face-fully-blocked-not-an-ssl-error)).

- No setup is required; the fallback is transparent and lands the files in the
  same cache location a Hugging Face download would use.
- Disable it with the environment variable `STT_APP_DISABLE_MODELSCOPE=1`.
- Override the mirror host with `STT_APP_MODELSCOPE_ENDPOINT` if needed.
- Some repositories are **not** mirrored on ModelScope. Verified against the
  ModelScope API on 2026-08-18:

  | Model | Upstream repo |
  |-------|---------------|
  | `distil-large-v3.5` | `distil-whisper/distil-large-v3.5-ct2` |
  | `parakeet-tdt-0.6b-v3` | `istupakov/parakeet-tdt-0.6b-v3-onnx` |
  | `fluid-speech-pro` | `FluidVoice-Labs/fluid-speech-pro-onnx` (by design, not probed: the app downloads it at one pinned commit, and a mirror serves a repository by name at whatever it holds now) |
  | `canary-1b-v2` | `istupakov/canary-1b-v2-onnx` |
  | `granite-speech-5.0-470m-turboctc` | `qwertz92/granite-speech-5.0-470m-turboctc-onnx` (checked 2026-09-19) |

  On a network that blocks Hugging Face wholesale these models cannot be
  fetched at all, and the app says so instead of blaming the connection. The
  list lives in `config.MODELS_WITHOUT_MODELSCOPE_MIRROR`. Download them on an
  unrestricted machine and point **Model Dir** at the result, or have IT allow
  `huggingface.co`, `hf.co` and `cas-bridge.xethub.hf.co`.

## Offline download

If the app cannot reach HuggingFace Hub (corporate firewall, air-gapped network, SSL/proxy issues), download models in advance on a machine with internet access.

### Method 1: Download script (recommended)

```powershell
# Download the default model (parakeet-tdt-0.6b-v3):
uv run python scripts/download_model.py

# Download a specific model:
uv run python scripts/download_model.py --model large-v3-turbo

# Download a GPU ONNX/WebGPU model:
uv run python scripts/download_model.py --model cohere-transcribe-03-2026

# Download true-streaming Nemotron INT4:
uv run python scripts/download_model.py --model nemotron-3.5-asr-streaming-0.6b-int4

# Download into a custom directory (USB stick, network share):
uv run python scripts/download_model.py --model small --output-dir C:\whisper-models

# Download all models:
uv run python scripts/download_model.py --all

# List available models:
uv run python scripts/download_model.py --list
```

<details>
<summary>Without uv (inside an activated venv)</summary>

```powershell
# Create and activate a venv
...\stt_app> python -m venv .venv
...\stt_app> .\.venv\Scripts\Activate.ps1
# Download the model (small in this case)
python scripts/download_model.py --model small
```

</details>

### Method 2: Git clone

If git traffic is allowed through your proxy:

> **Important:** You must have [Git LFS](https://git-lfs.com/) installed **before** cloning.
> Without it, `git clone` downloads only tiny LFS pointer files (~130 bytes) instead of the actual
> model weights. The app will fail with `Unsupported model binary version` errors.
>
> **`git lfs install` is NOT a built-in Git command** — you must install the
> `git-lfs` package first via your system package manager:
>
> **Ubuntu / Debian:**
> ```bash
> sudo apt install git-lfs
> git lfs install      # one-time per-user hook setup
> ```
>
> **Windows (winget):**
> ```powershell
> winget install GitHub.GitLFS
> git lfs install      # one-time per-user hook setup
> ```
>
> **Windows (manual):** Download from https://git-lfs.com/ and run the installer,
> then run `git lfs install` in a terminal.
>
> **macOS (Homebrew):**
> ```bash
> brew install git-lfs
> git lfs install
> ```
>
> If you already cloned without git-lfs, run `git lfs pull` inside the cloned folder to fetch
> the actual model files.

```bash
git lfs install           # one-time setup (skip if already done)
git clone https://huggingface.co/Systran/faster-whisper-small
```

Then import CTranslate2/faster-whisper models into the app's cache structure:

```powershell
uv run python scripts/import_model.py C:\Downloads\faster-whisper-small
```

The import script is intentionally CTranslate2-only. For Cohere and Granite,
use `scripts/download_model.py`; it downloads only the ONNX precision tier
required by the app (q4 for Cohere and Granite 5.0 2B; INT8 for
Parakeet, Fluid Speech Pro and Canary) and stores it in a real local folder to avoid Windows
symlink privilege errors.

<details>
<summary>All model repositories</summary>

```bash
git lfs install           # one-time setup (skip if already done)
git clone https://huggingface.co/Systran/faster-whisper-small
git clone https://huggingface.co/Systran/faster-whisper-large-v3
git clone https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo
git clone https://huggingface.co/distil-whisper/distil-large-v3.5-ct2
git clone https://huggingface.co/onnx-community/cohere-transcribe-03-2026-ONNX
git clone https://huggingface.co/ibm-granite/granite-speech-5.0-2b-ONNX
git clone https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx
git clone https://huggingface.co/FluidVoice-Labs/fluid-speech-pro-onnx
git clone https://huggingface.co/istupakov/canary-1b-v2-onnx
git clone https://huggingface.co/onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4
git clone https://huggingface.co/qwertz92/granite-speech-5.0-470m-turboctc-onnx
```

The two `istupakov` repositories and `FluidVoice-Labs/fluid-speech-pro-onnx`
are the onnx-asr models, and `parakeet-tdt-0.6b-v3` is the app's
default. They matter most here: together with `distil-large-v3.5` and
`granite-speech-5.0-470m-turboctc` they are the
five models with no ModelScope mirror, so on a network that blocks Hugging
Face a clone from a machine that can reach it is the only route.

A plain clone fetches every weight file a repository holds, which is far more
than the app runs: the two `istupakov` repositories also ship fp32 graphs
(2.4 GB and 3.3 GB), and
`qwertz92/granite-speech-5.0-470m-turboctc-onnx` ships all three precisions of
one graph — `onnx/model.onnx` (1.89 GB), `onnx/model_fp16.onnx` (947 MB) and
`onnx/model_int8.onnx` (551 MB), of which the app uses only the int8 one.
`scripts/download_model.py` fetches the required tier and nothing else, so
prefer it where it can reach Hugging Face. To keep a clone to the same files,
skip the weights and pull back only what is needed:

```bash
GIT_LFS_SKIP_SMUDGE=1 git clone https://huggingface.co/qwertz92/granite-speech-5.0-470m-turboctc-onnx
cd granite-speech-5.0-470m-turboctc-onnx
git lfs pull --include "onnx/model_int8.onnx"
```

In PowerShell the first line is `$env:GIT_LFS_SKIP_SMUDGE = "1"` followed by
the `git clone` on its own line. The pointer files of the graphs you did not
pull stay behind as ~130-byte placeholders; the app never opens them.

`FluidVoice-Labs/fluid-speech-pro-onnx` needs one more step: the app
downloads it at commit `fa588e9f87f1b9ad0ca2e2d8f7eaf84dc3803581`, and a
clone of the default branch may hold other files under the same names, so
check that commit out. The repository also carries fp32 copies (2.67 GB),
"clean mode" graphs and sherpa-onnx's decoder and joiner, 3.6 GB in all, so
the same skip-and-pull approach keeps it to the six files the app runs (about
801 MB):

```bash
GIT_LFS_SKIP_SMUDGE=1 git clone https://huggingface.co/FluidVoice-Labs/fluid-speech-pro-onnx
cd fluid-speech-pro-onnx
GIT_LFS_SKIP_SMUDGE=1 git checkout fa588e9f87f1b9ad0ca2e2d8f7eaf84dc3803581
git lfs pull --include "encoder-model.int8.onnx.data,decoder_joint-model.int8.onnx.data"
```

A cloned ONNX repository needs no import step (`scripts/import_model.py` is for
the CTranslate2 models only): move the cloned folder, keeping its name, into
the folder **Model Dir** points at. The app looks for these models in
`<Model Dir>\<repository name>`, for example
`D:\stt-models\granite-speech-5.0-470m-turboctc-onnx`.

</details>

### Method 3: Manual browser download

Manual browser import is supported for CTranslate2/faster-whisper models only.
Download these files from the HuggingFace model page: `config.json`, `model.bin`,
`tokenizer.json`, `vocabulary.txt` (or `vocabulary.json`).

| Model | Download page |
|-------|---------------|
| small | [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small/tree/main) |
| large-v3 | [Systran/faster-whisper-large-v3](https://huggingface.co/Systran/faster-whisper-large-v3/tree/main) |
| large-v3-turbo | [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo/tree/main) |
| distil-large-v3.5 | [distil-whisper/distil-large-v3.5-ct2](https://huggingface.co/distil-whisper/distil-large-v3.5-ct2/tree/main) |

Place the files in a folder (e.g. `C:\Downloads\faster-whisper-small\`) and run the import script:

```powershell
uv run python scripts/import_model.py C:\Downloads\faster-whisper-small
# or if uv doesn't work
.\.venv\Scripts\Activate.ps1
python.exe .\scripts\import_model.py C:\Downloads\faster-whisper-small
```

### Transfer to target machine

**If you used `--output-dir`:** Copy the entire directory to the target machine and set **Model Dir** in Settings to that path.

**If you used the default cache:** Copy `%USERPROFILE%\.cache\huggingface\` to the same location on the target machine. The app finds models there automatically.

---

## Configure the app for offline use

After transferring model files to the target machine:

1. Open **Settings** (right-click tray icon → Settings).
2. Check **Offline mode** — prevents any network access.
3. Set **Model Dir** (only if you used a custom directory, not the default cache).

Alternatively, set an environment variable before launching:

```powershell
$env:HF_HUB_OFFLINE = "1"
python main.py
```

---

## How model loading works (technical)

When you select e.g. `small` in Settings, faster-whisper resolves the model in this order:

1. **Is the model name a directory path?** → If `model_size_or_path` points to an existing folder on disk (e.g. `C:\models\faster-whisper-small\`), it uses that folder directly.

2. **Is the model in the cache?** → The short name (`small`) is mapped to a HuggingFace repo ID (`Systran/faster-whisper-small`). faster-whisper checks the HuggingFace cache (or the configured Model Dir) for a downloaded snapshot. If found → loads from cache, no internet needed.

3. **Download from HuggingFace Hub** → If no cache hit, the model is downloaded and cached. This only happens once per model.

**Fallback behavior:** If the selected model cannot be loaded (download fails, file missing), the app falls back to any locally cached model (preferring `small` as last resort) and shows a warning.

### HuggingFace cache structure

The cache uses HuggingFace's internal directory format, not flat files:

```
%USERPROFILE%\.cache\huggingface\hub\
  models--Systran--faster-whisper-small\
    refs\main                              ← commit hash reference
    snapshots\abc123...\                   ← actual model files
      config.json
      model.bin
      tokenizer.json
      vocabulary.txt
```

This is why you cannot just drop files into a folder — the download script and import script handle this structure automatically.

### Reclaiming disk from retired models

When a model is retired its downloaded files stay on disk, untouched. If you had
one selected, the app moved the selection to its successor once and said so in a
tray message. **Settings > Models lists every retired model whose files are still
there** as a grey row marked "No longer offered, still on disk"; select it and
press Delete Selected to free the space (the row offers nothing else, and Download
never touches it). The retired models and the files they leave:

| Retired | Moved to | Folder or cache entry | Size |
| --- | --- | --- | --- |
| Granite 4.0 1B (2026-11-25) | Granite 5.0 2B | `granite-4.0-1b-speech-ONNX` | ~1.84 GB |
| Granite 4.1 2B (2026-11-25) | Granite 5.0 2B | `granite-speech-4.1-2b-ONNX` | ~1.84 GB |
| Parakeet v3 Ultra (2026-11-25) | Fluid Speech Pro | `parakeet-tdt-0.6b-v3-ultra-onnx` | ~668 MB |
| Whisper `tiny` (2026-11-25) | `small` | `models--Systran--faster-whisper-tiny` | ~78 MB |
| Whisper `base` (2026-11-25) | `small` | `models--Systran--faster-whisper-base` | ~148 MB |
| Whisper `medium` (2026-11-25) | `large-v3-turbo` | `models--Systran--faster-whisper-medium` | ~1.53 GB |
| Granite 4.1 Plus and NAR (2026-08-26) | | see below | up to 3.8 GB each |

The sizes of the 2026-11-25 rows are the app's own download estimates, not a
measurement of any disk. They sit in the **Model Dir** you set, or under
`%USERPROFILE%\.cache\huggingface\hub` (the ONNX models as a plain folder
named like the repository, the Whisper models as `models--...` entries). Keep a
folder only for your own experiments; the app never uses it again.

Granite 4.1 **Plus** and **NAR** were retired on 2026-08-26. Measured on the
development machine:

| directory (under `%USERPROFILE%\.cache\huggingface\hub`) | size |
| --- | --- |
| `ibm-granite-speech-4.1-2b-plus-onnx` | 3.8 GB |
| `ibm-granite-speech-4.1-2b-nar-onnx` | 2.4 GB |
| `granite-speech-4.1-2b-plus-ONNX` | 1.8 GB |
| `models--smcleod--ibm-granite-speech-4.1-2b-nar-onnx` | 1.0 GB |
| `models--smcleod--ibm-granite-speech-4.1-2b-plus-onnx` | 8 MB |
| `models--valoomba--granite-speech-4.1-2b-plus-ONNX` | negligible |

### Custom Model Dir

Setting **Model Dir** (e.g. `D:\whisper-models`) causes all model downloads and cache lookups to use that directory instead of the default HuggingFace cache. The same internal structure is created there.

Useful for: USB transfer, network share, keeping models separate from user profile.

### onnx-asr models (Parakeet, Fluid Speech Pro, Canary)

`parakeet-tdt-0.6b-v3`, `fluid-speech-pro` and `canary-1b-v2` run
through [`onnx-asr`](https://github.com/istupakov/onnx-asr), a pure-Python
runtime. Unlike the Cohere/Granite models they need **no Node.js**, and unlike
Nemotron they need no extra ONNX Runtime — they reuse the one the app already
ships.

All three are **CPU only and batch only**. That is not a limitation in practice:
measured on a Ryzen 5 7600X, Parakeet transcribes a 24.3-second recording in
about 1.03 s (RTF 0.043), which is faster than any GPU model in this app.

**Fluid Speech Pro** is FluidVoice Labs' post-training of Parakeet TDT 0.6B v3
(same architecture, run by onnx-asr as `nemo-conformer-tdt`), from the
vendor's own ONNX export
[`FluidVoice-Labs/fluid-speech-pro-onnx`](https://huggingface.co/FluidVoice-Labs/fluid-speech-pro-onnx).
**Licence: CC-BY-4.0** (attribution required; the repository's LICENSE and
NOTICE files are fetched with the model). The app fetches six model files plus
those texts, about 801 MB, at commit
`fa588e9f87f1b9ad0ca2e2d8f7eaf84dc3803581`: the repository is new, and the
size and the measurements here describe that commit. It replaced a community
export of Moondream's Parakeet Ultra on 2026-11-25.

Measured on 2026-11-25 against Parakeet v3 on 25 German and 25 English
clips of the FLEURS test set (CC-BY-4.0; 349.5 s and 233.9 s of audio, about
500 reference words per language), one text normaliser for both models, CPU,
PC shared with other work:

| Model | German WER | English WER | RTF |
|-------|-----------:|------------:|----:|
| Parakeet v3 | 6.5 % | 4.9 % | 0.05 to 0.09 |
| Fluid Speech Pro | 3.3 % | 3.6 % | 0.16 to 0.26 |
| Canary 1B v2 | 3.7 % | 5.5 % | 0.13 to 0.16 |

The sample is small (a difference of one point is about five words), so read
it as "clearly better than v3", not as a ranking among the two best. Part of
Parakeet v3's German figure is its Auto mode answering in English on about 3
of the 25 German clips. Fluid Speech Pro is about three times slower than v3
(a minute of audio takes about 12 s instead of 4 s), still far faster than
real time.

**v3 or Fluid Speech Pro?** v3 stays the default for speed, size and the 25
European languages NVIDIA documents for it; Fluid Speech Pro is the pick for
German and English dictation where accuracy matters more than a few seconds.
Languages other than German and English were not measured for Fluid Speech
Pro. It needs nothing written into its folder.

The ONNX Device setting does not apply to them and is disabled while one is
selected. A DirectML build of ONNX Runtime would be roughly twice as fast again,
but installing it overwrites the ONNX Runtime that Nemotron depends on and breaks
that engine, so the app deliberately does not ship it.

**Language selection differs between the two, and it matters:**

- **Parakeet** (v3 and Fluid Speech Pro) is implicitly multilingual and ignores any
  language you give it, so it offers only `Auto`.
- **Canary** has no automatic detection. Left to itself it would *translate* into
  English instead of transcribing, so the app requires you to pick one of its 25
  trained languages and never offers `Auto`.

Parakeet's TDT decoder can return no text for a short but real utterance
(about one to two seconds). That is a model miss, not the silence gate: the
overlay shows an Error with Retry, and History → Use last recording can send
the same clip to another model. Do not pad the audio as a workaround — extra
silence can drop words or invent new ones.
