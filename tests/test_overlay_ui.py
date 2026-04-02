from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from stt_app.config import (
    OVERLAY_HEIGHT,
    OVERLAY_INITIAL_DETAIL,
    OVERLAY_MARGIN_X,
    OVERLAY_MARGIN_Y,
    OVERLAY_MAX_HEIGHT,
)
from stt_app.overlay_ui import OverlayUI


class _FakeScreen:
    def __init__(self, geometry: QtCore.QRect):
        self._geometry = geometry

    def availableGeometry(self) -> QtCore.QRect:
        return self._geometry


class FakeClipboard:
    def __init__(self):
        self.value = ""

    def setText(self, text: str):
        self.value = text

    def text(self) -> str:
        return self.value


def test_overlay_copy_button_copies_detail_text(monkeypatch):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    overlay = OverlayUI()
    overlay.set_state("Done", "transcribed text")
    overlay._copy_button.click()

    assert fake_clipboard.text() == "transcribed text"
    assert overlay._copy_button.text() == "Copied"

    QtTest.QTest.qWait(900)
    assert overlay._copy_button.text() == "Copy"


def test_overlay_copy_button_stays_functional_after_repeated_clicks(monkeypatch):
    """Ensure the copy button remains clickable after multiple uses."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    overlay = OverlayUI()
    overlay.set_state("Done", "first text")

    overlay._copy_button.click()
    assert fake_clipboard.text() == "first text"
    assert overlay._copy_button.isEnabled()

    # Wait for feedback reset
    QtTest.QTest.qWait(900)
    assert overlay._copy_button.text() == "Copy"

    # Update text and click again
    overlay.set_state("Done", "second text")
    overlay._copy_button.click()
    assert fake_clipboard.text() == "second text"
    assert overlay._copy_button.text() == "Copied"
    assert overlay._copy_button.isEnabled()


def test_overlay_copy_button_survives_clipboard_error(monkeypatch):
    """If clipboard.setText() raises, the button must not freeze."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class FailingClipboard:
        def setText(self, text: str):
            raise RuntimeError("clipboard locked")

        def text(self) -> str:
            return ""

    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", FailingClipboard)

    overlay = OverlayUI()
    overlay.set_state("Done", "some text")
    overlay._copy_button.click()

    # Button should stay enabled and show "Copy" (not "Copied")
    assert overlay._copy_button.isEnabled()
    assert overlay._copy_button.text() == "Copy"


def test_overlay_copy_button_disabled_when_detail_empty():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()

    overlay.set_state("Idle", "")

    assert overlay._copy_button.isEnabled() is False
    assert overlay._clear_button.isEnabled() is False


def test_overlay_clear_button_enabled_for_done_text_only():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()

    overlay.set_state("Listening", "Speak now.")
    assert overlay._clear_button.isEnabled() is False

    overlay.set_state("Done", "transcribed text")
    assert overlay._clear_button.isEnabled() is True


def test_overlay_clear_button_restores_initial_hint_and_resets_compact_height():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    initial_height = overlay.height()
    overlay.set_state("Done", "word " * 900)
    large_height = overlay.height()
    assert large_height <= OVERLAY_MAX_HEIGHT

    overlay._clear_button.click()

    assert overlay._state_label.text() == "Idle"
    assert overlay._detail_label.text() == OVERLAY_INITIAL_DETAIL
    assert overlay._copy_button.isEnabled() is True
    assert overlay._clear_button.isEnabled() is False
    assert overlay.height() == initial_height
    assert overlay.height() < large_height


def test_overlay_clear_button_restores_last_idle_detail_text():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.set_state("Idle", "Hotkey: Ctrl+Shift+Space | Cancel: Ctrl+Shift+Esc")
    overlay.set_state("Done", "transcribed text")

    overlay._clear_button.click()

    assert overlay._state_label.text() == "Idle"
    assert (
        overlay._detail_label.text()
        == "Hotkey: Ctrl+Shift+Space | Cancel: Ctrl+Shift+Esc"
    )


def test_overlay_grows_for_long_text_but_caps_at_max_height():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    short_height = overlay.height()
    assert short_height >= OVERLAY_HEIGHT

    long_text = "word " * 800
    overlay.set_state("Done", long_text)

    assert overlay.height() > short_height
    assert overlay.height() <= OVERLAY_MAX_HEIGHT
    assert overlay._detail_scroll.verticalScrollBar().maximum() > 0


def test_overlay_has_native_event_override():
    """OverlayUI should override nativeEvent for single-click copy on Windows."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()

    # Verify the method exists and is callable on the subclass
    assert hasattr(overlay, "nativeEvent")
    # nativeEvent should be overridden, not just inherited
    assert type(overlay).nativeEvent is not QtWidgets.QWidget.nativeEvent


def test_overlay_has_show_event_override():
    """OverlayUI should override showEvent to set WS_EX_NOACTIVATE on Windows."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()

    # Verify the method exists and is overridden
    assert hasattr(overlay, "showEvent")
    assert type(overlay).showEvent is not QtWidgets.QWidget.showEvent
    assert hasattr(overlay, "_apply_noactivate_style")
    assert callable(overlay._apply_noactivate_style)


def test_overlay_control_buttons_follow_state():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()

    overlay.set_state("Idle", "ready")
    assert overlay._retry_button.isEnabled() is False
    assert overlay._cancel_button.isEnabled() is False

    overlay.set_state("Error", "failed")
    assert overlay._retry_button.isEnabled() is True
    assert overlay._cancel_button.isEnabled() is False

    overlay.set_state("Listening", "active")
    assert overlay._retry_button.isEnabled() is False
    assert overlay._cancel_button.isEnabled() is True


def test_overlay_control_signals_are_emitted():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    got = {"history": 0, "retry": 0, "cancel": 0}
    overlay.history_requested.connect(lambda: got.__setitem__("history", got["history"] + 1))
    overlay.retry_requested.connect(lambda: got.__setitem__("retry", got["retry"] + 1))
    overlay.cancel_requested.connect(lambda: got.__setitem__("cancel", got["cancel"] + 1))

    overlay.set_state("Error", "failed")
    overlay._history_button.click()
    overlay._retry_button.click()
    overlay.set_state("Listening", "active")
    overlay._cancel_button.click()

    assert got == {"history": 1, "retry": 1, "cancel": 1}


def test_overlay_shrinks_after_long_transcription():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    initial_height = overlay.height()

    overlay.set_state("Done", "word " * 900)
    large_height = overlay.height()
    assert large_height <= OVERLAY_MAX_HEIGHT

    overlay.set_state("Listening", "Speak now.")
    assert overlay.height() == initial_height
    assert overlay.height() < large_height
    assert overlay.height() >= OVERLAY_HEIGHT


def test_overlay_reset_position_preserves_expanded_result_size():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    initial_size = overlay.size()
    overlay.set_state("Done", "word " * 900)
    expanded_size = overlay.size()
    assert expanded_size.height() > initial_size.height()
    overlay.set_initial_position(QtCore.QPoint(120, 80))
    overlay.move(340, 260)

    overlay.reset_position()

    assert overlay.pos() == QtCore.QPoint(120, 80)
    assert overlay.size() == expanded_size


def test_overlay_processing_restores_initial_height_after_long_text():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    initial_height = overlay.height()

    overlay.set_state("Done", "word " * 900)
    assert overlay.height() > initial_height

    overlay.set_state("Processing", "Retrying transcription...")

    assert overlay.height() == initial_height


def test_overlay_reset_position_uses_current_screen_corner(monkeypatch):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    first_screen = _FakeScreen(QtCore.QRect(0, 0, 800, 600))
    second_screen = _FakeScreen(QtCore.QRect(1000, 0, 800, 600))
    overlay.move_to_corner("top-right", screen=first_screen)
    overlay.move(1180, 220)
    monkeypatch.setattr(overlay, "_current_screen", lambda: second_screen)

    overlay.reset_position()

    expected_x = (
        second_screen.availableGeometry().right() - overlay.width() - OVERLAY_MARGIN_X
    )
    expected_y = second_screen.availableGeometry().top() + OVERLAY_MARGIN_Y
    assert overlay.pos() == QtCore.QPoint(expected_x, expected_y)


def test_overlay_bottom_corner_resize_stays_within_current_screen(monkeypatch):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    screen = _FakeScreen(QtCore.QRect(0, 0, 460, 260))
    monkeypatch.setattr(overlay, "_current_screen", lambda: screen)
    overlay.move_to_corner("bottom-right", screen=screen)

    overlay.set_state("Done", "word " * 900)

    assert overlay.frameGeometry().bottom() <= screen.availableGeometry().bottom()
    assert overlay.frameGeometry().right() <= screen.availableGeometry().right()


def test_overlay_opacity_slider_emits_clamped_values():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    emitted: list[int] = []
    overlay.opacity_changed.connect(emitted.append)

    overlay.set_opacity_percent(5, emit_signal=False)
    assert round(overlay.windowOpacity() * 100) == 25

    overlay._opacity_slider.setValue(80)
    assert emitted[-1] == 80
    assert round(overlay.windowOpacity() * 100) == 80
