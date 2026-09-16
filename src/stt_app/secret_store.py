from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

from .app_paths import insecure_keys_path
from .config import KEYRING_SERVICE_NAME, LEGACY_KEYRING_SERVICE_NAMES
from .persistence import atomic_write_json, lock_for_path

logger = logging.getLogger(__name__)


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
        self._reported_damaged_insecure_store = False

    def set_insecure_fallback_enabled(self, enabled: bool) -> None:
        self._insecure_fallback_enabled = bool(enabled)

    def _load_insecure_payload(self) -> tuple[dict[str, str], bool]:
        """Return the readable keys plus whether the file is present but unusable.

        A missing file legitimately holds nothing. A file that exists and will
        not parse holds keys nobody can see, and collapsing the two cases into
        the same empty mapping is what let one save rebuild the file from it
        and discard every other provider it still carried.
        """
        with self._insecure_lock:
            try:
                raw = self._insecure_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return {}, False
            except Exception:
                return {}, self._report_damaged_insecure_store()
            try:
                payload = json.loads(raw)
            except Exception:
                return {}, self._report_damaged_insecure_store()
        if not isinstance(payload, dict):
            return {}, self._report_damaged_insecure_store()
        return (
            {
                key: value
                for key, value in payload.items()
                if isinstance(key, str) and isinstance(value, str) and value.strip()
            },
            False,
        )

    def _report_damaged_insecure_store(self) -> bool:
        """Log the damaged file once per store and answer that it is damaged."""
        if not self._reported_damaged_insecure_store:
            self._reported_damaged_insecure_store = True
            logger.warning(
                "insecure_key_store_unreadable path=%s", self._insecure_path
            )
        return True

    def _read_insecure_store(self) -> dict[str, str]:
        return self._load_insecure_payload()[0]

    def _write_insecure_store(self, payload: dict[str, str]) -> None:
        atomic_write_json(
            self._insecure_path,
            payload,
            ensure_ascii=True,
            keep_backup=False,
        )

    def _set_insecure_api_key(self, provider: str, api_key: str) -> None:
        with self._insecure_lock:
            payload, damaged = self._load_insecure_payload()
            if damaged:
                raise RuntimeError(
                    "The insecure API key file exists but cannot be read, so "
                    "storing this key would discard every other key it still "
                    f"holds. Repair or remove {self._insecure_path} and try "
                    "again."
                )
            payload[provider] = api_key
            self._write_insecure_store(payload)

    def _get_insecure_api_key(self, provider: str) -> str | None:
        payload = self._read_insecure_store()
        value = payload.get(provider)
        if not value:
            return None
        return str(value)

    def _delete_insecure_api_key(self, provider: str, *, strict: bool = False) -> None:
        """Remove one provider's plaintext copy.

        `strict` is for the explicit user-facing delete, which must not report
        success while an unreadable file may still hold that key in plain
        text. The stale-copy cleanup after a successful keyring write stays
        tolerant: the key is safely in the keyring there, and failing every
        future save over a leftover file the user never knew about would cost
        more than the stale copy does.
        """
        with self._insecure_lock:
            payload, damaged = self._load_insecure_payload()
            if damaged:
                if strict:
                    raise RuntimeError(
                        f"{self._insecure_path} exists but cannot be read, so "
                        "a plaintext copy of this key may still be on disk."
                    )
                return
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
        # A backend may answer "" for nothing stored (a credential with an
        # empty blob), and "" is no key: read as a value it was returned by
        # `get_api_key` without a look at the fallback file, counted by
        # `has_api_key`, reported as "keyring" -- and the shadow guard below
        # took it for a previous key and refused the fallback write, so a new
        # key was stored nowhere.
        return str(value) or None

    def _refuse_a_fallback_the_keyring_would_shadow(
        self, provider: str, api_key: str, cause: BaseException
    ) -> None:
        """Refuse the fallback write while the keyring still answers an old key.

        `get_api_key` and `get_api_key_source` read the keyring before the
        fallback file, so a fallback copy written after a refused keyring write
        is never read while the keyring still holds a value: the save reported
        success, every request kept using the previous key, and the new one sat
        unused in plaintext -- also after the keyring recovered, because nothing
        reconciles the two. Raising instead leaves the old key active, which the
        settings dialog's existing failure path reports while keeping the typed
        value.

        Two answers proceed to the fallback write. `None` means nothing in the
        keyring can shadow the new key -- and `_get_keyring_value` also answers
        `None` for a read that raised and for a blank value, which is exactly
        what every other reader does with them, so the fallback copy is the
        value they will all return. The new key itself means the write landed
        before the backend raised.

        Every name `get_api_key` reads is asked, in its order: the legacy
        service names shadow the fallback file exactly as the primary one does,
        and a keyring that refuses writes also refuses the migration that a
        successful read would otherwise perform, so a key living only under a
        legacy name stays there and keeps being returned first.
        """
        stored = None
        for service_name in (self._service_name, *self._legacy_service_names):
            stored = self._get_keyring_value(service_name, provider)
            if stored is not None:
                break
        if stored is None or stored == api_key:
            return
        raise RuntimeError(
            f"The keyring refused the new {provider} API key and still holds "
            "the previous one, which every read returns first, so nothing was "
            "changed."
        ) from cause

    def set_api_key(self, provider: str, api_key: str) -> None:
        try:
            self._keyring.set_password(self._service_name, provider, api_key)
            for legacy_name in self._legacy_service_names:
                try:
                    self._keyring.delete_password(legacy_name, provider)
                except Exception:
                    pass
        except Exception as exc:
            if not self._insecure_fallback_enabled:
                raise
            self._refuse_a_fallback_the_keyring_would_shadow(provider, api_key, exc)
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
            self._delete_insecure_api_key(provider, strict=True)
        except Exception as exc:
            errors.append(f"insecure fallback: {exc}")
        if errors:
            raise RuntimeError(
                "Could not confirm deletion from all credential stores: "
                + " | ".join(errors)
            )

    def has_api_key(self, provider: str) -> bool:
        return self.get_api_key(provider) is not None
