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
    assert dialog._copy_button.minimumWidth() >= dialog._copy_button.sizeHint().width()
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


def test_multiselect_copy_joins_selected_entries(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry("alpha"), _entry("beta"), _entry("gamma")])
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=20))
    clipboard = _FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: clipboard)

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )
    dialog._select_rows([0, 1])

    assert dialog._detail.toPlainText() == "2 entries selected."
    assert dialog._edit_button.isEnabled() is False

    dialog._copy_button.click()

    assert clipboard.text() == "gamma\n\nbeta"
    assert dialog._copy_button.text() == "Copied"
    _ = app


def test_multiselect_delete_removes_selected_entries(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry("alpha"), _entry("beta"), _entry("gamma")])
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
    dialog._select_rows([0, 1])
    dialog._delete_button.click()

    assert [entry.text for entry in history_store.load()] == ["alpha"]
    _ = app


def test_history_dialog_can_defer_initial_reload(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry("alpha")])
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=20))

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
        autoload=False,
    )

    assert dialog._table.rowCount() == 0
    dialog.reload()
    assert dialog._table.rowCount() == 1
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


def test_typing_larger_limit_does_not_confirm_intermediate_digits(
    monkeypatch,
    tmp_path,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry(f"entry-{index}") for index in range(224)])
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=224))

    def fail_question(*_args, **_kwargs):
        raise AssertionError("Limit confirmation must wait for the committed value")

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", fail_question)

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )
    dialog.show()
    app.processEvents()

    spin_editor = dialog._max_items_spin.lineEdit()
    dialog._max_items_spin.setFocus()
    spin_editor.selectAll()
    QtTest.QTest.keyClicks(spin_editor, "300")
    app.processEvents()

    assert settings_store.load().history_max_items == 224
    assert history_store.count() == 224

    QtTest.QTest.keyClick(spin_editor, QtCore.Qt.Key_Return)
    app.processEvents()

    assert settings_store.load().history_max_items == 300
    assert history_store.count() == 224
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
    assert bool(flags & QtCore.Qt.WindowMaximizeButtonHint)
    assert bool(flags & QtCore.Qt.WindowCloseButtonHint)
    _ = app


def test_history_dialog_opens_with_roomier_default_size(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=20))

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )

    assert dialog.size().width() >= 1040
    assert dialog.size().height() >= 660
    assert dialog.minimumSize().width() >= 700
    assert dialog.minimumSize().height() >= 460
    _ = app


def test_history_dialog_uses_compact_table_rows(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=20))

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )

    expected = max(dialog.fontMetrics().height() + 4, 18)
    assert dialog._table.verticalHeader().defaultSectionSize() == expected
    _ = app


def test_history_dialog_reload_preserves_selected_entry_and_scroll(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry(f"entry {index}") for index in range(30)])
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=50))

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )
    row_height = dialog._table.verticalHeader().defaultSectionSize()
    dialog._table.setFixedHeight(row_height * 5)
    dialog.show()
    app.processEvents()

    dialog._table.selectRow(10)
    expected_entry = dialog._entries[10]
    scroll_bar = dialog._table.verticalScrollBar()
    scroll_bar.setValue(scroll_bar.maximum())
    scroll_before = scroll_bar.value()

    dialog.reload()

    selected = dialog._table.selectionModel().selectedRows()
    assert len(selected) == 1
    assert dialog._entries[selected[0].row()] == expected_entry
    if scroll_before > 0:
        assert dialog._table.verticalScrollBar().value() == scroll_before
    _ = app


def test_history_dialog_uses_vertical_splitter(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=20))

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )

    assert dialog._splitter.orientation() == QtCore.Qt.Vertical
    assert dialog._splitter.childrenCollapsible() is False
    assert dialog._splitter.widget(0) is dialog._table
    assert dialog._splitter.widget(1) is dialog._detail
    _ = app


def test_increasing_limit_updates_label_without_rebuilding_table(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry("alpha"), _entry("beta")])
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=2))

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )

    def fail_reload():
        raise AssertionError("Increasing above stored count should not reload table")

    dialog.reload = fail_reload
    dialog._max_items_spin.setValue(3)

    assert settings_store.load().history_max_items == 3
    assert dialog._history_count_label.text() == (
        "Stored: 2 entries (limit 3; showing all stored entries)"
    )
    assert dialog._table.rowCount() == 2
    _ = app


def test_import_defaults_to_history_store_directory(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    history_store = TranscriptHistoryStore(path=tmp_path / "data" / "history.json")
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=20))
    captured = {}

    def fake_get_open_file_name(_parent, _title, directory, _filter):
        captured["directory"] = directory
        return "", ""

    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        fake_get_open_file_name,
    )

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )
    dialog._import_history()

    assert captured["directory"] == str(history_store.path.parent)
    assert history_store.path.parent.is_dir()
    _ = app


def test_history_table_shows_preview_but_detail_keeps_full_text(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    long_text = "word " * 80
    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.save([_entry(long_text)])
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(AppSettings(history_max_items=20))

    dialog = HistoryDialog(
        history_store=history_store,
        settings_store=settings_store,
    )

    assert dialog._table.item(0, 3).text().endswith("...")
    assert len(dialog._table.item(0, 3).text()) < len(long_text)
    assert dialog._detail.toPlainText() == long_text
    _ = app
