from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

LOCAL_MODEL_DOWNLOAD_WORKER_ARG = "--local-model-download-worker"


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
    error_log = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=error_log,
            creationflags=_subprocess_no_window_flags(),
        )
    except Exception:
        error_log.close()
        raise
    process._stt_error_log = error_log  # type: ignore[attr-defined]
    return process


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
        process.wait(timeout=2.0)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def model_download_process_error(process: subprocess.Popen[str]) -> str:
    error_log = getattr(process, "_stt_error_log", None)
    try:
        _stdout, stderr = process.communicate()
    except Exception:
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


def _package_source_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _subprocess_no_window_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
