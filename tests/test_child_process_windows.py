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


def _subprocess_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names bound to the module (`import subprocess as sp`) and to its
    spawning functions (`from subprocess import Popen`)."""
    modules: set[str] = set()
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SPAWNING_FUNCTIONS:
                    functions.add(alias.asname or alias.name)
    return modules, functions


def _spawn_calls_without_window_flags() -> list[str]:
    missing: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules, functions = _subprocess_aliases(tree)
        # `update_installer` takes `runner=subprocess.run` as a seam.
        functions.add("runner")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            spawns = (
                isinstance(func, ast.Attribute)
                and func.attr in _SPAWNING_FUNCTIONS
                and isinstance(func.value, ast.Name)
                and func.value.id in modules
            ) or (isinstance(func, ast.Name) and func.id in functions)
            if not spawns:
                continue
            flags = [kw.value for kw in node.keywords if kw.arg == "creationflags"]
            # A `**kwargs` spread (`run_bounded`, `benchmark_environment`) is
            # filled by its call site; `run_bounded` has its own test below.
            spread = any(kw.arg is None for kw in node.keywords)
            literal_zero = any(
                isinstance(value, ast.Constant) and not value.value for value in flags
            )
            if (flags and not literal_zero) or (spread and not flags):
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
