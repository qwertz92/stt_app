"""Audio retention decisions of 2026-11-24 (D1-D4).

D1: a recording without any signal is not archived. D2: the recordings count
never prunes a file a queued or running transcription still needs. D3: kept
copies count toward the same limit as archived recordings. D4: a recording
without a transcript is in a durable file before the managed slot is
overwritten, and that file goes once the transcript is in History.
"""

import concurrent.futures
import logging
import os
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

from stt_app.app_paths import ARCHIVED_RECORDING_NAME_RE, KEPT_RECORDING_NAME_RE
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


# --- D2 / D3 -----------------------------------------------------------------


def _files(folder: Path, names_and_ages: list[tuple[str, int]]) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, age in names_and_ages:
        path = folder / name
        path.write_bytes(b"RIFF")
        os.utime(path, (1_800_000_000 + age, 1_800_000_000 + age))
        paths.append(path)
    return paths


def test_the_prune_skips_a_file_a_waiting_transcription_will_read(
    monkeypatch, tmp_path
):
    controller, app, _unfinished = _controller(monkeypatch, tmp_path)
    folder = tmp_path / "recordings"
    oldest, middle, newest = _files(
        folder,
        [
            ("recording_20261101_100000_000000.wav", 0),
            ("recording_20261101_110000_000000.wav", 1),
            ("recording_20261101_120000_000000.wav", 2),
        ],
    )

    with controller._audio_file_in_use(str(oldest)):
        controller._prune_recordings(str(folder), 2)

    assert oldest.exists(), "History's Retranscribe is about to read it"
    assert not middle.exists(), "the next oldest goes instead"
    assert newest.exists()
    # Released, it is pruned like any other file.
    controller._prune_recordings(str(folder), 1)
    assert not oldest.exists()
    controller.shutdown()
    _ = app


class _PruningExecutor:
    """Runs the import's work after a dictation ended and pruned the archive,
    which is what happens while a retranscription waits in the lane."""

    def __init__(self, controller, folder):
        self.controller = controller
        self.folder = folder
        self.read_paths = []

    def submit(self, fn, audio_source, *args, **kwargs):
        self.controller._prune_recordings(str(self.folder), 1)
        future = concurrent.futures.Future()
        self.read_paths.append(Path(audio_source).exists())
        future.set_result("transcript")
        return future

    def shutdown(self, wait=False, cancel_futures=False):
        pass


def test_a_queued_retranscription_keeps_its_archived_file(monkeypatch, tmp_path):
    # The known-limitations entry of 2026-10-23: the archive at its count, the
    # oldest file retranscribed, a dictation ending during the wait.
    controller, app, _unfinished = _controller(monkeypatch, tmp_path)
    folder = tmp_path / "recordings"
    oldest, newest = _files(
        folder,
        [
            ("recording_20261101_100000_000000.wav", 0),
            ("recording_20261101_120000_000000.wav", 1),
        ],
    )
    controller._executor = _PruningExecutor(controller, folder)

    ok, text = controller.transcribe_audio_file(str(oldest))

    assert (ok, text) == (True, "transcript")
    assert controller._executor.read_paths == [True]
    assert oldest.exists()
    assert not newest.exists()
    controller.shutdown()
    _ = app


def test_kept_copies_count_toward_keep_recordings_and_unfinished_files_never(
    monkeypatch, tmp_path
):
    controller, app, _unfinished = _controller(monkeypatch, tmp_path)
    folder = tmp_path / "recordings"
    old_kept, untranscribed, archived, new_kept = _files(
        folder,
        [
            ("kept_20261101_090000_rec-a.de.wav", 0),
            ("unfinished_20261101_093000_rec-b.wav", 1),
            ("recording_20261101_100000_000000.wav", 2),
            ("kept_20261101_110000_rec-c.wav", 3),
        ],
    )

    controller._prune_recordings(str(folder), 2)

    assert not old_kept.exists(), "the oldest of the three counted files"
    assert archived.exists() and new_kept.exists()
    assert untranscribed.exists(), "may be a recording nobody transcribed"
    controller.shutdown()
    _ = app


def test_a_kept_copy_is_named_so_the_count_finds_it(monkeypatch, tmp_path):
    from datetime import datetime

    controller, app, _unfinished = _controller(monkeypatch, tmp_path)

    kept = controller._keep_audio_in_recordings_folder(
        _SPEECH_LIKE,
        recording_id="rec-1",
        recorded_at=datetime(2026, 11, 24, 10, 0, 0),  # noqa: DTZ001 (local wall clock, as the file names are)
        language_mode="de",
    )

    assert Path(kept).parent == tmp_path / "recordings"
    assert KEPT_RECORDING_NAME_RE.fullmatch(Path(kept).name), Path(kept).name
    controller.shutdown()
    _ = app
