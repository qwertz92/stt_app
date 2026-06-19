from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_paths import provider_connection_tests_path
from .config import VALID_ENGINES
from .persistence import atomic_write_json, load_json_with_backup, quarantine_corrupt_file

_CURRENT_SCHEMA_VERSION = 1
_REMOTE_PROVIDERS = tuple(engine for engine in VALID_ENGINES if engine != "local")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    return value if value in _REMOTE_PROVIDERS else ""


@dataclass(slots=True)
class ProviderConnectionTestResult:
    checked_at: str
    ok: bool
    message: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProviderConnectionTestResult":
        return cls(
            checked_at=str(raw.get("checked_at", "")).strip(),
            ok=bool(raw.get("ok", False)),
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

    @property
    def path(self) -> Path:
        return self._path

    def load_all(self) -> dict[str, ProviderConnectionTestResult]:
        payload, source = load_json_with_backup(self._path, expected_type=dict)
        if payload is None:
            if source != "missing":
                quarantine_corrupt_file(self._path, include_backup=True)
            return {}

        raw_results = payload.get("results", {})
        if not isinstance(raw_results, dict):
            quarantine_corrupt_file(self._path, include_backup=True)
            return {}

        results: dict[str, ProviderConnectionTestResult] = {}
        for provider, raw_result in raw_results.items():
            normalized_provider = _normalize_provider(str(provider))
            if not normalized_provider or not isinstance(raw_result, dict):
                continue
            result = ProviderConnectionTestResult.from_dict(raw_result)
            if result.checked_at:
                results[normalized_provider] = result
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
        results = self.load_all()
        results[normalized_provider] = ProviderConnectionTestResult(
            checked_at=checked_at or _utc_now(),
            ok=bool(ok),
            message=str(message or "").strip(),
        )
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
