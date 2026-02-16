"""Tests for app_paths — path resolution and directory creation."""

from __future__ import annotations

from pathlib import Path


def test_appdata_root_uses_APPDATA_env(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tts_app.app_paths import appdata_root

    result = appdata_root()
    assert result == tmp_path / "tts_app"
    assert result.is_dir()


def test_appdata_root_falls_back_to_home_when_APPDATA_unset(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    from tts_app.app_paths import appdata_root

    result = appdata_root()
    assert result == Path.home() / "AppData" / "Roaming" / "tts_app"


def test_settings_path_is_json_inside_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tts_app.app_paths import settings_path

    result = settings_path()
    assert result.name == "settings.json"
    assert str(tmp_path) in str(result)


def test_logs_dir_creates_subdirectory(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tts_app.app_paths import logs_dir

    result = logs_dir()
    assert result.name == "logs"
    assert result.is_dir()


def test_debug_audio_path_returns_wav(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tts_app.app_paths import debug_audio_path

    result = debug_audio_path()
    assert result.name == "last_recording.wav"
