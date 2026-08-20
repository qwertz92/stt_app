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


def _switch_to_tab(dialog: SettingsDialog, title: str) -> None:
    tabs = dialog.tabs
    for index in range(tabs.count()):
        if tabs.tabText(index) == title:
            tabs.setCurrentIndex(index)
            return
    raise AssertionError(f"tab not found: {title}")


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
    _switch_to_tab(dialog, "Audio && Recording")
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


def test_audio_and_recording_tab_hosts_capture_settings(
    dialog: SettingsDialog,
) -> None:
    """The capture setup moved off General into its own tab.

    General keeps what changes during daily dictation (hotkeys, display,
    engine/model, insertion); microphone, VAD, tones, and recordings live on
    the Audio & Recording tab directly after General.
    """
    titles = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
    assert titles[:3] == ["General", "Audio && Recording", "Local"]

    general_tab = dialog.tabs.widget(0)
    audio_tab = dialog.tabs.widget(1)
    for widget in (
        dialog.microphone_combo,
        dialog.vad_checkbox,
        dialog.silence_gate_checkbox,
        dialog.start_beep_tone_combo,
        dialog.completion_beep_checkbox,
        dialog.completion_beep_tone_combo,
        dialog.recordings_dir_edit,
        dialog.recordings_max_spin,
    ):
        assert audio_tab.isAncestorOf(widget)
        assert not general_tab.isAncestorOf(widget)
    for widget in (
        dialog.hotkey_edit,
        dialog.show_overlay_hotkey_edit,
        dialog.repaste_hotkey_edit,
        dialog.tray_middle_click_checkbox,
        dialog.engine_combo,
        dialog.paste_mode_combo,
    ):
        assert general_tab.isAncestorOf(widget)


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


def test_inline_field_buttons_match_their_field_height(
    dialog: SettingsDialog,
) -> None:
    """Inline buttons must render at their field's height, never taller.

    The dialog stylesheet's base QPushButton rule has a larger vertical box
    than native inputs. Without the inlineFieldButton stylesheet override,
    that QSS minimum beats the fixed height set by
    _match_field_button_height, so the button renders taller than its field
    or clipped at the bottom (seen on the microphone Refresh button).
    """
    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    _switch_to_tab(dialog, "Audio && Recording")
    dialog.benchmark_window.show()
    app.processEvents()

    rows = (
        (dialog.microphone_combo, dialog.microphone_refresh_button),
        (
            dialog.recordings_dir_edit,
            dialog.recordings_dir_browse,
            dialog.recordings_open_button,
        ),
        (
            dialog.benchmark_audio_edit,
            dialog.benchmark_audio_browse_button,
            dialog.benchmark_audio_last_button,
        ),
        (
            dialog.benchmark_select_all_button,
            dialog.benchmark_deselect_all_button,
            dialog.refresh_benchmark_models_button,
        ),
    )
    for field, *buttons in rows:
        for button in buttons:
            assert button.property("inlineFieldButton") is True
            assert button.height() == field.height(), (
                button.text(),
                button.height(),
                field.height(),
            )
            # The stylesheet minimum must fit inside the matched height, or
            # the style would draw the button clipped at the bottom.
            assert button.minimumSizeHint().height() <= field.height(), (
                button.text()
            )

    dialog.benchmark_window.hide()


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


def test_bottom_status_does_not_move_the_save_and_close_buttons(
    dialog: SettingsDialog,
) -> None:
    """The bottom status text must never move the Save/Close buttons.

    Their row also holds a status label whose text ranges from empty to a full
    failure message; the stretch in front of it is what keeps the buttons
    anchored, so guard that it stays there.
    """
    dialog.show()
    QtWidgets.QApplication.processEvents()
    save_button = dialog._save_button
    idle_position = save_button.pos().x()

    dialog._set_bottom_status("Settings saved")
    QtWidgets.QApplication.processEvents()
    assert save_button.pos().x() == idle_position

    dialog._set_bottom_status(
        "Failed to save settings: " + ("a very long failure reason " * 8),
        "#b71c1c",
    )
    QtWidgets.QApplication.processEvents()
    assert save_button.pos().x() == idle_position

    dialog._set_bottom_status("")
    QtWidgets.QApplication.processEvents()
    assert save_button.pos().x() == idle_position
    dialog.hide()


def test_onnx_device_row_never_moves_the_fields_below_it(dialog):
    """The picker only applies to the local ONNX models, but hiding the row for
    the others would shift every field beneath it. It stays present and only
    changes enabled state and note text."""
    dialog.show()

    def probe(engine: str, model: str | None) -> tuple[bool, int]:
        index = dialog.engine_combo.findData(engine)
        assert index >= 0
        dialog.engine_combo.setCurrentIndex(index)
        if model is not None:
            model_index = dialog.model_combo.findData(model)
            assert model_index >= 0, model
            dialog.model_combo.setCurrentIndex(model_index)
        QtWidgets.QApplication.processEvents()
        language_y = _position_in_dialog(
            dialog.language_combo,
            dialog.language_combo.rect().topLeft(),
            dialog,
        ).y()
        return dialog.local_onnx_device_combo.isEnabled(), language_y

    faster_whisper_enabled, y_faster_whisper = probe("local", "small")
    granite_enabled, y_granite = probe("local", "granite-speech-4.1-2b")
    nemotron_enabled, y_nemotron = probe(
        "local", "nemotron-3.5-asr-streaming-0.6b-int4"
    )
    remote_enabled, y_remote = probe("openai", None)

    assert granite_enabled is True
    assert nemotron_enabled is True
    assert faster_whisper_enabled is False
    assert remote_enabled is False
    assert {y_faster_whisper, y_granite, y_nemotron, y_remote} == {y_granite}


def test_language_note_names_the_selected_model_family(dialog):
    """Canary joined LOCAL_EXPLICIT_LANGUAGE_MODELS and inherited Granite's
    hint, which told the user Auto was available — the exact behaviour the
    model must never have."""
    dialog.show()
    index = dialog.engine_combo.findData("local")
    dialog.engine_combo.setCurrentIndex(index)

    def note_for(model: str) -> str:
        model_index = dialog.model_combo.findData(model)
        assert model_index >= 0, model
        dialog.model_combo.setCurrentIndex(model_index)
        QtWidgets.QApplication.processEvents()
        return dialog.language_note_label.text()

    canary_note = note_for("canary-1b-v2")
    assert "Granite" not in canary_note
    assert "translat" in canary_note.lower()

    granite_note = note_for("granite-speech-4.1-2b-plus")
    assert "Granite" not in granite_note

    assert "detects the language itself" in note_for("parakeet-tdt-0.6b-v3")


def test_the_language_hint_never_contradicts_the_language_picker(dialog):
    """The hint sits directly under the combo. A note claiming a model has no
    automatic detection while the combo offers Auto (or the reverse) is a
    user-facing falsehood, and asserting only on the model family's name cannot
    detect it."""
    from stt_app.config import VALID_MODEL_SIZES, language_modes_for_selection

    dialog.show()
    index = dialog.engine_combo.findData("local")
    dialog.engine_combo.setCurrentIndex(index)

    for model in VALID_MODEL_SIZES:
        model_index = dialog.model_combo.findData(model)
        if model_index < 0:
            continue
        dialog.model_combo.setCurrentIndex(model_index)
        QtWidgets.QApplication.processEvents()
        note = dialog.language_note_label.text()
        offers_auto = "auto" in language_modes_for_selection("local", model)
        claims_no_auto = (
            "no automatic" in note.lower()
            or "not provide automatic" in note.lower()
            or "cannot detect the language" in note.lower()
        )
        assert not (offers_auto and claims_no_auto), (
            f"{model}: picker offers Auto but the hint denies it -> {note!r}"
        )
        if not offers_auto and note:
            assert "supports auto" not in note.lower(), (
                f"{model}: picker has no Auto but the hint promises it -> {note!r}"
            )
