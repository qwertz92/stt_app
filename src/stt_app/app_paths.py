from __future__ import annotations

import os
from pathlib import Path

from .config import APP_NAME, LEGACY_APP_NAME


def _appdata_base_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata)
    return Path.home() / "AppData" / "Roaming"


def appdata_root() -> Path:
    root = _appdata_base_root()
    path = root / APP_NAME
    if path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
        return path

    # Keep existing user data when migrating from the legacy app folder name.
    legacy_path = root / LEGACY_APP_NAME
    if legacy_path.is_dir():
        try:
            legacy_path.replace(path)
        except OSError:
            # If atomic move fails, continue using legacy location.
            legacy_path.mkdir(parents=True, exist_ok=True)
            return legacy_path

    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return appdata_root() / "settings.json"


def logs_dir() -> Path:
    path = appdata_root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def debug_audio_path() -> Path:
    return appdata_root() / "last_recording.wav"


def temp_audio_dir() -> Path:
    path = appdata_root() / "temp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def recordings_dir() -> Path:
    path = appdata_root() / "recordings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def transcript_history_path() -> Path:
    return appdata_root() / "transcript_history.json"


def insecure_keys_path() -> Path:
    return appdata_root() / "insecure_api_keys.json"
