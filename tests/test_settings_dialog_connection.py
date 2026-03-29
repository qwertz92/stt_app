from __future__ import annotations

from PySide6 import QtWidgets

import stt_app.settings_dialog as settings_dialog_module
from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_store import AppSettings


class _FakeSettingsStore:
    def __init__(self, settings: AppSettings):
        self._settings = settings
        self.saved: AppSettings | None = None

    def load(self) -> AppSettings:
        return self._settings

    def save(self, settings: AppSettings) -> None:
        self.saved = settings


class _FakeSecretStore:
    def __init__(self, values: dict[str, str] | None = None):
        self._values = dict(values or {})
        self._sources = {
            provider: ("keyring" if value else "none")
            for provider, value in self._values.items()
        }
        self.set_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        self._insecure_enabled = False

    def get_api_key(self, provider: str) -> str | None:
        return self._values.get(provider)

    def get_api_key_source(self, provider: str) -> str:
        return self._sources.get(provider, "none")

    def set_api_key(self, provider: str, api_key: str) -> None:
        self.set_calls.append((provider, api_key))
        self._values[provider] = api_key
        self._sources[provider] = (
            "insecure" if self._insecure_enabled else "keyring"
        )

    def delete_api_key(self, provider: str) -> None:
        self.delete_calls.append(provider)
        self._values.pop(provider, None)
        self._sources[provider] = "none"

    def set_insecure_fallback_enabled(self, enabled: bool) -> None:
        self._insecure_enabled = bool(enabled)


class _FakeLogger:
    def diagnostics_text(self) -> str:
        return "diag"


class _ImmediateThread:
    def __init__(self, *args, target=None, kwargs=None, **extra) -> None:
        self._target = target
        thread_args = extra.get("args", ())
        self._args = tuple(thread_args if thread_args else args)
        self._kwargs = dict(kwargs or {})

    def start(self) -> None:
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


def _make_dialog(settings: AppSettings, secret_values: dict[str, str] | None = None):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    secret_store = _FakeSecretStore(secret_values)
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(settings),
        secret_store=secret_store,
        app_logger=_FakeLogger(),
    )
    return dialog, app, secret_store


def test_engine_combo_hides_unimplemented_providers():
    dialog, app, _secret_store = _make_dialog(AppSettings())
    assert dialog.engine_combo.findData("openai") >= 0
    assert dialog.engine_combo.findData("elevenlabs") >= 0
    assert dialog.engine_combo.findData("azure") == -1
    _ = app


def test_test_connection_missing_key_shows_error():
    dialog, app, _secret_store = _make_dialog(AppSettings(engine="assemblyai"))
    target_index = dialog.test_conn_target_combo.findData("assemblyai")
    dialog.test_conn_target_combo.setCurrentIndex(target_index)

    dialog._test_connection()

    assert "No API key entered" in dialog.test_conn_result.text()
    assert dialog.test_conn_button.isEnabled() is True
    _ = app


def test_test_connection_runs_in_background_worker(monkeypatch):
    import stt_app.transcriber.deepgram_provider as deepgram_provider_module

    class _FakeDeepgramTranscriber:
        def __init__(
            self,
            api_key: str,
            language_mode: str = "auto",
            model: str = "nova-3",
        ) -> None:
            self._api_key = api_key
            self._language_mode = language_mode
            self._model = model

        def test_connection(self) -> tuple[bool, str]:
            return True, "Connection OK — API key is valid."

    monkeypatch.setattr(
        settings_dialog_module.threading,
        "Thread",
        _ImmediateThread,
    )
    monkeypatch.setattr(
        deepgram_provider_module,
        "DeepgramTranscriber",
        _FakeDeepgramTranscriber,
    )

    dialog, app, _secret_store = _make_dialog(AppSettings(engine="local"))
    target_index = dialog.test_conn_target_combo.findData("deepgram")
    dialog.test_conn_target_combo.setCurrentIndex(target_index)
    dialog.deepgram_key_edit.setText("dg-test-key")

    dialog._test_connection()

    assert dialog.test_conn_button.isEnabled() is True
    assert dialog.test_conn_result.text().startswith("\u2713")
    assert "Connection OK" in dialog.test_conn_result.text()
    _ = app


def test_openai_connection_runs_in_background_worker(monkeypatch):
    import stt_app.transcriber.openai_provider as openai_provider_module

    class _FakeOpenAITranscriber:
        def __init__(
            self,
            api_key: str,
            language_mode: str = "auto",
            model: str = "gpt-4o-mini-transcribe",
        ) -> None:
            self._api_key = api_key
            self._language_mode = language_mode
            self._model = model

        def test_connection(self) -> tuple[bool, str]:
            return True, "Connection OK — API key is valid."

    monkeypatch.setattr(
        settings_dialog_module.threading,
        "Thread",
        _ImmediateThread,
    )
    monkeypatch.setattr(
        openai_provider_module,
        "OpenAITranscriber",
        _FakeOpenAITranscriber,
    )

    dialog, app, _secret_store = _make_dialog(AppSettings(engine="local"))
    target_index = dialog.test_conn_target_combo.findData("openai")
    dialog.test_conn_target_combo.setCurrentIndex(target_index)
    dialog.openai_key_edit.setText("oa-key")
    engine_index = dialog.engine_combo.findData("openai")
    dialog.engine_combo.setCurrentIndex(engine_index)
    model_index = dialog.remote_model_combo.findData("gpt-4o-transcribe")
    dialog.remote_model_combo.setCurrentIndex(model_index)

    dialog._test_connection()

    assert dialog.test_conn_button.isEnabled() is True
    assert dialog.test_conn_result.text().startswith("\u2713")
    assert "Connection OK" in dialog.test_conn_result.text()
    _ = app


def test_elevenlabs_connection_runs_in_background_worker(monkeypatch):
    import stt_app.transcriber.elevenlabs_provider as elevenlabs_provider_module

    class _FakeElevenLabsTranscriber:
        def __init__(
            self,
            api_key: str,
            language_mode: str = "auto",
            model: str = "scribe_v2",
        ) -> None:
            self._api_key = api_key
            self._language_mode = language_mode
            self._model = model

        def test_connection(self) -> tuple[bool, str]:
            return True, "Connection OK — API key is valid."

    monkeypatch.setattr(
        settings_dialog_module.threading,
        "Thread",
        _ImmediateThread,
    )
    monkeypatch.setattr(
        elevenlabs_provider_module,
        "ElevenLabsTranscriber",
        _FakeElevenLabsTranscriber,
    )

    dialog, app, _secret_store = _make_dialog(AppSettings(engine="local"))
    target_index = dialog.test_conn_target_combo.findData("elevenlabs")
    dialog.test_conn_target_combo.setCurrentIndex(target_index)
    dialog.elevenlabs_key_edit.setText("el-key")
    engine_index = dialog.engine_combo.findData("elevenlabs")
    dialog.engine_combo.setCurrentIndex(engine_index)
    model_index = dialog.remote_model_combo.findData("scribe_v1")
    dialog.remote_model_combo.setCurrentIndex(model_index)

    dialog._test_connection()

    assert dialog.test_conn_button.isEnabled() is True
    assert dialog.test_conn_result.text().startswith("\u2713")
    assert "Connection OK" in dialog.test_conn_result.text()
    _ = app


def test_stale_connection_result_is_ignored():
    dialog, app, _secret_store = _make_dialog(AppSettings(engine="deepgram"))
    dialog._connection_test_id = 2
    dialog.test_conn_result.setText("Testing...")

    dialog._on_connection_test_finished(1, True, "stale")

    assert dialog.test_conn_result.text() == "Testing..."
    _ = app


def test_test_all_configured_runs_multiple_provider_checks(monkeypatch):
    import stt_app.transcriber.deepgram_provider as deepgram_provider_module
    import stt_app.transcriber.openai_provider as openai_provider_module

    class _FakeDeepgramTranscriber:
        def __init__(
            self,
            api_key: str,
            language_mode: str = "auto",
            model: str = "nova-3",
        ) -> None:
            self._api_key = api_key
            self._language_mode = language_mode
            self._model = model

        def test_connection(self) -> tuple[bool, str]:
            return True, "Deepgram OK"

    class _FakeOpenAITranscriber:
        def __init__(
            self,
            api_key: str,
            language_mode: str = "auto",
            model: str = "gpt-4o-mini-transcribe",
        ) -> None:
            self._api_key = api_key
            self._language_mode = language_mode
            self._model = model

        def test_connection(self) -> tuple[bool, str]:
            return True, "OpenAI OK"

    monkeypatch.setattr(settings_dialog_module.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        deepgram_provider_module,
        "DeepgramTranscriber",
        _FakeDeepgramTranscriber,
    )
    monkeypatch.setattr(
        openai_provider_module,
        "OpenAITranscriber",
        _FakeOpenAITranscriber,
    )

    dialog, app, _secret_store = _make_dialog(AppSettings(engine="local"))
    dialog.openai_key_edit.setText("oa-key")
    dialog.deepgram_key_edit.setText("dg-key")
    all_index = dialog.test_conn_target_combo.findData("all-configured")
    dialog.test_conn_target_combo.setCurrentIndex(all_index)

    dialog._test_connection()

    assert dialog.test_conn_button.isEnabled() is True
    assert "provider tests passed" in dialog.test_conn_result.text()
    assert "OpenAI: OK" in dialog.test_conn_result.text()
    assert "Deepgram: OK" in dialog.test_conn_result.text()
    assert "Last test (" in dialog._provider_last_test_labels["openai"].text()
    assert "Last test (" in dialog._provider_last_test_labels["deepgram"].text()
    _ = app


def test_provider_badge_shows_insecure_storage_source():
    dialog, app, secret_store = _make_dialog(
        AppSettings(engine="local"),
        {"openai": "stored-key"},
    )
    secret_store._sources["openai"] = "insecure"
    dialog._refresh_provider_key_statuses()

    assert "insecure" in dialog._provider_status_labels["openai"].text().lower()
    _ = app


def test_save_can_clear_stored_provider_key():
    dialog, app, secret_store = _make_dialog(
        AppSettings(),
        {"openai": "stored-key"},
    )

    dialog._mark_provider_key_for_clear("openai")
    assert dialog._provider_status_labels["openai"].text() == "Will clear on Save"

    dialog._save()

    assert secret_store.delete_calls == ["openai"]
    assert secret_store.get_api_key("openai") is None
    assert dialog._provider_status_labels["openai"].text() == "Not configured"
    _ = app


def test_save_persists_only_supported_remote_keys():
    dialog, app, secret_store = _make_dialog(AppSettings())
    dialog.assemblyai_key_edit.setText("aai-key")
    dialog.groq_key_edit.setText("groq-key")
    dialog.openai_key_edit.setText("openai-key")
    dialog.deepgram_key_edit.setText("dg-key")
    dialog.elevenlabs_key_edit.setText("el-key")

    dialog._save()

    providers = [provider for provider, _value in secret_store.set_calls]
    assert providers == [
        "openai",
        "deepgram",
        "assemblyai",
        "groq",
        "elevenlabs",
    ]
    assert "azure" not in providers
    assert dialog._loaded_settings.openai_model in {
        "gpt-4o-mini-transcribe",
        "gpt-4o-transcribe",
        "whisper-1",
    }
    assert dialog._loaded_settings.deepgram_model in {"nova-3", "nova-2"}
    assert dialog._loaded_settings.assemblyai_model in {
        "best",
        "nano",
        "universal-3-pro",
        "universal",
        "slam-1",
    }
    assert dialog._loaded_settings.elevenlabs_model in {"scribe_v2", "scribe_v1"}
    _ = app
