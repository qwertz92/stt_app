import signal

from PySide6 import QtCore, QtWidgets

from stt_app.last_recording_store import LastRecordingStore
from stt_app.main import (
    _create_tray_icon,
    _install_signal_handlers,
    _prompt_recoverable_last_recording,
    _restore_overlay_after_settings_save,
)
from stt_app.settings_store import AppSettings


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

    def toggle_recording(self):
        self.toggle_calls += 1

    def reload_settings(self, re_register_hotkey=True):
        pass

    def show_idle_status(self):
        pass

    def copy_last_transcript_to_clipboard(self):
        return bool(self._last_transcript)

    def on_settings_changed(self):
        self.settings_changed_calls += 1

    def retry_last_transcription(self):
        return True

    def cancel_current_action(self):
        return None

    def shutdown(self):
        pass


class FakeSettingsStore:
    def load(self):
        return AppSettings()

    def save(self, s):
        pass


class FakeSecretStore:
    def get_api_key(self, name):
        return ""


class FakeAppLogger:
    def diagnostics_text(self):
        return "log output"


class FakeOverlay:
    def __init__(self):
        self.moved_to = None
        self.compact_calls = 0

    def move_to_corner(self, corner):
        self.moved_to = corner

    def ensure_compact_size(self):
        self.compact_calls += 1


class FakeLastRecordingStore:
    def has_recoverable_recording(self):
        return False

    def selectable_path(self):
        return None

    def load(self):
        return None


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
    assert "Settings" in action_labels
    assert "History" in action_labels
    assert "Retry last transcription" in action_labels
    assert "Cancel current action" in action_labels
    assert "Copy last transcript" in action_labels
    assert "Copy diagnostics" in action_labels
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


def test_restore_overlay_after_settings_save_repositions_and_compacts():
    overlay = FakeOverlay()
    store = FakeSettingsStore()

    _restore_overlay_after_settings_save(overlay, store)

    assert overlay.moved_to == "top-right"
    assert overlay.compact_calls == 1


def test_prompt_recoverable_last_recording_opens_settings(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    store.save_recording(b"RIFF", keep_after_success=False)
    store.mark_failed("network")

    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: QtWidgets.QMessageBox.Yes,
    )

    opened = []

    class _FakeDialog:
        def prepare_last_recording_import(self):
            opened.append(True)

    _prompt_recoverable_last_recording(store, lambda: _FakeDialog())

    assert opened == [True]
    _ = app
