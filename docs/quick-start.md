# Quick Start Guide

Get the dictation app up and running in 5 minutes.

## Prerequisites

- **Windows 11**
- **Python 3.12** — [Download here](https://www.python.org/downloads/) if not installed. During install, check "Add Python to PATH".
- **uv** (recommended) — Install with: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

## 1. Install

Open PowerShell in the project folder:

```powershell
uv python pin 3.12
uv sync --group dev
```

<details>
<summary><b>Without uv</b> (corporate environments where uv is blocked)</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev-win.txt
```

</details>

## 2. Start the app

```powershell
uv run python main.py
```

A small overlay window appears in the top-right corner and a tray icon is added to the system tray.

## 3. Dictate

1. Click into any text field (Notepad, Word, browser, email, etc.).
2. Press **Ctrl+Alt+Space** to start recording.
3. Speak normally.
4. Press **Ctrl+Alt+Space** again to stop.
5. The transcribed text is inserted at the cursor position.

That's it! The overlay shows the current state:

| Overlay color | Meaning |
|---------------|---------|
| Dark gray | Idle — ready |
| Green | Listening — recording your voice |
| Blue | Processing — transcribing |
| Brown | Done — text inserted |
| Red | Error — something went wrong |

## 4. First-time model download

On the first dictation, the app downloads the `small` Whisper model (~484 MB). This happens once and may take a minute depending on your connection. The overlay shows "Loading model..." during download.

After the first download, the model is cached locally and loads in seconds.

> **Corporate network?** If the download fails, see [Models & Offline Setup](models.md) and [SSL / proxy troubleshooting](advanced-setup.md#ssl--proxy-issues).

## 5. Settings

Access settings via: **System tray icon → Settings** (or right-click the overlay).

Common settings to adjust:

| Setting | What it does | Default |
|---------|-------------|---------|
| **Model size** | Larger = better quality, slower | `small` |
| **Engine** | `Local` (on device) or `AssemblyAI` (cloud) | `Local` |
| **Mode** | `Batch` (transcribe after stop) or `Streaming` (live, experimental) | `Batch` |
| **Hotkey** | Click the field, press your desired key combo | Ctrl+Alt+Space |
| **Paste mode** | How text is inserted into the target app | Auto |

## 6. Stop the app

Right-click the **system tray icon** → **Quit**.

## Next steps

- **Better accuracy?** Try `large-v3-turbo` (multilingual, ~809 MB) or `distil-large-v3.5` (English-only, fastest).
- **Cloud transcription?** Switch Engine to `AssemblyAI` in Settings and enter your API key.
- **Corporate/offline setup?** See [Models & Offline Setup](models.md) or [Advanced Setup](advanced-setup.md).
- **Full documentation:** See [README.md](../README.md).
