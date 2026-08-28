from __future__ import annotations

from pathlib import Path

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from stt_app import history_dialog as history_dialog_module
from stt_app.history_dialog import HistoryDialog
from stt_app.retranscribe_dialog import RetranscribeDialog
from stt_app.settings_dialog_helpers import (
    LOCAL_MODEL_LABELS,
    local_model_short_label,
    model_choices_for_engine,
)
from stt_app.settings_store import AppSettings, SettingsStore
from stt_app.transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore

_GROQ_CHOICES = model_choices_for_engine("groq")


class _FakeController:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.calls: list[tuple[str, AppSettings]] = []

    def transcribe_audio_file(
        self,
        path,
        settings_override=None,
        progress_callback=None,
    ):
        self.calls.append((str(path), settings_override))
        return True, "retranscribed text"


@pytest.fixture(autouse=True)
def _close_top_level_windows_after_test():
    yield
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    for widget in list(app.topLevelWidgets()):
        widget.close()
        widget.deleteLater()
    app.processEvents()


def _entry(**kwargs) -> TranscriptHistoryEntry:
    return TranscriptHistoryEntry.new(
        text=kwargs.pop("text", "hallo welt"),
        engine=kwargs.pop("engine", "local"),
        model=kwargs.pop("model", "small"),
        mode=kwargs.pop("mode", "batch"),
        **kwargs,
    )


def _make_dialog(tmp_path, entries, *, controller=None, recordings_dir=""):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save(list(entries))
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(
        AppSettings(history_max_items=20, recordings_dir=str(recordings_dir))
    )
    return HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
        controller=controller,
    )


def test_audio_actions_are_enabled_for_an_entry_with_retained_audio(tmp_path):
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"RIFF")
    controller = _FakeController(AppSettings())

    dialog = _make_dialog(
        tmp_path,
        [_entry(source_audio_path=str(audio))],
        controller=controller,
    )

    assert dialog._show_audio_button.isEnabled()
    assert dialog._retranscribe_button.isEnabled()


def test_audio_actions_are_disabled_when_the_recording_is_gone(tmp_path):
    controller = _FakeController(AppSettings())

    dialog = _make_dialog(
        tmp_path,
        [_entry(source_audio_path=str(tmp_path / "deleted.wav"))],
        controller=controller,
    )

    assert not dialog._show_audio_button.isEnabled()
    assert not dialog._retranscribe_button.isEnabled()


def test_retranscribe_stays_disabled_without_a_controller(tmp_path):
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"RIFF")

    dialog = _make_dialog(tmp_path, [_entry(source_audio_path=str(audio))])

    # Revealing the file needs no transcription lane, retranscribing does.
    assert dialog._show_audio_button.isEnabled()
    assert not dialog._retranscribe_button.isEnabled()


def test_selecting_multiple_entries_disables_the_single_audio_actions(tmp_path):
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"RIFF")
    controller = _FakeController(AppSettings())
    dialog = _make_dialog(
        tmp_path,
        [
            _entry(text="one", source_audio_path=str(audio)),
            _entry(text="two", source_audio_path=str(audio)),
        ],
        controller=controller,
    )

    dialog._select_rows([0, 1])
    dialog._on_selection_changed()

    assert not dialog._show_audio_button.isEnabled()
    assert not dialog._retranscribe_button.isEnabled()


def test_ctrl_c_copies_every_selected_transcript(monkeypatch, tmp_path):
    """Ctrl+C must match "Copy selected", not yield only the current cell."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    copied: list[str] = []
    monkeypatch.setattr(
        QtGui.QGuiApplication,
        "clipboard",
        lambda: type(
            "C",
            (),
            {
                "setText": staticmethod(copied.append),
                "text": staticmethod(lambda: ""),
            },
        )(),
    )
    dialog = _make_dialog(
        tmp_path,
        [_entry(text="one"), _entry(text="two"), _entry(text="three")],
    )
    dialog.show()
    app.processEvents()
    dialog._select_rows([0, 1, 2])
    dialog._on_selection_changed()

    dialog._table.setFocus()
    # The dialog installs a QtGui.QShortcut on the table.
    dialog._copy_shortcut.activated.emit()
    app.processEvents()

    assert copied, "Ctrl+C produced no clipboard write"
    assert copied[-1].count("one") == 1
    assert copied[-1].count("two") == 1
    assert copied[-1].count("three") == 1


def test_show_audio_file_reveals_the_recording(monkeypatch, tmp_path):
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"RIFF")
    revealed: list[str] = []
    monkeypatch.setattr(
        history_dialog_module,
        "reveal_path_in_file_manager",
        lambda path: revealed.append(str(path)) or True,
    )
    dialog = _make_dialog(
        tmp_path,
        [_entry(source_audio_path=str(audio))],
        controller=_FakeController(AppSettings()),
    )

    dialog._show_selected_audio_file()

    assert revealed == [str(audio)]


def test_recordings_folder_button_opens_the_configured_directory(
    monkeypatch,
    tmp_path,
):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    opened: list[str] = []
    monkeypatch.setattr(
        history_dialog_module,
        "open_directory",
        lambda path: opened.append(str(path)) or True,
    )
    dialog = _make_dialog(tmp_path, [_entry()], recordings_dir=recordings)

    dialog._open_recordings_folder()

    assert opened == [str(recordings)]


def test_recordings_folder_button_reports_a_missing_directory(
    monkeypatch,
    tmp_path,
):
    opened: list[str] = []
    monkeypatch.setattr(
        history_dialog_module,
        "open_directory",
        lambda path: opened.append(str(path)) or True,
    )
    warned: list[str] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "information",
        staticmethod(lambda *args, **kwargs: warned.append(str(args[2]))),
    )
    dialog = _make_dialog(
        tmp_path,
        [_entry()],
        recordings_dir=tmp_path / "never-created",
    )

    dialog._open_recordings_folder()

    assert opened == []
    assert warned and "never-created" in warned[0]


def test_retranscribe_dialog_uses_the_chosen_language_and_entry_model(tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"RIFF")
    entry = _entry(engine="local", model="large-v3-turbo")

    dialog = RetranscribeDialog(
        entry=entry,
        audio_path=audio,
        base_settings=AppSettings(
            engine="groq",
            model_size="small",
            language_mode="de",
        ),
        transcribe=lambda *args, **kwargs: (True, ""),
    )
    index = dialog._language_combo.findData("en")
    assert index >= 0
    dialog._language_combo.setCurrentIndex(index)

    settings = dialog.build_settings()

    # The entry's own transcriber is reused; only the language changes.
    assert settings.engine == "local"
    assert settings.model_size == "large-v3-turbo"
    assert settings.language_mode == "en"


def test_retranscribe_dialog_says_when_the_entrys_model_is_gone(tmp_path):
    """Retranscribing exists to repeat a run, so a substitution must be visible.

    Granite 4.1 Plus and NAR were removed on 2026-08-26. Their history entries
    still offer Retranscribe, and the picker silently fell back to another
    model -- producing a transcript that looks like a repeat of the entry but
    came from a different model.
    """
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"RIFF")
    entry = _entry(engine="local", model="granite-speech-4.1-2b-nar")

    dialog = RetranscribeDialog(
        entry=entry,
        audio_path=audio,
        base_settings=AppSettings(engine="local", model_size="small"),
        transcribe=lambda *args, **kwargs: (True, ""),
    )

    assert dialog._model_combo.findData("granite-speech-4.1-2b-nar") < 0
    note = dialog._language_note.text()
    # The recognisable name, not the raw settings id -- the note is prose the
    # user reads, and the id matches nothing on screen.
    assert local_model_short_label("granite-speech-4.1-2b-nar") in note
    assert "granite-speech-4.1-2b-nar" not in note
    assert "no longer offers" in note
    assert local_model_short_label(dialog.selected_model()) in note


def test_retranscribe_dialog_says_nothing_when_the_entrys_model_is_available(
    tmp_path,
):
    """The other half: the note must stay empty for an ordinary repeat."""
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"RIFF")

    dialog = RetranscribeDialog(
        entry=_entry(engine="local", model="large-v3-turbo"),
        audio_path=audio,
        base_settings=AppSettings(engine="local", model_size="small"),
        transcribe=lambda *args, **kwargs: (True, ""),
    )

    assert dialog._model_combo.findData("large-v3-turbo") >= 0
    assert dialog._language_note.text() == ""


def test_the_retranscribe_note_never_moves_the_widgets_below_it(tmp_path):
    """The note's reserved area must cover its longest possible text.

    It can carry a retired-model substitution *and* the Canary language
    warning at once. Two reserved lines measured 38 px against the 45 px that
    combination needs, so changing the model moved the buttons below by 7 px.
    Layout shift is a hard defect in this project, so the reservation is
    pinned here rather than left to whatever the current wording happens to
    need.
    """
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"RIFF")

    heights = {}
    for label, entry_model, current_model in (
        ("empty", "large-v3-turbo", "small"),
        ("retired", "granite-speech-4.1-2b-nar", "small"),
        ("canary", "canary-1b-v2", "canary-1b-v2"),
        ("retired+canary", "granite-speech-4.1-2b-nar", "canary-1b-v2"),
    ):
        dialog = RetranscribeDialog(
            entry=_entry(engine="local", model=entry_model),
            audio_path=audio,
            base_settings=AppSettings(engine="local", model_size=current_model),
            transcribe=lambda *args, **kwargs: (True, ""),
        )
        dialog.show()
        QtWidgets.QApplication.processEvents()
        note = dialog._language_note
        heights[label] = note.height()
        # Nothing may be cut off either: a reservation that hides half the
        # warning is not better than one that moves the layout.
        if note.text():
            assert note.heightForWidth(note.width()) <= note.height(), label
        dialog.close()

    assert heights["retired+canary"] > 0
    assert len(set(heights.values())) == 1, heights


def test_the_retranscribe_note_is_not_clipped_at_the_dialog_minimum_width():
    """The reservation is a line count; the note's real height is a wrap.

    The dialog is resizable down to `_MINIMUM_SIZE` and has a size grip, so
    the reserved three lines are only enough at the width they were measured
    at. Narrower, the worst-case note needs more, and what falls off the
    bottom is the *last* sentence -- the Canary warning, whose absence costs a
    translated transcript rather than a cosmetic clip. The note therefore
    carries `heightForWidth`, which this pins at the narrowest size the user
    can actually drag the dialog to.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(engine="local", model="granite-speech-4.1-2b-nar"),
        audio_path=Path(__file__),
        base_settings=AppSettings(engine="local", model_size="canary-1b-v2"),
        transcribe=lambda *args, **kwargs: (True, ""),
    )
    try:
        dialog.show()
        app.processEvents()
        note = dialog._language_note
        assert "Canary" in note.text(), note.text()

        for width in (dialog.width(), 600, dialog.minimumWidth()):
                dialog.resize(width, dialog.height())
                app.processEvents()
                assert note.heightForWidth(note.width()) <= note.height(), (
                    f"at dialog width {width} the note needs "
                    f"{note.heightForWidth(note.width())} px but has "
                    f"{note.height()}"
                )
    finally:
        dialog.close()


def test_the_retranscribe_note_does_not_move_the_layout_at_any_width():
    """Changing the model must not move anything below the note.

    A line count only covers the width it was measured at. At 560 px the
    worst-case note needs 60 px against the 54 px three lines reserve, so
    changing the model there used to move everything below it. The
    reservation is therefore re-measured from the longest note *this* dialog
    can compose, at the live width.

    Per dialog, not across dialogs: the note names the entry's own model, and
    `local_model_short_label` returns an unrecognised id verbatim, so a
    History import can make one entry's worst case legitimately taller than
    another's. What must never change is the height *within* one dialog as
    the user works the model picker.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialogs = []
    try:
        for entry_model in (
            "large-v3-turbo",
            "granite-speech-4.1-2b-nar",
            # An id no label covers, which is what a History import can carry.
            # Measured against a worst case built only from `LOCAL_MODEL_LABELS`:
            # 60 px reserved for a note that took 75 at the dialog's own
            # minimum width.
            "granite-speech-4.1-2b-plus-experimental-int8-preview-build-2026",
            # Shorter than the longest label (30 chars) but far wider drawn.
            # A character-count worst case picks the label and under-reserves
            # by ~15 px, which is the whole magnitude this test exists to
            # catch -- the length axis alone cannot see it.
            "W" * 29,
        ):
            dialog = RetranscribeDialog(
                entry=_entry(engine="local", model=entry_model),
                audio_path=Path(__file__),
                base_settings=AppSettings(engine="local", model_size="canary-1b-v2"),
                transcribe=lambda *args, **kwargs: (True, ""),
            )
            dialog.show()
            app.processEvents()
            dialogs.append(dialog)

        # The last width is below each dialog's own minimum on purpose: the
        # reservation has to be right for whatever width the widget is given,
        # and only a width narrower than the one it was shown at exercises the
        # resize-time re-measurement rather than the show-time one.
        for dialog in dialogs:
            dialog.setMinimumWidth(320)
        for dialog in dialogs:
            entry_model = dialog._entry_model
            note = dialog._language_note
            for width in (dialog.width(), 600, 560, 320):
                dialog.resize(width, dialog.height())
                app.processEvents()
                heights = []
                # Every model the user can pick, which is what changes the
                # note's second name and whether it appears at all.
                for index in range(dialog._model_combo.count()):
                    dialog._model_combo.setCurrentIndex(index)
                    app.processEvents()
                    heights.append(note.height())
                    assert note.heightForWidth(note.width()) <= note.height(), (
                        f"{entry_model} at width {width}, model "
                        f"{dialog.selected_model()}: the note needs "
                        f"{note.heightForWidth(note.width())} px but has "
                        f"{note.height()}"
                    )
                assert len(set(heights)) == 1, (
                    f"{entry_model} at dialog width {width}: picking a "
                    f"different model changed the note height across "
                    f"{sorted(set(heights))}, so everything below it moved"
                )
    finally:
        for dialog in dialogs:
            dialog.close()


def test_the_reserved_note_height_covers_every_substitutable_model_name():
    """The reservation must cover the worst case, not a guess at which it is.

    Two shortcuts were tried and both under-reserved, because the quantity
    being reserved is a *wrapped* height:

    * `key=len` -- character count is not drawn width. A 29-character run of
      `W` draws wider than the 30-character longest label.
    * `key=horizontalAdvance` -- drawn width is not wrapped height, and a key
      is only sound if it orders the quantity being maximised. It does not:
      two names one pixel apart can fall on opposite sides of a line break.
      With the names shipping today no under-reservation from this key could
      be produced at any dialog width from 200 to 1100, so it is guarded
      against rather than caught in the act.

    Measuring every candidate through the polished label is the only key that
    cannot be wrong, and at 1.26 ms per cache miss it is too cheap to trade
    for a proxy that can be. This asserts that directly: whatever the dialog
    reserved, no candidate may exceed it.

    Two ways of measuring this give wrong answers, and both were used in
    earlier versions of this docstring:

    * Querying `note.heightForWidth(w)` while the label is *not* w wide.
      `QLabel.heightForWidth` is not a pure function of its argument --
      measured, the identical call returned 60 px with the label 556 px wide
      and 90 px with it 476 px wide. The loop below resizes the dialog before
      every measurement for exactly this reason.
    * Measuring through a stand-in `QLabel` given the same stylesheet. It
      wraps differently again, and produced a `Plus`-vs-`Nemotron` inversion
      that does not occur in this dialog at any width.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(engine="local", model="granite-speech-4.1-2b-nar"),
        audio_path=Path(__file__),
        base_settings=AppSettings(engine="local", model_size="canary-1b-v2"),
        transcribe=lambda *args, **kwargs: (True, ""),
    )
    try:
        dialog.show()
        app.processEvents()
        note = dialog._language_note
        assert dialog._worst_case_notes, "no candidates were built"

        dialog.setMinimumWidth(320)
        # Each iteration resizes first, so `note.width()` is the width the
        # measurement is taken at. 399 and 320 are below the shipped
        # `_MINIMUM_SIZE`, reachable only because of the `setMinimumWidth`
        # above; they are here to widen the sweep, not because any known
        # shortcut fails there.
        for width in (dialog.width(), 640, 600, 560, 399, 320):
            dialog.resize(width, dialog.height())
            app.processEvents()
            reserved = note.minimumHeight()
            original = note.text()
            try:
                for candidate in dialog._worst_case_notes:
                    note.setText(candidate)
                    needed = note.heightForWidth(note.width())
                    assert needed <= reserved, (
                        f"at dialog width {width} the note reserves {reserved} px "
                        f"but this substitution needs {needed} px: {candidate!r}"
                    )
            finally:
                note.setText(original)
    finally:
        dialog.close()


def test_the_note_candidates_include_every_offered_model_and_the_entrys_own():
    """A name missing from the set is a name the reservation never measured."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    imported_id = "some-model-a-history-import-carried"
    dialog = RetranscribeDialog(
        entry=_entry(engine="local", model=imported_id),
        audio_path=Path(__file__),
        base_settings=AppSettings(engine="local", model_size="canary-1b-v2"),
        transcribe=lambda *args, **kwargs: (True, ""),
    )
    try:
        app.processEvents()
        joined = " ".join(dialog._worst_case_notes)
        for name in LOCAL_MODEL_LABELS:
            assert local_model_short_label(name) in joined, name
        assert local_model_short_label(imported_id) in joined
    finally:
        dialog.close()


def test_the_reservation_fits_the_note_the_dialog_actually_builds():
    """The other coverage test is self-referential; this one is not.

    `test_the_reserved_note_height_covers_every_substitutable_model_name`
    measures the dialog's own `_worst_case_notes` against the reservation
    computed from those same candidates, so it cannot see a candidate set
    that models the wrong string -- and it did: each candidate used one name
    twice, while `_update_substitution_note` uses the entry's model for the
    first name and the *selected* model for the second, so no candidate was a
    sentence the dialog could display.

    This walks the model picker instead and measures whatever the real note
    builder produces.

    What it catches, mutation-checked: a reservation that measures only some
    candidates, and one that stops re-measuring when the width changes. What
    it does not catch today is *which* candidates are used -- every candidate
    wraps to the same height at every width tried, so narrowing the set to one
    name leaves both this test and its sibling green. That is a property of
    the current names, not a guarantee, which is the reason to assert against
    the real builder rather than against the candidate list.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(engine="local", model="granite-speech-4.1-2b-nar"),
        audio_path=Path(__file__),
        base_settings=AppSettings(engine="local", model_size="canary-1b-v2"),
        transcribe=lambda *args, **kwargs: (True, ""),
    )
    try:
        dialog.show()
        app.processEvents()
        note = dialog._language_note
        combo = dialog._model_combo
        assert combo.count() > 1, "no models offered, so nothing is measured"

        dialog.setMinimumWidth(320)
        for width in (dialog.width(), 640, 560, 399, 320):
            dialog.resize(width, dialog.height())
            app.processEvents()
            reserved = note.minimumHeight()
            for index in range(combo.count()):
                combo.setCurrentIndex(index)
                dialog._update_substitution_note("auto")
                app.processEvents()
                needed = note.heightForWidth(note.width())
                assert needed <= reserved, (
                    f"at dialog width {width}, selecting "
                    f"{combo.itemText(index)!r} builds a note needing "
                    f"{needed} px against {reserved} px reserved: "
                    f"{note.text()!r}"
                )
    finally:
        dialog.close()


def test_retranscribe_dialog_reports_a_deleted_audio_file(tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(),
        audio_path=tmp_path / "gone.wav",
        base_settings=AppSettings(),
        transcribe=lambda *args, **kwargs: (True, "unreachable"),
    )

    dialog._start_run()

    assert "no longer available" in dialog._status_label.text()
    assert not dialog._run_button.isEnabled()


def test_retranscribe_dialog_empty_result_is_not_saved_to_history(tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(),
        audio_path=tmp_path / "clip.wav",
        base_settings=AppSettings(),
        transcribe=lambda *args, **kwargs: (True, ""),
    )

    dialog._on_finished(True, "")

    assert dialog.produced_transcript is False
    assert "Failed" in dialog._status_label.text()
    assert not dialog._copy_button.isEnabled()


def test_retranscribe_dialog_shows_the_result_and_enables_copy(tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(),
        audio_path=tmp_path / "gone.wav",
        base_settings=AppSettings(),
        transcribe=lambda *args, **kwargs: (True, "new text"),
    )

    dialog._on_finished(True, "new text")

    assert dialog._result_text.toPlainText() == "new text"
    assert dialog._copy_button.isEnabled()
    assert dialog.produced_transcript is True


def test_retranscribe_dialog_never_resizes_around_its_content(tmp_path):
    """Progress, a long error, and a long result must not move the dialog."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(),
        audio_path=tmp_path / "recording.wav",
        base_settings=AppSettings(),
        transcribe=lambda *args, **kwargs: (True, ""),
    )
    dialog.show()
    app.processEvents()
    baseline = dialog.size()

    dialog._set_status("Transcribing... (137s)")
    app.processEvents()
    assert dialog.size() == baseline

    dialog._on_finished(False, "Failed: " + "a long provider error " * 8)
    app.processEvents()
    assert dialog.size() == baseline

    dialog._on_finished(True, "ein neues transkript " * 40)
    app.processEvents()
    assert dialog.size() == baseline


def test_retranscribe_dialog_is_resizable(tmp_path):
    """A long transcript needs a bigger window, so nothing may fix the size."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(),
        audio_path=tmp_path / "recording.wav",
        base_settings=AppSettings(),
        transcribe=lambda *args, **kwargs: (True, ""),
    )
    dialog.show()
    app.processEvents()

    assert dialog.minimumSize() != dialog.maximumSize()
    dialog.resize(900, 800)
    app.processEvents()

    assert dialog.size() == QtCore.QSize(900, 800)


def test_retranscribe_dialog_preselects_the_entry_engine_and_model(tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(engine="local", model="cohere-transcribe-03-2026"),
        audio_path=tmp_path / "recording.wav",
        # Deliberately different from the entry: the entry wins.
        base_settings=AppSettings(engine="groq", model_size="small"),
        transcribe=lambda *args, **kwargs: (True, ""),
    )

    assert dialog.selected_engine() == "local"
    assert dialog.selected_model() == "cohere-transcribe-03-2026"


def test_retranscribe_dialog_switches_model_with_the_engine(tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(engine="local", model="cohere-transcribe-03-2026"),
        audio_path=tmp_path / "recording.wav",
        base_settings=AppSettings(engine="local", language_mode="de"),
        transcribe=lambda *args, **kwargs: (True, ""),
    )

    dialog._engine_combo.setCurrentIndex(dialog._engine_combo.findData("groq"))

    # The model list follows the engine, and the built settings write the
    # value into that engine's own field.
    assert dialog.selected_model() in {value for value, _ in _GROQ_CHOICES}
    settings = dialog.build_settings()
    assert settings.engine == "groq"
    assert settings.groq_model == dialog.selected_model()

    # Returning to the entry's engine restores the entry's own model.
    dialog._engine_combo.setCurrentIndex(dialog._engine_combo.findData("local"))
    assert dialog.selected_model() == "cohere-transcribe-03-2026"


def test_retranscribe_dialog_offers_only_languages_the_model_supports(tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(engine="local", model="distil-large-v3.5"),
        audio_path=tmp_path / "recording.wav",
        base_settings=AppSettings(engine="local", language_mode="de"),
        transcribe=lambda *args, **kwargs: (True, ""),
    )

    offered = {
        dialog._language_combo.itemData(index)
        for index in range(dialog._language_combo.count())
    }

    # distil-large-v3.5 is English-only; German must not be selectable.
    assert offered == {"auto", "en"}
    assert dialog.selected_language_mode() in offered


def test_retranscribe_dialog_keeps_a_failure_visible(tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = RetranscribeDialog(
        entry=_entry(),
        audio_path=tmp_path / "gone.wav",
        base_settings=AppSettings(),
        transcribe=lambda *args, **kwargs: (False, "boom"),
    )

    dialog._on_finished(False, "boom")

    assert "boom" in dialog._status_label.text()
    assert dialog.produced_transcript is False
    assert not dialog._copy_button.isEnabled()
