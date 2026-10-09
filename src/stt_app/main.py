from __future__ import annotations

import faulthandler
import logging
import signal
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__
from .app_icon import load_app_icon
from .app_paths import appdata_root, resolve_recordings_dir
from .config import (
    APP_DISPLAY_NAME,
    APP_LOGGER_NAME,
    APP_USER_MODEL_ID,
    DEFAULT_CANCEL_HOTKEY_ID,
    DEFAULT_REPASTE_HOTKEY_ID,
    DEFAULT_SHOW_OVERLAY_HOTKEY_ID,
    QUIT_WATCHDOG_TIMEOUT_S,
    SESSION_START_LOG_MARKER,
    TRAY_CANCEL_ACTION_LABEL,
    TRAY_REPASTE_ACTION_LABEL,
)
from .controller import DictationController
from .dialog_style import install_selectable_message_text, styled_message_box
from .history_dialog import HistoryDialog
from .hotkey import HotkeyManager, QtHotkeyEventFilter, QtPowerResumeEventFilter
from .last_recording_store import LastRecordingStore
from .local_model_inventory_store import LocalModelInventoryStore
from .local_model_scan import scan_cached_models_out_of_process
from .logger import AppLogger
from .model_download_coordinator import request_download_shutdown
from .overlay_ui import OverlayUI
from .paste_target_check import PasteTargetCheck
from .quit_dialog import QuitCoordinator
from .secret_store import KeyringSecretStore
from .settings_dialog import SettingsDialog
from .settings_store import SettingsStore
from .ssl_utils import inject_system_trust_store, sync_ca_bundle_env_vars
from .text_inserter import TextInserter
from .transcriber.base import transcript_has_gap
from .transcript_history import TranscriptHistoryStore
from .unfinished_recordings import UnfinishedRecording, UnfinishedRecordingStore
from .unfinished_recordings_dialog import UnfinishedRecordingsDialog
from .update_checker import UpdateCheckResult, check_for_updates
from .update_ui import show_update_available_dialog, show_update_status_dialog
from .win_tray_icon import create_tray_icon


def _set_windows_app_user_model_id() -> None:
    """Give the app its own Windows taskbar identity.

    Must run before the first window is created. Without an explicit
    AppUserModelID, Windows associates our windows with the host process
    (python.exe / pythonw.exe) and shows its generic icon on the taskbar
    button (most visibly for the Settings dialog). Setting an explicit ID
    makes Windows use the app/window icon for the taskbar button instead.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def _arm_quit_watchdog(log_path, logger):
    """End a quit that never finishes, and leave the reason in the log.

    A quit once removed the tray icon and then kept the process alive until
    Ctrl+C (2026-10-03), and nothing in the log said where it hung. Neither
    Ctrl+C nor Python code can rescue that: a thread blocked in native code
    never sees the signal, and once interpreter finalization starts no other
    Python thread runs. faulthandler's timer is a native thread, so it still
    writes every Python stack into the log and exits the process.

    Returns the open log stream (it must stay open until the process ends),
    or None when the log cannot be opened.
    """
    logger.info("app_quit_started watchdog_s=%s", QUIT_WATCHDOG_TIMEOUT_S)
    try:
        stream = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - held to exit
    except OSError:
        logger.warning("app_quit_watchdog_unavailable log=%s", log_path)
        return None
    faulthandler.dump_traceback_later(
        QUIT_WATCHDOG_TIMEOUT_S, repeat=False, file=stream, exit=True
    )
    return stream


def _connect_overlay_actions(overlay, controller, open_history_dialog) -> None:
    """Wire the overlay's user actions to the controller.

    Kept as its own function so the wiring can be tested by emitting the
    signals: the Error state's Insert was once connected to
    `repaste_last_transcript`, which pastes the *last transcript*, while the
    insert that failed after a streaming finalize was only the tail past the
    text already in the document -- so Insert pasted the whole dictation on
    top of it. `insert_failed_text` pastes exactly what the Error state offers.
    The tray action and the re-paste hotkey keep `repaste_last_transcript`,
    because there "the last transcript" is what the user asked for.
    """
    overlay.record_toggle_requested.connect(controller.toggle_recording)
    overlay.history_requested.connect(open_history_dialog)
    overlay.edit_requested.connect(lambda: controller.edit_last_transcript(overlay))
    overlay.retry_requested.connect(controller.retry_last_transcription)
    overlay.insert_again_requested.connect(controller.insert_failed_text)
    overlay.cancel_requested.connect(controller.cancel_current_action)
    overlay.queue_cancel_requested.connect(controller.cancel_queued_transcription)
    overlay.queue_clear_requested.connect(controller.clear_transcription_queue)
    overlay.detail_cleared.connect(controller.on_overlay_detail_cleared)
    overlay.opacity_changed.connect(controller.set_overlay_opacity_percent)
    overlay.always_on_top_changed.connect(controller.set_overlay_always_on_top)
    overlay.language_changed.connect(controller.set_language_mode)
    overlay.microphone_changed.connect(controller.set_input_device_name)
    overlay.microphone_menu_requested.connect(
        controller.refresh_overlay_microphone_options
    )


def _connect_tray_notifications(tray_icon, controller) -> None:
    """Wire the controller's out-of-band reports to tray notifications.

    Each is a message the overlay cannot carry: a queued transcription's
    failure and a queued transcript that was produced but not pasted, both
    while a live session may own the overlay, and an error raised while a
    recording or a transcription is in flight -- painted over "Listening"
    until wave 12, when the opacity slider's save refused by a locked
    `settings.json` told the user the recording had failed while the
    microphone kept recording underneath. Its own function so the wiring is
    pinned by emitting the signals at a fake tray.
    """

    def _notify_background_failure(message: str) -> None:
        # The overlay belongs to the live session, so a queued job's failure is
        # reported here as well; without it the failure was invisible.
        tray_icon.showMessage(
            "Transcription failed",
            message,
            QtWidgets.QSystemTrayIcon.Warning,
            10000,
        )

    def _notify_background_insertion_failure(message: str) -> None:
        # A queued transcript that was produced but not pasted is just as lost
        # to the user as a failed transcription; both must be reported.
        tray_icon.showMessage(
            "Transcript not inserted",
            message,
            QtWidgets.QSystemTrayIcon.Warning,
            10000,
        )

    def _notify_busy_overlay_error(message: str) -> None:
        tray_icon.showMessage(
            APP_DISPLAY_NAME,
            message,
            QtWidgets.QSystemTrayIcon.Warning,
            10000,
        )

    controller.background_transcription_failed.connect(_notify_background_failure)
    controller.background_insertion_failed.connect(_notify_background_insertion_failure)

    def _notify_clipboard_restore_failure(message: str) -> None:
        # The paste itself was reported long before; this is the user's own
        # clipboard content, which they will reach for next.
        tray_icon.showMessage(
            "Clipboard not restored",
            message,
            QtWidgets.QSystemTrayIcon.Warning,
            10000,
        )

    controller.busy_overlay_error.connect(_notify_busy_overlay_error)
    controller.clipboard_restore_failed.connect(_notify_clipboard_restore_failure)


def run() -> int:
    # SSL: trust OS certificate store (handles corporate proxies like Zscaler)
    # and synchronize env vars so all HTTP libraries use the same CA bundle.
    inject_system_trust_store()
    sync_ca_bundle_env_vars()

    _set_windows_app_user_model_id()

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setWindowIcon(load_app_icon())
    app.setQuitOnLastWindowClosed(False)
    # Qt message boxes are not selectable by default, so an error could only be
    # retyped or screenshotted. One filter covers every box the app raises,
    # including the ones built by the QMessageBox convenience statics.
    install_selectable_message_text(app)

    instance_lock = QtCore.QLockFile(str(appdata_root() / "stt_app.lock"))
    instance_lock.setStaleLockTime(0)
    if not instance_lock.tryLock(0):
        styled_message_box(
            icon=QtWidgets.QMessageBox.Information,
            title=APP_DISPLAY_NAME,
            text=f"{APP_DISPLAY_NAME} is already running.",
            buttons=QtWidgets.QMessageBox.Ok,
            default_button=QtWidgets.QMessageBox.Ok,
        ).exec()
        return 0

    app_logger = AppLogger()
    logger = app_logger.get_logger(APP_LOGGER_NAME)
    # Marks where a session begins. "Copy diagnostics" cuts here so the copied
    # text covers exactly the current run instead of an arbitrary line count.
    logger.info("%s version=%s", SESSION_START_LOG_MARKER, __version__)

    settings_store = SettingsStore()
    secret_store = KeyringSecretStore()
    history_store = TranscriptHistoryStore()
    last_recording_store = LastRecordingStore()
    unfinished_recording_store = UnfinishedRecordingStore()
    local_model_inventory_store = LocalModelInventoryStore()
    startup_settings = settings_store.load()
    _schedule_startup_local_model_inventory_refresh(
        local_model_inventory_store,
        startup_settings.model_dir,
    )

    overlay = OverlayUI()
    overlay.set_opacity_percent(startup_settings.overlay_opacity_percent)
    overlay.set_always_on_top(startup_settings.overlay_always_on_top)
    overlay.move_to_corner(startup_settings.overlay_corner)
    overlay.show()

    hotkey_manager = HotkeyManager()
    cancel_hotkey_manager = HotkeyManager(hotkey_id=DEFAULT_CANCEL_HOTKEY_ID)
    show_overlay_hotkey_manager = HotkeyManager(
        hotkey_id=DEFAULT_SHOW_OVERLAY_HOTKEY_ID
    )
    repaste_hotkey_manager = HotkeyManager(hotkey_id=DEFAULT_REPASTE_HOTKEY_ID)
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
        show_overlay_hotkey_manager=show_overlay_hotkey_manager,
        repaste_hotkey_manager=repaste_hotkey_manager,
        paste_target_check=PasteTargetCheck(),
        unfinished_recording_store=unfinished_recording_store,
    )

    event_filter = QtHotkeyEventFilter(hotkey_manager, controller.toggle_recording)
    cancel_event_filter = QtHotkeyEventFilter(
        cancel_hotkey_manager,
        controller.cancel_current_action,
    )
    show_overlay_event_filter = QtHotkeyEventFilter(
        show_overlay_hotkey_manager,
        controller.bring_overlay_to_front,
    )
    repaste_event_filter = QtHotkeyEventFilter(
        repaste_hotkey_manager,
        controller.repaste_last_transcript,
    )
    app.installNativeEventFilter(event_filter)
    app.installNativeEventFilter(cancel_event_filter)
    app.installNativeEventFilter(show_overlay_event_filter)
    app.installNativeEventFilter(repaste_event_filter)
    power_resume_timer = QtCore.QTimer(app)
    power_resume_timer.setSingleShot(True)
    power_resume_timer.setInterval(750)
    power_resume_timer.timeout.connect(
        lambda: _restore_after_system_resume(controller, overlay)
    )
    power_resume_filter = QtPowerResumeEventFilter(power_resume_timer.start)
    app.installNativeEventFilter(power_resume_filter)

    history_dialog_presenter = _HistoryDialogPresenter(
        history_store=history_store,
        settings_store=settings_store,
        on_history_limit_changed=controller.set_history_max_items,
        last_recording_store=last_recording_store,
        controller=controller,
    )
    open_history_dialog = history_dialog_presenter.open

    _connect_overlay_actions(overlay, controller, open_history_dialog)

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
        local_model_inventory_store=local_model_inventory_store,
        open_history_dialog=open_history_dialog,
    )
    tray_icon.show()

    _connect_tray_notifications(tray_icon, controller)
    update_checker = _TrayUpdateChecker(
        tray_icon=tray_icon, logger=logger, parent_widget=overlay
    )
    tray_icon._update_checker = update_checker
    _schedule_startup_update_check(update_checker)

    def _show_unfinished_recordings() -> None:
        # Kept on the tray icon: the notice has no parent to keep it alive.
        tray_icon._unfinished_recordings_dialog = _offer_unfinished_recordings(
            last_recording_store=last_recording_store,
            history_store=history_store,
            unfinished_store=unfinished_recording_store,
            transcribe=controller.transcribe_unfinished_recording,
            keep_dir=resolve_recordings_dir(controller.settings.recordings_dir),
        )

    QtCore.QTimer.singleShot(0, _show_unfinished_recordings)

    # Before any shutdown work, so that a step that hangs is still bounded.
    quit_watchdog_streams = []
    app.aboutToQuit.connect(
        lambda: quit_watchdog_streams.append(
            _arm_quit_watchdog(app_logger.log_path, logger)
        )
    )
    # First: a hand-registered icon must be removed explicitly, or a dead icon
    # stays in the tray until the user hovers over it. Doing it before the
    # shutdown work below also makes it disappear immediately instead of after
    # however long stopping the runtimes takes.
    if hasattr(tray_icon, "close"):
        app.aboutToQuit.connect(tray_icon.close)
    # First of all: stop anyone from waiting for the download slot. The dialog
    # shutdown below cancels the Local tab's download and releases the slot, so
    # without this a transcriber blocked in acquire() would start a fresh
    # multi-gigabyte download on a non-daemon thread that the interpreter then
    # joins at exit — a process with no tray icon still downloading for minutes.
    app.aboutToQuit.connect(request_download_shutdown)
    app.aboutToQuit.connect(tray_icon._shutdown_settings_dialog)
    app.aboutToQuit.connect(controller.shutdown)
    signal_timer = _install_signal_handlers(app)

    app._tts_refs = {
        "controller": controller,
        "overlay": overlay,
        "event_filter": event_filter,
        "cancel_event_filter": cancel_event_filter,
        "show_overlay_event_filter": show_overlay_event_filter,
        "repaste_event_filter": repaste_event_filter,
        "power_resume_filter": power_resume_filter,
        "power_resume_timer": power_resume_timer,
        "tray_icon": tray_icon,
        "history_dialog_presenter": history_dialog_presenter,
        "signal_timer": signal_timer,
        "instance_lock": instance_lock,
        "quit_watchdog_streams": quit_watchdog_streams,
    }

    exit_code = app.exec()
    # The interpreter joins every non-daemon thread after this, so a thread
    # that never ends keeps a process without windows or tray icon alive.
    lingering = [
        thread.name
        for thread in threading.enumerate()
        if not thread.daemon and thread is not threading.main_thread()
    ]
    logger.info("app_exec_returned code=%s non_daemon_threads=%s", exit_code, lingering)
    return exit_code


def _create_tray_icon(
    app: QtWidgets.QApplication,
    controller: DictationController,
    overlay: OverlayUI,
    settings_store: SettingsStore,
    secret_store: KeyringSecretStore,
    app_logger: AppLogger,
    last_recording_store: LastRecordingStore,
    open_history_dialog,
    local_model_inventory_store: LocalModelInventoryStore | None = None,
):
    # Windows gets a hand-registered notification icon; see win_tray_icon for
    # why QSystemTrayIcon closes the hidden-icons flyout.
    tray_icon = create_tray_icon(app, load_app_icon(), APP_DISPLAY_NAME)

    menu = QtWidgets.QMenu()

    toggle_action = menu.addAction("Toggle Dictation")
    toggle_action.triggered.connect(controller.toggle_recording)

    show_overlay_action = menu.addAction("Show overlay")
    show_overlay_action.triggered.connect(controller.bring_overlay_to_front)

    settings_action = menu.addAction("Settings")
    history_action = menu.addAction("History")
    retry_action = menu.addAction("Retry transcription")
    cancel_action = menu.addAction(TRAY_CANCEL_ACTION_LABEL)

    copy_last_action = menu.addAction("Copy transcript")
    repaste_action = menu.addAction(TRAY_REPASTE_ACTION_LABEL)
    copy_diag_action = menu.addAction("Copy diagnostics")
    check_updates_action = menu.addAction("Check for updates")

    menu.addSeparator()

    quit_action = menu.addAction("Quit")
    # Asks first when work is pending, and may wait for it; `app.quit` -- and
    # with it the watchdog armed on `aboutToQuit` -- runs only once the app
    # really quits.
    quit_coordinator = QuitCoordinator(controller, app.quit, parent=menu)
    quit_action.triggered.connect(quit_coordinator.request)

    _active_settings_dialog: SettingsDialog | None = None

    def present_settings_dialog(dialog: SettingsDialog) -> None:
        _present_dialog(dialog)

    def create_settings_dialog() -> SettingsDialog:
        nonlocal _active_settings_dialog
        dialog = SettingsDialog(
            settings_store=settings_store,
            secret_store=secret_store,
            app_logger=app_logger,
            controller=controller,
            last_recording_store=last_recording_store,
            local_model_inventory_store=local_model_inventory_store,
        )
        # A replaced API key never reaches ``AppSettings`` -- ``has_*_key``
        # only flips when a key is added or removed -- so without this
        # connection a runtime keeps running on the previous credential.
        # The ordering that makes it work is the dialog's *emit* order (it
        # emits this signal before ``settings_changed``), not the order of
        # these two ``connect`` calls: they are different signals, so
        # connection order does not relate them.
        dialog.provider_keys_changed.connect(
            controller.invalidate_transcriber_credentials
        )
        dialog.settings_changed.connect(controller.on_settings_changed)
        dialog.settings_changed.connect(
            lambda: _restore_overlay_after_settings_save(overlay, settings_store)
        )
        dialog.audio_device_refresh_requested.connect(
            controller.request_audio_device_refresh
        )
        _active_settings_dialog = dialog
        return dialog

    def prepare_settings_dialog() -> None:
        nonlocal _active_settings_dialog
        if _active_settings_dialog is None:
            _active_settings_dialog = create_settings_dialog()
        if not _active_settings_dialog.isVisible():
            _active_settings_dialog.prepare_for_first_show()

    def open_settings_dialog() -> SettingsDialog:
        nonlocal _active_settings_dialog
        if _active_settings_dialog is None:
            _active_settings_dialog = create_settings_dialog()
        elif not _active_settings_dialog.isVisible():
            reloader = getattr(_active_settings_dialog, "reload_from_store", None)
            if callable(reloader):
                reloader()
        present_settings_dialog(_active_settings_dialog)
        return _active_settings_dialog

    def shutdown_settings_dialog() -> None:
        if _active_settings_dialog is None:
            return
        shutdown = getattr(_active_settings_dialog, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def copy_diagnostics() -> None:
        text = app_logger.diagnostics_text()
        QtGui.QGuiApplication.clipboard().setText(text)
        controller.show_overlay_notice(
            f"Diagnostics copied to clipboard ({len(text.splitlines())} lines)."
        )

    def copy_last_transcript() -> None:
        if not controller.copy_last_transcript_to_clipboard():
            controller.show_overlay_error("No transcript available to copy yet.")
            return
        controller.show_overlay_notice("Last transcript copied to clipboard.")

    settings_action.triggered.connect(open_settings_dialog)
    history_action.triggered.connect(open_history_dialog)
    retry_action.triggered.connect(controller.retry_last_transcription)
    cancel_action.triggered.connect(controller.cancel_current_action)
    copy_last_action.triggered.connect(copy_last_transcript)
    repaste_action.triggered.connect(controller.repaste_last_transcript)
    copy_diag_action.triggered.connect(copy_diagnostics)

    def check_for_updates_from_tray() -> None:
        checker = getattr(tray_icon, "_update_checker", None)
        if checker is None:
            checker = _TrayUpdateChecker(tray_icon=tray_icon, parent_widget=overlay)
            tray_icon._update_checker = checker
        checker.start(manual=True, action=check_updates_action)

    check_updates_action.triggered.connect(check_for_updates_from_tray)

    def on_tray_activated(reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        # First, because for a context-menu click this runs while the
        # user's own window is still in front: the native menu is about to
        # take the foreground for our hidden host window, as the
        # notification-icon contract requires, and after that there is no
        # way to find out what was there.
        controller.note_foreground_window()
        if reason == QtWidgets.QSystemTrayIcon.DoubleClick:
            open_settings_dialog()
            return
        if reason == QtWidgets.QSystemTrayIcon.Trigger:
            # A single left click has no other meaning here and there is no
            # main window, so use it to surface the overlay. Together with the
            # overlay's Record button this makes dictation reachable entirely
            # without a keyboard.
            controller.bring_overlay_to_front()
            return
        if reason == QtWidgets.QSystemTrayIcon.MiddleClick and bool(
            getattr(controller.settings, "tray_middle_click_toggle", True)
        ):
            controller.toggle_recording()

    tray_icon.activated.connect(on_tray_activated)
    # Also kept reachable for callers/tests that need the menu itself.
    tray_icon._context_menu = menu
    tray_icon.setContextMenu(menu)
    tray_icon._open_settings_dialog = open_settings_dialog
    tray_icon._shutdown_settings_dialog = shutdown_settings_dialog
    tray_icon._quit_coordinator = quit_coordinator
    QtCore.QTimer.singleShot(2500, prepare_settings_dialog)
    return tray_icon


class _TrayUpdateChecker(QtCore.QObject):
    finished = QtCore.Signal(object, bool)

    def __init__(
        self,
        *,
        tray_icon: QtWidgets.QSystemTrayIcon,
        logger=None,
        runner=check_for_updates,
        parent_widget: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(tray_icon)
        self._tray_icon = tray_icon
        self._logger = logger
        self._runner = runner
        self._parent_widget = parent_widget
        self._active_thread: threading.Thread | None = None
        self._active_action: QtGui.QAction | None = None
        self._manual_requested_while_active = False
        self.finished.connect(self._on_finished)

    def start(
        self,
        *,
        manual: bool = False,
        action: QtGui.QAction | None = None,
    ) -> None:
        if self._active_thread is not None:
            if manual:
                self._manual_requested_while_active = True
                if action is not None:
                    self._active_action = action
                    action.setEnabled(False)
            return
        self._active_action = action
        if action is not None:
            action.setEnabled(False)

        def _run() -> None:
            try:
                result = self._runner()
            except Exception as exc:
                result = UpdateCheckResult(
                    current_version="",
                    error=f"Update check failed: {exc}",
                )
            self.finished.emit(result, manual)

        thread = threading.Thread(
            target=_run,
            name="stt_app_update_check",
            daemon=True,
        )
        self._active_thread = thread
        thread.start()

    @QtCore.Slot(object, bool)
    def _on_finished(self, result: object, manual: bool) -> None:
        manual = bool(manual or self._manual_requested_while_active)
        self._manual_requested_while_active = False
        self._active_thread = None
        if self._active_action is not None:
            self._active_action.setEnabled(True)
            self._active_action = None
        if not isinstance(result, UpdateCheckResult):
            result = UpdateCheckResult(
                current_version="",
                error="Update check returned an unexpected result.",
            )

        if result.update_available:
            self._tray_icon.showMessage(
                APP_DISPLAY_NAME,
                (
                    f"Update {result.latest_tag or result.latest_version} is "
                    f"available. Current version: {result.current_version}."
                ),
                QtWidgets.QSystemTrayIcon.Information,
                10000,
            )
            if manual:
                show_update_available_dialog(result, parent=self._parent_widget)
            return

        if result.error:
            if self._logger is not None:
                try:
                    self._logger.info("Update check skipped/failed: %s", result.error)
                except Exception:
                    pass
            if manual:
                show_update_status_dialog(
                    parent=self._parent_widget,
                    title="Update check failed",
                    text=result.error,
                    icon=QtWidgets.QMessageBox.Warning,
                )
            return

        if manual:
            show_update_status_dialog(
                parent=self._parent_widget,
                title="You're up to date",
                text=(
                    f"Version {result.current_version} is installed. "
                    "No newer release is available."
                ),
            )


def _schedule_startup_update_check(checker: _TrayUpdateChecker) -> None:
    QtCore.QTimer.singleShot(5000, lambda: checker.start(manual=False))


class _HistoryDialogPresenter:
    def __init__(
        self,
        *,
        history_store: TranscriptHistoryStore,
        settings_store: SettingsStore,
        on_history_limit_changed,
        last_recording_store=None,
        controller=None,
    ) -> None:
        self._history_store = history_store
        self._settings_store = settings_store
        self._on_history_limit_changed = on_history_limit_changed
        self._last_recording_store = last_recording_store
        self._controller = controller
        self._active_dialog: HistoryDialog | None = None

    def open(self) -> HistoryDialog:
        if self._active_dialog is not None:
            # Refresh once so re-clicking History shows current entries;
            # reload(force=True) preserves selection and scroll position.
            _reload_history_dialog(self._active_dialog, force=True)
            _present_dialog(self._active_dialog)
            return self._active_dialog

        dialog = HistoryDialog(
            history_store=self._history_store,
            settings_store=self._settings_store,
            on_history_limit_changed=self._on_history_limit_changed,
            autoload=False,
            last_recording_store=self._last_recording_store,
            controller=self._controller,
        )
        dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        dialog.finished.connect(lambda: self._clear_dialog(dialog))
        self._active_dialog = dialog
        _present_dialog(dialog)
        QtCore.QTimer.singleShot(0, lambda: _reload_history_dialog(dialog))
        return dialog

    def _clear_dialog(self, dialog: HistoryDialog) -> None:
        if self._active_dialog is dialog:
            self._active_dialog = None


def _present_dialog(dialog: QtWidgets.QDialog) -> None:
    if dialog.isMinimized():
        dialog.showNormal()
    elif not dialog.isVisible():
        dialog.show()
    else:
        dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def _reload_history_dialog(dialog: HistoryDialog, force: bool = False) -> None:
    try:
        if dialog.isVisible():
            dialog.reload(force=force)
    except RuntimeError:
        return


def _restore_overlay_after_settings_save(
    overlay: OverlayUI,
    settings_store: SettingsStore,
) -> None:
    settings = settings_store.load()
    overlay.set_always_on_top(settings.overlay_always_on_top)
    overlay.apply_corner_setting(settings.overlay_corner)
    # Not `ensure_compact_size`: saving settings while a transcript is on the
    # overlay used to truncate it to the compact cap and leave the overlay
    # compact under a `Done` label.
    overlay.ensure_compact_size_unless_showing_a_result()


def _restore_after_system_resume(
    controller: DictationController,
    overlay: OverlayUI,
) -> None:
    resume_handler = getattr(controller, "handle_system_resume", None)
    if callable(resume_handler):
        resume_handler()
    else:
        controller.refresh_hotkey_registration()
    overlay.restore_visibility()


def _schedule_startup_local_model_inventory_refresh(
    inventory_store: LocalModelInventoryStore,
    model_dir: str,
) -> None:
    QtCore.QTimer.singleShot(
        1500,
        lambda: _refresh_local_model_inventory_in_background(
            inventory_store,
            model_dir,
        ),
    )


def _refresh_local_model_inventory_in_background(
    inventory_store: LocalModelInventoryStore,
    model_dir: str,
) -> None:
    normalized_dir = str(model_dir or "").strip()

    def _run() -> None:
        cached = scan_cached_models_out_of_process(normalized_dir)
        if cached is None:
            return
        try:
            inventory_store.save_cached_models(normalized_dir, cached)
        except Exception:
            pass

    threading.Thread(
        target=_run,
        name="stt_app_startup_local_model_inventory",
        daemon=True,
    ).start()


def _offer_unfinished_recordings(
    *,
    last_recording_store: LastRecordingStore,
    history_store: TranscriptHistoryStore | None,
    unfinished_store: UnfinishedRecordingStore,
    transcribe,
    keep_dir: Path,
) -> UnfinishedRecordingsDialog | None:
    """Show the recordings an earlier session did not transcribe, if any.

    One notice for all of them: the managed last recording, when it is
    still recoverable and not in history yet, joins the recordings the last
    quit kept (`_adopt_recoverable_last_recording`). A recording whose
    transcript is already in history is not offered
    (`_without_transcribed_recordings`). Returns the open notice, which the
    caller keeps a reference to.
    """
    problems = []
    adoption_problem = _adopt_recoverable_last_recording(
        last_recording_store, history_store, unfinished_store
    )
    if adoption_problem:
        problems.append(adoption_problem)
    try:
        listed = unfinished_store.list_recordings()
    except OSError as exc:
        problems.append(
            f"The folder {unfinished_store.directory} could not be read: {exc}"
        )
        listed = []
    recordings = _without_transcribed_recordings(
        listed, history_store, unfinished_store
    )
    if not recordings:
        if problems:
            styled_message_box(
                icon=QtWidgets.QMessageBox.Warning,
                title="Unfinished recordings",
                text="\n\n".join(problems),
                buttons=QtWidgets.QMessageBox.Ok,
                default_button=QtWidgets.QMessageBox.Ok,
            ).exec()
        return None
    dialog = UnfinishedRecordingsDialog(
        recordings=recordings,
        store=unfinished_store,
        transcribe=transcribe,
        keep_dir=keep_dir,
    )
    if problems:
        dialog.show_problem(" ".join(problems))
    _present_dialog(dialog)
    return dialog


def _adopt_recoverable_last_recording(
    last_recording_store: LastRecordingStore,
    history_store: TranscriptHistoryStore | None,
    unfinished_store: UnfinishedRecordingStore,
) -> str:
    """Move a recoverable managed last recording into the unfinished store.

    The notice then lists it like every other one. The copy comes first; the
    slot is marked completed only once the copy exists, which deletes its
    audio when "Keep last recording after successful transcription" is off.
    A recording without a state (or with one that cannot be read) gets an id
    from the file's modification time, so a slot that cannot be marked is
    not copied a second time at the next start. Returns a problem to show,
    or "".
    """
    if not last_recording_store.has_recoverable_recording():
        return ""
    state = last_recording_store.load()
    if _last_recording_already_transcribed(
        last_recording_store, history_store, state=state
    ):
        return ""
    audio_path = last_recording_store.audio_path
    try:
        audio = audio_path.read_bytes()
        modified_ns = audio_path.stat().st_mtime_ns
    except OSError as exc:
        return f"The last recording could not be read ({exc}); it is at {audio_path}."
    state_id = str(getattr(state, "recording_id", "") or "").strip()
    recorded_at = _local_time(str(getattr(state, "created_at", "") or ""))
    if recorded_at is None:
        recorded_at = (
            datetime.fromtimestamp(modified_ns / 1e9, UTC)
            .astimezone()
            .replace(tzinfo=None)
        )
    try:
        unfinished_store.save(
            audio,
            recording_id=state_id or f"orphan-{modified_ns}",
            recorded_at=recorded_at,
        )
    except (OSError, ValueError) as exc:
        return (
            f"The last recording could not be added to this list ({exc}); it is "
            f"at {audio_path}."
        )
    try:
        last_recording_store.mark_completed(expected_recording_id=state_id or None)
    except Exception:
        # The copy is kept under the same id, so the next start does not add
        # it twice; the slot stays recoverable until it can be marked.
        logging.getLogger(APP_LOGGER_NAME).exception(
            "Failed to mark the adopted last recording"
        )
    return ""


def _local_time(created_at: str) -> datetime | None:
    """A stored UTC timestamp as the user's wall clock, for display."""
    try:
        moment = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone().replace(tzinfo=None)


def _without_transcribed_recordings(
    recordings: list[UnfinishedRecording],
    history_store: TranscriptHistoryStore | None,
    unfinished_store: UnfinishedRecordingStore,
) -> list[UnfinishedRecording]:
    """Leave out every recording whose transcript is already in history.

    `transcribe_unfinished_recording` deletes a file once its transcript is
    in history; a delete that failed is finished here. A transcript with a
    gap marker keeps its file on purpose (the entry points at it), so it is
    left on disk and not offered again. Only an import's entry counts
    (`transcribe_unfinished_recording` and the Import tab write mode
    "import"): a dictation entry under the same id can be a partial
    transcript (`_is_partial_transcript`), and offering a recording twice
    costs a second transcript, deleting it costs the recording. An
    unreadable history answers no entries, and every recording is offered:
    nothing is deleted on a guess.
    """
    try:
        entries = history_store.load() if history_store is not None else []
    except Exception:
        entries = []
    texts_by_id: dict[str, list[str]] = {}
    for entry in entries:
        recording_id = str(getattr(entry, "source_recording_id", "") or "").strip()
        if recording_id and str(getattr(entry, "mode", "") or "") == "import":
            texts_by_id.setdefault(recording_id, []).append(str(entry.text or ""))
    offered = []
    for recording in recordings:
        texts = texts_by_id.get(recording.recording_id)
        if texts is None:
            offered.append(recording)
            continue
        if not any(transcript_has_gap(text) for text in texts):
            try:
                unfinished_store.discard(recording)
            except OSError:
                logging.getLogger(APP_LOGGER_NAME).exception(
                    "Failed to delete a transcribed recording"
                )
    return offered


def _last_recording_already_transcribed(
    last_recording_store: LastRecordingStore,
    history_store: TranscriptHistoryStore | None,
    *,
    state=None,
) -> bool:
    if history_store is None:
        return False

    current_state = state or last_recording_store.load()
    if current_state is None:
        return False

    recording_id = str(
        getattr(current_state, "recording_id", "")
        or getattr(current_state, "created_at", "")
    ).strip()
    recent_entries = [
        entry
        for entry in history_store.recent_entries(limit=50)
        if not _is_partial_transcript(entry)
    ]
    if recording_id:
        for entry in recent_entries:
            if str(getattr(entry, "source_recording_id", "")).strip() != recording_id:
                continue
            _complete_unless_gap(last_recording_store, entry)
            return True

    path = last_recording_store.selectable_path()
    if path is None:
        return False
    try:
        audio_mtime = path.stat().st_mtime
    except OSError:
        return False

    for entry in recent_entries:
        try:
            history_ts = datetime.fromisoformat(entry.created_at).timestamp()
        except Exception:
            continue
        if 0 <= (history_ts - audio_mtime) <= 180:
            _complete_unless_gap(last_recording_store, entry)
            return True
        if history_ts < audio_mtime:
            break
    return False


def _is_partial_transcript(entry) -> bool:
    """A history entry that may hold only part of its recording.

    A streaming dictation that dies writes what it heard so far under the
    recording's id (mode "streaming") and keeps the whole audio for Retry;
    so does a finalize that returned nothing. Such an entry does not mean
    the recording was transcribed, and taking it for one deleted the only
    complete audio (review of 3ee1e23). A streaming dictation that finished
    marks its recording completed, so it never reaches these checks.
    """
    return str(getattr(entry, "mode", "") or "").strip() == "streaming"


def _complete_unless_gap(last_recording_store: LastRecordingStore, entry) -> None:
    """Complete a recording whose transcript is already in history -- unless
    that transcript carries a gap marker. The controller kept such a
    recording on purpose (marked failed): completing it here deletes the audio
    with `keep_after_success` off, and the marker names a stretch of it the
    user may still need to listen to."""
    if transcript_has_gap(str(getattr(entry, "text", "") or "")):
        return
    try:
        last_recording_store.mark_completed()
    except Exception:
        pass


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
