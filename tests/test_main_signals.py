import os
import signal
from datetime import datetime, timezone
from types import SimpleNamespace

from PySide6 import QtCore, QtGui, QtWidgets

import stt_app.main as main_module
from stt_app.app_icon import app_icon_path, load_app_icon
from stt_app.last_recording_store import LastRecordingStore
from stt_app.main import (
    _HistoryDialogPresenter,
    _TrayUpdateChecker,
    _create_tray_icon,
    _refresh_local_model_inventory_in_background,
    _install_signal_handlers,
    _last_recording_already_transcribed,
    _prompt_recoverable_last_recording,
    _restore_after_system_resume,
    _restore_overlay_after_settings_save,
)
from stt_app.settings_store import AppSettings
from stt_app.transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore
from stt_app.update_checker import UpdateCheckResult


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
        self.settings = AppSettings()

    def toggle_recording(self):
        self.toggle_calls += 1

    def bring_overlay_to_front(self):
        self.bring_overlay_calls += 1

    def repaste_last_transcript(self):
        self.repaste_calls += 1

    def reload_settings(self, re_register_hotkey=True):
        pass

    def show_idle_status(self):
        pass

    def copy_last_transcript_to_clipboard(self):
        return bool(self._last_transcript)

    def on_settings_changed(self):
        self.settings_changed_calls += 1

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
        self.always_on_top_values = []
        self.restore_visibility_calls = 0

    def apply_corner_setting(self, corner):
        self.moved_to = corner

    def set_always_on_top(self, value):
        self.always_on_top_values.append(bool(value))

    def ensure_compact_size(self):
        self.compact_calls += 1

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
    menu = tray.contextMenu()
    action_labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert "Toggle Dictation" in action_labels
    assert "Show overlay" in action_labels
    assert "Settings" in action_labels
    assert "History" in action_labels
    assert "Retry last transcription" in action_labels
    assert "Cancel current action" in action_labels
    assert "Copy last transcript" in action_labels
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
    menu = tray.contextMenu()
    toggle_action = [a for a in menu.actions() if a.text() == "Toggle Dictation"][0]
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
    menu = tray.contextMenu()
    show_action = [a for a in menu.actions() if a.text() == "Show overlay"][0]
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


def test_tray_reuses_hidden_settings_dialog_and_retains_busy_state(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    instances = []

    class FakeSettingsDialog(QtWidgets.QDialog):
        settings_changed = QtCore.Signal()
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
    _ = app


def test_restore_overlay_after_settings_save_applies_corner_and_compacts():
    overlay = FakeOverlay()
    store = FakeSettingsStore()

    _restore_overlay_after_settings_save(overlay, store)

    assert overlay.moved_to == "top-right"
    assert overlay.always_on_top_values == [True]
    assert overlay.compact_calls == 1


def test_restore_after_system_resume_refreshes_hotkeys_and_overlay():
    controller = FakeController()
    overlay = FakeOverlay()

    _restore_after_system_resume(controller, overlay)

    assert controller.resume_calls == 1
    assert controller.hotkey_refresh_calls == 1
    assert overlay.restore_visibility_calls == 1


def test_prompt_recoverable_last_recording_opens_settings(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    store.save_recording(b"RIFF", keep_after_success=False)
    store.mark_failed("network")

    prompts = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda _parent, _title, text, *_args, **_kwargs: (
            prompts.append(text) or QtWidgets.QMessageBox.Yes
        ),
    )

    opened = []

    class _FakeDialog:
        def prepare_last_recording_import(self):
            opened.append(True)

    _prompt_recoverable_last_recording(store, lambda: _FakeDialog())

    assert opened == [True]
    assert "Settings -> Import Audio" in prompts[0]
    _ = app


def test_prompt_recoverable_last_recording_skips_completed_history_match(
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

    asked = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: asked.append(True) or QtWidgets.QMessageBox.Yes,
    )

    _prompt_recoverable_last_recording(store, lambda: None, history_store)

    assert asked == []
    assert store.has_recoverable_recording() is False
    _ = app


def test_legacy_recording_match_checks_past_newer_history_entry(tmp_path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    store.save_recording(b"RIFF", keep_after_success=False)
    audio_mtime = 1_800_000_000
    os.utime(store.audio_path, (audio_mtime, audio_mtime))
    history = TranscriptHistoryStore(path=tmp_path / "history.json")
    matching_time = datetime.fromtimestamp(audio_mtime + 60, timezone.utc).isoformat()
    newer_time = datetime.fromtimestamp(audio_mtime + 600, timezone.utc).isoformat()
    history.add_entry(
        TranscriptHistoryEntry(
            text="matching legacy transcript",
            created_at=matching_time,
            engine="local",
            model="small",
            mode="batch",
        ),
        max_items=20,
    )
    history.add_entry(
        TranscriptHistoryEntry(
            text="newer unrelated transcript",
            created_at=newer_time,
            engine="local",
            model="small",
            mode="batch",
        ),
        max_items=20,
    )
    legacy_state = SimpleNamespace(recording_id="", created_at="")

    assert _last_recording_already_transcribed(
        store,
        history,
        state=legacy_state,
    ) is True
    assert store.has_recoverable_recording() is False


def test_load_app_icon_uses_bundled_asset():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    assert app_icon_path().is_file()
    icon = load_app_icon()

    assert icon.isNull() is False
    assert icon.availableSizes()
    _ = app
