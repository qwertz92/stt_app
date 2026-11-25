"""Tells a source checkout when its Python packages are behind `requirements-win.txt`.

The app runs from a source checkout on some PCs. After a `git pull` that bumps
a pin (2026-11-24: `assemblyai` 1.6.1 -> 1.10.11) the installed packages stay
as they were until someone runs `uv sync` or `pip install -r
requirements-win.txt`, and the first symptom was a raw pydantic validation
error in the middle of a dictation. This compares the installed versions with
the pins at start and says what to run. It never installs anything.

Like `transcriber/_node_packages.py` it only concerns a source checkout (the
frozen build bundles exactly what it was built with) and it is cheap: one
`importlib.metadata` lookup per pin, a few milliseconds, run off the Qt thread.
`requirements-win.txt` is the file `tests/test_dependency_metadata.py` keeps
equal to `pyproject.toml`.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

logger = logging.getLogger(__name__)

REQUIREMENTS_FILE = "requirements-win.txt"
_PIN = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*"
    r"(?P<version>[^\s;#]+)"
)


@dataclass(frozen=True)
class Outdated:
    name: str
    installed: str | None
    pinned: str

    def describe(self) -> str:
        installed = self.installed or "not installed"
        return f"{self.name} (installed {installed}, pinned {self.pinned})"


def checkout_root() -> Path | None:
    """The source checkout this code runs from, or None.

    Never for a frozen build, and only when the folder two levels above the
    package holds `requirements-win.txt` and `pyproject.toml` (a wheel in
    `site-packages` has neither next to it).
    """
    if getattr(sys, "frozen", False):
        return None
    root = Path(__file__).resolve().parents[2]
    if (root / REQUIREMENTS_FILE).is_file() and (root / "pyproject.toml").is_file():
        return root
    return None


def read_pins(path: Path) -> dict[str, str]:
    """`name==version` lines of a requirements file, extras and markers dropped."""
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            pins[match["name"]] = match["version"]
    return pins


def _same_version(installed: str, pinned: str) -> bool:
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover - packaging ships with pip and uv
        return installed == pinned
    try:
        return Version(installed) == Version(pinned)
    except InvalidVersion:
        return installed == pinned


def version_at_least(installed: str, minimum: str) -> bool:
    """`installed >= minimum`; an unparsable version counts as too old."""
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover - packaging ships with pip and uv
        return False
    try:
        return Version(installed) >= Version(minimum)
    except InvalidVersion:
        return False


def installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def find_outdated(
    pins: dict[str, str],
    version_of: Callable[[str], str | None] = installed_version,
) -> list[Outdated]:
    """The pins whose installed version differs, in the file's order."""
    outdated: list[Outdated] = []
    for name, pinned in pins.items():
        installed = version_of(name)
        if installed is None or not _same_version(installed, pinned):
            outdated.append(Outdated(name, installed, pinned))
    return outdated


def update_command(root: Path, prefix: str | None = None) -> str:
    """The command that fits this checkout.

    `uv sync` when its `.venv` is the environment running the app and `uv.lock`
    exists; otherwise a plain `pip install -r requirements-win.txt`.
    """
    environment = Path(prefix or sys.prefix)
    try:
        uv_managed = (root / "uv.lock").is_file() and (
            environment.resolve() == (root / ".venv").resolve()
        )
    except OSError:
        uv_managed = False
    return "uv sync" if uv_managed else f"pip install -r {REQUIREMENTS_FILE}"


def outdated_packages_message(outdated: list[Outdated], command: str) -> str:
    """Plain words for the tray; the log line has the versions."""
    names = [item.name for item in outdated]
    shown = ", ".join(names[:5]) + (
        f" and {len(names) - 5} more" if len(names) > 5 else ""
    )
    return (
        f"Python packages are out of date after an update: {shown}. "
        f"Run in the app folder: {command} - then restart the app."
    )


def check_checkout_packages() -> tuple[list[Outdated], str] | None:
    """Outdated packages of this checkout and the command for them, or None.

    None when this is not a source checkout, the pins cannot be read or
    nothing differs. Logs one WARNING line listing every difference.
    """
    root = checkout_root()
    if root is None:
        return None
    try:
        pins = read_pins(root / REQUIREMENTS_FILE)
    except OSError:
        logger.warning("dependency_check_unreadable root=%s", root, exc_info=True)
        return None
    outdated = find_outdated(pins)
    if not outdated:
        return None
    command = update_command(root)
    logger.warning(
        "python_packages_out_of_date command=%r packages=[%s]",
        command,
        "; ".join(item.describe() for item in outdated),
    )
    return outdated, command
