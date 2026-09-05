from __future__ import annotations

import signal
import sys
import threading
from datetime import datetime

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__
from .app_icon import load_app_icon
from .app_paths import appdata_root
from .config import (
    APP_DISPLAY_NAME,
    APP_LOGGER_NAME,
    APP_USER_MODEL_ID,
    DEFAULT_CANCEL_HOTKEY_ID,
    DEFAULT_REPASTE_HOTKEY_ID,
    DEFAULT_SHOW_OVERLAY_HOTKEY_ID,
    SESSION_START_LOG_MARKER,
    TRAY_CANCEL_ACTION_LABEL,
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
from .secret_store import KeyringSecretStore
from .settings_dialog import SettingsDialog
from .settings_store import SettingsStore
from .ssl_utils import inject_system_trust_store, sync_ca_bundle_env_vars
from .text_inserter import TextInserter
from .transcript_history import TranscriptHistoryStore
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

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except Exception:
        pass


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

    controller.background_transcription_failed.connect(_notify_background_failure)
    controller.background_insertion_failed.connect(
        _notify_background_insertion_failure
    )
    update_checker = _TrayUpdateChecker(
        tray_icon=tray_icon, logger=logger, parent_widget=overlay
    )
    tray_icon._update_checker = update_checker
    _schedule_startup_update_check(update_checker)
    QtCore.QTimer.singleShot(
        0,
        lambda: _prompt_recoverable_last_recording(
            last_recording_store,
            tray_icon._open_settings_dialog,
            history_store,
            parent=overlay,
        ),
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
    repaste_action = menu.addAction("Insert transcript again")
    copy_diag_action = menu.addAction("Copy diagnostics")
    check_updates_action = menu.addAction("Check for updates")

    menu.addSeparator()

    quit_action = menu.addAction("Quit")
    quit_action.triggered.connect(app.quit)

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
            checker = _TrayUpdateChecker(
                tray_icon=tray_icon, parent_widget=overlay
            )
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


def _prompt_recoverable_last_recording(
    last_recording_store: LastRecordingStore,
    open_settings_dialog,
    history_store: TranscriptHistoryStore | None = None,
    *,
    parent: QtWidgets.QWidget | None = None,
) -> None:
    if not last_recording_store.has_recoverable_recording():
        return

    if last_recording_store.selectable_path() is None:
        return

    state = last_recording_store.load()
    if _last_recording_already_transcribed(
        last_recording_store,
        history_store,
        state=state,
    ):
        return

    description = "A previous recording is still available."
    if state is not None and state.created_at:
        description = (
            "A previous recording from "
            f"{state.created_at} is still available."
        )
    if state is not None and state.status == "failed" and state.error:
        description = f"{description}\n\nLast error: {state.error}"

    answer = styled_message_box(
        icon=QtWidgets.QMessageBox.Question,
        title="Recover last recording",
        text=(
            f"{description}\n\n"
            "Open Settings -> Import Audio and load it for transcription now?"
        ),
        buttons=QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        default_button=QtWidgets.QMessageBox.Yes,
        parent=parent,
    ).exec()
    if answer != QtWidgets.QMessageBox.Yes:
        return

    dialog = open_settings_dialog()
    if dialog is not None:
        dialog.prepare_last_recording_import()


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
    recent_entries = history_store.recent_entries(limit=50)
    if recording_id:
        for entry in recent_entries:
            if str(getattr(entry, "source_recording_id", "")).strip() != recording_id:
                continue
            try:
                last_recording_store.mark_completed()
            except Exception:
                pass
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
            try:
                last_recording_store.mark_completed()
            except Exception:
                pass
            return True
        if history_ts < audio_mtime:
            break
    return False


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
