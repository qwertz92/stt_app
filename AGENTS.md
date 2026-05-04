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
- User requests may come through speech-to-text and can contain mistranscribed words or malformed phrases.
- If the intent is unclear, ask for clarification before making a change that may not match the user's actual goal.

## Commit style

- Use logical commits for distinct bugfix/feature/refactor units.
- Match the existing history: short conventional subject line, blank line, then concise `-` bullet points.
- Hard-wrap every commit body line at a maximum of 100 characters.
- Never include literal escape sequences such as `\n` in commit messages; use real newlines.
- For shell-driven commits, prefer a message file or stdin with real line breaks, then verify with `git log -1 --format=%B`.
- Do not include validation blocks or lists of executed test commands in commit messages.
- It is fine to mention newly added or updated tests as part of the change summary.

## Language rule

**All project content must be in English.** Code, comments, docs, commits, error messages, UI labels, logs.
Exception: `stt-dictation-spec.md` (legacy bilingual).

## Runtime stack

- Python 3.12, PySide6 UI/tray/overlay
- Win32 RegisterHotKey + SendInput (Windows 11 only; Linux/WSL for dev tooling)
- sounddevice for mic capture
- faster-whisper (CTranslate2) for local transcription
- Remote providers: AssemblyAI (SDK), OpenAI (REST API), Groq (SDK),
  Deepgram (REST + WebSocket), ElevenLabs (REST API)
- keyring for secret storage

## Architecture

### Module responsibilities

| Module | Purpose |
| ------ | ------- |
| `config.py` | All tunables/constants; `MODEL_REPO_MAP` (single source of truth) |
| `controller.py` | Main orchestrator/state machine; hotkey, audio, transcriber, overlay, inserter, history, preload |
| `streaming_text.py` | Pure streaming text normalization, locked-prefix, live-tail, and finalization logic |
| `audio_capture.py` | sounddevice mic recording + VAD auto-stop + streaming chunk callback |
| `transcriber/local_faster_whisper.py` | Batch + streaming via faster-whisper; `find_cached_models`; `preload_model` |
| `transcriber/local_webgpu_asr.py` | Experimental batch-only Cohere/Granite q4 ONNX runtime via Transformers.js |
| `transcriber/assemblyai_provider.py` | Batch + streaming via AssemblyAI SDK |
| `transcriber/openai_provider.py` | Batch via OpenAI API |
| `transcriber/groq_provider.py` | Batch via Groq SDK |
| `transcriber/deepgram_provider.py` | Batch via REST + streaming via WebSocket |
| `transcriber/factory.py` | Creates transcriber from settings; routes engine to provider |
| `text_inserter.py` | Clipboard-safe paste: save > set > paste > restore |
| `overlay_ui.py` | Always-on-top frameless overlay with state colors, controls, opacity slider |
| `settings_dialog.py` | PySide6 settings UI with Local/Remote/History tabs, model management |
| `settings_store.py` | JSON settings persistence (`%APPDATA%\stt_app\settings.json`) |
| `local_model_inventory_store.py` | Persistent cache of last-known local model inventories keyed by `model_dir` |
| `secret_store.py` | keyring wrapper for API keys with optional insecure plain-text fallback for restricted environments |
| `transcript_history.py` | Persistent transcript history store (JSON) with import/export |
| `history_dialog.py` | History dialog with table view, copy, export/import, clear, limit control |
| `app_paths.py` | Centralized app data/config path helpers |
| `vad.py` | Energy-based voice activity detection with configurable threshold |
| `window_focus.py` | Win32 foreground/focus/caret window tracking for text insertion |
| `hotkey.py` | Global hotkey registration via Win32 RegisterHotKey |
| `scripts/import_model.py` | Import manually downloaded models; validates for Git LFS pointers |
| `scripts/download_model.py` | Automated model download for offline/corporate use |

### Key design decisions

- **Temp files for audio**: `transcribe_batch` writes WAV to temp file because `WhisperModel.transcribe()` is most reliable with file paths.
- **GUITHREADINFO duplication**: defined in both `text_inserter.py` and `window_focus.py`. Intentional — modules are self-contained.
- **SendInput restore delay (160ms)**: Empirical value. Some apps (Electron/Chrome) read clipboard asynchronously 50-100ms after Ctrl+V. 160ms prevents stale paste.
- **Local model inventory cache**: last-known local model lists are stored in a dedicated JSON cache file, not `settings.json`, so the Local tab can render immediately without silently mutating user settings.
  Cached inventories are trusted for initial Local/Benchmark tab rendering;
  do not start a disk verification just because a cached inventory-backed tab
  became visible. Use explicit Refresh, model-dir changes without a cache, and
  download/delete completion for verification scans.
  When no cache exists, automatic Local/Benchmark tab refreshes are deferred
  briefly after tab selection so the tab paints first.
- **Transcript history retention**: history defaults to 500 saved entries, and
  legacy settings that still have the old 20-entry default are migrated upward.
  Successful transcriptions are added to history before text insertion, so a
  paste/focus failure does not drop the transcript. The stored model name comes
  from the transcription settings snapshot, not from later UI changes.
- **AssemblyAI pre-recorded model selection**: use the current `speech_models`
  parameter for batch/import requests. `universal-3-pro` is sent with
  `universal-2` fallback; legacy `best`/`nano` settings are migrated to the
  current default in settings persistence and are not shown in the UI.
- **AltGr hotkey alias**: Windows reports AltGr as Ctrl+Alt. The hotkey
  manager ignores Ctrl+Alt hotkey messages while the right Alt key is down so
  AltGr combinations do not trigger dictation accidentally.
- **Line endings**: Repository text files are normalized to LF via `.gitattributes`; `.editorconfig` mirrors that policy so Windows/WSL edits do not create CRLF-only diffs.
- **Windows packaging**: end-user builds are layered. PyInstaller `onedir`
  is the base portable bundle; Inno Setup wraps that bundle into the
  installer; GitHub Actions builds artifacts manually on demand and publishes
  only on version tags. Official `v*` release tags must match
  `pyproject.toml`'s project version and must not be older than an existing
  numeric release tag. Standard releases should use
  `python scripts/create_release.py` from a clean, up-to-date `main`; the script
  prompts for the version, bumps metadata, runs checks, commits, pushes, tags,
  and pushes the tag.
- **Experimental ONNX/WebGPU local ASR**: Cohere Transcribe and IBM Granite
  Speech are selectable local models through `transcriber/local_webgpu_asr.py`.
  They are batch-only, use q4 ONNX snapshots, require Node.js plus
  `@huggingface/transformers`, and try WebGPU, then Windows DirectML, then CPU.
  They are not preloaded and are closed after normal batch dictation to avoid
  idle ONNX/Node CPU load.
  The resolved runtime device is reported through transcriber progress messages
  so the overlay/import UI can show whether WebGPU, DirectML, or CPU was used.
  Keep faster-whisper as the stable local default until real target-hardware
  benchmarks justify switching.
- **Streaming availability**: `config.supports_streaming()` is the shared
  source of truth for UI and controller checks. Local ONNX/WebGPU models are
  batch-only, but that local model selection must not disable remote provider
  streaming for AssemblyAI or Deepgram.
- **Streaming text state**: Keep provider partial-text reconciliation in
  `streaming_text.py`. The controller may expose thin compatibility wrappers,
  but it should only orchestrate Qt/audio/focus/insertion side effects.
- **Last recording selection**: `LastRecordingStore.selectable_path()` is the
  single selection point for "Use last recording". When an archived recordings
  directory is supplied, it chooses the newest managed/archive WAV, but
  recoverable managed recordings still win so retry/recovery state remains
  intact.

## Core flow

1. Global hotkey toggles recording.
2. Overlay: `Idle → Listening → Processing → Done/Error`.
3. Batch mode: recorded WAV transcribed on stop.
4. Streaming mode (local, AssemblyAI, Deepgram): live chunks with partial text and incremental insertion.
5. Text inserted at caret via clipboard-safe paste; clipboard restored.

## Engines

- **VALID_ENGINES**: local, assemblyai, openai, groq, deepgram, elevenlabs
- **STREAMING_ENGINES**: local, assemblyai, deepgram (others are batch-only)
- All engine/model constants defined in `config.py`

## Tests

- Preferred on Windows: `.venv\Scripts\pytest.exe -q`
- Alternate when the environment supports it: `uv run python -m pytest` or `python -m pytest`
- Note: the project uses a uv-managed Windows `.venv`; `pytest.exe` may be available even when `python -m pytest` or `python -m pip` is not.

## Known limitations

- Streaming: recent live tail is revisable, but older locked text is not rewritten; focus-change abort remains best-effort.
- ARM CPUs: not supported (CTranslate2 requires x86 AVX/SSE).
- Clipboard restore: Unicode text only.
- NVIDIA Parakeet is intentionally not implemented through NeMo; see
  `docs/local-asr-model-candidates-2026.md` for rationale.
