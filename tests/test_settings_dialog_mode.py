from PySide6 import QtCore, QtWidgets

from tts_app.app_paths import debug_audio_path
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


def _combo_data(combo: QtWidgets.QComboBox) -> list[str]:
    return [str(combo.itemData(i)) for i in range(combo.count())]


def _combo_item_enabled(combo: QtWidgets.QComboBox, value: str) -> bool:
    idx = combo.findData(value)
    if idx < 0:
        return False
    item = combo.model().item(idx)
    if item is None:
        return False
    return bool(item.isEnabled())


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


def test_streaming_disabled_for_openai():
    """Streaming mode item is disabled for OpenAI engine."""
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
    assert item.isEnabled() is False
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


def test_assemblyai_streaming_locks_language_to_auto():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(engine="assemblyai", mode="streaming", language_mode="de")
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert _combo_data(dialog.language_combo) == ["auto", "de", "en"]
    assert dialog.language_combo.currentData() == "auto"
    assert dialog.language_combo.isEnabled() is False
    assert _combo_item_enabled(dialog.language_combo, "auto") is True
    assert _combo_item_enabled(dialog.language_combo, "de") is False
    assert _combo_item_enabled(dialog.language_combo, "en") is False
    assert "fixed to Auto" in dialog.language_note_label.text()
    _ = app


def test_local_distil_model_limits_language_to_auto_and_english():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(
            engine="local",
            mode="batch",
            model_size="distil-large-v3.5",
            language_mode="de",
        )
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert _combo_data(dialog.language_combo) == ["auto", "de", "en"]
    assert dialog.language_combo.currentData() == "auto"
    assert dialog.language_combo.isEnabled() is True
    assert _combo_item_enabled(dialog.language_combo, "auto") is True
    assert _combo_item_enabled(dialog.language_combo, "de") is False
    assert _combo_item_enabled(dialog.language_combo, "en") is True
    assert "English-only model" in dialog.language_note_label.text()
    _ = app


def test_switching_assemblyai_mode_updates_language_options():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(engine="assemblyai", mode="batch", language_mode="de")
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    streaming_idx = dialog.mode_combo.findData("streaming")
    dialog.mode_combo.setCurrentIndex(streaming_idx)
    assert _combo_data(dialog.language_combo) == ["auto", "de", "en"]
    assert dialog.language_combo.isEnabled() is False
    assert _combo_item_enabled(dialog.language_combo, "auto") is True
    assert _combo_item_enabled(dialog.language_combo, "de") is False
    assert _combo_item_enabled(dialog.language_combo, "en") is False

    batch_idx = dialog.mode_combo.findData("batch")
    dialog.mode_combo.setCurrentIndex(batch_idx)
    assert _combo_data(dialog.language_combo) == ["auto", "de", "en"]
    assert dialog.language_combo.isEnabled() is True
    assert _combo_item_enabled(dialog.language_combo, "auto") is True
    assert _combo_item_enabled(dialog.language_combo, "de") is True
    assert _combo_item_enabled(dialog.language_combo, "en") is True
    _ = app


def test_debug_wav_path_is_visible_in_general_tab(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    expected = str(tmp_path / "tts_app" / "last_recording.wav")
    assert expected in dialog.save_wav_path_label.text()
    assert "overwritten on each recording" in dialog.save_wav_path_label.text()
    _ = app


def test_groq_language_note_explains_auto_and_hints():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(engine="groq", mode="batch", language_mode="auto")
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert _combo_data(dialog.language_combo) == ["auto", "de", "en"]
    assert dialog.language_note_label.isVisibleTo(dialog) is True
    assert "language hint" in dialog.language_note_label.text()
    _ = app


def test_delete_selected_cached_model_updates_feedback(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    calls = {"delete": 0}

    monkeypatch.setattr(
        "tts_app.settings_dialog.find_cached_models",
        lambda _model_dir="": ["small"],
    )
    monkeypatch.setattr(
        "tts_app.settings_dialog.delete_cached_model",
        lambda _model_name, _model_dir="": calls.__setitem__(
            "delete", calls["delete"] + 1
        )
        or 1,
    )
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: QtWidgets.QMessageBox.Yes,
    )

    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.cached_models_list.setCurrentRow(0)

    dialog._delete_selected_cached_model()

    assert calls["delete"] == 1
    assert "Deleted 'small'" in dialog.local_models_action_label.text()
    _ = app


# ------------------------------------------------------------------
# Save behaviour: dialog stays open, emits settings_changed signal
# ------------------------------------------------------------------


def test_save_emits_settings_changed_signal():
    """_save() emits settings_changed and does NOT close the dialog."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    received: list[bool] = []
    dialog.settings_changed.connect(lambda: received.append(True))

    dialog._save()

    assert store.saved is not None
    assert len(received) == 1, "settings_changed signal should fire once"
    # Dialog must still be visible (not closed via accept)
    assert dialog.result() != QtWidgets.QDialog.Accepted
    _ = app


def test_save_shows_status_feedback():
    """_save() shows a status message in the save-status label."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog._save_status_label.text() == ""
    dialog._save()
    assert "saved" in dialog._save_status_label.text().lower()
    _ = app


def test_settings_dialog_has_tab_stylesheet():
    """Tab widget should have distinct styling for selected/hover tabs."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    stylesheet = dialog.tabs.styleSheet()
    assert "QTabBar::tab:selected" in stylesheet
    assert "QTabBar::tab:hover" in stylesheet
    _ = app


def test_history_size_allows_unlimited_zero_and_persists():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    dialog.history_max_spin.setValue(0)
    dialog._save()

    assert store.saved is not None
    assert store.saved.history_max_items == 0
    _ = app


def test_select_last_recording_sets_selected_file(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))

    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    path = debug_audio_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF")

    dialog._select_last_recording_file()

    assert str(path) in dialog.import_selected_file_label.text()
    assert dialog.import_start_button.isEnabled() is True
    _ = app
