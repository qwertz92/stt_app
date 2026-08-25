from __future__ import annotations

from pathlib import Path

from PySide6 import QtWidgets

from stt_app import update_ui
from stt_app.update_checker import UpdateCheckResult
from stt_app.update_ui import UPDATE_DIALOG_STYLESHEET, UpdateDownloadDialog


def test_update_dialog_styles_keep_hover_text_contrasting():
    assert "QPushButton:hover:enabled" in UPDATE_DIALOG_STYLESHEET
    assert "color: #0b315c" in UPDATE_DIALOG_STYLESHEET
    assert 'QPushButton[primary="true"]:hover:enabled' in UPDATE_DIALOG_STYLESHEET
    assert "color: #ffffff" in UPDATE_DIALOG_STYLESHEET


def test_unsigned_download_cannot_start_installer(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    launches = []
    result = UpdateCheckResult(
        current_version="0.9.0",
        latest_version="1.0.0",
        latest_tag="v1.0.0",
        update_available=True,
    )
    dialog = UpdateDownloadDialog(
        result,
        downloader=lambda *_args, **_kwargs: tmp_path / "update.exe",
        signature_verifier=lambda _path: (False, "NotSigned"),
        launcher=lambda path: launches.append(path) or True,
    )
    dialog._on_completed(tmp_path / "update.exe", False, "NotSigned")

    assert dialog._primary_button.text() == "Open release page"
    assert dialog._signature_valid is False
    assert launches == []
    assert QtWidgets.QApplication.instance() is app


def test_verified_download_exposes_install_action(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    quits = []
    monkeypatch.setattr(QtWidgets.QApplication, "quit", lambda: quits.append(True))
    installer = tmp_path / "update.exe"
    launches = []
    result = UpdateCheckResult(
        current_version="0.9.0",
        latest_version="1.0.0",
        latest_tag="v1.0.0",
        update_available=True,
    )
    dialog = UpdateDownloadDialog(
        result,
        downloader=lambda *_args, **_kwargs: installer,
        signature_verifier=lambda _path: (True, "CN=Expected Publisher"),
        launcher=lambda path: launches.append(path) or True,
    )
    dialog._on_completed(installer, True, "CN=Expected Publisher")

    assert dialog._primary_button.text() == "Install update"
    assert dialog._primary_button.isEnabled() is True
    dialog._run_primary_action()
    assert launches == [installer]
    assert quits == [True]
    assert QtWidgets.QApplication.instance() is app


def test_detached_launcher_uses_boolean_from_pyside_tuple(monkeypatch):
    monkeypatch.setattr(
        "stt_app.update_ui.QtCore.QProcess.startDetached",
        lambda *_args: (False, -1),
    )

    assert UpdateDownloadDialog._launch_installer(Path("missing.exe")) is False


def test_the_update_status_dialog_has_a_reachable_button(monkeypatch):
    """One button, and Qt renders it 42 px wide in the bottom-right corner.

    Reported from use: the acknowledge button on "no update available" is a
    small target stranded away from the message it belongs to.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    seen = []

    original_init = QtWidgets.QMessageBox.__init__

    def _spy(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        seen.append(self)

    monkeypatch.setattr(QtWidgets.QMessageBox, "__init__", _spy)
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: None)

    update_ui.show_update_status_dialog(
        title="Update", text="No update available."
    )

    box = seen[-1]
    button = box.button(QtWidgets.QMessageBox.Ok)
    assert button is not None
    assert button.minimumWidth() >= update_ui.UPDATE_STATUS_BUTTON_MIN_WIDTH
    boxes = box.findChildren(QtWidgets.QDialogButtonBox)
    assert boxes and boxes[0].centerButtons(), "the button row is still right-aligned"
    _ = app


def test_the_update_status_dialog_text_is_readable(monkeypatch):
    """It was reported as invisible: white text on a white background.

    The dialog styling now sets an explicit dark foreground, so pin the
    contrast rather than trusting the platform palette.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    seen = []
    original_init = QtWidgets.QMessageBox.__init__

    def _spy(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        seen.append(self)

    monkeypatch.setattr(QtWidgets.QMessageBox, "__init__", _spy)
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: None)

    update_ui.show_update_status_dialog(title="Update", text="No update available.")
    box = seen[-1]
    box.show()
    app.processEvents()
    try:
        labels = [label for label in box.findChildren(QtWidgets.QLabel) if label.text()]
        assert labels, "the dialog has no visible text at all"
        background = box.palette().window().color()
        for label in labels:
            foreground = label.palette().windowText().color()
            difference = sum(
                abs(getattr(foreground, channel)() - getattr(background, channel)())
                for channel in ("red", "green", "blue")
            )
            assert difference > 150, (
                f"{label.text()[:30]!r} is {foreground.name()} on "
                f"{background.name()} -- effectively invisible"
            )
    finally:
        box.close()
    _ = app
