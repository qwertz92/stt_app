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
