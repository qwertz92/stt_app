"""Shared linking of transcript history entries to their retained audio.

The Settings History tab and the overlay's "Recent Transcriptions" dialog both
resolve an entry's audio file, reveal it in the system file manager, and open
the recordings directory. The logic lives here exactly once so the two views
cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui


def resolve_history_audio_path(
    entry: object,
    last_recording_store: object | None = None,
) -> Path | None:
    """Return the retained audio file for ``entry`` when it still exists.

    Entries recorded while "save all recordings" was enabled carry the archive
    path directly. Otherwise the managed last recording is the only remaining
    source, and only while it still describes this exact entry.
    """
    stored_path = str(getattr(entry, "source_audio_path", "") or "").strip()
    if stored_path:
        path = Path(stored_path)
        if path.is_file():
            return path

    source_id = str(getattr(entry, "source_recording_id", "") or "").strip()
    if not source_id or last_recording_store is None:
        return None
    try:
        state = last_recording_store.load()
    except Exception:
        return None
    if state is None or str(getattr(state, "recording_id", "")) != source_id:
        return None
    path = Path(str(getattr(state, "audio_path", "") or ""))
    return path if path.is_file() else None


def reveal_path_in_file_manager(path: str | Path) -> bool:
    """Open the system file manager with ``path`` selected.

    Falls back to opening the containing directory when the explicit
    selection call is unavailable or fails.
    """
    target = _resolved(path)
    native_path = QtCore.QDir.toNativeSeparators(str(target))
    started = QtCore.QProcess.startDetached(
        "explorer.exe",
        [f"/select,{native_path}"],
    )
    if isinstance(started, tuple):
        started = started[0]
    if started:
        return True
    return open_directory(target.parent)


def open_directory(path: str | Path) -> bool:
    """Open ``path`` in the system file manager."""
    return bool(
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(_resolved(path)))
        )
    )


def _resolved(path: str | Path) -> Path:
    target = Path(str(path))
    try:
        return target.resolve()
    except OSError:
        return target
