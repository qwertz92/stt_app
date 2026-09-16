from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .app_paths import local_model_inventory_path
from .config import VALID_MODEL_SIZES
from .persistence import (
    SOURCE_BACKUP_PRIMARY_UNREADABLE,
    SOURCE_UNREADABLE,
    atomic_write_json,
    backup_path,
    load_json_with_backup,
    lock_for_path,
    note_unreadable_store,
    quarantine_corrupt_file,
    store_unavailable_error,
)

_LOGGER = logging.getLogger(__name__)

_CURRENT_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
    def from_dict(cls, raw: dict[str, Any]) -> LocalModelInventoryEntry:
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
    def from_dict(cls, raw: dict[str, Any]) -> LocalModelInventoryState:
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
        # Whether the last `_load_state()` reached the file; see
        # `TranscriptHistoryStore.__init__` for why the flag is safe.
        self._last_read_unreadable = False

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
            state = self._load_state()
            self._refuse_when_unreadable()
            state = state or LocalModelInventoryState()
            key = _normalize_model_dir(model_dir)
            state.entries[key] = LocalModelInventoryEntry(
                cached_models=_normalize_cached_models(cached_models),
                updated_at=_utc_now(),
            )
            self._save_state(state)

    def clear_cached_models(self, model_dir: str = "") -> None:
        with self._lock:
            state = self._load_state()
            self._refuse_when_unreadable()
            if state is None:
                return
            key = _normalize_model_dir(model_dir)
            if state.entries.pop(key, None) is None:
                return
            self._save_state(state)

    def _refuse_when_unreadable(self) -> None:
        """Stop a read-modify-write whose read never reached the file.

        See `persistence.StoreUnavailableError`. Losing this file costs a
        rescan rather than data, but the write would still put one directory
        entry where every remembered directory was.
        """
        if self._last_read_unreadable:
            raise store_unavailable_error(self._path)

    def _load_state(self) -> LocalModelInventoryState | None:
        self._last_read_unreadable = False
        # Both, for consistency with the other stores. This one is only
        # a cache, so the cost of losing it is a rescan rather than data.
        if not self._path.exists() and not backup_path(self._path).exists():
            return None

        payload, source = load_json_with_backup(self._path, expected_type=dict)
        # The backup answered and the primary is there, unopened. Its content
        # is unknown, so the recovery below must not run: the quarantine would
        # rename the primary out from under the name the next load reads and
        # the republish would put the backup over it, and nothing here can
        # tell which of the two copies is the newer one. The data is answered
        # from the backup in memory, and the unreadable flag keeps every
        # read-modify-write off the file until it can be read again.
        primary_unreadable = source == SOURCE_BACKUP_PRIMARY_UNREADABLE
        if primary_unreadable:
            self._last_read_unreadable = True
            note_unreadable_store(self._path)
        if source == SOURCE_UNREADABLE:
            # The file is there and could not be opened; quarantining it
            # would rename an intact inventory out from under the name the
            # next load reads.
            self._last_read_unreadable = True
            note_unreadable_store(self._path)
            return None
        if payload is None:
            quarantine_corrupt_file(self._path, include_backup=True)
            return None

        raw = dict(payload)
        state = LocalModelInventoryState.from_dict(raw)
        if not primary_unreadable and (
            source == "backup" or raw != state.to_dict()
        ):
            # Guarded: the state is already in hand. This write is a
            # convenience -- a republish after a backup recovery, or
            # persisting the normalised shape -- and letting it escape
            # discarded the inventory it had just recovered.
            try:
                self._save_state(state)
            except OSError:
                _LOGGER.exception("Could not rewrite %s", self._path)
        return state

    def _save_state(self, state: LocalModelInventoryState) -> None:
        atomic_write_json(
            self._path,
            state.to_dict(),
            ensure_ascii=True,
            keep_backup=True,
        )
