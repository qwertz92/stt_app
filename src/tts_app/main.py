from __future__ import annotations

import signal
import sys

from PySide6 import QtCore, QtGui, QtWidgets

from .config import APP_DISPLAY_NAME, APP_LOGGER_NAME
from .controller import DictationController
from .hotkey import HotkeyManager, QtHotkeyEventFilter
from .logger import AppLogger
from .overlay_ui import OverlayUI
from .secret_store import KeyringSecretStore
from .settings_dialog import SettingsDialog
from .settings_store import SettingsStore
from .text_inserter import TextInserter


def run() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)

    app_logger = AppLogger()
    logger = app_logger.get_logger(APP_LOGGER_NAME)

    settings_store = SettingsStore()
    secret_store = KeyringSecretStore()

    overlay = OverlayUI()
    overlay.move_to_corner()
    overlay.show()

    hotkey_manager = HotkeyManager()
    text_inserter = TextInserter()

    controller = DictationController(
        settings_store=settings_store,
        hotkey_manager=hotkey_manager,
        overlay=overlay,
        text_inserter=text_inserter,
        logger=logger,
        secret_store=secret_store,
    )

    event_filter = QtHotkeyEventFilter(hotkey_manager, controller.toggle_recording)
    app.installNativeEventFilter(event_filter)

    try:
        controller.initialize()
    except Exception as exc:
        overlay.set_state("Error", str(exc))
        logger.exception("Failed to initialize controller")

    tray_icon = _create_tray_icon(
        app=app,
        controller=controller,
        settings_store=settings_store,
        secret_store=secret_store,
        app_logger=app_logger,
    )
    tray_icon.show()

    app.aboutToQuit.connect(controller.shutdown)
    signal_timer = _install_signal_handlers(app)

    app._tts_refs = {
        "controller": controller,
        "overlay": overlay,
        "event_filter": event_filter,
        "tray_icon": tray_icon,
        "signal_timer": signal_timer,
    }

    return app.exec()


def _create_tray_icon(
    app: QtWidgets.QApplication,
    controller: DictationController,
    settings_store: SettingsStore,
    secret_store: KeyringSecretStore,
    app_logger: AppLogger,
) -> QtWidgets.QSystemTrayIcon:
    style = app.style()
    icon = style.standardIcon(QtWidgets.QStyle.SP_MediaVolume)

    tray_icon = QtWidgets.QSystemTrayIcon(icon, app)
    tray_icon.setToolTip(APP_DISPLAY_NAME)

    menu = QtWidgets.QMenu()

    toggle_action = menu.addAction("Toggle Dictation")
    toggle_action.triggered.connect(controller.toggle_recording)

    settings_action = menu.addAction("Settings")

    copy_last_action = menu.addAction("Copy last transcript")
    copy_diag_action = menu.addAction("Copy diagnostics")

    menu.addSeparator()

    quit_action = menu.addAction("Quit")
    quit_action.triggered.connect(app.quit)

    def open_settings_dialog() -> None:
        dialog = SettingsDialog(
            settings_store=settings_store,
            secret_store=secret_store,
            app_logger=app_logger,
        )
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            controller.reload_settings(re_register_hotkey=True)
            controller.show_idle_status()

    def copy_diagnostics() -> None:
        QtGui.QGuiApplication.clipboard().setText(app_logger.diagnostics_text())

    def copy_last_transcript() -> None:
        if not controller.copy_last_transcript_to_clipboard():
            controller._overlay.set_state("Error", "No transcript available to copy yet.")

    settings_action.triggered.connect(open_settings_dialog)
    copy_last_action.triggered.connect(copy_last_transcript)
    copy_diag_action.triggered.connect(copy_diagnostics)

    tray_icon.setContextMenu(menu)
    return tray_icon


def _install_signal_handlers(app: QtWidgets.QApplication) -> QtCore.QTimer:
    def _handle_signal(_signum, _frame) -> None:
        app.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass

    # Keeps Python signal handling responsive while Qt event loop is running.
    timer = QtCore.QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(250)
    return timer


if __name__ == "__main__":
    raise SystemExit(run())
