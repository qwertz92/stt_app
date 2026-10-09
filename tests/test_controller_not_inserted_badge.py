"""The overlay's not-inserted badge as the controller drives it (owner's
request 2026-10-09): how many transcripts wait, with the re-paste hotkey's
real label, kept through the next recording."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)


def _controller(monkeypatch=None, *, repaste_hotkey="Ctrl+Alt+F10"):
    if monkeypatch is not None:
        monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    controller, app = make_controller(
        overlay=overlay,
        text_inserter=inserter,
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._settings = replace(controller._settings, repaste_hotkey=repaste_hotkey)
    controller._repaste_hotkey_registration_ok = True
    return controller, app, overlay, inserter


def _row(controller, text, **kwargs):
    return controller._record_undelivered_insert(
        text,
        may_have_pasted=kwargs.pop("may_have_pasted", False),
        created_at=datetime.now().astimezone(),
        history_entry=None,
        **kwargs,
    )


def test_the_badge_counts_what_waits_and_names_the_re_paste_hotkey():
    controller, app, overlay, _inserter = _controller()

    _row(controller, "one")
    assert overlay.badge == "1 not inserted · Ctrl+Alt+F10"
    _row(controller, "two", outside_text_field=True)
    assert overlay.badge == "2 not inserted · Ctrl+Alt+F10"
    # A paste that may have landed is never pasted again: it waits for no
    # hotkey, its row asks the user to check the window.
    _row(controller, "three", may_have_pasted=True)
    assert overlay.badge == "2 not inserted · Ctrl+Alt+F10"
    assert "press Ctrl+Alt+F10" in overlay.badges[-1][1]
    controller.shutdown()
    _ = app


def test_without_a_registered_hotkey_the_badge_points_at_the_tray():
    controller, app, overlay, _inserter = _controller(repaste_hotkey="")

    _row(controller, "one")

    assert overlay.badge == "1 not inserted · tray menu"
    assert "Insert transcript again" in overlay.badges[-1][1]
    controller.shutdown()
    _ = app


def test_the_badge_stays_through_the_next_recording(monkeypatch):
    """Listening paints over the Insert offer; the badge must not go with it."""
    controller, app, overlay, _inserter = _controller(monkeypatch)
    _row(controller, "one")

    controller.start_recording()

    assert controller._audio_capture is not None
    assert overlay.badge == "1 not inserted · Ctrl+Alt+F10"
    controller.stop_recording()
    controller.shutdown()
    _ = app


def test_the_badge_goes_once_the_re_paste_inserted_everything():
    controller, app, overlay, inserter = _controller()
    _row(controller, "one")
    _row(controller, "two")
    assert overlay.badge == "2 not inserted · Ctrl+Alt+F10"

    controller.repaste_last_transcript()

    assert [call[0] for call in inserter.calls] == ["one two"]
    assert overlay.badge == ""
    controller.shutdown()
    _ = app


def test_a_settings_save_renames_the_hotkey_on_the_badge():
    controller, app, overlay, _inserter = _controller()
    _row(controller, "one")
    store = controller._settings_store
    store._settings = replace(controller._settings, repaste_hotkey="Ctrl+Alt+F9")

    controller.reload_settings(re_register_hotkey=False)

    assert overlay.badge == "1 not inserted · Ctrl+Alt+F9"
    controller.shutdown()
    _ = app
