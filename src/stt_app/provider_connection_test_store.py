from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .app_paths import provider_connection_tests_path
from .config import VALID_ENGINES
from .persistence import (
    SOURCE_BACKUP_PRIMARY_UNREADABLE,
    SOURCE_UNREADABLE,
    atomic_write_json,
    backup_path,
    load_json_with_backup,
    lock_for_path,
    note_unreadable_store,
    parse_json_bool,
    quarantine_corrupt_file,
    store_unavailable_error,
)

_LOGGER = logging.getLogger(__name__)

_CURRENT_SCHEMA_VERSION = 1
_REMOTE_PROVIDERS = tuple(engine for engine in VALID_ENGINES if engine != "local")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    return value if value in _REMOTE_PROVIDERS else ""


@dataclass(slots=True)
class ProviderConnectionTestResult:
    checked_at: str
    ok: bool
    message: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProviderConnectionTestResult:
        return cls(
            checked_at=str(raw.get("checked_at", "")).strip(),
            ok=parse_json_bool(raw.get("ok")),
            message=str(raw.get("message", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "ok": self.ok,
            "message": self.message,
        }


class ProviderConnectionTestStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or provider_connection_tests_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = lock_for_path(self._path)
        # Whether the last `load_all()` reached the file; see
        # `TranscriptHistoryStore.__init__` for why the flag is safe.
        self._last_read_unreadable = False

    @property
    def path(self) -> Path:
        return self._path

    def _refuse_when_unreadable(self) -> None:
        """Stop a read-modify-write whose read never reached the file.

        See `persistence.StoreUnavailableError`: `_save` writes the whole
        result set, so one recorded test would replace every other one.
        """
        if self._last_read_unreadable:
            raise store_unavailable_error(self._path)

    def load_all(self) -> dict[str, ProviderConnectionTestResult]:
        with self._lock:
            self._last_read_unreadable = False
            payload, source = load_json_with_backup(
                self._path,
                expected_type=dict,
            )
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
                # would rename intact results out from under the name the
                # next load reads.
                self._last_read_unreadable = True
                note_unreadable_store(self._path)
                return {}
            if payload is None:
                # ``load_json_with_backup`` collapses "file absent" and "file
                # present but unparseable" into the same ``None`` return. All
                # sibling stores unconditionally quarantine here: the helper is a
                # no-op for genuinely-missing files, and a corrupt primary (even
                # with no usable backup) must be moved aside so the next write
                # does not keep failing on the same bad bytes.
                quarantine_corrupt_file(self._path, include_backup=True)
                return {}

            raw_results = payload.get("results", {})
            if not isinstance(raw_results, dict):
                # Only the file this payload actually came from. Reaching here
                # means the other one was never read, and `include_backup`
                # threw it away regardless: a primary damaged externally in a
                # way that still parses as JSON destroyed a perfectly good
                # backup, and both loads afterwards returned nothing. The two
                # history stores already quarantine just the file at fault; the
                # source is what tells them apart when the backup is the one
                # that parsed. Asked as "did this come from the primary",
                # so a payload the backup supplied while the primary could not
                # be opened moves the backup aside as well -- an unreadable
                # primary is the one file that may never be renamed here.
                quarantine_corrupt_file(
                    self._path if source == "primary" else backup_path(self._path)
                )
                return {}

            results: dict[str, ProviderConnectionTestResult] = {}
            for provider, raw_result in raw_results.items():
                normalized_provider = _normalize_provider(str(provider))
                if not normalized_provider or not isinstance(raw_result, dict):
                    continue
                result = ProviderConnectionTestResult.from_dict(raw_result)
                if result.checked_at:
                    results[normalized_provider] = result
            if source == "backup":
                # Republish, like every other store that recovers: until
                # something writes again the data lives only in the `.bak`, so
                # a second loss takes it for good.
                # Guarded: the results are already in hand, so a write
                # that cannot land must not discard them.
                try:
                    self._save(results)
                except OSError:
                    _LOGGER.exception(
                        "Could not republish %s from its backup", self._path
                    )
            return results

    def save_result(
        self,
        provider: str,
        *,
        ok: bool,
        message: str,
        checked_at: str | None = None,
    ) -> None:
        normalized_provider = _normalize_provider(provider)
        if not normalized_provider:
            return
        with self._lock:
            results = self.load_all()
            self._refuse_when_unreadable()
            results[normalized_provider] = ProviderConnectionTestResult(
                checked_at=checked_at or _utc_now(),
                ok=bool(ok),
                message=str(message or "").strip(),
            )
            self._save(results)

    def clear_result(self, provider: str) -> None:
        normalized_provider = _normalize_provider(provider)
        if not normalized_provider:
            return
        with self._lock:
            results = self.load_all()
            self._refuse_when_unreadable()
            if results.pop(normalized_provider, None) is None:
                return
            self._save(results)

    def _save(self, results: dict[str, ProviderConnectionTestResult]) -> None:
        payload = {
            "schema_version": _CURRENT_SCHEMA_VERSION,
            "results": {
                provider: result.to_dict()
                for provider, result in results.items()
                if _normalize_provider(provider)
            },
        }
        atomic_write_json(self._path, payload, ensure_ascii=True, keep_backup=True)
