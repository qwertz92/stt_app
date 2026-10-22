"""The recording start: what the log says about it, and when the overlay
invites speech.

Owner report 2026-10-23 (slow work laptop, Defender XDR, warm stream on):
the first words of a recording were missing from the saved WAV, and he
believed the overlay was green. A warm stream drops every block while no
recording is attached, so whatever is said between the hotkey press and the
attach is gone -- and nothing in the log said how long that was. And "Speak
now" was painted once `capture.start()` returned, before the microphone had
delivered anything.
"""

from __future__ import annotations

import logging
import time

from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    make_controller,
)

from stt_app.config import FALLBACK_HOTKEY
from stt_app.settings_store import AppSettings


def _start_timing_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "recording_start_timing" in r.getMessage()]


def _controller(monkeypatch, capture_class=FakeCapture, overlay=None, **settings):
    FakeCapture.instances = []
    monkeypatch.setattr("stt_app.controller.AudioCapture", capture_class)
    values = {"hotkey": FALLBACK_HOTKEY, "mode": "batch", **settings}
    return make_controller(
        settings_store=FakeSettingsStore(AppSettings(**values)),
        overlay=overlay or FakeOverlay(),
    )


class _SilentCapture(FakeCapture):
    """A capture whose microphone has not delivered yet: a cold stream
    before its first block, or a warm stream that was silent at the attach."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_delivering_audio = False


_STARTING = "Starting dictation. Please wait for the 'Speak now' message."
_SPEAK_NOW = "Speak now. Press hotkey again to stop."
_NO_AUDIO_YET = "No audio from the microphone yet. Wait for 'Speak now'."


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


def _listening_paints(overlay: FakeOverlay) -> list[tuple[str, bool]]:
    return [
        (detail, bool(kwargs.get("starting")))
        for (state, detail), kwargs in zip(
            overlay.states, overlay.state_kwargs, strict=True
        )
        if state == "Listening"
    ]


def test_speak_now_waits_for_the_microphones_first_block(monkeypatch):
    """`capture.start()` returning is no proof that anything is recorded:
    a cold stream's first block comes 0.1-0.2 s later on HomeBase, and a
    device that starts late or a starved callback thread sends nothing for
    seconds. Until the first block the overlay stays "Starting"; the block
    paints "Speak now" in green."""
    overlay = FakeOverlay()
    controller, app = _controller(monkeypatch, _SilentCapture, overlay)

    controller.start_recording()

    assert _listening_paints(overlay) == [(_STARTING, True)]
    capture = FakeCapture.instances[-1]
    capture.deliver_first_block()

    assert _listening_paints(overlay) == [(_STARTING, True), (_SPEAK_NOW, False)]
    controller.shutdown()
    _ = app


def test_a_delivering_warm_stream_invites_speech_at_once(monkeypatch):
    """A warm stream that was delivering at the attach records from its next
    block, so "Speak now" comes right after the attach, as before."""
    overlay = FakeOverlay()
    controller, app = _controller(monkeypatch, FakeCapture, overlay)

    controller.start_recording()

    assert _listening_paints(overlay) == [(_STARTING, True), (_SPEAK_NOW, False)]
    controller.shutdown()
    _ = app


def test_a_first_block_after_the_stop_paints_nothing(monkeypatch):
    """A delayed writer re-checks its session: the block of a capture the
    user already stopped must not paint "Speak now" over the stop."""
    overlay = FakeOverlay()
    controller, app = _controller(monkeypatch, _SilentCapture, overlay)
    monkeypatch.setattr(
        controller, "_submit_batch_transcription", lambda *a, **kw: None
    )
    controller.start_recording()
    capture = FakeCapture.instances[-1]
    controller.stop_recording()
    after_stop = list(overlay.states)

    capture.deliver_first_block()

    assert overlay.states == after_stop
    controller.shutdown()
    _ = app


def test_a_stale_first_block_does_not_invite_speech_for_the_next_capture(
    monkeypatch,
):
    overlay = FakeOverlay()
    controller, app = _controller(monkeypatch, _SilentCapture, overlay)
    monkeypatch.setattr(
        controller, "_submit_batch_transcription", lambda *a, **kw: None
    )
    controller.start_recording()
    first = FakeCapture.instances[-1]
    controller.stop_recording()
    controller.start_recording()
    paints = len(overlay.states)

    first.deliver_first_block()

    assert len(overlay.states) == paints
    assert _listening_paints(overlay)[-1] == (_STARTING, True)
    controller.shutdown()
    _ = app


def test_a_slow_first_block_says_the_microphone_has_sent_nothing_yet(monkeypatch):
    """The first-callback watchdog's first stage (2 s without a block on a
    stream PortAudio still runs) keeps waiting; the overlay, still on
    "Starting", now says why -- the owner's question was whether the app was
    recording at all."""
    overlay = FakeOverlay()
    controller, app = _controller(monkeypatch, _SilentCapture, overlay)
    controller.start_recording()
    capture = FakeCapture.instances[-1]

    controller._on_audio_callback_watchdog_timeout()

    assert _listening_paints(overlay)[-1] == (_NO_AUDIO_YET, True)
    assert controller._audio_capture is capture
    capture.deliver_first_block()
    assert _listening_paints(overlay)[-1] == (_SPEAK_NOW, False)
    controller.shutdown()
    _ = app


def test_a_slow_first_block_after_speak_now_leaves_the_overlay_alone(monkeypatch):
    """A warm stream that was delivering got "Speak now" at the attach; if it
    stalls right after, its audio waits in the buffer, and the watchdog's
    first stage repaints nothing (as before 2026-10-23)."""
    overlay = FakeOverlay()
    controller, app = _controller(monkeypatch, FakeCapture, overlay)
    controller.start_recording()
    paints = list(overlay.states)

    controller._on_audio_callback_watchdog_timeout()

    assert overlay.states == paints
    controller.shutdown()
    _ = app


def test_a_streaming_handshake_does_not_invite_speech_before_the_first_block(
    monkeypatch,
):
    """The local stream connects at once, and its completion used to paint
    "Streaming active. Speak now" whatever the microphone had delivered."""
    overlay = FakeOverlay()
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: FakeStreamingTranscriber(),
    )
    connected: list[bool] = []
    controller, app = _controller(
        monkeypatch, _SilentCapture, overlay, mode="streaming", model_size="small"
    )
    controller.stream_connect_finished.connect(
        lambda _generation, ok, _text: connected.append(ok)
    )
    controller.start_recording()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not connected:
        app.processEvents()
        time.sleep(0.01)
    assert connected == [True]

    assert _listening_paints(overlay) == [(_STARTING, True)]
    FakeCapture.instances[-1].deliver_first_block()
    assert _listening_paints(overlay)[-1] == (
        "Streaming active. Speak now, press hotkey to finalize.",
        False,
    )
    controller.shutdown()
    _ = app
