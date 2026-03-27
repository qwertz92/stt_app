from __future__ import annotations

import signal
import sys

from PySide6 import QtCore, QtGui, QtWidgets

from .config import APP_DISPLAY_NAME, APP_LOGGER_NAME, DEFAULT_CANCEL_HOTKEY_ID
from .history_dialog import HistoryDialog
from .controller import DictationController
from .hotkey import HotkeyManager, QtHotkeyEventFilter
from .last_recording_store import LastRecordingStore
from .logger import AppLogger
from .overlay_ui import OverlayUI
from .secret_store import KeyringSecretStore
from .settings_dialog import SettingsDialog
from .settings_store import SettingsStore
from .ssl_utils import inject_system_trust_store, sync_ca_bundle_env_vars
from .text_inserter import TextInserter
from .transcript_history import TranscriptHistoryStore


def run() -> int:
    # SSL: trust OS certificate store (handles corporate proxies like Zscaler)
    # and synchronize env vars so all HTTP libraries use the same CA bundle.
    inject_system_trust_store()
    sync_ca_bundle_env_vars()

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)

    app_logger = AppLogger()
    logger = app_logger.get_logger(APP_LOGGER_NAME)

    settings_store = SettingsStore()
    secret_store = KeyringSecretStore()
    history_store = TranscriptHistoryStore()
    last_recording_store = LastRecordingStore()
    startup_settings = settings_store.load()

    overlay = OverlayUI()
    overlay.set_opacity_percent(startup_settings.overlay_opacity_percent)
    overlay.move_to_corner(startup_settings.overlay_corner)
    overlay.show()

    hotkey_manager = HotkeyManager()
    cancel_hotkey_manager = HotkeyManager(hotkey_id=DEFAULT_CANCEL_HOTKEY_ID)
    text_inserter = TextInserter()

    controller = DictationController(
        settings_store=settings_store,
        hotkey_manager=hotkey_manager,
        cancel_hotkey_manager=cancel_hotkey_manager,
        overlay=overlay,
        text_inserter=text_inserter,
        logger=logger,
        secret_store=secret_store,
        history_store=history_store,
        last_recording_store=last_recording_store,
    )

    event_filter = QtHotkeyEventFilter(hotkey_manager, controller.toggle_recording)
    cancel_event_filter = QtHotkeyEventFilter(
        cancel_hotkey_manager,
        controller.cancel_current_action,
    )
    app.installNativeEventFilter(event_filter)
    app.installNativeEventFilter(cancel_event_filter)

    _active_history_dialog: HistoryDialog | None = None

    def open_history_dialog() -> None:
        nonlocal _active_history_dialog
        if _active_history_dialog is not None:
            _active_history_dialog.reload()
            _active_history_dialog.raise_()
            _active_history_dialog.activateWindow()
            return
        dialog = HistoryDialog(
            history_store=history_store,
            settings_store=settings_store,
            on_history_limit_changed=controller.set_history_max_items,
        )
        dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose)

        def _on_history_finished():
            nonlocal _active_history_dialog
            _active_history_dialog = None

        dialog.finished.connect(_on_history_finished)
        _active_history_dialog = dialog
        dialog.show()

    overlay.history_requested.connect(open_history_dialog)
    overlay.retry_requested.connect(controller.retry_last_transcription)
    overlay.cancel_requested.connect(controller.cancel_current_action)
    overlay.opacity_changed.connect(controller.set_overlay_opacity_percent)

    try:
        controller.initialize()
    except Exception as exc:
        overlay.set_state("Error", str(exc))
        logger.exception("Failed to initialize controller")

    tray_icon = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=overlay,
        settings_store=settings_store,
        secret_store=secret_store,
        app_logger=app_logger,
        last_recording_store=last_recording_store,
        open_history_dialog=open_history_dialog,
    )
    tray_icon.show()
    QtCore.QTimer.singleShot(
        0,
        lambda: _prompt_recoverable_last_recording(
            last_recording_store,
            tray_icon._open_settings_dialog,
        ),
    )

    app.aboutToQuit.connect(controller.shutdown)
    signal_timer = _install_signal_handlers(app)

    app._tts_refs = {
        "controller": controller,
        "overlay": overlay,
        "event_filter": event_filter,
        "cancel_event_filter": cancel_event_filter,
        "tray_icon": tray_icon,
        "signal_timer": signal_timer,
    }

    return app.exec()


def _create_tray_icon(
    app: QtWidgets.QApplication,
    controller: DictationController,
    overlay: OverlayUI,
    settings_store: SettingsStore,
    secret_store: KeyringSecretStore,
    app_logger: AppLogger,
    last_recording_store: LastRecordingStore,
    open_history_dialog,
) -> QtWidgets.QSystemTrayIcon:
    style = app.style()
    icon = style.standardIcon(QtWidgets.QStyle.SP_MediaVolume)

    tray_icon = QtWidgets.QSystemTrayIcon(icon, app)
    tray_icon.setToolTip(APP_DISPLAY_NAME)

    menu = QtWidgets.QMenu()

    toggle_action = menu.addAction("Toggle Dictation")
    toggle_action.triggered.connect(controller.toggle_recording)

    settings_action = menu.addAction("Settings")
    history_action = menu.addAction("History")
    retry_action = menu.addAction("Retry last transcription")
    cancel_action = menu.addAction("Cancel current action")

    copy_last_action = menu.addAction("Copy last transcript")
    copy_diag_action = menu.addAction("Copy diagnostics")

    menu.addSeparator()

    quit_action = menu.addAction("Quit")
    quit_action.triggered.connect(app.quit)

    _active_settings_dialog: SettingsDialog | None = None

    def open_settings_dialog() -> SettingsDialog:
        nonlocal _active_settings_dialog
        if _active_settings_dialog is not None:
            _active_settings_dialog.raise_()
            _active_settings_dialog.activateWindow()
            return _active_settings_dialog
        dialog = SettingsDialog(
            settings_store=settings_store,
            secret_store=secret_store,
            app_logger=app_logger,
            controller=controller,
            last_recording_store=last_recording_store,
        )
        dialog.settings_changed.connect(controller.on_settings_changed)
        dialog.settings_changed.connect(
            lambda: _restore_overlay_after_settings_save(overlay, settings_store)
        )
        dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose)

        def _on_dialog_finished():
            nonlocal _active_settings_dialog
            _active_settings_dialog = None

        dialog.finished.connect(_on_dialog_finished)
        _active_settings_dialog = dialog
        dialog.show()
        return dialog

    def copy_diagnostics() -> None:
        QtGui.QGuiApplication.clipboard().setText(app_logger.diagnostics_text())

    def copy_last_transcript() -> None:
        if not controller.copy_last_transcript_to_clipboard():
            controller._overlay.set_state(
                "Error", "No transcript available to copy yet."
            )

    settings_action.triggered.connect(open_settings_dialog)
    history_action.triggered.connect(open_history_dialog)
    retry_action.triggered.connect(controller.retry_last_transcription)
    cancel_action.triggered.connect(controller.cancel_current_action)
    copy_last_action.triggered.connect(copy_last_transcript)
    copy_diag_action.triggered.connect(copy_diagnostics)

    def on_tray_activated(reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason == QtWidgets.QSystemTrayIcon.DoubleClick:
            open_settings_dialog()

    tray_icon.activated.connect(on_tray_activated)
    tray_icon.setContextMenu(menu)
    tray_icon._open_settings_dialog = open_settings_dialog
    return tray_icon


def _restore_overlay_after_settings_save(
    overlay: OverlayUI,
    settings_store: SettingsStore,
) -> None:
    settings = settings_store.load()
    overlay.move_to_corner(settings.overlay_corner)
    overlay.ensure_compact_size()


def _prompt_recoverable_last_recording(
    last_recording_store: LastRecordingStore,
    open_settings_dialog,
) -> None:
    if not last_recording_store.has_recoverable_recording():
        return

    if last_recording_store.selectable_path() is None:
        return

    state = last_recording_store.load()
    description = "A previous recording is still available."
    if state is not None and state.created_at:
        description = (
            "A previous recording from "
            f"{state.created_at} is still available."
        )
    if state is not None and state.status == "failed" and state.error:
        description = f"{description}\n\nLast error: {state.error}"

    answer = QtWidgets.QMessageBox.question(
        None,
        "Recover last recording",
        (
            f"{description}\n\n"
            "Open Settings -> History and load it for transcription now?"
        ),
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        QtWidgets.QMessageBox.Yes,
    )
    if answer != QtWidgets.QMessageBox.Yes:
        return

    dialog = open_settings_dialog()
    if dialog is not None:
        dialog.prepare_last_recording_import()


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
