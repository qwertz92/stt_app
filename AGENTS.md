# AGENTS.md

## Purpose

Running project memory for `stt_app`. Agents: read this first before making changes.
Detailed history is in `docs/learning-log.md`.

## Quality principle

Quality has the highest priority. Take as much time as needed.

- No duplicated logic: every function/constant should exist in exactly one place.
- No dead code or unused imports.
- Every change must pass all existing tests.
- Document decisions here; document history in `docs/learning-log.md`.

## Language rule

**All project content must be in English.** Code, comments, docs, commits, error messages, UI labels, logs.
Exception: `stt-dictation-spec.md` (legacy bilingual).

## Runtime stack

- Python 3.12, PySide6 UI/tray/overlay
- Win32 RegisterHotKey + SendInput (Windows 11 only; Linux/WSL for dev tooling)
- sounddevice for mic capture
- faster-whisper (CTranslate2) for local transcription
- Remote providers: AssemblyAI (SDK), OpenAI (REST API), Groq (SDK), Deepgram (REST + WebSocket)
- keyring for secret storage

## Architecture

### Module responsibilities

| Module | Purpose |
|--------|---------|
| `config.py` | All tunables/constants; `MODEL_REPO_MAP` (single source of truth) |
| `controller.py` | Main orchestrator/state machine; hotkey, audio, transcriber, overlay, inserter |
| `audio_capture.py` | sounddevice mic recording + VAD auto-stop + streaming chunk callback |
| `transcriber/local_faster_whisper.py` | Batch + streaming via faster-whisper; `find_cached_models`; `preload_model` |
| `transcriber/assemblyai_provider.py` | Batch + streaming via AssemblyAI SDK |
| `transcriber/openai_provider.py` | Batch via OpenAI API |
| `transcriber/groq_provider.py` | Batch via Groq SDK |
| `transcriber/deepgram_provider.py` | Batch via REST + streaming via WebSocket |
| `transcriber/factory.py` | Creates transcriber from settings; routes engine to provider |
| `text_inserter.py` | Clipboard-safe paste: save > set > paste > restore |
| `overlay_ui.py` | Always-on-top frameless overlay with state colors |
| `settings_dialog.py` | PySide6 settings UI with Local/Remote tabs |
| `settings_store.py` | JSON settings persistence (`%APPDATA%\tts_app\settings.json`) |
| `secret_store.py` | keyring wrapper for API keys (never stored in JSON) |
| `scripts/import_model.py` | Import manually downloaded models; validates for Git LFS pointers |
| `scripts/download_model.py` | Automated model download for offline/corporate use |

### Key design decisions

- **Temp files for audio**: `transcribe_batch` writes WAV to temp file because `WhisperModel.transcribe()` is most reliable with file paths.
- **GUITHREADINFO duplication**: defined in both `text_inserter.py` and `window_focus.py`. Intentional — modules are self-contained.
- **SendInput restore delay (160ms)**: Empirical value. Some apps (Electron/Chrome) read clipboard asynchronously 50-100ms after Ctrl+V. 160ms prevents stale paste.

## Core flow

1. Global hotkey toggles recording.
2. Overlay: `Idle → Listening → Processing → Done/Error`.
3. Batch mode: recorded WAV transcribed on stop.
4. Streaming mode (local, AssemblyAI, Deepgram): live chunks with partial text and incremental insertion.
5. Text inserted at caret via clipboard-safe paste; clipboard restored.

## Engines

- **VALID_ENGINES**: local, assemblyai, openai, groq, deepgram
- **STREAMING_ENGINES**: local, assemblyai, deepgram (others are batch-only)
- All engine/model constants defined in `config.py`

## Tests

Run: `uv run python -m pytest` or `python -m pytest`

Current: 316 tests. Linux: all pass except 1 Windows-only ctypes struct-size test.

## Known limitations

- Streaming: append-oriented (no word deletions), best-effort focus-change abort.
- ARM CPUs: not supported (CTranslate2 requires x86 AVX/SSE).
- Clipboard restore: Unicode text only.
- NVIDIA Parakeet is intentionally not implemented; see `docs/parakeet-evaluation.md` for rationale.
