"""Tests for the single process-wide download slot.

Two independent downloaders used to race the same cache directory: the
controller's preload path and the Local tab's queue.
"""

from __future__ import annotations

import threading
import time

from stt_app.model_download_coordinator import (
    ACQUIRE_DOWNLOAD,
    ACQUIRE_JOINED,
    ModelDownloadCanceled,
    ModelDownloadCoordinator,
    model_download_coordinator,
)

import pytest


def test_a_second_downloader_waits_instead_of_racing_the_first():
    """Both paths spawning a worker against the same directory is what made the
    Local tab sit at 0%: it measured a directory the other process owned."""
    coordinator = ModelDownloadCoordinator()
    assert coordinator.acquire("m", "", explicit=False) == ACQUIRE_DOWNLOAD

    started = threading.Event()
    outcome: list[str] = []

    def second() -> None:
        started.set()
        outcome.append(coordinator.acquire("other", "", explicit=True))

    thread = threading.Thread(target=second, daemon=True)
    thread.start()
    started.wait(timeout=2)
    time.sleep(0.3)
    assert outcome == [], "second downloader must not run while the first holds the slot"

    coordinator.release("m", "", succeeded=True)
    thread.join(timeout=5)
    assert outcome == [ACQUIRE_DOWNLOAD]


def test_waiting_for_the_same_model_joins_instead_of_downloading_twice():
    coordinator = ModelDownloadCoordinator()
    coordinator.acquire("same", "", explicit=False)

    outcome: list[str] = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            coordinator.acquire("same", "", explicit=True)
        ),
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)
    coordinator.release("same", "", succeeded=True)
    thread.join(timeout=5)

    assert outcome == [ACQUIRE_JOINED]


def test_a_failed_download_does_not_let_the_waiter_skip_its_own():
    coordinator = ModelDownloadCoordinator()
    coordinator.acquire("same", "", explicit=False)

    outcome: list[str] = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            coordinator.acquire("same", "", explicit=True)
        ),
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)
    coordinator.release("same", "", succeeded=False)
    thread.join(timeout=5)

    assert outcome == [ACQUIRE_DOWNLOAD]


def test_explicit_interest_covers_a_request_that_is_still_waiting():
    """The preload path deletes partial files when its download is cancelled.
    A user request queued behind it must keep them: it resumes from them, and
    wiping them restarted a multi-gigabyte download from zero."""
    coordinator = ModelDownloadCoordinator()
    coordinator.acquire("model", "", explicit=False)
    assert coordinator.has_explicit_interest("model", "") is False

    thread = threading.Thread(
        target=lambda: coordinator.acquire("model", "", explicit=True),
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)

    assert coordinator.has_explicit_interest("model", "") is True

    coordinator.release("model", "", succeeded=True)
    thread.join(timeout=5)


def test_a_waiting_caller_honours_its_own_cancel():
    coordinator = ModelDownloadCoordinator()
    coordinator.acquire("busy", "", explicit=False)

    with pytest.raises(ModelDownloadCanceled):
        coordinator.acquire("queued", "", explicit=True, cancel_check=lambda: True)

    # A cancelled waiter must not leave its interest behind.
    assert coordinator.has_explicit_interest("queued", "") is False


def test_release_by_a_non_owner_leaves_the_real_claim_intact():
    coordinator = ModelDownloadCoordinator()
    coordinator.acquire("owner", "", explicit=False)
    coordinator.release("someone-else", "", succeeded=True)
    active = coordinator.active()
    assert active is not None and active.model_name == "owner"


def test_the_app_shares_one_coordinator():
    assert model_download_coordinator() is model_download_coordinator()


def test_concurrent_callers_never_overlap_and_never_leak():
    """The whole point is that two downloads can never touch the same cache
    directory at once. Exercised with mixed explicit/implicit callers, random
    failures and random cancels, since those are the paths that skip the normal
    release."""
    import random

    coordinator = ModelDownloadCoordinator()
    rng = random.Random(20260818)
    in_flight: list[str] = []
    peak = 0
    guard = threading.Lock()

    def worker(index: int) -> None:
        nonlocal peak
        model = ("a", "b", "c")[index % 3]
        explicit = index % 2 == 0
        cancels = index % 9 == 0
        try:
            outcome = coordinator.acquire(
                model, "", explicit=explicit, cancel_check=lambda: cancels
            )
        except ModelDownloadCanceled:
            return
        if outcome == ACQUIRE_JOINED:
            return
        succeeded = rng.random() < 0.7
        try:
            with guard:
                in_flight.append(model)
                peak = max(peak, len(in_flight))
            time.sleep(0.002)
        finally:
            with guard:
                in_flight.remove(model)
            coordinator.release(model, "", succeeded=succeeded)

    threads = [
        threading.Thread(target=worker, args=(index,), daemon=True)
        for index in range(60)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not [t for t in threads if t.is_alive()], "a caller deadlocked"
    assert peak == 1, f"{peak} downloads ran at once"
    assert coordinator.active() is None
    assert not [m for m in ("a", "b", "c") if coordinator.has_explicit_interest(m, "")]


def test_the_slot_takes_a_cross_process_lock_on_the_cache_dir(monkeypatch, tmp_path):
    """Wiring guard: the OS lock must be held for the whole download.

    Without this the coordinator is process-local again, and the benchmark
    worker or a second copy of the app can write the same cache directory.
    """
    from stt_app import model_download_coordinator as coordinator_module

    monkeypatch.setattr(
        coordinator_module, "_download_lock_dir", lambda: tmp_path / "locks"
    )
    held = []

    class _RecordingLock:
        def __init__(self, resource, *, lock_dir):
            self.resource = resource

        def acquire(self, *, cancel_check=None, poll_seconds=0.1):
            held.append(("acquire", self.resource))
            return True

        def release(self):
            held.append(("release", self.resource))

    monkeypatch.setattr(coordinator_module, "CrossProcessLock", _RecordingLock)

    coordinator = coordinator_module.ModelDownloadCoordinator()
    monkeypatch.setattr(
        coordinator_module, "model_download_coordinator", lambda: coordinator
    )
    during = []
    coordinator_module.run_coordinated_download(
        "some-model",
        r"C:\models",
        lambda: during.append(list(held)),
    )

    assert during == [[("acquire", r"C:\models")]]
    assert held == [("acquire", r"C:\models"), ("release", r"C:\models")]


def test_an_empty_model_dir_maps_onto_one_shared_lock_identity():
    """Every caller with no configured Model Dir shares the default HF cache."""
    from stt_app.model_download_coordinator import _cache_lock_resource

    assert _cache_lock_resource("") == _cache_lock_resource("   ")
    assert _cache_lock_resource("") != _cache_lock_resource(r"C:\models")


def test_cancelling_while_waiting_for_another_process_frees_the_slot(
    monkeypatch, tmp_path
):
    """A cancel during the cross-process wait must not strand the slot.

    The in-process slot is claimed before the OS lock is attempted, so an
    exception there would leave `_active` set for the process lifetime and no
    download could ever start again.
    """
    from stt_app import model_download_coordinator as coordinator_module

    monkeypatch.setattr(
        coordinator_module, "_download_lock_dir", lambda: tmp_path / "locks"
    )

    class _NeverAvailableLock:
        def __init__(self, resource, *, lock_dir):
            pass

        def acquire(self, *, cancel_check=None, poll_seconds=0.1):
            return False  # the caller's cancel_check fired

        def release(self):  # pragma: no cover - never reached
            raise AssertionError("released a lock that was never acquired")

    monkeypatch.setattr(
        coordinator_module, "CrossProcessLock", _NeverAvailableLock
    )
    coordinator = coordinator_module.ModelDownloadCoordinator()

    with pytest.raises(coordinator_module.ModelDownloadCanceled):
        coordinator.acquire("m", r"C:\models", explicit=True, cancel_check=lambda: True)

    assert coordinator.active() is None, "the slot stayed held after a cancel"
    assert not coordinator.has_explicit_interest("m", r"C:\models")
    # And the slot is genuinely reusable afterwards.
    monkeypatch.setattr(
        coordinator_module,
        "CrossProcessLock",
        type(
            "_OkLock",
            (),
            {
                "__init__": lambda self, resource, *, lock_dir: None,
                "acquire": lambda self, *, cancel_check=None, poll_seconds=0.1: True,
                "release": lambda self: None,
            },
        ),
    )
    assert coordinator.acquire("m", r"C:\models", explicit=False) == (
        coordinator_module.ACQUIRE_DOWNLOAD
    )
    coordinator.release("m", r"C:\models", succeeded=True)


def test_an_unlockable_filesystem_still_allows_downloads(monkeypatch, tmp_path):
    """A share that cannot lock must degrade, not block downloading entirely."""
    from stt_app import model_download_coordinator as coordinator_module

    monkeypatch.setattr(
        coordinator_module, "_download_lock_dir", lambda: tmp_path / "locks"
    )

    class _BrokenLock:
        def __init__(self, resource, *, lock_dir):
            pass

        def acquire(self, *, cancel_check=None, poll_seconds=0.1):
            raise coordinator_module.FileLockUnavailable("no locking here")

        def release(self):  # pragma: no cover - never acquired
            raise AssertionError("released a lock that was never acquired")

    monkeypatch.setattr(coordinator_module, "CrossProcessLock", _BrokenLock)
    coordinator = coordinator_module.ModelDownloadCoordinator()
    monkeypatch.setattr(
        coordinator_module, "model_download_coordinator", lambda: coordinator
    )

    ran = []
    assert coordinator_module.run_coordinated_download(
        "m", r"C:\models", lambda: ran.append(True)
    )
    assert ran == [True]
    assert coordinator.active() is None
