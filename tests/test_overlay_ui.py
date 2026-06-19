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

    QtTest.QTest.qWait(1100)
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
    QtTest.QTest.qWait(1100)
    assert overlay._copy_button.text() == "Copy"

    # Update text and click again
    overlay.set_state("Done", "second text")
    overlay._copy_button.click()
    assert fake_clipboard.text() == "second text"
    assert overlay._copy_button.text() == "Copied"
    assert overlay._copy_button.isEnabled()


def test_overlay_queue_panel_renders_and_emits_signals():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()

    # Empty queue hides the panel.
    overlay.set_transcription_queue([])
    assert overlay._queue_visible is False
    assert overlay._queue_widget.isHidden() is True

    canceled: list[int] = []
    cleared: list[bool] = []
    overlay.queue_cancel_requested.connect(canceled.append)
    overlay.queue_clear_requested.connect(lambda: cleared.append(True))

    overlay.set_transcription_queue([(7, "local · small"), (8, "groq · whisper")])
    assert overlay._queue_visible is True
    assert overlay._queue_widget.isHidden() is False
    assert overlay._queue_rows_layout.count() == 2

    first_row = overlay._queue_rows_layout.itemAt(0).widget()
    first_row.findChild(QtWidgets.QPushButton).click()
    assert canceled == [7]

    overlay._queue_clear_button.click()
    assert cleared == [True]

    # Emptying again hides the panel.
    overlay.set_transcription_queue([])
    assert overlay._queue_visible is False
    assert overlay._queue_widget.isHidden() is True


def test_overlay_queue_panel_caps_visible_rows():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()

    items = [(token, f"local · {token}") for token in range(6)]
    overlay.set_transcription_queue(items)

    # Four rows with cancel buttons plus a single "+N more" summary label.
    assert overlay._queue_rows_layout.count() == 5
    summary = overlay._queue_rows_layout.itemAt(4).widget()
    assert "more" in summary.text()


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
    assert overlay._edit_button.isEnabled() is False

    overlay.set_state("Done", "transcribed text")
    assert overlay._clear_button.isEnabled() is True
    assert overlay._edit_button.isEnabled() is True


def test_overlay_edit_button_emits_request():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    emitted = []
    overlay.edit_requested.connect(lambda: emitted.append(True))

    overlay.set_state("Done", "transcribed text")
    overlay._edit_button.click()

    assert emitted == [True]


def test_overlay_clear_button_restores_initial_hint_and_resets_compact_height():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    initial_height = overlay.height()
    initial_width = overlay.width()
    overlay.set_state("Done", "word " * 900)
    large_height = overlay.height()
    assert large_height <= OVERLAY_MAX_HEIGHT

    overlay._clear_button.click()
    QtTest.QTest.qWait(1)

    assert overlay._state_label.text() == "Idle"
    assert overlay._detail_label.text() == OVERLAY_INITIAL_DETAIL
    assert overlay._copy_button.isEnabled() is True
    assert overlay._clear_button.isEnabled() is False
    assert overlay.height() == initial_height
    assert overlay.width() == initial_width
    assert overlay.height() < large_height


def test_overlay_restore_visibility_reasserts_foreground_mode():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()
    overlay.set_always_on_top(False)

    overlay.hide()
    overlay.restore_visibility()

    assert overlay.isVisible() is True
    assert bool(overlay.windowFlags() & QtCore.Qt.WindowStaysOnTopHint)


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


def test_overlay_always_on_top_toggle_updates_state_and_signal():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()
    emitted: list[bool] = []
    overlay.always_on_top_changed.connect(emitted.append)

    assert overlay.always_on_top is True
    assert bool(overlay.windowFlags() & QtCore.Qt.WindowStaysOnTopHint)

    overlay._always_on_top_button.click()
    app.processEvents()

    assert overlay.always_on_top is False
    assert emitted == [False]
    assert overlay._always_on_top_button.text() == "Floating"
    assert not bool(overlay.windowFlags() & QtCore.Qt.WindowStaysOnTopHint)


def test_overlay_reveal_temporarily_restores_non_pinned_mode():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()
    overlay.set_always_on_top(False)

    overlay.reveal_temporarily(duration_ms=50)

    assert bool(overlay.windowFlags() & QtCore.Qt.WindowStaysOnTopHint)
    QtTest.QTest.qWait(80)
    app.processEvents()
    assert overlay.always_on_top is False
    assert not bool(overlay.windowFlags() & QtCore.Qt.WindowStaysOnTopHint)


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
    requested_position = QtCore.QPoint(120, 80)
    overlay.set_initial_position(requested_position)
    overlay.move(340, 260)

    overlay.reset_position()

    expected_position = QtCore.QPoint(requested_position)
    screen = QtGui.QGuiApplication.screenAt(requested_position)
    if screen is None:
        screen = overlay._current_screen()
    if screen is not None:
        expected_position = overlay._clamp_point_to_screen(expected_position, screen)

    assert overlay.pos() == expected_position
    assert overlay.size() == expanded_size


def test_overlay_screen_change_normalizes_runaway_width(monkeypatch):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    screen = _FakeScreen(QtCore.QRect(0, 0, 1400, 900))
    monkeypatch.setattr(overlay, "_current_screen", lambda: screen)

    overlay.resize(32767, overlay.height())
    overlay._on_screen_changed(screen)

    assert overlay.width() == overlay._target_window_width()
    assert overlay.frameGeometry().right() <= screen.availableGeometry().right()


def test_overlay_reset_position_normalizes_runaway_width(monkeypatch):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    screen = _FakeScreen(QtCore.QRect(0, 0, 1400, 900))
    monkeypatch.setattr(overlay, "_current_screen", lambda: screen)
    overlay.set_state("Done", "word " * 900)
    overlay.set_initial_position(QtCore.QPoint(120, 80))
    overlay.resize(32767, overlay.height())

    overlay.reset_position()

    assert overlay.width() == overlay._target_window_width()
    assert overlay.pos().x() >= screen.availableGeometry().left()


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


def test_overlay_apply_corner_setting_keeps_dragged_position(monkeypatch):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    screen = _FakeScreen(QtCore.QRect(0, 0, 800, 600))
    monkeypatch.setattr(overlay, "_current_screen", lambda: screen)
    overlay.move_to_corner("top-right", screen=screen)
    dragged = QtCore.QPoint(120, 220)
    overlay.move(dragged)
    overlay._manual_positioned = True

    overlay.apply_corner_setting("top-right")

    assert overlay.pos() == dragged


def test_overlay_apply_corner_setting_moves_when_corner_changes(monkeypatch):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    screen = _FakeScreen(QtCore.QRect(0, 0, 800, 600))
    monkeypatch.setattr(overlay, "_current_screen", lambda: screen)
    overlay.move_to_corner("top-right", screen=screen)
    overlay.move(120, 220)
    overlay._manual_positioned = True

    overlay.apply_corner_setting("top-left")

    expected = QtCore.QPoint(
        screen.availableGeometry().left() + OVERLAY_MARGIN_X,
        screen.availableGeometry().top() + OVERLAY_MARGIN_Y,
    )
    assert overlay.pos() == expected


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
