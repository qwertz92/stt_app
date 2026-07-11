from __future__ import annotations

import json
from pathlib import Path

from typing import Protocol

from .config import KEYRING_SERVICE_NAME, LEGACY_KEYRING_SERVICE_NAMES
from .app_paths import insecure_keys_path
from .persistence import atomic_write_json, lock_for_path


class SecretStore(Protocol):
    def set_api_key(self, provider: str, api_key: str) -> None: ...

    def get_api_key(self, provider: str) -> str | None: ...

    def get_api_key_source(self, provider: str) -> str: ...

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
        self._insecure_lock = lock_for_path(self._insecure_path)

    def set_insecure_fallback_enabled(self, enabled: bool) -> None:
        self._insecure_fallback_enabled = bool(enabled)

    def _read_insecure_store(self) -> dict[str, str]:
        with self._insecure_lock:
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
        atomic_write_json(
            self._insecure_path,
            payload,
            ensure_ascii=True,
            keep_backup=False,
        )

    def _set_insecure_api_key(self, provider: str, api_key: str) -> None:
        with self._insecure_lock:
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
        with self._insecure_lock:
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
        except Exception:
            if not self._insecure_fallback_enabled:
                raise
        else:
            # Keyring write succeeded: remove stale insecure fallback copy.
            # A failure here must NOT fall through to the insecure write below;
            # the key is safely in the keyring and we just failed to clean up
            # the old plaintext copy, which will be retried on the next write.
            try:
                self._delete_insecure_api_key(provider)
            except Exception as exc:
                raise RuntimeError(
                    "The API key was stored securely, but its stale insecure "
                    "fallback copy could not be removed."
                ) from exc
            return
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

    def get_api_key_source(self, provider: str) -> str:
        value = self._get_keyring_value(self._service_name, provider)
        if value is not None:
            return "keyring"

        for legacy_name in self._legacy_service_names:
            legacy_value = self._get_keyring_value(legacy_name, provider)
            if legacy_value is not None:
                return "legacy-keyring"

        insecure_value = self._get_insecure_api_key(provider)
        if insecure_value is not None:
            if self._insecure_fallback_enabled:
                return "insecure"
            return "insecure-disabled"

        return "none"

    def delete_api_key(self, provider: str) -> None:
        errors: list[str] = []
        for service_name in (self._service_name, *self._legacy_service_names):
            try:
                existing = self._keyring.get_password(service_name, provider)
            except Exception as exc:
                errors.append(f"{service_name}: {exc}")
                continue
            if existing is None:
                continue
            try:
                self._keyring.delete_password(service_name, provider)
            except Exception as exc:
                errors.append(f"{service_name}: {exc}")
        try:
            self._delete_insecure_api_key(provider)
        except Exception as exc:
            errors.append(f"insecure fallback: {exc}")
        if errors:
            raise RuntimeError(
                "Could not confirm deletion from all credential stores: "
                + " | ".join(errors)
            )

    def has_api_key(self, provider: str) -> bool:
        return self.get_api_key(provider) is not None
