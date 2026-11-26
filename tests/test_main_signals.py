import os
import signal
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

import stt_app.main as main_module
from stt_app.app_icon import app_icon_path, load_app_icon
from stt_app.config import APP_DISPLAY_NAME
from stt_app.controller import DictationController, PendingQuitWork
from stt_app.last_recording_store import LastRecordingStore
from stt_app.main import (
    _create_tray_icon,
    _HistoryDialogPresenter,
    _install_signal_handlers,
    _last_recording_already_transcribed,
    _refresh_local_model_inventory_in_background,
    _restore_after_system_resume,
    _restore_overlay_after_settings_save,
    _TrayUpdateChecker,
)
from stt_app.settings_store import AppSettings
from stt_app.transcriber import local_webgpu_asr
from stt_app.transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore
from stt_app.unfinished_recordings import UnfinishedRecordingStore
from stt_app.update_checker import UpdateCheckResult


@pytest.fixture(autouse=True)
def _no_real_tray_icon(monkeypatch):
    """Keep the tests off the real notification area.

    ``create_tray_icon`` registers a genuine Windows tray icon; these tests are
    about the wiring around it (menu actions, activation handling), so they use
    the QSystemTrayIcon fallback instead of littering the tray.
    """
    monkeypatch.setattr(
        main_module,
        "create_tray_icon",
        lambda parent, icon, tooltip=None, logger=None: QtWidgets.QSystemTrayIcon(
            icon, parent
        ),
    )


def test_install_signal_handlers_registers_int_and_term(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    registered = []

    def fake_signal(sig, handler):
        registered.append(sig)
        return handler

    monkeypatch.setattr(signal, "signal", fake_signal)

    timer = _install_signal_handlers(app)

    assert signal.SIGINT in registered
    assert signal.SIGTERM in registered
    assert timer.isActive() is True

    timer.stop()


# ---------------------------------------------------------------------------
# _create_tray_icon tests
# ---------------------------------------------------------------------------


class FakeController:
    def __init__(self):
        self.toggle_calls = 0
        self._overlay = type("obj", (object,), {"set_state": lambda *a: None})()
        self._last_transcript = ""
        self.settings_changed_calls = 0
        self.hotkey_refresh_calls = 0
        self.resume_calls = 0
        self.bring_overlay_calls = 0
        self.audio_device_refresh_calls = 0
        self.repaste_calls = 0
        self.insert_failed_calls = 0
        self.detail_cleared_calls: list[str] = []
        self.credentials_invalidated = 0
        self.credentials_providers: list[str] = []
        self.note_foreground_calls = 0
        self.microphone_picks: list[str] = []
        self.microphone_option_refreshes = 0
        self.settings = AppSettings()
        self.pending_quit_work = PendingQuitWork()
        self.quit_holds = 0

    def note_foreground_window(self):
        self.note_foreground_calls += 1

    def quit_pending_work(self):
        return self.pending_quit_work

    def has_active_session(self):
        return False

    def hold_for_quit(self):
        self.quit_holds += 1

    def release_quit_hold(self):
        pass

    def toggle_recording(self):
        self.toggle_calls += 1

    def bring_overlay_to_front(self):
        self.bring_overlay_calls += 1

    def repaste_last_transcript(self):
        self.repaste_calls += 1

    def insert_failed_text(self):
        self.insert_failed_calls += 1

    def on_overlay_detail_cleared(self, copy_text):
        self.detail_cleared_calls.append(copy_text)

    def edit_last_transcript(self, overlay):
        pass

    def cancel_queued_transcription(self, token):
        pass

    def clear_transcription_queue(self):
        pass

    def set_overlay_opacity_percent(self, percent):
        pass

    def set_overlay_always_on_top(self, enabled):
        pass

    def set_language_mode(self, mode):
        pass

    def set_input_device_name(self, name):
        self.microphone_picks.append(name)

    def refresh_overlay_microphone_options(self):
        self.microphone_option_refreshes += 1

    def reload_settings(self, re_register_hotkey=True):
        pass

    def show_idle_status(self):
        pass

    def copy_last_transcript_to_clipboard(self):
        return bool(self._last_transcript)

    def on_settings_changed(self):
        self.settings_changed_calls += 1

    def invalidate_transcriber_credentials(self, providers=None):
        self.credentials_invalidated += 1
        self.credentials_providers = list(providers or ())

    def refresh_hotkey_registration(self):
        self.hotkey_refresh_calls += 1

    def request_audio_device_refresh(self):
        self.audio_device_refresh_calls += 1

    def handle_system_resume(self):
        self.resume_calls += 1
        self.refresh_hotkey_registration()

    def retry_last_transcription(self):
        return True

    def cancel_current_action(self):
        return None

    def shutdown(self):
        pass


class FakeSettingsStore:
    def __init__(self, settings: AppSettings | None = None):
        self._settings = settings or AppSettings()

    def load(self):
        return self._settings

    def save(self, s):
        pass


class FakeSecretStore:
    def get_api_key(self, name):
        return ""


class FakeAppLogger:
    def diagnostics_text(self):
        return "log output"

    def exception(self, *_args, **_kwargs):
        return None


class FakeOverlay:
    def __init__(self):
        self.moved_to = None
        self.compact_calls = 0
        self.compact_policy_calls = 0
        self.always_on_top_values = []
        self.restore_visibility_calls = 0

    def apply_corner_setting(self, corner):
        self.moved_to = corner

    def set_always_on_top(self, value):
        self.always_on_top_values.append(bool(value))

    def ensure_compact_size(self):
        self.compact_calls += 1

    def ensure_compact_size_unless_showing_a_result(self):
        self.compact_policy_calls += 1

    def restore_visibility(self):
        self.restore_visibility_calls += 1


class FakeLastRecordingStore:
    def has_recoverable_recording(self):
        return False

    def selectable_path(self, archived_recordings_dir=None):
        return None

    def load(self):
        return None


class ImmediateThread:
    def __init__(self, target, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class FakeLocalModelInventoryStore:
    def __init__(self):
        self.saved: list[tuple[str, list[str]]] = []

    def save_cached_models(self, model_dir, cached_models):
        self.saved.append((model_dir, list(cached_models)))


class FakeTrayIcon(QtWidgets.QSystemTrayIcon):
    def __init__(self):
        super().__init__()
        self.messages: list[tuple[str, str]] = []

    def isVisible(self):
        # A registered icon: the start notices wait for one.
        return True

    def showMessage(self, title, message, icon=None, msecs=10000):
        self.messages.append((title, message))


def test_startup_local_model_inventory_refresh_saves_scan(monkeypatch):
    calls: list[str] = []
    inventory_store = FakeLocalModelInventoryStore()

    monkeypatch.setattr(
        main_module,
        "scan_cached_models_out_of_process",
        lambda model_dir: calls.append(model_dir) or ["small"],
    )
    monkeypatch.setattr(main_module.threading, "Thread", ImmediateThread)

    _refresh_local_model_inventory_in_background(
        inventory_store,
        " /tmp/models ",
    )

    assert calls == ["/tmp/models"]
    assert inventory_store.saved == [("/tmp/models", ["small"])]


def test_create_tray_icon_has_expected_menu_actions():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = FakeController()
    overlay = FakeOverlay()
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=overlay,
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )
    menu = tray._context_menu
    action_labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert "Toggle Dictation" in action_labels
    assert "Show overlay" in action_labels
    assert "Settings" in action_labels
    assert "History" in action_labels
    assert "Retry transcription" in action_labels
    assert "Cancel current action" in action_labels
    assert "Copy transcript" in action_labels
    assert "Insert transcript again" in action_labels
    assert "Copy diagnostics" in action_labels
    assert "Check for updates" in action_labels
    assert "Quit" in action_labels


def test_tray_toggle_action_calls_controller():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = FakeController()
    overlay = FakeOverlay()
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=overlay,
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )
    menu = tray._context_menu
    toggle_action = next(a for a in menu.actions() if a.text() == "Toggle Dictation")
    toggle_action.trigger()
    assert controller.toggle_calls == 1


def test_tray_show_overlay_action_calls_controller():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = FakeController()
    overlay = FakeOverlay()
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=overlay,
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )
    menu = tray._context_menu
    show_action = next(a for a in menu.actions() if a.text() == "Show overlay")
    show_action.trigger()
    assert controller.bring_overlay_calls == 1


def test_tray_update_checker_shows_message_for_available_update(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(main_module.threading, "Thread", ImmediateThread)
    tray = FakeTrayIcon()
    checker = _TrayUpdateChecker(
        tray_icon=tray,
        runner=lambda: UpdateCheckResult(
            current_version="0.4.1",
            latest_version="0.4.2",
            latest_tag="v0.4.2",
            update_available=True,
        ),
    )

    checker.start()

    assert tray.messages == [
        (
            "Voice Dictation App",
            "Update v0.4.2 is available. Current version: 0.4.1.",
        )
    ]
    _ = app


def test_tray_update_action_runs_manual_check_without_update(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(main_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        main_module,
        "show_update_status_dialog",
        lambda **kwargs: messages.append((kwargs["title"], kwargs["text"])),
    )
    tray = FakeTrayIcon()
    action = QtGui.QAction("Check for updates")
    checker = _TrayUpdateChecker(
        tray_icon=tray,
        runner=lambda: UpdateCheckResult(
            current_version="0.4.1",
            latest_version="0.4.1",
            latest_tag="v0.4.1",
        ),
    )

    checker.start(manual=True, action=action)

    assert action.isEnabled() is True
    assert messages == [
        (
            "You're up to date",
            "Version 0.4.1 is installed. No newer release is available.",
        )
    ]
    assert tray.messages == []
    _ = app


def test_manual_update_request_promotes_active_startup_check(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    pending_targets = []
    dialogs = []

    class DeferredThread:
        def __init__(self, target, **_kwargs):
            pending_targets.append(target)

        def start(self):
            return None

    monkeypatch.setattr(main_module.threading, "Thread", DeferredThread)
    monkeypatch.setattr(
        main_module,
        "show_update_status_dialog",
        lambda **kwargs: dialogs.append((kwargs["title"], kwargs["text"])),
    )
    checker = _TrayUpdateChecker(
        tray_icon=FakeTrayIcon(),
        runner=lambda: UpdateCheckResult(current_version="0.6.0"),
    )
    action = QtGui.QAction("Check for updates")

    checker.start(manual=False)
    checker.start(manual=True, action=action)

    assert len(pending_targets) == 1
    assert action.isEnabled() is False
    pending_targets[0]()
    app.processEvents()

    assert dialogs == [
        (
            "You're up to date",
            "Version 0.6.0 is installed. No newer release is available.",
        )
    ]
    assert action.isEnabled() is True
    _ = app


def test_tray_double_click_connected():
    """Double-clicking the tray icon should be connected to open settings."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = FakeController()
    overlay = FakeOverlay()
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=overlay,
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )
    # The activated signal should have at least one receiver connected.
    sig = QtCore.SIGNAL("activated(QSystemTrayIcon::ActivationReason)")
    assert tray.receivers(sig) > 0


# The interval during which a Trigger after a DoubleClick is dropped. Long next to
# the gap between two emits in a test (`_CLICK_GAP_MS`), so a stall of the
# machine cannot let it expire between them, and the tests wait for it once.
_DOUBLE_CLICK_INTERVAL_MS = 400
_CLICK_GAP_MS = 20


@pytest.fixture
def short_double_click_interval(monkeypatch):
    """The system's double-click time (500 ms by default) shortened for tests."""
    monkeypatch.setattr(
        QtWidgets.QApplication,
        "doubleClickInterval",
        lambda: _DOUBLE_CLICK_INTERVAL_MS,
    )


def _wait_past_the_double_click_interval() -> None:
    """Event loop running for the interval and a quarter more: a timer started
    at the interval has fired by the end, one that was stopped has not."""
    QtTest.QTest.qWait(int(_DOUBLE_CLICK_INTERVAL_MS * 1.25))


def _tray_with_a_recording_settings_dialog(monkeypatch):
    """A tray over a controller that counts `bring_overlay_to_front`, plus the
    settings dialogs it opened (a stand-in that only counts `show`)."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    opened = []

    class FakeSettingsDialog(QtWidgets.QDialog):
        settings_changed = QtCore.Signal()
        provider_keys_changed = QtCore.Signal(list)
        audio_device_refresh_requested = QtCore.Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()
            opened.append(self)

        def show(self):
            pass

        def showNormal(self):
            pass

        def raise_(self):
            pass

        def activateWindow(self):
            pass

        def prepare_for_first_show(self):
            return None

    monkeypatch.setattr(main_module, "SettingsDialog", FakeSettingsDialog)
    controller = FakeController()
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=FakeOverlay(),
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )
    return tray, controller, opened


def test_tray_left_click_reveals_overlay_at_once(
    short_double_click_interval, monkeypatch
):
    """A single left click surfaces the overlay (keyboard-free access) on the
    first click, with no wait for a possible second one: the notification-area
    guidance asks a single click to show something immediately."""
    tray, controller, _opened = _tray_with_a_recording_settings_dialog(monkeypatch)

    tray.activated.emit(QtWidgets.QSystemTrayIcon.Trigger)

    assert controller.bring_overlay_calls == 1
    _ = tray


def test_a_double_click_ends_in_one_click_state_plus_open_settings(
    short_double_click_interval, monkeypatch
):
    """The shell sends one double-click on the icon as Trigger, DoubleClick,
    Trigger (measured 0, 93 and 94 ms with this app's own icon on Windows 11).

    The first Trigger surfaces the overlay as one click does; the DoubleClick
    opens Settings; the trailing Trigger is the second click's release and must
    not surface the overlay again, over the dialog that just opened."""
    tray, controller, opened = _tray_with_a_recording_settings_dialog(monkeypatch)
    reason = QtWidgets.QSystemTrayIcon

    tray.activated.emit(reason.Trigger)
    assert controller.bring_overlay_calls == 1, "the single click acts at once"
    QtTest.QTest.qWait(_CLICK_GAP_MS)
    tray.activated.emit(reason.DoubleClick)
    tray.activated.emit(reason.Trigger)

    assert len(opened) == 1, "the double-click must still open Settings"
    assert controller.bring_overlay_calls == 1, "no second reveal"
    _ = tray


def test_clicks_after_a_double_click_surface_the_overlay_again(
    short_double_click_interval, monkeypatch
):
    """The trailing Trigger is dropped by the clock, not for good."""
    tray, controller, _opened = _tray_with_a_recording_settings_dialog(monkeypatch)
    reason = QtWidgets.QSystemTrayIcon
    tray.activated.emit(reason.DoubleClick)
    tray.activated.emit(reason.Trigger)
    assert controller.bring_overlay_calls == 0

    _wait_past_the_double_click_interval()
    tray.activated.emit(reason.Trigger)
    tray.activated.emit(reason.Trigger)

    assert controller.bring_overlay_calls == 2
    _ = tray


def test_tray_menu_is_registered_with_qt():
    """The menu is Qt's platform menu again.

    Popping it up manually and activating the icon window first were attempts
    at keeping Windows 11's hidden-icons flyout open; both were measured on the
    affected machine and neither changed the flyout's behaviour, so the extra
    machinery was removed. See docs/learning-log.md.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    tray = _create_tray_icon(
        app=app,
        controller=FakeController(),
        overlay=FakeOverlay(),
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )

    assert tray.contextMenu() is tray._context_menu
    _ = tray


def test_tray_middle_click_toggles_dictation_respecting_setting():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = FakeController()
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=FakeOverlay(),
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )

    tray.activated.emit(QtWidgets.QSystemTrayIcon.MiddleClick)
    assert controller.toggle_calls == 1

    # The Display-tab checkbox takes effect without restart: the guard reads
    # live controller settings on every activation.
    controller.settings = AppSettings(tray_middle_click_toggle=False)
    tray.activated.emit(QtWidgets.QSystemTrayIcon.MiddleClick)
    assert controller.toggle_calls == 1
    _ = tray


def test_tray_double_click_presents_settings_dialog(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    instances = []

    class FakeSettingsDialog(QtWidgets.QDialog):
        settings_changed = QtCore.Signal()
        provider_keys_changed = QtCore.Signal(list)
        audio_device_refresh_requested = QtCore.Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.show_calls = 0
            self.raise_calls = 0
            self.activate_calls = 0
            self.set_window_state_calls = 0
            instances.append(self)

        def show(self):
            self.show_calls += 1

        def showNormal(self):
            self.show_calls += 1

        def raise_(self):
            self.raise_calls += 1

        def activateWindow(self):
            self.activate_calls += 1

        def setWindowState(self, state):
            self.set_window_state_calls += 1
            super().setWindowState(state)

        def prepare_for_first_show(self):
            return None

    monkeypatch.setattr(main_module, "SettingsDialog", FakeSettingsDialog)

    tray = _create_tray_icon(
        app=app,
        controller=FakeController(),
        overlay=FakeOverlay(),
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )

    tray.activated.emit(QtWidgets.QSystemTrayIcon.DoubleClick)

    assert len(instances) == 1
    assert instances[0].show_calls == 1
    assert instances[0].raise_calls == 1
    assert instances[0].activate_calls == 1
    assert instances[0].set_window_state_calls == 0

    tray.activated.emit(QtWidgets.QSystemTrayIcon.DoubleClick)

    assert len(instances) == 1
    assert instances[0].show_calls == 2
    assert instances[0].raise_calls == 2
    assert instances[0].activate_calls == 2


def test_settings_dialog_key_changes_reach_the_controller(monkeypatch):
    """The credential signal must actually be wired to the controller.

    A replaced API key never reaches ``AppSettings`` -- ``has_*_key`` only
    flips when a key is added or removed -- so this connection is the only
    thing that stops a runtime from going on using the previous credential.
    The provider names have to travel with it: a blanket invalidation would
    unload a multi-gigabyte local model for a key it never reads.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    instances = []

    class FakeSettingsDialog(QtWidgets.QDialog):
        settings_changed = QtCore.Signal()
        provider_keys_changed = QtCore.Signal(list)
        audio_device_refresh_requested = QtCore.Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()
            instances.append(self)

        def show(self):
            pass

        def showNormal(self):
            pass

        def prepare_for_first_show(self):
            return None

    monkeypatch.setattr(main_module, "SettingsDialog", FakeSettingsDialog)
    controller = FakeController()

    tray = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=FakeOverlay(),
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )
    tray.activated.emit(QtWidgets.QSystemTrayIcon.DoubleClick)
    assert len(instances) == 1

    instances[0].provider_keys_changed.emit(["groq", "openai"])

    assert controller.credentials_invalidated == 1
    assert controller.credentials_providers == ["groq", "openai"]
    _ = tray


def test_tray_reuses_hidden_settings_dialog_and_retains_busy_state(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    instances = []

    class FakeSettingsDialog(QtWidgets.QDialog):
        settings_changed = QtCore.Signal()
        provider_keys_changed = QtCore.Signal(list)
        audio_device_refresh_requested = QtCore.Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.busy_token = object()
            self.reload_calls = 0
            self.shutdown_calls = 0
            instances.append(self)

        def prepare_for_first_show(self):
            return None

        def reload_from_store(self):
            self.reload_calls += 1

        def shutdown(self):
            self.shutdown_calls += 1

    monkeypatch.setattr(main_module, "SettingsDialog", FakeSettingsDialog)

    tray = _create_tray_icon(
        app=app,
        controller=FakeController(),
        overlay=FakeOverlay(),
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )

    first = tray._open_settings_dialog()
    busy_token = first.busy_token
    first.reject()
    app.processEvents()

    second = tray._open_settings_dialog()

    assert second is first
    assert len(instances) == 1
    assert second.busy_token is busy_token
    assert second.reload_calls == 1

    tray._shutdown_settings_dialog()
    assert second.shutdown_calls == 1
    second.close()


def test_tray_prepares_settings_dialog_without_showing(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    instances = []

    class FakeSettingsDialog(QtWidgets.QDialog):
        settings_changed = QtCore.Signal()
        provider_keys_changed = QtCore.Signal(list)
        audio_device_refresh_requested = QtCore.Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.prepare_calls = 0
            self.reload_calls = 0
            self.show_calls = 0
            instances.append(self)

        def prepare_for_first_show(self):
            self.prepare_calls += 1

        def reload_from_store(self):
            self.reload_calls += 1

        def show(self):
            self.show_calls += 1

        def raise_(self):
            return None

        def activateWindow(self):
            return None

    monkeypatch.setattr(main_module, "SettingsDialog", FakeSettingsDialog)
    monkeypatch.setattr(
        main_module.QtCore.QTimer,
        "singleShot",
        lambda _delay_ms, callback: callback(),
    )

    tray = _create_tray_icon(
        app=app,
        controller=FakeController(),
        overlay=FakeOverlay(),
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )

    assert len(instances) == 1
    assert instances[0].prepare_calls == 1
    assert instances[0].show_calls == 0

    tray._open_settings_dialog()

    assert instances[0].reload_calls == 1
    assert instances[0].show_calls == 1


def test_history_presenter_reuses_open_dialog_and_reloads_on_refocus(
    monkeypatch,
    tmp_path,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    instances = []
    callbacks = []

    class FakeHistoryDialog(QtWidgets.QDialog):
        def __init__(self, *args, autoload=True, **kwargs):
            super().__init__()
            self.autoload = autoload
            self.reload_calls = 0
            self.reload_force_values = []
            self.show_calls = 0
            self.raise_calls = 0
            self.activate_calls = 0
            instances.append(self)

        def reload(self, force=False):
            self.reload_calls += 1
            self.reload_force_values.append(force)

        def show(self):
            self.show_calls += 1
            super().show()

        def raise_(self):
            self.raise_calls += 1

        def activateWindow(self):
            self.activate_calls += 1

    monkeypatch.setattr(main_module, "HistoryDialog", FakeHistoryDialog)
    monkeypatch.setattr(
        main_module.QtCore.QTimer,
        "singleShot",
        lambda _delay_ms, callback: callbacks.append(callback),
    )

    presenter = _HistoryDialogPresenter(
        history_store=TranscriptHistoryStore(tmp_path / "history.json"),
        settings_store=FakeSettingsStore(),
        on_history_limit_changed=lambda _value: None,
    )

    first = presenter.open()
    second = presenter.open()

    assert first is second
    assert len(instances) == 1
    assert instances[0].autoload is False
    assert instances[0].show_calls == 2
    assert instances[0].raise_calls == 2
    assert instances[0].activate_calls == 2
    # Re-clicking History refreshes the open dialog exactly once, with the
    # selection/scroll-preserving forced reload.
    assert instances[0].reload_calls == 1
    assert instances[0].reload_force_values == [True]
    assert len(callbacks) == 1

    callbacks[0]()
    presenter.open()

    assert instances[0].reload_calls == 3
    assert instances[0].reload_force_values == [True, False, True]
    assert len(callbacks) == 1
    instances[0].close()
    _ = app


def test_restore_overlay_after_settings_save_applies_corner_and_compacts():
    """Saving settings must not shrink the box around a transcript.

    `ensure_compact_size` pins the detail area back to the compact cap, so
    pressing Save while a finished dictation is on the overlay truncated it
    and left the overlay compact under a `Done` label. The policy call makes
    the same distinction Reset Pos has always made.
    """
    overlay = FakeOverlay()
    store = FakeSettingsStore()

    _restore_overlay_after_settings_save(overlay, store)

    assert overlay.moved_to == "top-right"
    assert overlay.always_on_top_values == [True]
    assert overlay.compact_policy_calls == 1
    assert overlay.compact_calls == 0, "the save path compacts unconditionally again"


def test_restore_after_system_resume_refreshes_hotkeys_and_overlay():
    controller = FakeController()
    overlay = FakeOverlay()

    _restore_after_system_resume(controller, overlay)

    assert controller.resume_calls == 1
    assert controller.hotkey_refresh_calls == 1
    assert overlay.restore_visibility_calls == 1


def test_a_last_recording_already_in_history_is_completed_not_offered(
    monkeypatch, tmp_path
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    state = store.save_recording(b"RIFF", keep_after_success=False)
    store.mark_transcribing(engine="openai", model="whisper-1", mode="batch")

    history_store = TranscriptHistoryStore(path=tmp_path / "history.json")
    history_store.add_entry(
        TranscriptHistoryEntry.new(
            text="done",
            engine="openai",
            model="whisper-1",
            mode="batch",
            source_recording_id=state.recording_id,
        ),
        max_items=20,
    )

    unfinished = UnfinishedRecordingStore(tmp_path / "unfinished")

    dialog = main_module._offer_unfinished_recordings(
        last_recording_store=store,
        history_store=history_store,
        unfinished_store=unfinished,
        transcribe=lambda *_a: (True, ""),
        keep_dir=tmp_path / "recordings",
    )

    assert dialog is None
    assert unfinished.list_recordings() == []
    assert store.has_recoverable_recording() is False
    _ = app


def test_a_transcript_written_soon_after_the_recording_is_no_proof_it_was_transcribed(
    tmp_path,
):
    """D5 of 2026-11-24: the slot is told transcribed by its recording id only.
    The removed guess took any History entry written 0-180 s after the WAV's
    modification time as its transcript, so a recording stopped while the
    previous one's transcript was being written, followed by a crash, was
    completed -- with "keep last recording" off, deleted -- although nobody
    had transcribed it. Now it stays and the notice offers it."""
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    store.save_recording(b"RIFF", keep_after_success=False)
    audio_mtime = 1_800_000_000
    os.utime(store.audio_path, (audio_mtime, audio_mtime))
    history = TranscriptHistoryStore(path=tmp_path / "history.json")
    history.add_entry(
        TranscriptHistoryEntry(
            text="the previous recording's transcript",
            created_at=datetime.fromtimestamp(audio_mtime + 60, UTC).isoformat(),
            engine="local",
            model="small",
            mode="batch",
            source_recording_id="an-older-recording",
        ),
        max_items=20,
    )

    assert _last_recording_already_transcribed(store, history) is False
    assert store.audio_path.is_file()
    assert store.has_recoverable_recording() is True


def test_a_slot_without_a_recording_id_is_kept_whatever_history_holds(tmp_path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    store.save_recording(b"RIFF", keep_after_success=False)
    audio_mtime = 1_800_000_000
    os.utime(store.audio_path, (audio_mtime, audio_mtime))
    history = TranscriptHistoryStore(path=tmp_path / "history.json")
    history.add_entry(
        TranscriptHistoryEntry(
            text="matching legacy transcript",
            created_at=datetime.fromtimestamp(audio_mtime + 60, UTC).isoformat(),
            engine="local",
            model="small",
            mode="batch",
        ),
        max_items=20,
    )
    legacy_state = SimpleNamespace(recording_id="", created_at="")

    assert (
        _last_recording_already_transcribed(store, history, state=legacy_state) is False
    )
    assert store.has_recoverable_recording() is True


def test_load_app_icon_uses_bundled_asset():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    assert app_icon_path().is_file()
    icon = load_app_icon()

    assert icon.isNull() is False
    assert icon.availableSizes()
    _ = app


@pytest.mark.parametrize(
    "reason",
    [
        QtWidgets.QSystemTrayIcon.Context,
        QtWidgets.QSystemTrayIcon.Trigger,
        QtWidgets.QSystemTrayIcon.DoubleClick,
        QtWidgets.QSystemTrayIcon.MiddleClick,
    ],
    ids=["context", "trigger", "double-click", "middle-click"],
)
def test_every_tray_activation_notes_the_foreground_first(reason):
    """The tray menu takes the foreground for our own hidden host window.

    `activated` is emitted before `TrackPopupMenu` runs, which is the last
    moment the user's own window is still in front -- and on a fresh session
    it is the only chance, because nothing has remembered a foreign window
    yet. Without it the first dictation started from the tray menu aimed at
    our 0x0 helper window, and the transcript went nowhere while the overlay
    reported success.

    Every reason is driven, not just `Context`: the handler has two early
    returns below this call, so a version that only tested the context menu
    stayed green when the call was moved below them -- and `MiddleClick`,
    which starts a recording outright, is exactly the reason that needs the
    target most.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = FakeController()
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=FakeOverlay(),
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )

    tray.activated.emit(reason)

    assert controller.note_foreground_calls == 1, (
        "the foreground was not recorded before the menu takes it"
    )
    # A double-click opens the real, shown Settings dialog; left open it keeps
    # the process's focus window for every later test.
    tray._shutdown_settings_dialog()
    for widget in app.topLevelWidgets():
        if widget.isVisible() and isinstance(widget, QtWidgets.QDialog):
            widget.close()


class _OverlaySignals(QtCore.QObject):
    """The overlay's user-action signals and nothing else."""

    record_toggle_requested = QtCore.Signal()
    history_requested = QtCore.Signal()
    edit_requested = QtCore.Signal()
    retry_requested = QtCore.Signal()
    insert_again_requested = QtCore.Signal()
    cancel_requested = QtCore.Signal()
    opacity_changed = QtCore.Signal(int)
    always_on_top_changed = QtCore.Signal(bool)
    language_changed = QtCore.Signal(str)
    microphone_changed = QtCore.Signal(str)
    microphone_menu_requested = QtCore.Signal()
    queue_cancel_requested = QtCore.Signal(int)
    queue_clear_requested = QtCore.Signal()
    detail_cleared = QtCore.Signal(str)


def test_the_overlay_microphone_menu_reaches_the_controller():
    """A pick persists through the controller, and opening the menu asks it
    for today's devices first."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = _OverlaySignals()
    controller = FakeController()

    main_module._connect_overlay_actions(overlay, controller, lambda: None)
    overlay.microphone_menu_requested.emit()
    overlay.microphone_changed.emit("USB Microphone")

    assert controller.microphone_option_refreshes == 1
    assert controller.microphone_picks == ["USB Microphone"]


def test_the_overlay_clear_reaches_the_controller_with_the_cleared_offer():
    """The overlay's Clear retires the Insert offer it dismissed, which needs
    the controller to hear of it: pinned by emitting the signal."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = _OverlaySignals()
    controller = FakeController()

    main_module._connect_overlay_actions(overlay, controller, lambda: None)
    overlay.detail_cleared.emit(" zweiter teil")

    assert controller.detail_cleared_calls == [" zweiter teil"]


def test_the_overlay_insert_action_reaches_the_failed_text_slot_not_the_tray_re_paste():
    """The Error state's Insert pastes what failed; the tray pastes the last transcript.

    Wired to `repaste_last_transcript`, Insert after a failed streaming
    finalize pasted the whole dictation on top of the half already in the
    document. The two slots differ only there, which is why the wiring is
    pinned by emitting the signal rather than by reading `run()`.
    """
    from stt_app.overlay_ui import OverlayUI

    assert set(vars(_OverlaySignals)) >= {
        name
        for name, value in vars(OverlayUI).items()
        if isinstance(value, QtCore.Signal)
    }
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = _OverlaySignals()
    controller = FakeController()
    history_opened = []

    main_module._connect_overlay_actions(
        overlay, controller, lambda: history_opened.append(1)
    )
    overlay.insert_again_requested.emit()
    overlay.record_toggle_requested.emit()
    overlay.history_requested.emit()

    assert controller.insert_failed_calls == 1
    assert controller.repaste_calls == 0
    assert controller.toggle_calls == 1
    assert history_opened == [1]


class _NotificationSignals(QtCore.QObject):
    """The controller's out-of-band report signals and nothing else."""

    background_transcription_failed = QtCore.Signal(str)
    background_insertion_failed = QtCore.Signal(str)
    busy_overlay_error = QtCore.Signal(str)
    clipboard_restore_failed = QtCore.Signal(str)
    node_packages_not_updated = QtCore.Signal(str)


class _TrayMessages:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def showMessage(self, title, message, icon=None, timeout_ms=None):
        self.messages.append((title, message))


def test_packages_that_were_not_updated_reach_the_tray_from_the_runner_thread(
    monkeypatch,
):
    """The update runs where the Node runner starts -- a preload or
    transcription worker -- and its one-time notice must still end up as a
    tray message on the Qt thread."""
    monkeypatch.setattr(local_webgpu_asr, "_NODE_PACKAGES_NOTICE_HANDLER", None)
    monkeypatch.setattr(local_webgpu_asr, "_NODE_PACKAGES_NOTICE_PENDING", "")
    monkeypatch.setattr(local_webgpu_asr, "_NODE_PACKAGES_NOTICE_SENT", False)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    tray = _TrayMessages()
    shown_on: list[threading.Thread] = []
    original_show = tray.showMessage

    def show(*args, **kwargs):
        shown_on.append(threading.current_thread())
        original_show(*args, **kwargs)

    tray.showMessage = show
    controller = _NotificationSignals()
    main_module._connect_tray_notifications(tray, controller)

    retry_after = time.time() + 86400
    worker = threading.Thread(
        target=local_webgpu_asr._notify_previous_packages_kept,
        args=("npm reported an error (code E403)", retry_after),
    )
    worker.start()
    worker.join(timeout=10)
    deadline = time.monotonic() + 5
    while not tray.messages and time.monotonic() < deadline:
        app.processEvents()

    assert tray.messages == [
        (
            local_webgpu_asr.NODE_PACKAGES_NOTICE_TITLE,
            local_webgpu_asr.node_packages_notice_text(
                "npm reported an error (code E403)", retry_after
            ),
        )
    ]
    assert shown_on == [threading.main_thread()]


def test_an_error_the_overlay_cannot_show_reaches_the_tray(monkeypatch):
    """An error raised while a session owns the overlay is a tray message.

    `show_overlay_error` hands such an error to `busy_overlay_error` instead
    of painting over "Listening"; the wiring to the tray is pinned by
    emitting the signal, beside the two reports that already took this road.
    """
    # The wiring also registers a module-level handler; keep it to this test.
    monkeypatch.setattr(local_webgpu_asr, "_NODE_PACKAGES_NOTICE_HANDLER", None)
    monkeypatch.setattr(local_webgpu_asr, "_NODE_PACKAGES_NOTICE_PENDING", "")
    fake_signals = {
        name
        for name, value in vars(_NotificationSignals).items()
        if isinstance(value, QtCore.Signal)
    }
    real_signals = {
        name
        for name, value in vars(DictationController).items()
        if isinstance(value, QtCore.Signal)
    }
    assert fake_signals <= real_signals, fake_signals - real_signals
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    tray = _TrayMessages()
    controller = _NotificationSignals()

    main_module._connect_tray_notifications(tray, controller)
    controller.busy_overlay_error.emit("The overlay opacity was not saved.")
    controller.background_transcription_failed.emit("Recording 12:00:00 failed: x")
    controller.background_insertion_failed.emit("Recording 12:00:00 was not pasted.")
    controller.clipboard_restore_failed.emit("Your clipboard could not be put back.")

    assert tray.messages == [
        (APP_DISPLAY_NAME, "The overlay opacity was not saved."),
        ("Transcription failed", "Recording 12:00:00 failed: x"),
        ("Transcript not inserted", "Recording 12:00:00 was not pasted."),
        ("Clipboard not restored", "Your clipboard could not be put back."),
    ]


def test_a_transcript_with_a_gap_does_not_complete_its_recording_at_startup(
    tmp_path,
):
    """The controller keeps the recording of a transcript with a gap marker
    (marked failed), and the startup check must not undo that: it finds the
    transcript in history, and completing the recording there deletes the
    audio with `keep_after_success` off. No prompt either -- the transcript
    exists; the audio stays reachable from History and Import."""
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    state = store.save_recording(b"RIFF", keep_after_success=False)
    store.mark_failed("Part of the recording returned no text; the recording was kept.")
    history = TranscriptHistoryStore(path=tmp_path / "history.json")
    history.add_entry(
        TranscriptHistoryEntry.new(
            text="erster teil [no text returned for 3:00-6:00] rest",
            engine="openai",
            model="gpt-transcribe",
            mode="batch",
            source_recording_id=state.recording_id,
        ),
        max_items=20,
    )

    assert _last_recording_already_transcribed(store, history) is True
    assert store.audio_path.is_file()
    assert store.has_recoverable_recording() is True


def test_quit_watchdog_arms_a_native_dump_and_exit(monkeypatch, tmp_path):
    """A quit that never finishes must end anyway, with the stacks recorded.

    The owner's quit once left a process with no tray icon running until
    Ctrl+C. Ctrl+C cannot reach a thread blocked in native code, and Python
    code (a daemon thread) cannot run once finalization froze the other
    threads, so the guard has to be faulthandler's native timer. It writes
    into the session's crash log, never a second handle on `dictation.log`,
    which would make a rotation during the quit fail.
    """
    armed = []
    monkeypatch.setattr(
        main_module.faulthandler,
        "dump_traceback_later",
        lambda timeout, **kwargs: armed.append(
            (timeout, kwargs.get("repeat"), kwargs.get("file"), kwargs.get("exit"))
        ),
    )
    with (tmp_path / "crash.log").open("a", encoding="utf-8") as stream:
        crash_log = main_module.CrashLog(stream)
        messages = []
        logger = SimpleNamespace(
            warning=lambda msg, *args: messages.append(msg % args),
            info=lambda msg, *args: messages.append(msg % args),
        )

        main_module._arm_quit_watchdog(crash_log, logger)

    assert len(armed) == 1
    timeout, repeat, file, exit_ = armed[0]
    assert timeout == main_module.QUIT_WATCHDOG_TIMEOUT_S
    assert repeat is False
    assert exit_ is True, "the watchdog must end the process, not only report"
    assert file is stream
    assert any("app_quit_started" in m for m in messages)
    # A Windows shutdown may end the process right after this: the next
    # start must see that the quit had begun.
    assert "quit started" in (tmp_path / "crash.log").read_text(encoding="utf-8")


def test_tray_quit_asks_while_a_transcription_is_pending(monkeypatch):
    """The tray's Quit no longer ends the app under a running transcription;
    `app.quit` -- which arms the quit watchdog -- waits for the user."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    quits = []
    monkeypatch.setattr(
        QtWidgets.QApplication, "quit", staticmethod(lambda: quits.append(True))
    )
    controller = FakeController()
    controller.pending_quit_work = PendingQuitWork(transcribing=1)
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=FakeOverlay(),
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )
    quit_action = next(a for a in tray._context_menu.actions() if a.text() == "Quit")

    quit_action.trigger()

    assert quits == []
    dialog = tray._quit_coordinator.dialog
    assert dialog is not None and dialog.isVisible()
    dialog.quit_now_button.click()
    assert quits == [True]


def _wait_for_the_event_loop(condition, timeout_s: float = 5.0) -> bool:
    """Run the Qt event loop until ``condition()`` holds; False after the bound.

    PySide6's QTest has no `qWaitFor`. A fixed `qWait` against a short timer
    fails on a loaded machine that delays the timer.
    """
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() >= deadline:
            return False
        QtTest.QTest.qWait(5)
    return True


def test_the_settings_prebuild_waits_while_a_dictation_runs(monkeypatch):
    """The Settings dialog is built ahead on the Qt thread 2.5 s after start,
    which took 2.4-3.3 s in the owner's log; a dictation started right after
    launch (one did, 1 s after it) had its stop press, result and paste wait
    behind it. The build now waits until no recording or transcription runs."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    built = []

    class CountingSettingsDialog(QtWidgets.QDialog):
        settings_changed = QtCore.Signal()
        provider_keys_changed = QtCore.Signal(list)
        audio_device_refresh_requested = QtCore.Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()
            built.append(self)

        def prepare_for_first_show(self):
            return None

    monkeypatch.setattr(main_module, "SettingsDialog", CountingSettingsDialog)
    monkeypatch.setattr(main_module, "_SETTINGS_PREBUILD_DELAY_MS", 10)
    controller = FakeController()
    controller.session_active = True
    checks = []

    def has_active_session():
        checks.append(True)
        return controller.session_active

    controller.has_active_session = has_active_session
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        overlay=FakeOverlay(),
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
        last_recording_store=FakeLastRecordingStore(),
        open_history_dialog=lambda: None,
    )

    # Not a fixed wait against the 10 ms timer: wait until the build step has
    # run and put itself off three times (a loaded machine delays timers, so a
    # sleep of a few timer periods proved nothing), bounded generously.
    assert _wait_for_the_event_loop(lambda: len(checks) >= 3)
    assert built == [], "built while the dictation ran"
    controller.session_active = False
    assert _wait_for_the_event_loop(lambda: len(built) == 1)
    QtTest.QTest.qWait(50)
    assert len(built) == 1, "built once"
    _ = tray


def test_outdated_packages_reach_the_tray_from_the_worker_thread(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    stale = [main_module.dependency_check.Outdated("assemblyai", "1.6.1", "1.10.11")]
    worker_threads = []

    def fake_check():
        worker_threads.append(threading.current_thread())
        return stale, "uv sync"

    monkeypatch.setattr(
        main_module.dependency_check, "check_checkout_packages", fake_check
    )
    shown = []
    tray = SimpleNamespace(
        isVisible=lambda: True, showMessage=lambda *args: shown.append(args)
    )

    main_module._start_dependency_check(tray)
    deadline = time.monotonic() + 5
    while not shown and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert len(shown) == 1
    title, text = shown[0][:2]
    assert title == "Update the Python packages"
    assert "assemblyai" in text and "uv sync" in text
    assert worker_threads and worker_threads[0] is not threading.main_thread()


def test_an_up_to_date_checkout_shows_no_package_notice(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    done = threading.Event()

    def fake_check():
        done.set()

    monkeypatch.setattr(
        main_module.dependency_check, "check_checkout_packages", fake_check
    )
    shown = []
    tray = SimpleNamespace(
        isVisible=lambda: True, showMessage=lambda *args: shown.append(args)
    )

    main_module._start_dependency_check(tray)
    assert done.wait(5)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.01)

    assert shown == []


@pytest.fixture(autouse=True)
def _fresh_start_notice_clock(monkeypatch):
    monkeypatch.setattr(main_module, "_start_notice_not_before", 0.0)


def test_two_start_notices_are_never_sent_closer_than_the_spacing(monkeypatch):
    timers = []
    clock = [1000.0]
    monkeypatch.setattr(main_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        main_module,
        "QtCore",
        SimpleNamespace(
            QTimer=SimpleNamespace(
                singleShot=lambda delay, callback: timers.append((delay, callback))
            )
        ),
    )
    shown = []
    tray = SimpleNamespace(
        isVisible=lambda: True, showMessage=lambda *args: shown.append(args[:2])
    )

    main_module._show_tray_notice_when_ready(tray, "Update the Python packages", "a")
    main_module._show_model_notice_when_ready(tray, "b")

    # Same tick: the first goes out, the second is held for the spacing.
    assert shown == [("Update the Python packages", "a")]
    assert len(timers) == 1
    delay_ms, callback = timers.pop()
    assert delay_ms >= main_module._START_NOTICE_SPACING_S * 1000

    callback()  # fired too early (clock unchanged): held again, not sent
    assert len(shown) == 1 and len(timers) == 1
    timers.pop()

    clock[0] += main_module._START_NOTICE_SPACING_S + 0.1
    callback()
    assert shown[-1] == ("Speech model updated", "b")
    assert timers == []


def test_the_update_notice_keeps_the_spacing_after_the_model_notice(monkeypatch):
    """The update check's balloon is a start notice too: sent right after the
    model notice it would replace it."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    timers = []
    clock = [1000.0]
    monkeypatch.setattr(main_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(main_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        main_module.QtCore.QTimer,
        "singleShot",
        lambda delay, callback: timers.append((delay, callback)),
    )
    tray = FakeTrayIcon()
    checker = _TrayUpdateChecker(
        tray_icon=tray,
        runner=lambda: UpdateCheckResult(
            current_version="0.4.1",
            latest_version="0.4.2",
            latest_tag="v0.4.2",
            update_available=True,
        ),
    )

    main_module._show_model_notice_when_ready(tray, "model moved")
    checker.start()

    assert [title for title, _text in tray.messages] == ["Speech model updated"]
    assert len(timers) == 1
    delay_ms, callback = timers.pop()
    assert delay_ms >= main_module._START_NOTICE_SPACING_S * 1000

    clock[0] += main_module._START_NOTICE_SPACING_S + 0.1
    callback()

    assert [title for title, _text in tray.messages] == [
        "Speech model updated",
        "Voice Dictation App",
    ]
    _ = app


def test_the_model_notice_waits_for_the_tray_icon(monkeypatch):
    """The tray drops a balloon sent before the shell accepted the icon, and the
    move the notice announces is already saved, so it must be held back."""
    timers = []
    monkeypatch.setattr(
        main_module,
        "QtCore",
        SimpleNamespace(
            QTimer=SimpleNamespace(
                singleShot=lambda delay, callback: timers.append((delay, callback))
            )
        ),
    )
    shown = []
    tray = SimpleNamespace(
        visible=False,
        isVisible=lambda: tray.visible,
        showMessage=lambda *args: shown.append(args),
    )

    main_module._show_model_notice_when_ready(tray, "notice")
    assert shown == []
    assert len(timers) == 1

    # Still not registered after a retry: held back again, not sent.
    timers.pop()[1]()
    assert shown == []
    assert len(timers) == 1

    tray.visible = True
    timers.pop()[1]()
    assert len(shown) == 1 and shown[0][1] == "notice"
    assert timers == []


def test_the_model_notice_is_sent_after_the_last_wait_even_without_an_icon(
    monkeypatch,
):
    timers = []
    monkeypatch.setattr(
        main_module,
        "QtCore",
        SimpleNamespace(
            QTimer=SimpleNamespace(
                singleShot=lambda delay, callback: timers.append((delay, callback))
            )
        ),
    )
    shown = []
    tray = SimpleNamespace(
        isVisible=lambda: False, showMessage=lambda *args: shown.append(args)
    )

    main_module._show_model_notice_when_ready(
        tray, "notice", waited=main_module._MODEL_NOTICE_MAX_WAITS
    )

    assert len(shown) == 1
    assert timers == []


def test_a_moved_model_choice_is_shown_as_one_tray_message():
    shown = []
    tray = SimpleNamespace(showMessage=lambda *args: shown.append(args))

    main_module._show_model_notice(tray, "OpenAI: whisper-1 is no longer offered.")

    assert len(shown) == 1
    title, text, icon, _timeout = shown[0]
    assert title == "Speech model updated"
    assert text == "OpenAI: whisper-1 is no longer offered."
    assert icon == QtWidgets.QSystemTrayIcon.Information


def test_a_local_retirement_notice_is_shown_through_the_one_model_notice_path():
    """Local and remote retirements share one pending-moves path and the one
    "Speech model updated" tray notice (`_show_model_notice`)."""
    shown = []

    class _Tray:
        def showMessage(self, title, text, icon, timeout):
            shown.append((title, text))

    main_module._show_model_notice(
        _Tray(),
        "Local (on this PC): base is no longer offered, your choice is now small.",
    )

    assert shown == [
        (
            "Speech model updated",
            "Local (on this PC): base is no longer offered, your choice is now small.",
        )
    ]


def test_the_model_notice_is_cleared_only_after_the_balloon_was_shown(monkeypatch):
    """The pending move stays in the settings file while the tray icon is not
    there yet, so a start closed in the meantime announces it at the next one."""
    timers = []
    monkeypatch.setattr(
        main_module,
        "QtCore",
        SimpleNamespace(
            QTimer=SimpleNamespace(
                singleShot=lambda delay, callback: timers.append((delay, callback))
            )
        ),
    )
    events = []
    tray = SimpleNamespace(
        visible=False,
        isVisible=lambda: tray.visible,
        showMessage=lambda *args: events.append("shown"),
    )

    main_module._show_model_notice_when_ready(
        tray, "notice", on_shown=lambda: events.append("cleared")
    )
    assert events == []

    tray.visible = True
    timers.pop()[1]()
    assert events == ["shown", "cleared"]


def test_the_pending_notice_is_written_for_the_disk_as_it_is_at_display_time(
    monkeypatch,
):
    moves = [
        {
            "engine": "local",
            "from": "granite-speech-4.1-2b",
            "to": "granite-speech-5.0-2b",
            "extra": "",
        }
    ]
    settings = SimpleNamespace(
        pending_model_moves=moves, model_dir="", offline_mode=False
    )
    store = SimpleNamespace(load=lambda: settings)
    cached: list[str] = []
    monkeypatch.setattr(
        "stt_app.transcriber.local_faster_whisper.find_cached_models",
        lambda model_dir="": list(cached),
    )

    def text() -> str:
        return "\n".join(t for t, _ in main_module._pending_model_notices(store))

    assert "Downloading it now (~1.73 GB)" in text()
    settings.offline_mode = True
    assert "Offline mode is on" in text()
    cached.append("granite-speech-5.0-2b")
    assert text().endswith("2B.")


def test_two_model_notices_are_shown_one_after_the_other_and_cleared_one_by_one(
    monkeypatch,
):
    """A local and a cloud notice that do not fit one balloon are two. The second
    waits for the spacing between start notices, and each move is cleared only
    once its own balloon was handed to the tray."""
    timers = []
    clock = [1000.0]
    monkeypatch.setattr(main_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(main_module, "_start_notice_not_before", 0.0)
    monkeypatch.setattr(
        main_module,
        "QtCore",
        SimpleNamespace(
            QTimer=SimpleNamespace(
                singleShot=lambda delay, callback: timers.append((delay, callback))
            )
        ),
    )
    events: list = []
    tray = SimpleNamespace(
        isVisible=lambda: True,
        showMessage=lambda title, text, *args: events.append(("shown", text)),
    )
    local = {"engine": "local", "from": "a", "to": "b", "extra": ""}
    cloud = {"engine": "openai", "from": "c", "to": "d", "extra": ""}

    main_module._show_model_notices_when_ready(
        tray,
        [("local text", [local]), ("cloud text", [cloud])],
        lambda moves: events.append(("cleared", [m["engine"] for m in moves])),
    )

    assert events == [("shown", "local text"), ("cleared", ["local"])]
    delay_ms, callback = timers.pop()
    assert delay_ms >= main_module._START_NOTICE_SPACING_S * 1000
    clock[0] += main_module._START_NOTICE_SPACING_S + 0.1
    callback()
    assert events[2:] == [("shown", "cloud text"), ("cleared", ["openai"])]
    assert timers == []
