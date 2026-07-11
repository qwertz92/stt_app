import json
import threading

import pytest

from stt_app.secret_store import KeyringSecretStore


class FakeKeyringBackend:
    def __init__(self):
        self._store = {}

    def set_password(self, service_name, username, password):
        self._store[(service_name, username)] = password

    def get_password(self, service_name, username):
        return self._store.get((service_name, username))

    def delete_password(self, service_name, username):
        if (service_name, username) not in self._store:
            raise RuntimeError("missing")
        del self._store[(service_name, username)]


class FailingKeyringBackend:
    def set_password(self, service_name, username, password):
        _ = service_name
        _ = username
        _ = password
        raise FileNotFoundError("backend unavailable")

    def get_password(self, service_name, username):
        _ = service_name
        _ = username
        raise FileNotFoundError("backend unavailable")

    def delete_password(self, service_name, username):
        _ = service_name
        _ = username
        raise FileNotFoundError("backend unavailable")


def test_keyring_secret_store_set_get_delete():
    backend = FakeKeyringBackend()
    store = KeyringSecretStore(keyring_backend=backend, service_name="stt-app-test")

    store.set_api_key("openai", "sk-test")
    assert store.get_api_key("openai") == "sk-test"

    store.delete_api_key("openai")
    assert store.get_api_key("openai") is None


def test_keyring_secret_store_missing_delete_is_safe():
    backend = FakeKeyringBackend()
    store = KeyringSecretStore(keyring_backend=backend, service_name="stt-app-test")

    store.delete_api_key("azure")
    assert store.get_api_key("azure") is None


def test_keyring_secret_store_reads_legacy_service_and_migrates():
    backend = FakeKeyringBackend()
    backend.set_password("tts-app-test", "openai", "legacy-key")
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=("tts-app-test",),
    )

    assert store.get_api_key("openai") == "legacy-key"
    assert backend.get_password("stt-app-test", "openai") == "legacy-key"
    assert backend.get_password("tts-app-test", "openai") is None


def test_keyring_secret_store_reports_source_variants(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    backend = FakeKeyringBackend()
    backend.set_password("stt-app-test", "openai", "secure")
    backend.set_password("tts-app-test", "groq", "legacy")
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=("tts-app-test",),
    )
    store.set_insecure_fallback_enabled(False)
    store._set_insecure_api_key("deepgram", "plain")

    assert store.get_api_key_source("openai") == "keyring"
    assert store.get_api_key_source("groq") == "legacy-keyring"
    assert store.get_api_key_source("deepgram") == "insecure-disabled"

    store.set_insecure_fallback_enabled(True)
    assert store.get_api_key_source("deepgram") == "insecure"
    assert store.get_api_key_source("assemblyai") == "none"


def test_insecure_fallback_disabled_raises_on_set(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    store = KeyringSecretStore(
        keyring_backend=FailingKeyringBackend(),
        service_name="stt-app-test",
    )

    try:
        store.set_api_key("openai", "sk-test")
        assert False, "set_api_key should raise when fallback is disabled"
    except FileNotFoundError:
        pass


def test_insecure_fallback_stores_and_reads_key(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    store = KeyringSecretStore(
        keyring_backend=FailingKeyringBackend(),
        service_name="stt-app-test",
    )
    store.set_insecure_fallback_enabled(True)

    store.set_api_key("groq", "gsk_test")
    assert store.get_api_key("groq") == "gsk_test"
    assert store.has_api_key("groq") is True

    with pytest.raises(RuntimeError, match="Could not confirm deletion"):
        store.delete_api_key("groq")
    assert store.get_api_key("groq") is None


def test_delete_api_key_also_removes_insecure_copy_when_fallback_disabled(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    store = KeyringSecretStore(
        keyring_backend=FailingKeyringBackend(),
        service_name="stt-app-test",
    )
    store.set_insecure_fallback_enabled(True)
    store.set_api_key("openai", "sk-test")
    store.set_insecure_fallback_enabled(False)

    with pytest.raises(RuntimeError, match="Could not confirm deletion"):
        store.delete_api_key("openai")

    assert store.get_api_key_source("openai") == "none"


def test_delete_api_key_reports_backend_failure_for_existing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))

    class _DeleteFailureBackend(FakeKeyringBackend):
        def delete_password(self, service_name, username):
            raise OSError("credential vault is locked")

    backend = _DeleteFailureBackend()
    backend.set_password("stt-app-test", "openai", "secret")
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=(),
    )

    with pytest.raises(RuntimeError, match="credential vault is locked"):
        store.delete_api_key("openai")

    assert backend.get_password("stt-app-test", "openai") == "secret"


def test_insecure_store_preserves_parallel_provider_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    stores = [
        KeyringSecretStore(
            keyring_backend=FailingKeyringBackend(),
            service_name="stt-app-test",
            legacy_service_names=(),
        )
        for _index in range(8)
    ]
    for store in stores:
        store.set_insecure_fallback_enabled(True)
    barrier = threading.Barrier(len(stores))

    def save(index):
        barrier.wait(timeout=2.0)
        stores[index].set_api_key(f"provider-{index}", f"secret-{index}")

    threads = [threading.Thread(target=save, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert all(not thread.is_alive() for thread in threads)
    payload = json.loads(stores[0]._insecure_path.read_text(encoding="utf-8"))
    assert payload == {
        f"provider-{index}": f"secret-{index}" for index in range(8)
    }
