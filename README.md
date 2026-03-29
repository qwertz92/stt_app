# stt_app — Voice Dictation for Windows 11

Press a hotkey, speak, and the transcribed text appears at your cursor — in any application.

> **First time?** Follow the [Quick Start Guide](docs/quick-start.md) to get running in 5 minutes.

## What it does

- **Global hotkey** — press `Ctrl+Alt+Space` anywhere to start/stop dictation
- **Works offline** — transcription runs locally on your machine (no internet needed after first model download)
- **Cloud options** — use AssemblyAI, OpenAI, Groq, Deepgram, or ElevenLabs when you prefer managed transcription
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

## How to dictate

1. Click into any text field.
2. Press **Ctrl+Alt+Space** — overlay turns green ("Listening").
3. Speak normally.
4. Press **Ctrl+Alt+Space** again — text is inserted at the cursor.

## Settings

Right-click the **system tray icon** → **Settings**.

| Setting | What it does | Default |
|---------|-------------|---------|
| Model size | Larger = more accurate, slower | `small` |
| Engine | Local (on device) or remote: AssemblyAI, OpenAI, Groq, Deepgram, ElevenLabs | Local |
| Mode | Batch (after stop) or Streaming (live, experimental) | Batch |
| Hotkey | Click and press your preferred key combination | Ctrl+Alt+Space |
| Paste mode | How text is inserted (Auto, WM_PASTE, SendInput) | Auto |
| Offline mode | Prevent any network access for model loading | Off |

## Model recommendations

| Use case | Recommended model | Size |
|----------|-------------------|------|
| General use (German + English) | `small` (default) | ~484 MB |
| Better quality, still fast | `large-v3-turbo` | ~809 MB |
| Best quality with GPU | `large-v3` | ~3.09 GB |
| English only, fastest | `distil-large-v3.5` | ~756 MB |
| Quick testing / low resources | `tiny` | ~75 MB |

On first use, the selected model downloads automatically (~1 min for `small`). After that, it loads from cache in seconds.

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
| [Quick Start](docs/quick-start.md) | New users | 5-minute setup guide |
| [Models & Offline Setup](docs/models.md) | All users | Model choices, download, offline/corporate setup |
| [Advanced Setup](docs/advanced-setup.md) | IT / DevOps | Corporate deployment, wheelhouse, PyInstaller, SSL/proxy, benchmarking |
| [Provider Costs](docs/provider-costs.md) | Product / Ops | Cost comparison across providers and models used by this app |
| [Streaming Mode](docs/streaming-mode.md) | Developers | Streaming architecture and tradeoffs |
| [Parakeet Evaluation](docs/parakeet-evaluation.md) | Developers | Decision record: why NVIDIA Parakeet is not implemented |
| [Cohere Transcribe Evaluation](docs/cohere-transcribe-evaluation.md) | Developers | Decision record: why Cohere Transcribe is deferred as both a local and hosted option |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Model download fails on corporate network | See [Offline setup](docs/models.md#offline-download) |
| SSL: CERTIFICATE_VERIFY_FAILED | Corporate proxy (Zscaler) issue — see [SSL troubleshooting](docs/advanced-setup.md#ssl--proxy-issues) |
| Hotkey does not work | App auto-falls back to `Ctrl+Win+LShift`. Try another combo in Settings. |
| Text not inserted | Check paste mode in Settings. If it fails, transcript is copied to clipboard automatically. |
| No module named 'requests' | Run `uv sync --group dev` and restart. |

## Run tests

```powershell
uv run python -m pytest
```

## What's supported

| Feature | Status |
|---------|--------|
| Local batch transcription (faster-whisper) | Stable |
| Local streaming mode | Experimental |
| AssemblyAI cloud transcription (batch) | Stable |
| AssemblyAI streaming | Experimental |
| OpenAI cloud transcription (batch) | Stable |
| Groq cloud transcription (batch) | Stable |
| Deepgram cloud transcription (batch) | Stable |
| Deepgram streaming | Experimental |
| ElevenLabs cloud transcription (batch) | Stable |

## License

See [pyproject.toml](pyproject.toml) for license details.
