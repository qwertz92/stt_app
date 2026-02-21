# AGENTS.md

## Purpose

Running project memory for \`tts_app\`. Agents: read this first before making changes.
Detailed history is in \`docs/learning-log.md\`.

## Quality principle

Quality has the highest priority. Take as much time as needed.

- No duplicated logic: every function/constant should exist in exactly one place.
- No dead code or unused imports.
- Every change must pass all existing tests.
- Document decisions here; document history in \`docs/learning-log.md\`.

## Language rule

**All project content must be in English.** Code, comments, docs, commits, error messages, UI labels, logs.
Exception: \`stt-dictation-spec.md\` (legacy bilingual).

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
| \`config.py\` | All tunables/constants; \`MODEL_REPO_MAP\` (single source of truth) |
| \`ssl_utils.py\` | Shared \`is_ssl_error()\` for SSL/Zscaler detection |
| \`controller.py\` | Main orchestrator/state machine; hotkey, audio, transcriber, overlay, inserter; model preload with fallback |
| \`audio_capture.py\` | sounddevice mic recording + VAD auto-stop + streaming chunk callback |
| \`transcriber/local_faster_whisper.py\` | Batch + streaming via faster-whisper; \`find_cached_models\`; \`preload_model\` |
| `transcriber/assemblyai_provider.py` | Batch + streaming transcription via AssemblyAI SDK; `test_connection` |
| `transcriber/openai_provider.py` | Batch transcription via OpenAI API; `test_connection` |
| `transcriber/groq_provider.py` | Batch transcription via Groq SDK (whisper-large-v3, whisper-large-v3-turbo); `test_connection` |
| `transcriber/deepgram_provider.py` | Batch via Deepgram REST + streaming via Deepgram WebSocket; `test_connection` |
| \`transcriber/factory.py\` | Creates transcriber from settings; routes engine to provider |
| \`text_inserter.py\` | Clipboard-safe paste: save > set > paste > restore |
| \`overlay_ui.py\` | Always-on-top frameless overlay with state colors, copy button |
| \`hotkey.py\` | Win32 RegisterHotKey + Qt native event filter |
| \`window_focus.py\` | Capture/compare/restore foreground window |
| \`settings_store.py\` | JSON settings validation and persistence |
| \`settings_dialog.py\` | PySide6 settings UI with Local/Remote tabs |
| \`secret_store.py\` | keyring wrapper for API keys |
| \`scripts/download_model.py\` | Automated model download for offline/corporate use |
| \`scripts/import_model.py\` | Import manually downloaded models; validates for Git LFS pointers |
| `scripts/sync_to_windows.sh` | Bash/rsync script to sync repo from WSL to Windows-native directory |
### Key design decisions

- **Temp files for audio**: \`transcribe_batch\` writes WAV to temp file because \`WhisperModel.transcribe()\` is most reliable with file paths.
- **GUITHREADINFO duplication**: defined in both \`text_inserter.py\` and \`window_focus.py\`. Intentional — modules are self-contained.
- **SendInput restore delay (160ms)**: Empirical value. Some apps (Electron/Chrome) read clipboard asynchronously 50-100ms after Ctrl+V. 160ms prevents stale paste.

## Core flow

1. Global hotkey toggles recording.
2. Overlay: \`Idle > Listening > Processing > Done/Error\`.
3. Batch: recorded WAV transcribed on stop.
4. Streaming (local + AssemblyAI): live chunks with partial text and incremental insertion.
5. Text inserted at caret via clipboard-safe paste; clipboard restored.

## Text insertion

- **Auto** (default): SendInput (Ctrl+V) first, WM_PASTE fallback.
- **WM_PASTE**: direct message to focused control.
- **SendInput**: synthesized Ctrl+V via Win32.
- Insertion target prefers: caret window > focused control > foreground window.

## Configuration

All defaults in \`src/tts_app/config.py\`. Key values:

- \`DEFAULT_HOTKEY = "Ctrl+Alt+Space"\`, \`FALLBACK_HOTKEY = "Ctrl+Win+LShift"\`
- \`DEFAULT_MODEL_SIZE = "small"\`, \`DEFAULT_ENGINE = "local"\`
- \`VALID_ENGINES = ("local", "assemblyai", "openai", "groq", "deepgram")\`
- \`STREAMING_ENGINES = ("local", "assemblyai", "deepgram")\` — engines that support streaming mode
- \`VALID_MODEL_SIZES\`: tiny, base, small, medium, large-v3, large-v3-turbo, distil-large-v3.5
- \`GROQ_MODELS\`: whisper-large-v3, whisper-large-v3-turbo
- \`OPENAI_MODELS\`: gpt-4o-mini-transcribe, gpt-4o-transcribe, whisper-1

## Settings and secrets

- Settings JSON: \`%APPDATA%\tts_app\settings.json\`
- Secrets: Windows Credential Manager via \`keyring\`
- API keys are never stored in JSON

## Tests

Run: \`uv run python -m pytest\` or \`python -m pytest\`

Current: ~300 tests (Linux: all pass except 3 Windows-only ctypes/windll tests).

## Known limitations

- Streaming: local + AssemblyAI + Deepgram, append-oriented (no word deletions), best-effort focus-change abort.
- ARM CPUs: not supported (CTranslate2 requires x86 AVX/SSE).
- Clipboard restore: Unicode text only.
- OpenAI and Groq: batch mode only (streaming not supported).
- NVIDIA Parakeet is intentionally not implemented; see `docs/parakeet-evaluation.md` for rationale.
