"""Unsaved changes: what the dialog says about them, and what closing does.

Before this the dialog gave no sign that an edit had not been saved, Save was
enabled with nothing to save, and Close, Esc and the title bar's X all threw
unsaved edits away without a word.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import replace

import pytest
from PySide6 import QtCore, QtTest, QtWidgets

from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_store import AppSettings, SettingsStore


class _SecretStore:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    def get_api_key(self, provider: str) -> str | None:
        return self.keys.get(provider)

    def set_api_key(self, provider: str, value: str) -> None:
        self.keys[provider] = value

    def delete_api_key(self, provider: str) -> None:
        self.keys.pop(provider, None)


class _Logger:
    def diagnostics_text(self) -> str:
        return ""


@pytest.fixture
def dialog(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # No inventory scan thread: it would count as background work and defer
    # the reload a Discard asks for.
    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models", lambda _model_dir: []
    )
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings())
    settings_dialog = SettingsDialog(
        settings_store=store,
        secret_store=_SecretStore(),
        app_logger=_Logger(),
    )
    yield settings_dialog
    # Programmatic, so it must never prompt: the fixture teardown of a dirty
    # dialog would otherwise reach the blocked QMessageBox.question.
    settings_dialog.close()
    app.processEvents()


def _settle(dialog: SettingsDialog) -> None:
    """Let the debounced refresh run."""
    QtTest.QTest.qWait(dialog._UNSAVED_CHANGES_DEBOUNCE_MS + 100)


def _answer_prompt(monkeypatch, answer) -> list[str]:
    asked: list[str] = []

    def _question(_parent, title, text, *_args, **_kwargs):
        asked.append(f"{title}: {text}")
        return answer

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", _question)
    return asked


def test_a_fresh_dialog_has_nothing_to_save(dialog: SettingsDialog) -> None:
    _settle(dialog)

    assert dialog.has_unsaved_changes() is False
    assert dialog._save_button.isEnabled() is False
    assert dialog._save_status_label.text() == ""


def test_an_edit_enables_save_and_says_so(dialog: SettingsDialog) -> None:
    dialog.keep_clipboard_checkbox.toggle()
    _settle(dialog)

    assert dialog.has_unsaved_changes() is True
    assert dialog._save_button.isEnabled() is True
    assert dialog._save_status_label.text() == "Unsaved changes"


def test_taking_an_edit_back_is_no_change(dialog: SettingsDialog) -> None:
    dialog.keep_clipboard_checkbox.toggle()
    dialog.keep_clipboard_checkbox.toggle()
    _settle(dialog)

    assert dialog.has_unsaved_changes() is False
    assert dialog._save_button.isEnabled() is False
    assert dialog._save_status_label.text() == ""


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            lambda d: d.engine_combo.setCurrentIndex(d.engine_combo.findData("groq")),
            id="engine",
        ),
        pytest.param(
            lambda d: d.custom_vocabulary_edit.setPlainText("Splunk"), id="vocabulary"
        ),
        pytest.param(
            lambda d: d.vad_threshold_spin.setValue(
                d.vad_threshold_spin.value() + d.vad_threshold_spin.singleStep()
            ),
            id="spin",
        ),
        pytest.param(lambda d: d.model_dir_edit.setText("D:/models"), id="model-dir"),
        pytest.param(lambda d: d.openai_key_edit.setText("sk-typed"), id="typed-key"),
        pytest.param(
            lambda d: d.local_onnx_device_combo.setCurrentIndex(
                d.local_onnx_device_combo.findData("cpu")
            ),
            id="onnx-device",
        ),
        pytest.param(
            lambda d: d.history_timezone_combo.setCurrentIndex(
                d.history_timezone_combo.findData("utc")
            ),
            id="history-timezone",
        ),
        pytest.param(lambda d: d.tray_middle_click_checkbox.toggle(), id="hotkeys-tab"),
    ],
)
def test_every_settings_page_counts(dialog: SettingsDialog, edit) -> None:
    edit(dialog)
    _settle(dialog)

    assert dialog.has_unsaved_changes() is True
    assert dialog._save_button.isEnabled() is True


def test_a_model_chosen_for_a_provider_not_selected_counts(
    dialog: SettingsDialog,
) -> None:
    """Pick a Groq model, go back to the local engine: no widget shows the
    Groq choice any more, but Save writes it, so it is an unsaved change."""
    local_index = dialog.engine_combo.currentIndex()
    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("groq"))
    combo = dialog.remote_model_combo
    chosen = next(
        index
        for index in range(combo.count())
        if combo.itemData(index) != combo.currentData()
    )
    combo.setCurrentIndex(chosen)
    dialog.engine_combo.setCurrentIndex(local_index)
    _settle(dialog)

    assert dialog.has_unsaved_changes() is True


def test_marking_a_key_for_removal_counts(dialog: SettingsDialog) -> None:
    dialog._mark_provider_key_for_clear("openai")
    _settle(dialog)

    assert dialog.has_unsaved_changes() is True
    assert dialog._save_button.isEnabled() is True


def test_scrolling_a_page_or_a_list_is_not_an_edit(dialog: SettingsDialog) -> None:
    """A scroll bar is a slider, and sliders were read as settings.

    Every settings page is a scroll area and every combo box owns a popup
    list, so scrolling a page or a model list turned Save on and put
    "Unsaved changes" under a dialog whose settings were untouched -- and
    Close then asked whether to save them.
    """
    page_bar = dialog.tabs.widget(0).verticalScrollBar()
    popup_bar = dialog.model_combo.view().verticalScrollBar()
    for bar in (page_bar, popup_bar):
        bar.setRange(0, 500)
        bar.setValue(200)
    _settle(dialog)

    assert dialog.has_unsaved_changes() is False
    assert dialog._save_button.isEnabled() is False
    assert not any(
        isinstance(widget, QtWidgets.QScrollBar) for widget in dialog._unsaved_widgets
    )


def test_the_connection_test_target_is_not_a_setting(dialog: SettingsDialog) -> None:
    combo = dialog.test_conn_target_combo
    combo.setCurrentIndex((combo.currentIndex() + 1) % combo.count())
    _settle(dialog)

    assert dialog.has_unsaved_changes() is False


def test_a_save_leaves_nothing_to_save(dialog: SettingsDialog) -> None:
    dialog.keep_clipboard_checkbox.toggle()
    _settle(dialog)

    dialog._save()
    _settle(dialog)

    assert dialog.has_unsaved_changes() is False
    assert dialog._save_button.isEnabled() is False
    # The save's own confirmation is what shows, not "Unsaved changes".
    assert "Settings saved" in dialog._save_status_label.text()


@pytest.mark.parametrize("with_key", [False, True], ids=["nothing", "key-only"])
def test_a_save_with_nothing_to_write_settles_what_the_widgets_show(
    dialog: SettingsDialog, with_key: bool
) -> None:
    """A save that finds the file already equal still moves the baseline.

    The History dialog writes the limit straight to the file. Typing the same
    number here and saving writes no settings -- "No settings changes", or
    only a key -- and the dialog used to call itself clean without recording
    that the box now shows that number, so the next save read it as a fresh
    edit and wrote it back over whatever the History dialog had written since.
    """
    store = dialog._settings_store
    # Replacing a stored key, not adding one: adding flips `has_openai_key`
    # and so writes settings.
    dialog._secret_store.set_api_key("openai", "sk-old")
    store.save(replace(store.load(), history_max_items=800, has_openai_key=True))
    dialog.history_max_spin.setValue(800)
    if with_key:
        dialog.openai_key_edit.setText("sk-new")
    dialog._save()
    assert dialog._save_status_label.text() == (
        "\u2713 API keys saved" if with_key else "No settings changes"
    )
    _settle(dialog)
    assert dialog._save_button.isEnabled() is False

    store.save(replace(store.load(), history_max_items=300))
    dialog.keep_clipboard_checkbox.toggle()
    dialog._save()

    assert store.load().history_max_items == 300


def test_the_confirmation_gives_way_to_the_state_it_describes(
    dialog: SettingsDialog,
) -> None:
    """The three-second confirmation must not hide an edit made after it."""
    dialog.keep_clipboard_checkbox.toggle()
    dialog._save()
    dialog.tray_middle_click_checkbox.toggle()
    _settle(dialog)

    dialog._save_status_timer.timeout.emit()

    assert dialog._save_status_label.text() == "Unsaved changes"


def test_a_key_only_save_clears_what_it_saved_and_keeps_the_rest(
    dialog: SettingsDialog,
) -> None:
    dialog.keep_clipboard_checkbox.toggle()
    dialog.openai_key_edit.setText("sk-new")
    _settle(dialog)

    dialog._save_api_keys_only()
    _settle(dialog)

    # The key went; the checkbox edit is still unsaved.
    assert dialog.has_unsaved_changes() is True
    dialog.keep_clipboard_checkbox.toggle()
    _settle(dialog)
    assert dialog.has_unsaved_changes() is False


def test_the_history_import_moving_the_limit_is_not_an_edit(
    dialog: SettingsDialog,
) -> None:
    dialog._set_history_max_spin_value(dialog.history_max_spin.value() + 50)
    _settle(dialog)

    assert dialog.has_unsaved_changes() is False


def test_reopening_after_a_reload_starts_clean(dialog: SettingsDialog) -> None:
    dialog.keep_clipboard_checkbox.toggle()
    dialog.reload_from_store()
    _settle(dialog)

    assert dialog.has_unsaved_changes() is False


def test_the_engine_bar_names_the_active_selection_and_the_pending_one(
    dialog: SettingsDialog,
) -> None:
    before = dialog.engine_indicator.text()
    assert before.startswith("Active:")
    assert "after Save" not in before

    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("groq"))
    pending = dialog.engine_indicator.text()
    assert pending.startswith(before)
    assert "after Save" in pending
    assert "Groq" in pending.split("after Save", 1)[1]

    dialog._save()
    saved = dialog.engine_indicator.text()
    assert "after Save" not in saved
    assert "Groq" in saved


# --- Closing -------------------------------------------------------------


def test_close_on_a_clean_dialog_does_not_ask(dialog, monkeypatch) -> None:
    asked = _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Cancel)
    dialog.show()
    QtWidgets.QApplication.processEvents()

    dialog._request_close()

    assert asked == []
    assert dialog.isVisible() is False


def test_close_with_unsaved_changes_asks_and_cancel_stays(dialog, monkeypatch) -> None:
    asked = _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Cancel)
    dialog.show()
    dialog.keep_clipboard_checkbox.toggle()

    dialog._request_close()

    assert len(asked) == 1
    assert dialog.isVisible() is True
    assert dialog.has_unsaved_changes() is True


def test_save_from_the_prompt_saves_and_closes(dialog, monkeypatch) -> None:
    _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Save)
    dialog.show()
    wanted = not dialog.keep_clipboard_checkbox.isChecked()
    dialog.keep_clipboard_checkbox.setChecked(wanted)

    dialog._request_close()

    assert dialog.isVisible() is False
    assert dialog._settings_store.load().keep_transcript_in_clipboard is wanted


def test_a_save_that_fails_keeps_the_dialog_open(dialog, monkeypatch) -> None:
    _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Save)
    dialog.show()
    dialog.keep_clipboard_checkbox.toggle()

    def _refuse(_settings):
        raise OSError("disk full")

    monkeypatch.setattr(dialog._settings_store, "save", _refuse)
    dialog._request_close()

    assert dialog.isVisible() is True
    assert dialog.has_unsaved_changes() is True


def test_discard_from_the_prompt_restores_the_stored_values(
    dialog, monkeypatch
) -> None:
    _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Discard)
    dialog.show()
    stored = dialog.keep_clipboard_checkbox.isChecked()
    dialog.keep_clipboard_checkbox.toggle()

    dialog._request_close()

    assert dialog.isVisible() is False
    assert dialog.keep_clipboard_checkbox.isChecked() is stored
    assert dialog.has_unsaved_changes() is False
    assert dialog._settings_store.load().keep_transcript_in_clipboard is stored


def test_discard_while_dialog_work_runs_still_discards(dialog, monkeypatch) -> None:
    """Discard puts the settings back even while a connection test runs.

    It reloaded from the store, and that reload waits while dialog-owned work
    runs, so the edits stayed in the widgets behind a dialog that had been
    told to discard them -- and the next Save wrote them. The widgets go back
    now, while what the running work shows is left to it.
    """
    _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Discard)
    dialog.show()
    stored = dialog._settings_store.load()
    dialog.keep_clipboard_checkbox.toggle()
    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("groq"))
    dialog.openai_key_edit.setText("sk-typed")
    dialog._mark_provider_key_for_clear("deepgram")
    dialog.import_engine_combo.setCurrentIndex(
        dialog.import_engine_combo.findData("openai")
    )
    assert "typed but not saved" in dialog.import_engine_note.text()
    # A known inventory for this Model Dir, as a finished scan leaves it.
    dialog._cached_local_models = ["tiny"]
    dialog._cached_local_models_dir = dialog.model_dir_edit.text().strip()
    dialog._cached_local_models_available = True
    dialog._active_connection_test_thread = object()
    running_label = dialog._provider_last_test_labels["groq"]
    running_label.setText("Testing...")
    _settle(dialog)

    dialog._request_close()

    assert dialog.isVisible() is False
    assert (
        dialog.keep_clipboard_checkbox.isChecked()
        is stored.keep_transcript_in_clipboard
    )
    assert dialog.engine_combo.currentData() == stored.engine
    assert dialog.openai_key_edit.text() == ""
    assert dialog._provider_pending_clear == set()
    assert dialog.has_unsaved_changes() is False
    assert running_label.text() == "Testing..."
    assert "typed but not saved" not in dialog.import_engine_note.text()
    tiny = dialog.model_combo.findData("tiny")
    assert dialog.model_combo.itemText(tiny).startswith("\u2713")

    dialog._active_connection_test_thread = None
    dialog.show()
    dialog.tray_middle_click_checkbox.toggle()
    dialog._save()
    saved = dialog._settings_store.load()
    assert saved.keep_transcript_in_clipboard is stored.keep_transcript_in_clipboard
    assert saved.engine == stored.engine


def test_a_busy_discard_of_a_model_dir_repaints_the_models_tab(
    dialog, monkeypatch, tmp_path
) -> None:
    """The Model Dir is restored with its signals blocked, so the Models tab
    went on describing the folder that had just been discarded -- "not
    verified yet" and an empty list -- beside a field naming the stored one."""
    import stt_app.settings_dialog_helpers as helpers

    _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Discard)
    monkeypatch.setitem(helpers._LOCAL_MODEL_SCAN_SESSION_CACHE, "", ["tiny"])
    dialog.show()
    dialog._on_model_dir_changed()
    assert dialog.local_models_list.count() > 0
    dialog.model_dir_edit.setText(str(tmp_path / "somewhere-else"))
    assert "not been verified" in dialog.local_models_label.text()
    dialog._active_connection_test_thread = object()
    _settle(dialog)

    dialog._request_close()

    assert dialog.model_dir_edit.text() == ""
    assert "not been verified" not in dialog.local_models_label.text()
    assert dialog.local_models_list.count() > 0


def test_a_discarded_typed_key_leaves_no_note_behind(dialog, monkeypatch) -> None:
    """The key fields are cleared with their signals blocked, so the Import
    tab's note kept saying a new key was typed but not saved -- a key that
    had just been discarded -- when nothing else changed to repaint it."""
    _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Discard)
    dialog.show()
    dialog.import_engine_combo.setCurrentIndex(
        dialog.import_engine_combo.findData("openai")
    )
    dialog.openai_key_edit.setText("sk-typed")
    assert "typed but not saved" in dialog.import_engine_note.text()
    dialog._active_connection_test_thread = object()
    _settle(dialog)

    dialog._request_close()

    assert dialog.openai_key_edit.text() == ""
    assert "typed but not saved" not in dialog.import_engine_note.text()


def test_the_close_button_asks(dialog, monkeypatch) -> None:
    asked = _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Cancel)
    dialog.show()
    dialog.keep_clipboard_checkbox.toggle()

    dialog._close_button.click()

    assert len(asked) == 1
    assert dialog.isVisible() is True


def test_escape_asks(dialog, monkeypatch) -> None:
    asked = _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Cancel)
    dialog.show()
    dialog.keep_clipboard_checkbox.toggle()

    QtTest.QTest.keyClick(dialog, QtCore.Qt.Key_Escape)

    assert len(asked) == 1
    assert dialog.isVisible() is True


@pytest.mark.skipif(sys.platform != "win32", reason="posts a native WM_CLOSE")
def test_the_title_bar_close_asks(dialog, monkeypatch) -> None:
    """WM_CLOSE is what the title bar's X sends; Qt delivers it as a
    spontaneous close event (verified with a probe, 2026-09-27)."""
    asked = _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Cancel)
    dialog.show()
    QtTest.QTest.qWait(100)
    dialog.keep_clipboard_checkbox.toggle()

    wm_close = 0x0010
    ctypes.windll.user32.PostMessageW(int(dialog.winId()), wm_close, 0, 0)
    QtTest.QTest.qWait(300)

    assert len(asked) == 1
    assert dialog.isVisible() is True


def test_a_programmatic_close_never_asks(dialog, monkeypatch) -> None:
    """Application quit, shutdown and tests close the dialog in code; a
    prompt there would hold the quit (Qt 6 cancels it when a window refuses
    to close) or hang a test run."""
    asked = _answer_prompt(monkeypatch, QtWidgets.QMessageBox.Cancel)
    dialog.show()
    dialog.keep_clipboard_checkbox.toggle()

    dialog.close()
    dialog.show()
    dialog.reject()

    assert asked == []
    assert dialog.isVisible() is False
