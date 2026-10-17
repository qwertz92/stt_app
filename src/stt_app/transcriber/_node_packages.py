"""Keeps a source checkout's `node_modules` in step with `package-lock.json`.

The Cohere and Granite 4.x models run in a Node.js child whose packages live
in the checkout's `node_modules`, installed from `package-lock.json`. A
release that changes the lock (2026-10-10: Transformers.js 4.3.0 -> 4.3.1 and
sharp 0.35.4 -> 0.35.5 for an advisory) used to need an `npm ci` by hand after
`git pull`. The installed app bundles its tree (`stt_app.spec`) and never
comes here (`local_webgpu_asr._find_source_package_root`).

- **Drift** is `package-lock.json` against npm's own record of what it
  installed, `node_modules/.package-lock.json`, compared by version and
  integrity: two JSON reads, no npm and no network while they agree.
- **An update never touches the working tree until it is complete**:
  `npm ci` runs in a staging folder beside `node_modules`, the staged tree
  must match the lock, hold every package the tree in use has that the lock
  still lists, and import Transformers.js, and only then do two renames
  swap it in. `npm ci` deletes `node_modules` before installing, so
  in place a failure (a proxy, a blocked registry, no npm) left a model that
  had worked without packages -- measured: an unreachable registry failed
  after 71 s and left a half-deleted tree.
- **Never under a live runner.** Windows renames `node_modules` while a
  child has five DLLs mapped from it (measured, onnxruntime and sharp), so
  the rename does not protect a running child. Every runner child holds a
  presence lock (`hold_runner_presence`) in whichever process of this user
  started it -- the app or the benchmark worker -- and an update runs only
  while none is held, under the package root's lock (`package_root_lock`),
  which registering a presence takes as well. A runner of an older build or
  of a crashed app holds no presence; the DLLs it loaded do refuse a
  write-open (`loaded_native_modules`), checked before npm and again right
  before the swap.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..app_paths import appdata_root
from ..file_lock import (
    CrossProcessLock,
    FileLockUnavailable,
    is_lock_held,
    lock_path_for,
)
from ..process_tree import run_bounded

logger = logging.getLogger(__name__)

# Beside `node_modules`, so the swap is a rename on one volume. Ignored by git.
STAGING_DIR_NAME = ".node_modules_update"
# Where the replaced tree waits inside the staging folder until it is deleted.
_PREVIOUS_TREE_NAME = "node_modules.previous"
# A warm npm cache installed the whole tree in 16.5 s on HomeBase; a cold one
# on a slow corporate line downloads about 150 MB.
NPM_CI_TIMEOUT_S = 600.0
# Another process's update holds the root lock for at most its own npm run.
_ROOT_LOCK_WAIT_S = NPM_CI_TIMEOUT_S + 120.0
# Windows refuses to rename a folder while any file in it is open (measured
# in review: one open file three levels down, WinError 5), and a virus
# scanner opens every freshly extracted file. Up to 30 s, because giving up
# throws the verified staged tree away and the next start runs npm again.
_RENAME_ATTEMPTS = 60
_RENAME_PAUSE_S = 0.5
_REASON_MAX_CHARS = 80
_DETAIL_MAX_LINES = 20


@dataclass(frozen=True)
class SyncOutcome:
    """What `sync_node_modules` found and did.

    `failure` is set only when an update was needed and did not happen, in
    words for the user; the tree the runner finds is then the one from
    before (or none, if there was none).
    """

    changes: tuple[str, ...] = ()
    updated: bool = False
    deferred: bool = False
    failure: str = ""


class _UpdateFailed(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class _UpdateDeferred(Exception):
    """The tree came into use while the update was being prepared."""


def _lock_packages(path: Path) -> dict[str, dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} could not be read: {exc}") from exc
    packages = payload.get("packages") if isinstance(payload, dict) else None
    if not isinstance(packages, dict):
        raise ValueError(f"{path} has no 'packages' table.")
    return {
        key: entry for key, entry in packages.items() if key and isinstance(entry, dict)
    }


def lock_drift(root: Path) -> list[str]:
    """How `root/node_modules` differs from `root/package-lock.json`.

    An empty list means in step. Raises `ValueError` when the lock itself
    cannot be read, because then nothing can be compared or installed.

    An optional package absent from the tree is no drift: the lock lists
    sharp's binaries for every platform and npm installs only this one's.
    A package marked `dev` is left out, as `npm ci --omit=dev` leaves it.
    Versions and integrity are compared, not `resolved`: a registry mirror
    records its own URL for the same tarball.
    """
    wanted = _lock_packages(root / "package-lock.json")
    try:
        installed = _lock_packages(root / "node_modules" / ".package-lock.json")
    except ValueError:
        return ["no record of the installed packages (node_modules/.package-lock.json)"]
    changes = []
    for key, entry in sorted(wanted.items()):
        if entry.get("dev"):
            continue
        name = key.removeprefix("node_modules/")
        version = entry.get("version")
        have = installed.get(key)
        if have is None:
            if not (entry.get("optional") or entry.get("devOptional")):
                changes.append(f"{name} {version} missing")
            continue
        if have.get("version") != version:
            changes.append(f"{name} {have.get('version')} -> {version}")
        elif entry.get("integrity") and have.get("integrity") != entry.get("integrity"):
            changes.append(f"{name} {version} has different contents")
    for key in sorted(installed.keys() - wanted.keys()):
        name = key.removeprefix("node_modules/")
        changes.append(f"{name} {installed[key].get('version')} no longer in the lock")
    return changes


def _dropped_packages(root: Path, staging: Path) -> list[str]:
    """What the tree in use has and the lock still lists, but npm did not stage.

    npm skips an optional package it cannot fetch and still exits 0 -- for
    sharp that is its Windows binary, `@img/sharp-win32-x64`. `lock_drift`
    cannot tell that from another platform's binary, and the staged import
    probe found the old tree's copy one folder up, so a tree that could not
    load replaced one that could (review of d8a394c, reproduced). Without a
    tree in use there is nothing to fall back on and the probe decides.
    """
    try:
        live = _lock_packages(root / "node_modules" / ".package-lock.json")
    except ValueError:
        return []
    wanted = _lock_packages(staging / "package-lock.json")
    staged = _lock_packages(staging / "node_modules" / ".package-lock.json")
    return sorted(
        key.removeprefix("node_modules/")
        for key in (live.keys() & wanted.keys()) - staged.keys()
    )


def _presence_dir() -> Path:
    return appdata_root() / "locks" / "node-runners"


class RunnerPresence:
    """One live runner child, visible to every process of this Windows user."""

    def __init__(self, lock: CrossProcessLock, path: Path) -> None:
        self._lock = lock
        self._path = path

    def release(self) -> None:
        """Give the presence back; safe to call more than once."""
        self._lock.release()
        with contextlib.suppress(OSError):
            self._path.unlink(missing_ok=True)


def hold_runner_presence() -> RunnerPresence | None:
    """Announce a runner child that is about to start.

    Called under `package_root_lock`, so an update in any process either
    finished before this or sees it. None when the lock directory cannot be
    used; the runner then starts unannounced, as it did before updates
    existed.
    """
    lock_dir = _presence_dir()
    resource = f"node runner {os.getpid()} {uuid.uuid4().hex}"
    lock = CrossProcessLock(resource, lock_dir=lock_dir)
    try:
        lock.acquire()
    except FileLockUnavailable as exc:
        logger.warning("node_runner_presence_unavailable error=%s", exc)
        return None
    return RunnerPresence(lock, lock_path_for(resource, lock_dir=lock_dir))


def live_runner_count() -> int:
    """Runner children alive in any process of this user; stale files go.

    A presence whose process ended -- even by a crash -- is no longer
    locked, because the OS drops a lock with its process.
    """
    try:
        candidates = sorted(_presence_dir().glob("*.lock"))
    except OSError:
        return 0
    live = 0
    for path in candidates:
        if is_lock_held(path):
            live += 1
            continue
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
    return live


def loaded_native_modules(tree: Path) -> list[str]:
    """Native modules in `tree` that some process has loaded right now.

    Windows refuses to open a loaded DLL for writing -- measured: exactly the
    five files a runner child maps, and none once it exited -- whichever
    process and whichever app version loaded it. That covers what presence
    locks cannot: a runner started by an older build that is still running
    after a `git pull`, or one a crashed app left behind. Nothing is written;
    a read-only file is skipped rather than counted. Elsewhere a loaded
    library opens fine and this finds nothing. Walks the tree (0.45 s for
    466 MB), so it runs only when an update is due.
    """
    if os.name != "nt":
        return []
    loaded = []
    for directory, _subdirectories, files in os.walk(tree):
        for name in files:
            if not name.lower().endswith((".node", ".dll")):
                continue
            path = os.path.join(directory, name)
            try:
                if not os.stat(path).st_mode & stat.S_IWRITE:
                    continue
                with open(path, "r+b"):
                    pass
            except PermissionError:
                loaded.append(name)
            except OSError:
                continue
    return sorted(loaded)


def _tree_in_use(root: Path) -> str:
    """Why `root/node_modules` must not be replaced now, or ""."""
    live = live_runner_count()
    if live:
        return f"{live} runner(s) announced"
    loaded = loaded_native_modules(root / "node_modules")
    if loaded:
        return "loaded by a running process: " + ", ".join(loaded)
    return ""


@contextlib.contextmanager
def package_root_lock(root: Path) -> Iterator[None]:
    """Hold the lock that orders updates and runner starts on `root`.

    Across processes: the app and its benchmark worker can both start a
    runner, and two updates in one staging folder would delete each other's
    files. Within one process the caller serializes (`_JS_RUNTIME_LOCK`).
    Waits at most `_ROOT_LOCK_WAIT_S` for another process's update, then
    raises `TimeoutError`. A directory that cannot hold locks degrades to
    process-local order, as the download slot does.
    """
    lock = CrossProcessLock(f"node_modules {root}", lock_dir=appdata_root() / "locks")
    deadline = time.monotonic() + _ROOT_LOCK_WAIT_S
    try:
        held: bool | None = lock.acquire(
            cancel_check=lambda: time.monotonic() > deadline
        )
    except FileLockUnavailable as exc:
        logger.warning("node_modules_lock_unavailable root=%s error=%s", root, exc)
        held = None
    if held is None:
        yield
        return
    if not held:
        raise TimeoutError(
            "Another stt_app process has been updating the JavaScript packages "
            f"for more than {round(_ROOT_LOCK_WAIT_S / 60)} minutes."
        )
    try:
        yield
    finally:
        lock.release()


def _clear_read_only(function, path, _exc) -> None:
    # A file still in use stays; `_discard_staging` reports the folder as not
    # gone rather than stopping at the first such file.
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IWRITE)
        function(path)


def _rename(source: Path, target: Path) -> None:
    for attempt in range(_RENAME_ATTEMPTS):
        try:
            os.rename(source, target)
            return
        except FileNotFoundError:
            raise
        except OSError:
            if attempt == _RENAME_ATTEMPTS - 1:
                raise
            time.sleep(_RENAME_PAUSE_S)


def _restore_stranded_tree(root: Path) -> bool:
    """Move a replaced tree back if `node_modules` is missing; False on failure.

    Only a swap cut between its two renames (a failed way back, a crash)
    leaves one: then it is the only tree there is.
    """
    previous = root / STAGING_DIR_NAME / _PREVIOUS_TREE_NAME
    live = root / "node_modules"
    if not previous.is_dir() or live.exists():
        return True
    try:
        _rename(previous, live)
    except OSError as exc:
        logger.error("node_modules_restore_failed previous=%s error=%s", previous, exc)
        return False
    logger.warning("node_modules_restored from=%s", previous)
    return True


def _discard_staging(root: Path) -> bool:
    """Remove the staging folder; True when it is gone.

    Deletes nothing while a stranded previous tree cannot be moved back.
    """
    staging = root / STAGING_DIR_NAME
    if not staging.exists():
        return True
    if not _restore_stranded_tree(root):
        return False
    shutil.rmtree(staging, onexc=_clear_read_only)
    return not staging.exists()


def _npm_error_summary(stderr: str) -> str:
    """The line of npm's error output that names the failure, shortened.

    npm ends with "A complete log of this run can be found in: <path>", which
    says nothing; its `npm error code ECONNREFUSED` line does.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    # Current npm writes "npm error"; older releases wrote "npm ERR!".
    errors = [
        line.removeprefix("npm error ").removeprefix("npm ERR! ")
        for line in lines
        if line.startswith(("npm error ", "npm ERR! "))
    ]
    summary = next(
        (line for line in errors if line.startswith("code ")),
        errors[0] if errors else (lines[-1] if lines else "no output"),
    )
    if len(summary) > _REASON_MAX_CHARS:
        summary = summary[: _REASON_MAX_CHARS - 3].rstrip() + "..."
    return summary


def _tail(text: str) -> str:
    return "\n".join((text or "").strip().splitlines()[-_DETAIL_MAX_LINES:])


def _install_and_swap(
    root: Path,
    npm_path: str,
    probe: Callable[[Path], subprocess.CompletedProcess[str]],
) -> None:
    staging = root / STAGING_DIR_NAME
    try:
        if not _discard_staging(root):
            raise _UpdateFailed(
                "a folder left by an earlier update could not be removed"
            )
        staging.mkdir()
        for name in ("package.json", "package-lock.json", ".npmrc"):
            if (root / name).is_file():
                shutil.copy2(root / name, staging / name)
    except OSError as exc:
        raise _UpdateFailed(
            "the update folder could not be prepared", str(exc)
        ) from exc
    try:
        result = run_bounded(
            [
                npm_path,
                "ci",
                "--omit=dev",
                "--no-audit",
                "--no-fund",
                "--no-update-notifier",
            ],
            timeout=NPM_CI_TIMEOUT_S,
            cwd=str(staging),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise _UpdateFailed(
            f"npm did not finish within {round(NPM_CI_TIMEOUT_S / 60)} minutes"
        ) from exc
    except OSError as exc:
        raise _UpdateFailed("npm could not be started", str(exc)) from exc
    if result.returncode != 0:
        raise _UpdateFailed(
            f"npm reported an error ({_npm_error_summary(result.stderr)})",
            _tail(result.stderr or result.stdout),
        )
    # Both checks also keep the probe below honest: a package missing from
    # the staged tree would resolve from the old tree one folder up.
    try:
        mismatch = lock_drift(staging)
        dropped = _dropped_packages(root, staging)
    except ValueError as exc:
        mismatch, dropped = [str(exc)], []
    if mismatch:
        raise _UpdateFailed(
            "the new packages do not match package-lock.json", "; ".join(mismatch)
        )
    if dropped:
        raise _UpdateFailed(f"npm did not install {', '.join(dropped)}")
    try:
        check = probe(staging)
    except Exception as exc:
        raise _UpdateFailed("the new packages could not be checked", str(exc)) from exc
    if check.returncode != 0:
        raise _UpdateFailed(
            "the new packages did not load", _tail(check.stderr or check.stdout)
        )
    live = root / "node_modules"
    previous = staging / _PREVIOUS_TREE_NAME
    # Again, just before the renames: npm took seconds to minutes, and a
    # runner of an older build can have started meanwhile (one of this build
    # cannot: it waits for `package_root_lock`).
    loaded = loaded_native_modules(live)
    if loaded:
        raise _UpdateDeferred("loaded by a running process: " + ", ".join(loaded))
    had_live = live.exists()
    if had_live:
        try:
            _rename(live, previous)
        except OSError as exc:
            raise _UpdateFailed(
                "the packages folder is in use and could not be replaced", str(exc)
            ) from exc
    try:
        _rename(staging / "node_modules", live)
    except OSError as exc:
        if had_live:
            with contextlib.suppress(OSError):
                # `_discard_staging` tries again and deletes nothing if this
                # fails too.
                _rename(previous, live)
        raise _UpdateFailed(
            "the new packages could not be moved into place", str(exc)
        ) from exc


def sync_node_modules(
    root: Path,
    *,
    npm_path: str | None,
    probe: Callable[[Path], subprocess.CompletedProcess[str]],
    progress: Callable[[str], None],
    force: bool = False,
) -> SyncOutcome:
    """Bring `root/node_modules` in step with `root/package-lock.json`.

    The caller holds `package_root_lock(root)` and starts no runner in this
    process meanwhile. `force` reinstalls although the record says in step,
    for a tree that does not load. `probe(cwd)` imports Transformers.js
    from `cwd`. Never raises for an update that failed: the outcome says so.
    """
    _restore_stranded_tree(root)
    try:
        changes = lock_drift(root)
    except ValueError as exc:
        logger.warning("node_modules_drift_unknown root=%s error=%s", root, exc)
        return SyncOutcome()
    if not changes and not force:
        with contextlib.suppress(OSError):
            _discard_staging(root)
        return SyncOutcome()
    changes = changes or ["the installed packages do not load"]
    summary = ", ".join(changes)
    in_use = _tree_in_use(root)
    if in_use:
        logger.info(
            "node_modules_update_deferred root=%s in_use=%s changes=%s",
            root,
            in_use,
            summary,
        )
        return SyncOutcome(changes=tuple(changes), deferred=True)
    if not npm_path:
        reason = "npm, which comes with Node.js, was not found"
        logger.warning(
            "node_modules_update_failed root=%s reason=%s changes=%s",
            root,
            reason,
            summary,
        )
        return SyncOutcome(changes=tuple(changes), failure=reason)
    logger.info("node_modules_update_started root=%s changes=%s", root, summary)
    progress(
        "Updating the JavaScript packages for Cohere/Granite to match this "
        "version of the app (once per update, can take a few minutes)."
    )
    started = time.monotonic()
    try:
        _install_and_swap(root, npm_path, probe)
    except _UpdateDeferred as exc:
        logger.info(
            "node_modules_update_deferred root=%s in_use=%s changes=%s",
            root,
            exc,
            summary,
        )
        with contextlib.suppress(OSError):
            _discard_staging(root)
        return SyncOutcome(changes=tuple(changes), deferred=True)
    except _UpdateFailed as exc:
        logger.warning(
            "node_modules_update_failed root=%s reason=%s changes=%s detail=%s",
            root,
            exc.reason,
            summary,
            exc.detail,
        )
        with contextlib.suppress(OSError):
            _discard_staging(root)
        return SyncOutcome(changes=tuple(changes), failure=exc.reason)
    removed = False
    with contextlib.suppress(OSError):
        removed = _discard_staging(root)
    logger.info(
        "node_modules_updated root=%s elapsed_s=%.1f previous_tree_removed=%s changes=%s",
        root,
        time.monotonic() - started,
        removed,
        summary,
    )
    progress("JavaScript packages for Cohere/Granite updated.")
    return SyncOutcome(changes=tuple(changes), updated=True)
