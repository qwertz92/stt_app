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


class SwitchableKeyringBackend(FakeKeyringBackend):
    """A keyring whose reads and writes can be made to fail independently.

    `FailingKeyringBackend` fails both at once, which is the machine with no
    usable credential store at all. The case that needs the two apart is a
    keyring that is present and readable while a write is refused -- a locked
    vault, a policy, a transient backend error -- because then the old value
    is still there to be read after the new one was not stored.
    """

    def __init__(self):
        super().__init__()
        self.read_fails = False
        self.write_fails = False

    def set_password(self, service_name, username, password):
        if self.write_fails:
            raise OSError("the credential vault refused the write")
        super().set_password(service_name, username, password)

    def get_password(self, service_name, username):
        if self.read_fails:
            raise OSError("the credential vault refused the read")
        return super().get_password(service_name, username)


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


def test_a_refused_keyring_write_over_an_existing_key_is_not_reported_as_saved(
    tmp_path, monkeypatch
):
    """A key stored where nothing ever reads it is worse than a failed save.

    `get_api_key` reads the keyring before the fallback file, so a fallback
    copy written after a refused keyring write is never read while the keyring
    still answers: the dialog reported the new key as saved, every request
    kept using the old one, and the new one sat unused in plaintext -- also
    after the keyring recovered, because nothing reconciles the two.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    backend = SwitchableKeyringBackend()
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=(),
    )
    store.set_insecure_fallback_enabled(True)
    store.set_api_key("openai", "sk-old")
    backend.write_fails = True

    with pytest.raises(RuntimeError, match="openai"):
        store.set_api_key("openai", "sk-new")

    assert backend.get_password("stt-app-test", "openai") == "sk-old"
    assert store.get_api_key("openai") == "sk-old"
    assert store.get_api_key_source("openai") == "keyring"
    plaintext = (
        store._insecure_path.read_text(encoding="utf-8")
        if store._insecure_path.exists()
        else ""
    )
    assert "sk-new" not in plaintext


def test_a_key_under_a_legacy_service_name_shadows_the_fallback_too(
    tmp_path, monkeypatch
):
    """`get_api_key` reads the legacy names before the fallback file as well.

    An install upgraded from the old service name whose migration write never
    succeeded holds its key there only. A keyring that refuses the new write
    refuses the migration too, so that key keeps being returned first, and a
    fallback copy written behind it would be the same silent failure as one
    written behind the primary name.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    backend = SwitchableKeyringBackend()
    backend.set_password("stt-app-legacy", "openai", "sk-old")
    backend.write_fails = True
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=("stt-app-legacy",),
    )
    store.set_insecure_fallback_enabled(True)

    with pytest.raises(RuntimeError, match="openai"):
        store.set_api_key("openai", "sk-new")

    assert store.get_api_key("openai") == "sk-old"
    assert store.get_api_key_source("openai") == "legacy-keyring"
    plaintext = (
        store._insecure_path.read_text(encoding="utf-8")
        if store._insecure_path.exists()
        else ""
    )
    assert "sk-new" not in plaintext


def test_a_keyring_write_that_raised_after_it_landed_still_writes_the_fallback(
    tmp_path, monkeypatch
):
    """The refusal must not fire when the keyring holds the new value itself.

    A backend can raise after the value has landed. The keyring then answers
    the key the user just typed, so nothing shadows it and reporting a failure
    would be wrong.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))

    class _StoresThenRaises(SwitchableKeyringBackend):
        def set_password(self, service_name, username, password):
            FakeKeyringBackend.set_password(self, service_name, username, password)
            raise OSError("the credential vault reported a failure after writing")

    backend = _StoresThenRaises()
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=(),
    )
    store.set_insecure_fallback_enabled(True)

    store.set_api_key("openai", "sk-new")

    assert store.get_api_key("openai") == "sk-new"
    assert store.get_api_key_source("openai") == "keyring"
    assert "sk-new" in store._insecure_path.read_text(encoding="utf-8")


def test_a_refused_write_with_an_empty_keyring_still_reaches_the_fallback(
    tmp_path, monkeypatch
):
    """The case the fallback exists for: a keyring that stores nothing.

    Nothing shadows the new key, so the fallback copy is what every read
    answers and the save really did store the key the user typed.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    backend = SwitchableKeyringBackend()
    backend.write_fails = True
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=(),
    )
    store.set_insecure_fallback_enabled(True)

    store.set_api_key("groq", "gsk-new")

    assert store.get_api_key("groq") == "gsk-new"
    assert store.get_api_key_source("groq") == "insecure"


def test_an_empty_keyring_answer_is_nothing_stored(tmp_path, monkeypatch):
    """`get_password` may answer "" for nothing stored, and "" is no key.

    Every reader answered it as a value: `get_api_key` returned "" without
    looking at the fallback file, `has_api_key` said True and the source
    read "keyring" -- for a provider whose every request would then fail
    with a missing key while Settings showed one stored.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    backend = FakeKeyringBackend()
    backend.set_password("stt-app-test", "openai", "")
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=("stt-app-legacy",),
    )

    assert store.get_api_key("openai") is None
    assert store.has_api_key("openai") is False
    assert store.get_api_key_source("openai") == "none"

    backend.set_password("stt-app-legacy", "groq", "")
    assert store.get_api_key("groq") is None
    assert store.get_api_key_source("groq") == "none"

    store.set_insecure_fallback_enabled(True)
    store._set_insecure_api_key("openai", "sk-fallback")
    assert store.get_api_key("openai") == "sk-fallback"
    assert store.get_api_key_source("openai") == "insecure"


def test_a_refused_write_over_an_empty_keyring_answer_still_reaches_the_fallback(
    tmp_path, monkeypatch
):
    """The shadow guard read "" as a previous key and refused the save.

    The keyring held nothing that could shadow the fallback copy, yet the
    new key was written nowhere, and the error claimed the keyring still
    held the previous one. A blank answer is the `None` case.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    backend = SwitchableKeyringBackend()
    backend.set_password("stt-app-test", "openai", "")
    backend.write_fails = True
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=(),
    )
    store.set_insecure_fallback_enabled(True)

    store.set_api_key("openai", "sk-new")

    assert store.get_api_key("openai") == "sk-new"
    assert store.get_api_key_source("openai") == "insecure"


def test_a_keyring_whose_read_also_fails_cannot_shadow_the_fallback(
    tmp_path, monkeypatch
):
    """A read that raises is no evidence of an old key, so the save proceeds.

    Every reader treats a raising keyring read as "nothing stored" and falls
    through to the fallback file, so the new key does become the active one.
    Refusing here would block every save on the machine the fallback exists
    for -- the one whose keyring cannot be reached at all.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    backend = SwitchableKeyringBackend()
    backend.set_password("stt-app-test", "openai", "sk-old")
    backend.read_fails = True
    backend.write_fails = True
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=(),
    )
    store.set_insecure_fallback_enabled(True)

    store.set_api_key("openai", "sk-new")

    assert store.get_api_key("openai") == "sk-new"
    assert store.get_api_key_source("openai") == "insecure"


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


def _damaged_store(tmp_path, monkeypatch, damage):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    store = KeyringSecretStore(
        keyring_backend=FailingKeyringBackend(),
        service_name="stt-app-test",
        legacy_service_names=(),
    )
    store.set_insecure_fallback_enabled(True)
    store.set_api_key("openai", "sk-openai")
    store.set_api_key("groq", "gsk-groq")
    store.set_api_key("deepgram", "dg-deepgram")

    damage(store._insecure_path)
    return store


_DAMAGE_KINDS = [
    ("truncated json", lambda p: p.write_text('{"openai": "sk-open', "utf-8")),
    ("a json array", lambda p: p.write_text("[]", encoding="utf-8")),
    ("bytes that are not utf-8", lambda p: p.write_bytes(b'{"openai": "\xff"}')),
]


@pytest.mark.parametrize(
    ("label", "damage"),
    _DAMAGE_KINDS,
    ids=[label for label, _damage in _DAMAGE_KINDS],
)
def test_a_damaged_insecure_store_refuses_the_save_instead_of_emptying_it(
    tmp_path, monkeypatch, label, damage
):
    """An unreadable file holds keys nobody can see, not zero keys.

    The read returned the same empty mapping for a missing file and for one
    that will not parse, and the save built the new payload out of that
    mapping -- so storing one key rewrote the file with only that key in it
    and every other provider's key was gone, with nothing reported. Refusing
    the save keeps the file exactly as it is, which is what the user needs in
    order to repair it.
    """
    store = _damaged_store(tmp_path, monkeypatch, damage)
    before = store._insecure_path.read_bytes()

    with pytest.raises(RuntimeError, match="cannot be read"):
        store.set_api_key("elevenlabs", "el-value")

    assert store._insecure_path.read_bytes() == before, (
        f"{label}: the refused save still rewrote the file"
    )


def test_an_explicit_delete_reports_a_damaged_insecure_store(tmp_path, monkeypatch):
    """Deleting a key must not answer 'done' while the plaintext may remain.

    With the file unreadable the provider is simply absent from the empty
    mapping, so the removal was skipped and reported as a success -- while a
    plaintext copy of the very key the user asked to remove may still be
    sitting in that file.
    """
    store = _damaged_store(
        tmp_path,
        monkeypatch,
        lambda p: p.write_text('{"openai": "sk-open', encoding="utf-8"),
    )

    with pytest.raises(RuntimeError, match=r"insecure fallback:.*cannot be read"):
        store.delete_api_key("openai")


def test_a_keyring_write_still_succeeds_over_a_damaged_insecure_store(
    tmp_path, monkeypatch
):
    """The stale-copy cleanup is deliberately the tolerant one.

    It runs after every successful keyring write, including for users who
    never enabled the fallback at all, so raising there would make one
    leftover file block every future key save. Nothing is lost by skipping
    it: the key is in the keyring, and the explicit delete above is the path
    that has to be honest about the leftover copy.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    backend = FakeKeyringBackend()
    store = KeyringSecretStore(
        keyring_backend=backend,
        service_name="stt-app-test",
        legacy_service_names=(),
    )
    store._insecure_path.parent.mkdir(parents=True, exist_ok=True)
    store._insecure_path.write_text('{"openai": "sk-open', encoding="utf-8")

    store.set_api_key("openai", "sk-test")

    assert backend.get_password("stt-app-test", "openai") == "sk-test"


def test_a_missing_insecure_store_is_not_treated_as_damaged(tmp_path, monkeypatch):
    """The whole point of the distinction: a first run still stores its key."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    store = KeyringSecretStore(
        keyring_backend=FailingKeyringBackend(),
        service_name="stt-app-test",
        legacy_service_names=(),
    )
    store.set_insecure_fallback_enabled(True)

    store.set_api_key("azure", "az-value")

    assert store.get_api_key("azure") == "az-value"
