# Build, release and tooling: design decisions

Binding rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30; history and full measurements are in `docs/learning-log.md` and
git history. Read before changing packaging, release scripts, CI gates, ruff,
diagnostics scripts or test-suite invariants. "Above/below" refers to this
file; "Known limitations" is `docs/agents/known-limitations.md`.

- **Line endings**: LF via `.gitattributes`, mirrored by `.editorconfig`.
- **Packaging is layered**: PyInstaller `onedir`, wrapped by Inno Setup;
  Actions builds on demand and publishes only on version tags. `v*` tags match
  `pyproject.toml`'s version and are not older than an existing numeric tag.
  Release with `python scripts/create_release.py` from a clean, current
  `main`; it can tag an already bumped newer version and commits metadata only
  when files changed. Release notes with backticks use a literal PowerShell
  here-string (`@'`). `stt_app-win-x64-setup.exe.sha256` is made after the
  final installer bytes; future Authenticode signing goes before it.
- **Gates**: `.github/workflows/quality.yml` runs Ruff and the suite on
  Windows (`main`, review branches, PRs) plus the locked JS audit on Linux;
  publishing stays in `windows-release.yml`. Release builds:
  `uv sync --locked`, `npm ci`, Ruff, tests, audit; any worktree change is
  rejected; version bumping prepares all edits and rolls back on a failed
  write.
- **`scripts/setup_node_windows.py`**: numeric `major.minor.patch` only,
  archives verified against `SHASUMS256.txt`, escaping ZIP members rejected.
  Keep all three.
- **Ruff's rule set is written out, never inherited**: `pyproject.toml` names
  every rule and every ignore with its reason (defaults checked almost
  nothing). An upgrade may add findings; it must not silently change the gate.
- **`tests/conftest.py` blocks the real `create_transcriber`**: the isolated
  arm of `_acquire_transcriber_runtime` calls it directly, past patches of
  `_get_or_create_transcriber`. Tests patching
  `stt_app.controller.create_transcriber` still win (`monkeypatch` order).
- **`_start_streaming_recording` has two capture-failure arms**; failing
  `_build_audio_capture` reaches only the first. The `AudioCaptureError` arm
  needs a capture that builds and refuses `start()`. The isolated and shared
  branches of `_acquire_transcriber_runtime` need separate tests (only the
  shared one holds `_transcriber_runtime_lock`).
- **`scripts/smoke_test.py` must never touch the real settings or move the
  data folder**: `SettingsStore.load` writes and quarantines
  (`*.corrupt.<timestamp>`), so it loads a throwaway copy; the model step
  skips non-`local` engines. Loading a model reaches `appdata_root()` via
  `preload_model` -> `_coordinated_download_if_missing` ->
  `run_coordinated_download` -> `acquire` -> `_acquire_cache_lock` ->
  `_download_lock_dir`, which migrates `tts_app`, so
  `_legacy_data_folder_would_be_moved()` declines the step. Creating a missing
  folder is allowed (refusing would strand a legacy install).
- **A diagnostic reads the file it proved readable** (`usable_path`), not the
  primary that just raised; with unusable settings `--check-model` checks the
  defaults the app will run on, not nothing.
- **Hugging Face isolation lives in `pytest_configure`, not a fixture**:
  `huggingface_hub` freezes `HF_HUB_CACHE` and `HF_HUB_OFFLINE` at import
  (during collection), and `download_model_snapshot` passes no `cache_dir` for
  an empty Model Dir. One session directory (`tmp_path_factory` rescans per
  call). The `_coordinated_download_if_missing` stub is per test;
  `real_model_prefetch` restores it where the pre-fetch is asserted.
- **Every string a script can print is ASCII**
  (`tests/test_script_output_is_ascii.py`): redirected Windows output is
  cp1252, so other characters raise `UnicodeEncodeError` (crashed
  `--validate-only`) or print as `\uXXXX`, and in-cp1252 ones like an em dash
  show as U+FFFD in UTF-8 logs. Comments and docstrings are exempt, except a
  module docstring in a script that loads `__doc__` at all (e.g.
  `print(__doc__)` in `experiment_native_tray_icon.py`).
