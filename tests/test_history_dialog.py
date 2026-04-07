from __future__ import annotations

import json

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from stt_app.history_dialog import HistoryDialog
from stt_app.settings_store import AppSettings, SettingsStore
from stt_app.transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore


class _FakeClipboard:
    def __init__(self) -> None:
        self.value = ""

    def setText(self, text: str) -> None:
        self.value = text

    def text(self) -> str:
        return self.value


def _entry(text: str) -> TranscriptHistoryEntry:
    return TranscriptHistoryEntry.new(
        text=text,
        engine="local",
        model="small",
        mode="batch",
    )


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


def test_copy_selected_button_shows_feedback(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry("alpha"), _entry("beta")])
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=20))

    clipboard = _FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: clipboard)

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )
    dialog._table.selectRow(0)
    dialog._copy_button.click()

    assert clipboard.text() == "beta"
    assert dialog._copy_button.text() == "Copied"
    QtTest.QTest.qWait(1100)
    assert dialog._copy_button.text() == "Copy selected"
    _ = app


def test_delete_selected_button_removes_entry(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry("alpha"), _entry("beta")])
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=20))

    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: QtWidgets.QMessageBox.Yes,
    )

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )
    dialog._table.selectRow(0)

    dialog._delete_button.click()

    assert [entry.text for entry in history_store.load()] == ["alpha"]
    _ = app


def test_reducing_limit_confirms_and_trims(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry("one"), _entry("two"), _entry("three")])
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=3))
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: QtWidgets.QMessageBox.Yes,
    )

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )
    dialog._max_items_spin.setValue(2)

    assert history_store.count() == 2
    assert settings_store.load().history_max_items == 2
    _ = app


def test_import_overflow_can_switch_to_unlimited(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry("old-1"), _entry("old-2")])
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=2))

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
    monkeypatch.setattr(
        HistoryDialog,
        "_prompt_import_overflow",
        lambda *args, **kwargs: "unlimited",
    )
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "information",
        lambda *args, **kwargs: QtWidgets.QMessageBox.Ok,
    )

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )
    dialog._import_history()

    assert settings_store.load().history_max_items == 0
    assert history_store.count() == 4
    _ = app


def test_history_dialog_window_has_native_minimize_button(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=20))

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )

    flags = dialog.windowFlags()
    assert bool(flags & QtCore.Qt.Window)
    assert bool(flags & QtCore.Qt.WindowSystemMenuHint)
    assert bool(flags & QtCore.Qt.WindowMinimizeButtonHint)
    assert bool(flags & QtCore.Qt.WindowCloseButtonHint)
    _ = app
