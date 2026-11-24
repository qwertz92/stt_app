"""Audio retention decisions of 2026-11-24 (D1-D4).

D1: a recording without any signal is not archived. D2: the recordings count
never prunes a file a queued or running transcription still needs. D3: kept
copies count toward the same limit as archived recordings. D4: a recording
without a transcript is in a durable file before the managed slot is
overwritten, and that file goes once the transcript is in History.
"""

import logging
from pathlib import Path

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

from stt_app.app_paths import ARCHIVED_RECORDING_NAME_RE
from stt_app.config import FALLBACK_HOTKEY
from stt_app.last_recording_store import LastRecordingStore
from stt_app.settings_store import AppSettings
from stt_app.transcript_history import TranscriptHistoryStore
from stt_app.unfinished_recordings import UnfinishedRecordingStore


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
    settings_overrides.setdefault("silence_gate_enabled", False)
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
        model_size="small",
        recordings_dir=str(tmp_path / "recordings"),
        **settings_overrides,
    )
    unfinished = UnfinishedRecordingStore(tmp_path / "unfinished")
    controller, app = make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=TranscriptHistoryStore(tmp_path / "history.json"),
        last_recording_store=LastRecordingStore(
            audio_path=tmp_path / "last_recording.wav",
            state_path=tmp_path / "last_recording.json",
        ),
        unfinished_recording_store=unfinished,
        logger=logging.getLogger("test.recording_retention"),
    )
    controller._executor = DeferredExecutor()
    return controller, app, unfinished


def _record(controller, wav):
    controller.start_recording()
    FakeCapture.instances[-1]._wav_bytes = wav
    controller.stop_recording()
    return controller._active_request_token


_SPEECH_LIKE = wav_bytes(room_tone(2.0, rms=0.05, seed=3))


# --- D1 ----------------------------------------------------------------------


def test_a_recording_without_signal_is_not_archived(monkeypatch, tmp_path):
    controller, app, _unfinished = _controller(
        monkeypatch, tmp_path, save_all_recordings=True
    )

    _record(controller, wav_bytes(np.zeros(32000, dtype=np.int16)))

    # The fake capture records the archive write instead of making it.
    assert FakeCapture.instances[-1].last_saved_path is None
    # The managed slot still holds it, as before.
    assert (tmp_path / "last_recording.wav").is_file()
    controller.shutdown()
    _ = app


def test_a_quiet_recording_the_gate_rejects_is_still_archived(monkeypatch, tmp_path):
    # The gate can be wrong about quiet speech, so its rejection keeps the file.
    controller, app, _unfinished = _controller(
        monkeypatch, tmp_path, save_all_recordings=True, silence_gate_enabled=True
    )

    _record(controller, wav_bytes(room_tone(2.0, rms=0.001)))

    assert controller._executor.calls == [], "the gate skipped it"
    saved = FakeCapture.instances[-1].last_saved_path
    assert saved is not None
    assert ARCHIVED_RECORDING_NAME_RE.fullmatch(Path(saved).name)
    controller.shutdown()
    _ = app
