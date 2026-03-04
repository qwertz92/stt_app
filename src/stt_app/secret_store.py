from __future__ import annotations

import json
from pathlib import Path

from typing import Protocol

from .config import KEYRING_SERVICE_NAME, LEGACY_KEYRING_SERVICE_NAMES
from .app_paths import insecure_keys_path


class SecretStore(Protocol):
    def set_api_key(self, provider: str, api_key: str) -> None: ...

    def get_api_key(self, provider: str) -> str | None: ...

    def delete_api_key(self, provider: str) -> None: ...

    def has_api_key(self, provider: str) -> bool: ...

    def set_insecure_fallback_enabled(self, enabled: bool) -> None: ...


class KeyringSecretStore:
    def __init__(
        self,
        keyring_backend=None,
        service_name: str = KEYRING_SERVICE_NAME,
        legacy_service_names: tuple[str, ...] = LEGACY_KEYRING_SERVICE_NAMES,
    ) -> None:
        if keyring_backend is None:
            import keyring  # type: ignore

            keyring_backend = keyring

        self._keyring = keyring_backend
        self._service_name = service_name
        self._legacy_service_names = tuple(
            name
            for name in legacy_service_names
            if isinstance(name, str)
            and name.strip()
            and name.strip() != self._service_name
        )
        self._insecure_fallback_enabled = False
        self._insecure_path: Path = insecure_keys_path()

    def set_insecure_fallback_enabled(self, enabled: bool) -> None:
        self._insecure_fallback_enabled = bool(enabled)

    def _read_insecure_store(self) -> dict[str, str]:
        try:
            payload = json.loads(self._insecure_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, str] = {}
        for key, value in payload.items():
            if isinstance(key, str) and isinstance(value, str) and value.strip():
                result[key] = value
        return result

    def _write_insecure_store(self, payload: dict[str, str]) -> None:
        self._insecure_path.parent.mkdir(parents=True, exist_ok=True)
        self._insecure_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def _set_insecure_api_key(self, provider: str, api_key: str) -> None:
        payload = self._read_insecure_store()
        payload[provider] = api_key
        self._write_insecure_store(payload)

    def _get_insecure_api_key(self, provider: str) -> str | None:
        payload = self._read_insecure_store()
        value = payload.get(provider)
        if not value:
            return None
        return str(value)

    def _delete_insecure_api_key(self, provider: str) -> None:
        payload = self._read_insecure_store()
        if provider not in payload:
            return
        payload.pop(provider, None)
        self._write_insecure_store(payload)

    def _get_keyring_value(self, service_name: str, provider: str) -> str | None:
        try:
            value = self._keyring.get_password(service_name, provider)
        except Exception:
            # keyring backends can fail with FileNotFoundError, OSError,
            # or backend-specific errors on misconfigured systems.
            return None
        if value is None:
            return None
        return str(value)

    def set_api_key(self, provider: str, api_key: str) -> None:
        try:
            self._keyring.set_password(self._service_name, provider, api_key)
            for legacy_name in self._legacy_service_names:
                try:
                    self._keyring.delete_password(legacy_name, provider)
                except Exception:
                    pass
            # Keyring succeeded: remove stale insecure fallback copy.
            self._delete_insecure_api_key(provider)
            return
        except Exception:
            if not self._insecure_fallback_enabled:
                raise
        self._set_insecure_api_key(provider, api_key)

    def get_api_key(self, provider: str) -> str | None:
        value = self._get_keyring_value(self._service_name, provider)
        if value is not None:
            return value
        for legacy_name in self._legacy_service_names:
            legacy_value = self._get_keyring_value(legacy_name, provider)
            if legacy_value is None:
                continue
            try:
                self._keyring.set_password(
                    self._service_name,
                    provider,
                    legacy_value,
                )
                self._keyring.delete_password(legacy_name, provider)
            except Exception:
                pass
            return legacy_value
        if self._insecure_fallback_enabled:
            return self._get_insecure_api_key(provider)
        return None

    def delete_api_key(self, provider: str) -> None:
        for service_name in (self._service_name, *self._legacy_service_names):
            try:
                self._keyring.delete_password(service_name, provider)
            except Exception:
                pass
        if self._insecure_fallback_enabled:
            try:
                self._delete_insecure_api_key(provider)
            except Exception:
                pass

    def has_api_key(self, provider: str) -> bool:
        return self.get_api_key(provider) is not None
