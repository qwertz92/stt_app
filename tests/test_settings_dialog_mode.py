import logging
import os

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

import stt_app.settings_dialog as settings_dialog_module
from stt_app.app_paths import debug_audio_path
from stt_app.benchmark_history import (
    BenchmarkHistoryEntry,
    BenchmarkHistoryStore,
    BenchmarkOptions,
)
from stt_app.last_recording_store import LastRecordingStore
from stt_app.local_benchmark import BenchmarkCase, BenchmarkRun
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
    def __init__(self):
        self._values: dict[str, str] = {}

    def set_api_key(self, provider: str, key: str) -> None:
        self._values[provider] = key

    def get_api_key(self, provider: str) -> str | None:
        return self._values.get(provider)


class _FakeLogger:
    def diagnostics_text(self) -> str:
        return "diag"


class _FakeLocalModelInventoryStore:
    def __init__(self, values: dict[str, list[str]] | None = None):
        self.values = {
            str(model_dir).strip(): list(models)
            for model_dir, models in (values or {}).items()
        }
        self.saved: list[tuple[str, list[str]]] = []

    def load_cached_models(self, model_dir: str = "") -> list[str] | None:
        key = str(model_dir or "").strip()
        if key not in self.values:
            return None
        return list(self.values[key])

    def save_cached_models(self, model_dir: str, cached_models: list[str]) -> None:
        key = str(model_dir or "").strip()
        self.values[key] = list(cached_models)
        self.saved.append((key, list(cached_models)))

    def clear_cached_models(self, model_dir: str = "") -> None:
        self.values.pop(str(model_dir or "").strip(), None)


class _ImmediateThread:
    def __init__(self, target, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class _IdleThread:
    def __init__(self, target, name=None, daemon=None):
        self._target = target

    def start(self):
        return None


def _select_local_model_names(dialog: SettingsDialog, *model_names: str) -> None:
    selected = set(model_names)
    for index in range(dialog.local_models_list.count()):
        item = dialog.local_models_list.item(index)
        item.setSelected(str(item.data(QtCore.Qt.UserRole) or "") in selected)


@pytest.fixture(autouse=True)
def _close_top_level_windows_after_test():
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS.clear()
    yield
    app = QtWidgets.QApplication.instance()
    if app is None:
        settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
        settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS.clear()
        return
    for widget in list(app.topLevelWidgets()):
        widget.close()
        widget.deleteLater()
    app.processEvents()
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS.clear()


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


def _send_wheel_event(widget: QtWidgets.QWidget) -> None:
    center = widget.rect().center()
    event = QtGui.QWheelEvent(
        QtCore.QPointF(center),
        QtCore.QPointF(widget.mapToGlobal(center)),
        QtCore.QPoint(),
        QtCore.QPoint(0, 120),
        QtCore.Qt.NoButton,
        QtCore.Qt.NoModifier,
        QtCore.Qt.ScrollUpdate,
        False,
    )
    QtWidgets.QApplication.sendEvent(widget, event)


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


def test_remote_streaming_ignores_batch_only_local_model_selection():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(
            engine="deepgram",
            mode="streaming",
            model_size="cohere-transcribe-03-2026",
        )
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    streaming_idx = dialog.mode_combo.findData("streaming")
    item = dialog.mode_combo.model().item(streaming_idx)

    assert item is not None
    assert item.isEnabled() is True
    assert dialog.mode_combo.currentData() == "streaming"
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

    assert _combo_data(dialog.language_combo) == ["auto"]
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

    assert _combo_data(dialog.language_combo) == ["auto", "en"]
    assert dialog.language_combo.currentData() == "auto"
    assert dialog.language_combo.isEnabled() is True
    assert _combo_item_enabled(dialog.language_combo, "auto") is True
    assert _combo_item_enabled(dialog.language_combo, "de") is False
    assert _combo_item_enabled(dialog.language_combo, "en") is True
    assert "English-only model" in dialog.language_note_label.text()
    _ = app


def test_local_webgpu_model_is_batch_only_and_warns_about_cpu_fallback():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(
            engine="local",
            mode="streaming",
            model_size="cohere-transcribe-03-2026",
            language_mode="auto",
        )
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.model_combo.currentData() == "cohere-transcribe-03-2026"
    assert dialog.mode_combo.currentData() == "batch"
    assert _combo_item_enabled(dialog.mode_combo, "streaming") is False
    assert dialog.language_combo.currentData() == "de"
    assert dialog.language_combo.isEnabled() is True
    assert _combo_item_enabled(dialog.language_combo, "auto") is False
    assert _combo_item_enabled(dialog.language_combo, "de") is True
    assert _combo_item_enabled(dialog.language_combo, "en") is True
    assert _combo_item_enabled(dialog.language_combo, "fr") is True
    assert _combo_item_enabled(dialog.language_combo, "ja") is True
    assert "does not provide automatic language detection" in (
        dialog.language_note_label.text()
    )
    assert "ONNX/WebGPU" in dialog.engine_indicator.text()
    assert "DirectML" in dialog.local_model_runtime_warning_label.text()
    assert "CPU fallback" in dialog.local_model_runtime_warning_label.text()
    assert "Batch mode only" in dialog.local_model_runtime_warning_label.text()
    assert dialog.keep_onnx_model_loaded_checkbox.isChecked() is False
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
    assert _combo_data(dialog.language_combo) == ["auto"]
    assert dialog.language_combo.isEnabled() is False
    assert _combo_item_enabled(dialog.language_combo, "auto") is True
    assert _combo_item_enabled(dialog.language_combo, "de") is False
    assert _combo_item_enabled(dialog.language_combo, "en") is False

    batch_idx = dialog.mode_combo.findData("batch")
    dialog.mode_combo.setCurrentIndex(batch_idx)
    assert _combo_data(dialog.language_combo)[:3] == ["auto", "de", "en"]
    assert dialog.language_combo.isEnabled() is True
    assert _combo_item_enabled(dialog.language_combo, "auto") is True
    assert _combo_item_enabled(dialog.language_combo, "de") is True
    assert _combo_item_enabled(dialog.language_combo, "en") is True
    assert _combo_item_enabled(dialog.language_combo, "ja") is True
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

    expected = str(tmp_path / "stt_app" / "last_recording.wav")
    assert expected in dialog.save_wav_path_label.text()
    assert "always preserved until transcription finishes" in (
        dialog.save_wav_path_label.text()
    )
    _ = app


def test_remote_model_selector_tracks_selected_provider():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(engine="groq", groq_model="whisper-large-v3")
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.remote_model_combo.currentData() == "whisper-large-v3"

    deepgram_idx = dialog.engine_combo.findData("deepgram")
    dialog.engine_combo.setCurrentIndex(deepgram_idx)
    nova2_idx = dialog.remote_model_combo.findData("nova-2")
    dialog.remote_model_combo.setCurrentIndex(nova2_idx)

    dialog._save()

    assert store.saved is not None
    assert store.saved.groq_model == "whisper-large-v3"
    assert store.saved.deepgram_model == "nova-2"
    _ = app


def test_assemblyai_streaming_disables_remote_model_combo():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(
            engine="assemblyai",
            mode="streaming",
            assemblyai_model="universal-2",
        )
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.remote_model_combo.isEnabled() is False
    assert "batch transcription and imports" in dialog.remote_model_note_label.text()
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

    assert _combo_data(dialog.language_combo)[:3] == ["auto", "de", "en"]
    assert _combo_item_enabled(dialog.language_combo, "fr") is True
    assert _combo_item_enabled(dialog.language_combo, "ja") is True
    assert dialog.language_note_label.isVisibleTo(dialog) is True
    assert "recognition hint" in dialog.language_note_label.text()
    _ = app


def test_elevenlabs_remote_model_note_mentions_batch_only_app_support():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(engine="elevenlabs", mode="batch", language_mode="auto")
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.remote_model_combo.isEnabled() is True
    assert "not yet wired" in dialog.remote_model_note_label.text()
    assert "language hint" in dialog.language_note_label.text()
    _ = app


def test_remote_model_selector_is_visible_on_general_tab():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings(engine="openai"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    app.processEvents()

    assert dialog.tabs.currentWidget() is not None
    assert dialog.remote_model_combo.isVisibleTo(dialog.tabs.currentWidget()) is True
    _ = app


def test_history_list_matches_detail_font_and_compact_item_spacing():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert (
        dialog.history_list.font().pointSizeF()
        == dialog.history_detail.font().pointSizeF()
    )
    assert dialog.history_list.uniformItemSizes() is True
    assert "padding: 0px 4px" in dialog.history_list.styleSheet()
    assert dialog.history_list.sizePolicy().verticalPolicy() == (
        QtWidgets.QSizePolicy.Expanding
    )
    assert dialog.history_detail.sizePolicy().verticalPolicy() == (
        QtWidgets.QSizePolicy.Expanding
    )
    assert dialog.history_splitter.orientation() == QtCore.Qt.Vertical
    assert dialog.history_splitter.childrenCollapsible() is False
    _ = app


def test_granite_language_options_follow_selected_variant():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(
            engine="local",
            model_size="granite-speech-4.1-2b",
        )
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert _combo_data(dialog.language_combo) == [
        "auto",
        "de",
        "en",
        "fr",
        "es",
        "pt",
        "ja",
    ]

    plus_index = dialog.model_combo.findData("granite-speech-4.1-2b-plus")
    dialog.model_combo.setCurrentIndex(plus_index)

    assert _combo_item_enabled(dialog.language_combo, "auto") is True
    assert _combo_item_enabled(dialog.language_combo, "pt") is True
    assert _combo_item_enabled(dialog.language_combo, "ja") is False
    _ = app


def test_deepgram_language_options_follow_selected_model():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings(engine="deepgram", deepgram_model="nova-3"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert _combo_item_enabled(dialog.language_combo, "ar") is True
    nova_2_index = dialog.remote_model_combo.findData("nova-2")
    dialog.remote_model_combo.setCurrentIndex(nova_2_index)

    assert _combo_item_enabled(dialog.language_combo, "ar") is False
    assert _combo_item_enabled(dialog.language_combo, "fr") is True
    _ = app


def test_combo_popups_use_single_pass_uniform_list_views():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    view = dialog.engine_combo.view()
    assert isinstance(view, QtWidgets.QListView)
    assert view.layoutMode() == QtWidgets.QListView.SinglePass
    assert view.spacing() == 0
    _ = app


def test_settings_dialog_precomputes_size_before_first_show():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog._initial_dialog_size_applied is True
    assert dialog.size() == dialog._default_dialog_size
    _ = app


def test_delete_selected_cached_model_updates_feedback(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    calls = {"delete": 0}

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda _model_dir="": ["small"],
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.delete_cached_model",
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
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )

    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)
    _select_local_model_names(dialog, "small")

    dialog._delete_selected_cached_model()

    assert calls["delete"] == 1
    assert "Deleted small" in dialog.local_models_action_label.text()
    _ = app


def test_local_tab_can_download_selected_model(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cached: list[str] = []

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda _model_dir="": list(cached),
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.download_model_snapshot",
        lambda model_name, _model_dir="": cached.append(model_name) or f"/tmp/{model_name}",
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )

    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)

    _select_local_model_names(dialog, "tiny")

    dialog._download_selected_local_models()

    assert "Downloaded: tiny" in dialog.local_models_action_label.text()
    assert "tiny" in dialog.local_models_label.text()
    _ = app


def test_download_selected_is_disabled_when_selection_is_cached(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda _model_dir="": ["tiny"],
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)

    _select_local_model_names(dialog, "tiny")

    assert dialog.download_selected_models_button.isEnabled() is False
    assert dialog.delete_selected_model_button.isEnabled() is True

    _select_local_model_names(dialog, "tiny", "base")

    assert dialog.download_selected_models_button.isEnabled() is True
    assert dialog.delete_selected_model_button.isEnabled() is True
    _ = app


def test_benchmark_tab_runs_for_installed_models(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda _model_dir="": ["small"],
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )

    captured_kwargs = {}

    def _fake_run_benchmark_cases(**kwargs):
        captured_kwargs.update(kwargs)
        return [
            BenchmarkCase(
                model="small",
                device="auto",
                compute_type="int8",
                download_seconds=0.0,
                load_seconds=0.45,
                runs=[
                    BenchmarkRun(
                        run_index=1,
                        seconds=1.2,
                        audio_duration_seconds=2.0,
                        real_time_factor=0.6,
                        transcript_chars=12,
                        transcript_words=2,
                        detected_language="en",
                        language_probability=0.98,
                    )
                ],
            )
        ]

    monkeypatch.setattr(
        "stt_app.settings_dialog.run_benchmark_cases",
        _fake_run_benchmark_cases,
    )

    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._benchmark_history_store = BenchmarkHistoryStore(
        path=tmp_path / "benchmark_history.json"
    )
    dialog._refresh_benchmark_history_list()
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    QtTest.QTest.qWait(250)
    dialog._set_benchmark_audio_path(str(audio_path))

    assert dialog.benchmark_models_list.count() == 1
    assert dialog.run_benchmark_button.isEnabled() is True

    dialog._run_local_benchmark()

    assert dialog.benchmark_results_table.rowCount() == 1
    assert dialog.benchmark_results_table.item(0, 0).text() == "small"
    assert dialog.benchmark_results_table.item(0, 1).text() == "auto"
    assert captured_kwargs["webgpu_devices"] == ["auto"]
    assert dialog.benchmark_summary_text.toPlainText().startswith("Benchmark summary:")
    assert "Benchmark details:" in dialog.benchmark_summary_text.toPlainText()
    assert "System details:" in dialog.benchmark_summary_text.toPlainText()
    assert "Benchmark finished" in dialog.benchmark_status_label.text()
    assert dialog.benchmark_history_list.count() == 1
    assert dialog.export_benchmark_results_button.isEnabled() is True
    _ = app


def test_benchmark_history_double_click_loads_entry(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    case = BenchmarkCase(
        model="small",
        device="auto",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.2,
        runs=[
            BenchmarkRun(
                run_index=1,
                seconds=1.0,
                audio_duration_seconds=2.0,
                real_time_factor=0.5,
                transcript_chars=8,
                transcript_words=2,
                detected_language="en",
                language_probability=0.9,
            )
        ],
    )
    entry = BenchmarkHistoryEntry.new(
        status="completed",
        summary="Benchmark summary:\nsmall",
        options=BenchmarkOptions(
            audio_path="C:/sample.wav",
            audio_name="sample.wav",
            model_names=["small"],
            device="auto",
            compute_type="int8",
            webgpu_devices=["auto"],
            runs=1,
            beam_size=5,
            language="auto",
            vad_filter=False,
            warmup=False,
            threads=0,
        ),
        cases=[case],
    )
    benchmark_store = BenchmarkHistoryStore(path=tmp_path / "benchmark_history.json")
    benchmark_store.save([entry])

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._benchmark_history_store = benchmark_store
    dialog._refresh_benchmark_history_list()

    item = dialog.benchmark_history_list.item(0)
    dialog.benchmark_history_list.itemDoubleClicked.emit(item)

    assert dialog.benchmark_results_table.rowCount() == 1
    assert dialog.benchmark_results_table.item(0, 0).text() == "small"
    assert dialog.benchmark_summary_text.toPlainText() == entry.summary
    _ = app


def test_benchmark_audio_picker_starts_in_recordings_dir(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    recordings_path = tmp_path / "recordings"
    captured: dict[str, str] = {}

    def fake_get_open_file_name(parent, title, directory, file_filter):
        captured["directory"] = directory
        return "", ""

    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        fake_get_open_file_name,
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(
            AppSettings(recordings_dir=str(recordings_path))
        ),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    dialog._choose_benchmark_audio_file()

    assert captured["directory"] == str(recordings_path)
    assert recordings_path.is_dir()
    _ = app


def test_import_audio_picker_starts_in_recordings_dir(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    recordings_path = tmp_path / "recordings"
    captured: dict[str, str] = {}

    def fake_get_open_file_name(parent, title, directory, file_filter):
        captured["directory"] = directory
        return "", ""

    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        fake_get_open_file_name,
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(
            AppSettings(recordings_dir=str(recordings_path))
        ),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    dialog._choose_import_file()

    assert captured["directory"] == str(recordings_path)
    assert recordings_path.is_dir()
    _ = app


def test_import_audio_picker_reuses_selected_file_directory(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    selected_dir = tmp_path / "selected"
    selected_dir.mkdir()
    selected_path = selected_dir / "sample.wav"
    selected_path.write_bytes(b"RIFF")
    captured: dict[str, str] = {}

    def fake_get_open_file_name(parent, title, directory, file_filter):
        captured["directory"] = directory
        return "", ""

    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        fake_get_open_file_name,
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._set_selected_import_file(str(selected_path))

    dialog._choose_import_file()

    assert captured["directory"] == str(selected_dir)
    _ = app


def test_clear_benchmark_results_restores_initial_dialog_size():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    app.processEvents()

    initial_size = dialog.size()
    dialog.resize(initial_size.width() + 180, initial_size.height() + 140)
    dialog.benchmark_results_table.setRowCount(1)
    for column in range(dialog.benchmark_results_table.columnCount()):
        dialog.benchmark_results_table.setItem(
            0,
            column,
            QtWidgets.QTableWidgetItem(f"value-{column}"),
        )
    dialog.benchmark_summary_text.setPlainText("Benchmark summary:\nsmall")
    dialog._set_benchmark_status("Benchmark finished.", "#1b5e20")

    dialog._clear_benchmark_results()
    app.processEvents()

    assert dialog.size() == initial_size
    assert dialog.benchmark_results_table.rowCount() == 0
    assert dialog.benchmark_summary_text.toPlainText() == ""
    assert dialog.benchmark_status_label.text() == ""
    _ = app


def test_benchmark_tab_is_last():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.tabs.tabText(dialog.tabs.count() - 1) == "Benchmark"
    _ = app


def test_settings_dialog_scans_local_models_once_after_local_tab_is_selected(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    calls: list[str] = []
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda model_dir="": calls.append(model_dir) or ["small"],
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )

    store = _FakeSettingsStore(AppSettings(model_dir="/tmp/models"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert calls == []
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)
    assert calls == ["/tmp/models"]
    assert "small" in dialog.local_models_label.text()
    _ = app


def test_settings_dialog_defers_local_model_scan_until_tab_event_loop(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    calls: list[str] = []
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda model_dir="": calls.append(model_dir) or ["small"],
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )

    store = _FakeSettingsStore(AppSettings(model_dir="/tmp/models"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert calls == []
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    assert calls == []
    QtTest.QTest.qWait(250)
    assert calls == ["/tmp/models"]
    _ = app


def test_settings_dialog_uses_session_cached_models_without_rescan(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    calls: list[str] = []
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE["/tmp/models"] = ["small"]
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS.add("/tmp/models")

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda model_dir="": calls.append(model_dir) or ["small"],
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )

    store = _FakeSettingsStore(AppSettings(model_dir="/tmp/models"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert "small" in dialog.local_models_label.text()
    assert calls == []
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)
    assert calls == []
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS.clear()
    _ = app


def test_settings_dialog_uses_persistent_cache_before_auto_rescan(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    calls: list[str] = []
    inventory_store = _FakeLocalModelInventoryStore({"/tmp/models": ["tiny"]})

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda model_dir="": calls.append(model_dir) or ["small"],
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(model_dir="/tmp/models")),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
        local_model_inventory_store=inventory_store,
    )

    assert calls == []
    assert "tiny" in dialog.local_models_label.text()
    assert dialog.local_models_list.count() > 0
    assert "last known local models" in dialog.local_models_scan_status_label.text()

    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)

    assert calls == ["/tmp/models"]
    assert "small" in dialog.local_models_label.text()
    assert inventory_store.values["/tmp/models"] == ["small"]
    assert dialog.local_models_scan_status_label.text() == ""
    _ = app


def test_manual_refresh_updates_persistent_local_model_cache(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    calls: list[str] = []
    inventory_store = _FakeLocalModelInventoryStore({"/tmp/models": ["tiny"]})

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda model_dir="": calls.append(model_dir) or ["small"],
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(model_dir="/tmp/models")),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
        local_model_inventory_store=inventory_store,
    )

    assert "tiny" in dialog.local_models_label.text()
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)
    assert calls == ["/tmp/models"]
    calls.clear()

    dialog._refresh_local_model_views(force=True)

    assert calls == ["/tmp/models"]
    assert "small" in dialog.local_models_label.text()
    assert inventory_store.values["/tmp/models"] == ["small"]
    assert dialog.local_models_scan_status_label.text() == ""
    _ = app


def test_settings_dialog_treats_empty_persistent_cache_as_valid(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _IdleThread,
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(model_dir="/tmp/empty-models")),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
        local_model_inventory_store=_FakeLocalModelInventoryStore(
            {"/tmp/empty-models": []}
        ),
    )

    assert "No local models found" in dialog.local_models_label.text()
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)

    assert "Showing the last known local models" in dialog.local_models_scan_status_label.text()
    _ = app


def test_soft_local_model_refresh_keeps_lists_enabled(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE[""] = ["small"]

    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _IdleThread,
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)

    assert dialog.local_models_list.isEnabled() is True
    assert dialog.refresh_local_models_button.isEnabled() is True
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
    _ = app


def test_model_dir_change_triggers_single_rescan(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    calls: list[str] = []

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda model_dir="": calls.append(model_dir) or ["small"],
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _ImmediateThread,
    )

    store = _FakeSettingsStore(AppSettings(model_dir=""))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)
    calls.clear()

    dialog.model_dir_edit.setText("/tmp/other-models")
    QtTest.QTest.qWait(300)

    assert calls == ["/tmp/other-models"]
    _ = app


def test_benchmark_controls_explain_their_options():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert "fastest" in dialog.benchmark_compute_type_combo.toolTip()
    assert "Cohere and Granite" in dialog.benchmark_webgpu_device_combo.toolTip()
    assert "reduce noise" in dialog.benchmark_runs_spin.toolTip()
    assert "Beam size controls decoding breadth" in dialog.benchmark_beam_size_spin.toolTip()
    assert "fixed language removes one source of model guesswork" in dialog.benchmark_language_combo.toolTip()
    assert "first-run caches" in dialog.benchmark_warmup_checkbox.toolTip()
    assert "Filters silence before transcription" in dialog.benchmark_vad_checkbox.toolTip()
    _ = app


def test_closed_combo_does_not_change_selection_on_mouse_wheel():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings(engine="local"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    app.processEvents()

    combo = dialog.engine_combo
    combo.clearFocus()
    initial_index = combo.currentIndex()
    _send_wheel_event(combo)

    assert combo.currentIndex() == initial_index
    _ = app


def test_focused_combo_still_ignores_mouse_wheel_until_popup_is_open():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings(engine="local"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    app.processEvents()

    combo = dialog.engine_combo
    combo.setFocus()
    initial_index = combo.currentIndex()
    _send_wheel_event(combo)

    assert combo.currentIndex() == initial_index
    _ = app


def test_focused_spin_box_ignores_mouse_wheel():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    app.processEvents()

    spin_box = dialog.recordings_max_spin
    spin_box.setFocus()
    initial_value = spin_box.value()

    _send_wheel_event(spin_box)

    assert spin_box.value() == initial_value
    _ = app


def test_focused_double_spin_box_ignores_mouse_wheel():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    app.processEvents()

    spin_box = dialog.vad_threshold_spin
    spin_box.setFocus()
    initial_value = spin_box.value()

    _send_wheel_event(spin_box)

    assert spin_box.value() == initial_value
    _ = app


def test_remote_provider_rows_limit_key_and_badge_growth():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.assemblyai_key_edit.maximumWidth() == 16777215
    assert dialog._provider_status_labels["assemblyai"].maximumWidth() == 170
    assert dialog._provider_status_labels["assemblyai"].minimumWidth() == 148
    _ = app


def test_import_model_selector_tracks_selected_import_engine():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(
            engine="local",
            model_size="medium",
            openai_model="whisper-1",
        )
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.import_model_combo.currentData() == "medium"

    openai_idx = dialog.import_engine_combo.findData("openai")
    dialog.import_engine_combo.setCurrentIndex(openai_idx)

    assert _combo_data(dialog.import_model_combo) == [
        "gpt-4o-mini-transcribe",
        "gpt-4o-transcribe",
        "whisper-1",
    ]
    assert dialog.import_model_combo.currentData() == "whisper-1"
    _ = app


def test_local_model_lists_use_compact_item_spacing():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.local_models_list.uniformItemSizes() is True
    assert dialog.local_models_list.spacing() == 0
    assert "padding: 0px 4px" in dialog.local_models_list.styleSheet()
    assert dialog.local_models_list.sizeAdjustPolicy() == (
        QtWidgets.QAbstractScrollArea.AdjustToContents
    )
    assert dialog.benchmark_models_list.sizeAdjustPolicy() == (
        QtWidgets.QAbstractScrollArea.AdjustToContents
    )
    _ = app


def test_settings_dialog_logs_local_tab_timing(caplog, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE[""] = ["small"]

    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _IdleThread,
    )
    caplog.set_level(logging.INFO, logger="stt_app")

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    app.processEvents()
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(10)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "settings_timing event=tab_change" in message and "tab=Local" in message
        for message in messages
    )
    assert any(
        "settings_timing event=tab_paint" in message and "tab=Local" in message
        for message in messages
    )
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
    _ = app


def test_settings_dialog_show_expands_to_remote_tab_width():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    dialog.show()
    app.processEvents()
    remote_index = [
        index
        for index in range(dialog.tabs.count())
        if dialog.tabs.tabText(index) == "Remote"
    ][0]
    dialog.tabs.setCurrentIndex(remote_index)
    app.processEvents()
    remote_tab = dialog.tabs.currentWidget()

    assert dialog.width() >= settings_dialog_module._DEFAULT_SETTINGS_DIALOG_SIZE.width()
    assert remote_tab.horizontalScrollBar().maximum() == 0
    _ = app


def test_local_models_box_grows_when_dialog_is_resized(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE[""] = ["small", "medium"]

    monkeypatch.setattr(
        "stt_app.settings_dialog.threading.Thread",
        _IdleThread,
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    app.processEvents()

    initial_box_height = dialog.local_models_box.height()
    initial_list_height = dialog.local_models_list.height()

    dialog.resize(dialog.width() + 120, dialog.height() + 220)
    app.processEvents()

    assert dialog.local_models_box.height() > initial_box_height
    assert dialog.local_models_list.height() > initial_list_height
    settings_dialog_module._LOCAL_MODEL_SCAN_SESSION_CACHE.clear()
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


def test_settings_tabs_use_scroll_areas_and_scroll_buttons():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    for index in range(dialog.tabs.count()):
        widget = dialog.tabs.widget(index)
        assert isinstance(widget, QtWidgets.QScrollArea)
        assert widget.widgetResizable() is True
        assert widget.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarAsNeeded

    assert dialog.tabs.tabBar().usesScrollButtons() is True
    assert dialog.tabs.tabBar().elideMode() == QtCore.Qt.ElideRight
    _ = app


def test_history_and_import_are_separate_tabs():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    tab_labels = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
    assert "History" in tab_labels
    assert "Import Audio" in tab_labels
    assert dialog._history_tab is not dialog._import_tab
    _ = app


def test_settings_dialog_window_has_native_minimize_button():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    flags = dialog.windowFlags()
    assert bool(flags & QtCore.Qt.Window)
    assert bool(flags & QtCore.Qt.WindowSystemMenuHint)
    assert bool(flags & QtCore.Qt.WindowMinimizeButtonHint)
    assert bool(flags & QtCore.Qt.WindowMaximizeButtonHint)
    assert bool(flags & QtCore.Qt.WindowCloseButtonHint)
    _ = app


def test_settings_dialog_can_enter_maximized_state():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    dialog.showMaximized()
    app.processEvents()

    assert dialog.isMaximized() is True
    _ = app


def test_settings_dialog_applies_custom_scrollbar_stylesheet():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    stylesheet = dialog.styleSheet()
    assert "QScrollBar:vertical" in stylesheet
    assert "width: 12px" in stylesheet
    assert "QScrollBar:horizontal" in stylesheet
    _ = app


def test_general_tab_explains_paste_mode_and_clipboard_retention_separately():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert "WM_PASTE" in dialog.paste_mode_combo.toolTip()
    assert "SendInput simulates the real Ctrl+V" in dialog.paste_mode_combo.toolTip()
    assert "some modern apps ignore" in dialog.paste_mode_combo.toolTip()
    assert "SendInput behaves like pressing Ctrl+V" in (
        dialog.paste_mode_hint_label.text()
    )
    assert "WM_PASTE bypasses keyboard simulation" in (
        dialog.paste_mode_hint_label.text()
    )
    assert "previous clipboard contents are restored" in (
        dialog.keep_clipboard_checkbox.toolTip()
    )
    _ = app


def test_general_tab_local_engine_mentions_faster_whisper_and_onnx():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(engine="local")),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.engine_combo.currentText() == "Local (faster-whisper / ONNX)"
    assert "faster-whisper" in dialog.remote_model_note_label.text()
    assert "ONNX/WebGPU" in dialog.remote_model_note_label.text()
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


def test_history_import_engine_selection_applies_without_switching_main_engine():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class _Controller:
        def __init__(self):
            self.received_engine = None
            self.received_model = None

        def transcribe_audio_file(self, _path: str, settings_override=None):
            self.received_engine = getattr(settings_override, "engine", None)
            self.received_model = getattr(settings_override, "openai_model", None)
            return True, "ok"

    controller = _Controller()
    store = _FakeSettingsStore(AppSettings(engine="local"))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
        controller=controller,
    )

    openai_idx = dialog.import_engine_combo.findData("openai")
    dialog.import_engine_combo.setCurrentIndex(openai_idx)
    whisper1_idx = dialog.import_model_combo.findData("whisper-1")
    dialog.import_model_combo.setCurrentIndex(whisper1_idx)
    settings = dialog._build_current_settings(
        engine_override="openai",
        model_override="whisper-1",
    )
    dialog._transcribe_import_file("dummy.wav", settings)

    assert controller.received_engine == "openai"
    assert controller.received_model == "whisper-1"
    assert dialog.engine_combo.currentData() == "local"
    assert dialog.model_combo.currentData() == "small"
    _ = app


def test_import_start_transcribes_without_confirmation(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(settings_dialog_module.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: pytest.fail("Import should start without confirmation"),
    )

    class _Controller:
        def transcribe_audio_file(
            self,
            _path: str,
            settings_override=None,
            progress_callback=None,
        ):
            return True, "imported text"

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(engine="local")),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
        controller=_Controller(),
    )
    import_path = tmp_path / "dummy.wav"
    import_path.write_bytes(b"RIFF")
    dialog._set_selected_import_file(str(import_path))

    dialog._transcribe_selected_import_file()

    assert dialog.import_result_label.text() == "Transcription finished."
    assert dialog.import_result_text.toPlainText() == "imported text"
    _ = app


def test_import_start_rejects_missing_selected_file():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(engine="local")),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._set_selected_import_file("missing.wav")

    dialog._transcribe_selected_import_file()

    assert "no longer exists" in dialog.import_result_label.text()
    assert dialog.import_start_button.isEnabled() is False
    _ = app


def test_import_progress_callback_is_passed_to_controller(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(settings_dialog_module.threading, "Thread", _ImmediateThread)
    received_progress_callback = []

    class _Controller:
        def transcribe_audio_file(
            self,
            _path: str,
            settings_override=None,
            progress_callback=None,
        ):
            received_progress_callback.append(callable(progress_callback))
            if progress_callback is not None:
                progress_callback("Uploading audio to provider...")
            return True, "ok"

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(engine="local")),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
        controller=_Controller(),
    )
    dialog._set_selected_import_file("dummy.wav")

    dialog._start_import_transcription("dummy.wav")

    assert received_progress_callback == [True]
    assert dialog.import_result_text.toPlainText() == "ok"
    _ = app


def test_import_failure_details_are_copyable():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    detail = "AssemblyAI transcription failed: speech_model is deprecated."

    dialog._finish_import_transcription(False, detail)

    assert detail in dialog.import_result_label.text()
    assert detail in dialog.import_result_text.toPlainText()
    assert dialog.import_result_label.textInteractionFlags() & (
        QtCore.Qt.TextSelectableByMouse | QtCore.Qt.TextSelectableByKeyboard
    )
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


def test_select_last_recording_prefers_newest_archived_recording(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    archive_dir = tmp_path / "recordings"
    archive_dir.mkdir()

    store = _FakeSettingsStore(
        AppSettings(
            save_all_recordings=True,
            recordings_dir=str(archive_dir),
        )
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    managed_store = LastRecordingStore()
    managed_store.save_recording(b"RIFF-old", keep_after_success=True)
    managed_store.mark_completed()
    managed = debug_audio_path()
    archived = archive_dir / "recording_20260428_101500_000000.wav"
    archived.write_bytes(b"RIFF-new")
    os.utime(managed, (100, 100))
    os.utime(archived, (200, 200))

    dialog._select_last_recording_file()

    assert str(archived) in dialog.import_selected_file_label.text()
    _ = app


def test_prepare_last_recording_import_switches_to_import_tab(monkeypatch, tmp_path):
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

    opened = dialog.prepare_last_recording_import()

    assert opened is True
    assert dialog.tabs.currentIndex() == dialog.tabs.indexOf(dialog._import_tab)
    assert str(path) in dialog.import_selected_file_label.text()
    _ = app
