"""Process-wide serialization of local model downloads.

Two independent downloaders used to exist: the controller's preload path (a
model is selected and Save starts fetching it) and the Local tab's download
queue. Both spawned their own worker process against the same Hugging Face
cache directory, which produced three user-visible failures:

* Selecting an uncached model and pressing Save downloaded it, but the Local
  tab showed no download at all — it only knew about its own queue.
* Starting the Local tab download for that same model then sat at 0%: progress
  is measured as growth of the destination directory, and the other process
  owned it.
* Switching to a different model terminated the preload download mid-file, so a
  multi-gigabyte model stopped after a few hundred megabytes even though the
  user had explicitly asked for it in the Local tab.

Every download now passes through one coordinator. At most one runs at a time,
a second request for the *same* model waits for the running one instead of
starting a rival process, and an explicit (user-requested) download is never
cancelled by the implicit preload path.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
import logging
from dataclasses import dataclass

# Poll interval while waiting for the active download to finish. Short enough
# that a cancel is honoured promptly, long enough not to spin.
logger = logging.getLogger(__name__)

_WAIT_POLL_SECONDS = 0.1

ACQUIRE_DOWNLOAD = "download"
ACQUIRE_JOINED = "joined"


class ModelDownloadCanceled(RuntimeError):
    """Raised when a caller's own cancel check fires while it waits its turn."""


@dataclass(frozen=True, slots=True)
class ActiveModelDownload:
    model_name: str
    model_dir: str
    explicit: bool


class ModelDownloadCoordinator:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active: ActiveModelDownload | None = None
        # Completion counter per model, not a set: several callers can be
        # waiting for the same model, and each of them must learn it is done.
        self._completed: dict[tuple[str, str], int] = {}
        # Models an explicit (user-requested) caller is running *or waiting for*.
        # A waiting request counts: the implicit path must not delete the
        # partial download the waiting caller is about to resume from.
        self._explicit_interest: dict[tuple[str, str], int] = {}

    # -- observation ------------------------------------------------------

    def active(self) -> ActiveModelDownload | None:
        with self._condition:
            return self._active

    # -- claiming ---------------------------------------------------------

    def acquire(
        self,
        model_name: str,
        model_dir: str,
        *,
        explicit: bool,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str:
        """Claim the right to download, waiting for any download in flight.

        Returns ``ACQUIRE_DOWNLOAD`` when the caller owns the slot and must run
        the download, or ``ACQUIRE_JOINED`` when the very same model finished
        while this caller waited, in which case there is nothing left to do but
        re-check the cache.
        """
        key = (model_name, model_dir)
        with self._condition:
            completed_before = self._completed.get(key, 0)
            if explicit:
                self._explicit_interest[key] = self._explicit_interest.get(key, 0) + 1
        try:
            return self._acquire_slot(
                key, model_name, model_dir, explicit, cancel_check, completed_before
            )
        except BaseException:
            if explicit:
                self._drop_explicit_interest(key)
            raise

    def _acquire_slot(
        self,
        key: tuple[str, str],
        model_name: str,
        model_dir: str,
        explicit: bool,
        cancel_check: Callable[[], bool] | None,
        completed_before: int,
    ) -> str:
        with self._condition:
            while self._active is not None:
                if cancel_check is not None and cancel_check():
                    raise ModelDownloadCanceled("Model download canceled.")
                waiting_for_same_model = (
                    self._active.model_name == model_name
                    and self._active.model_dir == model_dir
                )
                self._condition.wait(_WAIT_POLL_SECONDS)
                if (
                    waiting_for_same_model
                    and self._completed.get(key, 0) > completed_before
                ):
                    if explicit:
                        self._drop_explicit_interest(key)
                    return ACQUIRE_JOINED
            self._active = ActiveModelDownload(model_name, model_dir, bool(explicit))
        return ACQUIRE_DOWNLOAD

    def _drop_explicit_interest(self, key: tuple[str, str]) -> None:
        with self._condition:
            remaining = self._explicit_interest.get(key, 0) - 1
            if remaining > 0:
                self._explicit_interest[key] = remaining
            else:
                self._explicit_interest.pop(key, None)

    def release(self, model_name: str, model_dir: str, *, succeeded: bool) -> None:
        with self._condition:
            active = self._active
            explicit_release = active is not None and active.explicit
            if (
                active is None
                or active.model_name != model_name
                or active.model_dir != model_dir
            ):
                # Not ours to release; leave the real owner's claim intact. This
                # should be unreachable -- every caller releases what it
                # acquired -- and if it ever happens the slot stays held and
                # every later download blocks, so say so loudly.
                logger.warning(
                    "Ignoring a download release for %r that does not own the "
                    "slot (held by %r).",
                    (model_name, model_dir),
                    None if active is None else (active.model_name, active.model_dir),
                )
                return
            self._active = None
            if succeeded:
                key = (model_name, model_dir)
                self._completed[key] = self._completed.get(key, 0) + 1
            self._condition.notify_all()
        if explicit_release:
            self._drop_explicit_interest((model_name, model_dir))

    def register_explicit_interest(self, model_name: str, model_dir: str) -> None:
        """Mark a user-requested model as wanted before it reaches the slot.

        A Local-tab queue entry that is still waiting its turn is just as much
        a user request as the one running: the preload path must not delete the
        partial bytes it is going to resume from.
        """
        key = (model_name, model_dir)
        with self._condition:
            self._explicit_interest[key] = self._explicit_interest.get(key, 0) + 1

    def drop_explicit_interest(self, model_name: str, model_dir: str) -> None:
        self._drop_explicit_interest((model_name, model_dir))

    def has_explicit_interest(self, model_name: str, model_dir: str) -> bool:
        """Whether the user asked for this model in the Local tab.

        True while such a request is running *or queued behind* another
        download. The preload path checks this before deleting partial files on
        a cancel: the user requested the model independently of what is
        currently selected, and wiping the partial makes the waiting request
        restart from zero.
        """
        with self._condition:
            return self._explicit_interest.get((model_name, model_dir), 0) > 0


_COORDINATOR = ModelDownloadCoordinator()


def model_download_coordinator() -> ModelDownloadCoordinator:
    """The single coordinator every download path must go through."""
    return _COORDINATOR


def run_coordinated_download(
    model_name: str,
    model_dir: str,
    download: Callable[[], object],
    *,
    explicit: bool = False,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """Run ``download`` while holding the single slot.

    Returns False when another caller finished this exact model while we
    waited, in which case nothing was fetched here and the caller should simply
    re-check its cache. Transcribers download from their own load path (a cache
    miss during preload or transcription), and those downloads have to take the
    slot too — otherwise "there is exactly one download slot" is only true for
    the two paths that happen to go through the worker process.
    """
    coordinator = model_download_coordinator()
    outcome = coordinator.acquire(
        model_name, model_dir, explicit=explicit, cancel_check=cancel_check
    )
    if outcome == ACQUIRE_JOINED:
        return False
    succeeded = False
    try:
        download()
        succeeded = True
    finally:
        coordinator.release(model_name, model_dir, succeeded=succeeded)
    return True
