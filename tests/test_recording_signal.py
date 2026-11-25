"""A microphone that delivers no signal, or stops delivering, during a recording.

Field evidence (owner's log, 2026-10-25..11-24): six recordings of exact
digital silence -- every second about -97 dBFS, the loudest 100 ms below
0.00005 -- right after an output-endpoint change, one of them 21 s of speech
into a dead stream, each answered with advice to lower the silence gate.
"""

import logging
from dataclasses import replace

import numpy as np
from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)
from speech_fixtures import room_tone, wav_bytes

from stt_app.config import (
    AUDIO_NO_SIGNAL_HINT_S,
    FALLBACK_HOTKEY,
    OVERLAY_WARNING_NO_SIGNAL,
)
from stt_app.settings_store import AppSettings
from stt_app.transcript_history import TranscriptHistoryStore


class DeferredExecutor:
    """Captures submitted work without running it."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))

    def shutdown(self, wait=False, cancel_futures=False):
        pass


def _controller(monkeypatch, tmp_path, **settings_overrides):
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _s, **kw: FakeStreamingTranscriber(),
    )
    FakeCapture.instances = []
    settings_overrides.setdefault("silence_gate_enabled", True)
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
        model_size="small",
        **settings_overrides,
    )
    overlay = FakeOverlay()
    controller, app = make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=TranscriptHistoryStore(tmp_path / "history.json"),
        logger=logging.getLogger("test.recording_signal"),
    )
    controller._executor = DeferredExecutor()
    return controller, app, overlay


def _stop_with(controller, wav):
    controller.start_recording()
    FakeCapture.instances[-1]._wav_bytes = wav
    controller.stop_recording()


def _messages(caplog, prefix):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


# A muted or dead endpoint as the owner's log shows it: sub-LSB noise that the
# WAV conversion turns into zeros.
_DEAD_MICROPHONE = np.zeros(16000 * 3, dtype=np.int16)


def test_a_level_gate_skip_logs_that_the_speech_check_did_not_run(
    monkeypatch, tmp_path, caplog
):
    # The level gate decided before Silero was asked; "unavailable" read as a
    # broken speech model in every such line.
    controller, app, _overlay = _controller(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO)

    _stop_with(controller, wav_bytes(room_tone(3.0, rms=0.001)))

    lines = _messages(caplog, "silence_gate_skipped")
    assert len(lines) == 1
    assert "reason=level" in lines[0]
    assert "silero_max_probability=not_run" in lines[0]
    controller.shutdown()
    _ = app


def test_a_recording_without_signal_says_the_microphone_sent_nothing(
    monkeypatch, tmp_path, caplog
):
    # The owner spoke 21 s into a dead stream and was told to lower the
    # silence gate, which cannot help with zeros.
    controller, app, overlay = _controller(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO)

    _stop_with(controller, wav_bytes(_DEAD_MICROPHONE))

    assert controller._executor.calls == []
    state, detail = overlay.states[-1]
    assert state == "Done"
    assert "microphone delivered no signal" in detail
    assert "silence gate" not in detail
    assert "muted" in detail
    lines = _messages(caplog, "silence_gate_skipped")
    assert len(lines) == 1
    assert "reason=no_signal" in lines[0]
    controller.shutdown()
    _ = app


def test_a_quiet_room_still_gets_the_silence_gate_advice(monkeypatch, tmp_path):
    # A live microphone in a quiet room (the owner's quietest: 0.0009) is the
    # gate's case, and lowering the gate is the right advice there.
    controller, app, overlay = _controller(monkeypatch, tmp_path)

    _stop_with(controller, wav_bytes(room_tone(3.0, rms=0.0004)))

    state, detail = overlay.states[-1]
    assert state == "Done"
    assert "lower the silence gate" in detail
    assert "no signal" not in detail
    controller.shutdown()
    _ = app


# --- While recording: the health poll -------------------------------------
#
# `_on_capture_health_tick` is driven directly with a fake clock; the fake
# capture answers `health()` as a real one whose stream stopped calling back
# (`silent_s` grows) or sends zeros (`no_signal_s` grows).


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _recording(monkeypatch, tmp_path, *, mode="batch", **settings):
    clock = _Clock()
    monkeypatch.setattr("stt_app.controller._health_clock", clock)
    controller, app, overlay = _controller(monkeypatch, tmp_path, **settings)
    if mode == "streaming":
        controller._settings = replace(controller._settings, mode="streaming")
    controller.start_recording()
    capture = FakeCapture.instances[-1]
    assert controller._audio_capture is capture
    return controller, app, overlay, capture, clock


def _tick(
    controller,
    clock,
    capture,
    *,
    silent_s=None,
    no_signal_s=None,
    signal_seen=None,
    step=0.5,
):
    clock.now += step
    if signal_seen is not None:
        capture.signal_seen = signal_seen
    if silent_s is not None:
        capture.silent_s = silent_s
    if no_signal_s is not None:
        capture.no_signal_s = no_signal_s
    controller._on_capture_health_tick()


def _warning(overlay):
    return overlay.state_kwargs[-1].get("warning")


def test_a_microphone_sending_zeros_is_shown_at_once_and_cleared_by_signal(
    monkeypatch, tmp_path
):
    controller, app, overlay, capture, clock = _recording(monkeypatch, tmp_path)

    _tick(controller, clock, capture, no_signal_s=AUDIO_NO_SIGNAL_HINT_S - 0.5)
    assert _warning(overlay) is None

    _tick(controller, clock, capture, no_signal_s=AUDIO_NO_SIGNAL_HINT_S)
    state, detail = overlay.states[-1]
    assert state == "Listening"
    assert _warning(overlay) == OVERLAY_WARNING_NO_SIGNAL
    _assert_neutral_no_sound_text(detail)
    assert controller._audio_capture is capture, "the recording runs on"

    _tick(controller, clock, capture, no_signal_s=0.0)
    state, detail = overlay.states[-1]
    assert state == "Listening"
    assert _warning(overlay) is None
    assert "Speak now" in detail
    controller.stop_recording()
    controller.shutdown()
    _ = app


def test_the_no_signal_warning_shows_in_streaming_too(monkeypatch, tmp_path):
    controller, app, overlay, capture, clock = _recording(
        monkeypatch, tmp_path, mode="streaming"
    )
    assert controller._streaming_recording

    _tick(controller, clock, capture, no_signal_s=3.0)

    assert overlay.states[-1][0] == "Listening"
    assert _warning(overlay) == OVERLAY_WARNING_NO_SIGNAL
    controller.shutdown()
    _ = app


def _assert_neutral_no_sound_text(detail):
    # True whatever the cause (muted, dead, or a headset's noise gate in a
    # pause): no claim that speech is lost, no instruction to stop.
    assert "No sound is arriving from the microphone" in detail
    assert "not recorded" not in detail
    assert "stop" not in detail.lower()


def test_zeros_after_sound_are_shown_after_a_longer_run(monkeypatch, tmp_path):
    # The owner's dead endpoints turned to exact zeros mid-session; a pause
    # after speech is room tone on a live microphone, never seconds of zeros.
    controller, app, overlay, capture, clock = _recording(monkeypatch, tmp_path)

    _tick(controller, clock, capture, signal_seen=True, no_signal_s=2.5)
    assert _warning(overlay) is None, "a short run after sound is a pause"

    _tick(controller, clock, capture, signal_seen=True, no_signal_s=3.0)
    assert _warning(overlay) == OVERLAY_WARNING_NO_SIGNAL
    _assert_neutral_no_sound_text(overlay.states[-1][1])

    _tick(controller, clock, capture, signal_seen=True, no_signal_s=0.0)
    assert _warning(overlay) is None
    controller.stop_recording()
    controller.shutdown()
    _ = app


def test_the_live_text_comes_back_when_sound_returns(monkeypatch, tmp_path):
    controller, app, overlay, capture, clock = _recording(
        monkeypatch, tmp_path, mode="streaming"
    )
    controller._stream_text_state.live_text = "hallo welt"

    _tick(controller, clock, capture, signal_seen=True, no_signal_s=3.0)
    assert _warning(overlay) == OVERLAY_WARNING_NO_SIGNAL

    _tick(controller, clock, capture, signal_seen=True, no_signal_s=0.0)
    assert _warning(overlay) is None
    assert overlay.states[-1] == ("Listening", "Live: hallo welt")
    controller.shutdown()
    _ = app


def test_normal_callback_jitter_raises_nothing(monkeypatch, tmp_path):
    # The owner's worst gap between blocks in 1029 recordings: 358 ms.
    controller, app, overlay, capture, clock = _recording(monkeypatch, tmp_path)
    painted = len(overlay.states)

    for _ in range(10):
        _tick(controller, clock, capture, silent_s=0.36)

    assert len(overlay.states) == painted
    assert controller._audio_capture is capture
    controller.shutdown()
    _ = app


def test_a_stream_that_stops_mid_recording_warns_then_delivers_what_it_has(
    monkeypatch, tmp_path, caplog
):
    controller, app, overlay, capture, clock = _recording(
        monkeypatch, tmp_path, silence_gate_enabled=False
    )
    tray = []
    controller.busy_overlay_error.connect(tray.append)
    caplog.set_level(logging.INFO)

    # A starved callback thread looks the same for a while: warn, keep going.
    _tick(controller, clock, capture, silent_s=2.5, step=2.0)
    _tick(controller, clock, capture, silent_s=3.0)
    assert overlay.states[-1][0] == "Listening"
    assert _warning(overlay) == OVERLAY_WARNING_NO_SIGNAL
    assert controller._audio_capture is capture
    assert tray == []

    # Past the hard limit the stream is dead: the recording ends, its audio
    # goes through the normal stop and is transcribed.
    for _ in range(19):
        _tick(controller, clock, capture, silent_s=capture.silent_s + 0.5)
    assert controller._audio_capture is None
    assert capture.stopped
    assert capture.stop_calls == [{"drain": False}]
    assert len(controller._executor.calls) == 1, "what was recorded is transcribed"
    assert len(tray) == 1
    assert "stopped sending audio" in tray[0]
    assert any("audio_capture_stalled" in r.getMessage() for r in caplog.records)
    controller.shutdown()
    _ = app


def test_a_burst_after_a_stall_clears_the_warning_and_loses_nothing(
    monkeypatch, tmp_path
):
    controller, app, overlay, capture, clock = _recording(monkeypatch, tmp_path)

    for silent_s in (2.0, 4.0, 6.0, 8.0):
        _tick(controller, clock, capture, silent_s=silent_s, step=2.0)
    assert _warning(overlay) is not None
    _tick(controller, clock, capture, silent_s=0.1)

    assert _warning(overlay) is None
    assert controller._audio_capture is capture
    assert not capture.stopped
    controller.shutdown()
    _ = app


def test_a_stream_portaudio_reports_stopped_ends_at_the_first_limit(
    monkeypatch, tmp_path
):
    controller, app, _overlay, capture, clock = _recording(
        monkeypatch, tmp_path, silence_gate_enabled=False
    )
    capture.stream_active = False

    _tick(controller, clock, capture, silent_s=1.5, step=1.5)
    assert controller._audio_capture is capture
    _tick(controller, clock, capture, silent_s=2.0)

    assert controller._audio_capture is None
    assert len(controller._executor.calls) == 1
    controller.shutdown()
    _ = app


def test_system_sleep_is_not_a_stalled_microphone(monkeypatch, tmp_path):
    controller, app, overlay, capture, clock = _recording(monkeypatch, tmp_path)
    painted = len(overlay.states)

    # An hour asleep: no tick ran, the last block is an hour old.
    _tick(controller, clock, capture, silent_s=3600.0, step=3600.0)
    assert len(overlay.states) == painted
    assert controller._audio_capture is capture

    # A stream that stays dead after the resume is caught, counted from the
    # resume, not from the last block before the sleep.
    for silent_s in (3600.5, 3601.0, 3601.5):
        _tick(controller, clock, capture, silent_s=silent_s)
    assert len(overlay.states) == painted
    _tick(controller, clock, capture, silent_s=3602.0)
    assert _warning(overlay) is not None
    assert controller._audio_capture is capture
    controller.shutdown()
    _ = app


def test_a_streaming_stream_that_stops_is_finalized_with_its_audio(
    monkeypatch, tmp_path
):
    controller, app, _overlay, capture, clock = _recording(
        monkeypatch, tmp_path, mode="streaming"
    )
    finalized = []
    monkeypatch.setattr(
        controller,
        "_submit_stream_finalize",
        lambda **kwargs: finalized.append(kwargs),
    )

    for _ in range(25):
        _tick(controller, clock, capture, silent_s=capture.silent_s + 0.5)

    assert controller._audio_capture is None
    assert capture.stop_calls == [{"drain": False}]
    assert len(finalized) == 1
    assert finalized[0]["wav_bytes"] == capture._wav_bytes
    controller.shutdown()
    _ = app


def test_the_health_poll_stops_with_the_recording(monkeypatch, tmp_path):
    controller, app, _overlay, _capture, _clock = _recording(monkeypatch, tmp_path)
    assert controller._capture_health_timer.isActive()

    controller.stop_recording()

    assert not controller._capture_health_timer.isActive()
    controller.shutdown()
    _ = app
