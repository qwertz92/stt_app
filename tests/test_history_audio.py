from __future__ import annotations

from pathlib import Path

from stt_app.app_paths import resolve_recordings_dir
from stt_app.history_audio import resolve_history_audio_path
from stt_app.settings_store import AppSettings, apply_engine_model_selection
from stt_app.transcript_history import TranscriptHistoryEntry


class _FakeLastRecordingState:
    def __init__(self, recording_id: str, audio_path: str) -> None:
        self.recording_id = recording_id
        self.audio_path = audio_path


class _FakeLastRecordingStore:
    def __init__(self, state=None, raises: bool = False) -> None:
        self._state = state
        self._raises = raises

    def load(self):
        if self._raises:
            raise OSError("unavailable")
        return self._state


def _entry(**kwargs) -> TranscriptHistoryEntry:
    return TranscriptHistoryEntry.new(
        text=kwargs.pop("text", "hello"),
        engine=kwargs.pop("engine", "local"),
        model=kwargs.pop("model", "small"),
        mode=kwargs.pop("mode", "batch"),
        **kwargs,
    )


def test_stored_audio_path_is_used_when_the_file_exists(tmp_path):
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"RIFF")
    entry = _entry(source_audio_path=str(audio))

    assert resolve_history_audio_path(entry, None) == audio


def test_missing_stored_audio_path_falls_back_to_last_recording(tmp_path):
    managed = tmp_path / "last_recording.wav"
    managed.write_bytes(b"RIFF")
    entry = _entry(
        source_audio_path=str(tmp_path / "deleted.wav"),
        source_recording_id="rec-1",
    )
    store = _FakeLastRecordingStore(
        _FakeLastRecordingState("rec-1", str(managed))
    )

    assert resolve_history_audio_path(entry, store) == managed


def test_last_recording_of_another_entry_is_not_offered(tmp_path):
    managed = tmp_path / "last_recording.wav"
    managed.write_bytes(b"RIFF")
    entry = _entry(source_recording_id="rec-1")
    store = _FakeLastRecordingStore(
        _FakeLastRecordingState("rec-2", str(managed))
    )

    assert resolve_history_audio_path(entry, store) is None


def test_unreadable_last_recording_store_is_not_fatal(tmp_path):
    entry = _entry(source_recording_id="rec-1")

    assert resolve_history_audio_path(entry, _FakeLastRecordingStore(raises=True)) is None


def test_entry_without_any_audio_reference_resolves_to_none():
    assert resolve_history_audio_path(_entry(), None) is None


def test_resolve_recordings_dir_prefers_the_configured_directory(tmp_path):
    configured = tmp_path / "custom recordings"

    assert resolve_recordings_dir(str(configured)) == Path(configured)


def test_resolve_recordings_dir_falls_back_to_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    resolved = resolve_recordings_dir("   ")

    assert resolved.name == "recordings"
    assert resolved.is_dir()


def test_apply_engine_model_selection_sets_the_local_model():
    settings = AppSettings(engine="local", model_size="small")

    updated = apply_engine_model_selection(settings, "local", "large-v3-turbo")

    assert updated.model_size == "large-v3-turbo"


def test_apply_engine_model_selection_ignores_an_unknown_local_model():
    settings = AppSettings(engine="local", model_size="small")

    updated = apply_engine_model_selection(settings, "local", "not-a-model")

    assert updated.model_size == "small"


def test_apply_engine_model_selection_writes_the_provider_field():
    settings = AppSettings(engine="groq")

    updated = apply_engine_model_selection(settings, "groq", "whisper-large-v3")

    assert updated.groq_model == "whisper-large-v3"


def test_apply_engine_model_selection_keeps_settings_without_a_model():
    settings = AppSettings(engine="groq", groq_model="whisper-large-v3")

    updated = apply_engine_model_selection(settings, "groq", "  ")

    assert updated.groq_model == "whisper-large-v3"


def test_apply_engine_model_selection_ignores_an_unknown_engine():
    settings = AppSettings(engine="local", model_size="small")

    updated = apply_engine_model_selection(settings, "nope", "whatever")

    assert updated == settings
