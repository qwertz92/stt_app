from __future__ import annotations

import json
import os

from stt_app.last_recording_store import LastRecordingStore


def test_save_and_complete_without_keep_clears_files(tmp_path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )

    store.save_recording(b"RIFF", keep_after_success=False)
    store.mark_transcribing(engine="openai", model="whisper-1", mode="batch")
    store.mark_completed()

    assert store.audio_path.exists() is False
    assert store.state_path.exists() is False


def test_save_and_complete_with_keep_preserves_audio(tmp_path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )

    store.save_recording(b"RIFF", keep_after_success=True)
    store.mark_completed()

    state = store.load()
    assert state is not None
    assert state.recording_id
    assert state.status == "completed"
    assert store.selectable_path() == store.audio_path
    assert store.has_recoverable_recording() is False


def test_conditional_transition_does_not_modify_a_newer_recording(tmp_path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    first = store.save_recording(b"RIFF-first", keep_after_success=False)
    snapshot = store.snapshot_managed_recording(store.audio_path)
    assert snapshot is not None
    assert snapshot.recording_id == first.recording_id
    assert snapshot.audio_bytes == b"RIFF-first"

    second = store.save_recording(b"RIFF-second", keep_after_success=False)

    assert (
        store.mark_failed(
            "stale failure",
            expected_recording_id=snapshot.recording_id,
        )
        is False
    )
    assert (
        store.mark_transcribing(
            engine="openai",
            model="gpt-4o-transcribe",
            mode="import",
            expected_recording_id=snapshot.recording_id,
        )
        is False
    )
    assert store.mark_completed(expected_recording_id=snapshot.recording_id) is False
    assert (
        store.mark_canceled(
            "stale cancel",
            expected_recording_id=snapshot.recording_id,
        )
        is False
    )
    current = store.load()
    assert current is not None
    assert current.recording_id == second.recording_id
    assert current.status == "captured"
    assert current.error == ""
    assert store.audio_path.read_bytes() == b"RIFF-second"

    # The current id is the one every mark accepts.
    assert (
        store.mark_canceled(
            "canceled by its own job",
            expected_recording_id=second.recording_id,
        )
        is True
    )
    current = store.load()
    assert current is not None
    assert current.status == "canceled"
    assert current.error == "canceled by its own job"


def test_clear_failure_preserves_state_for_a_later_retry(tmp_path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    state = store.save_recording(b"RIFF-audio", keep_after_success=False)
    store.audio_path.unlink()
    store.audio_path.mkdir()

    assert store.clear(expected_recording_id=state.recording_id) is False
    assert store.state_path.is_file()
    loaded = store.load()
    assert loaded is not None
    assert loaded.recording_id == state.recording_id


def test_orphaned_audio_without_state_is_treated_as_recoverable(tmp_path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    store.audio_path.write_bytes(b"RIFF")

    state = store.load()

    assert state is not None
    assert state.status == "captured"
    assert store.has_recoverable_recording() is True


def test_selectable_path_uses_newest_archived_recording(tmp_path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    store.save_recording(b"RIFF-old", keep_after_success=True)
    store.mark_completed()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archived = archive_dir / "recording_20260428_101500_000000.wav"
    archived.write_bytes(b"RIFF-new")
    os.utime(store.audio_path, (100, 100))
    os.utime(archived, (200, 200))

    assert store.selectable_path(archive_dir) == archived


def test_selectable_path_keeps_managed_recording_when_it_is_newer(tmp_path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    store.save_recording(b"RIFF-new", keep_after_success=True)
    store.mark_completed()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archived = archive_dir / "recording_20260428_101500_000000.wav"
    archived.write_bytes(b"RIFF-old")
    os.utime(store.audio_path, (300, 300))
    os.utime(archived, (200, 200))

    assert store.selectable_path(archive_dir) == store.audio_path


def test_selectable_path_keeps_recoverable_managed_recording(tmp_path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    store.save_recording(b"RIFF-failed", keep_after_success=False)
    store.mark_failed("provider failed")
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archived = archive_dir / "recording_20260428_101500_000000.wav"
    archived.write_bytes(b"RIFF-newer-mtime")
    os.utime(store.audio_path, (100, 100))
    os.utime(archived, (200, 200))

    assert store.selectable_path(archive_dir) == store.audio_path


def test_the_language_a_recording_was_made_in_survives_a_restart(tmp_path):
    """Retry pins the recording's language, also after the app was restarted:
    a new store instance on the same files answers it, through every mark."""
    paths = {
        "audio_path": tmp_path / "last_recording.wav",
        "state_path": tmp_path / "last_recording.json",
    }
    store = LastRecordingStore(**paths)
    state = store.save_recording(b"RIFF", keep_after_success=False, language_mode="de")
    assert state.language_mode == "de"
    store.mark_transcribing(engine="openai", model="gpt-transcribe", mode="batch")
    store.mark_failed("provider down")

    restarted = LastRecordingStore(**paths).load()

    assert restarted is not None
    assert restarted.language_mode == "de"
    assert restarted.status == "failed"


def test_a_state_file_from_before_the_language_was_stored_still_loads(tmp_path):
    """A file the previous version wrote has no `language_mode`: it reads as
    unknown (""), which Retry answers with the language selected now."""
    paths = {
        "audio_path": tmp_path / "last_recording.wav",
        "state_path": tmp_path / "last_recording.json",
    }
    paths["audio_path"].write_bytes(b"RIFF")
    paths["state_path"].write_text(
        json.dumps(
            {
                "schema_version": 1,
                "audio_path": str(paths["audio_path"]),
                "recording_id": "rec-old",
                "created_at": "2026-10-01T10:00:00+00:00",
                "status": "failed",
                "keep_after_success": False,
                "engine": "openai",
                "model": "gpt-transcribe",
                "mode": "batch",
                "error": "provider down",
                "transcription_started_at": "",
                "completed_at": "",
            }
        ),
        encoding="utf-8",
    )
    store = LastRecordingStore(**paths)

    state = store.load()

    assert state is not None
    assert state.recording_id == "rec-old"
    assert state.language_mode == ""
    # The next write keeps working and does not invent a language.
    assert store.mark_failed("again") is True
    reloaded = store.load()
    assert reloaded is not None and reloaded.language_mode == ""
