from __future__ import annotations

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
