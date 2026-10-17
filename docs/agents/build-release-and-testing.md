# Build, release and tooling: design decisions

Binding rules, condensed from the entries moved out of `AGENTS.md` on
2026-09-30; history and full measurements are in `docs/learning-log.md` and
git history. Read before changing packaging, release scripts, CI gates, ruff,
diagnostics scripts or test-suite invariants. "Above/below" refers to this
file; "Known limitations" is `docs/agents/known-limitations.md`.

Verbatim pre-condensation text: `git show e608f86:docs/agents/build-release-and-testing.md` (original AGENTS.md: `df2642a`).

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
- **`create_release.py` runs the workflow's `npm audit` before it changes
  anything** (2026-10-17). On 2026-10-10 `--version 0.12.1` pushed `main` and
  the tag, then "Windows Release" failed at `npm audit --omit=dev
  --package-lock-only --audit-level=moderate` on a sharp advisory
  (GHSA-wq5f-xc86-pv6w, fixed by `dc998a9`), leaving a tag with no release.
  The audit asks npm's live advisory database, so its verdict moves with no
  change to the repository: a green push-time run, or a pytest run a minute
  ago, says nothing about it.
  - **Where**: `_ensure_javascript_audit_passes`, right after the branch and
    clean-tree checks and before the version bump, so a failure has nothing to
    restore. It runs **whatever `--skip-tests` says** (seconds, and no other
    check can stand in for it).
  - **It fails closed**: a non-zero exit (an advisory, or an unreachable
    advisory database -- npm's output is printed above the message) and an
    `npm` that is not on PATH both stop the release with a message that says
    nothing was changed, committed or tagged. A skipped audit is the gap this
    closes, so a missing npm is never skipped. npm is resolved with
    `shutil.which` (on Windows it is `npm.cmd`, which CreateProcess does not
    find as `npm`).
  - **One command, three copies**: `NPM_AUDIT_ARGS` in the script,
    `windows-release.yml` and `quality.yml`. `tests/test_create_release.py`
    compares all three (`quality.yml`'s own comment already asked for them to
    match) and drives `create_release` with every subprocess faked, so no test
    tags or pushes.
- **A release is dry-run on `main` first and tagged only after that is green**
  (2026-10-17; this is procedure, nothing in `create_release.py` checks it).
  The v0.12.0 build failed at a test that expected a 1026 px dialog on the
  runner's 976 px screen (`docs/agents/testing.md`): the CI runner is a fresh
  VM with a ~1028x749 desktop, so no local run, and no `create_release.py`
  check, can catch that class; only a build on the runner can.
  `windows-release.yml` has `workflow_dispatch` (no inputs) for exactly that.
  What a dispatch run does, read from the YAML:
  - **It runs every step of the tag run** -- locked Python and JavaScript
    installs, `ruff check`, `ruff format --check`, the whole `pytest`, the
    `npm audit`, the Inno Setup install, the portable bundle, the installer
    and its checksum -- on whichever ref it is started on (`gh workflow run
    windows-release.yml --ref main`, then `gh run watch`).
  - **It skips the two steps guarded by `startsWith(github.ref,
    'refs/tags/v')`**: "Verify release tag matches project version" and
    "Publish GitHub release assets". It adds one guarded by
    `github.event_name == 'workflow_dispatch'`: "Upload manual workflow
    artifacts" keeps the ZIP, installer and checksum as the artifact
    `stt_app-windows-release` for three days. So nothing is published and no
    release or tag is created. (The workflow's own comment records the dry
    run for v0.9.0 failing on seven layout tests, run 35378290905.)
  - **Never dispatch with a tag as the ref**: `github.ref` is then
    `refs/tags/v...`, and the run takes the tag path, verifies and
    publishes.
  - **What it does not cover**: the dry run builds the commit it is started
    on, and `create_release.py` then adds the bump commit (`pyproject.toml`,
    `uv.lock`, `src/stt_app/__init__.py`, the Inno script) on top and tags
    that. Those four files are the only difference unless the version was
    already bumped on `main`; to dry-run exactly the tagged commit, bump on
    `main` first (the manual path in `docs/windows-distribution.md`), run
    the dry run, then `create_release.py` tags the current `main` without a
    commit. The tag-match step is skipped in a dry run; the script runs the
    same check (`verify_release`) before it pushes.
  - **The dry run costs no Actions minutes**: the repository is public
    (checked 2026-10-17 with the unauthenticated GitHub API,
    `"private": false`), and only private repositories consume minutes, so
    there is nothing to ration and no reason to skip it for a small release.
  - Order of a release: `release_check_*.py` scripts, dry run on `main`,
    green, `create_release.py`.
- **`scripts/setup_node_windows.py`**: numeric `major.minor.patch` only,
  archives verified against `SHASUMS256.txt`, escaping ZIP members rejected.
  Keep all three.
- **Ruff's rule set is written out, never inherited**: `pyproject.toml` names
  every rule and every ignore with its reason (defaults checked almost
  nothing). An upgrade may add findings; it must not silently change the gate.
- **`ruff format --check .` is a gate** (2026-10-01): both workflows run it
  as its own step after `ruff check`, so a step's exit code is its own. The
  whole repository was reformatted once (listed in `.git-blame-ignore-revs`;
  `git config blame.ignoreRevsFile .git-blame-ignore-revs` makes blame skip
  it). Run `ruff format .` before committing; a ruff upgrade that changes the
  style is a reformat commit of its own, added to that file.
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
- **Every string a script can print is ASCII**
  (`tests/test_script_output_is_ascii.py`): redirected Windows output is
  cp1252, so other characters raise `UnicodeEncodeError` (crashed
  `--validate-only`) or print as `\uXXXX`, and in-cp1252 ones like an em dash
  show as U+FFFD in UTF-8 logs. Comments and docstrings are exempt, except a
  module docstring in a script that loads `__doc__` at all (e.g.
  `print(__doc__)` in `experiment_native_tray_icon.py`).
