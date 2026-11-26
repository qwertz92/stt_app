"""The retired-model notice a start still owes the user.

`pending_model_moves` lives in the settings file until the tray has shown the
balloon. Anything that saves a stale in-memory copy of the settings (the
overlay's own controls do) must neither bring a shown notice back nor drop one
that was not shown yet.
"""

import json
import logging
from dataclasses import replace

import pytest
from conftest import (
    FakeHotkeyManager,
    FakeOverlay,
    FakeTextInserter,
    FakeWindowFocusHelper,
)
from PySide6 import QtWidgets

from stt_app import settings_store as settings_store_module
from stt_app.config import FALLBACK_HOTKEY
from stt_app.controller import DictationController
from stt_app.persistence import store_unavailable_error
from stt_app.settings_store import AppSettings, SettingsStore


def _write(path, **fields):
    payload = AppSettings(hotkey=FALLBACK_HOTKEY).to_dict()
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _pending(path):
    return SettingsStore(path).load().pending_model_moves


def _controller(store):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=store,
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.pending"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._start_local_model_preload = lambda *a, **k: None
    return controller, app


def _a_controller_holding_a_stale_notice(tmp_path, **fields):
    path = tmp_path / "settings.json"
    _write(path, engine="local", model_size="tiny", **fields)
    store = SettingsStore(path)
    store.load()
    controller, app = _controller(store)
    controller.reload_settings(re_register_hotkey=False)
    # The controller's own copy still carries the notice ...
    assert controller.settings.pending_model_moves
    # ... and the tray shows it and clears it in the file.
    store.clear_model_notice()
    assert _pending(path) == []
    return controller, app, path


def test_a_plain_save_of_a_stale_copy_never_brings_a_shown_notice_back(tmp_path):
    path = tmp_path / "settings.json"
    _write(path, engine="local", model_size="tiny")
    store = SettingsStore(path)
    stale = store.load()
    assert stale.pending_model_moves
    store.clear_model_notice()

    store.save(stale)

    assert _pending(path) == []


def test_a_plain_save_never_drops_a_notice_that_was_not_shown_yet(tmp_path):
    path = tmp_path / "settings.json"
    _write(path, engine="local", model_size="tiny")
    store = SettingsStore(path)
    fresh = store.load()

    store.save(replace(fresh, pending_model_moves=[]))

    assert [m["from"] for m in _pending(path)] == ["tiny"]


def test_the_language_menu_does_not_bring_a_shown_notice_back(tmp_path):
    controller, app, path = _a_controller_holding_a_stale_notice(
        tmp_path, language_mode="auto"
    )

    controller.set_language_mode("de")

    assert _pending(path) == []
    controller.shutdown()
    _ = app


def test_the_opacity_slider_does_not_bring_a_shown_notice_back(tmp_path):
    controller, app, path = _a_controller_holding_a_stale_notice(tmp_path)

    controller.set_overlay_opacity_percent(
        int(controller.settings.overlay_opacity_percent) - 10
    )

    assert _pending(path) == []
    controller.shutdown()
    _ = app


def test_the_pin_button_does_not_bring_a_shown_notice_back(tmp_path):
    controller, app, path = _a_controller_holding_a_stale_notice(tmp_path)

    controller.set_overlay_always_on_top(not controller.settings.overlay_always_on_top)

    assert _pending(path) == []
    controller.shutdown()
    _ = app


def test_clearing_the_notice_never_raises_into_qt_and_keeps_it_pending(
    tmp_path, monkeypatch, caplog
):
    """With a primary settings file that cannot be opened the save refuses; the
    timer callback that clears the notice must log it, not raise."""
    path = tmp_path / "settings.json"
    _write(path, engine="local", model_size="tiny")
    store = SettingsStore(path)
    store.load()

    def refuse(file_path):
        raise store_unavailable_error(file_path)

    monkeypatch.setattr(settings_store_module, "refuse_to_overwrite_unreadable", refuse)
    with caplog.at_level(logging.WARNING):
        store.clear_model_notice()
    monkeypatch.undo()

    assert [m["from"] for m in _pending(path)] == ["tiny"]
    assert any("pending" in record.getMessage().lower() for record in caplog.records)


@pytest.mark.parametrize("which", ["one", "all"])
def test_clearing_named_moves_leaves_the_others_pending(tmp_path, which):
    path = tmp_path / "settings.json"
    _write(
        path,
        engine="local",
        model_size="tiny",
        openai_model="whisper-1",
        has_openai_key=True,
    )
    store = SettingsStore(path)
    moves = store.load().pending_model_moves
    assert {m["engine"] for m in moves} == {"local", "openai"}

    if which == "one":
        store.clear_model_notice([m for m in moves if m["engine"] == "local"])
        assert [m["engine"] for m in _pending(path)] == ["openai"]
    else:
        store.clear_model_notice()
        assert _pending(path) == []
