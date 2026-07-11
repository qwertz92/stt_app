from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_create_release_module():
    root = Path(__file__).resolve().parents[1]
    scripts_dir = root / "scripts"
    script_path = scripts_dir / "create_release.py"
    sys.path.insert(0, str(scripts_dir))
    try:
        spec = importlib.util.spec_from_file_location("create_release", script_path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["create_release"] = module
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
    finally:
        sys.path.remove(str(scripts_dir))
    return module


def test_latest_release_version_uses_highest_numeric_tag():
    module = _load_create_release_module()

    latest = module.latest_release_version(["v0.2.9", "v0.3.0", "not-a-release"])

    assert latest.tag == "v0.3.0"


def test_select_release_version_defaults_to_next_patch_after_latest():
    module = _load_create_release_module()
    latest = module.ReleaseVersion.parse("v0.2.1")
    current = module.ReleaseVersion.parse("0.2.1")

    selected = module.select_release_version("", latest=latest, current=current)

    assert selected.tag == "v0.2.2"


def test_select_release_version_defaults_to_unreleased_current_version():
    module = _load_create_release_module()
    latest = module.ReleaseVersion.parse("v0.3.1")
    current = module.ReleaseVersion.parse("0.4.0")

    selected = module.select_release_version("", latest=latest, current=current)

    assert selected.tag == "v0.4.0"


def test_select_release_version_accepts_explicit_v_prefixed_value():
    module = _load_create_release_module()
    latest = module.ReleaseVersion.parse("v0.2.1")
    current = module.ReleaseVersion.parse("0.2.1")

    selected = module.select_release_version("v0.3.0", latest=latest, current=current)

    assert selected.text == "0.3.0"


def test_validate_new_release_version_rejects_existing_tag():
    module = _load_create_release_module()
    version = module.ReleaseVersion.parse("v0.2.1")
    latest = module.ReleaseVersion.parse("v0.2.1")

    with pytest.raises(module.CreateReleaseError, match="already exists"):
        module.validate_new_release_version(
            version,
            existing_tags=["v0.2.1"],
            latest=latest,
        )


def test_validate_new_release_version_rejects_lower_version():
    module = _load_create_release_module()
    version = module.ReleaseVersion.parse("v0.2.1")
    latest = module.ReleaseVersion.parse("v0.3.0")

    with pytest.raises(module.CreateReleaseError, match="must be higher"):
        module.validate_new_release_version(
            version,
            existing_tags=["v0.3.0"],
            latest=latest,
        )


def test_validate_new_release_version_rejects_current_version_downgrade():
    module = _load_create_release_module()
    version = module.ReleaseVersion.parse("v0.3.2")
    latest = module.ReleaseVersion.parse("v0.3.1")
    current = module.ReleaseVersion.parse("0.4.0")

    with pytest.raises(module.CreateReleaseError, match="lower than current"):
        module.validate_new_release_version(
            version,
            existing_tags=["v0.3.1"],
            latest=latest,
            current=current,
        )


def test_validate_new_release_version_accepts_next_patch():
    module = _load_create_release_module()
    version = module.ReleaseVersion.parse("v0.2.2")
    latest = module.ReleaseVersion.parse("v0.2.1")

    module.validate_new_release_version(
        version,
        existing_tags=["v0.2.1"],
        latest=latest,
    )


def test_release_rejects_untracked_worktree_files(monkeypatch, tmp_path):
    module = _load_create_release_module()
    captured_command = None

    def fake_git_stdout(command, *, root):
        nonlocal captured_command
        captured_command = command
        assert root == tmp_path
        return "?? untracked.py"

    monkeypatch.setattr(module, "_git_stdout", fake_git_stdout)

    with pytest.raises(module.CreateReleaseError, match="Working tree changes"):
        module._ensure_no_tracked_changes(root=tmp_path)

    assert captured_command == [
        "git",
        "status",
        "--porcelain",
        "--untracked-files=normal",
    ]
