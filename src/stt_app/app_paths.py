from __future__ import annotations

import os
import re
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


def unfinished_recordings_dir() -> Path:
    """Recordings an earlier session did not transcribe (not created here)."""
    return appdata_root() / "unfinished_recordings"


def resolve_recordings_dir(configured: str = "") -> Path:
    """Where recordings are archived: the configured directory, else default."""
    value = str(configured or "").strip()
    if value:
        return Path(value)
    return recordings_dir()


# The name of a file "Archive every recording" wrote, `recording_<date>_<time>_
# <six digits>.wav` (a kept unfinished recording that joins the archive takes the
# same shape). The one definition of "an archived recording": "Use last
# recording" considers only these, because the recordings folder also holds
# copies of audio that History entries link and untranscribed recordings the
# startup notice's "Keep files and close" moved there
# (`unfinished_<date>_<time>_<id>.wav`).
ARCHIVED_RECORDING_NAME_RE = re.compile(
    r"^recording_[0-9]{8}_[0-9]{6}_[0-9]{6}\.wav$",
    re.IGNORECASE,
)
# A kept copy: the audio of a transcript that History links, moved to the
# recordings folder (a gap or language-fallback transcript, a transcribed
# unfinished recording with only "Keep last recording" on), named
# `kept_<date>_<time>_<id>[.<language>][_<n>].wav` since 2026-11-24. The
# retention count never deletes it (part of a gap's recording returned no
# text), nor an `unfinished_*` file, which may be a recording nobody
# transcribed. A copy whose History write was refused goes back to the
# unfinished store under its `unfinished_` name
# (`UnfinishedRecordingStore.take_back`).
KEPT_RECORDING_PREFIX = "kept_"
KEPT_RECORDING_NAME_RE = re.compile(
    r"^kept_(?P<stamp>[0-9]{8}_[0-9]{6})_(?P<rest>[A-Za-z0-9-]+"
    r"(?:\.[A-Za-z0-9-]+)?)(?:_[0-9]+)?\.wav$",
    re.IGNORECASE,
)


def transcript_history_path() -> Path:
    return appdata_root() / "transcript_history.json"


def benchmark_history_path() -> Path:
    return appdata_root() / "benchmark_history.json"


def provider_connection_tests_path() -> Path:
    return appdata_root() / "provider_connection_tests.json"


def insecure_keys_path() -> Path:
    return appdata_root() / "insecure_api_keys.json"


def node_packages_back_off_path() -> Path:
    """The last failed JavaScript package update (`transcriber._node_packages`)."""
    return appdata_root() / "node_packages_update.json"
