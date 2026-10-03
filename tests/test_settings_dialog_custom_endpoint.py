"""The Custom endpoint's settings: the Providers tab's group, the editable
model row and the Refresh worker with its saved model list."""

from __future__ import annotations

import threading

from PySide6 import QtCore, QtTest, QtWidgets
from test_settings_dialog_connection import (
    _FakeLogger,
    _FakeSecretStore,
    _FakeSettingsStore,
    _ImmediateThread,
)

from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_store import AppSettings
from stt_app.transcriber import custom_endpoint_provider
from stt_app.transcriber.custom_endpoint_provider import CustomEndpointModel

_STORED = AppSettings(
    engine="custom",
    custom_endpoint="https://llm-gateway.example.com/v1",
    custom_model="whisper-1",
    custom_api_mode="chat",
    custom_key_command="token-helper --print",
)


def _dialog(settings: AppSettings = _STORED, secrets: dict[str, str] | None = None):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(settings)
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(secrets),
        app_logger=_FakeLogger(),
    )
    return dialog, store


def test_the_fields_are_populated_and_the_model_row_is_editable():
    dialog, _store = _dialog()
    try:
        assert dialog.custom_endpoint_edit.text() == _STORED.custom_endpoint
        assert dialog.custom_key_command_edit.text() == _STORED.custom_key_command
        assert dialog.custom_api_mode_combo.currentData() == "chat"
        assert dialog.remote_model_combo.isEditable()
        assert dialog.remote_model_combo.currentText() == "whisper-1"
        assert not dialog.custom_fetch_models_button.isHidden()
        # The key command alone makes the endpoint testable.
        assert dialog._provider_test_buttons["custom"].isEnabled()

        dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("openai"))
        assert not dialog.remote_model_combo.isEditable()
        assert dialog.custom_fetch_models_button.isHidden()
    finally:
        dialog.deleteLater()


def test_a_typed_model_and_the_api_keys_fields_are_saved():
    dialog, store = _dialog()
    try:
        dialog.remote_model_combo.lineEdit().clear()
        dialog.remote_model_combo.lineEdit().textEdited.emit("gemini-2.5-flash")
        dialog.custom_endpoint_edit.setText("http://localhost:8000/v1 ")
        dialog.custom_key_command_edit.setText("")
        dialog.custom_api_mode_combo.setCurrentIndex(
            dialog.custom_api_mode_combo.findData("transcriptions")
        )
        assert dialog.has_unsaved_changes()

        dialog._save()

        saved = store.saved
        assert saved is not None
        assert saved.custom_model == "gemini-2.5-flash"
        assert saved.custom_endpoint == "http://localhost:8000/v1"
        assert saved.custom_key_command == ""
        assert saved.custom_api_mode == "transcriptions"
        assert not dialog.has_unsaved_changes()
    finally:
        dialog.deleteLater()


def test_the_key_command_alone_makes_the_provider_configured():
    dialog, _store = _dialog()
    try:
        assert "custom" in dialog._providers_for_connection_target("all-configured")
        assert dialog._import_engine_credential_issue("custom") is None
        dialog.custom_key_command_edit.setText("")
        assert "custom" not in dialog._providers_for_connection_target("all-configured")
    finally:
        dialog.deleteLater()


def test_fetch_models_fills_the_combo_from_the_typed_fields(monkeypatch):
    seen: list[dict] = []

    class _FakeTranscriber:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def list_models(self):
            return [
                CustomEndpointModel("gemini-2.5-flash", "chat"),
                CustomEndpointModel("whisper-1", "audio_transcription"),
            ]

    monkeypatch.setattr(
        custom_endpoint_provider, "CustomEndpointTranscriber", _FakeTranscriber
    )
    dialog, store = _dialog(secrets={"custom": "stored-key"})
    try:
        dialog.custom_endpoint_edit.setText("http://localhost:8000/v1")
        monkeypatch.setattr(threading, "Thread", _ImmediateThread)

        dialog._fetch_custom_models()

        assert seen == [
            {
                "api_key": "stored-key",
                "endpoint": "http://localhost:8000/v1",
                "api_mode": "chat",
                "key_command": "token-helper --print",
            }
        ]
        items = [
            dialog.remote_model_combo.itemData(index)
            for index in range(dialog.remote_model_combo.count())
        ]
        assert items == ["whisper-1", "gemini-2.5-flash"]
        # The typed model is kept.
        assert dialog.remote_model_combo.currentText() == "whisper-1"
        assert "offers 2 models" in dialog.remote_model_note_label.text()
        assert dialog.custom_fetch_models_button.isEnabled()
        assert dialog._background_work_active() is False
        # The list is saved with the settings, in the endpoint's order, so
        # the combo offers it again after a restart.
        assert dialog.has_unsaved_changes()
        dialog._save()
        assert store.saved is not None
        assert store.saved.custom_model == "whisper-1"
        assert store.saved.custom_models == ("gemini-2.5-flash", "whisper-1")
        assert not dialog.has_unsaved_changes()
    finally:
        dialog.deleteLater()


def test_the_saved_model_list_is_offered_after_a_restart():
    dialog, store = _dialog(
        AppSettings(
            engine="custom",
            custom_endpoint="http://localhost:8000/v1",
            custom_model="whisper-1",
            custom_models=("whisper-1", "gemini-2.5-flash"),
        )
    )
    try:
        combo = dialog.remote_model_combo
        assert [combo.itemData(i) for i in range(combo.count())] == [
            "whisper-1",
            "gemini-2.5-flash",
        ]
        assert "2 models listed at the last Refresh" in (
            dialog.remote_model_note_label.text()
        )
        assert not dialog.has_unsaved_changes()
        dialog._save()
        assert store.saved is None, "an untouched Save rewrote the file"
    finally:
        dialog.deleteLater()


def test_a_refresh_that_changed_the_list_is_an_unsaved_change(monkeypatch):
    """Discarding it puts the saved list back; Save is what writes it."""

    class _FakeTranscriber:
        def __init__(self, **_kwargs):
            pass

        def list_models(self):
            return [CustomEndpointModel("new-model", "audio_transcription")]

    monkeypatch.setattr(
        custom_endpoint_provider, "CustomEndpointTranscriber", _FakeTranscriber
    )
    saved_list = ("whisper-1",)
    dialog, store = _dialog(
        AppSettings(
            engine="custom",
            custom_endpoint="http://x/v1",
            custom_model="whisper-1",
            custom_models=saved_list,
        ),
        secrets={"custom": "k"},
    )
    try:
        monkeypatch.setattr(threading, "Thread", _ImmediateThread)
        dialog._fetch_custom_models()
        assert dialog._custom_fetched_models == ("new-model",)
        assert dialog.has_unsaved_changes()

        dialog._discard_unsaved_edits_while_busy()

        assert dialog._custom_fetched_models == saved_list
        assert not dialog.has_unsaved_changes()
        assert store.saved is None
    finally:
        dialog.deleteLater()


def test_picking_the_current_list_entry_after_typing_replaces_the_typed_text(
    monkeypatch,
):
    """Qt only rewrites the line edit when the picked entry is the current one.

    With the index unchanged neither `textEdited` nor `currentIndexChanged`
    fires, so the field read "B" while the typed "foo" was still what Save
    wrote. Driven with real keystrokes and a real click on the popup.
    """

    class _FakeTranscriber:
        def __init__(self, **_kwargs):
            pass

        def list_models(self):
            return [
                CustomEndpointModel("A", "chat"),
                CustomEndpointModel("B", "chat"),
            ]

    monkeypatch.setattr(
        custom_endpoint_provider, "CustomEndpointTranscriber", _FakeTranscriber
    )
    app = QtWidgets.QApplication.instance()
    assert app is not None
    # The list the fetch returns is already the saved one, so the pick below
    # is the only thing that could make the dialog unsaved.
    dialog, store = _dialog(
        AppSettings(
            engine="custom",
            custom_endpoint="http://x/v1",
            custom_model="B",
            custom_models=("A", "B"),
        )
    )
    try:
        monkeypatch.setattr(threading, "Thread", _ImmediateThread)
        dialog._fetch_custom_models()
        combo = dialog.remote_model_combo
        assert [combo.itemData(i) for i in range(combo.count())] == ["B", "A"]
        dialog.show()
        for _ in range(3):
            app.processEvents()

        line_edit = combo.lineEdit()
        line_edit.selectAll()
        QtTest.QTest.keyClicks(line_edit, "foo")
        assert line_edit.text() == "foo"
        assert dialog._remote_model_values["custom"] == "foo"
        assert combo.currentIndex() == 0

        combo.showPopup()
        for _ in range(3):
            app.processEvents()
        view = combo.view()
        rect = view.visualRect(view.model().index(0, 0))
        QtTest.QTest.mouseClick(
            view.viewport(), QtCore.Qt.LeftButton, pos=rect.center()
        )
        for _ in range(3):
            app.processEvents()

        assert line_edit.text() == "B"
        # What Save would write follows the field, and "B" is what is stored.
        assert dialog._remote_model_values["custom"] == "B"
        assert not dialog.has_unsaved_changes()
        dialog._save()
        assert store.saved is None
    finally:
        dialog.close()
        dialog.deleteLater()


def test_a_failed_fetch_is_reported_in_the_note(monkeypatch):
    class _FailingTranscriber:
        def __init__(self, **_kwargs):
            raise custom_endpoint_provider.TranscriptionError("HTTP 401 refused")

    monkeypatch.setattr(
        custom_endpoint_provider, "CustomEndpointTranscriber", _FailingTranscriber
    )
    dialog, _store = _dialog()
    try:
        monkeypatch.setattr(threading, "Thread", _ImmediateThread)
        dialog._fetch_custom_models()
        assert "Could not fetch models: HTTP 401 refused" in (
            dialog.remote_model_note_label.text()
        )
        assert dialog.custom_fetch_models_button.isEnabled()
    finally:
        dialog.deleteLater()


def test_a_fetch_that_cannot_start_gives_the_button_back(monkeypatch):
    class _RefusingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    dialog, _store = _dialog()
    try:
        monkeypatch.setattr(threading, "Thread", _RefusingThread)
        dialog._fetch_custom_models()
        assert dialog._active_custom_models_fetch_thread is None
        assert dialog._background_work_active() is False
        assert dialog.custom_fetch_models_button.isEnabled()
        assert "Could not start the model fetch" in (
            dialog.remote_model_note_label.text()
        )
    finally:
        dialog.deleteLater()
