from __future__ import annotations

import io
import wave
from datetime import datetime
from pathlib import Path

import pytest

from stt_app.unfinished_recordings import UnfinishedRecordingStore


def _wav(seconds: float, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


def test_a_saved_recording_is_listed_with_its_time_length_and_size(tmp_path):
    store = UnfinishedRecordingStore(tmp_path / "unfinished")
    audio = _wav(2.5)

    path = store.save(
        audio,
        recording_id="abc123",
        recorded_at=datetime.fromisoformat("2026-10-09T14:03:12"),
    )

    assert path is not None and path.name == "unfinished_20261009_140312_abc123.wav"
    [recording] = store.list_recordings()
    assert recording.path == path
    assert recording.recording_id == "abc123"
    assert recording.recorded_at == datetime.fromisoformat("2026-10-09T14:03:12")
    assert recording.duration_s == pytest.approx(2.5)
    assert recording.size_bytes == len(audio)


def test_the_same_recording_is_kept_once(tmp_path):
    """Startup adopts the managed slot's recording and the quit saved it too:
    without the id check every start would add another copy."""
    store = UnfinishedRecordingStore(tmp_path)
    first = store.save(
        _wav(1),
        recording_id="r1",
        recorded_at=datetime.fromisoformat("2026-01-01T00:00:00"),
    )

    second = store.save(
        _wav(1),
        recording_id="r1",
        recorded_at=datetime.fromisoformat("2026-01-02T00:00:00"),
    )

    assert first is not None
    assert second is None
    assert [item.path for item in store.list_recordings()] == [first]


def test_listing_ignores_files_that_are_not_kept_recordings(tmp_path):
    store = UnfinishedRecordingStore(tmp_path)
    store.save(
        _wav(1),
        recording_id="r1",
        recorded_at=datetime.fromisoformat("2026-01-01T00:00:00"),
    )
    (tmp_path / ".unfinished_20260101_000000_r2.wav.x.tmp").write_bytes(b"partial")
    (tmp_path / "notes.txt").write_text("mine", encoding="utf-8")

    assert [item.recording_id for item in store.list_recordings()] == ["r1"]


def test_a_folder_that_cannot_be_listed_is_not_an_empty_store(tmp_path, monkeypatch):
    store = UnfinishedRecordingStore(tmp_path)
    store.save(
        _wav(1),
        recording_id="r1",
        recorded_at=datetime.fromisoformat("2026-01-01T00:00:00"),
    )

    def _refuse(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", _refuse)

    with pytest.raises(OSError):
        store.list_recordings()


def test_moving_out_never_replaces_a_file_of_the_same_name(tmp_path):
    store = UnfinishedRecordingStore(tmp_path / "unfinished")
    store.save(
        _wav(1),
        recording_id="r1",
        recorded_at=datetime.fromisoformat("2026-01-01T00:00:00"),
    )
    [recording] = store.list_recordings()
    kept = tmp_path / "recordings"
    kept.mkdir()
    existing = kept / recording.path.name
    existing.write_bytes(b"someone else's")

    moved = store.move_to(recording, kept)

    assert existing.read_bytes() == b"someone else's"
    assert moved.parent == kept and moved != existing and moved.is_file()
    assert store.list_recordings() == []


def test_an_unreadable_wav_is_listed_without_a_length(tmp_path):
    store = UnfinishedRecordingStore(tmp_path)
    store.save(
        b"RIFF",
        recording_id="r1",
        recorded_at=datetime.fromisoformat("2026-01-01T00:00:00"),
    )

    [recording] = store.list_recordings()

    assert recording.duration_s is None
    assert recording.size_bytes == 4


def test_a_kept_recording_names_the_language_it_was_made_in(tmp_path):
    """The directory is the whole store, so the language rides in the file
    name beside the id; the id alone still finds the recording."""
    store = UnfinishedRecordingStore(tmp_path)

    path = store.save(
        _wav(1),
        recording_id="abc123",
        recorded_at=datetime.fromisoformat("2026-10-09T14:03:12"),
        language_mode="de",
    )

    assert path is not None and path.name == "unfinished_20261009_140312_abc123.de.wav"
    [recording] = store.list_recordings()
    assert recording.recording_id == "abc123"
    assert recording.language_mode == "de"
    found = store.find("abc123")
    assert found is not None and found.path == path
    assert (
        store.save(
            _wav(1),
            recording_id="abc123",
            recorded_at=datetime.fromisoformat("2026-10-10T08:00:00"),
            language_mode="en",
        )
        is None
    )


def test_a_recording_kept_without_a_language_reads_as_unknown(tmp_path):
    """A file the previous version wrote has no language in its name; so has
    a recording whose language was never known. Both are listed as before."""
    store = UnfinishedRecordingStore(tmp_path)
    (tmp_path / "unfinished_20260101_000000_old1.wav").write_bytes(_wav(1))
    store.save(
        _wav(1),
        recording_id="new1",
        recorded_at=datetime.fromisoformat("2026-01-02T00:00:00"),
    )

    languages = {
        item.recording_id: item.language_mode for item in store.list_recordings()
    }

    assert languages == {"old1": "", "new1": ""}


def test_a_language_that_cannot_stand_in_a_file_name_is_made_safe(tmp_path):
    store = UnfinishedRecordingStore(tmp_path)

    path = store.save(
        _wav(1),
        recording_id="r1",
        recorded_at=datetime.fromisoformat("2026-01-01T00:00:00"),
        language_mode="../De t",
    )

    assert path is not None and path.parent == tmp_path
    [recording] = store.list_recordings()
    assert recording.recording_id == "r1"
    assert recording.language_mode == "de-t"
