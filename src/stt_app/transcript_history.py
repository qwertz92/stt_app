from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_paths import transcript_history_path
from .persistence import atomic_write_json, load_json_with_backup, quarantine_corrupt_file


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

    def recent_entries(self, limit: int = 10) -> list[TranscriptHistoryEntry]:
        entries = self.load()
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
            quarantine_corrupt_file(path)
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
