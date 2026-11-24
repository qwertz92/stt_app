"""A microphone that delivers no signal, or stops delivering, during a recording.

Field evidence (owner's log, 2026-10-25..11-24): six recordings of exact
digital silence -- every second about -97 dBFS, the loudest 100 ms below
0.00005 -- right after an output-endpoint change, one of them 21 s of speech
into a dead stream, each answered with advice to lower the silence gate.
"""

import logging

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

from stt_app.config import FALLBACK_HOTKEY
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
