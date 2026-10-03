"""Every child process the app starts must not open a console window.

The installed app is a windowed executable (`console=False` in
`stt_app.spec`), so it owns no console. Windows gives a console-subsystem
child of such a process -- node.exe, powershell.exe, npm, a key command --
a new console window of its own unless `CREATE_NO_WINDOW` is passed. Run
from a terminal (`uv run main.py`), the same child shares that terminal's
console, which is why a missing flag never shows on a development machine:
the ONNX/WebGPU runner opened a window on every transcription only in the
installed build.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from stt_app import process_tree

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "stt_app"
_SPAWNING_FUNCTIONS = {"Popen", "run", "call", "check_call", "check_output"}


def _spawn_calls_without_window_flags() -> list[str]:
    missing: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            spawns = (
                isinstance(func, ast.Attribute)
                and func.attr in _SPAWNING_FUNCTIONS
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ) or (
                # `update_installer` takes `runner=subprocess.run` as a seam.
                isinstance(func, ast.Name) and func.id == "runner"
            )
            if not spawns:
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            # `None` is a `**kwargs` spread, which the call site fills itself.
            if "creationflags" in keywords or None in keywords:
                continue
            missing.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}")
    return missing


def test_every_child_process_is_started_without_a_console_window():
    assert _spawn_calls_without_window_flags() == []


def test_no_window_flags_are_the_windows_flag_or_nothing():
    expected = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert process_tree.no_window_flags() == expected
    if os.name == "nt":
        assert expected != 0


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW exists only on Windows")
def test_run_bounded_starts_its_child_without_a_console_window(monkeypatch):
    seen: list[int] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        seen.append(int(kwargs.get("creationflags", 0)))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(process_tree.subprocess, "Popen", recording_popen)
    completed = process_tree.run_bounded(
        ["cmd", "/c", "echo", "ok"], timeout=20, text=True
    )

    assert completed.returncode == 0
    assert seen and seen[0] & subprocess.CREATE_NO_WINDOW
