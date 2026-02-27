from PySide6 import QtGui, QtTest, QtWidgets

from tts_app.config import OVERLAY_HEIGHT, OVERLAY_MAX_HEIGHT
from tts_app.overlay_ui import OverlayUI


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
