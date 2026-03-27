"""Tests for app_paths — path resolution and directory creation."""

from __future__ import annotations

from pathlib import Path


def test_appdata_root_uses_APPDATA_env(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from stt_app.app_paths import appdata_root

    result = appdata_root()
    assert result == tmp_path / "stt_app"
    assert result.is_dir()


def test_appdata_root_falls_back_to_home_when_APPDATA_unset(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    from stt_app.app_paths import appdata_root

    result = appdata_root()
    assert result == Path.home() / "AppData" / "Roaming" / "stt_app"


def test_settings_path_is_json_inside_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from stt_app.app_paths import settings_path

    result = settings_path()
    assert result.name == "settings.json"
    assert str(tmp_path) in str(result)


def test_logs_dir_creates_subdirectory(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from stt_app.app_paths import logs_dir

    result = logs_dir()
    assert result.name == "logs"
    assert result.is_dir()


def test_debug_audio_path_returns_wav(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from stt_app.app_paths import debug_audio_path

    result = debug_audio_path()
    assert result.name == "last_recording.wav"


def test_last_recording_state_path_returns_json(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from stt_app.app_paths import last_recording_state_path

    result = last_recording_state_path()
    assert result.name == "last_recording.json"


def test_temp_audio_dir_is_created(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from stt_app.app_paths import temp_audio_dir

    result = temp_audio_dir()
    assert result.name == "temp"
    assert result.is_dir()


def test_recordings_dir_is_created(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from stt_app.app_paths import recordings_dir

    result = recordings_dir()
    assert result.name == "recordings"
    assert result.is_dir()


def test_transcript_history_path_points_to_json(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from stt_app.app_paths import transcript_history_path

    result = transcript_history_path()
    assert result.name == "transcript_history.json"
    assert str(tmp_path) in str(result)


def test_insecure_keys_path_points_to_json(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from stt_app.app_paths import insecure_keys_path

    result = insecure_keys_path()
    assert result.name == "insecure_api_keys.json"
    assert str(tmp_path) in str(result)


def test_appdata_root_migrates_legacy_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    legacy = tmp_path / "tts_app"
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text("{}", encoding="utf-8")

    from stt_app.app_paths import appdata_root

    result = appdata_root()
    assert result == tmp_path / "stt_app"
    assert (result / "settings.json").is_file()
    assert legacy.exists() is False
