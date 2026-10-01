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


def test_vocabulary_hint_explains_parsing_only(dialog: SettingsDialog) -> None:
    """Which models use the terms is the job of the changing note under the
    field. The parsing rules used to be a static hint of their own, a third
    block of text under one field; they are the field's tooltip now, and the
    placeholder says the short version."""
    hint = dialog.custom_vocabulary_edit.toolTip()
    placeholder = dialog.custom_vocabulary_edit.placeholderText()

    assert "commas, semicolons, or new lines" in hint
    assert "Spaces inside a phrase are kept" in hint
    assert "Splunk SOAR" in hint
    assert "Splunk SOAR" in placeholder
    assert "comma or new line" in placeholder
    assert not hasattr(dialog, "vocabulary_hint_label")
    for name in ("faster-whisper", "Nemotron", "Cohere", "Granite", "ignore"):
        assert name not in hint, name


def test_the_vocabulary_note_says_whether_the_selected_model_uses_the_terms(
    dialog: SettingsDialog,
) -> None:
    """One sentence per selection, naming the model the way the screen does,
    and agreeing with what the factory actually hands the runtime."""
    from stt_app.config import (
        CUSTOM_VOCABULARY_SUPPORTED_SUMMARY,
        VALID_ENGINES,
        VALID_MODEL_SIZES,
        supports_custom_vocabulary,
    )
    from stt_app.settings_dialog_helpers import local_model_short_label

    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    app.processEvents()

    def note_for(engine: str, model: str = "") -> str:
        dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData(engine))
        if model:
            dialog.model_combo.setCurrentIndex(dialog.model_combo.findData(model))
        app.processEvents()
        return dialog.vocabulary_support_label.text()

    for engine in VALID_ENGINES:
        models = VALID_MODEL_SIZES if engine == "local" else ("",)
        for model in models:
            if model and dialog.model_combo.findData(model) < 0:
                continue
            note = note_for(engine, model)
            name = (
                local_model_short_label(model)
                if engine == "local"
                else dialog._provider_label(engine)
            )
            assert note.startswith(name), (engine, model, note)
            assert note == dialog.vocabulary_support_label.toolTip()
            # A remote engine is asked about the model it would run: for
            # Speechmatics the answer differs by model (Melia 1 ignores the
            # terms), so asking with no model asked a different question.
            if engine != "local":
                model = dialog._remote_model_value_for_provider(engine)
            if supports_custom_vocabulary(engine, model):
                assert "uses the custom vocabulary" in note, (engine, model)
                assert "ignores" not in note, (engine, model)
            else:
                assert "ignores the custom vocabulary" in note, (engine, model)
                # The note has to answer "so what do I switch to".
                assert CUSTOM_VOCABULARY_SUPPORTED_SUMMARY in note

    # A model that ignores the terms must not disable the field: the user may
    # be typing them for the model they are about to switch to.
    note_for("local", "parakeet-tdt-0.6b-v3")
    assert dialog.custom_vocabulary_edit.isEnabled()
    assert dialog.custom_vocabulary_edit.isReadOnly() is False


def test_every_engine_that_uses_the_vocabulary_has_a_sentence() -> None:
    """A supported engine missing from the table would paint an empty note --
    indistinguishable from "nothing to say" on the row that decides whether
    typed terms do anything."""
    from stt_app.config import CUSTOM_VOCABULARY_ENGINES, DEFAULT_ENGINE
    from stt_app.settings_dialog_general import _VOCABULARY_SUPPORTED_NOTES

    assert set(_VOCABULARY_SUPPORTED_NOTES) == {
        DEFAULT_ENGINE,
        *CUSTOM_VOCABULARY_ENGINES,
    }
    for engine, sentence in _VOCABULARY_SUPPORTED_NOTES.items():
        assert "{name}" in sentence, engine
        assert sentence.format(name="X").strip()


def test_the_vocabulary_note_moves_nothing_below_it(dialog: SettingsDialog) -> None:
    """It changes on every engine and model switch, so it is reserved rather
    than sized to its text -- one model's two-line sentence would otherwise
    push Mode, While busy and the whole Text Insertion group down."""
    from stt_app.config import VALID_ENGINES

    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    app.processEvents()

    # One model per local runtime, plus every engine.
    selections: list[tuple[str, str]] = [
        ("local", "small"),  # faster-whisper
        ("local", "cohere-transcribe-03-2026"),  # Transformers.js
        ("local", "nemotron-3.5-asr-streaming-0.6b-int4"),  # ORT GenAI
        ("local", "parakeet-tdt-0.6b-v3"),  # onnx-asr
        ("local", "granite-speech-5.0-470m-turboctc"),  # Granite CTC
    ]
    selections.extend((engine, "") for engine in VALID_ENGINES)
    selections.append(("local", "small"))

    watched = {
        "mode": dialog.mode_combo,
        "while busy": dialog.concurrent_mode_combo,
        "paste mode": dialog.paste_mode_combo,
    }
    baseline: dict[str, int] | None = None
    note_height: int | None = None
    for engine, model in selections:
        dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData(engine))
        if model:
            index = dialog.model_combo.findData(model)
            assert index >= 0, model
            dialog.model_combo.setCurrentIndex(index)
        app.processEvents()

        positions = {
            name: widget.mapTo(dialog, QtCore.QPoint()).y()
            for name, widget in watched.items()
        }
        if baseline is None:
            baseline = positions
            note_height = dialog.vocabulary_support_label.height()
        assert positions == baseline, (engine, model)
        assert dialog.vocabulary_support_label.height() == note_height

        # And the sentence fits the area reserved for it rather than clipping.
        label = dialog.vocabulary_support_label
        required = (
            label.fontMetrics()
            .boundingRect(
                QtCore.QRect(0, 0, label.width(), 1000),
                QtCore.Qt.TextWordWrap,
                label.text(),
            )
            .height()
        )
        assert required <= label.height(), (engine, model, label.text())


def test_while_busy_choice_explains_the_previous_job(
    dialog: SettingsDialog,
) -> None:
    """The hint that sat under the combo moved into its tooltip, which already
    carried one line per choice."""
    values = [
        dialog.concurrent_mode_combo.itemData(index)
        for index in range(dialog.concurrent_mode_combo.count())
    ]
    choices = [
        dialog.concurrent_mode_combo.itemText(index)
        for index in range(dialog.concurrent_mode_combo.count())
    ]
    tooltip = dialog.concurrent_mode_combo.toolTip()

    assert values == ["insert", "insert_immediate", "history", "cancel"]
    assert all("previous" in choice.lower() for choice in choices)
    assert "press the recording hotkey again" in tooltip
    assert "previous transcription finishes" in tooltip
    assert not hasattr(dialog, "concurrent_mode_hint_label")


def test_field_hints_are_closer_to_their_control_than_the_next_field(
    dialog: SettingsDialog,
) -> None:
    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    _switch_to_tab(dialog, "Audio")
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
        ("custom", None),
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
    notes = (
        dialog.local_model_runtime_warning_label,
        dialog.remote_model_note_label,
        dialog.language_note_label,
        dialog.vocabulary_support_label,
    )
    reserved_heights = {label.minimumHeight() for label in notes}

    assert len(reserved_heights) == 1
    reserved_height = reserved_heights.pop()
    assert reserved_height <= dialog.fontMetrics().lineSpacing() * 2 + 10
    for label in notes:
        assert label.maximumHeight() == reserved_height


def test_dynamic_notes_fit_their_reserved_area(
    dialog: SettingsDialog,
) -> None:
    """At the default width and at the dialog's own minimum.

    The minimum is where the reservation is actually tight: with "The API key
    for this provider is set on the API Keys tab." appended, only the Fun-ASR
    note overflowed, and only at 581 px -- 45 px against the 42 reserved.
    """
    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    app.processEvents()
    default_width = dialog.width()

    for width in (default_width, dialog.minimumWidth()):
        dialog.resize(width, dialog.height())
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
            "custom",
        ):
            dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData(engine))
            app.processEvents()
            model_note = (
                dialog.local_model_runtime_warning_label
                if engine == "local"
                else dialog.remote_model_note_label
            )
            for label in (
                model_note,
                dialog.language_note_label,
                dialog.vocabulary_support_label,
            ):
                required_height = (
                    label.fontMetrics()
                    .boundingRect(
                        QtCore.QRect(0, 0, label.width(), 1000),
                        QtCore.Qt.TextWordWrap,
                        label.text(),
                    )
                    .height()
                )
                assert required_height <= label.height(), (
                    width,
                    engine,
                    label.text(),
                )

    dialog.resize(default_width, dialog.height())
    app.processEvents()
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
            required_height = (
                label.fontMetrics()
                .boundingRect(
                    QtCore.QRect(0, 0, label.width(), 1000),
                    QtCore.Qt.TextWordWrap,
                    label.text(),
                )
                .height()
            )
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


def test_the_eight_tab_titles_fit_the_default_width_without_scroll_arrows(
    dialog: SettingsDialog,
) -> None:
    """A title that does not fit turns the tab bar into a scrolling strip.

    `usesScrollButtons` is on, so an over-long bar does not clip -- it hides
    tabs behind two arrows, and the tab a user is told to open ("set the key
    on the API Keys tab") is then not on screen. The eight titles measure
    771 px of the 840 px the default dialog width gives the bar at 9 pt;
    they measured 797 px before the rename.
    """
    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    for _ in range(5):
        app.processEvents()

    bar = dialog.tabs.tabBar()
    needed = bar.sizeHint().width()
    available = dialog.tabs.width()

    point_size = app.font().pointSizeF()
    if point_size != 9.0:
        # Windows' "Text size" raises the application font without the DPI,
        # and the titles grow with it; the budget is a 9 pt number.
        pytest.skip(
            f"the tab-bar budget was measured at 9 pt; this session runs at "
            f"{point_size} pt and the bar needs {needed} px of {available} px"
        )
    assert dialog.width() == _DEFAULT_SETTINGS_DIALOG_SIZE.width()
    assert needed <= available, (needed, available)
    # And no arrow is actually showing: Qt's scroll buttons are tool buttons
    # parented to the bar, so an off-by-one in the arithmetic above still
    # leaves this visible.
    arrows = [
        button
        for button in bar.findChildren(QtWidgets.QToolButton)
        if button.isVisible()
    ]
    assert arrows == []
    assert bar.tabRect(bar.count() - 1).right() <= bar.width()


def test_the_model_row_says_where_downloads_and_api_keys_live(
    dialog: SettingsDialog,
) -> None:
    """Choosing the model and getting it are on different tabs.

    The Model row offers local models that may not be downloaded yet and
    remote providers that need a key, and both of those are done elsewhere --
    on tabs no longer called Local and Remote, so the row has to name them.
    """
    from stt_app.config import VALID_ENGINES, VALID_MODEL_SIZES

    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    app.processEvents()

    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("local"))
    for model in VALID_MODEL_SIZES:
        index = dialog.model_combo.findData(model)
        if index < 0:
            continue
        dialog.model_combo.setCurrentIndex(index)
        app.processEvents()
        note = dialog.local_model_runtime_warning_label.text()
        assert "Models tab" in note, (model, note)
        assert note == dialog.local_model_runtime_warning_label.toolTip()

    for engine in VALID_ENGINES:
        if engine == "local":
            continue
        dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData(engine))
        app.processEvents()
        note = dialog.remote_model_note_label.text()
        assert "API Keys tab" in note, (engine, note)
        assert note == dialog.remote_model_note_label.toolTip()
        # Said once: Azure used to name the key here as well as the endpoint,
        # which filled both reserved lines with one instruction.
        assert note.count("API Keys tab") == 1, (engine, note)


def test_audio_and_recording_tab_hosts_capture_settings(
    dialog: SettingsDialog,
) -> None:
    """The capture setup moved off the first tab into its own.

    Transcription keeps what changes during daily dictation (engine/model,
    insertion); microphone, VAD, tones, and recordings live on the Audio tab,
    after Transcription and Hotkeys & Display.
    """
    titles = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
    assert titles[:5] == [
        "Transcription",
        "Hotkeys && Display",
        "Audio",
        "Models",
        "API Keys",
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
    _switch_to_tab(dialog, "Transcription")
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
    _switch_to_tab(dialog, "Audio")
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
    monkeypatch.setattr(
        "stt_app.audio_devices.query_input_devices", lambda: ([], False)
    )

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
    _switch_to_tab(dialog, "Audio")
    dialog.benchmark_window.show()
    app.processEvents()

    def _check(rows) -> None:
        for field, *buttons in rows:
            for button in buttons:
                assert button.property("inlineFieldButton") is True
                assert button.height() == field.height(), (
                    button.text(),
                    button.height(),
                    field.height(),
                )
                # The stylesheet minimum must fit inside the matched height,
                # or the style would draw the button clipped at the bottom.
                assert button.minimumSizeHint().height() <= field.height(), (
                    button.text()
                )

    _check(
        (
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
    )
    # Real heights exist only for a shown page, so the Model Dir row is
    # measured on its own tab.
    _switch_to_tab(dialog, "Models")
    app.processEvents()
    _check(((dialog.model_dir_edit, dialog.model_dir_browse),))

    dialog.benchmark_window.hide()


def test_microphone_refresh_requests_controller_reenumeration(
    dialog: SettingsDialog,
    monkeypatch,
) -> None:
    monkeypatch.setattr("stt_app.audio_devices.query_input_devices", lambda: ([], True))
    requests: list[bool] = []
    dialog.audio_device_refresh_requested.connect(lambda: requests.append(True))

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
    changes enabled state and note text. It sits in the Models tab's Local
    runtime group now, so what must not move is the checkbox under it."""
    dialog.show()
    _switch_to_tab(dialog, "Models")

    def probe(engine: str, model: str | None) -> tuple[bool, int]:
        index = dialog.engine_combo.findData(engine)
        assert index >= 0
        dialog.engine_combo.setCurrentIndex(index)
        if model is not None:
            model_index = dialog.model_combo.findData(model)
            assert model_index >= 0, model
            dialog.model_combo.setCurrentIndex(model_index)
        QtWidgets.QApplication.processEvents()
        below_y = _position_in_dialog(
            dialog.keep_onnx_model_loaded_checkbox,
            dialog.keep_onnx_model_loaded_checkbox.rect().topLeft(),
            dialog,
        ).y()
        return dialog.local_onnx_device_combo.isEnabled(), below_y

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
    assert "detects the language itself" in note_for("parakeet-tdt-0.6b-v3-ultra")


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
    _switch_to_tab(dialog, "Models")
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


@pytest.mark.parametrize("downloaded", [1, 10, 13])
def test_the_model_popup_paints_no_empty_strip_under_the_last_model(
    dialog: SettingsDialog,
    downloaded: int,
) -> None:
    """Scrolled to the bottom, the last model must touch the popup's edge.

    `QComboBox::showPopup` sizes the popup by summing the first
    `maxVisibleItems` entries' heights, and a separator measures
    `PM_DefaultFrameWidth` (2 px) against a model row's 30 px; QListView's
    default `ScrollPerItem` then scrolls whole entries and paints the
    viewport's leftover height as empty space below the last one. Measured
    before the fix, with the dialog's own 30 px rows: 2 px of blank viewport
    with 1 to 4 models downloaded and 28 px -- a whole row -- with 10 to 13,
    where the separator falls outside the first ten entries so the popup is a
    full 300 px while its last ten entries measure 272 px.
    """
    from stt_app.config import VALID_MODEL_SIZES

    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    app.processEvents()

    dialog._refresh_model_combo(cached=list(VALID_MODEL_SIZES[:downloaded]))
    app.processEvents()
    combo = dialog.model_combo

    # The grouping this popup exists for is still there: checked models on
    # top, a separator, indented ones below.
    texts = [combo.itemText(index) for index in range(combo.count())]
    assert texts[0].startswith("✓")
    assert [text.strip() for text in texts].count("") == 1
    assert texts[-1].startswith("   ")

    combo.showPopup()
    app.processEvents()
    try:
        view = combo.view()
        scrollbar = view.verticalScrollBar()
        # Without scrolling there is nothing to reach the bottom of.
        assert scrollbar.maximum() > 0
        scrollbar.setValue(scrollbar.maximum())
        app.processEvents()

        last = combo.model().index(combo.count() - 1, combo.modelColumn())
        bottom = view.visualRect(last).bottom()
        empty_below = view.viewport().height() - 1 - bottom
        assert empty_below == 0, (
            f"{empty_below} px of empty popup below the last model "
            f"(viewport {view.viewport().height()} px, last row ends at {bottom})"
        )
    finally:
        combo.hidePopup()
        app.processEvents()


# --- Phase 1 of the 2026-09-27 UX review ------------------------------------


class _AppFont:
    """Raise the application font the way Windows' "Text size" does."""

    def __init__(self, point_size: float) -> None:
        self._point_size = point_size
        self._previous: QtGui.QFont | None = None

    def __enter__(self) -> QtWidgets.QApplication:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self._previous = QtGui.QFont(app.font())
        font = QtGui.QFont(app.font())
        font.setPointSizeF(self._point_size)
        app.setFont(font)
        return app

    def __exit__(self, *_exc: object) -> None:
        app = QtWidgets.QApplication.instance()
        if app is not None and self._previous is not None:
            app.setFont(self._previous)


def _dialog_at(monkeypatch, tmp_path) -> SettingsDialog:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models", lambda _model_dir: []
    )
    return SettingsDialog(
        settings_store=_SettingsStore(),
        secret_store=_SecretStore(),
        app_logger=_Logger(),
    )


def _settle_layout(app: QtWidgets.QApplication) -> None:
    for _ in range(3):
        QtTest.QTest.qWait(30)
        app.processEvents()


@pytest.mark.parametrize("point_size", [9.0, 11.25, 13.5])
def test_nothing_scrolls_sideways_or_hides_a_tab_at_the_minimum_width(
    point_size: float, monkeypatch, tmp_path
) -> None:
    """The minimum width covers the tab bar and the widest settings page.

    It used to follow the tab widget's own hint, which a `QScrollArea` answers
    with a fixed 58 px whatever it holds, so at the 611 px minimum four tabs
    scrolled sideways (Models by 124 px at 9 pt) and the tab bar, which needs
    771 px at 9 pt, hid tabs behind its scroll arrows. And a fixed-width
    Browse or Clear button cut its own caption off at larger text sizes.
    """
    with _AppFont(point_size) as app:
        dialog = _dialog_at(monkeypatch, tmp_path)
        try:
            dialog.show()
            _settle_layout(app)
            available = dialog._available_dialog_size().width()
            if dialog.minimumWidth() >= available:
                pytest.skip(
                    f"this screen is {available} px wide, and the dialog needs "
                    f"{dialog.minimumWidth()} px at {point_size} pt"
                )
            dialog.resize(dialog.minimumWidth(), dialog.height())
            _settle_layout(app)
            assert dialog.width() == dialog.minimumWidth()

            bar = dialog.tabs.tabBar()
            arrows = [
                button
                for button in bar.findChildren(QtWidgets.QToolButton)
                if button.isVisible()
            ]
            assert arrows == [], "the tab bar scrolls at the minimum width"
            for index in range(bar.count()):
                assert bar.tabRect(index).width() >= bar.tabSizeHint(index).width(), (
                    bar.tabText(index)
                )

            for index in range(dialog.tabs.count()):
                dialog.tabs.setCurrentIndex(index)
                _settle_layout(app)
                page = dialog.tabs.widget(index)
                title = dialog.tabs.tabText(index)
                if isinstance(page, QtWidgets.QScrollArea):
                    assert page.horizontalScrollBar().maximum() == 0, (
                        title,
                        page.horizontalScrollBar().maximum(),
                    )
                else:
                    assert page.minimumSizeHint().width() <= page.width(), title
                for button in page.findChildren(QtWidgets.QPushButton):
                    if not button.isVisible() or not button.text():
                        continue
                    assert button.width() >= button.sizeHint().width(), (
                        title,
                        button.text(),
                        button.width(),
                        button.sizeHint().width(),
                    )
        finally:
            dialog.close()
            dialog.deleteLater()
            app.processEvents()


def test_the_full_final_checkbox_is_enabled_only_where_it_acts(
    monkeypatch, tmp_path
) -> None:
    """It re-transcribes a local faster-whisper stream at its end, so it is
    enabled for exactly that: local engine, a faster-whisper model, streaming
    mode -- and follows each of the three as it changes."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = _dialog_at(monkeypatch, tmp_path)
    check = dialog.streaming_full_final_check

    def pick(combo: QtWidgets.QComboBox, value: str) -> None:
        index = combo.findData(value)
        assert index >= 0, value
        combo.setCurrentIndex(index)

    try:
        pick(dialog.engine_combo, "local")
        pick(dialog.model_combo, "small")
        pick(dialog.mode_combo, "batch")
        assert check.isEnabled() is False
        pick(dialog.mode_combo, "streaming")
        assert check.isEnabled() is True
        pick(dialog.model_combo, "nemotron-3.5-asr-streaming-0.6b-int4")
        assert check.isEnabled() is False
        pick(dialog.model_combo, "small")
        assert check.isEnabled() is True
        pick(dialog.engine_combo, "deepgram")
        assert check.isEnabled() is False
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_an_unbroken_word_in_a_status_line_does_not_widen_the_dialog(
    monkeypatch, tmp_path
) -> None:
    """No wrapped label on any page may set the dialog's minimum width.

    A word-wrapped label reports its longest word as its minimum width, the
    pin reads every page's minimum and only ever raises, so one path in the
    Import tab's "Selected:" line (1538 px for 346 characters), a URL in a
    provider's error or a folder in a download failure kept the dialog that
    wide for the life of the app. Every wrapped label on every page is given
    such a word here, one page at a time, with that page on screen.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = _dialog_at(monkeypatch, tmp_path)
    unbroken = "C:\\" + "\\".join(["averyveryverylongfoldername"] * 12) + "\\a.wav"
    try:
        dialog.show()
        _settle_layout(app)
        before = dialog.minimumWidth()
        changed = 0
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            page = dialog.tabs.widget(index)
            for label in page.findChildren(QtWidgets.QLabel):
                if label.wordWrap():
                    label.setText(unbroken)
                    changed += 1
            _settle_layout(app)
            dialog._pin_content_minimum_width()
            assert dialog.minimumWidth() == before, dialog.tabs.tabText(index)
        assert changed > 20
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_a_status_line_that_can_cut_off_a_path_shows_it_whole_on_hover(
    monkeypatch, tmp_path
) -> None:
    """The three status lines that report paths and provider errors are cut
    at the label's edge like every wrapped label; their tooltip holds the whole
    message."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = _dialog_at(monkeypatch, tmp_path)
    message = "Could not write C:\\" + "\\".join(["averyveryverylongfoldername"] * 5)
    try:
        for label in (
            dialog.local_models_action_label,
            dialog.key_storage_status_label,
            dialog.test_conn_result,
        ):
            label.setText(message)
            assert label.toolTip() == message
            label.setText("")
            assert label.toolTip() == ""
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_the_dialog_opens_as_wide_as_it_needs_and_does_not_grow_after(
    monkeypatch, tmp_path
) -> None:
    """The pin runs before the first show, so the window does not open at one
    width and jump to another a moment later."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = _dialog_at(monkeypatch, tmp_path)
    try:
        minimum_before_show = dialog.minimumWidth()
        width_before_show = dialog.width()
        assert width_before_show >= minimum_before_show
        dialog.show()
        _settle_layout(app)
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            _settle_layout(app)
        assert dialog.minimumWidth() == minimum_before_show
        assert dialog.width() == width_before_show
    finally:
        dialog.close()
        app.processEvents()


def test_unlabelled_form_rows_carry_no_blank_line(dialog: SettingsDialog) -> None:
    """A checkbox row added with an empty label string kept the height its
    hint needs at 460 px, whatever width the hint really had: a
    `QFormLayout` row without a label widget does not honour height-for-width.
    Measured on the Transcription and Audio tabs: 15 px of blank line under
    four hints."""
    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    _settle_layout(app)
    for title in ("Transcription", "Hotkeys && Display", "Audio", "Models"):
        _switch_to_tab(dialog, title)
        _settle_layout(app)
        page = dialog.tabs.currentWidget()
        for label in page.findChildren(QtWidgets.QLabel):
            if not label.property("fieldHint") or not label.isVisible():
                continue
            if label.minimumHeight() == label.maximumHeight():
                continue  # a reserved note: its height is chosen, not wrapped
            needed = label.heightForWidth(label.width())
            assert label.height() <= needed, (
                title,
                label.text()[:50],
                label.height(),
                needed,
            )


def test_hint_text_follows_the_system_text_size(monkeypatch, tmp_path) -> None:
    """Hints were `font-size: 11px`, a pixel size: at 13.5 pt every control
    grew by half and the hints did not grow at all."""
    with _AppFont(13.5) as app:
        dialog = _dialog_at(monkeypatch, tmp_path)
        try:
            expected = 13.5 * 0.92
            widgets = [dialog, *dialog.findChildren(QtWidgets.QWidget)]
            for widget in widgets:
                assert "font-size" not in widget.styleSheet(), (
                    type(widget).__name__,
                    widget.styleSheet(),
                )
            for label in (
                dialog.language_note_label,
                dialog.local_model_runtime_warning_label,
                dialog.vocabulary_support_label,
                dialog.local_onnx_device_note_label,
                dialog._provider_last_test_labels["openai"],
                dialog.local_models_scan_status_label,
            ):
                assert label.font().pointSizeF() == pytest.approx(expected), (
                    label.text()[:40]
                )
            # And the reservation grew with the font, so two lines still fit.
            label = dialog.language_note_label
            assert label.minimumHeight() >= label.fontMetrics().lineSpacing() * 2
        finally:
            dialog.close()
            app.processEvents()


def test_the_onnx_device_lives_with_the_local_runtime_on_the_models_tab(
    dialog: SettingsDialog,
) -> None:
    models_page = dialog.tabs.widget(dialog._local_tab_index)
    general_page = dialog.tabs.widget(0)
    assert models_page.isAncestorOf(dialog.local_onnx_device_combo)
    assert not general_page.isAncestorOf(dialog.local_onnx_device_combo)
    group = dialog.local_runtime_box
    assert group.title() == "Local runtime"
    assert group.isAncestorOf(dialog.local_onnx_device_combo)
    assert group.isAncestorOf(dialog.keep_onnx_model_loaded_checkbox)


def test_the_onnx_device_note_names_what_decides_for_a_cloud_engine(
    dialog: SettingsDialog,
) -> None:
    """For a remote engine the note used to depend on whichever local model
    the hidden combo still held -- "This model always runs on the CPU" about
    a model that was not going to run at all."""
    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("local"))
    dialog.model_combo.setCurrentIndex(
        dialog.model_combo.findData("parakeet-tdt-0.6b-v3")
    )
    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("openai"))

    assert dialog.local_onnx_device_note_label.text() == "Only used by local models."
    assert dialog.local_onnx_device_combo.isEnabled() is False


def test_the_onnx_device_note_names_the_selected_model(dialog: SettingsDialog) -> None:
    """The row now sits on another tab than the model picker, so its note says
    which model it is talking about."""
    from stt_app.settings_dialog_helpers import local_model_short_label

    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("local"))
    checked = 0
    for model in ("small", "parakeet-tdt-0.6b-v3", "granite-speech-4.1-2b"):
        index = dialog.model_combo.findData(model)
        if index < 0:
            continue
        dialog.model_combo.setCurrentIndex(index)
        note = dialog.local_onnx_device_note_label.text()
        assert local_model_short_label(model) in note, (model, note)
        checked += 1
    assert checked == 3


def test_the_concurrency_choice_is_called_while_busy_everywhere(
    dialog: SettingsDialog,
) -> None:
    general_tab = dialog.tabs.widget(0)
    labels = {label.text() for label in general_tab.findChildren(QtWidgets.QLabel)}

    assert "While busy" in labels
    assert "New Recording" not in labels
    assert "While transcribing" not in labels
    assert dialog.concurrent_mode_combo.toolTip().startswith("While busy:")


def test_the_models_note_shows_one_ampersand(dialog: SettingsDialog) -> None:
    """A QLabel without a buddy shows "&&" as two characters."""
    text = dialog.local_active_model_note.text()
    assert "Engine & Mode" in text
    assert "&&" not in text


def test_the_downloaded_models_line_is_plain_text(
    dialog: SettingsDialog,
) -> None:
    """Qt's rich text has no border-radius or padding on a span, so the "tag
    badges" rendered as bare settings ids run together."""
    dialog._refresh_local_models_label(cached=["tiny", "parakeet-tdt-0.6b-v3"])
    label = dialog.local_models_label

    assert label.textFormat() == QtCore.Qt.PlainText
    assert "<span" not in label.text()
    assert "tiny" in label.text()
    assert "NVIDIA Parakeet TDT 0.6B v3" in label.text()


def test_the_auto_stop_hint_names_the_silence_it_waits_for(
    dialog: SettingsDialog,
) -> None:
    from stt_app.config import VAD_MAX_SILENCE_MS

    seconds = f"{VAD_MAX_SILENCE_MS / 1000:g} s"
    assert seconds in dialog.vad_hint_label.text()
    assert "configured silence period" not in dialog.vad_hint_label.text()


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_download_all_missing_asks_first_and_names_the_size(
    dialog: SettingsDialog, monkeypatch, answer: str
) -> None:
    from stt_app.config import MODEL_ESTIMATED_SIZE_MB

    missing = ["small", "medium"]
    monkeypatch.setattr(dialog, "_missing_downloadable_models", lambda: list(missing))
    started: list[list[str]] = []
    monkeypatch.setattr(
        dialog,
        "_start_local_model_download",
        lambda models, *args, **kwargs: started.append(list(models)),
    )
    asked: list[str] = []

    def _question(_parent, _title, text, *_args, **_kwargs):
        asked.append(text)
        return (
            QtWidgets.QMessageBox.Yes if answer == "yes" else QtWidgets.QMessageBox.No
        )

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", _question)

    dialog._download_all_missing_local_models()

    total_gb = sum(MODEL_ESTIMATED_SIZE_MB[name] for name in missing) / 1000
    assert len(asked) == 1
    assert "2 models" in asked[0]
    assert f"{total_gb:.1f} GB" in asked[0]
    assert started == ([missing] if answer == "yes" else [])


def test_the_microphone_picker_names_the_device_system_default_means(
    dialog: SettingsDialog, monkeypatch
) -> None:
    from stt_app.audio_devices import InputDeviceInfo

    monkeypatch.setattr(
        "stt_app.audio_devices.query_input_devices",
        lambda: ([InputDeviceInfo(name="USB Mic", index=3)], True),
    )
    monkeypatch.setattr(
        "stt_app.audio_devices.system_default_input_name", lambda: "USB Mic"
    )
    dialog._populate_microphone_combo("")
    assert dialog.microphone_combo.itemText(0) == "System default: USB Mic"
    assert dialog.microphone_combo.itemData(0) == ""

    monkeypatch.setattr("stt_app.audio_devices.system_default_input_name", lambda: "")
    dialog._populate_microphone_combo("")
    assert dialog.microphone_combo.itemText(0) == "System default (follow Windows)"


def test_the_engine_picker_groups_local_and_cloud(dialog: SettingsDialog) -> None:
    from stt_app.config import VALID_ENGINES

    combo = dialog.engine_combo
    texts = [combo.itemText(index) for index in range(combo.count())]
    assert texts[0] == "Local (on this PC)"
    header_index = texts.index("Cloud")
    header = combo.model().item(header_index)
    assert not header.isEnabled()
    assert not (header.flags() & QtCore.Qt.ItemIsSelectable)
    for engine in VALID_ENGINES:
        assert combo.findData(engine) >= 0, engine
    assert all(not text.startswith("Remote (") for text in texts)
    # The same list on the Import Audio tab.
    import_texts = [
        dialog.import_engine_combo.itemText(index)
        for index in range(dialog.import_engine_combo.count())
    ]
    assert import_texts == texts


def test_hotkey_fields_are_as_wide_as_a_hotkey(dialog: SettingsDialog) -> None:
    """A single chord stretched across 500 px reads as a text field."""
    app = QtWidgets.QApplication.instance()
    assert app is not None
    dialog.show()
    _switch_to_tab(dialog, "Hotkeys && Display")
    _settle_layout(app)
    longest = dialog.hotkey_edit.fontMetrics().horizontalAdvance(
        "Ctrl+Shift+Alt+Win+F12"
    )
    for edit in (
        dialog.hotkey_edit,
        dialog.cancel_hotkey_edit,
        dialog.show_overlay_hotkey_edit,
        dialog.repaste_hotkey_edit,
    ):
        assert longest < edit.width() <= longest + 120, edit.width()


@pytest.mark.parametrize(
    ("engine", "model", "mode", "enabled"),
    [
        ("local", "small", "streaming", True),
        ("local", "small", "batch", False),
        ("local", "parakeet-tdt-0.6b-v3", "batch", False),
        ("groq", "small", "batch", False),
    ],
)
def test_the_full_final_checkbox_opens_enabled_only_where_it_acts(
    monkeypatch, tmp_path, engine: str, model: str, mode: str, enabled: bool
) -> None:
    """Its hint moved into the tooltip on the promise that the box is disabled
    wherever it does nothing -- which has to hold from the first paint, not
    only after the user touches engine, model or mode: the model combo is
    refilled with its signals blocked, and selecting the value a combo already
    shows emits nothing."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models", lambda _model_dir: []
    )
    stored = AppSettings(engine=engine, model_size=model, mode=mode)
    dialog = SettingsDialog(
        settings_store=_SettingsStore(stored),
        secret_store=_SecretStore(),
        app_logger=_Logger(),
    )
    try:
        assert dialog.mode_combo.currentData() == mode
        assert dialog.streaming_full_final_check.isEnabled() is enabled
    finally:
        dialog.close()
        app.processEvents()
