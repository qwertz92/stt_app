from __future__ import annotations

import tomllib
from pathlib import Path

import stt_app


def test_package_version_matches_pyproject():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert stt_app.__version__ == pyproject["project"]["version"]
