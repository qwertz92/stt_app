# Testing: detailed rules

Condensed from the `Tests` section moved out of `AGENTS.md` on 2026-09-30;
history and full measurements are in `docs/learning-log.md` and git history.
The commands every change needs are in `AGENTS.md`; this file holds the
release-workflow rule, the `scripts/release_check_*.py` contract, the autouse
fixtures in `tests/conftest.py` and the CI-runner rules. Read it before adding
a fixture, a layout test, a subprocess test or a release check.

Verbatim pre-condensation text: `git show e608f86:docs/agents/testing.md` (original AGENTS.md: `df2642a`).

- **The release workflow tests with `quality.yml`'s command on a real
  desktop** (since 2026-09-18): under `QT_QPA_PLATFORM=offscreen` tests marked
  `pixel_exact` / `platform_dependent` skip and unmarked layout tests rotted
  (v0.9.0 dry run: 7 failures; v0.8.0 hung six hours, hence
  `faulthandler_timeout`). A new geometry test still needs a marker for
  headless machines (check: `QT_QPA_PLATFORM=offscreen pytest <file>`); no CI
  gate depends on them. **Before a release tag run
  `gh workflow run windows-release.yml`** (artifact only, publishes nothing):
  the only run of the bundle and installer steps.
- **Four `scripts/release_check_*.py` scripts measure what the suite fakes**,
  run by hand before a release, never by pytest or CI:
  `release_check_file_locks.py` (23 checks, share-mode-0 locks),
  `release_check_clipboard_paste.py` (19: format round trip, Explorer
  cut/copy, WM_PASTE and SendInput into a real EDIT, deferred restore,
  foreground guard), `release_check_providers.py` (AssemblyAI batch/realtime,
  Groq batch, quit during the poll), `release_check_frozen_bundle.py` (scan
  worker, one model per runtime, GUI 40 s); plumbing in
  `scripts/_release_check_common.py`. Contract: one `OK`/`FAIL`/`SKIP` line
  per check; exit 0/1/2, 2 = nothing measured, never a code failure; an
  uncaught exception or Ctrl+C becomes a failed check via `run_main`, so the
  `SUMMARY` line always exists; children that may spawn children run through
  `run_child`, which kills the tree on timeout (`subprocess.run` waits for
  every pipe inheritor); ASCII names and details; a throwaway `APPDATA` set
  before the first `stt_app` import, `HF_HUB_OFFLINE=1`, no machine paths;
  keys only via `KeyringSecretStore`, never printed; the user's clipboard
  captured first, restored in a `finally`, reported as format ids and sizes
  only. All passed 2026-09-18; commands in `docs/windows-distribution.md`.
  Not covered: running the installer or an upgrade.
- **Autouse fixtures in `tests/conftest.py`**, each raising a named
  `AssertionError` or resetting state:
  - `_forbid_handing_paths_to_the_desktop_shell` (`QProcess.startDetached`,
    `QDesktopServices.openUrl`) and `_forbid_blocking_modal_dialogs`
    (`QMessageBox` statics, `QFileDialog` getters): an unstubbed modal hangs
    a run silently. A test driving such a path patches it and asserts.
  - `_reset_the_transcription_shutdown_flag`: clears the process-wide flag
    around every test; any process-global state `shutdown()` sets needs the
    same (per-file runs hid 26 suite failures).
  - `_no_benchmark_worker_outlives_its_test`: fails a test leaving the
    `stt_app_local_benchmark` thread running (after 2-4 s of PowerShell it
    calls whatever `run_benchmark_cases` is installed, or the real launcher).
    Run-starting tests patch an immediate thread, the facade function and the
    environment query.
  - `_one_decoder_per_stream_session` (in `tests/test_transcriber.py`): fails
    when two threads decode partials for one session; only the stream worker
    calls `_maybe_emit_partial`, and a second caller double-counts
    `silent_seconds`. Push through `_push_and_decode`.
  - `_no_keyboard_modifier_outlives_its_test`: clears a modifier that
    `QTest.keyClick(..., ControlModifier)` on a widget without a native window
    leaves in `QGuiApplication.keyboardModifiers()`, which makes a later
    programmatic `selectRow` toggle. Clears rather than asserts (a real Ctrl
    can leak in). Trap: with `addopts = "-q"`, list ids with
    `-o addopts="" --collect-only -q`.
  - `_empty_local_model_scan_session_cache`: empties
    `_LOCAL_MODEL_SCAN_SESSION_CACHE` and its verified-dirs set (a cached scan
    replaced a later test's model list).
  - `_a_fresh_download_coordinator_per_test`: replaces `_COORDINATOR` (a
    leaked explicit interest `('large-v3', '')`).
  - `_silero_speech_check_unavailable`: makes the Silero speech check answer
    "unmeasurable" (`silero_vad._get_session` -> None), which every gate reads
    as "leave the energy decision standing". The suite drives the energy gates
    with tones and seeded noise that the real graph rightly calls non-speech.
    A test about the speech check takes the `real_silero` fixture, which
    restores the real loader and resets the cached session on both sides.
    Speech for those tests comes from `tests/speech_fixtures.py` (six
    LibriSpeech excerpts, CC BY 4.0, attribution in the `.npz`); never read
    the `.npz` as text.
  The alphabetical full suite hid the last three leaks; only another file
  order showed them.
- **`tests/conftest.py` blocks the real `create_transcriber`**: the isolated
  arm of `_acquire_transcriber_runtime` calls it directly, past patches of
  `_get_or_create_transcriber`. Tests patching
  `stt_app.controller.create_transcriber` still win (`monkeypatch` order).
- **`_start_streaming_recording` has two capture-failure arms**; failing
  `_build_audio_capture` reaches only the first. The `AudioCaptureError` arm
  needs a capture that builds and refuses `start()`. The isolated and shared
  branches of `_acquire_transcriber_runtime` need separate tests (only the
  shared one holds `_transcriber_runtime_lock`).
- **Hugging Face isolation lives in `pytest_configure`, not a fixture**:
  `huggingface_hub` freezes `HF_HUB_CACHE` and `HF_HUB_OFFLINE` at import
  (during collection), and `download_model_snapshot` passes no `cache_dir` for
  an empty Model Dir. One session directory (`tmp_path_factory` rescans per
  call). The `_coordinated_download_if_missing` stub is per test;
  `real_model_prefetch` restores it where the pre-fetch is asserted.
- **Gate a push on the printed `N passed` line**, never a pipeline's exit
  code; a chain pushing after a background suite *started* published a red
  suite (2026-09-04).
- **The CI runner (`quality.yml`, `windows-latest`, real desktop) is a fresh
  VM: under an hour of uptime, no microphone, ~1028x749 usable screen.**
  - "long ago" is `-interval`, never 0 (`last_partial_at = 0.0` is recent:
    `time.monotonic()` counts from boot);
  - a controller test that records uses `FakeCapture`;
  - a resizing test asks `screen().availableGeometry()` first, asserts the
    size the window HAS, and skips with the measured room if it cannot fit;
  - a table-geometry snapshot settles first (`_settle_table`: `QTest.qWait`
    plus a stability loop) at a size where rows do not fit, so a late
    scrollbar is covered;
  - a subprocess timeout bounds a hang, not speed (the Node probe in
    `test_local_webgpu_asr.py` flaked at 10 s; now a minute);
  - BLAS results use a measured tolerance, never `array_equal`: OpenBLAS picks
    kernels per CPU (`OPENBLAS_CORETYPE` moves Granite CTC features 1.19e-7)
    on a mixed fleet; the blockwise test uses 1e-6 (a real bug moves >1.4).
    Bit equality is for copies.
