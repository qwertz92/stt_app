from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_release_version_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "release_version.py"
    spec = importlib.util.spec_from_file_location("release_version", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_version"] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _write_version_project(root: Path, version: str) -> None:
    (root / "src/stt_app").mkdir(parents=True)
    (root / "installer/windows").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "stt-app"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "src/stt_app/__init__.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (root / "installer/windows/stt_app.iss").write_text(
        f'#define MyAppName "Voice Dictation App"\n#define MyAppVersion "{version}"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        f'[[package]]\nname = "stt-app"\nversion = "{version}"\n',
        encoding="utf-8",
    )


def test_bump_version_updates_release_metadata(tmp_path):
    module = _load_release_version_module()
    _write_version_project(tmp_path, "0.2.1")

    module.bump_version("v0.2.2", root=tmp_path)
    versions = module.read_version_files(tmp_path)

    assert versions.pyproject == "0.2.2"
    assert versions.package == "0.2.2"
    assert versions.installer == "0.2.2"
    assert versions.uv_lock == "0.2.2"


def test_verify_release_allows_matching_newer_tag(tmp_path):
    module = _load_release_version_module()
    _write_version_project(tmp_path, "0.2.2")

    module.verify_release(
        "v0.2.2",
        root=tmp_path,
        released_tags=["v0.2.0", "v0.2.1"],
    )


def test_verify_release_rejects_metadata_mismatch(tmp_path):
    module = _load_release_version_module()
    _write_version_project(tmp_path, "0.2.1")

    with pytest.raises(module.ReleaseVersionError, match="pyproject.toml"):
        module.verify_release("v0.2.2", root=tmp_path)


def test_verify_release_rejects_older_tag_than_existing_release(tmp_path):
    module = _load_release_version_module()
    _write_version_project(tmp_path, "0.2.1")

    with pytest.raises(module.ReleaseVersionError, match="older than existing"):
        module.verify_release(
            "v0.2.1",
            root=tmp_path,
            released_tags=["v0.2.0", "v0.3.0"],
        )


def test_verify_release_allows_rerunning_current_tag(tmp_path):
    module = _load_release_version_module()
    _write_version_project(tmp_path, "0.2.1")

    module.verify_release(
        "v0.2.1",
        root=tmp_path,
        released_tags=["v0.2.0", "v0.2.1"],
    )
