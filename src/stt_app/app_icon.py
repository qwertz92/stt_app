"""Shared application icon loading for top-level windows and dialogs."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtGui, QtWidgets


def app_icon_path() -> Path:
    bundled_root = getattr(sys, "_MEIPASS", "")
    if bundled_root:
        bundled = Path(str(bundled_root)) / "stt_app" / "assets" / "app_icon.ico"
        if bundled.is_file():
            return bundled
    return Path(__file__).resolve().parent / "assets" / "app_icon.ico"


def load_app_icon() -> QtGui.QIcon:
    """Load the bundled app icon, falling back to a Qt standard icon."""
    path = app_icon_path()
    if path.is_file():
        icon = QtGui.QIcon(str(path))
        if not icon.isNull():
            return icon
    app = QtWidgets.QApplication.instance()
    if isinstance(app, QtWidgets.QApplication):
        return app.style().standardIcon(QtWidgets.QStyle.SP_MediaVolume)
    return QtGui.QIcon()
