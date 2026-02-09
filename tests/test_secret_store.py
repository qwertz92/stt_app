from tts_app.secret_store import KeyringSecretStore


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


def test_keyring_secret_store_set_get_delete():
    backend = FakeKeyringBackend()
    store = KeyringSecretStore(keyring_backend=backend, service_name="tts-app-test")

    store.set_api_key("openai", "sk-test")
    assert store.get_api_key("openai") == "sk-test"

    store.delete_api_key("openai")
    assert store.get_api_key("openai") is None


def test_keyring_secret_store_missing_delete_is_safe():
    backend = FakeKeyringBackend()
    store = KeyringSecretStore(keyring_backend=backend, service_name="tts-app-test")

    store.delete_api_key("azure")
    assert store.get_api_key("azure") is None
