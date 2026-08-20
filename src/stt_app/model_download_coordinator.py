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
from dataclasses import dataclass

# Poll interval while waiting for the active download to finish. Short enough
# that a cancel is honoured promptly, long enough not to spin.
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
        self._completed: set[tuple[str, str]] = set()
        # Models an explicit (user-requested) caller is running *or waiting for*.
        # A waiting request counts: the implicit path must not delete the
        # partial download the waiting caller is about to resume from.
        self._explicit_interest: dict[tuple[str, str], int] = {}
        self._listeners: list[Callable[[], None]] = []

    # -- observation ------------------------------------------------------

    def active(self) -> ActiveModelDownload | None:
        with self._condition:
            return self._active

    def add_listener(self, callback: Callable[[], None]) -> None:
        with self._condition:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        with self._condition:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _notify(self) -> None:
        with self._condition:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback()
            except Exception:
                # A listener is UI glue; never let it break a download.
                pass

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
            self._completed.discard(key)
            if explicit:
                self._explicit_interest[key] = self._explicit_interest.get(key, 0) + 1
        try:
            return self._acquire_slot(key, model_name, model_dir, explicit, cancel_check)
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
                if waiting_for_same_model and key in self._completed:
                    self._completed.discard(key)
                    if explicit:
                        self._drop_explicit_interest(key)
                    return ACQUIRE_JOINED
            self._active = ActiveModelDownload(model_name, model_dir, bool(explicit))
        self._notify()
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
                # Not ours to release; leave the real owner's claim intact.
                return
            self._active = None
            if succeeded:
                self._completed.add((model_name, model_dir))
            self._condition.notify_all()
        if explicit_release:
            self._drop_explicit_interest((model_name, model_dir))
        self._notify()

    def has_explicit_interest(self, model_name: str, model_dir: str = "") -> bool:
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
