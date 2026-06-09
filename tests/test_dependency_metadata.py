from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _requirement_base(value: str) -> str:
    return value.split(";", 1)[0].strip()


def test_windows_requirements_match_direct_runtime_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_dependencies = {
        _requirement_base(value)
        for value in project["project"]["dependencies"]
    }
    windows_requirements = {
        line.strip()
        for line in (ROOT / "requirements-win.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-r "))
    }

    assert windows_requirements == project_dependencies
