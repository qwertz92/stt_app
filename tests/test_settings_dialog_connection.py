from __future__ import annotations

import ast
import dataclasses
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6 import QtWidgets

import stt_app.settings_dialog as settings_dialog_module
from stt_app.provider_connection_test_store import ProviderConnectionTestStore
from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_dialog_persistence import _PersistenceMixin
from stt_app.settings_store import AppSettings


class _FakeSettingsStore:
    def __init__(self, settings: AppSettings):
        self._settings = settings
        self.saved: AppSettings | None = None

    def load(self) -> AppSettings:
        return self._settings

    def save(self, settings: AppSettings) -> None:
        self.saved = settings
        # A real store's `load` returns what `save` last wrote, and the save
        # path reads the file back to merge the dialog's edits onto it. A fake
        # that kept answering with the constructor's object made a second save
        # in one test see disk as if the first had never happened.
        self._settings = settings


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


def _make_dialog(
    settings: AppSettings,
    secret_values: dict[str, str] | None = None,
    connection_test_store: ProviderConnectionTestStore | None = None,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    secret_store = _FakeSecretStore(secret_values)
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(settings),
        secret_store=secret_store,
        app_logger=_FakeLogger(),
        provider_connection_test_store=connection_test_store,
    )
    return dialog, app, secret_store


def test_engine_combo_lists_implemented_providers():
    dialog, app, _secret_store = _make_dialog(AppSettings())
    assert dialog.engine_combo.findData("openai") >= 0
    assert dialog.engine_combo.findData("elevenlabs") >= 0
    assert dialog.engine_combo.findData("azure") >= 0
    # Engines not wired up must stay hidden from the selector.
    assert dialog.engine_combo.findData("not-a-real-engine") == -1
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
    model_index = dialog.remote_model_combo.findData("scribe_v2")
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
    assert "padding: 0 0 6px 0" in (
        dialog._provider_last_test_labels["openai"].styleSheet()
    )
    _ = app


def test_provider_connection_test_result_persists_between_dialogs(tmp_path):
    store_path = tmp_path / "provider_connection_tests.json"
    first_store = ProviderConnectionTestStore(store_path)
    dialog, app, _secret_store = _make_dialog(
        AppSettings(engine="local"),
        connection_test_store=first_store,
    )

    dialog._remember_provider_connection_test(
        "openai",
        ok=True,
        message="OpenAI OK",
        timestamp="2026-06-19 17:30:00",
    )

    reopened = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(engine="local")),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
        provider_connection_test_store=ProviderConnectionTestStore(store_path),
    )

    assert (
        reopened._provider_last_test_labels["openai"].text()
    ) == "Last test (2026-06-19 17:30:00): \u2713 OpenAI OK"
    assert "color: #1b5e20" in reopened._provider_last_test_labels[
        "openai"
    ].styleSheet()
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


def test_save_can_clear_stored_provider_key(tmp_path):
    connection_store = ProviderConnectionTestStore(
        tmp_path / "provider_connection_tests.json"
    )
    dialog, app, secret_store = _make_dialog(
        AppSettings(),
        {"openai": "stored-key"},
        connection_test_store=connection_store,
    )
    connection_store.save_result(
        "openai",
        ok=True,
        message="OpenAI OK",
        checked_at="2026-06-19 17:30:00",
    )
    dialog._restore_provider_connection_test_labels()
    assert "OpenAI OK" in dialog._provider_last_test_labels["openai"].text()

    dialog._mark_provider_key_for_clear("openai")
    assert dialog._provider_status_labels["openai"].text() == "Will clear on Save"

    dialog._save()

    assert secret_store.delete_calls == ["openai"]
    assert secret_store.get_api_key("openai") is None
    assert dialog._provider_status_labels["openai"].text() == "Not configured"
    assert dialog._provider_last_test_labels["openai"].text() == "Last test: never."
    assert connection_store.load_all() == {}
    _ = app


def test_save_new_provider_key_clears_previous_connection_test(tmp_path):
    connection_store = ProviderConnectionTestStore(
        tmp_path / "provider_connection_tests.json"
    )
    dialog, app, secret_store = _make_dialog(
        AppSettings(),
        {"openai": "old-key"},
        connection_test_store=connection_store,
    )
    connection_store.save_result(
        "openai",
        ok=True,
        message="OpenAI OK",
        checked_at="2026-06-19 17:30:00",
    )
    dialog._restore_provider_connection_test_labels()

    dialog.openai_key_edit.setText("new-key")
    dialog._save()

    assert secret_store.set_calls == [("openai", "new-key")]
    assert dialog._provider_last_test_labels["openai"].text() == "Last test: never."
    assert connection_store.load_all() == {}
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
        "assemblyai",
        "groq",
        "openai",
        "deepgram",
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
        "universal-3-5-pro",
        "universal-2",
    }
    assert dialog._loaded_settings.elevenlabs_model == "scribe_v2"
    _ = app


def test_save_api_keys_only_emits_settings_changed_for_effective_key_change():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    secret_store = _FakeSecretStore()
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=secret_store,
        app_logger=_FakeLogger(),
    )
    changed = []
    dialog.settings_changed.connect(lambda: changed.append(True))

    dialog.openai_key_edit.setText("openai-key")
    dialog._save_api_keys_only()

    assert secret_store.set_calls == [("openai", "openai-key")]
    assert store.saved is not None
    assert store.saved.has_openai_key is True
    assert dialog.openai_key_edit.text() == ""
    assert changed == [True]
    _ = app


def test_saving_an_api_key_reports_the_credential_change_separately():
    """A replaced key is invisible in AppSettings, so it needs its own signal.

    ``has_*_key`` only flips when a key is added or removed. Overwriting one
    with a different value leaves the saved settings identical, and the
    controller would keep a transcriber built with the previous credential.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings(has_openai_key=True))
    secret_store = _FakeSecretStore()
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=secret_store,
        app_logger=_FakeLogger(),
    )
    order: list[str] = []
    reported: list[list[str]] = []
    dialog.provider_keys_changed.connect(lambda p: order.append("keys"))
    dialog.provider_keys_changed.connect(reported.append)
    dialog.settings_changed.connect(lambda: order.append("settings"))

    dialog.openai_key_edit.setText("replacement-key")
    dialog._save_api_keys_only()

    assert secret_store.set_calls == [("openai", "replacement-key")]
    # The credential signal must come first: the controller has to drop the
    # stale transcriber before it decides whether a preload is needed.
    assert order == ["keys", "settings"]
    # The controller only rebuilds a runtime that uses the changed key, so the
    # provider name has to travel with the signal.
    assert reported == [["openai"]]
    _ = app


def test_saving_settings_without_a_key_change_reports_no_credential_change():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    secret_store = _FakeSecretStore()
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=secret_store,
        app_logger=_FakeLogger(),
    )
    keys_changed: list[bool] = []
    dialog.provider_keys_changed.connect(lambda p: keys_changed.append(p))

    dialog._save_api_keys_only()

    assert secret_store.set_calls == []
    assert keys_changed == []
    _ = app


def test_settings_dialog_uses_app_window_icon():
    dialog, app, _secret_store = _make_dialog(AppSettings())

    assert dialog.windowIcon().isNull() is False
    _ = app


@pytest.mark.parametrize(
    ("label", "field", "stored", "default"),
    [
        ("the Pinned button", "overlay_always_on_top", False, True),
        ("the opacity slider", "overlay_opacity_percent", 40, 100),
    ],
)
def test_saving_settings_keeps_what_the_overlay_owns(label, field, stored, default):
    """These two have no widget here; the overlay writes them to the store.

    A save rebuilds `AppSettings` from widget state, so a field not read back
    reverts to its dataclass default. `overlay_always_on_top` defaults to True,
    so unpinning the overlay and then saving anything at all in Settings --
    a hotkey, a tone, the model -- silently re-pinned it, on every save.
    """
    settings = replace(AppSettings(), **{field: stored})
    assert getattr(settings, field) != default, (
        "the fixture must differ from the default or this proves nothing"
    )
    dialog, app, _secret_store = _make_dialog(settings)
    try:
        built = dialog._construct_settings_from_widgets()

        assert getattr(built, field) == stored, (
            f"{label}: a save reverted it to {getattr(built, field)}"
        )
    finally:
        dialog.deleteLater()
    _ = app


@pytest.mark.parametrize(
    ("label", "field", "changed"),
    [
        ("the Pinned button", "overlay_always_on_top", False),
        ("the opacity slider", "overlay_opacity_percent", 40),
    ],
)
def test_saving_api_keys_keeps_what_the_overlay_owns(label, field, changed):
    """The key-save path writes the snapshot taken when the dialog opened.

    So an overlay change made while Settings was open -- which is exactly when
    someone reaches for the Pinned button, to see the dialog -- was written
    back to its old value by pressing Save API Keys.
    """
    store = _FakeSettingsStore(AppSettings())
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    try:
        # The overlay writes straight to the store while the dialog is open.
        store._settings = replace(AppSettings(), **{field: changed})
        dialog.openai_key_edit.setText("sk-something")

        dialog._save_api_keys_only()

        assert store.saved is not None, "the key save wrote nothing"
        assert getattr(store.saved, field) == changed, (
            f"{label}: the key save reverted it to {getattr(store.saved, field)}"
        )
    finally:
        dialog.deleteLater()
    _ = app


def test_every_settings_field_is_either_built_or_deliberately_exempt():
    """The structural guard: a new field must not be able to slip through.

    `overlay_always_on_top` was missing from a 54-keyword constructor call and
    nothing noticed, because the only symptom is a default quietly winning.
    This lists what may be absent and why, so adding a field forces a choice.
    """
    source = (
        Path(settings_dialog_module.__file__).parent
        / "settings_dialog_persistence.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    passed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "AppSettings":
            passed.update(kw.arg for kw in node.keywords if kw.arg)

    # Read back from the store instead of from a widget, because the overlay
    # owns them; `schema_version` is stamped by `AppSettings` itself.
    exempt = set(_PersistenceMixin._OVERLAY_OWNED_FIELDS) | {"schema_version"}
    names = {field.name for field in dataclasses.fields(AppSettings)}

    assert names - passed - exempt == set(), (
        "these settings fields are neither built from a widget nor exempt: "
        f"{sorted(names - passed - exempt)}"
    )
    assert exempt <= names, f"the exemption list names no such field: {exempt - names}"


@pytest.mark.parametrize(
    ("label", "typed", "expected"),
    [
        ("left empty", "", ""),
        ("only whitespace", "   ", ""),
        ("an explicit folder", "D:/my-recordings", "D:/my-recordings"),
    ],
)
def test_an_empty_recordings_folder_stays_empty_when_saved(label, typed, expected):
    """Empty means "follow the data folder", and saving must not spell it out.

    The save path built the setting from `_effective_recordings_dir`, which
    expands blank to the resolved default so a folder can be opened. Stored,
    that pins an absolute path: it stops tracking the data folder the way the
    field's own placeholder promises, and the state is one-way -- clearing the
    field and saving writes the same absolute path back, so the documented
    default becomes unreachable through the UI.
    """
    dialog, app, _secret_store = _make_dialog(AppSettings(recordings_dir="D:/old"))
    try:
        dialog.recordings_dir_edit.setText(typed)

        built = dialog._construct_settings_from_widgets()

        assert built.recordings_dir == expected, label
    finally:
        dialog.deleteLater()
    _ = app


def test_a_failed_connection_test_moves_nothing_on_the_remote_tab():
    """Every provider row carries a word-wrapped "Last test" label, and a
    failure message is two lines where "Last test: never." is one. Only the
    first line was reserved, so each failing provider pushed everything under
    it -- including the Run Connection Test button at the bottom of the same
    grid -- 15 px down: measured at 105 px with all seven failing, with the
    pointer still on the button that had just caused it.
    """
    dialog, app, _secret_store = _make_dialog(AppSettings())
    dialog.resize(900, 880)
    dialog.show()
    for index in range(dialog.tabs.count()):
        if dialog.tabs.tabText(index) == "Remote":
            dialog.tabs.setCurrentIndex(index)
            break
    else:  # pragma: no cover - the tab is always built
        raise AssertionError("Remote tab not found")

    tab = dialog.tabs.currentWidget()
    watched = [
        widget
        for widget in tab.findChildren(QtWidgets.QWidget)
        if widget.isVisible() and widget.width() > 4 and widget.height() > 4
    ]
    assert dialog.test_conn_button in watched

    def geometry():
        for _ in range(10):
            app.processEvents()
        return {
            id(widget): (
                widget.mapTo(dialog, widget.rect().topLeft()).y(),
                widget.height(),
            )
            for widget in watched
        }

    before = geometry()
    assert geometry() == before, "the tab had not settled before the measurement"

    message = (
        "Failed: HTTP 401: the API key was rejected by the provider. Check that "
        "the key belongs to an active account with transcription enabled."
    )
    for provider in dialog._provider_last_test_labels:
        dialog._provider_test_history[provider] = (
            False,
            message,
            "2026-08-30 12:00:00",
        )
        dialog._apply_provider_connection_test_label(provider)

    assert geometry() == before, "a failed connection test moved the Remote tab"

    label = dialog._provider_last_test_labels["assemblyai"]
    assert message in label.toolTip(), (
        "the reserved area holds two lines, so the full message has to stay "
        "readable on hover"
    )
    dialog.hide()


_LONG_BENCHMARK_FAILURE = (
    "The benchmark stopped early: granite-speech-4.1-2b failed to load because "
    "the ONNX Runtime session could not be created on this machine. "
    "Cases already measured were saved."
)


def _benchmark_dialog():
    dialog, app, _secret_store = _make_dialog(AppSettings())
    dialog.resize(900, 880)
    dialog.show()
    for index in range(dialog.tabs.count()):
        if dialog.tabs.tabText(index) == "Benchmark":
            dialog.tabs.setCurrentIndex(index)
            break
    else:  # pragma: no cover - the tab is always built
        raise AssertionError("Benchmark tab not found")
    for _ in range(15):
        app.processEvents()
    return dialog, app


def test_a_benchmark_failure_does_not_widen_the_settings_dialog():
    """The tab's status label shares a fixed-height row with a button, so it
    cannot wrap -- but a plain QLabel then reports the full text width as its
    minimum, and a failure message pushed the dialog's own minimum width from
    492 px to 1109 px. It also showed only the leading 77% of that message with
    no ellipsis and no tooltip, so the reason was both invisible and unsayable.
    """
    dialog, app = _benchmark_dialog()
    label = dialog.benchmark_status_label
    minimum_before = dialog.minimumSizeHint().width()

    dialog._set_benchmark_status(_LONG_BENCHMARK_FAILURE, "#b71c1c")
    for _ in range(15):
        app.processEvents()

    assert dialog.minimumSizeHint().width() == minimum_before
    assert label.text() == _LONG_BENCHMARK_FAILURE, "text() must report what was set"
    assert label.toolTip() == _LONG_BENCHMARK_FAILURE
    shown = QtWidgets.QLabel.text(label)
    assert shown.endswith("\u2026"), (
        f"the truncation has to be visible, got {shown!r}"
    )
    dialog.hide()


def test_the_benchmark_status_re_elides_when_the_dialog_is_resized():
    """A width-dependent shortening that is computed once is wrong at every
    other width."""
    dialog, app = _benchmark_dialog()
    label = dialog.benchmark_status_label
    dialog._set_benchmark_status(_LONG_BENCHMARK_FAILURE, "#b71c1c")

    dialog.resize(600, 880)
    for _ in range(15):
        app.processEvents()
    narrow = QtWidgets.QLabel.text(label)

    dialog.resize(1400, 880)
    for _ in range(15):
        app.processEvents()
    wide = QtWidgets.QLabel.text(label)

    assert len(narrow) < len(wide)
    assert wide == _LONG_BENCHMARK_FAILURE, "a label wide enough must show it all"
    dialog.hide()


def test_a_long_benchmark_status_does_not_move_the_run_and_cancel_buttons():
    """The pop-out window's status label sits under the scroll area that holds
    Run/Cancel, so every wrapped line took 16 px off that viewport and lifted
    both buttons by 16 px -- while the run they belong to reported its failure.
    """
    dialog, app = _benchmark_dialog()
    window = dialog.benchmark_window
    window.resize(860, 880)
    window.show()
    for _ in range(20):
        app.processEvents()

    watched = {
        "run": dialog.run_benchmark_button,
        "cancel": dialog.cancel_benchmark_button,
        "models": dialog.benchmark_models_list,
        "status": dialog.benchmark_window_status_label,
    }

    def geometry():
        for _ in range(10):
            app.processEvents()
        return {
            name: (widget.mapTo(window, widget.rect().topLeft()).y(), widget.height())
            for name, widget in watched.items()
        }

    before = geometry()
    assert geometry() == before, "the window had not settled before the measurement"

    dialog._set_benchmark_status(_LONG_BENCHMARK_FAILURE, "#b26a00")

    assert geometry() == before, "a long status moved the Run Benchmark window"
    assert dialog.benchmark_window_status_label.toolTip() == _LONG_BENCHMARK_FAILURE
    window.hide()
    dialog.hide()
