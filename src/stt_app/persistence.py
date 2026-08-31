from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_BACKUP_SUFFIX = ".bak"
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


def lock_for_path(path: Path) -> threading.RLock:
    """Return one in-process reentrant lock for a normalized file path."""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    key = Path(os.path.normcase(str(resolved)))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def parse_json_bool(value: Any, *, default: bool = False) -> bool:
    """Parse persisted booleans without Python's truthy-string behavior."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return bool(default)


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}{_BACKUP_SUFFIX}")


def quarantine_corrupt_file(
    path: Path,
    *,
    include_backup: bool = False,
) -> Path | None:
    """Move an unusable persisted file out of the way.

    With ``include_backup`` the ``.bak`` sibling is quarantined too. Use it
    only when the backup is known to be unusable as well (e.g. after
    ``load_json_with_backup`` returned no payload); otherwise the backup must
    stay available for recovery on the next load.
    """
    if include_backup:
        _quarantine_single_file(backup_path(path))
    return _quarantine_single_file(path)


def _quarantine_single_file(path: Path) -> Path | None:
    if not path.exists():
        return None

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{path.name}.corrupt.{timestamp}"
    target = path.with_name(base_name)
    counter = 1
    while target.exists():
        if counter > 10000:
            # Pathological number of same-timestamp corrupt files; give up
            # rather than spinning forever.
            return None
        target = path.with_name(f"{base_name}.{counter}")
        counter += 1

    try:
        path.replace(target)
    except OSError:
        return None
    return target


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    ensure_ascii: bool = True,
    keep_backup: bool = False,
) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=ensure_ascii)
    atomic_write_text(path, text)
    if keep_backup:
        # The backup is redundancy only; a failed backup write must not turn
        # an already successful primary write into an error.
        try:
            atomic_write_text(backup_path(path), text)
        except OSError:
            pass


def load_json_with_backup(
    path: Path,
    *,
    expected_type: type[Any],
    is_usable: Callable[[Any], bool] | None = None,
) -> tuple[Any | None, str]:
    """The primary if it is readable and usable, else the backup.

    ``is_usable`` exists because ``expected_type`` alone is too weak a test for
    "the primary survived". A store's payload can parse, be the right container,
    and still carry nothing the store can read -- a transcript history rewritten
    as ``["a", 1, null]``, or a list of dicts with no ``text`` key. That is not a
    shape the app can write, so it means external damage, which is exactly the
    condition the backup exists for; without this the backup was never even
    opened, and the next write put the emptiness over it as well. Measured on
    the transcript history: five entries, primary rewritten as a list of
    scalars, ``load()`` returned 0 and one further dictation left the backup
    holding 1.

    Callers must accept a *legitimately* empty store here -- an empty list is a
    cleared history, not damage -- so the predicate tests usability, not
    emptiness.
    """
    for candidate, source in (
        (path, "primary"),
        (backup_path(path), "backup"),
    ):
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # `ValueError` covers both members on purpose. `json.JSONDecodeError`
            # alone let `UnicodeDecodeError` -- also a `ValueError` -- escape,
            # and `read_text` raises that for any file not written as UTF-8: a
            # `settings.json` re-saved by hand in the Windows ANSI code page
            # then propagated out of `SettingsStore.load`, which `main` calls
            # unprotected, so the app could not start at all -- with a perfectly
            # good backup sitting next to it.
            continue
        if not isinstance(payload, expected_type):
            continue
        if is_usable is not None and not is_usable(payload):
            continue
        return payload, source
    return None, "missing"
