from __future__ import annotations

from typing import Protocol

from .config import KEYRING_SERVICE_NAME


class SecretStore(Protocol):
    def set_api_key(self, provider: str, api_key: str) -> None: ...

    def get_api_key(self, provider: str) -> str | None: ...

    def delete_api_key(self, provider: str) -> None: ...

    def has_api_key(self, provider: str) -> bool: ...


class KeyringSecretStore:
    def __init__(
        self,
        keyring_backend=None,
        service_name: str = KEYRING_SERVICE_NAME,
    ) -> None:
        if keyring_backend is None:
            import keyring  # type: ignore

            keyring_backend = keyring

        self._keyring = keyring_backend
        self._service_name = service_name

    def set_api_key(self, provider: str, api_key: str) -> None:
        self._keyring.set_password(self._service_name, provider, api_key)

    def get_api_key(self, provider: str) -> str | None:
        value = self._keyring.get_password(self._service_name, provider)
        if value is None:
            return None
        return str(value)

    def delete_api_key(self, provider: str) -> None:
        try:
            self._keyring.delete_password(self._service_name, provider)
        except Exception:
            return

    def has_api_key(self, provider: str) -> bool:
        return self.get_api_key(provider) is not None
