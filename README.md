# stt_app — Voice Dictation for Windows 11

Press a hotkey, speak, and the transcribed text appears at your cursor — in any application.

> **First time?** Follow the [Quick Start Guide](docs/quick-start.md) to get running in 5 minutes.

## What it does

- **Global hotkey** — press `Ctrl+Alt+Space` anywhere to start/stop dictation
- **Works offline** — transcription runs locally on your machine (no internet needed after first model download)
- **GPU-accelerated models** — optional Cohere and IBM Granite Speech models run on your GPU (WebGPU); Granite Speech 4.1 2B currently tops the [Open ASR Leaderboard](https://huggingface.co/spaces/hf-audio/open_asr_leaderboard) for accuracy
- **Cloud options** — use AssemblyAI, OpenAI, Groq, Deepgram, ElevenLabs, Azure LLM Speech, or Fun-ASR (Alibaba) when you prefer managed transcription
- **Any text field** — inserts text at the cursor in Notepad, Word, browsers, email, chat apps, etc.
- **Visual feedback** — a small overlay shows the current state (idle, listening, processing, done)
- **Streaming** (experimental) — see partial results while you speak

## Requirements

- Windows 11
- Python 3.12
- [uv](https://docs.astral.sh/uv/) (recommended) — or plain pip

## Install & run

```powershell
uv python pin 3.12
uv sync --group dev
uv run python main.py
```

<details>
<summary><b>Without uv</b> (corporate environments where uv is blocked)</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements-dev-win.txt
python main.py
```

</details>

## End-user Windows download

For end users, the recommended path is a GitHub Release asset, not the source
repo.

- `stt_app-win-x64.zip` is the portable bundle: unzip it and run
  `stt_app.exe`.
- `stt_app-win-x64-setup.exe` is the installer: run it once, then start the
  app from the Start menu.

Those artifacts only exist after a maintainer builds and publishes a release.
The source repository by itself is still the developer path.

## How to dictate

1. Click into any text field.
2. Press **Ctrl+Alt+Space** — overlay turns green ("Listening").
3. Speak normally.
4. Press **Ctrl+Alt+Space** again — text is inserted at the cursor.

## Settings

Right-click the **system tray icon** → **Settings**.

| Setting | What it does | Default |
|---------|-------------|---------|
| Model size | Which local model transcribes. Parakeet is the fastest of the accurate ones and needs no GPU; the Whisper sizes trade speed for quality. Streaming mode needs a Whisper size or Nemotron; Parakeet, Canary, Cohere and Granite are batch-only | `parakeet-tdt-0.6b-v3` |
| Engine | Local (on device) or remote: AssemblyAI, OpenAI, Groq, Deepgram, ElevenLabs, Azure LLM Speech, Fun-ASR | Local |
| Mode | Batch (after stop) or Streaming (live, experimental) | Batch |
| Hotkey | Click and press your preferred key combination | Ctrl+Alt+Space |
| Paste mode | How text is inserted (Auto, WM_PASTE, SendInput) | Auto |
| Offline mode | Prevent any network access for model loading | Off |

## Model recommendations

There are four local families. **GPU-accelerated ONNX models** (Cohere, IBM
Granite) are the most accurate — they run on the GPU via WebGPU, need Node.js,
and are batch-only. **onnx-asr models** (Parakeet, Canary) are pure Python, CPU
only, and need neither a GPU nor Node.js; Parakeet is the fastest local
model here that transcribed the benchmark recording correctly (only Whisper
`tiny` is quicker, and it is the weakest of the models that did).
**Nemotron 3.5** is the only local true-streaming
model. **Whisper models** (CTranslate2) need no extra setup, run on the CPU, and
also support streaming.

| Use case | Recommended model | Runtime | Size |
|----------|-------------------|---------|------|
| Best accuracy (tops the Open ASR Leaderboard) | `granite-speech-4.1-2b` | ONNX/WebGPU q4 | ~1.84 GB |
| High accuracy on a GPU | `cohere-transcribe-03-2026` | ONNX/WebGPU q4 | ~2.13 GB |
| Zero-setup default: fastest accurate local transcription, no GPU or Node.js | `parakeet-tdt-0.6b-v3` (default) | onnx-asr int8 | ~670 MB |
| Higher German accuracy than Parakeet, still CPU | `canary-1b-v2` | onnx-asr int8 | ~1.03 GB |
| Lowest-latency live streaming | `nemotron-3.5-asr-streaming-0.6b-int4` | ORT GenAI int4 | ~793 MB |
| Whisper on CPU, multilingual, supports streaming | `small` | CTranslate2 | ~486 MB |
| Better Whisper quality, still fast | `large-v3-turbo` | CTranslate2 | ~1.62 GB |
| English only, fast Whisper | `distil-large-v3.5` | CTranslate2 | ~1.52 GB |
| Quick testing / low resources | `tiny` | CTranslate2 | ~78 MB |

The default is `parakeet-tdt-0.6b-v3`: it needs no GPU and no Node.js, and it
measured a mean real-time factor of 0.043 on a Ryzen 5 7600X, against 0.154 for
`small` on the same recording and the same CPU. It transcribes 25 European
languages (the list is in the model card) and
detects the language itself. The one thing it cannot do is live streaming
(batch only), so switch to `small` or `nemotron-3.5-asr-streaming-0.6b-int4`
for that. For maximum accuracy with a GPU and Node.js, prefer
`granite-speech-4.1-2b` or `cohere-transcribe-03-2026`. Then run the
[benchmark](docs/advanced-setup.md#benchmarking) to find the best model for
*your* hardware. See [Models & Offline Setup](docs/models.md) for details. On
first use, the selected model downloads automatically; after that it loads from
cache.

## Offline / corporate networks

If the app cannot reach HuggingFace Hub (firewall, air-gapped machine), download models in advance:

```powershell
uv run python scripts/download_model.py --model small
```

Then transfer the files and enable **Offline mode** in Settings. Full instructions: [Models & Offline Setup](docs/models.md).

## Stop the app

Right-click the **system tray icon** → **Quit**.

## Documentation

| Document | For whom | Content |
|----------|----------|---------|
| [Agent & Contributor Guide](AGENTS.md) | Agents / contributors | Architecture, module map, design decisions, and coding/commit rules — the cross-agent source of truth. `CLAUDE.md` bridges Claude Code to it; history lives in the [learning log](docs/learning-log.md). |
| [Quick Start](docs/quick-start.md) | New users | 5-minute setup guide |
| [Models & Offline Setup](docs/models.md) | All users | Model choices, download, offline/corporate setup |
| [Advanced Setup](docs/advanced-setup.md) | IT / DevOps | Corporate deployment, wheelhouse, PyInstaller, SSL/proxy, benchmarking |
| [Local Benchmark Results](docs/benchmarks/README.md) | Users / maintainers | Measured local transcription results for specific hardware |
| [Windows Distribution](docs/windows-distribution.md) | Maintainers | Recommended release path for end-user Windows builds |
| [Provider Costs](docs/provider-costs.md) | Product / Ops | Cost comparison across providers and models used by this app |
| [Azure LLM Speech Setup](docs/azure-llm-speech.md) | All users | How to configure the Azure LLM Speech (MAI-Transcribe) engine: endpoint + key |
| [Streaming Mode](docs/streaming-mode.md) | Developers | Streaming architecture and tradeoffs |
| [Local ONNX Runtime Guide](docs/local-onnx-runtime.md) | Developers | How the GPU/ONNX local models run (WebGPU, DirectML, CPU, memory) |
| [How q4 Conversion Works](docs/local-onnx-q4-conversion.md) | Curious users | What q4 means, q4 vs int4, why 1B/2B local model downloads are ~2 GB |
| [Granite 4.1 ONNX Variants](docs/granite-speech-4.1-onnx-variants.md) | Developers | Why only the base 4.1 2B is shipped, and why Plus and NAR were retired |
| [Granite Speech 5.0 ONNX Export](docs/granite-speech-5-onnx-export.md) | Developers | Reproduce, quantize, validate, and benchmark the English TurboCTC ONNX exports |
| [Parakeet Evaluation](docs/parakeet-evaluation.md) | Developers | Decision record: why the NVIDIA NeMo runtime is not used (the model itself ships via onnx-asr) |
| [Cohere Transcribe Evaluation](docs/cohere-transcribe-evaluation.md) | Developers | Notes on the Cohere Transcribe local model |
| [FLEURS & Fun-ASR Evaluation](docs/funasr-and-fleurs-evaluation.md) | Developers | Background on the FLEURS benchmark and the Alibaba Fun-ASR engine |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Model download fails on corporate network | See [Offline setup](docs/models.md#offline-download) |
| SSL: CERTIFICATE_VERIFY_FAILED | Corporate proxy (Zscaler) issue — see [SSL troubleshooting](docs/advanced-setup.md#ssl--proxy-issues) |
| Hotkey does not work | Another program is holding it. The app falls back to `Ctrl+Win+F9` (then `Ctrl+Win+F8`, `Ctrl+Win+F7`, `Ctrl+Win+F6`), says so in the overlay, and takes your own combination back automatically once it is free. Your setting is never overwritten. |
| Text not inserted | Check paste mode in Settings. If it fails, transcript is copied to clipboard automatically. |
| No module named 'requests' | Run `uv sync --group dev` and restart. |

## Run tests

```powershell
uv run python -m pytest
```

## What's supported

| Feature | Status |
|---------|--------|
| Local batch transcription (faster-whisper, CPU) | Stable |
| Local GPU transcription (Cohere / IBM Granite ONNX via WebGPU) | Stable |
| Local cache-aware streaming (Nemotron 3.5) | Stable |
| Local streaming mode (faster-whisper rolling window) | Experimental |
| AssemblyAI cloud transcription (batch) | Stable |
| AssemblyAI streaming | Experimental |
| OpenAI cloud transcription (batch) | Stable |
| Groq cloud transcription (batch) | Stable |
| Deepgram cloud transcription (batch) | Stable |
| Deepgram streaming | Experimental |
| ElevenLabs cloud transcription (batch) | Stable |
| Azure LLM Speech / MAI-Transcribe (batch) | Stable (model in public preview) |
| Fun-ASR / Alibaba (batch, no German) | Stable |

## License

No license file or package license metadata has been declared yet. Contact the
repository owner before copying, redistributing, or reusing the project.
