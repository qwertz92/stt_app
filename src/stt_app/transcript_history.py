from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from dataclasses import replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .app_paths import transcript_history_path
from .persistence import atomic_write_json, load_json_with_backup, quarantine_corrupt_file

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

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TranscriptHistoryEntry":
        return cls(
            created_at=str(raw.get("created_at", "")),
            text=str(raw.get("text", "")),
            engine=str(raw.get("engine", "")),
            model=str(raw.get("model", "")),
            mode=str(raw.get("mode", "")),
            source_recording_id=str(raw.get("source_recording_id", "")).strip(),
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
    ) -> "TranscriptHistoryEntry":
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return cls(
            created_at=timestamp,
            text=str(text or ""),
            engine=str(engine or ""),
            model=str(model or ""),
            mode=str(mode or ""),
            source_recording_id=str(source_recording_id or "").strip(),
        )


class TranscriptHistoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or transcript_history_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)

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
        return self._load_from_path(self._path)

    def count(self) -> int:
        return len(self.load())

    def save(self, entries: list[TranscriptHistoryEntry]) -> None:
        payload = [asdict(item) for item in entries]
        atomic_write_json(self._path, payload, ensure_ascii=True, keep_backup=True)

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
        current = self.load()
        merged = self._trim_entries(current + incoming, max_items=max_items)
        self.save(merged)
        return len(incoming)

    def apply_max_items(self, max_items: int) -> int:
        entries = self.load()
        trimmed = self._trim_entries(entries, max_items=max_items)
        removed = len(entries) - len(trimmed)
        if removed > 0:
            self.save(trimmed)
        return removed

    def clear(self) -> int:
        removed = self.count()
        if removed:
            self.save([])
        return removed

    def delete_entry(self, entry: TranscriptHistoryEntry) -> int:
        return self.delete_entries([entry])

    def delete_entries(self, entries: list[TranscriptHistoryEntry]) -> int:
        if not entries:
            return 0
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
        except json.JSONDecodeError as exc:
            raise ValueError("Selected file is not valid JSON.") from exc
        return self._entries_from_payload(payload)

    def _trim_entries(
        self,
        entries: list[TranscriptHistoryEntry],
        *,
        max_items: int,
    ) -> list[TranscriptHistoryEntry]:
        keep = _normalize_limit(max_items)
        if keep == 0:
            return entries
        if len(entries) <= keep:
            return entries
        return entries[-keep:]

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
    def _load_from_path(cls, path: Path) -> list[TranscriptHistoryEntry]:
        if not path.exists():
            return []
        payload, source = load_json_with_backup(path, expected_type=list)
        if payload is None:
            quarantine_corrupt_file(path, include_backup=True)
            return []
        try:
            entries = cls._entries_from_payload(payload)
        except ValueError:
            quarantine_corrupt_file(path)
            return []
        if source == "backup":
            cls(path=path).save(entries)
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
        return f"{dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
