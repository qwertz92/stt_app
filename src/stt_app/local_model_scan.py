from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

LOCAL_MODEL_SCAN_WORKER_ARG = "--local-model-scan-worker"
LOCAL_MODEL_SCAN_TIMEOUT_SECONDS = 30


def scan_cached_models_out_of_process(model_dir: str) -> list[str] | None:
    with tempfile.TemporaryDirectory(prefix="stt-app-local-model-scan-") as temp_dir:
        output_path = Path(temp_dir) / "result.json"
        result = run_scan_cached_models_process(model_dir, output_path)
        if result is None or result.returncode != 0:
            return None
        return load_scan_cached_models_payload(output_path)


def run_scan_cached_models_process(
    model_dir: str,
    output_path: Path,
) -> subprocess.CompletedProcess[str] | None:
    env = dict(os.environ)
    command = scan_cached_models_command(model_dir, output_path, env)
    cwd = None if getattr(sys, "frozen", False) else str(_repo_root())
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=LOCAL_MODEL_SCAN_TIMEOUT_SECONDS,
            check=False,
            creationflags=_subprocess_no_window_flags(),
        )
    except Exception:
        return None


def scan_cached_models_command(
    model_dir: str,
    output_path: Path,
    env: dict[str, str],
) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            LOCAL_MODEL_SCAN_WORKER_ARG,
            "--model-dir",
            str(model_dir or ""),
            "--output",
            str(output_path),
        ]

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
        "stt_app.local_model_scan_worker",
        "--model-dir",
        str(model_dir or ""),
        "--output",
        str(output_path),
    ]


def load_scan_cached_models_payload(output_path: Path) -> list[str] | None:
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    values = payload.get("cached_models", [])
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str)]


def _package_source_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _subprocess_no_window_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
