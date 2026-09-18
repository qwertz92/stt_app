from __future__ import annotations

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from stt_app.config import (
    DEFAULT_RECORDINGS_MAX_COUNT,
    RECORDINGS_MAX_COUNT_CEILING,
    RECORDINGS_MAX_COUNT_UNLIMITED,
)
from stt_app.dialog_style import make_label_selectable
from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_dialog_helpers import (
    _DEFAULT_SETTINGS_DIALOG_SIZE,
    ElidingLabel,
)
from stt_app.settings_store import AppSettings, SettingsStore


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


@pytest.mark.pixel_exact
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


def test_every_local_model_note_fits_the_two_lines_reserved_for_it(
    dialog: SettingsDialog,
) -> None:
    """The engine loop above only ever sees the default local model.

    Both notes sit above the rest of the form and are reserved at two lines,
    so a model whose text wraps to three is clipped -- and the reservation is
    per label, not per model, so one long sentence cannot simply grow it.
    """
    from stt_app.config import VALID_MODEL_SIZES

    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("local"))
    app.processEvents()

    for model in VALID_MODEL_SIZES:
        model_index = dialog.model_combo.findData(model)
        if model_index < 0:
            continue
        dialog.model_combo.setCurrentIndex(model_index)
        app.processEvents()
        for label in (
            dialog.local_model_runtime_warning_label,
            dialog.language_note_label,
        ):
            required_height = label.fontMetrics().boundingRect(
                QtCore.QRect(0, 0, label.width(), 1000),
                QtCore.Qt.TextWordWrap,
                label.text(),
            ).height()
            assert required_height <= label.height(), (model, label.text())


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

    General keeps what changes during daily dictation (engine/model,
    insertion); microphone, VAD, tones, and recordings live on the Audio &
    Recording tab, after General and Hotkeys & Display.
    """
    titles = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
    assert titles[:4] == [
        "General",
        "Hotkeys && Display",
        "Audio && Recording",
        "Local",
    ]

    general_tab = dialog.tabs.widget(0)
    audio_tab = dialog.tabs.widget(2)
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
        dialog.engine_combo,
        dialog.model_selector_stack,
        dialog.language_combo,
        dialog.mode_combo,
        dialog.paste_mode_combo,
    ):
        assert general_tab.isAncestorOf(widget)


def test_hotkeys_and_display_tab_hosts_the_set_once_controls(
    dialog: SettingsDialog,
) -> None:
    """Hotkeys, overlay corner and the tray toggle left General for their own
    tab, and the history time zone sits on the History tab whose list it
    formats. The attribute names are what persistence reads, so they stay.
    """
    general_tab = dialog.tabs.widget(0)
    hotkeys_tab = dialog.tabs.widget(1)
    assert dialog.tabs.tabText(1) == "Hotkeys && Display"
    for widget in (
        dialog.hotkey_edit,
        dialog.cancel_hotkey_edit,
        dialog.show_overlay_hotkey_edit,
        dialog.repaste_hotkey_edit,
        dialog.overlay_corner_combo,
        dialog.tray_middle_click_checkbox,
    ):
        assert hotkeys_tab.isAncestorOf(widget)
        assert not general_tab.isAncestorOf(widget)

    assert dialog._history_tab.isAncestorOf(dialog.history_timezone_combo)
    assert not hotkeys_tab.isAncestorOf(dialog.history_timezone_combo)


def test_general_tab_fits_the_default_dialog_height_without_scrolling(
    dialog: SettingsDialog,
) -> None:
    """General held four groups and needed 1342 px, so on a 1392 px screen it
    scrolled by 283 px (measured; a smaller screen scrolls by more) -- and it
    is the tab the dialog opens on. With Hotkeys and Display on their own
    tab it needs 781 px at the 9 pt font.

    Measured against the design height rather than this screen's: the dialog
    grows to the screen it is on, and a test that read the live viewport
    would pass on a tall monitor whatever the tab held.
    """
    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    _switch_to_tab(dialog, "General")
    for _ in range(5):
        app.processEvents()

    general_tab = dialog.tabs.widget(0)
    needed = general_tab.widget().minimumSizeHint().height()
    # Everything of the dialog that is not the scroll viewport: tab bar,
    # margins, the engine line and the button row.
    chrome = dialog.height() - general_tab.viewport().height()
    design_height = _DEFAULT_SETTINGS_DIALOG_SIZE.height()

    point_size = app.font().pointSizeF()
    if point_size != 9.0:
        # Windows' "Text size" raises the application font without the DPI,
        # and the form grows with it; the budget is a 9 pt number.
        pytest.skip(
            f"the height budget was measured at 9 pt; this session runs at "
            f"{point_size} pt and General needs {needed} px plus {chrome} px"
        )
    assert needed + chrome <= design_height, (needed, chrome, design_height)


def test_audio_values_are_greyed_out_while_their_checkbox_is_off(
    dialog: SettingsDialog,
) -> None:
    """A threshold or tone that is only read while its checkbox is on says so
    by being disabled while it is off -- including right after the dialog was
    populated, where an unchanged `setChecked(False)` emits no `toggled`."""
    links = (
        (dialog.vad_checkbox, dialog.vad_threshold_spin),
        (dialog.start_beep_checkbox, dialog.start_beep_tone_combo),
        (dialog.completion_beep_checkbox, dialog.completion_beep_tone_combo),
    )
    # The fixture's default settings have all three switched off.
    for checkbox, dependent in links:
        assert checkbox.isChecked() is False
        assert dependent.isEnabled() is False

    for checkbox, dependent in links:
        checkbox.setChecked(True)
        assert dependent.isEnabled() is True
        checkbox.setChecked(False)
        assert dependent.isEnabled() is False

    # Streaming reads the silence-gate threshold for its pause handling even
    # with the gate off, so that value must stay editable.
    dialog.silence_gate_checkbox.setChecked(False)
    assert dialog.silence_gate_threshold_spin.isEnabled() is True


def test_populating_enabled_audio_checkboxes_enables_their_values(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings_dialog = SettingsDialog(
        settings_store=_SettingsStore(
            AppSettings(
                vad_enabled=True,
                start_beep_enabled=True,
                completion_beep_enabled=True,
            )
        ),
        secret_store=_SecretStore(),
        app_logger=_Logger(),
    )
    try:
        assert settings_dialog.vad_threshold_spin.isEnabled() is True
        assert settings_dialog.start_beep_tone_combo.isEnabled() is True
        assert settings_dialog.completion_beep_tone_combo.isEnabled() is True
    finally:
        settings_dialog.close()
        app.processEvents()


def test_recordings_retention_offers_unlimited_at_zero(
    dialog: SettingsDialog,
) -> None:
    """0 is a reachable, labelled choice, not a number the user has to guess."""
    spin = dialog.recordings_max_spin

    assert spin.minimum() == RECORDINGS_MAX_COUNT_UNLIMITED
    assert spin.maximum() == RECORDINGS_MAX_COUNT_CEILING
    assert spin.specialValueText() == "Unlimited (0)"
    assert spin.value() == DEFAULT_RECORDINGS_MAX_COUNT

    spin.setValue(RECORDINGS_MAX_COUNT_UNLIMITED)

    assert spin.text() == "Unlimited (0)"
    assert "0 = keep every recording" in dialog.recordings_max_hint_label.text()
    assert "0 keeps every one" in spin.toolTip()


def test_a_saved_unlimited_recordings_count_reloads_as_unlimited(
    monkeypatch,
    tmp_path,
) -> None:
    """The whole round trip: spin box -> real store -> file -> spin box.

    The store used to clamp this field to >= 1, so a 0 chosen in the dialog
    came back as 1 -- "keep exactly one recording", the most destructive value
    in the range, from the setting that means "delete nothing".
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    settings_dialog = SettingsDialog(
        settings_store=store,
        secret_store=_SecretStore(),
        app_logger=_Logger(),
    )
    try:
        settings_dialog.recordings_max_spin.setValue(RECORDINGS_MAX_COUNT_UNLIMITED)

        settings_dialog._save()

        assert store.load().recordings_max_count == RECORDINGS_MAX_COUNT_UNLIMITED

        settings_dialog.recordings_max_spin.setValue(DEFAULT_RECORDINGS_MAX_COUNT)
        settings_dialog.reload_from_store()

        assert (
            settings_dialog.recordings_max_spin.value()
            == RECORDINGS_MAX_COUNT_UNLIMITED
        )
        assert settings_dialog.recordings_max_spin.text() == "Unlimited (0)"
    finally:
        settings_dialog.close()
        app.processEvents()


def test_the_recordings_retention_spin_box_never_changes_width(
    dialog: SettingsDialog,
) -> None:
    """Nothing may jump: the widest text the box can hold decides its size.

    A special value text and a five-digit ceiling both widen a `QSpinBox`'s
    size hint, so the risk is a box that resizes as the user types past 999 or
    steps down onto "Unlimited (0)". Qt sizes it from the widest of its range
    and its special text rather than from the current value, which is what
    this pins -- together with the box still being narrower than the field
    column it sits in, so it is not what decides the row's width.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _switch_to_tab(dialog, "Audio && Recording")
    dialog.show()
    app.processEvents()
    spin = dialog.recordings_max_spin

    measured: list[tuple[int, int, int]] = []
    for value in (
        RECORDINGS_MAX_COUNT_UNLIMITED,
        DEFAULT_RECORDINGS_MAX_COUNT,
        RECORDINGS_MAX_COUNT_CEILING,
    ):
        spin.setValue(value)
        app.processEvents()
        assert spin.value() == value
        measured.append((spin.width(), spin.sizeHint().width(), spin.x()))

    assert len(set(measured)) == 1, measured
    assert spin.sizeHint().width() <= spin.width()


def test_microphone_picker_lists_devices_and_keeps_missing_selection(
    dialog: SettingsDialog,
    monkeypatch,
) -> None:
    from stt_app.audio_devices import InputDeviceInfo

    monkeypatch.setattr(
        "stt_app.audio_devices.query_input_devices",
        lambda: ([InputDeviceInfo(name="USB Mic", index=3)], True),
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


def test_microphone_picker_does_not_call_a_device_disconnected_when_portaudio_did_not_answer(
    dialog: SettingsDialog,
    monkeypatch,
) -> None:
    """During a re-enumeration the query answers nothing, not "no devices".

    The picker labelled the user's plugged-in microphone "(not connected)"
    for the window between the refresh worker's terminate/initialize and the
    dialog's repopulate timer. The selection itself must still survive.
    """
    monkeypatch.setattr("stt_app.audio_devices.query_input_devices", lambda: ([], False))

    dialog._populate_microphone_combo("Headset Microphone (Jabra)")

    combo = dialog.microphone_combo
    labels = [combo.itemText(index) for index in range(combo.count())]
    assert labels[1] == "Headset Microphone (Jabra) (device list unavailable)"
    assert "not connected" not in labels[1]
    assert combo.currentData() == "Headset Microphone (Jabra)"


@pytest.mark.pixel_exact
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
    monkeypatch.setattr(
        "stt_app.audio_devices.query_input_devices", lambda: ([], True)
    )
    requests: list[bool] = []
    dialog.audio_device_refresh_requested.connect(
        lambda: requests.append(True)
    )

    dialog._on_microphone_refresh_clicked()

    assert requests == [True]
    # The delayed repopulate is armed so the list updates again after the
    # controller's off-thread re-enumeration finished.
    assert dialog._microphone_repopulate_timer.isActive()


@pytest.mark.pixel_exact
def test_bottom_status_does_not_move_the_save_and_close_buttons(
    dialog: SettingsDialog,
) -> None:
    """The bottom status text must never move the Save/Close buttons.

    Their row also holds a status label whose text ranges from empty to a full
    failure message; the label takes the leftover space with a width policy
    the layout ignores, so guard that a message never pushes the buttons.
    """
    dialog.show()
    QtWidgets.QApplication.processEvents()
    save_button = dialog._save_button
    idle_position = save_button.pos()

    dialog._set_bottom_status("Settings saved")
    QtWidgets.QApplication.processEvents()
    assert save_button.pos() == idle_position

    dialog._set_bottom_status(
        "Failed to save settings: " + ("a very long failure reason " * 8),
        "#b71c1c",
    )
    QtWidgets.QApplication.processEvents()
    assert save_button.pos() == idle_position

    # A multi-line exception message: the label stays one line, or the row
    # grows past the 34 px buttons and both move up (measured before the
    # fix: 7 px at three lines and 8 px more per line after).
    label = dialog._save_status_label
    one_line_height = label.height()
    for lines in (3, 6):
        dialog._set_bottom_status(
            "\n".join(f"line {index} of a failed save" for index in range(lines)),
            "#b71c1c",
        )
        QtWidgets.QApplication.processEvents()
        assert save_button.pos() == idle_position
        assert label.height() == one_line_height
        assert "\n" not in QtWidgets.QLabel.text(label)

    dialog._set_bottom_status("")
    QtWidgets.QApplication.processEvents()
    assert save_button.pos() == idle_position
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

    granite_note = note_for("granite-speech-4.1-2b")
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


def test_the_local_download_bar_appearing_moves_nothing(dialog) -> None:
    """The Local tab's progress bar appears the instant a download starts.

    Without a retained size it took its 28 px out of the layout while hidden,
    so pressing Download pulled Download/Cancel/Delete up under the cursor --
    with Cancel sliding into the place the pointer was already on -- and pushed
    them back down when the download finished, shrinking the model list twice
    per download.
    """
    dialog.show()
    _switch_to_tab(dialog, "Local")
    QtWidgets.QApplication.processEvents()

    watched = {
        "list": dialog.local_models_list,
        "action label": dialog.local_models_action_label,
        "download": dialog.download_selected_models_button,
        "cancel": dialog.cancel_model_downloads_button,
        "delete": dialog.delete_selected_model_button,
    }

    def geometry() -> dict[str, tuple[int, int]]:
        QtWidgets.QApplication.processEvents()
        return {
            name: (widget.mapTo(dialog, QtCore.QPoint(0, 0)).y(), widget.height())
            for name, widget in watched.items()
        }

    hidden = geometry()
    assert geometry() == hidden, "the tab had not settled before the measurement"

    bar = dialog.local_model_download_progress_bar
    bar.setValue(42)
    bar.setFormat("small: 210/486 MB (43%)")
    bar.setVisible(True)
    assert geometry() == hidden, "starting a download moved the controls"

    bar.setVisible(False)
    assert geometry() == hidden, "finishing a download moved the controls"
    dialog.hide()


@pytest.mark.pixel_exact
def test_a_long_bottom_status_is_elided_and_widens_nothing(
    dialog: SettingsDialog,
) -> None:
    """A failed save's status is the whole exception message. As a plain
    label it was clipped with no ellipsis and raised the dialog's minimum
    hint to the text's width for the seconds it showed -- 1360 px for a
    172-character `WinError 5` -- which the width pin then captured for the
    life of the app."""
    dialog.show()
    QtWidgets.QApplication.processEvents()
    before = dialog.minimumSizeHint().width()
    message = "Failed to save settings: " + "a very long failure reason " * 16

    dialog._set_bottom_status(message, "#b71c1c")
    QtWidgets.QApplication.processEvents()

    label = dialog._save_status_label
    shown = QtWidgets.QLabel.text(label)
    assert label.text() == message
    assert shown != message
    assert shown.endswith("\u2026")
    assert label.toolTip() == message
    assert dialog.minimumSizeHint().width() == before
    assert label.geometry().right() < dialog._save_button.geometry().left()

    # And a short message is shown whole: the label takes the row's leftover
    # space, which an `Ignored` width policy alone would not get it.
    dialog._set_bottom_status("Settings saved")
    QtWidgets.QApplication.processEvents()
    assert QtWidgets.QLabel.text(label) == "Settings saved"
    dialog._set_bottom_status("")
    dialog.hide()


def test_copying_an_elided_status_yields_the_message_as_written(monkeypatch):
    """`QLabel` copies from its own text control, which holds the elided
    text: a failed save's message -- the one text worth pasting into a bug
    report -- came back as its first 37 characters and an ellipsis
    (measured), and the tooltip that holds the rest cannot be copied."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    copied: list[str] = []

    class _Clipboard:
        def setText(self, text: str) -> None:
            copied.append(text)

    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: _Clipboard())
    label = ElidingLabel()
    make_label_selectable(label)
    label.resize(200, 20)
    message = "Failed to save settings: " + "a very long failure reason " * 16
    label.setText(message)
    shown = QtWidgets.QLabel.text(label)
    assert shown != message
    assert shown.endswith("\u2026")

    # Ctrl+C with nothing selected, and with the whole painted text selected.
    QtTest.QTest.keyClick(label, QtCore.Qt.Key_C, QtCore.Qt.ControlModifier)
    label.setSelection(0, len(shown))
    QtTest.QTest.keyClick(label, QtCore.Qt.Key_C, QtCore.Qt.ControlModifier)
    assert copied == [message, message]

    # A part of the painted text copies as selected.
    label.setSelection(0, 6)
    QtTest.QTest.keyClick(label, QtCore.Qt.Key_C, QtCore.Qt.ControlModifier)
    assert copied[-1] == "Failed"

    # The label's own context menu takes the same road.
    label.setSelection(0, len(shown))
    menu = label._context_menu()
    [copy_action] = [action for action in menu.actions() if action.text() == "Copy"]
    copy_action.trigger()
    assert copied[-1] == message
    _ = app
