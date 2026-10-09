"""Quitting with work pending: what the quit window is told, what a wait
does, and which recordings a quit keeps for the next start."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from datetime import datetime

import pytest
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
    # True even when the start is then refused further down: it names the
    # request (hotkey, Record button or tray), not a recording that may not
    # have started.
    assert any(
        message.startswith("Quit canceled by the request to record")
        for message in tray_messages
    ), tray_messages
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


def test_a_transcribed_unfinished_recording_waits_for_the_re_paste(
    monkeypatch, tmp_path
):
    """Owner's idea 2026-10-09: nothing is pasted on its own -- the window it
    was dictated for is long gone -- but the transcript is listed as not
    inserted, so the re-paste hotkey pastes it at the current caret, and it
    counts as not inserted."""
    controller, app, overlay, unfinished, history = _controller(monkeypatch, tmp_path)
    controller._executor = _DoneExecutor()
    monkeypatch.setattr(
        controller, "_transcribe_import_worker", lambda *_a: "hello again"
    )
    recording = _kept_recording(unfinished)

    ok, _text = controller.transcribe_unfinished_recording(recording)

    assert ok is True
    assert controller._text_inserter.calls == [], "pasted without being asked"
    [row] = controller._undelivered_inserts
    assert row.text == "hello again"
    assert row.parts[0][0] == history.load()[0], "an edit must reach the row"
    assert overlay.queue_kinds[-1] == ["undelivered"]
    # The time the recording was made, as every row shows it.
    assert "· 00:00:00 ·" in overlay.queue_updates[-1][0][1]
    assert controller.quit_pending_work().not_inserted == 1

    controller.repaste_last_transcript()

    assert [call[0] for call in controller._text_inserter.calls] == ["hello again"]
    assert controller._undelivered_inserts == []
    controller.shutdown()
    _ = app


def test_unfinished_transcripts_are_listed_on_the_qt_thread(monkeypatch, tmp_path):
    """The startup notice transcribes on a worker thread; the row it leaves
    is listed by the controller's own thread, never from the worker."""
    import threading

    controller, app, _overlay, unfinished, _history = _controller(monkeypatch, tmp_path)
    controller._executor = _DoneExecutor()
    monkeypatch.setattr(controller, "_transcribe_import_worker", lambda *_a: "later")
    recording = _kept_recording(unfinished)
    listed_on = []
    original = controller._record_undelivered_insert

    def _spy(*args, **kwargs):
        listed_on.append(threading.current_thread())
        return original(*args, **kwargs)

    monkeypatch.setattr(controller, "_record_undelivered_insert", _spy)
    worker = threading.Thread(
        target=controller.transcribe_unfinished_recording, args=(recording,)
    )
    worker.start()
    worker.join(10)
    assert listed_on == [], "listed from the worker thread"
    deadline = time.monotonic() + 5
    while not listed_on and time.monotonic() < deadline:
        app.processEvents()
    assert listed_on == [threading.main_thread()]
    assert [row.text for row in controller._undelivered_inserts] == ["later"]
    controller.shutdown()
    _ = app


def test_a_transcribed_unfinished_recording_is_archived_like_any_recording(
    monkeypatch, tmp_path
):
    """With "Archive every recording" on, a dictation's audio stays in the
    recordings folder; a recording from the startup notice was deleted
    instead (owner's report 2026-10-09). It now joins the archive under the
    archive's own name, the retention count applies, and its history entry
    points at it."""
    import re

    archive = tmp_path / "recordings"
    controller, app, _overlay, unfinished, history = _controller(
        monkeypatch,
        tmp_path,
        save_all_recordings=True,
        recordings_dir=str(archive),
        recordings_max_count=2,
    )
    controller._executor = _DoneExecutor()
    monkeypatch.setattr(controller, "_transcribe_import_worker", lambda *_a: "hello")
    archive.mkdir()
    oldest = archive / "recording_20260101_000000_000000.wav"
    older = archive / "recording_20260102_000000_000000.wav"
    for age, path in ((200, oldest), (100, older)):
        path.write_bytes(b"RIFF-old")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    recording = _kept_recording(unfinished)
    # Written by the quit, before every archived file: oldest of the three.
    kept_at = time.time() - 300
    os.utime(recording.path, (kept_at, kept_at))

    ok, _text = controller.transcribe_unfinished_recording(recording)

    assert ok is True
    assert unfinished.list_recordings() == []
    [entry] = history.load()
    archived = entry.source_audio_path
    assert os.path.dirname(archived) == os.path.abspath(archive)
    assert re.fullmatch(
        r"recording_20260101_000000_[0-9]{6}\.wav", os.path.basename(archived)
    ), archived
    with open(archived, "rb") as handle:
        assert handle.read() == b"RIFF-kept"
    # The count applies as to any archived recording, and this one counts as
    # the newest: pruned at once, its entry would point at nothing.
    assert not oldest.exists()
    assert older.exists()
    controller.shutdown()
    _ = app


def test_keep_last_recording_keeps_a_transcribed_unfinished_recording(
    monkeypatch, tmp_path
):
    """With "Keep last recording after successful transcription" on, a
    dictation's audio stays after its transcript; the notice's transcription
    deleted it. It goes to the recordings folder under its own name, where
    no retention count deletes it, and its entry points at it."""
    keep_dir = tmp_path / "recordings"
    controller, app, _overlay, unfinished, history = _controller(
        monkeypatch, tmp_path, save_last_wav=True, recordings_dir=str(keep_dir)
    )
    controller._executor = _DoneExecutor()
    monkeypatch.setattr(controller, "_transcribe_import_worker", lambda *_a: "hello")
    recording = _kept_recording(unfinished)

    ok, _text = controller.transcribe_unfinished_recording(recording)

    assert ok is True
    assert unfinished.list_recordings() == []
    kept = keep_dir / recording.path.name
    assert kept.read_bytes() == b"RIFF-kept"
    assert history.load()[0].source_audio_path == os.path.abspath(kept)
    controller.shutdown()
    _ = app


@pytest.mark.parametrize("failing_step", ["utime", "prune"])
def test_a_failure_after_the_archive_move_still_saves_the_transcript(
    monkeypatch, tmp_path, failing_step
):
    """Owner's rule: a transcript is never lost. Once the file is in the
    archive, the notice no longer offers it, so a step after the move --
    another archive file vanishing under the prune's age sort, a refused
    mtime update -- must not stop the history write, and the entry points
    at where the file really is."""
    archive = tmp_path / "recordings"
    controller, app, _overlay, unfinished, history = _controller(
        monkeypatch, tmp_path, save_all_recordings=True, recordings_dir=str(archive)
    )
    controller._executor = _DoneExecutor()
    monkeypatch.setattr(controller, "_transcribe_import_worker", lambda *_a: "hello")

    def _vanished(*_args, **_kwargs):
        raise FileNotFoundError("gone")

    if failing_step == "utime":
        monkeypatch.setattr("stt_app.controller.os.utime", _vanished)
    else:
        monkeypatch.setattr(controller, "_prune_recordings", _vanished)
    recording = _kept_recording(unfinished)

    ok, _text = controller.transcribe_unfinished_recording(recording)

    assert ok is True
    [entry] = history.load()
    assert os.path.isfile(entry.source_audio_path), entry.source_audio_path
    assert os.path.dirname(entry.source_audio_path) == os.path.abspath(archive)
    controller.shutdown()
    _ = app


def test_an_unresolvable_recordings_folder_keeps_the_file_and_links_it(
    monkeypatch, tmp_path
):
    controller, app, _overlay, unfinished, history = _controller(
        monkeypatch, tmp_path, save_all_recordings=True
    )
    controller._executor = _DoneExecutor()
    monkeypatch.setattr(controller, "_transcribe_import_worker", lambda *_a: "hello")

    def _refuse():
        raise OSError("no recordings folder")

    monkeypatch.setattr(controller, "_resolve_recordings_dir", _refuse)
    recording = _kept_recording(unfinished)

    ok, _text = controller.transcribe_unfinished_recording(recording)

    assert ok is True
    assert recording.path.is_file()
    assert history.load()[0].source_audio_path == os.path.abspath(recording.path)
    controller.shutdown()
    _ = app


def test_the_prune_skips_an_archive_file_that_vanishes_meanwhile(monkeypatch, tmp_path):
    """Another prune or the user can delete an archive file between the
    listing and the age sort; the prune then goes on with the rest."""
    controller, app, _overlay, _unfinished, _history = _controller(
        monkeypatch, tmp_path
    )
    archive = tmp_path / "recordings"
    archive.mkdir()
    names = [f"recording_2026010{day}_000000_000000.wav" for day in (1, 2, 3)]
    for age, name in zip((300, 200, 100), names, strict=True):
        path = archive / name
        path.write_bytes(b"RIFF")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    real_getmtime = os.path.getmtime

    def _getmtime(path):
        if os.path.basename(path) == names[1]:
            raise FileNotFoundError(path)
        return real_getmtime(path)

    monkeypatch.setattr("stt_app.controller.os.path.getmtime", _getmtime)

    controller._prune_recordings(str(archive), 1)

    assert not (archive / names[0]).exists()
    assert (archive / names[2]).exists()
    controller.shutdown()
    _ = app


def test_an_archive_move_that_fails_keeps_the_file_and_links_it(monkeypatch, tmp_path):
    archive = tmp_path / "recordings"
    controller, app, _overlay, unfinished, history = _controller(
        monkeypatch, tmp_path, save_all_recordings=True, recordings_dir=str(archive)
    )
    controller._executor = _DoneExecutor()
    monkeypatch.setattr(controller, "_transcribe_import_worker", lambda *_a: "hello")

    def _refuse(*_args, **_kwargs):
        raise OSError("locked")

    monkeypatch.setattr("stt_app.controller.shutil.move", _refuse)
    recording = _kept_recording(unfinished)

    ok, _text = controller.transcribe_unfinished_recording(recording)

    assert ok is True
    assert recording.path.is_file(), "a failed move deleted the recording"
    assert history.load()[0].source_audio_path == os.path.abspath(recording.path)
    controller.shutdown()
    _ = app


def test_a_gap_transcript_keeps_its_audio_out_of_the_pruned_archive(
    monkeypatch, tmp_path
):
    """The only audio of a transcript with a gap marker is never put where
    the archive's retention count could delete it."""
    archive = tmp_path / "recordings"
    controller, app, _overlay, unfinished, history = _controller(
        monkeypatch,
        tmp_path,
        save_all_recordings=True,
        recordings_dir=str(archive),
        recordings_max_count=1,
    )
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
    assert not archive.exists() or not any(archive.iterdir())
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
