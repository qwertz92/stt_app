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
- ONNX Runtime GenAI for Nemotron 3.5 cache-aware local streaming
- Remote providers: AssemblyAI (SDK batch + Universal-Streaming v3),
  OpenAI (REST API), Groq (SDK), Deepgram (REST + WebSocket),
  ElevenLabs (REST API), Azure LLM Speech / MAI-Transcribe (REST, batch-only),
  Fun-ASR / Alibaba (DashScope WebSocket, batch-only, no German)
- keyring for secret storage

## Architecture

### Module responsibilities

| Module | Purpose |
| ------ | ------- |
| `config.py` | All tunables/constants; `MODEL_REPO_MAP` (single source of truth) |
| `controller.py` | Main orchestrator/state machine; hotkey, audio, transcriber, overlay, inserter, history, preload |
| `streaming_text.py` | Pure streaming text normalization, locked-prefix, live-tail, and finalization logic |
| `audio_capture.py` | sounddevice mic recording + VAD auto-stop + streaming chunk callback |
| `transcriber/local_faster_whisper.py` | Batch + streaming via faster-whisper; `find_cached_models`; `preload_model`; cooperative batch cancel via `set_cancel_check` |
| `transcriber/local_nemotron.py` | Batch + true cache-aware streaming for Nemotron 3.5 INT4 via ONNX Runtime GenAI |
| `transcriber/local_webgpu_asr.py` | Shared local ONNX inventory/download helpers plus the batch-only Cohere/Granite Node.js runtime (supported daily-use GPU models) |
| `transcriber/assemblyai_provider.py` | Batch + streaming via AssemblyAI SDK |
| `transcriber/openai_provider.py` | Batch via OpenAI API |
| `transcriber/groq_provider.py` | Batch via Groq SDK |
| `transcriber/deepgram_provider.py` | Batch via REST + streaming via WebSocket |
| `transcriber/elevenlabs_provider.py` | Batch via ElevenLabs REST API |
| `transcriber/azure_provider.py` | Batch via Azure LLM Speech fast-transcription REST (enhanced mode / MAI-Transcribe); needs endpoint + key |
| `transcriber/funasr_provider.py` | Batch via Alibaba Fun-ASR over the DashScope realtime WebSocket (key-only; no German) |
| `transcriber/factory.py` | Creates transcriber from settings; routes engine to provider |
| `text_inserter.py` | Clipboard-safe paste: save > set > paste > restore |
| `overlay_ui.py` | Always-on-top frameless overlay with state colors, controls, opacity slider, transcription queue panel |
| `settings_dialog.py` | PySide6 settings UI with Local/Remote/History tabs, model management |
| `settings_store.py` | JSON settings persistence (`%APPDATA%\stt_app\settings.json`) |
| `ui_feedback.py` | Shared Qt button feedback styles, stable feedback widths, scroll restoration helpers |
| `local_model_inventory_store.py` | Persistent cache of last-known local model inventories keyed by `model_dir` |
| `local_model_download.py` | Cancellable source/packaged worker-process launcher for local model downloads |
| `model_download_progress.py` | Shared approximate model download percent and transfer-rate calculation |
| `secret_store.py` | keyring wrapper for API keys with optional insecure plain-text fallback for restricted environments |
| `transcript_history.py` | Persistent transcript history store (JSON) with import/export |
| `history_dialog.py` | History dialog with table view, copy, export/import, clear, limit control |
| `app_paths.py` | Centralized app data/config path helpers |
| `vad.py` | Energy-based voice activity detection with configurable threshold |
| `window_focus.py` | Win32 foreground/focus/caret window tracking for text insertion |
| `hotkey.py` | Global hotkey registration via Win32 RegisterHotKey |
| `benchmark_environment.py` | Best-effort benchmark system metadata |
| `scripts/import_model.py` | Import manually downloaded models; validates for Git LFS pointers |
| `scripts/download_model.py` | Automated model download for offline/corporate use |

### Key design decisions

- **Temp files for audio**: `transcribe_batch` writes WAV to temp file because `WhisperModel.transcribe()` is most reliable with file paths.
- **GUITHREADINFO duplication**: defined in both `text_inserter.py` and `window_focus.py`. Intentional — modules are self-contained.
- **SendInput restore delay (160ms)**: Empirical value. Some apps (Electron/Chrome) read clipboard asynchronously 50-100ms after Ctrl+V. 160ms prevents stale paste.
- **Local model inventory cache**: last-known local model lists are stored in a dedicated JSON cache file, not `settings.json`, so the Local tab can render immediately without silently mutating user settings.
  Cached inventories are used for initial Local/Benchmark tab rendering, then
  disk verification starts automatically after the tab has had a chance to
  paint. App startup also refreshes the persistent inventory in the background.
  Source-tree and packaged runs isolate that scan in a subprocess so Python
  filesystem work cannot stall the Qt UI thread.
  Settings dialog lifecycle, tab paint, inventory render, and inventory scan
  timings are logged as `settings_timing` diagnostics for later troubleshooting.
  Local/Benchmark list widgets intentionally keep `AdjustToContents`; if first
  paint regresses again, use the timing diagnostics before changing this policy.
  The tray schedules a hidden settings-dialog preparation after startup so the
  first visible open and first Local tab paint avoid lazy Qt layout work. A
  hidden prepared dialog reloads settings from disk before it is shown.
- **Qt dialog feedback and refresh state**: transient button text such as
  "Copied" must reserve enough width for all feedback states via
  `ui_feedback.py` so layouts do not jump. Dialog/list refreshes should preserve
  selection, current item, and scroll position when the same entry still exists;
  use the shared scroll helper instead of rebuilding lists in a way that resets
  the user's place.
- **Local model download queue**: Settings downloads run serially through one
  worker process so Hugging Face cache writes and network usage remain
  predictable and the active download can be terminated safely. Additional
  models can be queued while a download is active. Cancel clears the queue and
  removes unusable `*.incomplete` files while preserving completed files for a
  later resume. Progress and its rolling transfer rate are approximate because
  they are derived from cache growth and the estimated total sizes in
  `MODEL_ESTIMATED_SIZE_MB`.
- **Transcript history retention**: history defaults to 500 saved entries, and
  legacy settings that still have the old 20-entry default are migrated upward.
  Successful transcriptions are added to history before text insertion, so a
  paste/focus failure does not drop the transcript. The stored model name comes
  from the transcription settings snapshot, not from later UI changes.
- **AssemblyAI pre-recorded model selection**: use the current `speech_models`
  parameter for batch/import requests. `universal-3-pro` is sent with
  `universal-2` fallback; legacy `best`/`nano` settings are migrated to the
  current default in settings persistence and are not shown in the UI.
- **AssemblyAI streaming (Universal-Streaming v3)**: the legacy v2 realtime
  API is retired and must not be reintroduced. Streaming uses
  `assemblyai.streaming.v3.StreamingClient` with the
  `universal-streaming-multilingual` model, language detection, and formatted
  turns; the batch model selection does not apply to streaming. Turn text is
  keyed by `turn_order` because the formatted end-of-turn transcript arrives
  as a second event for the same turn. Bound SDK `disconnect` joins with a
  helper thread; they can hang on dead connections.
- **Streaming provider sends must not block the audio callback**:
  `push_audio_chunk` runs on the PortAudio callback thread. Providers must
  only enqueue there (Deepgram has a dedicated sender thread; the AssemblyAI
  SDK and local transcribers queue internally) and never perform blocking
  socket I/O.
- **Deepgram streaming language**: the live WebSocket API rejects
  `detect_language`; auto maps to `language=multi` (nova-2/nova-3
  multilingual code-switching). Batch keeps `detect_language=true`.
- **AltGr hotkey alias**: Windows reports AltGr as Ctrl+Alt. The hotkey
  manager ignores Ctrl+Alt hotkey messages while the right Alt key is down so
  AltGr combinations do not trigger dictation accidentally.
- **Overlay visibility after activity/resume**: every recording start
  re-presents the overlay without activation and reasserts native Windows
  topmost z-order. `WM_POWERBROADCAST` resume events also restore overlay
  visibility and refresh both global hotkey registrations after display/session
  state has stabilized.
- **Model-aware language selection**: `config.language_modes_for_selection()`
  is the shared source of truth for the General-tab language list and provider
  validation. Auto remains the persisted default where supported; Cohere
  requires an explicit language.
- **Remote first-request diagnostics**: transcription workers log
  `transcription_timing` with initialization, transcription, and total
  durations. Groq reuses its SDK/HTTP client for the lifetime of the cached
  transcriber so later requests can reuse connections.
- **Line endings**: Repository text files are normalized to LF via `.gitattributes`; `.editorconfig` mirrors that policy so Windows/WSL edits do not create CRLF-only diffs.
- **Windows packaging**: end-user builds are layered. PyInstaller `onedir`
  is the base portable bundle; Inno Setup wraps that bundle into the
  installer; GitHub Actions builds artifacts manually on demand and publishes
  only on version tags. Official `v*` release tags must match
  `pyproject.toml`'s project version and must not be older than an existing
  numeric release tag. Standard releases should use
  `python scripts/create_release.py` from a clean, up-to-date `main`; the script
  prompts for the version, bumps metadata, runs checks, commits when metadata
  changed, pushes, tags, and pushes the tag.
- **Local ONNX ASR**: Cohere Transcribe, IBM Granite Speech 4.0,
  and IBM Granite Speech 4.1 are selectable local models through
  `transcriber/local_webgpu_asr.py`. They are batch-only and require Node.js.
  These are supported daily-use models, not experimental trials; do not
  reintroduce "experimental" framing in UI labels or user-facing model docs.
  Cohere, Granite 4.0, and Granite 4.1 2B use q4 ONNX snapshots through the
  high-level Transformers.js `GraniteSpeechForConditionalGeneration` pipeline.
  Granite 4.1 2B points at `onnx-community/granite-speech-4.1-2b-ONNX` (verified
  on WebGPU / Arc A750 on 2026-06-17: correct de/en/fr, no `Einsum` crash).
  Granite 4.1 **Plus** and **NAR** stay on raw INT8 `onnxruntime-node` graph
  sessions because they are different architectures (`granite_speech_plus` /
  `granite_speech_nar`) with no faithful q4 Transformers.js package — see
  `docs/granite-speech-4.1-onnx-variants.md` for the full status, the three
  blockers, and what would change that. Do not relabel a Plus build as base
  `granite_speech` to force it onto the pipeline path: that produces broken
  English (verified with the valoomba build).
  The raw Granite 4.1 Plus/NAR graphs run through `onnxruntime-node` execution
  providers: `webgpu_asr_runner.mjs` `ortExecutionProviders` returns
  `webgpu`/`dml`/`cpu`, so `auto`/`gpu` mode tries WebGPU, then DirectML, then
  CPU. DirectML ships with `onnxruntime-node` on Windows. GPU acceleration of
  these raw graphs is unverified (WebGPU `Einsum` shader bug, DirectML operator
  gaps) and they usually run on CPU; the active device is reported in the runtime
  status. This raw path is separate from the Cohere / Granite 4.0 / Granite 4.1
  2B Transformers.js pipeline path.
  They are not preloaded and are closed after normal batch dictation to avoid
  idle ONNX/Node CPU load.
  The resolved runtime device is reported through transcriber progress messages
  so the overlay/import UI can show whether WebGPU, DirectML, or CPU was used.
  Keep faster-whisper as the stable local default until real target-hardware
  benchmarks justify switching.
  The Granite 4.1 Plus (AR) and NAR raw paths must stay separate from each other
  and from the pipeline path because their ONNX graph contracts differ. Keep
  `granite-4.0-1b-speech` selectable as a smaller q4 option until real
  benchmarks justify removing it.
- **Nemotron 3.5 true streaming**:
  `nemotron-3.5-asr-streaming-0.6b-int4` uses the published 793 MB multilingual
  ONNX Runtime GenAI export through `transcriber/local_nemotron.py`. It reuses
  the model's encoder cache and emits incremental RNNT tokens every fixed
  560 ms chunk instead of re-transcribing a rolling window. The published ONNX
  graph is fixed to 560 ms even though the original NeMo model supports other
  latency profiles. The app ships the installable CPU ORT GenAI package and
  tries DirectML first when a compatible DirectML runtime is present. As of
  2026-06-08, Microsoft's DirectML GenAI package depends on an unpublished
  `onnxruntime-directml>=1.26.0`, so reproducible installs fall back to CPU.
  A Ryzen 5 7600X benchmark measured 0.229 RTF and 0.81 s cold load on the
  repository sample. Nemotron stays preloaded and cached like faster-whisper so
  pressing the recording hotkey does not block on model loading. Its internal
  runtime VAD follows the app's VAD setting. The language UI exposes only the
  transcription-ready and broad-coverage official prompt IDs.
- **Streaming availability**: `config.supports_streaming()` is the shared
  source of truth for UI and controller checks. Cohere/Granite ONNX/WebGPU
  models are batch-only; Nemotron is true streaming. A local model selection
  must not disable remote provider streaming for AssemblyAI or Deepgram.
- **Streaming text state**: Keep provider partial-text reconciliation in
  `streaming_text.py`; the controller should only orchestrate
  Qt/audio/focus/insertion side effects.
  Streaming insertion is append-only: do not use live partial revisions to
  select/delete previously inserted text.
  Local rolling-window partials may be merged by safe word overlap, but only to
  append new text.
- **Streaming finalization**: the full re-transcription of the recording when
  local faster-whisper streaming stops is opt-in via
  `streaming_full_final_transcript` (default off). When off, finalization
  transcribes only the trailing partial window and merges it into the
  provider-tracked live transcript, so stop returns quickly and the history
  entry matches the streamed text. Inserted text stays append-only either way.
- **Concurrent transcription mode + cooperative cancel**: a finished
  transcription is *never* discarded. `concurrent_transcription_mode`
  (`insert` default / `history` / `cancel`) decides what happens to the
  in-flight transcription when a new recording starts: `insert` keeps it and
  inserts its result into the window that was focused when it was recorded
  (plus history); `history` keeps it but only saves to history; `cancel`
  requests a real stop and, if it still finishes, keeps it in history. Local and
  remote share the single `max_workers=1` transcription executor, so jobs
  serialize — this only changes delivery, never runs two at once. Each recording
  snapshots its target window into a `_TranscriptionJob`; the job also carries
  `background_delivery` (`insert`/`history`) and `aborting`. A result is
  "foreground" only when its token is active, no newer recording is active, and
  the job is not aborting — `_new_recording_active()` intentionally excludes
  `_streaming_recording` because a pending streaming finalize keeps that flag
  True. Background results are delivered via `_handle_background_transcription_ready`
  per `background_delivery` (streaming finalize is always history-only). Never
  reset foreground session state from a background result handler.
  Explicit cancel — the overlay per-row ✕, Clear queue, and the Cancel button —
  goes through `_request_job_stop` (delivery `history`): it sets `aborting` (so a
  not-yet-started worker skips and a cooperative transcriber stops) and cancels
  the future if it has not started. Real mid-run abort exists for faster-whisper
  via `set_cancel_check` (polled between segments → raises `TranscriptionCanceled`
  → worker emits `transcription_canceled`); other engines only skip-if-not-started
  and otherwise run to completion with their result kept in history. The cancel
  hook must be cleared after each batch run so it cannot leak into the cached
  transcriber's next request.
- **Overlay corner vs. dragged position**: after a settings save, apply the
  corner through `OverlayUI.apply_corner_setting`, which repositions only when
  the configured corner changed. Never call `move_to_corner` unconditionally
  on save; it would discard a manually dragged overlay position.
- **App icon**: `src/stt_app/assets/app_icon.ico`/`.png` are generated by
  `scripts/generate_app_icon.py` and committed. The icon is wired into the
  Qt window/tray icons (with a standard-icon fallback), the wheel, the
  PyInstaller bundle/EXE, and the Inno Setup installer. Rerun the script only
  when the design changes.
- **Release script behavior**: `scripts/create_release.py` can tag an already
  bumped current project version when it is newer than the latest numeric
  release tag. It commits release metadata only when files actually changed, so
  a pre-bumped `0.4.0` main can still be released as `v0.4.0` without a dummy
  bump commit.
- **Last recording selection**: `LastRecordingStore.selectable_path()` is the
  single selection point for "Use last recording". When an archived recordings
  directory is supplied, it chooses the newest managed/archive WAV, but
  recoverable managed recordings still win so retry/recovery state remains
  intact.
- **Benchmark environment metadata**: benchmark summaries and exports include a
  best-effort system context from `benchmark_environment.py`. Keep hardware,
  OS, Python, Node.js, and local runtime/framework version collection there so
  Settings, history exports, and the CLI benchmark do not drift. ONNX benchmark
  cases also persist concise runtime fallback details so a CPU result explains
  why WebGPU or DirectML was rejected.

## Core flow

1. Global hotkey toggles recording.
2. Overlay: `Idle → Listening → Processing → Done/Error`.
3. Batch mode: recorded WAV transcribed on stop.
4. Streaming mode (local, AssemblyAI, Deepgram): live chunks with partial text
   and append-only stable insertion. Nemotron local streaming is cache-aware;
   faster-whisper local streaming uses rolling windows.
5. Text inserted at caret via clipboard-safe paste; clipboard restored.

## Engines

- **VALID_ENGINES**: local, assemblyai, openai, groq, deepgram, elevenlabs,
  azure, funasr
- **STREAMING_ENGINES**: local, assemblyai, deepgram (others are batch-only)
- **Azure LLM Speech** needs two settings: `azure_endpoint` (per-resource, e.g.
  `https://<resource>.cognitiveservices.azure.com`) and the `azure` key in the
  secret store. Model select picks `mai-transcribe-1.5` / `mai-transcribe-1`.
- **Fun-ASR (Alibaba)** is key-only (`funasr` key, Singapore-region DashScope),
  driven over the realtime WebSocket in batch mode. It covers 31 languages but
  **not German** (`FUNASR_LANGUAGE_MODES` excludes `de`).
- All engine/model constants defined in `config.py`

## Tests

- Preferred on Windows: `.venv\Scripts\pytest.exe -q`
- Alternate when the environment supports it: `uv run python -m pytest` or `python -m pytest`
- Note: the project uses a uv-managed Windows `.venv`; `pytest.exe` may be available even when `python -m pytest` or `python -m pip` is not.

## Known limitations

- Streaming: inserted text is append-only and never rewritten; focus-change
  abort remains best-effort.
- ARM CPUs: not supported (CTranslate2 requires x86 AVX/SSE).
- Clipboard restore: Unicode text only.
- NVIDIA Parakeet remains intentionally unimplemented through NeMo; Nemotron
  uses the separate ONNX Runtime GenAI path. See
  `docs/local-asr-model-candidates-2026.md` for rationale.
