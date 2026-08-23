"""The cross-process half of the download slot.

These use real subprocesses on purpose. A same-process test cannot show what
this module exists for -- the in-process `threading.Condition` already
serializes callers there -- and the OS lock is only observable between separate
processes.
"""

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from stt_app.file_lock import (
    CrossProcessLock,
    LockHeldInThisProcess,
    lock_path_for,
)

_REPO_SRC = Path(__file__).resolve().parents[1] / "src"


def _spawn_holder(lock_dir: Path, resource: str, hold_seconds: float, ready: Path):
    script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_SRC)!r})
        from stt_app.file_lock import CrossProcessLock
        lock = CrossProcessLock({resource!r}, lock_dir=Path({str(lock_dir)!r}))
        assert lock.acquire()
        Path({str(ready)!r}).write_text("locked", encoding="utf-8")
        time.sleep({hold_seconds!r})
        lock.release()
        """
    )
    return subprocess.Popen([sys.executable, "-c", script])


def _acquire_bounded(lock, timeout: float = 20.0, poll_seconds: float = 0.01):
    """Acquire with a deadline.

    `CrossProcessLock.acquire` waits forever by design -- correct in the app,
    wrong in a test: a holder that fails to release would hang the whole run
    with no output naming the cause, which is exactly the hazard the
    conftest fixtures exist to prevent.
    """
    deadline = time.monotonic() + timeout
    acquired = lock.acquire(
        cancel_check=lambda: time.monotonic() > deadline,
        poll_seconds=poll_seconds,
    )
    assert acquired, f"timed out after {timeout}s waiting for the lock"
    return acquired

def _wait_for_file(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def test_lock_is_held_against_another_process(tmp_path):
    lock_dir = tmp_path / "locks"
    ready = tmp_path / "ready"
    holder = _spawn_holder(lock_dir, "cache-a", 2.0, ready)
    try:
        _wait_for_file(ready)
        lock = CrossProcessLock("cache-a", lock_dir=lock_dir)
        started = time.monotonic()
        _acquire_bounded(lock)
        waited = time.monotonic() - started
        lock.release()
    finally:
        holder.wait(timeout=30)
    assert waited > 0.5, "the second process did not wait for the first"


def test_a_killed_holder_frees_the_lock(tmp_path):
    """No stale-lock recovery is needed because the kernel owns the lock."""
    lock_dir = tmp_path / "locks"
    ready = tmp_path / "ready"
    holder = _spawn_holder(lock_dir, "cache-b", 60.0, ready)
    try:
        _wait_for_file(ready)
    finally:
        # Without this a _wait_for_file timeout leaves a child sleeping for
        # a minute, holding the lock the rest of the run may need.
        holder.kill()
        holder.wait(timeout=30)

    lock = CrossProcessLock("cache-b", lock_dir=lock_dir)
    started = time.monotonic()
    _acquire_bounded(lock)
    lock.release()
    assert time.monotonic() - started < 5.0


def test_distinct_resources_do_not_block_each_other(tmp_path):
    lock_dir = tmp_path / "locks"
    ready = tmp_path / "ready"
    holder = _spawn_holder(lock_dir, "cache-c", 3.0, ready)
    try:
        _wait_for_file(ready)
        other = CrossProcessLock("cache-d", lock_dir=lock_dir)
        started = time.monotonic()
        _acquire_bounded(other)
        other.release()
        assert time.monotonic() - started < 1.0
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_cancel_check_stops_waiting_without_taking_the_lock(tmp_path):
    lock_dir = tmp_path / "locks"
    ready = tmp_path / "ready"
    holder = _spawn_holder(lock_dir, "cache-e", 10.0, ready)
    try:
        _wait_for_file(ready)
        lock = CrossProcessLock("cache-e", lock_dir=lock_dir)
        calls = []

        def cancel_check():
            calls.append(1)
            return len(calls) > 2

        assert lock.acquire(cancel_check=cancel_check, poll_seconds=0.01) is False
        # "without taking the lock" is the half that matters: a version that
        # takes the lock and *then* returns False passes the line above.
        assert lock._handle is None, "gave up waiting but kept a handle open"
        assert lock._held_key is None, "gave up waiting but stayed registered"
    finally:
        holder.kill()
        holder.wait(timeout=30)


@pytest.mark.parametrize(
    "resource",
    [r"C:\very\long\path\that\is\not\a\legal\filename", "", "a" * 500],
)
def test_lock_filename_is_always_usable(tmp_path, resource):
    path = lock_path_for(resource, lock_dir=tmp_path)
    assert path.parent == tmp_path
    assert path.suffix == ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert path.exists()

def test_after_release_another_process_gets_the_lock_immediately(tmp_path):
    """Release must free the lock for other processes, not just this one.

    Deliberately cross-process. Within one process the release is
    unfalsifiable: closing the handle frees the OS lock on its own, so a
    broken or missing unlock call is invisible. Only another process can
    show that the resource is genuinely free.
    """
    lock_dir = tmp_path / "locks"
    lock = CrossProcessLock("cache-f", lock_dir=lock_dir)
    assert lock.acquire()
    lock.release()
    assert lock._handle is None

    ready = tmp_path / "ready"
    holder = _spawn_holder(lock_dir, "cache-f", 0.1, ready)
    try:
        started = time.monotonic()
        _wait_for_file(ready, timeout=10.0)
        assert time.monotonic() - started < 5.0, (
            "another process could not take the lock after release"
        )
    finally:
        holder.wait(timeout=30)

def test_the_same_directory_spelled_differently_is_one_lock(tmp_path):
    """Case and separator folding is what makes the lock actually exclusive."""
    lock_dir = tmp_path / "locks"
    variants = [r"D:\Models\cache", "d:/models/CACHE", r"D:\models\cache"]
    paths = {lock_path_for(v, lock_dir=lock_dir) for v in variants}

    assert len(paths) == 1, f"one directory produced {len(paths)} locks: {paths}"
    assert lock_path_for("D:/other", lock_dir=lock_dir) not in paths


def test_a_reentrant_acquire_is_refused_instead_of_spinning(tmp_path):
    """A self-held lock reports EACCES, exactly like another process's.

    Without the registry, the second attempt polls forever at 10 Hz while
    logging that it is waiting for another process -- no timeout, no cancel
    path, and a misleading log to diagnose it by.
    """
    lock_dir = tmp_path / "locks"
    first = CrossProcessLock("cache-g", lock_dir=lock_dir)
    assert first.acquire()
    try:
        second = CrossProcessLock("cache-g", lock_dir=lock_dir)
        with pytest.raises(LockHeldInThisProcess):
            second.acquire(poll_seconds=0.01)
    finally:
        first.release()

    reusable = CrossProcessLock("cache-g", lock_dir=lock_dir)
    assert reusable.acquire(), "the refusal left the resource permanently held"
    reusable.release()


def test_the_coordinator_really_holds_the_os_lock_against_another_process(
    tmp_path, monkeypatch
):
    """End-to-end: the real coordinator, the real lock, a real subprocess.

    Every other coordinator test substitutes a fake lock class, so reverting
    the coordinator wiring entirely left them all green. This is the test
    that fails if the two halves are ever disconnected.
    """
    from stt_app import model_download_coordinator as coordinator_module

    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(coordinator_module, "_download_lock_dir", lambda: lock_dir)
    cache_dir = str(tmp_path / "cache")
    resource = coordinator_module._cache_lock_resource(cache_dir)
    ready = tmp_path / "ready"
    holder = _spawn_holder(lock_dir, resource, 3.0, ready)
    try:
        _wait_for_file(ready)
        started = time.monotonic()
        ran = []
        coordinator_module.run_coordinated_download(
            "some-model", cache_dir, lambda: ran.append(True)
        )
        waited = time.monotonic() - started
    finally:
        holder.wait(timeout=30)

    assert ran == [True]
    assert waited > 0.5, (
        "the coordinator downloaded while another process held the cache lock"
    )
