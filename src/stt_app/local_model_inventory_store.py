from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_paths import local_model_inventory_path
from .config import VALID_MODEL_SIZES
from .persistence import (
    atomic_write_json,
    load_json_with_backup,
    lock_for_path,
    quarantine_corrupt_file,
)

_CURRENT_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_model_dir(model_dir: str | None) -> str:
    return str(model_dir or "").strip()


def _normalize_cached_models(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    requested = {
        str(value).strip()
        for value in raw
        if str(value).strip()
    }
    return [model_name for model_name in VALID_MODEL_SIZES if model_name in requested]


@dataclass(slots=True)
class LocalModelInventoryEntry:
    cached_models: list[str] = field(default_factory=list)
    updated_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LocalModelInventoryEntry":
        return cls(
            cached_models=_normalize_cached_models(raw.get("cached_models", [])),
            updated_at=str(raw.get("updated_at", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cached_models": list(self.cached_models),
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class LocalModelInventoryState:
    schema_version: int = _CURRENT_SCHEMA_VERSION
    entries: dict[str, LocalModelInventoryEntry] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LocalModelInventoryState":
        entries: dict[str, LocalModelInventoryEntry] = {}
        entries_raw = raw.get("entries", {})
        if isinstance(entries_raw, dict):
            for model_dir, value in entries_raw.items():
                if not isinstance(value, dict):
                    continue
                normalized_dir = _normalize_model_dir(model_dir)
                entries[normalized_dir] = LocalModelInventoryEntry.from_dict(value)
        return cls(
            schema_version=_CURRENT_SCHEMA_VERSION,
            entries=entries,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _CURRENT_SCHEMA_VERSION,
            "entries": {
                model_dir: entry.to_dict()
                for model_dir, entry in self.entries.items()
            },
        }


class LocalModelInventoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or local_model_inventory_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = lock_for_path(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def load_cached_models(self, model_dir: str = "") -> list[str] | None:
        with self._lock:
            state = self._load_state()
            if state is None:
                return None
            key = _normalize_model_dir(model_dir)
            entry = state.entries.get(key)
            if entry is None:
                return None
            return list(entry.cached_models)

    def save_cached_models(self, model_dir: str, cached_models: list[str]) -> None:
        with self._lock:
            state = self._load_state() or LocalModelInventoryState()
            key = _normalize_model_dir(model_dir)
            state.entries[key] = LocalModelInventoryEntry(
                cached_models=_normalize_cached_models(cached_models),
                updated_at=_utc_now(),
            )
            self._save_state(state)

    def clear_cached_models(self, model_dir: str = "") -> None:
        with self._lock:
            state = self._load_state()
            if state is None:
                return
            key = _normalize_model_dir(model_dir)
            if state.entries.pop(key, None) is None:
                return
            self._save_state(state)

    def _load_state(self) -> LocalModelInventoryState | None:
        if not self._path.exists():
            return None

        payload, source = load_json_with_backup(self._path, expected_type=dict)
        if payload is None:
            quarantine_corrupt_file(self._path, include_backup=True)
            return None

        raw = dict(payload)
        state = LocalModelInventoryState.from_dict(raw)
        if source == "backup" or raw != state.to_dict():
            self._save_state(state)
        return state

    def _save_state(self, state: LocalModelInventoryState) -> None:
        atomic_write_json(
            self._path,
            state.to_dict(),
            ensure_ascii=True,
            keep_backup=True,
        )
