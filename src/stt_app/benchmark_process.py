"""Launch and stream the out-of-process local benchmark.

``run_benchmark_cases`` here is a drop-in replacement for the pure function in
``local_benchmark`` with the *same* signature and return type, but it runs the
benchmark in a child process (see ``benchmark_worker``) and translates the
streamed JSON events back into the same ``progress_callback`` / ``case_callback``
calls. This keeps the Qt UI responsive during a benchmark (the heavy model
loading/inference never runs in the GUI process) while leaving the settings
dialog code and its test seam unchanged: the facade re-exports this
``run_benchmark_cases`` under the same name the tests patch.

Cancellation terminates the child process tree; cases completed before the
cancel are already streamed to the caller and preserved, and a
``BenchmarkCancelled`` is raised to match the pure function's contract.
"""
from __future__ import annotations

import collections
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .benchmark_worker import BENCHMARK_EVENT_PREFIX
from .local_benchmark import BenchmarkCancelled, BenchmarkCase, _case_from_dict

BENCHMARK_WORKER_ARG = "--local-benchmark-worker"

_EVENT_POLL_SECONDS = 0.15
_STDERR_TAIL_LINES = 50


class _Eof:
    """Sentinel pushed by the stdout reader once the child stream closes."""


_EOF = _Eof()


def run_benchmark_cases(
    *,
    audio_path: str | Path,
    model_names: list[str],
    device: str = "auto",
    compute_type: str = "int8",
    runs: int = 1,
    beam_size: int = 5,
    language: str | None = None,
    vad_filter: bool = False,
    warmup: bool = False,
    threads: int = 0,
    model_dir: str = "",
    webgpu_devices: str | list[str] | tuple[str, ...] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    case_callback: Callable[[BenchmarkCase], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[BenchmarkCase]:
    if isinstance(webgpu_devices, tuple):
        webgpu_devices = list(webgpu_devices)
    options: dict[str, Any] = {
        "audio_path": str(audio_path),
        "model_names": list(model_names),
        "device": device,
        "compute_type": compute_type,
        "runs": runs,
        "beam_size": beam_size,
        "language": language,
        "vad_filter": vad_filter,
        "warmup": warmup,
        "threads": threads,
        "model_dir": model_dir,
        "webgpu_devices": webgpu_devices,
    }
    with tempfile.TemporaryDirectory(prefix="stt-app-benchmark-") as temp_dir:
        options_path = Path(temp_dir) / "options.json"
        options_path.write_text(json.dumps(options), encoding="utf-8")
        return _stream_benchmark_process(
            options_path,
            progress_callback=progress_callback,
            case_callback=case_callback,
            cancel_check=cancel_check,
        )


def _stream_benchmark_process(
    options_path: Path,
    *,
    progress_callback: Callable[[str], None] | None,
    case_callback: Callable[[BenchmarkCase], None] | None,
    cancel_check: Callable[[], bool] | None,
) -> list[BenchmarkCase]:
    process = start_benchmark_process(options_path)
    events: "queue.Queue[Any]" = queue.Queue()
    stderr_tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)

    stdout_reader = threading.Thread(
        target=_pump_events,
        args=(process.stdout, events),
        name="stt_app_benchmark_stdout",
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=_pump_stderr,
        args=(process.stderr, stderr_tail),
        name="stt_app_benchmark_stderr",
        daemon=True,
    )
    stdout_reader.start()
    stderr_reader.start()

    cases: list[BenchmarkCase] = []
    error_message: str | None = None
    canceled = False

    stream_finished = False
    try:
        while True:
            if cancel_check is not None and cancel_check():
                canceled = True
                break
            try:
                item = events.get(timeout=_EVENT_POLL_SECONDS)
            except queue.Empty:
                continue
            if item is _EOF:
                stream_finished = True
                break
            event = item.get("event") if isinstance(item, dict) else None
            if event == "progress":
                if progress_callback is not None:
                    progress_callback(str(item.get("text", "")))
            elif event == "case":
                case = _case_from_dict(item.get("case") or {})
                cases.append(case)
                if case_callback is not None:
                    case_callback(case)
            elif event == "canceled":
                canceled = True
            elif event == "error":
                error_message = str(item.get("message", "")) or "Benchmark failed."
    finally:
        if stream_finished:
            try:
                process.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                _terminate_process_tree(process)
        else:
            _terminate_process_tree(process)
        stdout_reader.join(timeout=1.0)
        stderr_reader.join(timeout=1.0)

    if canceled:
        raise BenchmarkCancelled("Benchmark canceled.")
    if error_message:
        raise RuntimeError(error_message)
    return_code = process.poll()
    if return_code not in (0, None):
        tail = "\n".join(stderr_tail).strip()
        raise RuntimeError(
            tail
            or f"Benchmark worker exited with code {return_code} after "
            f"streaming {len(cases)} completed case(s)."
        )
    return cases


def _pump_events(stream, events: "queue.Queue[Any]") -> None:
    try:
        if stream is None:
            return
        for line in stream:
            text = line.rstrip("\n")
            if not text.startswith(BENCHMARK_EVENT_PREFIX):
                continue
            payload = text[len(BENCHMARK_EVENT_PREFIX):]
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            events.put(event)
    finally:
        events.put(_EOF)


def _pump_stderr(stream, tail: "collections.deque[str]") -> None:
    if stream is None:
        return
    for line in stream:
        stripped = line.strip()
        if stripped:
            tail.append(stripped)


def start_benchmark_process(options_path: Path) -> subprocess.Popen[str]:
    env = dict(os.environ)
    command = benchmark_command(options_path, env)
    cwd = None if getattr(sys, "frozen", False) else str(_repo_root())
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_subprocess_no_window_flags(),
        start_new_session=os.name != "nt",
    )


def benchmark_command(options_path: Path, env: dict[str, str]) -> list[str]:
    worker_args = ["--options", str(options_path)]
    if getattr(sys, "frozen", False):
        return [sys.executable, BENCHMARK_WORKER_ARG, *worker_args]

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
        "stt_app.benchmark_worker",
        *worker_args,
    ]


def _terminate_process_tree(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_subprocess_no_window_flags(),
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


def _package_source_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _subprocess_no_window_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
