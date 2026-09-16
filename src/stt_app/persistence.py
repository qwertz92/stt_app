from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

_BACKUP_SUFFIX = ".bak"
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}

#: ``load_json_with_backup``'s source for "a file is there and the read of it
#: raised ``OSError``, and no candidate was usable". Distinct from ``missing``
#: (no candidate exists) and from a candidate that opened and did not parse:
#: those two are "nothing saved yet" and "damage", and the stores answer them
#: by starting empty and by quarantining. Neither answer is right for a file
#: whose content is unknown because nobody could open it.
SOURCE_UNREADABLE = "unreadable"

#: The backup was the copy that parsed, and the primary is there and could not
#: be opened. Separate from plain ``"backup"`` because the two call for
#: opposite treatment of the primary: a primary that opened and turned out
#: unusable is damage, and is quarantined and republished; a primary nobody
#: could open holds unknown content, and moving it aside or writing over it
#: destroys whichever of the two copies is the newer one.
SOURCE_BACKUP_PRIMARY_UNREADABLE = "backup_primary_unreadable"


class StoreUnavailableError(OSError):
    """A persisted store could not be read, so it must not be rewritten.

    Every store here is read-modify-write: it loads the whole file, changes
    one entry and writes the whole file back. When the load could not open
    the file, the store holds its empty default, and writing that back
    replaces the user's transcripts, settings or benchmark runs with the
    default plus whatever was being added. Raising instead leaves the file
    exactly as it is; the caller reports a failed save and the next attempt,
    once the scanner or sync client has let go, works normally.

    An ``OSError`` subclass so that every caller already guarding these calls
    against a failing disk catches it unchanged.
    """


def store_unavailable_error(path: Path) -> StoreUnavailableError:
    """The refusal, worded for the dialogs that show ``str(exc)`` verbatim."""
    return StoreUnavailableError(
        f"{path.name} could not be read, so it was left unchanged. "
        "Another program (an antivirus scan, a backup or a sync client) may "
        "be holding the file; try again in a moment."
    )


def note_unreadable_store(path: Path) -> None:
    """Log that a store's file exists but could not be opened.

    One line per load call rather than per candidate: the primary and its
    backup are one store, and a lock that blocks one usually blocks both.
    """
    _LOGGER.warning("store_unreadable path=%s", path.name)


def refuse_to_overwrite_unreadable(path: Path) -> None:
    """Raise when ``path`` exists and cannot be opened for reading.

    For a writer that does not read the file first. ``SettingsStore.save`` is
    the one: the settings dialog merges its edits onto a fresh ``load()``, and
    a load that could not open the file answers defaults -- so the save would
    put defaults plus those edits over an intact ``settings.json``. The
    overlay's opacity slider and pin button write through stores that never
    loaded at all, which is why the guard sits in ``save`` rather than in the
    load.

    A file that is not there is not a file that will be lost, so
    ``FileNotFoundError`` passes: that is the first run, and writing the
    defaults is what it is for.
    """
    try:
        with path.open("rb"):
            pass
    except FileNotFoundError:
        return
    except OSError as exc:
        note_unreadable_store(path)
        raise store_unavailable_error(path) from exc


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

    With no payload to return, the source separates two states the caller must
    treat differently. ``"missing"`` means every candidate is either absent or
    opened and turned out unusable: nothing saved yet, or damage, and the
    stores answer those by starting empty and by quarantining the file so the
    next load does not fail on the same bytes. ``SOURCE_UNREADABLE`` means a
    candidate is *there* and its read raised ``OSError`` -- so what it holds is
    unknown, quarantining it would move the user's data out from under the name
    every later load looks at, and writing the empty default over it would
    finish the job. Measured on the transcript history: five entries, both
    files held open, one appended dictation, and the five survived only as
    ``.corrupt.*`` copies.

    A payload carries one further distinction.
    ``SOURCE_BACKUP_PRIMARY_UNREADABLE`` means the backup is the copy that
    parsed *and* the primary is there with a read that raised ``OSError``. The
    backup's content is good to answer with; the primary must be left exactly
    as it is. Stores answer plain ``"backup"`` by quarantining the primary and
    republishing it from the backup, which is right for a primary that opened
    and turned out unusable and destructive for one nobody could open -- the
    two copies were never compared, so the newer one may be the copy being
    moved aside.
    """
    unreadable = False
    primary_unreadable = False
    for candidate, source in (
        (path, "primary"),
        (backup_path(path), "backup"),
    ):
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            # The file exists and something else is holding it: an antivirus
            # scan, a backup tool, a sync client, a locked-down profile. This
            # says nothing about the content, which is why it is reported
            # apart from the two arms below.
            unreadable = True
            if candidate is path:
                primary_unreadable = True
            continue
        except ValueError:
            # `UnicodeDecodeError`, which `read_text` raises for any file not
            # written as UTF-8 and which is a sibling of
            # `json.JSONDecodeError` under `ValueError`, not an `OSError`.
            # Naming `json.JSONDecodeError` alone let it escape: a
            # `settings.json` re-saved by hand in the Windows ANSI code page
            # propagated out of `SettingsStore.load`, which `main` calls
            # unprotected, so the app could not start at all -- with a
            # perfectly good backup sitting next to it.
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if not isinstance(payload, expected_type):
            continue
        if is_usable is not None and not is_usable(payload):
            continue
        if primary_unreadable:
            # Reachable only on the backup pass: the primary raised, so it
            # never got here.
            return payload, SOURCE_BACKUP_PRIMARY_UNREADABLE
        return payload, source
    return None, (SOURCE_UNREADABLE if unreadable else "missing")
