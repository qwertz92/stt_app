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

from stt_app.file_lock import CrossProcessLock, lock_path_for

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
        assert lock.acquire()
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
    _wait_for_file(ready)
    holder.kill()
    holder.wait(timeout=30)

    lock = CrossProcessLock("cache-b", lock_dir=lock_dir)
    started = time.monotonic()
    assert lock.acquire()
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
        assert other.acquire()
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
