from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from .update_checker import UpdateCheckResult


def show_update_available_dialog(
    result: UpdateCheckResult,
    *,
    parent: QtWidgets.QWidget | None = None,
) -> None:
    message = (
        f"Version {result.latest_tag or result.latest_version} is available.\n\n"
        f"Current version: {result.current_version or 'unknown'}\n"
        f"Latest version: {result.latest_version or result.latest_tag}"
    )
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle("Update available")
    box.setText(message)
    box.setIcon(QtWidgets.QMessageBox.Information)
    open_button = box.addButton("Open release", QtWidgets.QMessageBox.AcceptRole)
    box.addButton("Later", QtWidgets.QMessageBox.RejectRole)
    box.exec()
    if box.clickedButton() is open_button:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(result.release_url))
