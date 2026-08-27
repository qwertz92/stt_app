from __future__ import annotations

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from stt_app import history_dialog as history_dialog_module
from stt_app.history_dialog import HistoryDialog
from stt_app.retranscribe_dialog import RetranscribeDialog
from stt_app.settings_dialog_helpers import model_choices_for_engine
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
    assert "granite-speech-4.1-2b-nar" in note
    assert "no longer offers" in note
    assert dialog.selected_model() in note


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
