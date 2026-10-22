from __future__ import annotations

import importlib.util
import re
import subprocess
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


# -- the npm audit gate ------------------------------------------------------
#
# 2026-10-10: `create_release.py --version 0.12.1` pushed main and the tag, and
# the "Windows Release" workflow then failed at its `npm audit` step on a sharp
# advisory published since the last merge, leaving a tag with no release. The
# audit asks the live advisory database, so a green push-time run proves
# nothing at tag time; the script now runs the same command before it changes
# or pushes anything. The tests below never reach git: every subprocess call is
# recorded and answered by a fake.

_NPM = "C:/node/npm.CMD"
_REPO_ROOT = Path(__file__).resolve().parents[1]


class _ReleaseRun:
    """Drives `create_release` with every outside effect recorded, none run."""

    def __init__(self, module, monkeypatch, tmp_path, *, audit_exit_code=0, npm=_NPM):
        self.module = module
        self.root = tmp_path
        self.calls: list[list[str]] = []
        self._audit_exit_code = audit_exit_code

        def fake_run(command, **kwargs):
            self.calls.append([str(part) for part in command])
            code = self._audit_exit_code if command[1:2] == ["audit"] else 0
            # Like the real one: `check=True` turns a non-zero exit into a
            # CalledProcessError, which `main` prints as "Command failed with
            # exit code 1: [...]" instead of the audit message. The failing
            # audit test fails if the call ever stops passing `check=False`.
            if kwargs.get("check") and code != 0:
                raise subprocess.CalledProcessError(code, command)
            return subprocess.CompletedProcess(command, code)

        monkeypatch.setattr(module.subprocess, "run", fake_run)
        monkeypatch.setattr(
            module.shutil, "which", lambda name: npm if name == "npm" else None
        )
        monkeypatch.setattr(module, "_ensure_release_branch", self._record("branch"))
        monkeypatch.setattr(module, "_ensure_no_tracked_changes", self._record("clean"))
        monkeypatch.setattr(module, "bump_version", self._record("bump"))
        monkeypatch.setattr(module, "verify_release", self._record("verify"))
        monkeypatch.setattr(module, "_has_staged_changes", lambda **_kwargs: True)

    def _record(self, name):
        def record(*_args, **_kwargs):
            self.calls.append([f"<{name}>"])

        return record

    def release(self, *, skip_tests):
        version = self.module.ReleaseVersion.parse("0.12.4")
        self.module.create_release(
            version=version,
            existing_tags=["v0.12.3"],
            remote="origin",
            skip_tests=skip_tests,
            root=self.root,
        )

    def position_of(self, first_word):
        return next(i for i, call in enumerate(self.calls) if call[0] == first_word)

    def commands_that_change_something(self):
        return [
            call
            for call in self.calls
            if call[0] in {"<bump>", "uv"}
            or (call[0] == "git" and call[1] in {"add", "commit", "push", "tag"})
        ]


@pytest.mark.parametrize("skip_tests", [True, False])
def test_the_release_audits_the_javascript_dependencies_before_changing_anything(
    monkeypatch, tmp_path, skip_tests
):
    module = _load_create_release_module()
    run = _ReleaseRun(module, monkeypatch, tmp_path)

    run.release(skip_tests=skip_tests)

    assert [
        _NPM,
        "audit",
        "--omit=dev",
        "--package-lock-only",
        "--audit-level=moderate",
    ] in run.calls
    audit = run.position_of(_NPM)
    assert audit < run.position_of("<bump>")
    assert audit < run.position_of("uv")
    assert audit < run.position_of("git")
    # `--skip-tests` really did skip the suite; only the audit is unconditional.
    assert any("pytest" in call for call in run.calls) is not skip_tests


@pytest.mark.parametrize("skip_tests", [True, False])
def test_a_failed_audit_stops_the_release_before_any_change(
    monkeypatch, tmp_path, skip_tests
):
    module = _load_create_release_module()
    run = _ReleaseRun(module, monkeypatch, tmp_path, audit_exit_code=1)

    with pytest.raises(module.CreateReleaseError, match="npm audit failed") as raised:
        run.release(skip_tests=skip_tests)

    assert "Nothing was changed, committed or tagged" in str(raised.value)
    assert run.commands_that_change_something() == []


def test_a_missing_npm_stops_the_release_instead_of_skipping_the_audit(
    monkeypatch, tmp_path
):
    module = _load_create_release_module()
    run = _ReleaseRun(module, monkeypatch, tmp_path, npm=None)

    with pytest.raises(module.CreateReleaseError, match="npm was not found on PATH"):
        run.release(skip_tests=True)

    assert run.commands_that_change_something() == []
    assert [call for call in run.calls if "audit" in call] == []


@pytest.mark.parametrize("workflow", ["windows-release.yml", "quality.yml"])
def test_the_audit_is_the_command_both_workflows_run(workflow):
    """quality.yml says its audit "must match the release job's bar": three
    copies of one command drift unless something compares them. The regex
    reads single-line `run: npm audit ...` steps only; an audit moved into a
    multi-line `run: |` block finds nothing and fails the comparison loudly."""
    module = _load_create_release_module()
    text = (_REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")

    commands = re.findall(r"(?m)^\s*run:\s*(npm audit\b.*?)\s*$", text)

    assert commands == ["npm " + " ".join(module.NPM_AUDIT_ARGS)]
