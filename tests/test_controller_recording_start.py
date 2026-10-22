"""The recording start: what the log says about it.

Owner report 2026-10-23 (slow work laptop, Defender XDR, warm stream on):
the first words of a recording were missing from the saved WAV. A warm
stream drops every block while no recording is attached, so whatever is
said between the hotkey press and the attach is gone -- and nothing in the
log said how long that was.
"""

from __future__ import annotations

import logging

from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeSettingsStore,
    make_controller,
)

from stt_app.config import FALLBACK_HOTKEY
from stt_app.settings_store import AppSettings


def _start_timing_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "recording_start_timing" in r.getMessage()]


def _controller(monkeypatch, capture_class=FakeCapture):
    FakeCapture.instances = []
    monkeypatch.setattr("stt_app.controller.AudioCapture", capture_class)
    return make_controller(
        settings_store=FakeSettingsStore(
            AppSettings(hotkey=FALLBACK_HOTKEY, mode="batch")
        ),
        overlay=FakeOverlay(),
    )


def test_the_start_timing_says_how_long_the_hotkey_press_waited(monkeypatch, caplog):
    """The press's own `MSG.time` against the message clock: how long the Qt
    thread took to get to the press (`press_to_handle_ms`) and to the
    capture running (`press_to_capture_ms`). Speech in that span is not
    recorded, so a long one is a warning."""
    ticks = {"now": 10_400}
    monkeypatch.setattr("stt_app.controller.message_clock_ms", lambda: ticks["now"])

    class SlowStartCapture(FakeCapture):
        def start(self):
            ticks["now"] = 12_100
            super().start()

    controller, app = _controller(monkeypatch, SlowStartCapture)
    with caplog.at_level(logging.INFO, logger="test.controller"):
        controller.toggle_recording_from_hotkey(10_000)

    lines = _start_timing_lines(caplog)
    assert len(lines) == 1
    message = lines[0].getMessage()
    assert "press_to_handle_ms=400 press_to_capture_ms=2100" in message
    assert lines[0].levelno == logging.WARNING
    assert "not recorded" in message
    controller.shutdown()
    _ = app


def test_a_quick_start_from_the_hotkey_is_logged_at_info(monkeypatch, caplog):
    ticks = {"now": 10_020}
    monkeypatch.setattr("stt_app.controller.message_clock_ms", lambda: ticks["now"])
    controller, app = _controller(monkeypatch)
    with caplog.at_level(logging.INFO, logger="test.controller"):
        controller.toggle_recording_from_hotkey(10_000)

    lines = _start_timing_lines(caplog)
    assert len(lines) == 1
    assert "press_to_handle_ms=20 press_to_capture_ms=20" in lines[0].getMessage()
    assert lines[0].levelno == logging.INFO
    controller.shutdown()
    _ = app


def test_a_start_without_a_hotkey_press_has_no_press_time(monkeypatch, caplog):
    """The tray, the overlay's Record button and a queued toggle carry no
    message time; neither may a press that stopped the previous recording
    lend its time to the next start."""
    monkeypatch.setattr("stt_app.controller.message_clock_ms", lambda: 50_000)
    controller, app = _controller(monkeypatch)
    monkeypatch.setattr(
        controller, "_submit_batch_transcription", lambda *a, **kw: None
    )
    with caplog.at_level(logging.INFO, logger="test.controller"):
        controller.start_recording()
        controller.toggle_recording_from_hotkey(49_000)  # stops it
        controller.start_recording()

    lines = _start_timing_lines(caplog)
    assert len(lines) == 2
    for line in lines:
        assert "press_to_handle_ms=n/a press_to_capture_ms=n/a" in line.getMessage()
    controller.shutdown()
    _ = app
