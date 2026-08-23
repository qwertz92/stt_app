"""Cross-process advisory locking backed by the operating system.

The download coordinator serializes downloads inside one process. That is not
enough on its own: the out-of-process benchmark worker, a second copy of the
app, and `scripts/download_model.py` are separate processes, and two of them
writing one Hugging Face cache directory corrupts partial files and makes the
progress reading (directory growth) meaningless.

This uses a real OS lock (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere)
rather than a PID file on purpose. The kernel drops the lock when the owning
process exits for *any* reason -- crash, kill, power loss -- so there is no
stale-lock state to detect, no heartbeat to maintain, and no timeout that
guesses whether the other side is still alive. A PID file gets all three of
those wrong at some point, and the failure mode is a permanently unusable
download.

Locks are advisory between cooperating processes; nothing stops an unrelated
program from writing the same directory.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

if os.name == "nt":  # pragma: no cover - platform split
    import msvcrt
else:  # pragma: no cover - platform split
    import fcntl

_POLL_SECONDS = 0.1

# Resources this process holds, so a reentrant attempt can be reported instead
# of spinning on an EACCES that looks exactly like another process's lock.
_HELD_LOCK = threading.Lock()
_HELD_RESOURCES: set[str] = set()


class FileLockUnavailable(RuntimeError):
    """Raised when the lock file itself cannot be created or locked at all."""


def lock_path_for(resource: str, *, lock_dir: Path) -> Path:
    """Return the lock file representing ``resource``.

    The name is a hash rather than the path itself: a cache directory path can
    exceed the filename limit, and it contains separators and drive letters that
    are not legal in a filename.
    """
    digest = hashlib.sha256(os.path.normcase(str(resource)).encode("utf-8"))
    return Path(lock_dir) / f"{digest.hexdigest()[:32]}.lock"


class LockHeldInThisProcess(RuntimeError):
    """Raised when this process already holds the lock it is asking for."""


class CrossProcessLock:
    """An exclusive, OS-enforced lock on one resource.

    Not reentrant, and the failure is loud rather than silent. Measured on
    Windows, a second lock attempt on a resource this process already holds --
    through a new handle *or* the same one -- fails with ``EACCES``, which is
    indistinguishable from "another process holds it", so `acquire()` would spin
    forever at 10 Hz while logging that it is waiting for another process. That
    is a lie that would misdirect any later diagnosis, and there is no timeout
    to end it. `_HELD_RESOURCES` therefore tracks what this process holds and
    turns the second attempt into an immediate `LockHeldInThisProcess`.

    Callers are expected to be serialized within the process already -- the
    download coordinator's single slot does that -- so this is a backstop.
    """

    def __init__(self, resource: str, *, lock_dir: Path) -> None:
        self._resource = resource
        self._path = lock_path_for(resource, lock_dir=lock_dir)
        self._handle = None
        self._held_key: str | None = None

    def _open(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Opened for writing because Windows byte-range locks require write
            # access on the handle.
            self._handle = open(self._path, "a+b")
        except OSError as exc:
            raise FileLockUnavailable(
                f"Could not open the lock file {self._path}: {exc}"
            ) from exc

    def _try_lock(self) -> bool:
        handle = self._handle
        assert handle is not None
        try:
            if os.name == "nt":  # pragma: no cover - platform split
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - platform split
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            # Measured: Windows `LK_NBLCK` reports a held lock as EACCES (13),
            # POSIX `flock` as EAGAIN (and EACCES on some systems). EDEADLK is
            # listed because POSIX may report a self-deadlock that way; on
            # Windows it is the same value as EACCES.
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise FileLockUnavailable(
                f"Locking {self._path} failed: {exc}"
            ) from exc

    def acquire(
        self,
        *,
        cancel_check: Callable[[], bool] | None = None,
        poll_seconds: float = _POLL_SECONDS,
    ) -> bool:
        """Block until the lock is held.

        Returns ``False`` when ``cancel_check`` asked to stop waiting; the lock
        is not held in that case.
        """
        key = os.path.normcase(str(self._path))
        with _HELD_LOCK:
            if key in _HELD_RESOURCES:
                raise LockHeldInThisProcess(
                    f"This process already holds the lock for {self._resource!r}. "
                    "Waiting would spin forever: the OS reports a self-held lock "
                    "the same way it reports another process's."
                )
        if self._handle is None:
            self._open()
        waited_for_another_process = False
        while True:
            if cancel_check is not None and cancel_check():
                self._close_handle()
                return False
            if self._try_lock():
                with _HELD_LOCK:
                    _HELD_RESOURCES.add(key)
                self._held_key = key
                if waited_for_another_process:
                    logger.info(
                        "model_download_lock acquired after waiting for another "
                        "process resource=%s",
                        self._resource,
                    )
                return True
            if not waited_for_another_process:
                waited_for_another_process = True
                logger.info(
                    "model_download_lock waiting for another process resource=%s "
                    "lock=%s",
                    self._resource,
                    self._path,
                )
            time.sleep(max(0.01, float(poll_seconds)))

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - platform split
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - platform split
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            logger.warning(
                "model_download_lock release failed resource=%s error=%s",
                self._resource,
                exc,
            )
        finally:
            self._close_handle()

    def _close_handle(self) -> None:
        key, self._held_key = self._held_key, None
        if key is not None:
            with _HELD_LOCK:
                _HELD_RESOURCES.discard(key)
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except OSError:
            pass
