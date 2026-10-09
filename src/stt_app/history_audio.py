"""Shared linking of transcript history entries to their retained audio.

The Settings History tab and the overlay's "Recent Transcriptions" dialog both
resolve an entry's audio file, reveal it in the system file manager, and open
the recordings directory. The logic lives here exactly once so the two views
cannot drift apart. The startup notice of unfinished recordings reveals its
files through the same helpers.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from PySide6 import QtCore, QtGui

_LOGGER = logging.getLogger(__name__)


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


def reveal_paths_in_file_manager(paths: Sequence[str | Path]) -> bool:
    """Open the file manager with every one of ``paths`` selected.

    One path goes through `reveal_path_in_file_manager`. Explorer's command
    line selects a single file, so several are selected through the shell's
    `SHOpenFolderAndSelectItems`. That call can wait on Explorer, so it runs
    on a short-lived thread, and when it fails the folder is opened instead.
    One Explorer window shows one folder: paths outside the first path's
    folder are left out.
    """
    targets = [_resolved(path) for path in paths]
    if not targets:
        return False
    if len(targets) == 1:
        return reveal_path_in_file_manager(targets[0])
    folder = targets[0].parent
    in_folder = [target for target in targets if target.parent == folder]
    try:
        threading.Thread(
            target=_select_in_folder_or_open,
            args=(folder, in_folder),
            name="stt_app_reveal_files",
            daemon=True,
        ).start()
    except RuntimeError:
        return open_directory(folder)
    return True


def _select_in_folder_or_open(folder: Path, items: list[Path]) -> None:
    try:
        if select_items_in_folder(folder, items):
            return
    except Exception:
        _LOGGER.exception("Selecting files in Explorer failed")
    try:
        os.startfile(str(folder))
    except OSError:
        _LOGGER.exception("Opening %s failed", folder)


def select_items_in_folder(folder: Path, items: list[Path]) -> bool:
    """`SHOpenFolderAndSelectItems` for ``items`` in ``folder``; True on S_OK.

    Its own `WinDLL` handles, so the declarations below change nothing for
    other callers (docs/agents/windows-platform.md).
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.ILFree.restype = None
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
    ]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    coinit_apartmentthreaded = 0x2
    initialized = ole32.CoInitializeEx(None, coinit_apartmentthreaded) in (0, 1)
    folder_pidl = shell32.ILCreateFromPathW(str(folder))
    item_pidls = [shell32.ILCreateFromPathW(str(item)) for item in items]
    try:
        selected = [pidl for pidl in item_pidls if pidl]
        if not folder_pidl or not selected:
            return False
        array = (ctypes.c_void_p * len(selected))(*selected)
        return (
            shell32.SHOpenFolderAndSelectItems(folder_pidl, len(selected), array, 0)
            == 0
        )
    finally:
        for pidl in (folder_pidl, *item_pidls):
            if pidl:
                shell32.ILFree(pidl)
        if initialized:
            ole32.CoUninitialize()


def open_directory(path: str | Path) -> bool:
    """Open ``path`` in the system file manager."""
    return bool(
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(_resolved(path))))
    )


def _resolved(path: str | Path) -> Path:
    target = Path(str(path))
    try:
        return target.resolve()
    except OSError:
        return target
