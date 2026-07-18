from __future__ import annotations

import pytest
from PySide6 import QtCore, QtTest, QtWidgets

from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_store import AppSettings


class _SettingsStore:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self._settings = settings or AppSettings()

    def load(self) -> AppSettings:
        return self._settings

    def save(self, settings: AppSettings) -> None:
        self._settings = settings


class _SecretStore:
    def get_api_key(self, _provider: str) -> None:
        return None


class _Logger:
    def diagnostics_text(self) -> str:
        return ""


@pytest.fixture
def dialog(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings_dialog = SettingsDialog(
        settings_store=_SettingsStore(),
        secret_store=_SecretStore(),
        app_logger=_Logger(),
    )
    yield settings_dialog
    settings_dialog.close()
    app.processEvents()


def _position_in_dialog(
    widget: QtWidgets.QWidget,
    point: QtCore.QPoint,
    dialog: SettingsDialog,
) -> QtCore.QPoint:
    return widget.mapTo(dialog, point)


def test_vocabulary_hint_explains_parsing_and_model_support(
    dialog: SettingsDialog,
) -> None:
    hint = dialog.vocabulary_hint_label.text()

    assert "commas, semicolons, or new lines" in hint
    assert "Spaces inside a phrase are kept" in hint
    assert "Splunk SOAR" in hint
    assert "both modes by faster-whisper, AssemblyAI, and Deepgram" in hint
    assert "batch mode by OpenAI and Groq" in hint
    assert "Nemotron" in hint
    assert "Cohere/Granite ONNX" in hint
    assert "ignore it" in hint
    assert "Splunk SOAR" in dialog.custom_vocabulary_edit.placeholderText()


def test_new_recording_choice_explains_the_previous_job(
    dialog: SettingsDialog,
) -> None:
    general_tab = dialog.tabs.widget(0)
    labels = {
        label.text()
        for label in general_tab.findChildren(QtWidgets.QLabel)
    }
    values = [
        dialog.concurrent_mode_combo.itemData(index)
        for index in range(dialog.concurrent_mode_combo.count())
    ]
    choices = [
        dialog.concurrent_mode_combo.itemText(index)
        for index in range(dialog.concurrent_mode_combo.count())
    ]

    assert "New Recording" in labels
    assert "While transcribing" not in labels
    assert values == ["insert", "insert_immediate", "history", "cancel"]
    assert all("previous" in choice.lower() for choice in choices)
    assert "press the recording hotkey again" in (
        dialog.concurrent_mode_combo.toolTip()
    )
    assert "previous transcription finishes" in (
        dialog.concurrent_mode_hint_label.text()
    )


def test_field_hints_are_closer_to_their_control_than_the_next_field(
    dialog: SettingsDialog,
) -> None:
    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    app.processEvents()

    control = dialog.keep_microphone_warm_checkbox
    hint = dialog.keep_microphone_warm_hint_label
    next_control = dialog.vad_checkbox
    control_bottom = _position_in_dialog(
        control,
        QtCore.QPoint(0, control.height()),
        dialog,
    ).y()
    hint_top = _position_in_dialog(hint, QtCore.QPoint(0, 0), dialog).y()
    hint_bottom = _position_in_dialog(
        hint,
        QtCore.QPoint(0, hint.height()),
        dialog,
    ).y()
    next_top = _position_in_dialog(next_control, QtCore.QPoint(0, 0), dialog).y()

    control_to_hint = hint_top - control_bottom
    hint_to_next_control = next_top - hint_bottom
    assert 0 <= control_to_hint <= 3
    assert hint_to_next_control >= dialog._GENERAL_FORM_ROW_SPACING_PX
    assert hint_to_next_control > control_to_hint


def test_dynamic_engine_hints_keep_general_rows_stationary(
    dialog: SettingsDialog,
) -> None:
    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    app.processEvents()

    baseline_stack_height = dialog.model_selector_stack.height()
    baseline_language_y = dialog.language_combo.mapTo(dialog, QtCore.QPoint()).y()
    baseline_vocabulary_y = dialog.custom_vocabulary_edit.mapTo(
        dialog,
        QtCore.QPoint(),
    ).y()

    selections = (
        ("local", "cohere-transcribe-03-2026"),
        ("assemblyai", None),
        ("azure", None),
        ("funasr", None),
        ("local", "small"),
    )
    for engine, model in selections:
        dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData(engine))
        if model is not None:
            dialog.model_combo.setCurrentIndex(dialog.model_combo.findData(model))
        app.processEvents()

        assert dialog.model_selector_stack.height() == baseline_stack_height
        assert (
            dialog.language_combo.mapTo(dialog, QtCore.QPoint()).y()
            == baseline_language_y
        )
        assert (
            dialog.custom_vocabulary_edit.mapTo(dialog, QtCore.QPoint()).y()
            == baseline_vocabulary_y
        )


def test_dynamic_notes_reserve_exactly_two_text_lines(
    dialog: SettingsDialog,
) -> None:
    reserved_heights = {
        label.minimumHeight()
        for label in (
            dialog.local_model_runtime_warning_label,
            dialog.remote_model_note_label,
            dialog.language_note_label,
        )
    }

    assert len(reserved_heights) == 1
    reserved_height = reserved_heights.pop()
    assert reserved_height <= dialog.fontMetrics().lineSpacing() * 2 + 10
    for label in (
        dialog.local_model_runtime_warning_label,
        dialog.remote_model_note_label,
        dialog.language_note_label,
    ):
        assert label.maximumHeight() == reserved_height


def test_dynamic_notes_fit_their_reserved_area(
    dialog: SettingsDialog,
) -> None:
    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    app.processEvents()

    for engine in (
        "local",
        "assemblyai",
        "groq",
        "openai",
        "deepgram",
        "elevenlabs",
        "azure",
        "funasr",
    ):
        dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData(engine))
        app.processEvents()
        model_note = (
            dialog.local_model_runtime_warning_label
            if engine == "local"
            else dialog.remote_model_note_label
        )
        for label in (model_note, dialog.language_note_label):
            required_height = label.fontMetrics().boundingRect(
                QtCore.QRect(0, 0, label.width(), 1000),
                QtCore.Qt.TextWordWrap,
                label.text(),
            ).height()
            assert required_height <= label.height(), (engine, label.text())

    assert dialog.language_note_label.text().strip()


def test_owned_delayed_callback_is_cancelled_with_its_dialog() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    owner = QtWidgets.QDialog()
    calls: list[str] = []

    SettingsDialog._schedule_owned_callback(owner, 10, lambda: calls.append("called"))
    owner.deleteLater()
    app.sendPostedEvents(owner, QtCore.QEvent.DeferredDelete)
    QtTest.QTest.qWait(25)

    assert calls == []


def test_microphone_picker_lists_devices_and_keeps_missing_selection(
    dialog: SettingsDialog,
    monkeypatch,
) -> None:
    from stt_app.audio_devices import InputDeviceInfo

    monkeypatch.setattr(
        "stt_app.audio_devices.list_input_devices",
        lambda: [InputDeviceInfo(name="USB Mic", index=3)],
    )

    dialog._populate_microphone_combo("Old Mic")

    combo = dialog.microphone_combo
    values = [combo.itemData(index) for index in range(combo.count())]
    labels = [combo.itemText(index) for index in range(combo.count())]
    assert values == ["", "USB Mic", "Old Mic"]
    assert labels[0].startswith("System default")
    assert labels[2] == "Old Mic (not connected)"
    # The stored-but-disconnected device stays selected so saving cannot
    # silently drop the user's choice.
    assert combo.currentData() == "Old Mic"


def test_microphone_refresh_requests_controller_reenumeration(
    dialog: SettingsDialog,
    monkeypatch,
) -> None:
    monkeypatch.setattr("stt_app.audio_devices.list_input_devices", lambda: [])
    requests: list[bool] = []
    dialog.audio_device_refresh_requested.connect(
        lambda: requests.append(True)
    )

    dialog._on_microphone_refresh_clicked()

    assert requests == [True]
    # The delayed repopulate is armed so the list updates again after the
    # controller's off-thread re-enumeration finished.
    assert dialog._microphone_repopulate_timer.isActive()
