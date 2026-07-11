from __future__ import annotations

from pathlib import Path

from PySide6 import QtWidgets

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
