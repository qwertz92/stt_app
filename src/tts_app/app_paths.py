from __future__ import annotations

import os
from pathlib import Path

from .config import APP_NAME


def appdata_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        root = Path(appdata)
    else:
        root = Path.home() / "AppData" / "Roaming"

    path = root / APP_NAME
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


def recordings_dir() -> Path:
    path = appdata_root() / "recordings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def transcript_history_path() -> Path:
    return appdata_root() / "transcript_history.json"
