from __future__ import annotations

from PySide6 import QtWidgets

import tts_app.settings_dialog as settings_dialog_module
from tts_app.settings_dialog import SettingsDialog
from tts_app.settings_store import AppSettings


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

    def get_api_key(self, provider: str) -> str | None:
        return self._values.get(provider)

    def set_api_key(self, provider: str, api_key: str) -> None:
        self._values[provider] = api_key


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
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(settings),
        secret_store=_FakeSecretStore(secret_values),
        app_logger=_FakeLogger(),
    )
    return dialog, app


def test_engine_combo_hides_unimplemented_providers():
    dialog, app = _make_dialog(AppSettings())
    assert dialog.engine_combo.findData("openai") == -1
    assert dialog.engine_combo.findData("azure") == -1
    _ = app


def test_test_connection_missing_key_shows_error():
    dialog, app = _make_dialog(AppSettings(engine="assemblyai"))
    engine_index = dialog.engine_combo.findData("assemblyai")
    dialog.engine_combo.setCurrentIndex(engine_index)

    dialog._test_connection()

    assert "No API key entered" in dialog.test_conn_result.text()
    assert dialog.test_conn_button.isEnabled() is True
    _ = app


def test_test_connection_runs_in_background_worker(monkeypatch):
    import tts_app.transcriber.deepgram_provider as deepgram_provider_module

    class _FakeDeepgramTranscriber:
        def __init__(self, api_key: str, language_mode: str = "auto") -> None:
            self._api_key = api_key
            self._language_mode = language_mode

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

    dialog, app = _make_dialog(AppSettings(engine="deepgram"))
    engine_index = dialog.engine_combo.findData("deepgram")
    dialog.engine_combo.setCurrentIndex(engine_index)
    dialog.deepgram_key_edit.setText("dg-test-key")

    dialog._test_connection()

    assert dialog.test_conn_button.isEnabled() is True
    assert dialog.test_conn_result.text().startswith("\u2713")
    assert "Connection OK" in dialog.test_conn_result.text()
    _ = app
