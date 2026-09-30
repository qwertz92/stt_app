# Build, release and tooling: design decisions

Binding project rules, moved verbatim from `AGENTS.md` on 2026-09-30 so that
the always-loaded agent file stays small. Read this file before changing
packaging, release scripts, CI gates, ruff, diagnostics scripts and test-suite invariants. Entries keep their original order, so "the entry above/below" still
refers to this file; "Known limitations" is `docs/agents/known-limitations.md`.
History is in `docs/learning-log.md`.

- **Line endings**: Repository text files are normalized to LF via `.gitattributes`; `.editorconfig` mirrors that policy so Windows/WSL edits do not create CRLF-only diffs.
- **Windows packaging**: end-user builds are layered. PyInstaller `onedir`
  is the base portable bundle; Inno Setup wraps that bundle into the
  installer; GitHub Actions builds artifacts manually on demand and publishes
  only on version tags. Official `v*` release tags must match
  `pyproject.toml`'s project version and must not be older than an existing
  numeric release tag. Standard releases should use
  `python scripts/create_release.py` from a clean, up-to-date `main`; the script
  prompts for the version, bumps metadata, runs checks, commits when metadata
  changed, pushes, tags, and pushes the tag. GitHub Actions release notes that
  contain Markdown backticks must use a literal PowerShell here-string (`@'`) so
  asset-name backticks are not consumed as PowerShell escapes.
  The release workflow publishes `stt_app-win-x64-setup.exe.sha256`, generated
  only after the final installer bytes. Authenticode signing must run before
  that checksum step once a managed signing identity is configured.
- **Continuous quality gates**: `.github/workflows/quality.yml` runs Ruff and
  the complete pytest suite on Windows for `main`, review branches, and pull
  requests. It also audits the locked production JavaScript dependency tree on
  Linux. Keep release publishing separate in `windows-release.yml`.
- **Release builds are locked and prevalidated**: Windows builds use
  `uv sync --locked` and `npm ci`, then run Ruff, all tests, and the production
  dependency audit. Release creation rejects tracked and untracked worktree
  changes. Version bumping prepares every metadata edit before writing and
  rolls back earlier files if a later atomic write fails.
- **Portable Node bootstrap security**: `scripts/setup_node_windows.py` accepts
  numeric `major.minor.patch` versions only, verifies every downloaded archive
  against that release directory's `SHASUMS256.txt`, and rejects ZIP members
  that escape the selected install directory. Keep all three checks when
  changing download mirrors or extraction behavior.
- **Release script behavior**: `scripts/create_release.py` can tag an already
  bumped current project version when it is newer than the latest numeric
  release tag. It commits release metadata only when files actually changed, so
  a pre-bumped `0.4.0` main can still be released as `v0.4.0` without a dummy
  bump commit.
- **Ruff's rule set is written out, never inherited**: `pyproject.toml` names
  every selected rule and every ignore with its reason. Before this ruff ran on
  its bare defaults, so the CI gate checked pyflakes and a handful of
  pycodestyle errors only — a naive-datetime elapsed counter, three unchecked
  `zip()` length assumptions and loop-variable closures all passed it. A ruff
  upgrade must be allowed to surface new findings; it must never silently
  change what the gate means.
- **`tests/conftest.py` blocks the real `create_transcriber`.** The isolated
  arm of `_acquire_transcriber_runtime` -- taken whenever the shared lock is
  already held -- calls the module-level function, so the 27 patch sites that
  replace `_get_or_create_transcriber` do not cover it, and a test slipping
  onto that arm builds a real provider client or a real local runtime that
  downloads its model. The fixture raises a named `AssertionError`; the 64
  tests that patch `stt_app.controller.create_transcriber` themselves are
  unaffected, because `monkeypatch` applies theirs afterwards.
- **`_start_streaming_recording` has two capture-failure arms, and a test that
  fails `_build_audio_capture` reaches only the first.** That call returns
  before `capture.start()` exists, so the `AudioCaptureError` arm below it is
  never entered; reverting its guard left the whole suite green. A test for
  that arm needs a capture object that builds and then refuses to `start()`,
  which is what a microphone held by another application does. Same shape for
  `_acquire_transcriber_runtime`: the isolated branch and the shared branch
  need separate tests, and only the shared one holds
  `_transcriber_runtime_lock`.
- **`scripts/smoke_test.py` must never touch the real settings file**:
  `SettingsStore.load` is not a read -- it creates the file and a `.bak` when
  none exists, rewrites it whenever the stored payload differs from the
  normalized one, and renames both the file and its backup to
  `*.corrupt.<timestamp>` when the JSON will not parse. A diagnostic that
  quarantines a user's configuration is worse than no diagnostic, so it loads a
  throwaway copy. Its model step also skips every non-`local` engine: the
  transcriber is built without a secret store there and dies with "API key is
  missing", and no remote provider implements `preload_model` at all.
  **And it must not move the folder the settings live in.** Loading a model
  reaches `appdata_root()` -- measured chain: `preload_model` ->
  `_coordinated_download_if_missing` -> `run_coordinated_download` ->
  `acquire` -> `_acquire_cache_lock` -> `_download_lock_dir` -- which is a
  *setup* call that renames a legacy `tts_app` install onto the current name.
  So the commit that stopped the script touching `settings.json` reintroduced
  the same side effect one level out, through a call chain no grep of the
  script reveals, and made it *more* likely by adding a fresh-install branch
  that reaches the model step where the old code returned early.
  `_legacy_data_folder_would_be_moved()` now declines the step and says why.
  Creating a data folder that does not exist yet is left alone: it holds
  nothing of the user's, and refusing to create it would strand a legacy
  install forever -- an empty `stt_app` beside `tts_app` is exactly the state
  in which `appdata_root()` stops migrating.
- **A diagnostic must read the file it proved it can read, not the one it
  was handed.** The settings reader falls through to the `.bak` when the
  primary raises `OSError`, and then copied *the primary* into the sandbox --
  reading again the file that had just refused, so every `OSError` the backup
  exists to survive came back as a crash and `--strict` 1 for an install the
  app starts fine on. Tracking `usable_path` removes the failure mode instead
  of guarding it: the file that parsed cannot fail to be copied for that
  reason.
- **When the settings are unusable, check the defaults -- do not check
  nothing.** `SettingsStore.load` quarantines a file that will not parse and
  writes defaults, so the app runs on the default model. Reporting the
  problem and skipping the model check made `--check-model` verify nothing on
  exactly the broken install it exists for, while the script's own message
  already said "the app will discard it".
- **The suite's Hugging Face isolation lives in `pytest_configure`, not a
  fixture**: `huggingface_hub` computes `HF_HUB_CACHE` and `HF_HUB_OFFLINE` at
  **import**, and a test module imports it at module scope, which pytest does
  during collection -- before any fixture runs. Set from a fixture they did
  nothing at all: measured after collection, the constants still pointed at the
  developer's real `~/.cache/huggingface/hub` with offline False, and
  `download_model_snapshot` passes no `cache_dir` when Model Dir is empty, so
  those frozen constants decide where a download lands. One session directory,
  not one per test: `tmp_path_factory` rescans its base directory on every
  call. The per-test `_coordinated_download_if_missing` stub is the half that
  does belong in a fixture, and `real_model_prefetch` restores it for the two
  files that assert the pre-fetch happens.
- **Every string a script can print is ASCII**, enforced for all of `scripts/`
  by `tests/test_script_output_is_ascii.py`. Redirected output on Windows is
  cp1252: a character outside it raises `UnicodeEncodeError` on stdout (this
  crashed `--validate-only` on a *valid* model) and is escaped to a literal
  `\uXXXX` on stderr (this wrapped the SSL-proxy guidance in two walls of
  `\u2550`). A character *inside* cp1252, such as an em dash, still renders as
  U+FFFD wherever the log is opened as UTF-8. Comments and docstrings are
  exempt -- except a module docstring in a script that *reads* `__doc__`,
  which is then on its way to stdout; the test computes that per file.
  Matching `ArgumentParser(description=__doc__)` was too narrow and missed
  `print(__doc__)`, which is how four em dashes stayed in
  `experiment_native_tray_icon.py`'s banner. Any load of the name now counts:
  it subsumes the argparse case, cannot produce a false negative, and the
  cost of a false positive is only that one more docstring stays ASCII.
