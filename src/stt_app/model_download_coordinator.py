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

Scope: the slot is enforced at two levels. Inside one process a
`threading.Condition` serializes callers and lets a second request for the same
model join the running one. Across processes -- the out-of-process benchmark
worker, `scripts/download_model.py`, a second Windows user sharing one
Model Dir -- an OS-level
lock on the cache directory (`file_lock.CrossProcessLock`) makes sure only one
of them writes it at a time. Both levels are needed: the in-process half
provides the join/interest behaviour the UI depends on, and only the OS half can
see another process at all.

The machine-wide lock is keyed on the *cache directory*, not on the model: two
writers corrupt each other through the shared blob and ref trees even when they
fetch different models, and the progress reading (directory growth) becomes
meaningless for both.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .app_paths import appdata_root
from .file_lock import CrossProcessLock, FileLockUnavailable

logger = logging.getLogger(__name__)


def _download_lock_dir():
    """Where the cross-process lock files live.

    Deliberately *not* inside the model cache: the inventory scan walks that
    tree and a stray lock file there would be read as an unknown model file.
    """
    return appdata_root() / "locks"


def _cache_lock_resource(model_dir: str) -> str:
    r"""Normalize a configured model dir into one lock identity.

    Two spellings of one directory must produce one lock, or both writers think
    they own it and the lock protects nothing. `os.path.normcase` alone is not
    enough -- it folds case and slashes but keeps `D:\models` and `D:\models\`
    apart, and leaves `..` segments and relative paths untouched.

    The empty case is resolved to the *actual* default Hugging Face cache path
    rather than a sentinel, because a user who types that path into Model Dir by
    hand is pointing at the very same directory the empty setting uses; a
    sentinel would give those two callers different locks.
    """
    normalized = str(model_dir or "").strip()
    if not normalized:
        from .transcriber.local_faster_whisper import _default_hf_cache_dir

        normalized = _default_hf_cache_dir()
    return os.path.normcase(os.path.abspath(os.path.normpath(normalized)))

# Poll interval while waiting for the active download to finish. Short enough
# that a cancel is honoured promptly, long enough not to spin.
_WAIT_POLL_SECONDS = 0.1

ACQUIRE_DOWNLOAD = "download"
ACQUIRE_JOINED = "joined"


class ModelDownloadCanceled(RuntimeError):
    """Raised when a caller's own cancel check fires while it waits its turn."""


# Set once the app is quitting. Without it a caller blocked in `acquire()` keeps
# waiting, and worse: the shutdown sequence releases the slot before the
# controller stops, so the waiter would *start* a fresh multi-gigabyte download
# on a non-daemon executor thread that the interpreter then joins at exit —
# a tray-less process still downloading for minutes.
_SHUTDOWN = threading.Event()


def request_download_shutdown() -> None:
    """Refuse any further waiting for the download slot."""
    _SHUTDOWN.set()


def download_shutdown_requested() -> bool:
    return _SHUTDOWN.is_set()


def reset_download_shutdown_for_tests() -> None:
    _SHUTDOWN.clear()


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
        # Callers parked in the wait loop, per model. Distinct from
        # `_explicit_interest`: a preload waiting for a model is implicit, so
        # the explicit-interest check cannot see it -- and cancelling a
        # Local-tab download for that same model then deleted the partial
        # bytes the parked preload was about to resume from, so it restarted
        # the multi-gigabyte fetch from zero seconds after the user cancelled
        # it.
        self._waiters: dict[tuple[str, str], int] = {}
        # Models an explicit (user-requested) caller is running *or waiting for*.
        # A waiting request counts: the implicit path must not delete the
        # partial download the waiting caller is about to resume from.
        self._explicit_interest: dict[tuple[str, str], int] = {}
        # The OS lock held by whoever owns the slot. Only the slot owner ever
        # touches it, so it needs no lock of its own beyond the condition.
        self._cache_lock: CrossProcessLock | None = None
        # True while a caller owns the in-process slot but is still waiting
        # for another process to release the cache directory. The UI needs
        # this: progress is directory growth, so while another process owns
        # the directory the bar reads a frozen 0% and the overlay claims to
        # be downloading. That misleading state is precisely what the slot
        # was built to remove -- reintroducing it across processes would be
        # the same bug one level up.
        self._waiting_for_other_process = False

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
        interest_already_registered: bool = False,
    ) -> str:
        """Claim the right to download, waiting for any download in flight.

        Returns ``ACQUIRE_DOWNLOAD`` when the caller owns the slot and must run
        the download, or ``ACQUIRE_JOINED`` when the very same model finished
        while this caller waited, in which case there is nothing left to do but
        re-check the cache.
        """
        key = (model_name, model_dir)
        if _SHUTDOWN.is_set():
            raise ModelDownloadCanceled("The application is shutting down.")
        with self._condition:
            completed_before = self._completed.get(key, 0)
            if explicit and not interest_already_registered:
                self._explicit_interest[key] = self._explicit_interest.get(key, 0) + 1
        try:
            return self._acquire_slot(
                key,
                model_name,
                model_dir,
                explicit,
                cancel_check,
                completed_before,
                interest_already_registered,
            )
        except BaseException:
            if explicit and not interest_already_registered:
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
        interest_already_registered: bool = False,
    ) -> str:
        with self._condition:
            # Registered unconditionally, and given back in the `finally`
            # below. Incrementing only when the slot looks busy is a race:
            # `release` sets `_active = None` and notifies, and a caller that
            # slips in before the parked thread wakes sees a free slot, skips
            # the increment, and then decrements the *parked* thread's
            # registration to zero -- after which a cancel deletes the bytes
            # that thread was waiting to resume from, the exact loss this
            # registry exists to stop. Nothing can observe the transient count
            # of a caller that takes the slot immediately: every reader holds
            # `self._condition`, which is only released inside `wait`.
            self._waiters[key] = self._waiters.get(key, 0) + 1
            try:
                while self._active is not None:
                    if _SHUTDOWN.is_set():
                        raise ModelDownloadCanceled(
                            "The application is shutting down."
                        )
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
                        if explicit and not interest_already_registered:
                            self._drop_explicit_interest(key)
                        return ACQUIRE_JOINED
            finally:
                # Every exit, including the JOINED return and a cancel raise.
                remaining = self._waiters.get(key, 0) - 1
                if remaining > 0:
                    self._waiters[key] = remaining
                else:
                    self._waiters.pop(key, None)
            self._active = ActiveModelDownload(model_name, model_dir, bool(explicit))

        # Held *outside* the condition: waiting for another process can take
        # minutes, and blocking the condition would freeze every observer
        # (`active()`, the progress poll, `has_explicit_interest`) with it.
        try:
            self._acquire_cache_lock(model_dir, cancel_check)
        except BaseException:
            # Give the in-process slot back; otherwise a cancel while waiting
            # for another process leaves the slot held for the process lifetime
            # and no download can ever start again.
            with self._condition:
                self._active = None
                self._waiting_for_other_process = False
                self._condition.notify_all()
            raise
        return ACQUIRE_DOWNLOAD

    def _acquire_cache_lock(
        self,
        model_dir: str,
        cancel_check: Callable[[], bool] | None,
    ) -> None:
        lock = CrossProcessLock(
            _cache_lock_resource(model_dir), lock_dir=_download_lock_dir()
        )

        def _should_stop() -> bool:
            if _SHUTDOWN.is_set():
                return True
            return cancel_check is not None and cancel_check()

        with self._condition:
            self._waiting_for_other_process = True
        try:
            acquired = lock.acquire(cancel_check=_should_stop)
        except FileLockUnavailable as exc:
            # A filesystem that cannot lock (some network shares) must not make
            # downloading impossible. The in-process slot still holds, so this
            # degrades to the previous behaviour rather than to nothing.
            logger.warning(
                "Cross-process download lock unavailable, falling back to "
                "process-local serialization only: %s",
                exc,
            )
            with self._condition:
                self._waiting_for_other_process = False
            return
        if not acquired:
            if _SHUTDOWN.is_set():
                raise ModelDownloadCanceled("The application is shutting down.")
            raise ModelDownloadCanceled("Model download canceled.")
        # Publishing the held lock is the one step that must not be skipped.
        # `acquire()` one frame up gives the in-process slot back on any
        # exception, but it cannot see this `lock`, so a raise between holding
        # it and storing it strands an *OS* lock: `_release_cache_lock` reads
        # `self._cache_lock` and would find None, and the lock is machine-wide,
        # so every other process wanting this cache directory blocks until this
        # one exits. `release()` is idempotent and swallows OSError, so the
        # defensive call is free.
        try:
            with self._condition:
                self._cache_lock = lock
                self._waiting_for_other_process = False
        except BaseException:
            lock.release()
            raise

    def _release_cache_lock(self) -> None:
        lock, self._cache_lock = self._cache_lock, None
        if lock is not None:
            lock.release()

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
                    "slot (held by %r). The cross-process lock stays with the "
                    "real owner; releasing it here would free a lock this "
                    "caller never took.",
                    (model_name, model_dir),
                    None if active is None else (active.model_name, active.model_dir),
                )
                return
            self._active = None
            if succeeded:
                key = (model_name, model_dir)
                self._completed[key] = self._completed.get(key, 0) + 1
            self._release_cache_lock()
            self._waiting_for_other_process = False
            self._condition.notify_all()
        if explicit_release:
            self._drop_explicit_interest((model_name, model_dir))

    def has_waiting_download(self, model_name: str, model_dir: str) -> bool:
        """Is another caller parked waiting to download this exact model?

        Used before deleting partial files: those bytes are what the waiter
        resumes from, and wiping them turns its wait into a fresh download.
        Unlike `has_explicit_interest` this sees implicit callers too, which is
        the case that bit -- a preload is implicit.
        """
        with self._condition:
            return self._waiters.get((model_name, model_dir), 0) > 0

    def downloading_other_model(self, model_name: str, model_dir: str) -> bool:
        """Is the slot held by a *different* model right now?

        Progress is measured as growth of the destination directory, so while
        another model owns the slot nothing is writing to ours and any
        percentage rendered for it is invented. That is the exact symptom the
        slot was built to remove, so the callers that render progress have to
        be able to ask.
        """
        with self._condition:
            active = self._active
            return active is not None and (
                active.model_name != model_name or active.model_dir != model_dir
            )

    def waiting_for_other_process(self) -> bool:
        """Is a download blocked on another process holding the cache dir?"""
        with self._condition:
            return self._waiting_for_other_process

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
