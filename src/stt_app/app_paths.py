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


def existing_appdata_root() -> Path | None:
    """The data folder as it is on disk right now, creating and moving nothing.

    `appdata_root` is a *setup* call, not a lookup: it creates the folder, and
    when only the legacy one exists it renames the user's entire data
    directory onto the current name. Both are right for the app, which is
    about to write there anyway, and wrong for anything that only wants to
    read -- a diagnostic script asked for the settings path and thereby moved
    a legacy install's settings, history and recordings, which is precisely
    the class of side effect it was written to avoid.

    Returns `None` when neither folder exists, because "there is nothing to
    read" is a different answer from "here is where it would go".
    """
    root = _appdata_base_root()
    path = root / APP_NAME
    if path.is_dir():
        return path
    legacy_path = root / LEGACY_APP_NAME
    if legacy_path.is_dir():
        return legacy_path
    return None


def existing_settings_path() -> Path | None:
    """The saved settings file, or `None` if this install has never written one."""
    root = existing_appdata_root()
    if root is None:
        return None
    path = root / "settings.json"
    return path if path.is_file() else None


def settings_path() -> Path:
    return appdata_root() / "settings.json"


def logs_dir() -> Path:
    path = appdata_root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def debug_audio_path() -> Path:
    return appdata_root() / "last_recording.wav"


def last_recording_state_path() -> Path:
    return appdata_root() / "last_recording.json"


def local_model_inventory_path() -> Path:
    return appdata_root() / "local_model_inventory.json"


def temp_audio_dir() -> Path:
    path = appdata_root() / "temp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def recordings_dir() -> Path:
    path = appdata_root() / "recordings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_recordings_dir(configured: str = "") -> Path:
    """Where recordings are archived: the configured directory, else default."""
    value = str(configured or "").strip()
    if value:
        return Path(value)
    return recordings_dir()


def transcript_history_path() -> Path:
    return appdata_root() / "transcript_history.json"


def benchmark_history_path() -> Path:
    return appdata_root() / "benchmark_history.json"


def provider_connection_tests_path() -> Path:
    return appdata_root() / "provider_connection_tests.json"


def insecure_keys_path() -> Path:
    return appdata_root() / "insecure_api_keys.json"
