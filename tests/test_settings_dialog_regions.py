"""Speechmatics, Mistral and the data-residency regions in the Settings dialog.

The backends came first, with no widget for any of their settings. A Save
rebuilds `AppSettings` from the widgets, so a field without one fell back to
its dataclass default and an untouched Save wrote that default over the
stored value -- a hand-set `"deepgram_region": "eu"` came back `us`.
"""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtWidgets

import stt_app.settings_dialog_remote as remote_module
from stt_app.config import SPEECHMATICS_MODELS
from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_store import AppSettings, SettingsStore


class _SecretStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get_api_key(self, provider: str) -> str | None:
        return self._values.get(provider)

    def get_api_key_source(self, provider: str) -> str:
        return "keyring" if self._values.get(provider) else "none"

    def set_api_key(self, provider: str, api_key: str) -> None:
        self._values[provider] = api_key

    def delete_api_key(self, provider: str) -> None:
        self._values.pop(provider, None)


class _Logger:
    def diagnostics_text(self) -> str:
        return ""


def _dialog(
    tmp_path, settings: AppSettings, secrets: dict[str, str] | None = None
) -> tuple[SettingsDialog, SettingsStore, QtWidgets.QApplication]:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    store.save(settings)
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_SecretStore(secrets),
        app_logger=_Logger(),
    )
    return dialog, store, app


@pytest.mark.parametrize(
    ("field", "stored"),
    [
        ("deepgram_region", "eu"),
        ("assemblyai_region", "eu"),
        ("speechmatics_region", "us1"),
        ("speechmatics_model", "enhanced"),
    ],
)
def test_an_unchanged_save_keeps_a_stored_provider_setting(tmp_path, field, stored):
    assert getattr(AppSettings(), field) != stored, "the fixture must differ"
    dialog, store, app = _dialog(tmp_path, AppSettings(**{field: stored}))
    try:
        dialog._save()

        assert getattr(store.load(), field) == stored, (
            f"an untouched Save wrote {field} back to its default"
        )
        assert dialog._save_status_label.text() == "No settings changes"
    finally:
        dialog.deleteLater()
    _ = app


def test_a_save_records_the_keys_of_the_new_providers(tmp_path):
    dialog, store, app = _dialog(
        tmp_path,
        AppSettings(),
        secrets={"speechmatics": "sm-key", "mistral": "mi-key"},
    )
    try:
        dialog.completion_beep_checkbox.setChecked(
            not dialog.completion_beep_checkbox.isChecked()
        )
        dialog._save()

        saved = store.load()
        assert saved.has_speechmatics_key is True
        assert saved.has_mistral_key is True
    finally:
        dialog.deleteLater()
    _ = app


def test_the_new_engines_have_a_label_models_and_a_key_row(tmp_path):
    dialog, _store, app = _dialog(tmp_path, AppSettings())
    try:
        for engine, label in (
            ("speechmatics", "Speechmatics"),
            ("mistral", "Mistral (Voxtral)"),
        ):
            index = dialog.engine_combo.findData(engine)
            assert index >= 0, engine
            assert dialog.engine_combo.itemText(index) == label
            assert engine in dialog._provider_key_edits
            assert dialog.test_conn_target_combo.findData(engine) >= 0

        dialog.engine_combo.setCurrentIndex(
            dialog.engine_combo.findData("speechmatics")
        )
        offered = [
            dialog.remote_model_combo.itemData(i)
            for i in range(dialog.remote_model_combo.count())
        ]
        assert offered == list(SPEECHMATICS_MODELS)
        assert dialog.remote_model_combo.isEnabled()
    finally:
        dialog.deleteLater()
    _ = app


def test_a_picked_speechmatics_model_and_region_are_saved(tmp_path):
    dialog, store, app = _dialog(tmp_path, AppSettings(engine="speechmatics"))
    try:
        dialog.remote_model_combo.setCurrentIndex(
            dialog.remote_model_combo.findData("standard")
        )
        dialog._on_remote_model_activated(dialog.remote_model_combo.currentIndex())
        combo = dialog._provider_region_combos["speechmatics"]
        combo.setCurrentIndex(combo.findData("au1"))
        deepgram = dialog._provider_region_combos["deepgram"]
        deepgram.setCurrentIndex(deepgram.findData("eu"))

        dialog._save()

        saved = store.load()
        assert saved.speechmatics_model == "standard"
        assert saved.speechmatics_region == "au1"
        assert saved.deepgram_region == "eu"
    finally:
        dialog.deleteLater()
    _ = app


def test_each_region_choice_says_what_it_guarantees(tmp_path):
    """The default AssemblyAI streaming host routes to the US or the EU, so
    it is not labelled US; Deepgram's default endpoint is "global"."""
    dialog, _store, app = _dialog(tmp_path, AppSettings())
    try:

        def choices(provider: str) -> list[tuple[str, str, str]]:
            combo = dialog._provider_region_combos[provider]
            return [
                (
                    combo.itemData(i),
                    combo.itemText(i),
                    str(combo.itemData(i, QtCore.Qt.ToolTipRole) or ""),
                )
                for i in range(combo.count())
            ]

        assemblyai = choices("assemblyai")
        assert [value for value, _label, _tip in assemblyai] == ["auto", "us", "eu"]
        assert assemblyai[0][1].startswith("Automatic")
        assert "US or EU" in assemblyai[0][2]
        assert assemblyai[1][1] == "US only" and assemblyai[2][1] == "EU only"
        deepgram = choices("deepgram")
        assert [value for value, _label, _tip in deepgram] == ["global", "eu"]
        assert deepgram[0][1].startswith("Global")
        assert all(tip for _value, _label, tip in [*assemblyai, *deepgram])
        assert all(tip for _value, _label, tip in choices("speechmatics"))
    finally:
        dialog.deleteLater()
    _ = app


def test_a_region_picked_and_saved_through_save_api_keys_is_kept(tmp_path):
    """The region sits on the API Keys tab, so its own Save button must
    write it too, not only the dialog's Save."""
    dialog, store, app = _dialog(tmp_path, AppSettings())
    try:
        combo = dialog._provider_region_combos["assemblyai"]
        combo.setCurrentIndex(combo.findData("eu"))

        dialog._save_api_keys_only()

        assert store.load().assemblyai_region == "eu"
    finally:
        dialog.deleteLater()
    _ = app


@pytest.mark.parametrize(
    ("provider", "region"),
    [("assemblyai", "eu"), ("deepgram", "eu"), ("speechmatics", "us1")],
)
def test_the_connection_test_asks_the_region_the_dictation_uses(
    tmp_path, monkeypatch, provider, region
):
    received: dict[str, object] = {}

    class _Tester:
        def test_connection(self) -> tuple[bool, str]:
            return True, "ok"

    def _factory(**kwargs: object) -> object:
        received.update(kwargs)
        return _Tester()

    _real_factory, extras = remote_module._CONNECTION_TESTER_FACTORIES[provider]
    monkeypatch.setitem(
        remote_module._CONNECTION_TESTER_FACTORIES, provider, (_factory, extras)
    )
    dialog, _store, app = _dialog(tmp_path, AppSettings(), secrets={provider: "k"})
    try:
        combo = dialog._provider_region_combos[provider]
        combo.setCurrentIndex(combo.findData(region))
        snapshot = dialog._connection_test_snapshot(
            provider, dialog._provider_key_edits[provider]
        )

        tester, error = remote_module._build_connection_tester(provider, snapshot)

        assert error is None and tester is not None
        assert received.get("region") == region
    finally:
        dialog.deleteLater()
    _ = app


def test_mistral_connection_test_builds_the_mistral_transcriber(tmp_path):
    dialog, _store, app = _dialog(tmp_path, AppSettings(), secrets={"mistral": "k"})
    try:
        snapshot = dialog._connection_test_snapshot(
            "mistral", dialog._provider_key_edits["mistral"]
        )

        tester, error = remote_module._build_connection_tester("mistral", snapshot)

        assert error is None
        assert type(tester.__self__).__name__ == "MistralTranscriber"
    finally:
        dialog.deleteLater()
    _ = app


def test_picking_a_region_moves_nothing_on_the_api_keys_tab(tmp_path):
    """Each region selector is always present and enabled, whatever the engine
    or the pick, so a pick cannot move the rows below it."""
    dialog, _store, app = _dialog(tmp_path, AppSettings())
    try:
        dialog.show()
        dialog.tabs.setCurrentIndex(_api_keys_tab_index(dialog))
        app.processEvents()

        def _below() -> tuple[int, int]:
            button = dialog.test_conn_button
            return (
                button.mapTo(dialog, button.rect().topLeft()).y(),
                dialog.save_api_keys_button.mapTo(
                    dialog, dialog.save_api_keys_button.rect().topLeft()
                ).y(),
            )

        before = _below()
        for provider, combo in dialog._provider_region_combos.items():
            for index in range(combo.count()):
                combo.setCurrentIndex(index)
                app.processEvents()
                assert combo.isVisible() and combo.isEnabled(), provider
                assert _below() == before, (provider, combo.itemData(index))
    finally:
        dialog.hide()
        dialog.deleteLater()
    _ = app


def _api_keys_tab_index(dialog: SettingsDialog) -> int:
    for index in range(dialog.tabs.count()):
        if dialog.tabs.tabText(index) == "API Keys":
            return index
    raise AssertionError("no API Keys tab")


def test_the_vocabulary_note_follows_the_speechmatics_model(tmp_path):
    """Melia 1 has no custom dictionary; Enhanced and Standard do. The note
    read the *local* model combo for every engine, so a remote engine whose
    models differ was described by a model it does not run."""
    dialog, _store, app = _dialog(tmp_path, AppSettings(engine="speechmatics"))
    try:

        def pick(model: str) -> str:
            dialog.remote_model_combo.setCurrentIndex(
                dialog.remote_model_combo.findData(model)
            )
            dialog._on_remote_model_activated(dialog.remote_model_combo.currentIndex())
            return dialog.vocabulary_support_label.text()

        assert "ignores the custom vocabulary" in pick("melia-1")
        enhanced = pick("enhanced")
        assert enhanced.startswith("Speechmatics uses the custom vocabulary")
        assert "ignores" not in enhanced
    finally:
        dialog.deleteLater()
    _ = app


def test_save_api_keys_records_the_keys_of_the_new_providers(tmp_path):
    dialog, store, app = _dialog(
        tmp_path, AppSettings(), secrets={"speechmatics": "sm-key"}
    )
    try:
        dialog._provider_key_edits["mistral"].setText("mi-key")

        dialog._save_api_keys_only()

        saved = store.load()
        assert saved.has_speechmatics_key is True
        assert saved.has_mistral_key is True
    finally:
        dialog.deleteLater()
    _ = app
