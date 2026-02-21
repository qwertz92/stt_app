import signal

from PySide6 import QtWidgets

from tts_app.main import _install_signal_handlers, _create_tray_icon


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

    def toggle_recording(self):
        self.toggle_calls += 1

    def reload_settings(self, re_register_hotkey=True):
        pass

    def show_idle_status(self):
        pass

    def copy_last_transcript_to_clipboard(self):
        return bool(self._last_transcript)

    def shutdown(self):
        pass


class FakeSettingsStore:
    def load(self):
        return None

    def save(self, s):
        pass


class FakeSecretStore:
    def get_api_key(self, name):
        return ""


class FakeAppLogger:
    def diagnostics_text(self):
        return "log output"


def test_create_tray_icon_has_expected_menu_actions():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = FakeController()
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
    )
    menu = tray.contextMenu()
    action_labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert "Toggle Dictation" in action_labels
    assert "Settings" in action_labels
    assert "Copy last transcript" in action_labels
    assert "Copy diagnostics" in action_labels
    assert "Quit" in action_labels


def test_tray_toggle_action_calls_controller():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = FakeController()
    tray = _create_tray_icon(
        app=app,
        controller=controller,
        settings_store=FakeSettingsStore(),
        secret_store=FakeSecretStore(),
        app_logger=FakeAppLogger(),
    )
    menu = tray.contextMenu()
    toggle_action = [a for a in menu.actions() if a.text() == "Toggle Dictation"][0]
    toggle_action.trigger()
    assert controller.toggle_calls == 1