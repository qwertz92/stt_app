from PySide6 import QtWidgets

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
    def set_api_key(self, provider: str, key: str) -> None:
        _ = provider
        _ = key


class _FakeLogger:
    def diagnostics_text(self) -> str:
        return "diag"


def test_streaming_mode_is_selectable_and_persisted():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings(mode="batch"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    index = dialog.mode_combo.findData("streaming")
    assert index >= 0
    item = dialog.mode_combo.model().item(index)
    assert item is not None
    assert item.isEnabled() is True

    dialog.mode_combo.setCurrentIndex(index)
    dialog._save()

    assert store.saved is not None
    assert store.saved.mode == "streaming"
    _ = app


def test_streaming_disabled_for_non_streaming_engine():
    """Streaming mode item is disabled when engine does not support it."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings(engine="groq", mode="batch"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    streaming_idx = dialog.mode_combo.findData("streaming")
    assert streaming_idx >= 0
    item = dialog.mode_combo.model().item(streaming_idx)
    assert item is not None
    assert item.isEnabled() is False
    _ = app


def test_streaming_enabled_for_assemblyai():
    """Streaming mode item is enabled for AssemblyAI engine."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings(engine="assemblyai", mode="batch"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    streaming_idx = dialog.mode_combo.findData("streaming")
    assert streaming_idx >= 0
    item = dialog.mode_combo.model().item(streaming_idx)
    assert item is not None
    assert item.isEnabled() is True
    _ = app


def test_streaming_enabled_for_openai():
    """Streaming mode item is enabled for OpenAI engine."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings(engine="openai", mode="batch"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    streaming_idx = dialog.mode_combo.findData("streaming")
    assert streaming_idx >= 0
    item = dialog.mode_combo.model().item(streaming_idx)
    assert item is not None
    assert item.isEnabled() is True
    _ = app


def test_streaming_enabled_for_deepgram():
    """Streaming mode item is enabled for Deepgram engine."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings(engine="deepgram", mode="batch"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    streaming_idx = dialog.mode_combo.findData("streaming")
    assert streaming_idx >= 0
    item = dialog.mode_combo.model().item(streaming_idx)
    assert item is not None
    assert item.isEnabled() is True
    _ = app


def test_switching_to_non_streaming_engine_resets_mode_to_batch():
    """Changing to a non-streaming engine auto-switches mode from streaming to batch."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # Start with local + streaming (valid combo).
    store = _FakeSettingsStore(AppSettings(engine="local", mode="streaming"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    # Mode should be streaming initially.
    assert dialog.mode_combo.currentData() == "streaming"

    # Switch engine to groq (no streaming support).
    groq_idx = dialog.engine_combo.findData("groq")
    dialog.engine_combo.setCurrentIndex(groq_idx)

    # Mode should have auto-switched to batch.
    assert dialog.mode_combo.currentData() == "batch"
    _ = app
