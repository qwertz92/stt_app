"""A source checkout keeps `node_modules` in step with `package-lock.json`.

The owner runs the app from a git checkout on a machine where he cannot be
expected to know npm; a release that changed the lock (Transformers.js 4.3.0
-> 4.3.1, sharp 0.35.4 -> 0.35.5) needed an `npm ci` by hand after `git pull`.
npm is faked here; `docs/learning-log.md` (2026-10-17) has the real
measurement on HomeBase.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from stt_app import app_paths
from stt_app.config import TRAY_MESSAGE_MAX_CHARS
from stt_app.transcriber import _node_packages, local_webgpu_asr
from stt_app.transcriber.local_webgpu_asr import LocalOnnxWebGpuTranscriber

_ROOT_ENTRY = {
    "name": "stt-app",
    "dependencies": {"@huggingface/transformers": "^4.3.0"},
}
# What npm writes into the lock: sharp's binaries for every platform, marked
# optional, of which it installs only this machine's.
_OTHER_PLATFORM = {
    "node_modules/@img/sharp-linux-x64": {
        "version": "0.35.5",
        "integrity": "sha512-linux",
        "optional": True,
        "os": ["linux"],
    },
}
OLD = {
    "node_modules/@huggingface/transformers": {
        "version": "4.3.0",
        "integrity": "sha512-transformers-430",
    },
    "node_modules/sharp": {"version": "0.35.4", "integrity": "sha512-sharp-354"},
}
NEW = {
    "node_modules/@huggingface/transformers": {
        "version": "4.3.1",
        "integrity": "sha512-transformers-431",
    },
    "node_modules/sharp": {"version": "0.35.5", "integrity": "sha512-sharp-355"},
}


def _lock(packages: dict) -> dict:
    return {
        "name": "stt-app",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {"": _ROOT_ENTRY, **packages},
    }


# This machine's sharp binary: optional in the lock like the others, but npm
# installs it here, and sharp does not load without it.
THIS_PLATFORM_KEY = "node_modules/@img/sharp-win32-x64"
THIS_PLATFORM = {
    THIS_PLATFORM_KEY: {
        "version": "0.35.5",
        "integrity": "sha512-win32",
        "optional": True,
        "os": ["win32"],
    },
}


def _write_tree(
    node_modules: Path,
    packages: dict,
    marker: str,
    *,
    platform: tuple = (),
    omit_dev: bool = False,
) -> None:
    """A tree as `npm ci` leaves it: its record, minus what it skipped.

    Optional packages are skipped unless named in `platform`, and `dev`
    ones with `omit_dev` (`npm ci --omit=dev`).
    """
    node_modules.mkdir(parents=True)
    installed = {
        key: entry
        for key, entry in packages.items()
        if key
        and (not entry.get("optional") or key in platform)
        and not (omit_dev and entry.get("dev"))
    }
    (node_modules / ".package-lock.json").write_text(
        json.dumps({**_lock({}), "packages": installed}), encoding="utf-8"
    )
    (node_modules / "marker.txt").write_text(marker, encoding="utf-8")


def _checkout(
    tmp_path: Path, *, lock: dict, installed: dict | None, platform: tuple = ()
) -> Path:
    """A checkout with a lock and an `old` tree; returns the runner path."""
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": "stt-app", "dependencies": _ROOT_ENTRY["dependencies"]}),
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text(
        json.dumps(_lock({**lock, **_OTHER_PLATFORM})), encoding="utf-8"
    )
    if installed is not None:
        _write_tree(root / "node_modules", installed, "old", platform=platform)
    runner = root / "src" / "stt_app" / "webgpu_asr_runner.mjs"
    runner.parent.mkdir(parents=True)
    runner.write_text("", encoding="utf-8")
    return runner


def _marker(root: Path) -> str:
    return (root / "node_modules" / "marker.txt").read_text(encoding="utf-8")


class _FakeNpm:
    """`run_bounded` for `npm ci`: installs the staged lock, or fails half-way."""

    def __init__(
        self, *, returncode: int = 0, stderr: str = "", platform: tuple = ()
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.platform = platform
        self.calls: list[tuple[list[str], Path, float]] = []

    def __call__(self, arguments, *, timeout, cwd, **_kwargs):
        staging = Path(cwd)
        self.calls.append((list(arguments), staging, timeout))
        lock = json.loads((staging / "package-lock.json").read_text(encoding="utf-8"))
        _write_tree(
            staging / "node_modules",
            lock["packages"],
            "new",
            platform=self.platform,
            omit_dev=True,
        )
        if self.returncode != 0:
            # npm ci removes what it extracted on a failure, and on Windows
            # not always all of it.
            (staging / "node_modules" / ".package-lock.json").unlink()
        return subprocess.CompletedProcess(arguments, self.returncode, "", self.stderr)


class _Probe:
    """`_run_transformers_import_probe`, answering per call."""

    def __init__(self, *answers: int) -> None:
        self.answers = list(answers)
        self.cwds: list[Path] = []

    def __call__(self, node_path, cwd):
        self.cwds.append(Path(cwd))
        code = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        stderr = "" if code == 0 else "Cannot find package '@huggingface/transformers'"
        return subprocess.CompletedProcess(["node"], code, "", stderr)


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()
        self.stdout = None
        self.stderr = None
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 0


@pytest.fixture(autouse=True)
def _fresh_runtime_state(monkeypatch):
    monkeypatch.setattr(local_webgpu_asr, "_JS_RUNTIME_READY", set())
    monkeypatch.setattr(local_webgpu_asr, "_NODE_PACKAGES_NOTICE_SENT", False)
    monkeypatch.setattr(local_webgpu_asr, "_NODE_PACKAGES_NOTICE_HANDLER", None)
    monkeypatch.setattr(local_webgpu_asr, "_NODE_PACKAGES_NOTICE_PENDING", "")
    monkeypatch.setattr(local_webgpu_asr, "_npm_executable", lambda node_path: "npm")
    monkeypatch.setattr(_node_packages, "_RENAME_PAUSE_S", 0.0)


def _transcriber(monkeypatch, tmp_path, runner, on_spawn) -> LocalOnnxWebGpuTranscriber:
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber, "_ensure_snapshot", lambda self: tmp_path
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber, "_start_reader_threads", lambda self, state: None
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_read_json_message",
        lambda self, state, deadline: {
            "ok": True,
            "device": "webgpu",
            "gpuAvailable": True,
        },
    )

    def spawn(command, **_kwargs):
        on_spawn()
        return _FakeProcess()

    monkeypatch.setattr(local_webgpu_asr.subprocess, "Popen", spawn)
    return LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026",
        language_mode="de",
        node_path="node",
        runner_path=runner,
    )


def test_packages_in_step_run_no_npm_and_the_runner_is_announced(monkeypatch, tmp_path):
    """No drift, no npm: an update check that misread sharp's other-platform
    binaries (in the lock, never installed here) would run npm -- and reach
    the network -- at every start. The runner holds its presence while its
    child lives and gives it back when the child ends."""
    runner = _checkout(tmp_path, lock=NEW, installed=NEW)
    npm = _FakeNpm()
    probe = _Probe(0)
    monkeypatch.setattr(_node_packages, "run_bounded", npm)
    monkeypatch.setattr(local_webgpu_asr, "_run_transformers_import_probe", probe)
    live_at_spawn: list[int] = []
    transcriber = _transcriber(
        monkeypatch,
        tmp_path,
        runner,
        lambda: live_at_spawn.append(_node_packages.live_runner_count()),
    )

    transcriber.preload_model()
    live_while_loaded = _node_packages.live_runner_count()
    transcriber.close()

    assert npm.calls == []
    assert probe.cwds == [runner.parent]
    assert live_at_spawn == [1]
    assert live_while_loaded == 1
    assert _node_packages.live_runner_count() == 0


def test_a_changed_lock_is_installed_before_the_first_runner_starts(
    monkeypatch, tmp_path
):
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    root = runner.parents[2]
    npm = _FakeNpm()
    probe = _Probe(0)
    monkeypatch.setattr(_node_packages, "run_bounded", npm)
    monkeypatch.setattr(local_webgpu_asr, "_run_transformers_import_probe", probe)
    tree_at_spawn: list[str] = []
    transcriber = _transcriber(
        monkeypatch, tmp_path, runner, lambda: tree_at_spawn.append(_marker(root))
    )
    progress: list[str] = []
    transcriber.set_progress_callback(progress.append)

    transcriber.preload_model()
    transcriber.close()

    staging = root / _node_packages.STAGING_DIR_NAME
    [(arguments, cwd, timeout)] = npm.calls
    assert arguments[:3] == ["npm", "ci", "--omit=dev"]
    assert cwd == staging
    assert timeout == _node_packages.NPM_CI_TIMEOUT_S
    # The staged tree is checked before it replaces anything, then the one
    # in use.
    assert probe.cwds == [staging, runner.parent]
    assert tree_at_spawn == ["new"]
    assert _node_packages.lock_drift(root) == []
    assert not staging.exists()
    assert any("Updating the JavaScript packages" in line for line in progress)


def test_a_failed_update_keeps_the_old_packages_and_tells_the_user_once(
    monkeypatch, tmp_path
):
    """`npm ci` deletes the tree before installing; behind a proxy that left a
    model that had worked without packages. Here the old tree stays, the
    runner starts on it, and the user is told once in plain words."""
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    root = runner.parents[2]
    npm = _FakeNpm(
        returncode=1,
        stderr=(
            "npm error code ECONNREFUSED\n"
            "npm error A complete log of this run can be found in: C:\\x.log\n"
        ),
    )
    monkeypatch.setattr(_node_packages, "run_bounded", npm)
    monkeypatch.setattr(local_webgpu_asr, "_run_transformers_import_probe", _Probe(0))
    notices: list[str] = []
    local_webgpu_asr.set_node_packages_notice_handler(notices.append)

    first = local_webgpu_asr._ensure_js_runtime_available("node", runner)
    first.release()
    # A second start in the same session that checks again (another Node):
    # the back-off holds it, and the user has been told already.
    local_webgpu_asr._JS_RUNTIME_READY.clear()
    local_webgpu_asr._ensure_js_runtime_available("node", runner).release()

    assert len(npm.calls) == 1
    assert _marker(root) == "old"
    assert _node_packages.lock_drift(root) != []
    assert not (root / _node_packages.STAGING_DIR_NAME).exists()
    assert notices == [
        local_webgpu_asr.node_packages_notice_text(
            "npm reported an error (code ECONNREFUSED)", _retry_after()
        )
    ]
    assert "keep working with the previous packages" in notices[0]
    assert "tries again after " in notices[0]


def _back_off_path() -> Path:
    return app_paths.node_packages_back_off_path()


def _retry_after() -> float:
    record = json.loads(_back_off_path().read_text(encoding="utf-8"))
    return record["failed_at"] + _node_packages.UPDATE_BACK_OFF_S


def _a_day_passes() -> None:
    record = json.loads(_back_off_path().read_text(encoding="utf-8"))
    record["failed_at"] -= _node_packages.UPDATE_BACK_OFF_S
    _back_off_path().write_text(json.dumps(record), encoding="utf-8")


def test_a_failed_update_waits_a_day_before_npm_runs_again(monkeypatch, tmp_path):
    """Whether the work PC's network reaches the npm registry is unknown;
    retrying at every start delayed each session's first Cohere start by as
    long as npm takes to fail (71 s measured, up to the 600 s bound). The
    tree in use keeps working meanwhile, and a success forgets the failure."""
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    root = runner.parents[2]
    failing = _FakeNpm(returncode=1, stderr="npm error code ETIMEDOUT\n")
    monkeypatch.setattr(_node_packages, "run_bounded", failing)

    failed = _sync(root)
    waiting = _sync(root)
    _a_day_passes()
    npm = _FakeNpm()
    monkeypatch.setattr(_node_packages, "run_bounded", npm)
    updated = _sync(root)

    assert failed.failure == "npm reported an error (code ETIMEDOUT)"
    assert failed.retry_after == pytest.approx(
        time.time() + _node_packages.UPDATE_BACK_OFF_S, abs=60
    )
    assert len(failing.calls) == 1
    assert waiting.backed_off
    assert (waiting.failure, waiting.retry_after) == (
        failed.failure,
        failed.retry_after,
    )
    assert len(npm.calls) == 1
    assert updated.updated
    assert _marker(root) == "new"
    assert not _back_off_path().exists()


def test_a_changed_lock_is_tried_at_once_despite_a_back_off(monkeypatch, tmp_path):
    """The back-off belongs to the lock that failed. The next `git pull`
    that changes the lock may be the fix, so it is not made to wait."""
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    root = runner.parents[2]
    monkeypatch.setattr(_node_packages, "run_bounded", _FakeNpm(returncode=1))
    _sync(root)
    newer = {
        **NEW,
        "node_modules/sharp": {"version": "0.35.6", "integrity": "sha512-sharp-356"},
    }
    (root / "package-lock.json").write_text(
        json.dumps(_lock({**newer, **_OTHER_PLATFORM})), encoding="utf-8"
    )
    npm = _FakeNpm()
    monkeypatch.setattr(_node_packages, "run_bounded", npm)

    outcome = _sync(root)

    assert outcome.updated, outcome
    assert len(npm.calls) == 1


def _damage_back_off(damage: str) -> None:
    path = _back_off_path()
    record = json.loads(path.read_text(encoding="utf-8"))
    if damage == "not-json":
        path.write_bytes(b"\xff\xfe{ half a record")
    elif damage == "not-an-object":
        path.write_text("[]", encoding="utf-8")
    elif damage == "wrong-types":
        path.write_text(json.dumps({**record, "failed_at": "yesterday"}), "utf-8")
    elif damage == "no-reason":
        del record["reason"]
        path.write_text(json.dumps(record), encoding="utf-8")
    elif damage == "in-the-future":
        # The clock was set back after the failure: without a bound the
        # back-off would last until the clock caught up again.
        record["failed_at"] = time.time() + 3600
        path.write_text(json.dumps(record), encoding="utf-8")
    elif damage == "unreadable":
        path.unlink()
        path.mkdir()


@pytest.mark.parametrize(
    "damage",
    [
        "not-json",
        "not-an-object",
        "wrong-types",
        "no-reason",
        "in-the-future",
        "unreadable",
    ],
)
def test_a_damaged_back_off_record_never_blocks_an_update(
    monkeypatch, tmp_path, damage
):
    """The record only saves a retry; one that cannot be read or trusted
    counts as none and is never a crash."""
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    root = runner.parents[2]
    monkeypatch.setattr(_node_packages, "run_bounded", _FakeNpm(returncode=1))
    _sync(root)
    _damage_back_off(damage)
    npm = _FakeNpm()
    monkeypatch.setattr(_node_packages, "run_bounded", npm)

    outcome = _sync(root)

    assert outcome.updated, outcome
    assert len(npm.calls) == 1


def test_a_backed_off_tree_that_does_not_load_is_tried_again_at_once(
    monkeypatch, tmp_path
):
    """The back-off protects a tree that works. One that does not load
    leaves Cohere and Granite unusable whatever happens, so waiting a day
    would only add a day without them."""
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    root = runner.parents[2]
    monkeypatch.setattr(_node_packages, "run_bounded", _FakeNpm(returncode=1))
    _sync(root)
    npm = _FakeNpm()
    monkeypatch.setattr(_node_packages, "run_bounded", npm)
    # The tree in use fails; the staged one, and then the swapped-in one, load.
    probe = _Probe(1, 0, 0)
    monkeypatch.setattr(local_webgpu_asr, "_run_transformers_import_probe", probe)

    local_webgpu_asr._ensure_js_runtime_available("node", runner).release()

    assert len(npm.calls) == 1
    assert _marker(root) == "new"


def test_each_session_on_old_packages_says_so_once_with_the_next_try(
    monkeypatch, tmp_path
):
    """A later start inside the day runs no npm but still runs on the old
    packages: its one notice gives the reason of the failed try and when the
    next one comes."""
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    monkeypatch.setattr(
        _node_packages,
        "run_bounded",
        _FakeNpm(returncode=1, stderr="npm error code E403\n"),
    )
    _sync(runner.parents[2])
    npm = _FakeNpm()
    monkeypatch.setattr(_node_packages, "run_bounded", npm)
    monkeypatch.setattr(local_webgpu_asr, "_run_transformers_import_probe", _Probe(0))
    notices: list[str] = []
    local_webgpu_asr.set_node_packages_notice_handler(notices.append)

    local_webgpu_asr._ensure_js_runtime_available("node", runner).release()

    assert npm.calls == []
    assert notices == [
        local_webgpu_asr.node_packages_notice_text(
            "npm reported an error (code E403)", _retry_after()
        )
    ]


def test_a_notice_raised_before_the_tray_exists_reaches_it(monkeypatch, tmp_path):
    """The startup preload runs from `controller.initialize()`, before
    `main` registers the tray's handler; "npm was not found" fails in
    milliseconds, so the notice can come first and must not be lost."""
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    monkeypatch.setattr(local_webgpu_asr, "_npm_executable", lambda node_path: None)
    monkeypatch.setattr(local_webgpu_asr, "_run_transformers_import_probe", _Probe(0))

    local_webgpu_asr._ensure_js_runtime_available("node", runner).release()
    notices: list[str] = []
    local_webgpu_asr.set_node_packages_notice_handler(notices.append)
    local_webgpu_asr.set_node_packages_notice_handler(notices.append)

    assert notices == [
        local_webgpu_asr.node_packages_notice_text(
            "npm, which comes with Node.js, was not found", _retry_after()
        )
    ]


@pytest.mark.parametrize(
    ("retry_after", "ending"),
    [
        (
            # 14:05 on this machine's clock, daylight saving time included.
            time.mktime((2026, 10, 18, 14, 5, 0, 0, 0, -1)),
            "the app tries again after 2026-10-18 14:05.",
        ),
        # The back-off could not be saved: the next start does try again.
        (0.0, "the app tries again at its next start."),
    ],
    ids=["backed-off", "not-saved"],
)
def test_the_notice_fits_the_tray_whole_however_long_the_reason(retry_after, ending):
    """Windows cuts a tray text after 255 characters. A fixed cap on the
    reason made the text 256 long, so its final full stop went; sized
    against the tray's limit, the text arrives whole."""
    text = local_webgpu_asr.node_packages_notice_text(
        "npm did not install " + "x" * 200, retry_after
    )

    assert len(text) <= TRAY_MESSAGE_MAX_CHARS
    assert text.endswith(ending)
    assert "x..." in text


@pytest.mark.parametrize(
    "stderr",
    [
        "npm error code ENOTFOUND\nnpm error A complete log of this run ...\n",
        "npm ERR! code ENOTFOUND\nnpm ERR! A complete log of this run ...\n",
    ],
    ids=["current-npm", "older-npm"],
)
def test_the_reason_names_npms_error_code(stderr):
    """Older npm releases, still bundled with older Node.js installs, wrote
    `npm ERR!`; unrecognised, the reason was npm's last line, which only
    says where its log file is."""
    assert _node_packages._npm_error_summary(stderr) == "code ENOTFOUND"


_HOLD_PRESENCE = """
import sys
from stt_app.transcriber import _node_packages
presence = _node_packages.hold_runner_presence()
print("held" if presence is not None else "none", flush=True)
sys.stdin.read()
"""


def test_no_update_while_another_process_runs_a_runner(monkeypatch, tmp_path):
    """Windows renames `node_modules` under a child with DLLs mapped from it
    (measured), so nothing but the presence stops an update in the app from
    replacing the tree under the benchmark worker's runner, or the reverse."""
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    root = runner.parents[2]
    npm = _FakeNpm()
    monkeypatch.setattr(_node_packages, "run_bounded", npm)
    monkeypatch.setattr(local_webgpu_asr, "_run_transformers_import_probe", _Probe(0))
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(
        Path(local_webgpu_asr.__file__).resolve().parents[2]
    )
    other = subprocess.Popen(
        [sys.executable, "-c", _HOLD_PRESENCE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=environment,
    )
    watchdog = threading.Timer(60, other.kill)
    watchdog.start()
    try:
        assert other.stdout.readline().strip() == "held"

        local_webgpu_asr._ensure_js_runtime_available("node", runner).release()
        deferred_calls = len(npm.calls)
        marker_while_alive = _marker(root)
    finally:
        other.kill()
        other.wait(timeout=30)
        watchdog.cancel()
    local_webgpu_asr._JS_RUNTIME_READY.clear()
    local_webgpu_asr._ensure_js_runtime_available("node", runner).release()

    assert deferred_calls == 0
    assert marker_while_alive == "old"
    assert len(npm.calls) == 1
    assert _marker(root) == "new"


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows refuses a write-open of a loaded DLL; POSIX does not",
)
def test_no_update_while_any_process_has_the_trees_native_modules_loaded(
    monkeypatch, tmp_path
):
    """A runner started by an older build -- still running after `git pull`
    while the benchmark worker already runs the new code -- holds no presence
    lock; its loaded onnxruntime DLL is what gives it away. A copy of
    `python3.dll` loaded here stands in for that DLL."""
    import ctypes
    from ctypes import wintypes

    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    root = runner.parents[2]
    npm = _FakeNpm()
    monkeypatch.setattr(_node_packages, "run_bounded", npm)
    native = root / "node_modules" / "onnxruntime-node" / "onnxruntime_binding.node"
    native.parent.mkdir(parents=True)
    shutil.copy2(Path(sys.base_prefix) / "python3.dll", native)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FreeLibrary.argtypes = (wintypes.HMODULE,)
    kernel32.FreeLibrary.restype = wintypes.BOOL

    def sync() -> _node_packages.SyncOutcome:
        return _node_packages.sync_node_modules(
            root,
            npm_path="npm",
            probe=lambda cwd: _Probe(0)("node", cwd),
            progress=lambda _text: None,
        )

    handle = ctypes.WinDLL(str(native))._handle
    try:
        while_loaded = sync()
    finally:
        assert kernel32.FreeLibrary(handle)
    after_unload = sync()

    assert while_loaded.deferred
    assert _node_packages.loaded_native_modules(root / "node_modules") == []
    assert after_unload.updated
    assert len(npm.calls) == 1
    assert _marker(root) == "new"


def test_a_tree_that_does_not_load_is_reinstalled_once(monkeypatch, tmp_path):
    """npm's record says in step, the import fails (a deleted file): the
    repair that once ran `npm install` in place now goes the staged way."""
    runner = _checkout(tmp_path, lock=NEW, installed=NEW)
    root = runner.parents[2]
    npm = _FakeNpm()
    probe = _Probe(1, 0, 0)
    monkeypatch.setattr(_node_packages, "run_bounded", npm)
    monkeypatch.setattr(local_webgpu_asr, "_run_transformers_import_probe", probe)

    local_webgpu_asr._ensure_js_runtime_available("node", runner).release()

    staging = root / _node_packages.STAGING_DIR_NAME
    assert [cwd for _arguments, cwd, _timeout in npm.calls] == [staging]
    assert probe.cwds == [runner.parent, staging, runner.parent]
    assert _marker(root) == "new"


def _sync(root: Path, probe=None) -> _node_packages.SyncOutcome:
    return _node_packages.sync_node_modules(
        root,
        npm_path="npm",
        # By default it passes everywhere, as the real probe did: Node
        # resolved what the staged tree lacked from the tree in use one
        # folder up.
        probe=probe or (lambda cwd: _Probe(0)("node", cwd)),
        progress=lambda _text: None,
    )


def test_a_swap_that_fails_half_way_puts_the_old_tree_back(monkeypatch, tmp_path):
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    root = runner.parents[2]
    monkeypatch.setattr(_node_packages, "run_bounded", _FakeNpm())
    real_rename = _node_packages.os.rename

    staged_tree = root / _node_packages.STAGING_DIR_NAME / "node_modules"

    def rename(source, target):
        # A virus scanner holding a file of the new tree, for good.
        if Path(source) == staged_tree:
            raise PermissionError(5, "Access is denied", str(source))
        real_rename(source, target)

    monkeypatch.setattr(_node_packages.os, "rename", rename)

    outcome = _sync(root)

    assert outcome.failure == "the new packages could not be moved into place"
    assert _marker(root) == "old"
    assert not (root / _node_packages.STAGING_DIR_NAME).exists()


def test_a_staged_tree_without_a_package_the_tree_in_use_has_is_refused(
    monkeypatch, tmp_path
):
    """npm exits 0 when it could not fetch an optional package -- sharp's
    Windows binary, behind a proxy that blocks it. The lock check cannot
    tell that binary from another platform's, so the swap deleted the only
    tree sharp loaded from (review of d8a394c)."""
    runner = _checkout(
        tmp_path,
        lock={**NEW, **THIS_PLATFORM},
        installed={**OLD, **THIS_PLATFORM},
        platform=(THIS_PLATFORM_KEY,),
    )
    root = runner.parents[2]
    monkeypatch.setattr(_node_packages, "run_bounded", _FakeNpm())

    skipped = _sync(root)
    marker_after_skip = _marker(root)
    _a_day_passes()
    monkeypatch.setattr(
        _node_packages, "run_bounded", _FakeNpm(platform=(THIS_PLATFORM_KEY,))
    )
    installed = _sync(root)

    assert skipped.failure == "npm did not install @img/sharp-win32-x64"
    assert marker_after_skip == "old"
    assert installed.updated
    assert _marker(root) == "new"


def test_dev_packages_of_a_full_install_do_not_block_the_update(monkeypatch, tmp_path):
    """A developer's plain `npm ci` also installs dev packages; the update
    leaves them out by design (`--omit=dev`), so their absence from the
    staged tree is no lost package -- counted as one, it would refuse every
    update in that checkout for good."""
    dev = {
        "node_modules/prettier": {
            "version": "3.6.2",
            "integrity": "sha512-prettier",
            "dev": True,
        },
    }
    runner = _checkout(tmp_path, lock={**NEW, **dev}, installed={**OLD, **dev})
    root = runner.parents[2]
    monkeypatch.setattr(_node_packages, "run_bounded", _FakeNpm())

    outcome = _sync(root)

    assert outcome.updated, outcome.failure
    assert _marker(root) == "new"


@pytest.mark.parametrize("force", [False, True], ids=["changed-lock", "repair"])
def test_a_tree_for_another_platform_is_replaced_not_protected(
    monkeypatch, tmp_path, force
):
    """A tree installed from WSL into the Windows checkout holds sharp's
    Linux binary, which a Windows `npm ci` rightly never installs. That tree
    does not load here, so there is nothing to protect: refusing the staged
    tree for lacking the Linux binary (review of 9a09c99, reproduced) failed
    every update at every start, and the repair that exists for such a tree
    too."""
    linux_key = next(iter(_OTHER_PLATFORM))
    tree = NEW if force else OLD
    runner = _checkout(
        tmp_path,
        lock={**NEW, **THIS_PLATFORM},
        installed={**tree, **_OTHER_PLATFORM},
        platform=(linux_key,),
    )
    root = runner.parents[2]
    monkeypatch.setattr(
        _node_packages, "run_bounded", _FakeNpm(platform=(THIS_PLATFORM_KEY,))
    )

    def probe(cwd):
        # Loads wherever the staged tree is found first; not the Linux tree.
        return _Probe(1 if Path(cwd) == root else 0)("node", cwd)

    outcome = _node_packages.sync_node_modules(
        root,
        npm_path="npm",
        probe=probe,
        progress=lambda _text: None,
        force=force,
    )

    assert outcome.updated, outcome.failure
    assert _marker(root) == "new"


def test_a_first_install_needs_no_tree_to_compare_with(monkeypatch, tmp_path):
    """A fresh checkout has no `node_modules`: nothing to protect, and the
    staged tree goes in on the lock check and the probe alone."""
    runner = _checkout(tmp_path, lock=NEW, installed=None)
    root = runner.parents[2]
    monkeypatch.setattr(_node_packages, "run_bounded", _FakeNpm())

    outcome = _sync(root)

    assert outcome.updated, outcome.failure
    assert _marker(root) == "new"
    assert _node_packages.lock_drift(root) == []


def test_a_swap_cut_between_its_renames_is_undone_at_the_next_start(
    monkeypatch, tmp_path
):
    """A crash after the tree in use was moved aside and before the new one
    took its place leaves no `node_modules`; the moved tree is then the only
    one there is, and the next start puts it back rather than deleting it
    with the staging folder."""
    runner = _checkout(tmp_path, lock=NEW, installed=None)
    root = runner.parents[2]
    staging = root / _node_packages.STAGING_DIR_NAME
    _write_tree(staging / _node_packages._PREVIOUS_TREE_NAME, NEW, "old")
    npm = _FakeNpm()
    monkeypatch.setattr(_node_packages, "run_bounded", npm)

    outcome = _sync(root)

    assert outcome == _node_packages.SyncOutcome()
    assert npm.calls == []
    assert _marker(root) == "old"
    assert not staging.exists()


def test_a_runner_that_started_during_npm_defers_the_swap(monkeypatch, tmp_path):
    """npm runs for seconds to minutes; a runner of an older build can start
    meanwhile, so the loaded-DLL check runs again right before the renames."""
    runner = _checkout(tmp_path, lock=NEW, installed=OLD)
    root = runner.parents[2]
    npm = _FakeNpm()
    monkeypatch.setattr(_node_packages, "run_bounded", npm)
    answers = [[], ["onnxruntime.dll"]]
    monkeypatch.setattr(
        _node_packages, "loaded_native_modules", lambda tree: answers.pop(0)
    )

    outcome = _sync(root)

    assert outcome.deferred
    assert not outcome.failure
    assert len(npm.calls) == 1
    assert answers == []
    assert _marker(root) == "old"
    assert not (root / _node_packages.STAGING_DIR_NAME).exists()


def test_a_staging_folder_left_behind_goes_when_nothing_needs_updating(
    monkeypatch, tmp_path
):
    """A staging folder outlives an update only when its deletion failed (a
    file still open); the tree it held can be hundreds of megabytes."""
    runner = _checkout(tmp_path, lock=NEW, installed=NEW)
    root = runner.parents[2]
    staging = root / _node_packages.STAGING_DIR_NAME
    _write_tree(staging / _node_packages._PREVIOUS_TREE_NAME, OLD, "older")
    npm = _FakeNpm()
    monkeypatch.setattr(_node_packages, "run_bounded", npm)

    outcome = _sync(root)

    assert outcome == _node_packages.SyncOutcome()
    assert npm.calls == []
    assert _marker(root) == "old"
    assert not staging.exists()


@pytest.mark.parametrize(
    ("installed", "expected"),
    [
        (NEW, []),
        (OLD, ["@huggingface/transformers 4.3.0 -> 4.3.1", "sharp 0.35.4 -> 0.35.5"]),
        (
            {
                **NEW,
                "node_modules/sharp": {"version": "0.35.5", "integrity": "sha512-x"},
            },
            ["sharp 0.35.5 has different contents"],
        ),
        (
            {
                "node_modules/@huggingface/transformers": NEW[
                    "node_modules/@huggingface/transformers"
                ]
            },
            ["sharp 0.35.5 missing"],
        ),
        (
            {**NEW, "node_modules/left-pad": {"version": "1.3.0"}},
            ["left-pad 1.3.0 no longer in the lock"],
        ),
    ],
    ids=["in-step", "versions", "contents", "missing", "extra"],
)
def test_drift_is_what_npm_ci_would_change(tmp_path, installed, expected):
    root = _checkout(tmp_path, lock=NEW, installed=installed).parents[2]

    assert _node_packages.lock_drift(root) == expected


def test_a_tree_without_npms_record_counts_as_drift(tmp_path):
    root = _checkout(tmp_path, lock=NEW, installed=None).parents[2]

    assert _node_packages.lock_drift(root) == [
        "no record of the installed packages (node_modules/.package-lock.json)"
    ]


def test_only_this_apps_source_checkout_is_ever_updated(monkeypatch, tmp_path):
    """The installed app bundles its tree; a PyInstaller build run from
    `dist/` inside a checkout must not replace the checkout's packages, and
    neither may a wheel installed into some other project's tree."""
    runner = _checkout(tmp_path, lock=NEW, installed=NEW)
    root = runner.parents[2]

    assert local_webgpu_asr._find_source_package_root(runner) == root
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert local_webgpu_asr._find_source_package_root(runner) is None
    monkeypatch.delattr(sys, "frozen")
    (root / "package.json").write_text('{"name": "another-project"}', encoding="utf-8")
    assert local_webgpu_asr._find_source_package_root(runner) is None
