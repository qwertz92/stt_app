from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stt_app.persistence import atomic_write_text  # noqa: E402

PROJECT_VERSION_RE = re.compile(
    r'(?ms)(^\[project\]\s*.*?^version\s*=\s*")[^"]+(")'
)
INIT_VERSION_RE = re.compile(r'(?m)^(__version__\s*=\s*")[^"]+(")')
INNO_VERSION_RE = re.compile(
    r'(?m)^(\s*#define\s+MyAppVersion\s+")[^"]+(")'
)
UV_LOCK_VERSION_RE = re.compile(
    r'(?ms)(\[\[package\]\]\s*name\s*=\s*"stt-app"\s*version\s*=\s*")[^"]+(")'
)
RELEASE_TAG_RE = re.compile(
    r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


class ReleaseVersionError(ValueError):
    pass


@dataclass(frozen=True)
class VersionFiles:
    pyproject: str
    package: str
    installer: str
    uv_lock: str | None


@dataclass(frozen=True, order=True)
class ReleaseVersion:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "ReleaseVersion":
        match = RELEASE_TAG_RE.fullmatch(str(value or "").strip())
        if not match:
            raise ReleaseVersionError(
                f"Expected a numeric release version like 0.2.1 or v0.2.1: {value!r}"
            )
        return cls(*(int(part) for part in match.groups()))

    @property
    def text(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def tag(self) -> str:
        return f"v{self.text}"


def read_version_files(root: Path = REPO_ROOT) -> VersionFiles:
    pyproject = _read_project_version(root / "pyproject.toml")
    package = _read_regex_version(root / "src/stt_app/__init__.py", INIT_VERSION_RE)
    installer = _read_regex_version(
        root / "installer/windows/stt_app.iss",
        INNO_VERSION_RE,
    )
    uv_lock_path = root / "uv.lock"
    uv_lock = None
    if uv_lock_path.exists():
        uv_lock = _read_regex_version(uv_lock_path, UV_LOCK_VERSION_RE)
    return VersionFiles(
        pyproject=pyproject,
        package=package,
        installer=installer,
        uv_lock=uv_lock,
    )


def bump_version(version: str, root: Path = REPO_ROOT) -> None:
    release_version = ReleaseVersion.parse(version)
    targets = [
        (root / "pyproject.toml", PROJECT_VERSION_RE),
        (root / "src/stt_app/__init__.py", INIT_VERSION_RE),
        (root / "installer/windows/stt_app.iss", INNO_VERSION_RE),
    ]
    uv_lock_path = root / "uv.lock"
    if uv_lock_path.exists():
        targets.append((uv_lock_path, UV_LOCK_VERSION_RE))

    # Validate and prepare every edit before replacing any file. A malformed
    # final metadata file must not leave the earlier files at a new version.
    updates = [
        (
            path,
            path.read_text(encoding="utf-8"),
            _replacement_text(path, pattern, release_version.text),
        )
        for path, pattern in targets
    ]
    written: list[tuple[Path, str]] = []
    try:
        for path, original, replacement in updates:
            atomic_write_text(path, replacement)
            written.append((path, original))
    except OSError as exc:
        rollback_errors: list[str] = []
        for path, original in reversed(written):
            try:
                atomic_write_text(path, original)
            except OSError as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        detail = (
            " Rollback also failed for: " + " | ".join(rollback_errors)
            if rollback_errors
            else ""
        )
        raise ReleaseVersionError(
            f"Unable to update all release metadata: {exc}.{detail}"
        ) from exc


def verify_release(
    tag: str,
    *,
    root: Path = REPO_ROOT,
    released_tags: Iterable[str] = (),
) -> None:
    release_version = ReleaseVersion.parse(tag)
    versions = read_version_files(root)
    expected_versions = {
        "pyproject.toml": versions.pyproject,
        "src/stt_app/__init__.py": versions.package,
        "installer/windows/stt_app.iss": versions.installer,
    }
    if versions.uv_lock is not None:
        expected_versions["uv.lock"] = versions.uv_lock

    mismatches = [
        f"{path} has {version!r}, expected {release_version.text!r}"
        for path, version in expected_versions.items()
        if version != release_version.text
    ]
    if mismatches:
        raise ReleaseVersionError(
            "Release tag does not match project metadata:\n"
            + "\n".join(f"- {item}" for item in mismatches)
        )

    newer_tags = []
    for released_tag in released_tags:
        try:
            released_version = ReleaseVersion.parse(released_tag)
        except ReleaseVersionError:
            continue
        if released_version > release_version:
            newer_tags.append(released_version.tag)

    if newer_tags:
        latest = max(ReleaseVersion.parse(tag_value) for tag_value in newer_tags)
        raise ReleaseVersionError(
            f"Release tag {release_version.tag} is older than existing release "
            f"tag {latest.tag}."
        )


def git_release_tags(root: Path = REPO_ROOT) -> list[str]:
    result = subprocess.run(
        ["git", "tag", "--list", "v[0-9]*"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _read_project_version(path: Path) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise ReleaseVersionError(f"Unable to determine project version from {path}.")
    return version.strip()


def _read_regex_version(path: Path, pattern: re.Pattern[str]) -> str:
    text = path.read_text(encoding="utf-8")
    match = pattern.search(text)
    if not match:
        raise ReleaseVersionError(f"Unable to determine version from {path}.")
    return match.group(0).split('"')[-2]


def _replacement_text(
    path: Path,
    pattern: re.Pattern[str],
    version: str,
) -> str:
    text = path.read_text(encoding="utf-8")
    new_text, replacements = pattern.subn(rf"\g<1>{version}\2", text, count=1)
    if replacements != 1:
        raise ReleaseVersionError(f"Unable to update version in {path}.")
    return new_text


def _cmd_bump(args: argparse.Namespace) -> int:
    bump_version(args.version)
    print(f"Updated release metadata to {ReleaseVersion.parse(args.version).text}.")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    released_tags = args.released_tag or []
    if args.against_git_tags:
        released_tags = [*released_tags, *git_release_tags()]
    verify_release(args.tag, released_tags=released_tags)
    print(f"Release metadata verified for {ReleaseVersion.parse(args.tag).tag}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage stt_app release versions.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bump = subparsers.add_parser(
        "bump",
        help="Update release metadata files to a numeric version.",
    )
    bump.add_argument("version", help="Version like 0.2.2 or v0.2.2.")
    bump.set_defaults(func=_cmd_bump)

    verify = subparsers.add_parser(
        "verify",
        help="Verify release metadata and optional existing release tags.",
    )
    verify.add_argument("tag", help="Release tag like v0.2.2.")
    verify.add_argument(
        "--against-git-tags",
        action="store_true",
        help="Compare the release tag with local Git tags.",
    )
    verify.add_argument(
        "--released-tag",
        action="append",
        default=[],
        help="Existing release tag to compare against; may be passed repeatedly.",
    )
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ReleaseVersionError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    sys.exit(main())
