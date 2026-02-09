import logging

from PySide6 import QtCore, QtGui

from tts_app.config import DEFAULT_HOTKEY, FALLBACK_HOTKEY
from tts_app.controller import DictationController
from tts_app.settings_store import AppSettings
from tts_app.text_inserter import TextInsertionError


class FakeSettingsStore:
    def __init__(self, settings):
        self._settings = settings
        self.saved = None

    def load(self):
        return self._settings

    def save(self, settings):
        self.saved = settings


class FakeHotkeyManager:
    def __init__(self):
        self.calls = []

    def register(self, hotkey):
        self.calls.append(hotkey)
        if hotkey != FALLBACK_HOTKEY:
            raise ValueError("blocked")

    def unregister(self):
        pass


class FakeHotkeyManagerAllFail(FakeHotkeyManager):
    def register(self, hotkey):
        self.calls.append(hotkey)
        raise ValueError("blocked")


class FakeOverlay:
    def __init__(self):
        self.states = []

    def set_state(self, state, detail=""):
        self.states.append((state, detail))


class FakeTextInserter:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.calls = []

    def insert_text(self, text, target_hwnd=None):
        self.calls.append((text, target_hwnd))
        if self.should_fail:
            raise TextInsertionError("failed insert")
        return True


class FakeWindowFocusHelper:
    def __init__(self):
        self.captured = 987
        self.restore_calls = []

    def capture_target_window(self):
        return self.captured

    def restore_target_window(self, hwnd):
        self.restore_calls.append(hwnd)
        return True


def test_controller_falls_back_to_safe_hotkey():
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    settings = AppSettings(hotkey=DEFAULT_HOTKEY)
    store = FakeSettingsStore(settings)
    hotkey_manager = FakeHotkeyManager()
    overlay = FakeOverlay()

    controller = DictationController(
        settings_store=store,
        hotkey_manager=hotkey_manager,
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
    )

    controller.reload_settings(re_register_hotkey=True)
    controller.show_idle_status()

    assert hotkey_manager.calls[0] == DEFAULT_HOTKEY
    assert hotkey_manager.calls[1] == FALLBACK_HOTKEY
    assert store.saved is not None
    assert store.saved.hotkey == FALLBACK_HOTKEY
    assert any("Using fallback" in detail for _state, detail in overlay.states)

    controller.shutdown()
    _ = app


def test_controller_shows_error_when_all_hotkey_registration_fails():
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    settings = AppSettings(hotkey=DEFAULT_HOTKEY)
    store = FakeSettingsStore(settings)
    hotkey_manager = FakeHotkeyManagerAllFail()
    overlay = FakeOverlay()

    controller = DictationController(
        settings_store=store,
        hotkey_manager=hotkey_manager,
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
    )

    controller.reload_settings(re_register_hotkey=True)
    controller.show_idle_status()

    assert overlay.states
    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "Hotkey registration failed" in detail

    controller.shutdown()
    _ = app


def test_controller_restores_target_focus_before_insert():
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY)
    store = FakeSettingsStore(settings)
    hotkey_manager = FakeHotkeyManager()
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    focus_helper = FakeWindowFocusHelper()

    controller = DictationController(
        settings_store=store,
        hotkey_manager=hotkey_manager,
        overlay=overlay,
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )

    controller._target_window_handle = 555
    controller._on_transcription_ready("hello world")

    assert focus_helper.restore_calls == [555]
    assert inserter.calls == [("hello world", 555)]

    controller.shutdown()
    _ = app


class FakeClipboard:
    def __init__(self):
        self.value = ""

    def setText(self, text):
        self.value = text

    def text(self):
        return self.value


def test_controller_copies_transcript_on_insert_error(monkeypatch):
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY)
    store = FakeSettingsStore(settings)
    hotkey_manager = FakeHotkeyManager()
    overlay = FakeOverlay()
    inserter = FakeTextInserter(should_fail=True)
    focus_helper = FakeWindowFocusHelper()

    controller = DictationController(
        settings_store=store,
        hotkey_manager=hotkey_manager,
        overlay=overlay,
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    controller._target_window_handle = 555
    controller._on_transcription_ready("copy me")

    assert fake_clipboard.text() == "copy me"
    assert overlay.states[-1][0] == "Error"
    assert "Transcript copied to clipboard." in overlay.states[-1][1]

    controller.shutdown()
    _ = app


def test_copy_last_transcript_returns_false_when_empty(monkeypatch):
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY)
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    assert controller.copy_last_transcript_to_clipboard() is False

    controller._last_transcript = "latest text"
    assert controller.copy_last_transcript_to_clipboard() is True
    assert fake_clipboard.text() == "latest text"

    controller.shutdown()
    _ = app
