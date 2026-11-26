"""A dictation aimed at one of this app's own windows.

The Settings dialog is a valid target for its text fields. With the focus on
anything else the paste used to go out, reach nothing and be reported as
"Done" (the owner's log, 2026-11-24 22:48: target class `Qt6120QWindowIcon`,
check verdict unknown, the same text pasted by hand into another window
twelve seconds later).
"""

from __future__ import annotations

import logging

from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)
from PySide6 import QtWidgets
from test_controller_queue import DeferredExecutor

from stt_app.config import (
    FALLBACK_HOTKEY,
    OVERLAY_ERROR_ACTION_INSERT,
    OVERLAY_WARNING_NOT_INSERTED,
    QUEUE_ROW_KIND_UNDELIVERED,
)
from stt_app.settings_store import AppSettings
from stt_app.transcript_history import TranscriptHistoryStore


def _make(monkeypatch, tmp_path):
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _s, **kw: FakeStreamingTranscriber(),
    )
    FakeCapture.instances = []
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    focus = FakeWindowFocusHelper()
    history = TranscriptHistoryStore(tmp_path / "history.json")
    controller, app = make_controller(
        settings_store=FakeSettingsStore(
            AppSettings(
                hotkey=FALLBACK_HOTKEY,
                keep_transcript_in_clipboard=False,
                silence_gate_enabled=False,
            )
        ),
        overlay=overlay,
        text_inserter=inserter,
        window_focus_helper=focus,
        history_store=history,
        logger=logging.getLogger("test.controller.own_window_target"),
    )
    controller._executor = DeferredExecutor()
    return controller, app, overlay, inserter, focus, history


def _own_dialog(focus):
    """A dialog of this process, in front as far as the focus helper says."""
    line = QtWidgets.QLineEdit()
    table = QtWidgets.QTableWidget(2, 2)
    dialog = QtWidgets.QDialog()
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.addWidget(line)
    layout.addWidget(table)
    hwnd = int(dialog.winId())
    focus.captured = focus.current = hwnd
    focus.captured_focus = focus.current_focus = hwnd
    focus.captured_caret = focus.current_caret = hwnd
    return dialog, line, table


def _dictate(controller, text):
    controller.start_recording()
    controller.stop_recording()
    controller._on_transcription_ready(
        text, request_token=controller._active_request_token
    )


def test_a_dictation_into_an_own_window_without_a_text_field_is_not_pasted(
    monkeypatch, tmp_path
):
    controller, app, overlay, inserter, focus, history = _make(monkeypatch, tmp_path)
    dialog, _line, table = _own_dialog(focus)
    table.setFocus()

    _dictate(controller, "dictated into the settings window")

    assert inserter.calls == [], "the clipboard and the keystroke were used anyway"
    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "not in a text field" in detail
    assert "dictated into the settings window" in detail
    kwargs = overlay.state_kwargs[-1]
    assert kwargs["error_action"] == OVERLAY_ERROR_ACTION_INSERT
    assert kwargs["warning"] == OVERLAY_WARNING_NOT_INSERTED
    assert overlay.queue_kinds[-1] == [QUEUE_ROW_KIND_UNDELIVERED]
    assert [entry.text for entry in history.load()] == [
        "dictated into the settings window"
    ]
    controller.shutdown()
    dialog.deleteLater()
    _ = app


def test_a_dictation_into_a_text_field_of_an_own_window_is_pasted(
    monkeypatch, tmp_path
):
    controller, app, overlay, inserter, focus, _history = _make(monkeypatch, tmp_path)
    dialog, line, _table = _own_dialog(focus)
    line.setFocus()

    _dictate(controller, "dictated into a settings field")

    assert [call[0] for call in inserter.calls] == ["dictated into a settings field"]
    assert overlay.states[-1] == ("Done", "dictated into a settings field")
    controller.shutdown()
    dialog.deleteLater()
    _ = app


def test_insert_after_the_focus_moved_into_a_text_field_pastes_the_transcript(
    monkeypatch, tmp_path
):
    """The offer the refusal painted is the way out: click into a field,
    press Insert."""
    controller, app, overlay, inserter, focus, _history = _make(monkeypatch, tmp_path)
    dialog, line, table = _own_dialog(focus)
    table.setFocus()
    _dictate(controller, "second try lands")
    assert inserter.calls == []

    line.setFocus()
    controller.insert_failed_text()

    assert [call[0] for call in inserter.calls] == ["second try lands"]
    assert overlay.queue_kinds[-1] == [], "the row of the inserted text stayed listed"
    controller.shutdown()
    dialog.deleteLater()
    _ = app
