"""Quitting with work pending: what the quit window is told, what a wait
does, and which recordings a quit keeps for the next start."""

from __future__ import annotations

import concurrent.futures
import logging
import os
from datetime import datetime

from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)

from stt_app.config import FALLBACK_HOTKEY
from stt_app.controller import PendingQuitWork
from stt_app.settings_store import AppSettings
from stt_app.transcript_history import TranscriptHistoryStore
from stt_app.unfinished_recordings import UnfinishedRecordingStore


class _DeferredExecutor:
    """Takes work without running it: every job stays in the queue."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))

    def shutdown(self, wait=False, cancel_futures=False):
        pass


class _DoneExecutor:
    """Runs work at once and hands back a finished future."""

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait=False, cancel_futures=False):
        pass


def _controller(monkeypatch, tmp_path, **settings_overrides):
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _s, **kw: FakeStreamingTranscriber(),
    )
    FakeCapture.instances = []
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
        model_size="small",
        silence_gate_enabled=False,
        **settings_overrides,
    )
    overlay = FakeOverlay()
    unfinished = UnfinishedRecordingStore(tmp_path / "unfinished")
    history = TranscriptHistoryStore(tmp_path / "history.json")
    controller, app = make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=history,
        unfinished_recording_store=unfinished,
        logger=logging.getLogger("test.controller.quit"),
    )
    controller._executor = _DeferredExecutor()
    return controller, app, overlay, unfinished, history


def _record(controller, audio: bytes) -> int:
    controller.start_recording()
    FakeCapture.instances[-1]._wav_bytes = audio
    controller.stop_recording()
    return controller._active_request_token


def test_nothing_pending_quits_without_asking(monkeypatch, tmp_path):
    controller, app, _overlay, _unfinished, _history = _controller(
        monkeypatch, tmp_path
    )

    assert controller.quit_pending_work() == PendingQuitWork()
    assert controller.quit_pending_work().asks_before_quit is False
    controller.shutdown()
    _ = app


def test_queued_and_open_recordings_are_reported_as_pending(monkeypatch, tmp_path):
    controller, app, _overlay, _unfinished, _history = _controller(
        monkeypatch, tmp_path
    )
    _record(controller, b"RIFF-one")
    _record(controller, b"RIFF-two")
    controller.start_recording()

    pending = controller.quit_pending_work()

    assert pending.recording is True
    assert pending.transcribing == 2
    assert pending.can_wait and pending.asks_before_quit
    controller.shutdown()
    _ = app


def test_a_quit_keeps_every_queued_recording_and_the_open_one(monkeypatch, tmp_path):
    """The managed last recording holds the newest recording only, so before
    this a quit with a queue dropped all the older ones, and the open capture
    as well."""
    controller, app, _overlay, unfinished, _history = _controller(monkeypatch, tmp_path)
    first = _record(controller, b"RIFF-one")
    second = _record(controller, b"RIFF-two")
    first_id = controller._jobs[first].source_recording_id
    second_id = controller._jobs[second].source_recording_id
    controller.start_recording()
    FakeCapture.instances[-1]._wav_bytes = b"RIFF-open"

    controller.shutdown()

    kept = unfinished.list_recordings()
    assert sorted(item.path.read_bytes() for item in kept) == [
        b"RIFF-one",
        b"RIFF-open",
        b"RIFF-two",
    ]
    ids = {item.recording_id for item in kept}
    assert {first_id, second_id} <= ids
    _ = app


def test_a_quit_does_not_keep_a_finished_or_cancelled_recording(monkeypatch, tmp_path):
    """A finished transcript waiting for its paste is in history, and the user
    stopped a cancelled one: offering either at the next start would make a
    second transcript of the same dictation."""
    controller, app, _overlay, unfinished, _history = _controller(monkeypatch, tmp_path)
    finished = _record(controller, b"RIFF-finished")
    cancelled = _record(controller, b"RIFF-cancelled")
    running = _record(controller, b"RIFF-running")
    controller._jobs[finished].insertion_deferred = True
    controller._jobs[cancelled].aborting = True

    controller.shutdown()

    assert [item.path.read_bytes() for item in unfinished.list_recordings()] == [
        b"RIFF-running"
    ]
    _ = (running, app)


def test_a_quit_keeps_the_failures_waiting_for_retry_once_each(monkeypatch, tmp_path):
    controller, app, _overlay, unfinished, _history = _controller(monkeypatch, tmp_path)
    controller._recorded_at_by_recording_id["older"] = datetime.fromisoformat(
        "2026-10-09T09:01:02"
    )
    controller._hold_failed_audio_for_retry(b"RIFF-older", "older")
    controller._hold_failed_audio_for_retry(b"RIFF-newer", "newer")
    # A retry of the newest failure is running: the same recording twice.
    token = controller._next_request_token()
    controller._store_request_audio(token, b"RIFF-newer", controller._settings)
    controller._register_transcription_job(
        token, controller._settings, "batch", source_recording_id="newer"
    )

    controller.shutdown()

    kept = {item.recording_id: item for item in unfinished.list_recordings()}
    assert set(kept) == {"older", "newer"}
    assert kept["older"].recorded_at == datetime.fromisoformat("2026-10-09T09:01:02")
    assert kept["newer"].path.read_bytes() == b"RIFF-newer"
    _ = app


def test_waiting_to_quit_stops_the_open_recording(monkeypatch, tmp_path):
    controller, app, _overlay, _unfinished, _history = _controller(
        monkeypatch, tmp_path
    )
    controller.start_recording()

    controller.hold_for_quit()

    assert controller._audio_capture is None, "the open recording was not stopped"
    assert controller.quit_pending_work().transcribing == 1
    controller.shutdown()
    _ = app


def test_a_new_recording_during_the_wait_calls_the_quit_off(monkeypatch, tmp_path):
    """Owner decision 2026-10-09: the wait used to refuse a new recording
    until "Don't quit" was chosen. Dictating again now releases the hold,
    tells the quit window to close and records as usual."""
    controller, app, _overlay, _unfinished, _history = _controller(
        monkeypatch, tmp_path
    )
    tray_messages = []
    canceled = []
    controller.busy_overlay_error.connect(tray_messages.append)
    controller.quit_canceled_by_recording.connect(lambda: canceled.append(True))
    controller.hold_for_quit()
    starts = len(FakeCapture.instances)

    controller.start_recording()

    assert len(FakeCapture.instances) == starts + 1, "the recording was refused"
    assert controller._audio_capture is not None
    assert canceled == [True]
    assert any("Quit canceled" in message for message in tray_messages)
    # The hold is gone: a stray hold-free poll path must not stop it, and a
    # second start emits nothing more.
    assert controller._quit_hold is False
    controller.stop_recording()
    controller.start_recording()
    assert canceled == [True]
    controller.shutdown()
    _ = app


def _kept_recording(unfinished, audio=b"RIFF-kept", recording_id="kept1"):
    unfinished.save(
        audio,
        recording_id=recording_id,
        recorded_at=datetime.fromisoformat("2026-01-01T00:00:00"),
    )
    return unfinished.find(recording_id)


def test_a_transcribed_unfinished_recording_goes_to_history_and_its_file_goes(
    monkeypatch, tmp_path
):
    controller, app, _overlay, unfinished, history = _controller(monkeypatch, tmp_path)
    controller._executor = _DoneExecutor()
    monkeypatch.setattr(
        controller, "_transcribe_import_worker", lambda *_a: "  hello again  "
    )
    recording = _kept_recording(unfinished)

    ok, text = controller.transcribe_unfinished_recording(recording)

    assert (ok, text) == (True, "hello again")
    [entry] = history.load()
    assert entry.text == "hello again"
    assert entry.source_recording_id == "kept1"
    assert unfinished.list_recordings() == []
    controller.shutdown()
    _ = app


def test_a_transcript_with_a_gap_keeps_its_recording(monkeypatch, tmp_path):
    controller, app, _overlay, unfinished, history = _controller(monkeypatch, tmp_path)
    controller._executor = _DoneExecutor()
    monkeypatch.setattr(
        controller,
        "_transcribe_import_worker",
        lambda *_a: "first [no text returned for 3:00-6:00] rest",
    )
    recording = _kept_recording(unfinished)

    ok, _text = controller.transcribe_unfinished_recording(recording)

    assert ok is True
    assert recording.path.is_file()
    assert history.load()[0].source_audio_path == os.path.abspath(recording.path)
    controller.shutdown()
    _ = app


def test_a_failed_transcription_keeps_the_recording(monkeypatch, tmp_path):
    controller, app, _overlay, unfinished, history = _controller(monkeypatch, tmp_path)
    controller._executor = _DoneExecutor()

    def _fail(*_args):
        raise RuntimeError("provider down")

    monkeypatch.setattr(controller, "_transcribe_import_worker", _fail)
    recording = _kept_recording(unfinished)

    ok, message = controller.transcribe_unfinished_recording(recording)

    assert (ok, message) == (False, "provider down")
    assert recording.path.is_file()
    assert history.load() == []
    controller.shutdown()
    _ = app


def test_the_wait_lasts_until_the_last_paste_has_settled(monkeypatch, tmp_path):
    """Quitting inside the restore window flushes the clipboard restore at
    once, and a target that reads the clipboard late then pastes the user's
    old clipboard instead of the transcript (review of 3ee1e23). A settling
    paste alone is no reason to ask, though."""
    controller, app, _overlay, _unfinished, _history = _controller(
        monkeypatch, tmp_path
    )
    remaining = [1.2]
    controller._text_inserter.paste_pace_remaining_s = lambda: remaining[0]

    pending = controller.quit_pending_work()

    assert pending.can_wait is True
    assert pending.asks_before_quit is False
    remaining[0] = 0.0
    assert controller.quit_pending_work().can_wait is False
    controller.shutdown()
    _ = app
