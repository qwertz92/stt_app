from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from release_version import (
    REPO_ROOT,
    ReleaseVersion,
    ReleaseVersionError,
    bump_version,
    git_release_tags,
    read_version_files,
    verify_release,
)


RELEASE_METADATA_PATHS = [
    "pyproject.toml",
    "uv.lock",
    "src/stt_app/__init__.py",
    "installer/windows/stt_app.iss",
]


class CreateReleaseError(RuntimeError):
    pass


def latest_release_version(tags: Sequence[str]) -> ReleaseVersion | None:
    versions = []
    for tag in tags:
        try:
            versions.append(ReleaseVersion.parse(tag))
        except ReleaseVersionError:
            continue
    if not versions:
        return None
    return max(versions)


def next_patch_version(version: ReleaseVersion) -> ReleaseVersion:
    return ReleaseVersion(version.major, version.minor, version.patch + 1)


def select_release_version(
    raw_value: str,
    *,
    latest: ReleaseVersion | None,
    current: ReleaseVersion,
) -> ReleaseVersion:
    default_version = next_patch_version(latest or current)
    value = raw_value.strip()
    if not value:
        return default_version
    return ReleaseVersion.parse(value)


def validate_new_release_version(
    version: ReleaseVersion,
    *,
    existing_tags: Sequence[str],
    latest: ReleaseVersion | None,
) -> None:
    if version.tag in existing_tags:
        raise CreateReleaseError(f"Release tag {version.tag} already exists.")
    if latest is not None and version <= latest:
        raise CreateReleaseError(
            f"Release version {version.text} must be higher than latest tag "
            f"{latest.tag}."
        )


def create_release(
    *,
    version: ReleaseVersion,
    existing_tags: Sequence[str],
    remote: str,
    skip_tests: bool,
    root: Path = REPO_ROOT,
) -> None:
    _ensure_release_branch(root=root, remote=remote)
    _ensure_no_tracked_changes(root=root)

    bump_version(version.text, root=root)
    _run(["uv", "lock"], root=root)
    verify_release(version.tag, root=root, released_tags=existing_tags)

    if not skip_tests:
        _run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "AGENTS.md",
                "docs",
                "src",
                "scripts",
                "tests",
            ],
            root=root,
        )
        _run([sys.executable, "-m", "pytest", "-q"], root=root)

    _run(["git", "add", *RELEASE_METADATA_PATHS], root=root)
    _ensure_staged_release_changes(version=version, root=root)
    _commit_release(version=version, root=root)
    _run(["git", "push", remote, "main"], root=root)
    _run(["git", "tag", "-a", version.tag, "-m", f"Release {version.tag}"], root=root)
    _run(["git", "push", remote, version.tag], root=root)


def _ensure_release_branch(*, root: Path, remote: str) -> None:
    branch = _git_stdout(["git", "branch", "--show-current"], root=root)
    if branch != "main":
        raise CreateReleaseError(
            f"Release script must run on main, but current branch is {branch!r}."
        )

    local_head = _git_stdout(["git", "rev-parse", "HEAD"], root=root)
    remote_head = _git_stdout(["git", "rev-parse", f"{remote}/main"], root=root)
    if local_head != remote_head:
        raise CreateReleaseError(
            f"Local main must match {remote}/main before creating a release."
        )


def _ensure_no_tracked_changes(*, root: Path) -> None:
    status = _git_stdout(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        root=root,
    )
    if status:
        raise CreateReleaseError(
            "Tracked working tree changes are present. Commit, stash, or revert "
            "them before creating a release."
        )


def _ensure_staged_release_changes(*, version: ReleaseVersion, root: Path) -> None:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=root,
        check=False,
    )
    if result.returncode == 0:
        raise CreateReleaseError(
            f"No release metadata changes were staged for {version.tag}."
        )


def _commit_release(*, version: ReleaseVersion, root: Path) -> None:
    message = (
        f"chore(release): bump version to {version.text}\n\n"
        f"- Align package, lock, and installer metadata for {version.tag}.\n"
    )
    _run(["git", "commit", "-F", "-"], root=root, input_text=message)


def _git_stdout(command: Sequence[str], *, root: Path) -> str:
    result = subprocess.run(
        command,
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _run(
    command: Sequence[str],
    *,
    root: Path,
    input_text: str | None = None,
) -> None:
    print(f"==> {' '.join(command)}", flush=True)
    subprocess.run(
        command,
        cwd=root,
        check=True,
        input=input_text,
        text=input_text is not None,
    )


def _prompt_for_version(
    *,
    latest: ReleaseVersion | None,
    current: ReleaseVersion,
) -> ReleaseVersion:
    default_version = next_patch_version(latest or current)
    latest_label = latest.tag if latest is not None else "none"
    print(f"Latest release tag: {latest_label}")
    print(f"Current project version: {current.text}")
    raw_value = input(
        f"New release version [{default_version.text}]: "
    )
    return select_release_version(raw_value, latest=latest, current=current)


def _confirm_release(version: ReleaseVersion) -> None:
    answer = input(
        f"Create, commit, push, tag, and publish {version.tag}? Type 'yes' to continue: "
    )
    if answer.strip().lower() != "yes":
        raise CreateReleaseError("Release canceled.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a tagged stt_app release from main.",
    )
    parser.add_argument(
        "--version",
        help="Release version like 0.2.2 or v0.2.2. Omit for interactive prompt.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt. Intended for deliberate automation only.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip ruff and pytest before committing the release bump.",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote to fetch and push. Defaults to origin.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _run(["git", "fetch", "--tags", args.remote], root=REPO_ROOT)
        tags = git_release_tags()
        latest = latest_release_version(tags)
        current = ReleaseVersion.parse(read_version_files().pyproject)
        if args.version:
            version = select_release_version(args.version, latest=latest, current=current)
            latest_label = latest.tag if latest is not None else "none"
            print(f"Latest release tag: {latest_label}")
            print(f"Selected release version: {version.text}")
        else:
            version = _prompt_for_version(latest=latest, current=current)
        validate_new_release_version(version, existing_tags=tags, latest=latest)
        if not args.yes:
            _confirm_release(version)
        create_release(
            version=version,
            existing_tags=tags,
            remote=args.remote,
            skip_tests=args.skip_tests,
        )
    except (CreateReleaseError, ReleaseVersionError) as exc:
        parser.exit(1, f"{exc}\n")
    except subprocess.CalledProcessError as exc:
        parser.exit(1, f"Command failed with exit code {exc.returncode}: {exc.cmd}\n")
    print("")
    print(f"Release {version.tag} was pushed. GitHub Actions will publish assets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
