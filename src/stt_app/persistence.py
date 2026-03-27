from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BACKUP_SUFFIX = ".bak"


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}{_BACKUP_SUFFIX}")


def quarantine_corrupt_file(path: Path) -> Path | None:
    if not path.exists():
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{path.name}.corrupt.{timestamp}"
    target = path.with_name(base_name)
    counter = 1
    while target.exists():
        target = path.with_name(f"{base_name}.{counter}")
        counter += 1

    try:
        path.replace(target)
    except OSError:
        return None
    return target


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
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
        atomic_write_text(backup_path(path), text)


def load_json_with_backup(
    path: Path,
    *,
    expected_type: type[Any],
) -> tuple[Any | None, str]:
    for candidate, source in (
        (path, "primary"),
        (backup_path(path), "backup"),
    ):
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, expected_type):
            return payload, source
    return None, "missing"
