import json
import logging
import os
import threading
import time
from pathlib import Path

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
from stt_app.transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore
from stt_app.update_checker import UpdateCheckResult


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


class _FakeClipboard:
    def __init__(self) -> None:
        self.value = ""

    def setText(self, text: str) -> None:
        self.value = text

    def text(self) -> str:
        return self.value


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


class _CompletedDownloadProcess:
    returncode = 0

    def poll(self):
        return self.returncode

    def communicate(self):
        return "", ""


class _BlockingDownloadProcess:
    def __init__(self, release_event: threading.Event):
        self._release_event = release_event
        self.returncode: int | None = None
        self.terminated = False

    def poll(self):
        if self.returncode is None and self._release_event.is_set():
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._release_event.set()

    def wait(self, timeout=None):
        self._release_event.wait(timeout=timeout)
        return self.returncode

    def kill(self):
        self.terminate()

    def communicate(self):
        return "", ""


def _select_local_model_names(dialog: SettingsDialog, *model_names: str) -> None:
    selected = set(model_names)
    for index in range(dialog.local_models_list.count()):
        item = dialog.local_models_list.item(index)
        item.setSelected(str(item.data(QtCore.Qt.UserRole) or "") in selected)


def _wait_for_local_models(
    dialog: SettingsDialog,
    *model_names: str,
    cached: bool | None = None,
    timeout_ms: int = 3000,
) -> None:
    """Wait until the deferred inventory render lists the given models.

    With ``cached`` set, also wait until the items carry that cached flag,
    which only the verified inventory scan provides.
    """
    expected = set(model_names)
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        states: dict[str, bool] = {}
        for index in range(dialog.local_models_list.count()):
            item = dialog.local_models_list.item(index)
            if item is None:
                continue
            name = str(item.data(QtCore.Qt.UserRole) or "")
            states[name] = bool(item.data(QtCore.Qt.UserRole + 1))
        if expected.issubset(states) and (
            cached is None or all(states[name] == cached for name in expected)
        ):
            return
        QtTest.QTest.qWait(25)
    raise AssertionError(f"Local models {sorted(expected)} were not rendered in time.")


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


def _combo_text_for_data(combo: QtWidgets.QComboBox, value: str) -> str:
    idx = combo.findData(value)
    assert idx >= 0
    return combo.itemText(idx)


def _history_entry(text: str) -> TranscriptHistoryEntry:
    return TranscriptHistoryEntry.new(
        text=text,
        engine="local",
        model="small",
        mode="batch",
    )


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
    assert "falls back to CPU" in dialog.local_model_runtime_warning_label.text()
    assert "Batch mode only" in dialog.local_model_runtime_warning_label.text()
    assert dialog.keep_onnx_model_loaded_checkbox.isChecked() is False
    _ = app


def test_local_model_labels_show_onnx_precision_tags():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert "[Q4]" in _combo_text_for_data(
        dialog.model_combo,
        "cohere-transcribe-03-2026",
    )
    assert "[Q4]" in _combo_text_for_data(
        dialog.model_combo,
        "granite-speech-4.1-2b",
    )
    assert "[INT8]" in _combo_text_for_data(
        dialog.model_combo,
        "granite-speech-4.1-2b-plus",
    )
    assert "[INT8]" in _combo_text_for_data(
        dialog.model_combo,
        "granite-speech-4.1-2b-nar",
    )
    assert "[INT4]" in _combo_text_for_data(
        dialog.model_combo,
        "nemotron-3.5-asr-streaming-0.6b-int4",
    )
    _ = app


def test_local_model_runtime_note_is_short_and_attached_to_model_choice():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    model_index = dialog.model_combo.findData("granite-speech-4.1-2b-plus")
    assert model_index >= 0
    dialog.model_combo.setCurrentIndex(model_index)

    note_layout = dialog.local_model_runtime_warning_label.parentWidget().layout()
    assert note_layout is not None
    assert note_layout.spacing() == 2
    assert dialog.local_model_runtime_warning_label.isHidden() is False
    warning_text = dialog.local_model_runtime_warning_label.text()
    assert "Batch mode only" in warning_text
    assert "DirectML" in warning_text
    assert "Granite 4.1" not in warning_text
    assert "raw ONNX" not in warning_text
    assert len(warning_text) < 180
    _ = app


def test_nemotron_model_enables_true_streaming_and_directml_fallback_note():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(
        AppSettings(
            engine="local",
            mode="streaming",
            model_size="nemotron-3.5-asr-streaming-0.6b-int4",
            language_mode="auto",
        )
    )
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.mode_combo.currentData() == "streaming"
    assert _combo_item_enabled(dialog.mode_combo, "streaming") is True
    assert dialog.language_combo.currentData() == "auto"
    assert _combo_item_enabled(dialog.language_combo, "de") is True
    assert _combo_item_enabled(dialog.language_combo, "bg") is True
    assert _combo_item_enabled(dialog.language_combo, "vi") is True
    assert _combo_item_enabled(dialog.language_combo, "et") is True
    assert _combo_item_enabled(dialog.language_combo, "el") is False
    assert "automatic language detection" in dialog.language_note_label.text()
    assert "560 ms streaming" in dialog.engine_indicator.text()
    assert "DirectML" in dialog.local_model_runtime_warning_label.text()
    assert "fixed 560 ms" in dialog.local_model_runtime_warning_label.text()
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
    assert dialog.history_copy_button.minimumWidth() >= (
        dialog.history_copy_button.sizeHint().width()
    )
    _ = app


def test_settings_history_refresh_preserves_selected_entry_and_scroll(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_history_entry(f"entry {index}") for index in range(30)])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog.tabs.setCurrentWidget(dialog._history_tab)
    row_height = dialog._compact_list_item_size(dialog.history_list).height()
    dialog.history_list.setFixedHeight(row_height * 4)
    dialog._refresh_history_list()
    dialog.show()
    app.processEvents()

    item = dialog.history_list.item(10)
    expected_entry = item.data(QtCore.Qt.UserRole)
    item.setSelected(True)
    dialog.history_list.setCurrentItem(item)
    scroll_bar = dialog.history_list.verticalScrollBar()
    scroll_bar.setValue(scroll_bar.maximum())
    scroll_before = scroll_bar.value()

    dialog._refresh_history_list()

    selected = dialog.history_list.selectedItems()
    assert len(selected) == 1
    assert selected[0].data(QtCore.Qt.UserRole) == expected_entry
    assert dialog.history_list.currentItem().data(QtCore.Qt.UserRole) == expected_entry
    if scroll_before > 0:
        assert dialog.history_list.verticalScrollBar().value() == scroll_before
    _ = app


def test_settings_history_list_formats_utc_display_timezone(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save(
        [
            TranscriptHistoryEntry(
                created_at="2026-06-24T16:45:00+00:00",
                text="entry",
                engine="local",
                model="small",
                mode="batch",
            )
        ]
    )
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(display_timezone="utc")),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store

    dialog._refresh_history_list()

    assert dialog.history_timezone_combo.currentData() == "utc"
    assert dialog.history_list.item(0).text().startswith(
        "2026-06-24 16:45:00 UTC | local/small"
    )
    _ = app


def test_settings_history_refresh_skips_unchanged_history(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    first = _history_entry("first")
    second = _history_entry("second")
    history_store.save([first, second])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list()
    original_item = dialog.history_list.item(0)

    dialog._refresh_history_list()

    assert dialog.history_list.item(0) is original_item
    _ = app


def test_settings_history_refresh_prepends_new_entries_without_rebuilding_old_items(
    tmp_path,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    first = _history_entry("first")
    second = _history_entry("second")
    history_store.save([first, second])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list()
    second_item = dialog.history_list.item(0)

    history_store.save(
        [
            first,
            second,
            _history_entry("third"),
        ]
    )
    dialog._refresh_history_list()

    assert "third" in dialog.history_list.item(0).text()
    assert dialog.history_list.item(1) is second_item
    _ = app


def test_settings_history_limit_change_removes_extra_items_without_rebuilding(
    tmp_path,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save(
        [
            _history_entry("first"),
            _history_entry("second"),
            _history_entry("third"),
        ]
    )
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list()
    newest_item = dialog.history_list.item(0)

    dialog.history_max_spin.setValue(2)

    assert dialog.history_list.count() == 2
    assert dialog.history_list.item(0) is newest_item
    _ = app


def test_settings_history_multiselect_copy_joins_selected_entries(
    monkeypatch, tmp_path
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save(
        [
            _history_entry("first"),
            _history_entry("second"),
            _history_entry("third"),
        ]
    )
    clipboard = _FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: clipboard)
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list()

    dialog.history_list.item(0).setSelected(True)
    dialog.history_list.item(1).setSelected(True)
    dialog._copy_selected_history()

    assert clipboard.text() == "second\n\nthird"
    assert dialog.history_edit_button.isEnabled() is False
    assert dialog.history_delete_button.isEnabled() is True
    _ = app


def test_settings_history_multiselect_delete_removes_selected_entries(
    monkeypatch, tmp_path
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save(
        [
            _history_entry("first"),
            _history_entry("second"),
            _history_entry("third"),
        ]
    )
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: QtWidgets.QMessageBox.Yes,
    )
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list()
    first_item = dialog.history_list.item(2)

    dialog.history_list.item(0).setSelected(True)
    dialog.history_list.item(1).setSelected(True)
    dialog._delete_selected_history()

    assert [entry.text for entry in history_store.load()] == ["first"]
    assert dialog.history_list.count() == 1
    assert dialog.history_list.item(0) is first_item
    _ = app


def test_settings_history_edit_updates_item_without_rebuilding_others(
    monkeypatch, tmp_path
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save(
        [
            _history_entry("first"),
            _history_entry("second"),
        ]
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.TranscriptEditDialog.get_text",
        lambda *_args, **_kwargs: "second edited",
    )
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list()
    first_item = dialog.history_list.item(1)

    dialog.history_list.item(0).setSelected(True)
    dialog._edit_selected_history()

    assert "second edited" in dialog.history_list.item(0).text()
    assert dialog.history_list.item(0).isSelected() is True
    assert dialog.history_detail.toPlainText() == "second edited"
    assert dialog.history_list.item(1) is first_item
    _ = app


def test_settings_history_tab_has_export_import_clear_buttons_and_count_label(
    tmp_path,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_history_entry("alpha"), _history_entry("beta")])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(history_max_items=1)),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list()

    assert dialog.history_export_button.text() == "Export..."
    assert dialog.history_import_button.text() == "Import..."
    assert dialog.history_clear_button.text() == "Clear history"
    assert dialog.history_count_label.text() == (
        "Stored: 2 entries (limit 1; showing latest 1)"
    )
    _ = app


def test_settings_history_export_writes_file(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_history_entry("alpha"), _history_entry("beta")])
    export_path = tmp_path / "export.json"
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(export_path), "JSON files (*.json)"),
    )
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store

    dialog._export_history()

    assert export_path.exists()
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert len(exported) == 2
    assert "Exported 2 entries" in dialog.history_status_label.text()
    _ = app


def test_settings_history_clear_empties_store_after_confirmation(
    monkeypatch, tmp_path
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_history_entry("alpha"), _history_entry("beta")])
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: QtWidgets.QMessageBox.Yes,
    )
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list()

    dialog._clear_history()

    assert history_store.count() == 0
    assert dialog.history_list.count() == 0
    assert "History cleared" in dialog.history_status_label.text()
    _ = app


def test_settings_history_import_appends_entries(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_history_entry("old")])
    import_file = tmp_path / "import.json"
    import_file.write_text(
        json.dumps(
            [
                {
                    "created_at": "2026-03-03T00:00:00+00:00",
                    "text": "new-1",
                    "engine": "local",
                    "model": "small",
                    "mode": "batch",
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(import_file), "JSON files (*.json)"),
    )
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(history_max_items=20)),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list()

    dialog._import_history()

    assert history_store.count() == 2
    assert [entry.text for entry in history_store.load()] == ["old", "new-1"]
    assert "Imported 1 entry" in dialog.history_status_label.text()
    _ = app


def test_settings_history_import_overflow_switches_to_unlimited(
    monkeypatch, tmp_path
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_history_entry("old-1"), _history_entry("old-2")])
    import_file = tmp_path / "import.json"
    import_file.write_text(
        json.dumps(
            [
                {
                    "created_at": "2026-03-03T00:00:00+00:00",
                    "text": "new-1",
                    "engine": "local",
                    "model": "small",
                    "mode": "batch",
                },
                {
                    "created_at": "2026-03-03T00:00:01+00:00",
                    "text": "new-2",
                    "engine": "local",
                    "model": "small",
                    "mode": "batch",
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(import_file), "JSON files (*.json)"),
    )

    store = _FakeSettingsStore(AppSettings(history_max_items=2))
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list()
    dialog._prompt_history_import_overflow = lambda **_kwargs: "unlimited"

    dialog._import_history()

    assert dialog.history_max_spin.value() == 0
    assert store.saved is not None
    assert store.saved.history_max_items == 0
    assert dialog._loaded_settings.history_max_items == 0
    assert history_store.count() == 4
    assert "Imported 2 entries" in dialog.history_status_label.text()

    # A later Save must not treat the already-persisted limit as a pending change
    # (no "Reduce history size" confirmation would be answered in this test).
    dialog._save()
    assert store.saved.history_max_items == 0
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


def test_settings_dialog_uses_roomier_default_size_when_screen_allows(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(
        SettingsDialog,
        "_available_dialog_size",
        lambda _self: QtCore.QSize(2000, 2000),
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.size().width() >= 780
    assert dialog.size().height() >= 960
    _ = app


def test_settings_dialog_size_stays_stable_when_switching_tabs(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(
        SettingsDialog,
        "_available_dialog_size",
        lambda _self: QtCore.QSize(2000, 2000),
    )
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    app.processEvents()

    initial_size = QtCore.QSize(dialog.size())
    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        app.processEvents()
        assert dialog.size() == initial_size
    _ = app


def test_settings_tab_widths_stay_stable_when_selection_changes():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    app.processEvents()

    tab_bar = dialog.tabs.tabBar()
    initial_widths = [
        tab_bar.tabRect(index).width()
        for index in range(dialog.tabs.count())
    ]
    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        app.processEvents()
        assert [
            tab_bar.tabRect(inner_index).width()
            for inner_index in range(dialog.tabs.count())
        ] == initial_widths
    _ = app


def test_general_tab_uses_shared_label_column_width():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.show()
    app.processEvents()

    general_tab = dialog.tabs.widget(0)
    labels_by_text = {
        label.text(): label
        for label in general_tab.findChildren(QtWidgets.QLabel)
    }
    labels = [
        labels_by_text["Hotkey"],
        labels_by_text["Engine"],
        labels_by_text["Paste Mode"],
        labels_by_text["VAD Threshold"],
        labels_by_text["Recordings Folder"],
        labels_by_text["Overlay Corner"],
    ]

    widths = {label.minimumWidth() for label in labels}
    assert len(widths) == 1
    assert next(iter(widths)) > 0
    _ = app


def test_remote_provider_key_fields_align_with_azure_endpoint():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    remote_index = next(
        index
        for index in range(dialog.tabs.count())
        if dialog.tabs.tabText(index) == "Remote"
    )
    dialog.tabs.setCurrentIndex(remote_index)
    dialog.show()
    app.processEvents()

    key_x = dialog.assemblyai_key_edit.mapTo(dialog, QtCore.QPoint(0, 0)).x()
    endpoint_x = dialog.azure_endpoint_edit.mapTo(dialog, QtCore.QPoint(0, 0)).x()
    assert abs(key_x - endpoint_x) <= 2
    _ = app


def test_remote_provider_labels_align_with_key_field_center():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    remote_index = next(
        index
        for index in range(dialog.tabs.count())
        if dialog.tabs.tabText(index) == "Remote"
    )
    dialog.tabs.setCurrentIndex(remote_index)
    dialog.show()
    app.processEvents()

    provider_label = next(
        label
        for label in dialog.findChildren(QtWidgets.QLabel)
        if label.text() == "AssemblyAI"
    )
    provider_center = provider_label.mapTo(dialog, provider_label.rect().center()).y()
    key_center = dialog.assemblyai_key_edit.mapTo(
        dialog,
        dialog.assemblyai_key_edit.rect().center(),
    ).y()
    assert abs(provider_center - key_center) <= 2
    _ = app


def test_remote_provider_status_badges_use_calculated_fixed_width():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    expected_width = dialog._provider_status_badge_width()
    longest_status = max(
        settings_dialog_module._PROVIDER_STATUS_BADGE_TEXTS,
        key=dialog.fontMetrics().horizontalAdvance,
    )
    assert longest_status == "Will clear on Save"
    assert expected_width < 150
    for badge in dialog._provider_status_labels.values():
        assert badge.minimumWidth() == expected_width
        assert badge.maximumWidth() == expected_width
    _ = app


def test_inline_field_buttons_match_their_field_height():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.model_dir_browse.maximumHeight() == (
        dialog.model_dir_edit.maximumHeight()
    )
    assert dialog.recordings_dir_browse.maximumHeight() == (
        dialog.recordings_dir_edit.maximumHeight()
    )
    assert dialog.recordings_open_button.maximumHeight() == (
        dialog.recordings_dir_edit.maximumHeight()
    )
    assert dialog.benchmark_audio_browse_button.maximumHeight() == (
        dialog.benchmark_audio_edit.maximumHeight()
    )
    assert dialog.benchmark_audio_last_button.maximumHeight() == (
        dialog.benchmark_audio_edit.maximumHeight()
    )
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
    _wait_for_local_models(dialog, "small", cached=True)
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
        "stt_app.settings_dialog.start_model_download_process",
        lambda model_name, _model_dir="": (
            cached.append(model_name) or _CompletedDownloadProcess()
        ),
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
    _wait_for_local_models(dialog, "tiny")

    _select_local_model_names(dialog, "tiny")

    dialog._download_selected_local_models()

    assert "Downloaded: tiny" in dialog.local_models_action_label.text()
    assert "tiny" in dialog.local_models_label.text()
    _ = app


def test_local_tab_queues_another_download_while_one_is_active(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []

    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda _model_dir="": list(calls),
    )

    def _start_download(model_name: str, _model_dir: str = ""):
        calls.append(model_name)
        if model_name == "tiny":
            first_started.set()
            return _BlockingDownloadProcess(release_first)
        return _CompletedDownloadProcess()

    monkeypatch.setattr(
        "stt_app.settings_dialog.start_model_download_process",
        _start_download,
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtTest.QTest.qWait(250)

    dialog._start_local_model_download(["tiny"])
    worker = dialog._active_local_model_download_thread
    assert worker is not None
    assert first_started.wait(timeout=1.0)
    app.processEvents()

    _select_local_model_names(dialog, "base")
    assert dialog.download_selected_models_button.isEnabled() is True
    assert dialog.model_dir_edit.isEnabled() is False

    dialog._download_selected_local_models()
    dialog._start_local_model_download(["base"])

    active, queued, running = dialog._local_model_download_snapshot()
    assert active is not None and active[0] == "tiny"
    assert [name for name, _model_dir in queued] == ["base"]
    assert running is True

    release_first.set()
    worker.join(timeout=3.0)
    QtTest.QTest.qWait(50)

    assert worker.is_alive() is False
    assert calls == ["tiny", "base"]
    assert "Downloaded: tiny, base" in dialog.local_models_action_label.text()
    _ = app


def test_local_tab_can_cancel_active_and_queued_downloads(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    started = threading.Event()
    release = threading.Event()
    process = _BlockingDownloadProcess(release)

    monkeypatch.setattr(
        "stt_app.settings_dialog.start_model_download_process",
        lambda _model_name, _model_dir="": started.set() or process,
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog.cleanup_incomplete_model_download",
        lambda _model_name, _model_dir="": (1, 2_000_000),
    )

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._start_local_model_download(["tiny", "base"])
    worker = dialog._active_local_model_download_thread
    assert worker is not None
    assert started.wait(timeout=1.0)
    app.processEvents()

    assert dialog.cancel_model_downloads_button.isEnabled() is True
    dialog._cancel_local_model_downloads()
    worker.join(timeout=3.0)
    QtTest.QTest.qWait(50)

    assert process.terminated is True
    assert dialog.cancel_model_downloads_button.isEnabled() is False
    assert "Download canceled" in dialog.local_models_action_label.text()
    assert "1 incomplete file" in dialog.local_models_action_label.text()
    assert "2.0 MB" in dialog.local_models_action_label.text()
    active, queued, running = dialog._local_model_download_snapshot()
    assert active is None
    assert queued == []
    assert running is False
    _ = app


def test_local_model_download_progress_shows_percent_speed_and_queue(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    with dialog._local_model_download_lock:
        dialog._local_model_download_active = ("small", "")
        dialog._local_model_download_queue = [("base", "")]
        dialog._local_model_download_worker_running = True
    dialog._local_model_download_speed_tracker.reset(
        "small",
        142_000_000,
        now=10.0,
    )

    monkeypatch.setattr(
        "stt_app.settings_dialog.estimate_cached_model_bytes",
        lambda _model_name, _model_dir="": 242_000_000,
    )
    monkeypatch.setattr("stt_app.settings_dialog.time.monotonic", lambda: 12.0)

    dialog._refresh_local_model_download_progress()

    assert dialog.local_model_download_progress_bar.isHidden() is False
    assert dialog.local_model_download_progress_bar.value() == 50
    assert "approx. 50%" in dialog.local_models_action_label.text()
    assert "50.0 MB/s" in dialog.local_models_action_label.text()
    assert "400.0 Mbit/s" in dialog.local_models_action_label.text()
    assert "1 model queued" in dialog.local_models_action_label.text()
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
    _wait_for_local_models(dialog, "tiny", cached=True)

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


def test_benchmark_results_table_and_summary_are_resizable():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    dialog.show()
    app.processEvents()

    splitter = dialog.benchmark_results_splitter
    assert isinstance(splitter, QtWidgets.QSplitter)
    assert splitter.orientation() == QtCore.Qt.Vertical
    assert splitter.childrenCollapsible() is False
    assert splitter.widget(0) is dialog.benchmark_results_table
    assert splitter.widget(1) is dialog.benchmark_summary_text
    assert (
        dialog.benchmark_results_table.horizontalScrollMode()
        == QtWidgets.QAbstractItemView.ScrollPerPixel
    )
    assert (
        dialog.benchmark_results_table.verticalScrollMode()
        == QtWidgets.QAbstractItemView.ScrollPerPixel
    )
    assert splitter.sizes()[0] > 0
    assert splitter.sizes()[1] > 0
    _ = app


def test_benchmark_tab_prioritizes_history_and_results_above_run_controls():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    dialog.show()
    app.processEvents()

    splitter = dialog.benchmark_main_splitter
    assert isinstance(splitter, QtWidgets.QSplitter)
    assert splitter.orientation() == QtCore.Qt.Vertical
    assert splitter.childrenCollapsible() is False
    assert splitter.indexOf(dialog.benchmark_history_list.parentWidget()) == 0
    assert splitter.indexOf(dialog.benchmark_results_splitter.parentWidget()) == 1
    assert splitter.widget(2) is dialog.benchmark_setup_scroll
    assert dialog.benchmark_setup_scroll.widget() is dialog.benchmark_setup_box
    assert dialog.benchmark_setup_box.title() == "Run Benchmark"
    assert dialog.benchmark_history_list.minimumHeight() >= 120
    assert dialog.benchmark_setup_scroll.minimumHeight() >= 360
    assert splitter.sizes()[2] >= splitter.sizes()[1]
    assert dialog.benchmark_options_box.isVisible() is False
    assert dialog.benchmark_options_toggle.text().endswith("Show Run Options")

    dialog.benchmark_options_toggle.click()

    assert dialog.benchmark_options_box.isVisible() is True
    assert dialog.benchmark_options_toggle.text().endswith("Hide Run Options")
    assert splitter.sizes()[2] >= splitter.sizes()[1]
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
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    dialog.show()
    app.processEvents()

    item = dialog.benchmark_history_list.item(0)
    dialog.benchmark_history_list.itemDoubleClicked.emit(item)
    app.processEvents()

    assert dialog.benchmark_results_table.rowCount() == 1
    assert dialog.benchmark_results_table.item(0, 0).text() == "small"
    assert dialog.benchmark_summary_text.toPlainText() == entry.summary
    main_sizes = dialog.benchmark_main_splitter.sizes()
    assert main_sizes[1] > main_sizes[0]
    assert main_sizes[2] > main_sizes[0]
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


def test_import_audio_picker_starts_in_recordings_dir(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    recordings_path = tmp_path / "recordings"

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(
            AppSettings(recordings_dir=str(recordings_path))
        ),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    dialog._choose_import_file()

    assert dialog._import_file_dialog is not None
    assert Path(dialog._import_file_dialog.directory().absolutePath()) == (
        recordings_path
    )
    assert dialog._import_file_dialog.testOption(
        QtWidgets.QFileDialog.DontUseNativeDialog
    )
    assert dialog._import_file_dialog.isModal() is False
    assert recordings_path.is_dir()
    dialog._on_import_file_dialog_finished(0)
    _ = app


def test_open_recordings_dir_refreshes_global_hotkeys(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    recordings_path = tmp_path / "recordings"

    class FakeController:
        def __init__(self):
            self.refresh_calls = 0

        def refresh_hotkey_registration(self):
            self.refresh_calls += 1

    controller = FakeController()
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(
            AppSettings(recordings_dir=str(recordings_path))
        ),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
        controller=controller,
    )
    opened = []
    monkeypatch.setattr(
        QtGui.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    monkeypatch.setattr(
        QtCore.QTimer,
        "singleShot",
        lambda _delay, callback: callback(),
    )

    dialog._open_recordings_dir()

    assert [Path(path) for path in opened] == [recordings_path]
    assert recordings_path.is_dir()
    assert controller.refresh_calls == 1
    _ = app


def test_import_audio_picker_reuses_selected_file_directory(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    selected_dir = tmp_path / "selected"
    selected_dir.mkdir()
    selected_path = selected_dir / "sample.wav"
    selected_path.write_bytes(b"RIFF")

    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._set_selected_import_file(str(selected_path))

    dialog._choose_import_file()

    assert dialog._import_file_dialog is not None
    assert Path(dialog._import_file_dialog.directory().absolutePath()) == selected_dir
    dialog._on_import_file_dialog_finished(0)
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


def test_local_model_refresh_preserves_current_selection_and_scroll():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    row_height = dialog._compact_list_item_size(dialog.local_models_list).height()
    dialog.local_models_list.setFixedHeight(row_height * 4)
    dialog._refresh_local_models_list(["small"])
    dialog.show()
    app.processEvents()

    target_item = dialog.local_models_list.item(dialog.local_models_list.count() - 2)
    target_model = str(target_item.data(QtCore.Qt.UserRole) or "")
    target_item.setSelected(True)
    dialog.local_models_list.setCurrentItem(
        target_item,
        QtCore.QItemSelectionModel.NoUpdate,
    )
    scroll_bar = dialog.local_models_list.verticalScrollBar()
    scroll_bar.setValue(scroll_bar.maximum())
    scroll_before = scroll_bar.value()

    dialog._refresh_local_models_list(["small"])

    restored_items = [
        dialog.local_models_list.item(index)
        for index in range(dialog.local_models_list.count())
        if str(
            dialog.local_models_list.item(index).data(QtCore.Qt.UserRole) or ""
        )
        == target_model
    ]
    assert len(restored_items) == 1
    assert restored_items[0].isSelected() is True
    assert dialog.local_models_list.currentItem() is restored_items[0]
    if scroll_before > 0:
        assert dialog.local_models_list.verticalScrollBar().value() == scroll_before
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
    expected_width = dialog._provider_status_badge_width()
    assert (
        dialog._provider_status_labels["assemblyai"].maximumWidth()
        == expected_width
    )
    assert (
        dialog._provider_status_labels["assemblyai"].minimumWidth()
        == expected_width
    )
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


def test_save_emits_settings_changed_signal_for_real_changes():
    """_save() emits settings_changed for changes and does NOT close the dialog."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    received: list[bool] = []
    dialog.settings_changed.connect(lambda: received.append(True))

    dialog.vad_checkbox.setChecked(True)
    dialog._save()

    assert store.saved is not None
    assert store.saved.vad_enabled is True
    assert len(received) == 1, "settings_changed signal should fire once"
    # Dialog must still be visible (not closed via accept)
    assert dialog.result() != QtWidgets.QDialog.Accepted
    _ = app


def test_save_without_changes_does_not_emit_settings_changed():
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

    assert store.saved is None
    assert received == []
    assert "no settings changes" in dialog._save_status_label.text().lower()
    _ = app


def test_save_shows_status_feedback_for_real_changes():
    """_save() shows a status message in the save-status label."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = _FakeSettingsStore(AppSettings())
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog._save_status_label.text() == ""
    dialog.vad_checkbox.setChecked(True)
    dialog._save()
    assert "saved" in dialog._save_status_label.text().lower()
    _ = app


def test_check_for_updates_button_reports_up_to_date(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(settings_dialog_module.threading, "Thread", _ImmediateThread)
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
        update_check_runner=lambda: UpdateCheckResult(
            current_version="0.4.1",
            latest_version="0.4.1",
            latest_tag="v0.4.1",
        ),
    )

    dialog.check_updates_button.click()

    assert dialog.check_updates_button.isEnabled() is True
    assert dialog._save_status_label.text() == "Already up to date"
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
    assert "border-bottom: 2px solid #bbb" in stylesheet
    assert "font-weight: bold" not in stylesheet
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
        assert (
            widget.sizeAdjustPolicy()
            == QtWidgets.QAbstractScrollArea.AdjustIgnored
        )
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


def test_import_result_has_copy_button_and_resizable_result_area(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    clipboard = _FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: clipboard)
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )

    assert dialog.import_splitter.orientation() == QtCore.Qt.Vertical
    assert dialog.import_splitter.childrenCollapsible() is False
    assert dialog.import_result_text.sizePolicy().verticalPolicy() == (
        QtWidgets.QSizePolicy.Expanding
    )
    assert dialog.import_copy_button.isEnabled() is False
    assert dialog.import_copy_button.minimumWidth() >= (
        dialog.import_copy_button.sizeHint().width()
    )

    dialog._finish_import_transcription(True, "imported text")
    dialog.import_copy_button.click()

    assert dialog.import_copy_button.isEnabled() is True
    assert clipboard.text() == "imported text"
    assert dialog.import_copy_button.text() == "Copied"
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
    assert dialog.import_copy_button.isEnabled() is True
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


def test_settings_history_double_click_copies_entry(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_history_entry("alpha"), _history_entry("beta")])

    copied: list[str] = []

    class _FakeClipboard:
        def setText(self, text: str) -> None:
            copied.append(text)

    monkeypatch.setattr(
        QtGui.QGuiApplication, "clipboard", lambda: _FakeClipboard()
    )
    dialog = SettingsDialog(
        settings_store=_FakeSettingsStore(AppSettings(history_max_items=20)),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
    )
    dialog._history_store = history_store
    dialog._refresh_history_list(force=True)

    item = dialog.history_list.item(0)
    dialog.history_list.itemDoubleClicked.emit(item)

    entry = item.data(QtCore.Qt.UserRole)
    assert copied == [entry.text]
    assert dialog.history_copy_button.text() == "Copied"
    _ = app
