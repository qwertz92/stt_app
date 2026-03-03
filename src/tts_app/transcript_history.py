from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_paths import transcript_history_path


@dataclass(slots=True)
class TranscriptHistoryEntry:
    created_at: str
    text: str
    engine: str
    model: str
    mode: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TranscriptHistoryEntry":
        return cls(
            created_at=str(raw.get("created_at", "")),
            text=str(raw.get("text", "")),
            engine=str(raw.get("engine", "")),
            model=str(raw.get("model", "")),
            mode=str(raw.get("mode", "")),
        )

    @classmethod
    def new(
        cls,
        *,
        text: str,
        engine: str,
        model: str,
        mode: str,
    ) -> "TranscriptHistoryEntry":
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return cls(
            created_at=timestamp,
            text=str(text or ""),
            engine=str(engine or ""),
            model=str(model or ""),
            mode=str(mode or ""),
        )


class TranscriptHistoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or transcript_history_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[TranscriptHistoryEntry]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []

        entries: list[TranscriptHistoryEntry] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            entry = TranscriptHistoryEntry.from_dict(item)
            if entry.text.strip():
                entries.append(entry)
        return entries

    def save(self, entries: list[TranscriptHistoryEntry]) -> None:
        payload = [asdict(item) for item in entries]
        self._path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def add_entry(self, entry: TranscriptHistoryEntry, max_items: int) -> None:
        entries = self.load()
        entries.append(entry)
        keep = max(1, int(max_items or 1))
        if len(entries) > keep:
            entries = entries[-keep:]
        self.save(entries)

    def recent_entries(self, limit: int = 10) -> list[TranscriptHistoryEntry]:
        entries = self.load()
        keep = max(1, int(limit or 1))
        return list(reversed(entries[-keep:]))
