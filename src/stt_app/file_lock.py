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
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

if os.name == "nt":  # pragma: no cover - platform split
    import msvcrt
else:  # pragma: no cover - platform split
    import fcntl

_POLL_SECONDS = 0.1


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


class CrossProcessLock:
    """An exclusive, OS-enforced lock on one resource.

    Not reentrant: acquiring twice from the same process deadlocks on POSIX and
    raises on Windows. Callers must already be serialized within the process,
    which the download coordinator's single slot guarantees.
    """

    def __init__(self, resource: str, *, lock_dir: Path) -> None:
        self._resource = resource
        self._path = lock_path_for(resource, lock_dir=lock_dir)
        self._handle = None

    @property
    def path(self) -> Path:
        return self._path

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
            # Windows reports a held lock as EDEADLOCK/EACCES, POSIX as
            # EAGAIN/EACCES. Anything else is a real failure worth reporting.
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, 36):
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
        if self._handle is None:
            self._open()
        waited_for_another_process = False
        while True:
            if cancel_check is not None and cancel_check():
                self._close_handle()
                return False
            if self._try_lock():
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
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except OSError:
            pass

    def __enter__(self) -> CrossProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.release()
