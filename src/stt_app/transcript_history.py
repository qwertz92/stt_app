from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .app_paths import transcript_history_path
from .persistence import (
    atomic_write_json,
    backup_path,
    load_json_with_backup,
    lock_for_path,
    quarantine_corrupt_file,
)

_LOGGER = logging.getLogger(__name__)

HistoryStorageSignature = tuple[int, int] | None
DISPLAY_TIMEZONE_LOCAL = "local"
DISPLAY_TIMEZONE_UTC = "utc"
VALID_HISTORY_DISPLAY_TIMEZONES = (DISPLAY_TIMEZONE_LOCAL, DISPLAY_TIMEZONE_UTC)

@dataclass(frozen=True, slots=True)
class HistoryEntryListChange:
    kind: str
    previous_start: int
    previous_stop: int
    current_start: int
    current_stop: int


@dataclass(slots=True)
class TranscriptHistoryEntry:
    created_at: str
    text: str
    engine: str
    model: str
    mode: str
    source_recording_id: str = ""
    source_audio_path: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TranscriptHistoryEntry:
        return cls(
            created_at=str(raw.get("created_at", "")),
            text=str(raw.get("text", "")),
            engine=str(raw.get("engine", "")),
            model=str(raw.get("model", "")),
            mode=str(raw.get("mode", "")),
            source_recording_id=str(raw.get("source_recording_id", "")).strip(),
            source_audio_path=str(raw.get("source_audio_path", "")).strip(),
        )

    @classmethod
    def new(
        cls,
        *,
        text: str,
        engine: str,
        model: str,
        mode: str,
        source_recording_id: str = "",
        source_audio_path: str = "",
    ) -> TranscriptHistoryEntry:
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        return cls(
            created_at=timestamp,
            text=str(text or ""),
            engine=str(engine or ""),
            model=str(model or ""),
            mode=str(mode or ""),
            source_recording_id=str(source_recording_id or "").strip(),
            source_audio_path=str(source_audio_path or "").strip(),
        )


_UNDATED_SORTS_OLDEST = datetime.min.replace(tzinfo=UTC)


def _chronological_sort_key(
    entry: TranscriptHistoryEntry,
) -> tuple[int, datetime]:
    """Sort by the recorded time, with anything undatable treated as oldest.

    The app always writes `datetime.now(UTC).isoformat(timespec="seconds")`, so
    only an imported or hand-edited file can carry something else. Such an entry
    sorting oldest means a trim removes it before it removes a real dictation,
    which is the safer of the two directions. Parsed rather than compared as a
    string so a file written with a different UTC offset still orders correctly.
    """
    raw = str(entry.created_at or "").strip()
    if not raw:
        return (0, _UNDATED_SORTS_OLDEST)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return (0, _UNDATED_SORTS_OLDEST)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (1, parsed)


class TranscriptHistoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or transcript_history_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = lock_for_path(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def storage_signature(self) -> HistoryStorageSignature:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return (time.monotonic_ns(), -1)
        return (int(stat.st_mtime_ns), int(stat.st_size))

    def load(self) -> list[TranscriptHistoryEntry]:
        with self._lock:
            return self._load_from_path(self._path)

    def count(self) -> int:
        return len(self.load())

    def save(self, entries: list[TranscriptHistoryEntry]) -> None:
        with self._lock:
            payload = [asdict(item) for item in entries]
            atomic_write_json(
                self._path,
                payload,
                ensure_ascii=True,
                keep_backup=True,
            )

    def add_entry(self, entry: TranscriptHistoryEntry, max_items: int) -> None:
        self.append_entries([entry], max_items=max_items)

    def append_entries(
        self,
        entries: list[TranscriptHistoryEntry],
        *,
        max_items: int,
    ) -> int:
        incoming = [item for item in entries if item.text.strip()]
        if not incoming:
            return 0
        with self._lock:
            current = self.load()
            merged = self._trim_entries(current + incoming, max_items=max_items)
            self.save(merged)
        return len(incoming)

    def apply_max_items(self, max_items: int) -> int:
        with self._lock:
            entries = self.load()
            trimmed = self._trim_entries(entries, max_items=max_items)
            removed = len(entries) - len(trimmed)
            if removed > 0:
                self.save(trimmed)
            return removed

    def clear(self) -> int:
        with self._lock:
            removed = self.count()
            if removed:
                self.save([])
            return removed

    def delete_entry(self, entry: TranscriptHistoryEntry) -> int:
        return self.delete_entries([entry])

    def delete_entries(self, entries: list[TranscriptHistoryEntry]) -> int:
        if not entries:
            return 0
        with self._lock:
            current = self.load()
            removed = 0
            for entry in entries:
                try:
                    index = current.index(entry)
                except ValueError:
                    continue
                current.pop(index)
                removed += 1
            if removed > 0:
                self.save(current)
            return removed

    def update_entry_text(self, entry: TranscriptHistoryEntry, text: str) -> int:
        next_text = str(text or "").strip()
        if not next_text:
            return 0
        return self.update_entry(entry, replace(entry, text=next_text))

    def update_entry(
        self,
        original: TranscriptHistoryEntry,
        updated: TranscriptHistoryEntry,
    ) -> int:
        if not updated.text.strip():
            return 0
        with self._lock:
            current = self.load()
            try:
                index = current.index(original)
            except ValueError:
                return 0
            current[index] = updated
            self.save(current)
            return 1

    def recent_entries(self, limit: int = 10) -> list[TranscriptHistoryEntry]:
        entries = self.load()
        return self._recent_entries_from(entries, limit)

    def recent_entries_with_count(
        self,
        limit: int = 10,
    ) -> tuple[list[TranscriptHistoryEntry], int]:
        entries = self.load()
        return self._recent_entries_from(entries, limit), len(entries)

    @staticmethod
    def _recent_entries_from(
        entries: list[TranscriptHistoryEntry],
        limit: int,
    ) -> list[TranscriptHistoryEntry]:
        keep = _normalize_limit(limit)
        selected = entries if keep == 0 else entries[-keep:]
        return list(reversed(selected))

    def export_to_file(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = self.load()
        payload = [asdict(item) for item in entries]
        atomic_write_json(path, payload, ensure_ascii=True, keep_backup=False)
        return len(entries)

    def import_from_file(self, path: Path) -> list[TranscriptHistoryEntry]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"Failed to read import file: {exc}") from exc
        except UnicodeDecodeError as exc:
            # Raised by `read_text` above, before `json.loads` is ever reached,
            # and it is not a `json.JSONDecodeError` -- the two are siblings
            # under `ValueError`, so clause order decides nothing here and the
            # arm below would never catch it. Without this arm it escapes
            # uncaught and the caller, which shows the message verbatim, reads
            # a codec offset instead of what to do.
            raise ValueError(
                "Selected file is not UTF-8 text. Re-export it, or save it as "
                "UTF-8, and try again."
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValueError("Selected file is not valid JSON.") from exc
        return self._entries_from_payload(payload)

    def _trim_entries(
        self,
        entries: list[TranscriptHistoryEntry],
        *,
        max_items: int,
    ) -> list[TranscriptHistoryEntry]:
        """Order chronologically, then keep the newest ``max_items``.

        Position used to stand in for time, and an import breaks that: the
        imported entries are appended at the end whatever their timestamps say,
        so the front of the list -- what this deletes -- is no longer the
        oldest. Measured: twelve August-2026 dictations, a 40-entry export from
        March 2024 imported back, then the limit lowered to 40. **All twelve
        dictations were deleted and all forty imports kept**, while both
        confirmation prompts said "will delete N oldest entries".
        `recent_entries` had the same defect from the same cause and reported
        the 2024 entries as the newest.

        The sort runs before the early returns on purpose: "import all and set
        unlimited" is exactly the flow that produces a mis-ordered store, and
        it lands here with ``max_items == 0``, so returning early would leave
        the file out of order for the *next* limit change to trim wrongly. It
        is stable, so entries sharing a timestamp -- the app stamps whole
        seconds -- keep their insertion order.
        """
        ordered = sorted(entries, key=_chronological_sort_key)
        keep = _normalize_limit(max_items)
        if keep == 0 or len(ordered) <= keep:
            return ordered
        return ordered[-keep:]

    @staticmethod
    def _entries_from_payload(payload: Any) -> list[TranscriptHistoryEntry]:
        if isinstance(payload, dict):
            payload = payload.get("entries", None)
        if not isinstance(payload, list):
            raise ValueError("Expected a JSON array of transcript entries.")

        entries: list[TranscriptHistoryEntry] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            entry = TranscriptHistoryEntry.from_dict(item)
            if entry.text.strip():
                entries.append(entry)
        return entries

    @classmethod
    def _payload_is_usable(cls, payload: Any) -> bool:
        """Does this list carry anything this store can read?

        Empty means a cleared history and is usable. Non-empty that yields no
        entry means the members are not entries at all, which no code path here
        can write -- so it is external damage and the backup should be tried.
        """
        if not payload:
            return True
        try:
            return bool(cls._entries_from_payload(payload))
        except ValueError:
            return False

    @classmethod
    def _load_from_path(cls, path: Path) -> list[TranscriptHistoryEntry]:
        # Both, not just the primary. An external deletion of
        # `transcript_history.json` -- a sync tool, an antivirus quarantine, a
        # user tidying `%APPDATA%` -- left the `.bak` holding every transcript
        # the user had, and this returned `[]` without ever looking at it. The
        # next dictation then saved that empty list over the backup too:
        # measured, five entries became one.
        if not path.exists() and not backup_path(path).exists():
            return []
        # `expected_type=list` alone was too weak: any JSON list satisfied it,
        # so a primary rewritten as `["a", 1, null]` or as dicts with no `text`
        # key counted as the good copy, the intact backup was never opened, and
        # the next dictation saved that emptiness over the backup too --
        # measured, five transcripts became one, with no quarantine and no log
        # line. An *empty* list is a cleared history and must still win.
        payload, source = load_json_with_backup(
            path, expected_type=list, is_usable=cls._payload_is_usable
        )
        if payload is None:
            quarantine_corrupt_file(path, include_backup=True)
            return []
        try:
            entries = cls._entries_from_payload(payload)
        except ValueError:
            quarantine_corrupt_file(path)
            return []
        if source == "backup":
            # Whatever is in the primary lost to the backup, so it is unusable
            # by definition; keep it under a `.corrupt.` name instead of having
            # the republish below overwrite it. A *missing* primary is the other
            # way to reach this branch and quarantining one is a no-op.
            quarantine_corrupt_file(path)
            # A republish is a convenience: the entries are already in hand,
            # recovered from the backup. Letting its write escape threw them
            # away along with it -- measured with the primary gone and the
            # directory unwritable (an antivirus quarantine plus a locked-down
            # profile), `load()` raised `PermissionError` and returned nothing,
            # and `SettingsDialog.__init__` calls these readers with no guard.
            # The data stays only in the `.bak` until a later write succeeds,
            # which is exactly the state this recovery already handles.
            try:
                cls(path=path).save(entries)
            except OSError:
                _LOGGER.exception(
                    "Could not republish %s from its backup", path
                )
        return entries


def _normalize_limit(value: int) -> int:
    try:
        keep = int(value)
    except (TypeError, ValueError):
        return 1
    if keep < 0:
        return 0
    return keep


def join_recent_entries_for_clipboard(
    entries_newest_first: Iterable[TranscriptHistoryEntry],
) -> str:
    """Join selected recent-history entries in chronological paste order."""
    texts: list[str] = []
    for entry in reversed(list(entries_newest_first)):
        text = str(getattr(entry, "text", "") or "")
        if text:
            texts.append(text)
    return "\n\n".join(texts)


def recent_entries_change_plan(
    previous_newest_first: Iterable[TranscriptHistoryEntry],
    current_newest_first: Iterable[TranscriptHistoryEntry],
) -> list[HistoryEntryListChange]:
    previous = list(previous_newest_first)
    current = list(current_newest_first)
    matcher = SequenceMatcher(
        None,
        [_history_entry_full_key(entry) for entry in previous],
        [_history_entry_full_key(entry) for entry in current],
        autojunk=False,
    )
    changes: list[HistoryEntryListChange] = []
    for tag, previous_start, previous_stop, current_start, current_stop in (
        matcher.get_opcodes()
    ):
        if tag == "equal":
            continue
        kind = str(tag)
        if (
            tag == "replace"
            and previous_stop - previous_start == current_stop - current_start
            and [
                _history_entry_identity_key(entry)
                for entry in previous[previous_start:previous_stop]
            ]
            == [
                _history_entry_identity_key(entry)
                for entry in current[current_start:current_stop]
            ]
        ):
            kind = "update"
        changes.append(
            HistoryEntryListChange(
                kind=kind,
                previous_start=previous_start,
                previous_stop=previous_stop,
                current_start=current_start,
                current_stop=current_stop,
            )
        )
    return changes


def map_recent_entry_rows(
    changes: Iterable[HistoryEntryListChange],
    previous_rows: Iterable[int],
) -> list[int]:
    ordered_changes = list(changes)
    mapped_rows: list[int] = []
    for row in previous_rows:
        current_row = _map_recent_entry_row(ordered_changes, row)
        if current_row is not None and current_row not in mapped_rows:
            mapped_rows.append(current_row)
    return mapped_rows


def _map_recent_entry_row(
    changes: list[HistoryEntryListChange],
    row: int,
) -> int | None:
    offset = 0
    for change in changes:
        if row < change.previous_start:
            break
        if row >= change.previous_stop:
            offset += (change.current_stop - change.current_start) - (
                change.previous_stop - change.previous_start
            )
            continue
        if change.kind == "update":
            return change.current_start + (row - change.previous_start)
        return None
    return row + offset


def _history_entry_identity_key(
    entry: TranscriptHistoryEntry,
) -> tuple[str, str, str, str, str]:
    return (
        entry.created_at,
        entry.engine,
        entry.model,
        entry.mode,
        entry.source_recording_id,
    )


def _history_entry_full_key(
    entry: TranscriptHistoryEntry,
) -> tuple[str, str, str, str, str, str]:
    return (
        entry.created_at,
        entry.text,
        entry.engine,
        entry.model,
        entry.mode,
        entry.source_recording_id,
    )


def format_history_timestamp(value: str, display_timezone: str = "local") -> str:
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return value

    mode = str(display_timezone or DISPLAY_TIMEZONE_LOCAL).strip().lower()
    if mode not in VALID_HISTORY_DISPLAY_TIMEZONES:
        mode = DISPLAY_TIMEZONE_LOCAL
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if mode == DISPLAY_TIMEZONE_UTC:
        return f"{dt.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
