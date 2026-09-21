from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .model_download_progress import (
    DOWNLOAD_EVENT_PREFIX,
    DOWNLOAD_PROGRESS_UNKNOWN,
)

LOCAL_MODEL_DOWNLOAD_WORKER_ARG = "--local-model-download-worker"

_logger = logging.getLogger(__name__)

# Every wait on the download child is bounded. The caller is the download
# queue worker thread, and it holds the single in-process download slot --
# plus, through `file_lock`, the machine-wide one -- for as long as it is
# blocked here.
_TERMINATE_GRACE_S = 2.0
_KILL_GRACE_S = 2.0
_DRAIN_TIMEOUT_S = 5.0
# The progress reader ends with the child's stdout -- normally at the child's
# exit, but a grandchild that inherited the handle keeps the pipe open past
# it, and then the reader outlives this wait. `_close_progress_reader` says
# what happens to the stream in that case.
_READER_JOIN_TIMEOUT_S = 2.0


@dataclass(frozen=True, slots=True)
class DownloadBytesSample:
    """What the worker last said about its own transfer."""

    downloaded_bytes: int
    total_bytes: int
    measured_at: float


class _ProgressState:
    """The latest byte sample of one download child.

    Written by that child's reader thread, read from the Qt thread. One lock,
    because a `DownloadBytesSample` is replaced wholesale rather than mutated.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sample: DownloadBytesSample | None = None
        self._ever_reported = False
        # One warning per download child, not one per process: a module-level
        # latch keyed by model name exempted the second download of the same
        # model in a session from ever reporting a damaged event.
        self.malformed_logged = False

    def set(self, sample: DownloadBytesSample | None) -> None:
        with self._lock:
            self._sample = sample
            if sample is not None:
                self._ever_reported = True

    def get(self) -> DownloadBytesSample | None:
        with self._lock:
            return self._sample

    @property
    def ever_reported(self) -> bool:
        with self._lock:
            return self._ever_reported


def start_model_download_process(
    model_name: str,
    model_dir: str = "",
) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    command = model_download_command(model_name, model_dir, env)
    cwd = None if getattr(sys, "frozen", False) else str(_repo_root())
    # The worker can run for minutes and third-party download libraries may
    # write enough diagnostics to fill an unread pipe. A seekable temporary
    # file keeps the polling callers non-blocking while preserving the final
    # error message for display.
    error_log = tempfile.TemporaryFile(  # noqa: SIM115 (outlives this call)
        mode="w+t", encoding="utf-8"
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            # A pipe rather than DEVNULL, because the worker reports its own
            # byte counters on it. It is drained continuously by the reader
            # thread below -- a pipe nobody reads blocks the child once the
            # OS buffer fills, and this one is written from inside hf_xet's
            # progress callback.
            stdout=subprocess.PIPE,
            stderr=error_log,
            creationflags=_subprocess_no_window_flags(),
        )
    except Exception:
        error_log.close()
        raise
    process._stt_error_log = error_log  # type: ignore[attr-defined]
    _attach_progress_reader(process, model_name)
    return process


def _attach_progress_reader(process: subprocess.Popen[str], model_name: str) -> None:
    state = _ProgressState()
    process._stt_progress = state  # type: ignore[attr-defined]
    process._stt_progress_reader = None  # type: ignore[attr-defined]
    stream = process.stdout
    if stream is None:
        return
    reader = threading.Thread(
        target=_pump_progress,
        args=(stream, state, model_name),
        name="stt_app_model_download_progress",
        daemon=True,
    )
    try:
        reader.start()
    except RuntimeError:
        # The interpreter could not create another thread. Close our read end
        # rather than leave a pipe filling up behind the child: the worker's
        # own emit swallows the resulting write error, and the caller falls
        # back to measuring the destination directory.
        _logger.warning(
            "model_download_progress_reader_unavailable model=%s", model_name
        )
        try:
            stream.close()
        except Exception:
            pass
        return
    process._stt_progress_reader = reader  # type: ignore[attr-defined]


def _pump_progress(stream, state: _ProgressState, model_name: str) -> None:
    try:
        for line in stream:
            text = str(line).strip()
            if not text.startswith(DOWNLOAD_EVENT_PREFIX):
                # Library noise on stdout, or a worker from an older build.
                continue
            sample = _sample_from_event(
                text[len(DOWNLOAD_EVENT_PREFIX) :], state, model_name
            )
            if sample is _MALFORMED:
                continue
            state.set(sample)  # type: ignore[arg-type]
    except Exception:
        # A read that fails is the child going away, or our own end being
        # closed. Either way the caller falls back to directory growth.
        _logger.debug("model_download_progress_reader_stopped", exc_info=True)
    finally:
        if not state.ever_reported:
            # Names the case where an upgraded huggingface_hub stopped
            # feeding the hook: the app still works, on the old estimate.
            _logger.info("model_download_progress_absent model=%s", model_name)
        try:
            stream.close()
        except Exception:
            pass


class _Malformed:
    """Sentinel: this line said nothing, so the last sample still stands."""


_MALFORMED = _Malformed()


def _sample_from_event(payload: str, state: _ProgressState, model_name: str):
    try:
        event = json.loads(payload)
    except ValueError:
        return _log_malformed(state, model_name, payload)
    if not isinstance(event, dict) or event.get("event") != "bytes":
        return _log_malformed(state, model_name, payload)
    done = event.get("done")
    total = event.get("total")
    if not isinstance(done, int) or not isinstance(total, int):
        return _log_malformed(state, model_name, payload)
    if done == DOWNLOAD_PROGRESS_UNKNOWN:
        # The worker handed the download to a path it cannot measure.
        return None
    if done < 0 or total < 0:
        return _log_malformed(state, model_name, payload)
    return DownloadBytesSample(
        downloaded_bytes=done,
        total_bytes=total,
        measured_at=time.monotonic(),
    )


def _log_malformed(state: _ProgressState, model_name: str, payload: str):
    # Once per download: this runs per line, and a worker producing garbage
    # would otherwise fill the log with it. The latch is this child's own --
    # only its reader thread touches it, and the next download of the same
    # model gets its own line.
    if not state.malformed_logged:
        state.malformed_logged = True
        _logger.warning(
            "model_download_progress_event_malformed model=%s payload=%s",
            model_name,
            payload[:200],
        )
    return _MALFORMED


def model_download_process_progress(
    process: subprocess.Popen[str] | None,
) -> DownloadBytesSample | None:
    """The worker's own byte counts, or None when it has not reported any."""
    if process is None:
        return None
    state = getattr(process, "_stt_progress", None)
    if state is None:
        return None
    return state.get()


def model_download_command(
    model_name: str,
    model_dir: str,
    env: dict[str, str],
) -> list[str]:
    worker_args = [
        "--model",
        str(model_name or "").strip(),
        "--model-dir",
        str(model_dir or "").strip(),
    ]
    if getattr(sys, "frozen", False):
        return [sys.executable, LOCAL_MODEL_DOWNLOAD_WORKER_ARG, *worker_args]

    source_root = str(_package_source_dir())
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        source_root
        if not existing_pythonpath
        else os.pathsep.join((source_root, existing_pythonpath))
    )
    return [
        sys.executable,
        "-m",
        "stt_app.local_model_download_worker",
        *worker_args,
    ]


def terminate_model_download_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_TERMINATE_GRACE_S)
        return
    except Exception:
        pass
    try:
        process.kill()
    except Exception:
        pass
    try:
        # Reap it, or at least find out that we could not. Without this the
        # function returned while the child was still alive, and its callers go
        # straight on to delete the `*.incomplete` files that child may still
        # be writing. On Windows `terminate()` and `kill()` are the same
        # `TerminateProcess` call, so a child that outlived the first wait is
        # not going to fall over on the second one either -- typically one
        # wedged in an uninterruptible kernel read on the download socket.
        process.wait(timeout=_KILL_GRACE_S)
    except Exception:
        pass


def model_download_process_error(process: subprocess.Popen[str]) -> str:
    error_log = getattr(process, "_stt_error_log", None)
    try:
        # `wait`, not `communicate`: stdout is drained by this process's own
        # progress reader thread, and `communicate` would read the same pipe
        # from a second thread of its own.
        process.wait(timeout=_DRAIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # A child that survived terminate and kill still owns the pipe, and an
        # unbounded wait there blocked this thread permanently: the Settings
        # cancel never completed, the download slot was never handed back, and
        # every later download in this process -- plus the benchmark worker and
        # `scripts/download_model.py`, which share the machine-wide lock --
        # waited on it until the app was restarted. Killing and waiting once
        # more is bounded for the same reason.
        try:
            process.kill()
            process.wait(timeout=_DRAIN_TIMEOUT_S)
        except Exception:
            pass
    except Exception:
        pass
    _close_progress_reader(process)
    stderr = ""
    if error_log is not None:
        try:
            error_log.flush()
            error_log.seek(0)
            stderr = error_log.read()
        except Exception:
            stderr = ""
        finally:
            try:
                error_log.close()
            except Exception:
                pass
            process._stt_error_log = None  # type: ignore[attr-defined]
    lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def release_model_download_process(process: subprocess.Popen[str] | None) -> None:
    """Give back the pipe and the spooled log of a child nobody will read.

    The cancel paths terminate the child and drop it. Its reader thread, its
    stdout pipe and its stderr `TemporaryFile` outlive that drop otherwise:
    the Models tab only avoids it because its own cancel goes on to call
    `model_download_process_error`, which reaps all three as a side effect of
    reading the message. The preload's cancel reads no message and left them
    behind.

    Nothing here waits. Five of the six call sites of
    `DictationController._terminate_preload_download_process` run on the Qt
    thread -- shutdown, a settings save, both preload restarts, the finished-
    preload slot and the cancel hotkey -- and a join there is the frozen UI
    this project keeps closing. The reader is a daemon thread that owns its
    stream and closes it when the child's write end goes, so dropping the
    reference is the whole handover.
    """
    if process is None:
        return
    reader = getattr(process, "_stt_progress_reader", None)
    if reader is None:
        _close_download_stream(process)
    else:
        process._stt_progress_reader = None  # type: ignore[attr-defined]
    error_log = getattr(process, "_stt_error_log", None)
    if error_log is not None:
        try:
            error_log.close()
        except Exception:
            pass
        process._stt_error_log = None  # type: ignore[attr-defined]


def _close_progress_reader(process: subprocess.Popen[str]) -> None:
    """Let the reader see EOF and go, bounded; the survivor owns the pipe.

    A reader still inside its read when the join runs out keeps the stream,
    and the caller must not touch it: on Windows, closing a pipe another
    thread is reading blocks until that read returns. Measured against a real
    child whose stdout handle a grandchild had inherited -- after terminate
    and kill of the child, `join(2.0)` returned with the reader still in
    `readline()` and `process.stdout.close()` then blocked for 16.56 s; with
    no grandchild both calls returned in 0.00 s. That close runs on the
    download queue worker thread, which holds the in-process and the
    machine-wide download slot, so an unbounded wait there is the very hang
    the bounded `wait()` beside it exists to prevent. Nothing is lost by
    leaving it: `_pump_progress` closes the stream in its own `finally`.
    """
    reader = getattr(process, "_stt_progress_reader", None)
    if reader is not None:
        try:
            reader.join(timeout=_READER_JOIN_TIMEOUT_S)
        except Exception:
            pass
        process._stt_progress_reader = None  # type: ignore[attr-defined]
        if reader.is_alive():
            _logger.warning(
                "model_download_progress_reader_still_reading timeout_s=%s",
                _READER_JOIN_TIMEOUT_S,
            )
            return
    _close_download_stream(process)


def _close_download_stream(process: subprocess.Popen[str]) -> None:
    stream = getattr(process, "stdout", None)
    if stream is None:
        return
    try:
        stream.close()
    except Exception:
        pass


def _package_source_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _subprocess_no_window_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
