# Testing: detailed rules

Moved verbatim from the `Tests` section of `AGENTS.md` on 2026-09-30. The
commands every change needs are still in `AGENTS.md`; this file holds the
release-workflow history, the `scripts/release_check_*.py` contract, the
autouse fixtures in `tests/conftest.py` and the CI-runner rules. Read it before
adding a fixture, a layout test, a subprocess test or a release check.

- **The release workflow's test step is the same command as `quality.yml`'s,
  on the runner's real desktop** (since 2026-09-18). From 2026-08-23 it ran
  under `QT_QPA_PLATFORM=offscreen`, where tests marked `pixel_exact` or
  `platform_dependent` skip themselves, and a gate that runs for a release
  only rots where nobody looks: seven layout tests added after that date
  carried no marker, and the dry run for v0.9.0 failed on them (run
  35378290905: 7 failed, 2855 passed, 23 skipped) while `quality.yml` was
  green on the same commit (2884 passed, 1 skipped). v0.8.0 is the same story
  one release earlier, read from its log: under offscreen two tests failed,
  the suite then printed nothing past 96% and GitHub cancelled the job after
  six hours (run 30862693087) -- the tag exists, the release does not, and the
  log cannot say which test hung, which is what the `faulthandler_timeout`
  above is for. The two markers still serve a run on a machine without a
  desktop (a cloud container, a headless agent): a new test that compares
  widget geometry needs one, and `QT_QPA_PLATFORM=offscreen pytest <file>` is
  the three-second check. No CI gate depends on them. **Before a release tag,
  start the workflow by hand** (`gh workflow run windows-release.yml`, which
  builds and uploads an artifact and publishes nothing): it is the only run
  that exercises the bundle and installer steps. Such a run is what caught the
  seven unmarked tests before v0.9.0 was tagged; v0.8.0 had none and its tag
  is still without a release.
- **Four `scripts/release_check_*.py` scripts measure what the suite can only
  fake**, and they are run before a release, not by pytest or CI: they need
  the real clipboard and keyboard focus, real share-mode-0 file locks, the
  user's provider keys and paid quota, and a built PyInstaller bundle.
  `release_check_file_locks.py` (23 checks, F10), `release_check_clipboard_paste.py`
  (19 checks: the five-format round trip, Explorer's cut and copy through
  Shell COM, WM_PASTE and SendInput into a real EDIT window, the deferred
  restore, the foreground guard; F01/F02/F07/F12),
  `release_check_providers.py` (AssemblyAI batch and realtime, Groq batch, a
  quit during the AssemblyAI poll) and `release_check_frozen_bundle.py` (scan
  worker, one model per local runtime through the benchmark worker, the GUI
  for 40 s). Shared plumbing is `scripts/_release_check_common.py`. Contract:
  one `OK`/`FAIL`/`SKIP` line per check, exit 0/1/2 where 2 means "nothing was
  measured" (this machine cannot run it, or every check was skipped -- a run
  with no provider key used to print PASSED with exit 0) and must never read
  as a failure of the code; an exception no check caught and Ctrl+C are
  recorded as a failed check by `run_main`, so the `SUMMARY` line and the
  report exist for a crashed run too (a scan worker that left half a JSON file
  ended the bundle check with a traceback and no verdict); a child that may
  have children of its own runs through `run_child`, which kills the process
  tree on a timeout -- plain `subprocess.run(timeout=3)` returned after 20.1 s
  for a grandchild sleeping 20 s, because the final pipe read waits for every
  inheritor, and left that grandchild alive; check names are made ASCII like
  the details, since they carry the clip's file name; a throwaway
  `APPDATA` set before the first `stt_app` import with `HF_HUB_OFFLINE=1`; no
  path of the machine they were written on; API keys read by
  `KeyringSecretStore` only and never printed or written to a report; the
  user's clipboard captured first, restored in a `finally` and compared at
  the end, with format ids and sizes in the report and never content. All
  four passed on 2026-09-18 on HomeBase (23/23, 19/19, 7/7, 11/11), and the
  clipboard check again on v0.10.0's tree on 2026-09-27 (19/19, run by the
  owner after unlocking; the user's six clipboard formats came back). The
  commands and what each needs are in `docs/windows-distribution.md`. They do
  not cover running the installer or an upgrade over an installed version.
- Two autouse fixtures in `tests/conftest.py` make desktop side effects
  impossible: `_forbid_handing_paths_to_the_desktop_shell` blocks
  `QProcess.startDetached` and `QDesktopServices.openUrl`, and
  `_forbid_blocking_modal_dialogs` blocks the `QMessageBox` statics and the
  `QFileDialog` getters. Both raise a named `AssertionError` rather than
  no-opping: an unstubbed modal dialog does not fail a run, it hangs it forever
  with no output naming the cause. A test that legitimately drives one of these
  paths patches it and asserts on the call, which every current one does — so
  these fixtures are preventive and currently catch nothing.
- A third autouse fixture, `_reset_the_transcription_shutdown_flag`, clears
  the process-wide transcription shutdown flag before and after every test.
  Any future process-global state that a controller's `shutdown()` sets needs
  the same treatment, and the full suite is the only run that can show the
  leak: per-file runs were green while the suite had 26 failures.
- A fourth, `_no_benchmark_worker_outlives_its_test`, fails a test that
  leaves the dialog's benchmark worker thread (`stt_app_local_benchmark`)
  running, after waiting for it so the next test starts clean. The worker
  collects the environment first -- 2-4 s of PowerShell -- and only then
  calls the facade's `run_benchmark_cases`: whichever fake the test running
  by then has installed, or the real process launcher when none is. A
  Canary-refusal test stubbed the options builder with a recorder and let
  the run start on a real thread; measured once in a run of 415 tests, that
  worker fed a later file's fake three extra entries, and in every other
  run it launched a real benchmark worker child through the facade. The
  test patches an immediate thread, the facade function and the
  environment query now, as the other run-starting tests do.
- A fifth, `_one_decoder_per_stream_session` in `tests/test_transcriber.py`,
  fails a test whose partial decodes ran on two threads for one streaming
  session. In the app the stream worker is the only caller of
  `_maybe_emit_partial`, so nothing serializes two callers; seven tests
  called it from the test thread beside the worker's own decode of every
  pushed chunk, both read the same `new_audio` slice before either advanced
  `last_partial_size`, `silent_seconds` counted every quiet chunk twice,
  and the pause route appended an invented window on trust once per decode
  -- 29 words once in the full suite, where the test bounds 12 (forced with
  a barrier: 20 appended windows and 169 words against 9 with one decoder).
  The tests push through `_push_and_decode`, which waits for the worker's
  partial; the race is timing -- three of three runs of the old tests under
  CPU load tripped the detector, the one idle run did not -- so the
  detector names it when it happens and the helper makes it impossible.
- A sixth, `_no_keyboard_modifier_outlives_its_test`, clears a keyboard
  modifier a test leaves set in Qt. `QTest.keyClick(widget, key,
  ControlModifier)` on a widget without a native window leaves
  `QGuiApplication.keyboardModifiers()` at Ctrl for the rest of the process
  (measured on PySide6 6.11.1: still Ctrl after the widget is deleted,
  NoModifier again only after a later modifier-free key event), and item
  views read that state for a programmatic `selectRow`, which then
  *toggles*: the History dialog selects its first row on open, so
  `selectRow(0)` deselected it and two `test_history_dialog.py` tests failed
  whenever `test_settings_dialog_general_ux.py` ran before them. The suite
  runs files alphabetically and never showed it; any other file order did.
  It clears rather than asserts, because on a real desktop the person at the
  keyboard can put a genuine Ctrl into Qt's state while a test window has
  the focus. Related tooling trap: `pytest --collect-only -q` prints no test
  ids here, because `addopts = "-q"` makes it `-qq`; use
  `-o addopts="" --collect-only -q` -- a bisect loop over an empty id list
  ran zero tests and looked like "no single test reproduces it".
- A seventh, `_empty_local_model_scan_session_cache`, empties the two
  module-level containers in which the settings dialog keeps every
  finished inventory scan for the life of the process
  (`_LOCAL_MODEL_SCAN_SESSION_CACHE` and its verified-dirs set). A test that
  patched the scan to answer `["small"]` left that for the Model Dir every
  test uses, and a later dialog's deferred inventory render replaced the
  model list a test had just set up: ten `test_benchmark_run_progress.py`
  tests failed whenever `test_settings_dialog_thread_start.py` ran first,
  which the alphabetical full suite never does (found 2026-09-27, present
  before that day's UX commits).
- An eighth, `_a_fresh_download_coordinator_per_test`, replaces the
  download slot's process-wide `_COORDINATOR` for every test: a test that
  patched out the step which gives an explicit interest back left
  `('large-v3', '')` registered for the rest of the run.
- **Read the suite's count before anything that publishes.** A shell chain
  that commits and pushes after a background suite has *started* publishes
  before the result exists; on 2026-09-04 four green per-file runs and one
  such chain pushed a red suite. Gate the push on the printed
  `N passed` line, never on the exit code of a pipeline ending in `tail`.
- **The CI runner is a fresh VM: under an hour of uptime, no microphone,
  a 1024x768 screen.** `quality.yml` runs the suite on `windows-latest`
  with a real desktop (not offscreen, for the pixel-exact tests), where
  `time.monotonic()` reads minutes because it counts from boot, no audio
  device exists, and the window manager grants a top-level window at most
  about 1028x749. Ten tests assumed the developer's desktop and failed on
  every one of the 135 runs from 2026-07-21 to 2026-09-06 while passing
  here: five drove a partial with `last_partial_at = 0.0`, which reaches
  the 3600 s interval only after an hour of uptime (reproduced here with
  `time.monotonic` shifted to read 100 s: `AssertionError('')`); one
  opened the real microphone before the handshake it was testing; four
  asked for dialog sizes the screen cannot grant -- `QSize(900, 800)`, a
  1400 px width for a 940 px message, 220 px more on a dialog already at
  the clamp -- or compared against the requested size instead of the
  granted one. Rules: a test that resizes a window asks
  `screen().availableGeometry()` first, asserts the size the window HAS,
  and skips with the measured room when the screen cannot host the change;
  a controller test that records uses `FakeCapture`; a helper that means
  "long ago" writes `-interval`, never 0. The runs cost nothing -- Actions
  minutes are free for public repositories -- which is why a red gate was
  ignored rather than fixed. Whether the runner is now green is verified by
  the next run itself, not by anything here. **An eleventh arrived with the
  benchmark feature**: the header-click test snapshotted the results table
  one event-loop pass after filling it, and on the runner's 749 px dialog
  that table is 110 px for four 20 px rows under a 33 px header -- Qt shows
  the vertical scrollbar on a later pass, the stretch column gives up its
  12 px then, and the first click was blamed (measured here at 860x700:
  132 -> 120 with one pass, unchanged across every click after twenty). A
  test that snapshots a table's geometry settles it first
  (`_settle_table`: `QTest.qWait` plus a stability loop) and measures at a
  size where the rows do not fit, so the scrollbar case is part of what it
  checks on every machine. And a twelfth was a flake rather than an
  assumption: the Node parser probe in `test_local_webgpu_asr.py` gave
  `node` 10 s and the first run after the fix (34026331715) killed it at
  the bound, on a VM where the same probe had passed twice before and takes
  0.05 s here. A subprocess timeout in a test is a bound against a hang,
  not a speed claim; it is a minute now. **A thirteenth depended on the
  runner's CPU** (2026-09-19, run 35407923973, red on `ab427e1` while its
  parent was green on another runner): a Granite CTC test asserted *bit*
  equality between the blockwise and the single-pass feature extraction.
  The mel projection is a BLAS product, OpenBLAS picks its kernels for the
  CPU it finds, and which kernel sums a row depends on the block's shape.
  Reproduced here by setting `OPENBLAS_CORETYPE`: Zen, Haswell and Core2
  differ by one float32 step (1.19e-7), SkylakeX, Sandybridge, Nehalem and
  this machine's default by nothing. GitHub's `windows-latest` fleet is
  mixed, so the test was a coin toss per run. It compares within 1e-6 now
  -- a real blocking bug (restarted offsets, a block shifted by one frame,
  a dropped last block) moves a feature by more than 1.4. Rule: results
  that went through BLAS are compared with a tolerance derived from a
  measurement, never with `array_equal`; bit equality is for copies.
