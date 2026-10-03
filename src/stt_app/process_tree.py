"""Ending a child process together with the processes it started.

`subprocess.run(timeout=...)` kills the direct child only and then reads its
pipes to the end, so a grandchild that inherited them -- what `wsl.exe -e ...`
or a `.cmd` wrapper starts -- holds the call open for its whole life: a key
command with a 2 s timeout took 15.1 s that way. `run_bounded` is the bounded
form, and `kill_process_tree` the one place that knows how to end a tree.
"""

from __future__ import annotations

import contextlib
import locale
import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence

# How long the pipes are read after the tree was killed. A descendant that
# survived the kill (a policy refused it, it left the tree) still holds them,
# and an unbounded read would wait for it to exit.
_DRAIN_AFTER_KILL_S = 2.0
# How long the pipes may stay open after the direct child exited with output
# on stdout, or with a failure. Its output is complete then; what still holds
# the pipes is a descendant it left running, which is ended so the read can
# finish. A child that exited with code 0 and printed nothing is different:
# what holds the pipes may be the tool it started and did not wait for, which
# has not printed yet, so the caller may grant that case a longer grace
# (`silent_exit_grace_s`).
_PIPES_GRACE_AFTER_EXIT_S = 0.5
# How often `run_bounded` looks whether the direct child has exited while it
# reads the pipes.
_POLL_SLICE_S = 0.1
_READ_CHUNK_BYTES = 65536


def kill_process_tree(process: subprocess.Popen) -> None:
    """Every road to ending the child and its descendants, failures swallowed.

    On POSIX the child must have been started in a session of its own
    (`start_new_session=True`), which makes its pid the group id `killpg`
    addresses -- also after the child itself exited, as long as one member
    of the group lives. On Windows `taskkill /T` walks the parent links, so
    it cannot reach a descendant whose parent already exited; `run_bounded`
    puts its child into a job object for that case.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                timeout=5,
                check=False,
            )
            process.wait(timeout=3.0)
            return
        except Exception:
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3.0)
            return
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3.0)
                return
            except Exception:
                pass
    try:
        process.terminate()
        process.wait(timeout=3.0)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=3.0)
        except Exception:
            pass


class _WindowsJob:
    """A job object holding the child and everything it starts (Windows).

    The child is created suspended, put into the job and only then resumed,
    so not even a descendant it starts in its first instruction escapes.
    `terminate` ends every process still in the job, an orphaned grandchild
    included. Closing the handle does not kill anything (no
    `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`): a helper that detached a daemon
    from its stdio on a normal exit leaves it running, as before.
    """

    CREATE_SUSPENDED = 0x00000004

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        ntdll = ctypes.WinDLL("ntdll")
        # Undocumented but stable since Windows XP; `Popen` closes the
        # child's main-thread handle, so `ResumeThread` has nothing to use.
        ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        ntdll.NtResumeProcess.restype = ctypes.c_long
        self._kernel32 = kernel32
        self._ntdll = ntdll
        self._get_last_error = ctypes.get_last_error
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(self._get_last_error(), "CreateJobObjectW failed")
        self._handle = handle

    def adopt_and_resume(self, process: subprocess.Popen) -> bool:
        """Put the suspended child into the job and let it run.

        Returns whether the child is in the job. It runs either way; a
        resume that fails ends it, since a suspended child would hold the
        call for its whole timeout.
        """
        process_handle = int(process._handle)  # type: ignore[attr-defined]
        adopted = bool(
            self._kernel32.AssignProcessToJobObject(self._handle, process_handle)
        )
        if self._ntdll.NtResumeProcess(process_handle) < 0:
            process.kill()
            raise OSError("the key command's process could not be resumed")
        return adopted

    def terminate(self) -> None:
        self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def run_bounded(
    arguments: Sequence[str],
    *,
    timeout: float,
    silent_exit_grace_s: float | None = None,
    **popen_kwargs,
) -> subprocess.CompletedProcess:
    """`subprocess.run(..., capture_output=True, timeout=...)`, bounded.

    Same result object and the same `subprocess.TimeoutExpired`, but:

    - a child that outlives `timeout` -- or an interrupt landing while it
      runs -- takes its whole process tree with it, and the read after the
      kill is bounded too;
    - a child that exited while a descendant still holds the inherited
      pipes returns its own output and exit code once the pipes have stayed
      open `_PIPES_GRACE_AFTER_EXIT_S` past its exit, and that descendant is
      ended. Waiting for EOF instead failed a key command whose token had
      arrived with exit code 0 (review of 2026-10-01). A child that exited 0
      with nothing on stdout may have left its tool to print it, so the pipes
      stay open `silent_exit_grace_s` instead (default: the same grace; review
      of dedcde4, 2026-10-01). After the grace the tree is ended and what was
      read is returned -- not a timeout: the child did finish.

    Worst case: `timeout` plus the kill (taskkill's 5 s, a 3 s wait) plus
    `_DRAIN_AFTER_KILL_S`.
    """
    # The pipes are read as bytes (see `_PipeReader`) and decoded here, so the
    # text-mode options are taken out of what reaches `Popen`.
    encoding = popen_kwargs.pop("encoding", None)
    errors = popen_kwargs.pop("errors", None)
    text = bool(
        popen_kwargs.pop("text", False)
        or popen_kwargs.pop("universal_newlines", False)
        or encoding
        or errors
    )
    job: _WindowsJob | None = None
    if os.name == "nt":
        try:
            job = _WindowsJob()
        except OSError:
            job = None
        if job is not None:
            popen_kwargs["creationflags"] = (
                int(popen_kwargs.get("creationflags", 0)) | job.CREATE_SUSPENDED
            )
    else:
        popen_kwargs.setdefault("start_new_session", True)
    try:
        process = subprocess.Popen(
            list(arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
    except BaseException:
        if job is not None:
            job.close()
        raise
    try:
        if job is not None and not job.adopt_and_resume(process):
            # Already in a job that refuses nesting: taskkill is the fallback.
            job.close()
            job = None
        return _communicate_bounded(
            process,
            arguments,
            timeout,
            job,
            silent_exit_grace_s=silent_exit_grace_s,
            text=text,
            encoding=encoding,
            errors=errors,
        )
    finally:
        if job is not None:
            job.close()


def _end_tree(process: subprocess.Popen, job: _WindowsJob | None) -> None:
    if job is not None:
        with contextlib.suppress(Exception):
            job.terminate()
    kill_process_tree(process)


class _PipeReader(threading.Thread):
    """Collects one pipe's bytes as they arrive.

    `communicate` shows nothing until the pipe closes, and a descendant can
    keep it open long after the child is gone; with the bytes collected here
    the caller can tell "the token is already there" from "nothing yet".
    `read1` returns what is available instead of waiting for a full buffer.
    """

    def __init__(self, stream) -> None:
        super().__init__(name="stt_app_pipe_reader", daemon=True)
        self._stream = stream
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self.done = threading.Event()

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read1(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                with self._lock:
                    self._chunks.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self.done.set()

    def has_output(self) -> bool:
        with self._lock:
            return bool(self._chunks)

    def data(self) -> bytes:
        with self._lock:
            return b"".join(self._chunks)


def _decode(data: bytes, encoding: str | None, errors: str | None) -> str:
    """What a text-mode pipe reads: decoded, with universal newlines."""
    codec = encoding or locale.getpreferredencoding(False)
    text = data.decode(codec, errors or "strict")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _communicate_bounded(
    process: subprocess.Popen,
    arguments: Sequence[str],
    timeout: float,
    job: _WindowsJob | None,
    *,
    silent_exit_grace_s: float | None,
    text: bool,
    encoding: str | None,
    errors: str | None,
) -> subprocess.CompletedProcess:
    readers = (_PipeReader(process.stdout), _PipeReader(process.stderr))
    stdout_reader, stderr_reader = readers
    silent_grace = (
        _PIPES_GRACE_AFTER_EXIT_S
        if silent_exit_grace_s is None
        else silent_exit_grace_s
    )

    def finish_readers(wait_s: float) -> None:
        limit = time.monotonic() + wait_s
        for reader in readers:
            if reader.ident is not None:  # one that never started cannot be joined
                reader.join(timeout=max(0.0, limit - time.monotonic()))

    deadline = time.monotonic() + timeout
    exited_at: float | None = None
    try:
        # Inside the try: a thread that cannot be started (RuntimeError,
        # MemoryError) must still end the child, which would otherwise run on.
        for reader in readers:
            reader.start()
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise subprocess.TimeoutExpired(list(arguments), timeout)
            exited = process.poll() is not None
            if exited and all(reader.done.is_set() for reader in readers):
                break
            if exited:
                exited_at = exited_at or now
                silent = process.returncode == 0 and not stdout_reader.has_output()
                grace = silent_grace if silent else _PIPES_GRACE_AFTER_EXIT_S
                if now - exited_at >= grace:
                    # The child is done and its output complete; what holds
                    # the pipes is a descendant it left behind.
                    _end_tree(process, job)
                    finish_readers(_DRAIN_AFTER_KILL_S)
                    break
            slice_s = min(_POLL_SLICE_S, deadline - now)
            if exited:
                time.sleep(slice_s)
            else:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=slice_s)
    except BaseException:
        _end_tree(process, job)
        finish_readers(_DRAIN_AFTER_KILL_S)
        raise
    stdout, stderr = stdout_reader.data(), stderr_reader.data()
    # A pipe whose reader finished can be closed; a survivor's is left to its
    # daemon thread, because closing a pipe another thread reads blocks on
    # Windows until that read returns.
    for reader, stream in zip(readers, (process.stdout, process.stderr), strict=True):
        if reader.done.is_set():
            with contextlib.suppress(OSError, ValueError):
                stream.close()
    if text:
        return subprocess.CompletedProcess(
            list(arguments),
            process.returncode,
            _decode(stdout, encoding, errors),
            _decode(stderr, encoding, errors),
        )
    return subprocess.CompletedProcess(
        list(arguments), process.returncode, stdout, stderr
    )
