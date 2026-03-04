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

    store.delete_api_key("groq")
    assert store.get_api_key("groq") is None
