"""Unsaved changes: what the dialog says about them, and what closing does.

Before this the dialog gave no sign that an edit had not been saved, Save was
enabled with nothing to save, and Close, Esc and the title bar's X all threw
unsaved edits away without a word.
"""

from __future__ import annotations

import ctypes
import sys

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
        pytest.param(lambda d: d.engine_combo.setCurrentIndex(
            d.engine_combo.findData("groq")), id="engine"),
        pytest.param(lambda d: d.custom_vocabulary_edit.setPlainText("Splunk"),
                     id="vocabulary"),
        pytest.param(lambda d: d.vad_threshold_spin.setValue(
            d.vad_threshold_spin.value() + d.vad_threshold_spin.singleStep()),
            id="spin"),
        pytest.param(lambda d: d.model_dir_edit.setText("D:/models"),
                     id="model-dir"),
        pytest.param(lambda d: d.openai_key_edit.setText("sk-typed"),
                     id="typed-key"),
        pytest.param(lambda d: d.local_onnx_device_combo.setCurrentIndex(
            d.local_onnx_device_combo.findData("cpu")), id="onnx-device"),
        pytest.param(lambda d: d.history_timezone_combo.setCurrentIndex(
            d.history_timezone_combo.findData("utc")), id="history-timezone"),
        pytest.param(lambda d: d.tray_middle_click_checkbox.toggle(),
                     id="hotkeys-tab"),
    ],
)
def test_every_settings_page_counts(dialog: SettingsDialog, edit) -> None:
    edit(dialog)
    _settle(dialog)

    assert dialog.has_unsaved_changes() is True
    assert dialog._save_button.isEnabled() is True


def test_marking_a_key_for_removal_counts(dialog: SettingsDialog) -> None:
    dialog._mark_provider_key_for_clear("openai")
    _settle(dialog)

    assert dialog.has_unsaved_changes() is True
    assert dialog._save_button.isEnabled() is True


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


def test_close_with_unsaved_changes_asks_and_cancel_stays(
    dialog, monkeypatch
) -> None:
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
