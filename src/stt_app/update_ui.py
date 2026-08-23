from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .app_paths import appdata_root
from .dialog_style import DIALOG_STYLESHEET, apply_dialog_style, make_label_selectable
from .update_checker import UpdateCheckResult
from .update_installer import (
    UpdateDownloadCancelled,
    download_verified_installer,
    verify_windows_publisher_signature,
)

# Kept under its original name so existing callers and tests keep working; the
# colours now live in ``dialog_style`` because every dialog needs them.
UPDATE_DIALOG_STYLESHEET = DIALOG_STYLESHEET

# Width for the single acknowledge button on the update status dialog.
UPDATE_STATUS_BUTTON_MIN_WIDTH = 120


def _style_dialog(dialog: QtWidgets.QWidget) -> None:
    apply_dialog_style(dialog)


def _centre_dialog_buttons(box: QtWidgets.QMessageBox) -> None:
    """Centre a message box's button row.

    `QMessageBox` puts its buttons in a `QDialogButtonBox` that is stretched
    to the right. With a single acknowledge button that leaves it stranded in
    the corner, far from where the eye already is.
    """
    layout = box.layout()
    if not isinstance(layout, QtWidgets.QGridLayout):
        return
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget() if item is not None else None
        if isinstance(widget, QtWidgets.QDialogButtonBox):
            widget.setCenterButtons(True)
            return

def show_update_status_dialog(
    *,
    title: str,
    text: str,
    icon: QtWidgets.QMessageBox.Icon = QtWidgets.QMessageBox.Information,
    parent: QtWidgets.QWidget | None = None,
) -> None:
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(icon)
    box.setStandardButtons(QtWidgets.QMessageBox.Ok)
    _style_dialog(box)
    # The only button in the dialog, and Qt renders it 42 px wide in the
    # bottom-right corner -- a small target for the one thing there is to do
    # here. Give it a real width and centre it under the message.
    ok_button = box.button(QtWidgets.QMessageBox.Ok)
    if ok_button is not None:
        ok_button.setMinimumWidth(UPDATE_STATUS_BUTTON_MIN_WIDTH)
        _centre_dialog_buttons(box)
    box.exec()


class _DownloadSignals(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    completed = QtCore.Signal(object, bool, str)
    failed = QtCore.Signal(str, bool)


class UpdateDownloadDialog(QtWidgets.QDialog):
    def __init__(
        self,
        result: UpdateCheckResult,
        *,
        parent: QtWidgets.QWidget | None = None,
        downloader: Callable = download_verified_installer,
        signature_verifier: Callable = verify_windows_publisher_signature,
        launcher: Callable[[Path], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._result = result
        self._downloader = downloader
        self._signature_verifier = signature_verifier
        self._launcher = launcher or self._launch_installer
        self._cancel_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._installer_path: Path | None = None
        self._signature_valid = False

        self.setWindowTitle(f"Update {result.latest_tag or result.latest_version}")
        self.setMinimumWidth(520)
        self.setModal(True)
        _style_dialog(self)

        self._status_label = QtWidgets.QLabel(
            "Downloading the installer from the verified GitHub release..."
        )
        self._status_label.setWordWrap(True)
        make_label_selectable(self._status_label)
        self._progress_bar = QtWidgets.QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._details_label = QtWidgets.QLabel(
            "The SHA-256 checksum and Windows publisher signature will be "
            "verified before installation."
        )
        self._details_label.setWordWrap(True)
        # This is where a failed update puts the provider error verbatim, which
        # is exactly the text worth pasting into a bug report.
        make_label_selectable(self._details_label)

        self._primary_button = QtWidgets.QPushButton("Downloading...")
        self._primary_button.setProperty("primary", True)
        self._primary_button.setEnabled(False)
        self._primary_button.clicked.connect(self._run_primary_action)
        self._folder_button = QtWidgets.QPushButton("Open download folder")
        self._folder_button.setVisible(False)
        self._folder_button.clicked.connect(self._open_download_folder)
        self._cancel_button = QtWidgets.QPushButton("Cancel")
        self._cancel_button.clicked.connect(self.reject)

        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(self._folder_button)
        actions.addStretch(1)
        actions.addWidget(self._primary_button)
        actions.addWidget(self._cancel_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(self._status_label)
        layout.addWidget(self._progress_bar)
        layout.addWidget(self._details_label)
        layout.addLayout(actions)

        self._signals = _DownloadSignals(self)
        self._signals.progress.connect(self._on_progress)
        self._signals.completed.connect(self._on_completed)
        self._signals.failed.connect(self._on_failed)
        self._download_scheduled = False

    def showEvent(self, event) -> None:
        """Start downloading when the dialog is actually shown.

        Scheduling this from the constructor meant merely *constructing* the
        dialog began a multi-megabyte download, and the deferred timer fired
        against whatever event loop ran next -- so a dialog that was never shown
        still spawned a worker thread doing real filesystem and network work.
        """
        super().showEvent(event)
        if self._download_scheduled:
            return
        self._download_scheduled = True
        QtCore.QTimer.singleShot(0, self._start_download)

    def reject(self) -> None:
        self._cancel_event.set()
        super().reject()

    def _start_download(self) -> None:
        if self._worker is not None:
            return

        def run() -> None:
            try:
                destination = (
                    appdata_root()
                    / "updates"
                    / (self._result.latest_tag or self._result.latest_version)
                )
                path = self._downloader(
                    self._result,
                    destination,
                    progress=self._signals.progress.emit,
                    cancelled=self._cancel_event.is_set,
                )
                signature_valid, signature_detail = self._signature_verifier(path)
                self._signals.completed.emit(
                    path,
                    bool(signature_valid),
                    str(signature_detail),
                )
            except UpdateDownloadCancelled:
                self._signals.failed.emit("Update download cancelled.", True)
            except Exception as exc:
                self._signals.failed.emit(str(exc), False)

        self._worker = threading.Thread(
            target=run,
            name="stt_app_update_download",
            daemon=True,
        )
        self._worker.start()

    @QtCore.Slot(int, int)
    def _on_progress(self, downloaded: int, total: int) -> None:
        percent = 0 if total <= 0 else round(downloaded * 100 / total)
        self._progress_bar.setValue(max(0, min(100, percent)))
        self._status_label.setText(
            f"Downloaded {downloaded / (1024 * 1024):.1f} of "
            f"{total / (1024 * 1024):.1f} MB"
        )

    @QtCore.Slot(object, bool, str)
    def _on_completed(
        self,
        installer_path: object,
        signature_valid: bool,
        signature_detail: str,
    ) -> None:
        self._installer_path = Path(installer_path)
        self._signature_valid = bool(signature_valid)
        self._progress_bar.setValue(100)
        self._folder_button.setVisible(True)
        self._cancel_button.setText("Close")
        if self._signature_valid:
            self._status_label.setText("The update is ready to install.")
            self._details_label.setText(
                "The download checksum matches and Windows verified the publisher "
                f"signature: {signature_detail}"
            )
            self._primary_button.setText("Install update")
            self._primary_button.setEnabled(True)
            return

        self._status_label.setText(
            "The update was downloaded but cannot be installed automatically."
        )
        self._details_label.setText(
            "The download checksum matches, but the Windows publisher signature "
            f"could not be verified. {signature_detail} Automatic installation is "
            "disabled to protect this computer."
        )
        self._primary_button.setText("Open release page")
        self._primary_button.setEnabled(True)

    @QtCore.Slot(str, bool)
    def _on_failed(self, message: str, cancelled: bool) -> None:
        if cancelled:
            return
        self._status_label.setText("The update could not be downloaded.")
        self._details_label.setText(message)
        self._progress_bar.setValue(0)
        self._primary_button.setText("Open release page")
        self._primary_button.setEnabled(True)
        self._cancel_button.setText("Close")

    def _run_primary_action(self) -> None:
        if not self._signature_valid or self._installer_path is None:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(self._result.release_url))
            return
        if self._launcher(self._installer_path):
            QtWidgets.QApplication.quit()
            return
        show_update_status_dialog(
            parent=self,
            title="Unable to start installer",
            text=(
                "The verified installer could not be started. You can open the "
                "download folder and run it manually."
            ),
            icon=QtWidgets.QMessageBox.Critical,
        )

    def _open_download_folder(self) -> None:
        if self._installer_path is not None:
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(self._installer_path.parent))
            )

    @staticmethod
    def _launch_installer(path: Path) -> bool:
        started = QtCore.QProcess.startDetached(
            str(path),
            ["/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
        )
        if isinstance(started, tuple):
            return bool(started[0])
        return bool(started)


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
    if result.supports_in_app_update:
        primary_button = box.addButton(
            "Download update",
            QtWidgets.QMessageBox.AcceptRole,
        )
    else:
        primary_button = box.addButton(
            "Open release",
            QtWidgets.QMessageBox.AcceptRole,
        )
    primary_button.setProperty("primary", True)
    box.addButton("Later", QtWidgets.QMessageBox.RejectRole)
    _style_dialog(box)
    box.exec()
    if box.clickedButton() is not primary_button:
        return
    if result.supports_in_app_update:
        UpdateDownloadDialog(result, parent=parent).exec()
    else:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(result.release_url))
