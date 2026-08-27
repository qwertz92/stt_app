import logging

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

import stt_app.overlay_ui as overlay_ui_module
from stt_app.config import (
    OVERLAY_ERROR_ACTION_INSERT,
    OVERLAY_ERROR_ACTION_NONE,
    OVERLAY_HEIGHT,
    OVERLAY_INITIAL_DETAIL,
    OVERLAY_MARGIN_X,
    OVERLAY_MARGIN_Y,
    OVERLAY_MAX_HEIGHT,
    OVERLAY_QUEUE_MAX_HEIGHT,
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
    cancel_button = first_row.findChild(QtWidgets.QPushButton)
    assert cancel_button.text() == "Cancel"
    assert "Cancel this transcription" in cancel_button.toolTip()
    cancel_button.click()
    assert canceled == [7]

    overlay._queue_clear_button.click()
    assert cleared == [True]

    # Emptying again hides the panel.
    overlay.set_transcription_queue([])
    assert overlay._queue_visible is False
    assert overlay._queue_widget.isHidden() is True


def test_overlay_queue_panel_renders_all_rows(monkeypatch):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    screen = _FakeScreen(QtCore.QRect(0, 0, 1400, 900))
    monkeypatch.setattr(overlay, "_current_screen", lambda: screen)

    items = [(token, f"local · {token}") for token in range(18)]
    overlay.set_transcription_queue(items)

    assert overlay._queue_rows_layout.count() == len(items)
    assert overlay.height() > OVERLAY_MAX_HEIGHT
    last_row = overlay._queue_rows_layout.itemAt(len(items) - 1).widget()
    assert last_row is not None
    assert last_row.isHidden() is False


def test_overlay_queue_height_resets_after_queue_finishes():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    initial_size = overlay.size()

    overlay.set_state("Processing", "Transcribing audio...", compact=False)
    processing_size = overlay.size()
    overlay.set_transcription_queue([(7, "local · small"), (8, "groq · whisper")])
    queued_height = overlay.height()
    assert queued_height > processing_size.height()

    overlay.set_state("Listening", "Speak now.", compact=True)
    assert overlay.height() > initial_size.height()

    overlay.set_state("Processing", "Transcribing audio...", compact=False)
    overlay.set_transcription_queue([])
    assert overlay.size() == processing_size

    overlay.set_state("Listening", "Speak now.", compact=True)
    assert overlay.size() == initial_size


def test_overlay_queue_scrolls_and_stays_bounded_with_many_rows(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    screen = _FakeScreen(QtCore.QRect(0, 0, 1400, 1000))
    monkeypatch.setattr(overlay, "_current_screen", lambda: screen)
    overlay.show()
    app.processEvents()

    overlay.set_state("Processing", "Transcribing audio...", compact=False)
    overlay.set_transcription_queue(
        [(i, f"#{i} - 12:00:00 - local - whisper-large-v3 model") for i in range(24)]
    )
    for _ in range(3):
        app.processEvents()

    # All rows exist, the panel scrolls, and the window stays bounded (does not
    # grow to full screen height like it used to).
    assert overlay._queue_rows_layout.count() == 24
    assert overlay._queue_scroll.verticalScrollBar().maximum() > 0
    assert overlay.height() <= OVERLAY_QUEUE_MAX_HEIGHT + 8
    assert overlay.height() < screen.availableGeometry().height()
    overlay.hide()


@pytest.mark.pixel_exact
def test_overlay_resets_size_after_queue_finishes_with_short_result(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    screen = _FakeScreen(QtCore.QRect(0, 0, 1400, 1000))
    monkeypatch.setattr(overlay, "_current_screen", lambda: screen)
    overlay.show()
    app.processEvents()
    initial_height = overlay.height()

    overlay.set_state("Processing", "Transcribing audio...", compact=False)
    overlay.set_transcription_queue([(i, f"#{i} file") for i in range(16)])
    for _ in range(3):
        app.processEvents()
    assert overlay.height() > initial_height  # grew for the queue

    # The last queued item finishes: the queue clears and a short result shows.
    # The overlay must return to its original compact size, not stay large.
    overlay.set_transcription_queue([])
    overlay.set_state("Done", "ok")
    for _ in range(3):
        app.processEvents()

    assert abs(overlay.height() - initial_height) <= 8
    overlay.hide()


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


def test_overlay_restore_visibility_reasserts_foreground_mode(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()
    overlay.set_always_on_top(False)
    z_order_calls: list[bool] = []
    monkeypatch.setattr(overlay_ui_module.sys, "platform", "win32")
    monkeypatch.setattr(
        overlay,
        "_apply_native_z_order",
        lambda: z_order_calls.append(overlay._temporary_foreground_active) or True,
    )

    overlay.hide()
    overlay.restore_visibility()

    assert overlay.isVisible() is True
    assert overlay._temporary_foreground_active is True
    assert z_order_calls == [True]
    assert not bool(overlay.windowFlags() & QtCore.Qt.WindowStaysOnTopHint)


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


def test_overlay_does_not_reapply_stylesheet_for_same_state():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class CountingOverlay(OverlayUI):
        def __init__(self):
            self.stylesheet_calls = 0
            super().__init__()

        def setStyleSheet(self, stylesheet: str) -> None:
            self.stylesheet_calls += 1
            super().setStyleSheet(stylesheet)

    overlay = CountingOverlay()
    initial_calls = overlay.stylesheet_calls

    overlay.set_state("Listening", "First", compact=True)
    overlay.set_state("Listening", "Second", compact=True)

    assert overlay.stylesheet_calls == initial_calls + 1


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


def test_overlay_initial_window_flags_are_not_reapplied(monkeypatch):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    reapplied = []
    monkeypatch.setattr(overlay, "setWindowFlags", reapplied.append)

    overlay._apply_window_flags()

    assert reapplied == []


def test_overlay_reveal_temporarily_does_not_rebuild_non_pinned_window(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()
    overlay.set_always_on_top(False)
    rebuilt_flags: list[QtCore.Qt.WindowType] = []
    monkeypatch.setattr(overlay_ui_module.sys, "platform", "win32")
    monkeypatch.setattr(overlay, "_apply_native_z_order", lambda: True)
    monkeypatch.setattr(overlay, "setWindowFlags", rebuilt_flags.append)

    overlay.reveal_temporarily(duration_ms=50)

    assert overlay._temporary_foreground_active is True
    assert rebuilt_flags == []
    assert not bool(overlay.windowFlags() & QtCore.Qt.WindowStaysOnTopHint)
    QtTest.QTest.qWait(80)
    app.processEvents()
    assert overlay.always_on_top is False
    assert overlay._temporary_foreground_active is False
    assert rebuilt_flags == []


def test_overlay_reveal_falls_back_to_temporary_topmost_flag(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()
    overlay.set_always_on_top(False)
    monkeypatch.setattr(overlay_ui_module.sys, "platform", "win32")
    monkeypatch.setattr(overlay, "_apply_native_z_order", lambda: False)

    overlay.reveal_temporarily(duration_ms=50)

    assert overlay._temporary_foreground_uses_window_flag is True
    assert bool(overlay.windowFlags() & QtCore.Qt.WindowStaysOnTopHint)
    QtTest.QTest.qWait(80)
    app.processEvents()
    assert overlay._temporary_foreground_uses_window_flag is False
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


def test_overlay_record_button_toggles_caption_and_emits():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    emitted: list[int] = []
    overlay.record_toggle_requested.connect(lambda: emitted.append(1))

    assert overlay._record_button.text() == overlay_ui_module.RECORD_BUTTON_START_TEXT

    overlay.set_state("Listening", "Speak now.")
    assert overlay._record_button.text() == overlay_ui_module.RECORD_BUTTON_STOP_TEXT
    assert overlay._record_button.isEnabled() is True

    overlay._record_button.click()
    overlay.set_state("Processing", "Transcribing audio...")

    assert emitted == [1]
    assert overlay._record_button.text() == overlay_ui_module.RECORD_BUTTON_START_TEXT


def test_overlay_cancel_and_retry_share_one_slot_without_resizing():
    """Only the applicable action is shown, and the row width stays constant."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()

    overlay.set_state("Listening", "Speak now.")
    listening_width = overlay._controls_widget.sizeHint().width()
    assert overlay._cancel_button.isHidden() is False
    assert overlay._retry_button.isHidden() is True
    assert overlay._cancel_button.isEnabled() is True

    overlay.set_state("Error", "Transcription failed.")
    assert overlay._cancel_button.isHidden() is True
    assert overlay._retry_button.isHidden() is False
    assert overlay._retry_button.isEnabled() is True
    assert overlay._controls_widget.sizeHint().width() == listening_width

    overlay.set_state("Done", "transcribed text")
    assert overlay._cancel_button.isHidden() is False
    assert overlay._cancel_button.isEnabled() is False
    assert overlay._controls_widget.sizeHint().width() == listening_width

    # A failed insertion has no failed transcription to retry, so the slot
    # offers inserting the transcript again instead.
    overlay.set_state(
        "Error",
        "Insertion failed.",
        error_action=OVERLAY_ERROR_ACTION_INSERT,
    )
    assert overlay._insert_button.isHidden() is False
    assert overlay._insert_button.isEnabled() is True
    assert overlay._retry_button.isHidden() is True
    assert overlay._cancel_button.isHidden() is True
    assert overlay._controls_widget.sizeHint().width() == listening_width

    inserts: list[int] = []
    overlay.insert_again_requested.connect(lambda: inserts.append(1))
    overlay._insert_button.click()
    assert inserts == [1]


def test_overlay_copy_uses_copy_text_override(monkeypatch):
    """An error detail may embed the transcript; Copy must yield only it."""
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()

    overlay.set_state(
        "Error",
        "Insertion failed.\n\ntranscribed text",
        copy_text="transcribed text",
    )
    overlay._copy_button.click()

    assert fake_clipboard.text() == "transcribed text"

    overlay.set_state("Done", "plain transcript")
    overlay._copy_button.click()

    assert fake_clipboard.text() == "plain transcript"


def test_batched_update_resizes_once_instead_of_shrinking_and_growing():
    """Finishing a transcription must be one visual step, not two.

    The queue row is cleared before the transcript is published; applied
    separately the overlay shrinks for the empty queue and grows again for the
    text, which reads as a stutter and shows the old content at the new size.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()

    sizes: list[int] = []
    overlay.resizeEvent = (  # type: ignore[method-assign]
        lambda event, _base=overlay.resizeEvent: (
            sizes.append(event.size().height()),
            _base(event),
        )[1]
    )

    overlay.set_state("Processing", "Transcribing audio...")
    overlay.set_transcription_queue([(1, "#1 - local - cohere")])
    for _ in range(4):
        app.processEvents()
    queued_height = overlay.height()
    sizes.clear()

    with overlay.batched_update():
        overlay.set_transcription_queue([])
        overlay.set_state("Done", "word " * 200)
    for _ in range(4):
        app.processEvents()

    assert overlay.height() != queued_height
    assert sizes == [overlay.height()]
    overlay.hide()


@pytest.mark.pixel_exact
def test_overlay_error_after_long_transcript_restores_compact_height():
    """A short error must not inherit the expanded transcript height.

    The window's minimum size is only refreshed when the layout is activated,
    so an expanded transcript used to keep the overlay large while the state
    switched to Error (typically an insertion failure).
    """
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    initial_height = overlay.height()

    overlay.set_state("Done", "word " * 900)
    expanded_height = overlay.height()
    assert expanded_height > initial_height

    overlay.set_state("Error", "Insertion failed: target window rejected paste.")

    assert overlay.height() == initial_height
    assert overlay.height() < expanded_height


def test_overlay_never_exceeds_max_height_including_container_border():
    """The styled container's border must be part of the height budget."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()

    overlay.set_state("Done", "word " * 900)

    assert overlay.height() <= OVERLAY_MAX_HEIGHT
    assert overlay.height() >= overlay.minimumSizeHint().height()


@pytest.mark.pixel_exact
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


@pytest.mark.pixel_exact
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


@pytest.mark.pixel_exact
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


@pytest.mark.pixel_exact
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


def test_overlay_language_button_selects_supported_language():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    emitted: list[str] = []
    overlay.language_changed.connect(emitted.append)

    overlay.set_language_options(("auto", "de", "en"), "de")

    assert overlay._language_button.text() == "Lang: German"
    assert overlay._language_button.isEnabled() is True
    german_action = next(
        action for action in overlay._language_menu.actions()
        if action.text() == "German"
    )
    german_action.trigger()
    assert next(
        action for action in overlay._language_menu.actions()
        if action.text() == "German"
    ).isChecked()
    english_action = next(
        action for action in overlay._language_menu.actions()
        if action.text() == "English"
    )
    english_action.trigger()

    assert emitted == ["en"]
    assert overlay._language_button.text() == "Lang: English"


@pytest.mark.pixel_exact
def test_overlay_language_button_draws_centered_chevron_and_opens_menu(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.set_language_options(("auto", "de", "en"), "de")
    overlay.show()
    app.processEvents()
    button = overlay._language_button
    arrow_rect = button._menu_arrow_rect()
    popup_positions: list[QtCore.QPoint] = []
    monkeypatch.setattr(overlay._language_menu, "popup", popup_positions.append)
    image = QtGui.QImage(button.size(), QtGui.QImage.Format_ARGB32)
    image.fill(QtCore.Qt.transparent)
    button.render(image)
    arrow_pixels = [
        QtCore.QPoint(x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y) == button._ARROW_COLOR
    ]

    button.click()

    assert button.menu() is None
    assert arrow_pixels
    assert all(arrow_rect.contains(point) for point in arrow_pixels)
    assert arrow_rect.center().y() == button.contentsRect().center().y()
    assert abs(
        min(point.y() for point in arrow_pixels)
        + max(point.y() for point in arrow_pixels)
        - 2 * arrow_rect.center().y()
    ) <= 1
    assert popup_positions == [
        button.mapToGlobal(QtCore.QPoint(0, button.height()))
    ]


@pytest.mark.pixel_exact
def test_overlay_record_button_indicator_stays_centered_in_both_states():
    """The state indicator is painted, not typed.

    "●"/"■" sit on the font baseline: the dot rendered 1.5 px below the
    button's middle, the square 1 px, and the indicator jumped between states
    because the two glyphs have different heights.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()
    button = overlay._record_button

    boxes: list[tuple[float, int, int]] = []
    for state in ("Idle", "Listening"):
        overlay.set_state(state, "detail")
        app.processEvents()
        image = QtGui.QImage(button.size(), QtGui.QImage.Format_ARGB32)
        image.fill(QtCore.Qt.transparent)
        button.render(image)
        points = [
            QtCore.QPoint(x, y)
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y) == button._COLOR
        ]

        assert points, state
        ys = [point.y() for point in points]
        xs = [point.x() for point in points]
        boxes.append(
            ((min(ys) + max(ys)) / 2, max(ys) - min(ys), max(xs) - min(xs))
        )

    # Vertically centred in both states...
    assert boxes[0][0] == (button.height() - 1) / 2
    assert boxes[1][0] == (button.height() - 1) / 2
    # ...and the same size, so the state swap cannot resize the indicator.
    assert boxes[0][1] == boxes[1][1]
    assert boxes[0][2] == boxes[1][2]
    overlay.hide()


def test_overlay_language_button_shows_fixed_auto_and_blocks_active_changes():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    emitted: list[str] = []
    overlay.language_changed.connect(emitted.append)

    overlay.set_language_options(("auto",), "de")

    assert overlay._language_button.text() == "Lang: Auto"
    assert overlay._language_button.isEnabled() is False

    overlay.set_language_options(("auto", "de"), "auto")
    overlay.set_state("Listening", "Recording...")
    overlay._select_language("de")

    assert emitted == []
    assert overlay._language_button.text() == "Lang: Auto"
    assert overlay._language_button.isEnabled() is False


@pytest.mark.pixel_exact
def test_dragging_claims_the_manual_position_on_the_first_movement(monkeypatch):
    """A drag must own the position immediately, not only on mouse release.

    Startup keeps updating the overlay (preload progress, "Model loaded",
    the idle status), and each of those repositions a not-yet-manual overlay
    back to its configured corner. With the claim deferred to the release,
    the window jumped out from under the cursor mid-drag.
    """
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    screen = _FakeScreen(QtCore.QRect(0, 0, 1920, 1080))
    monkeypatch.setattr(overlay, "_current_screen", lambda: screen)
    overlay.move_to_corner("top-right", screen=screen)
    assert overlay._manual_positioned is False

    overlay._drag_active = True
    overlay._drag_offset = QtCore.QPoint(0, 0)
    overlay.mouseMoveEvent(
        QtGui.QMouseEvent(
            QtCore.QEvent.MouseMove,
            QtCore.QPointF(400.0, 300.0),
            QtCore.QPointF(400.0, 300.0),
            QtCore.Qt.LeftButton,
            QtCore.Qt.LeftButton,
            QtCore.Qt.NoModifier,
        )
    )

    assert overlay._manual_positioned is True
    assert overlay.pos() == QtCore.QPoint(400, 300)


@pytest.mark.pixel_exact
def test_startup_updates_do_not_move_an_overlay_being_dragged(monkeypatch):
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    screen = _FakeScreen(QtCore.QRect(0, 0, 1920, 1080))
    monkeypatch.setattr(overlay, "_current_screen", lambda: screen)
    overlay.move_to_corner("top-right", screen=screen)
    dragged = QtCore.QPoint(300, 400)
    overlay.move(dragged)
    overlay._drag_active = True

    # A startup overlay update runs this while the button is still held.
    overlay._reposition_within_current_screen()

    assert overlay.pos() == dragged


@pytest.mark.pixel_exact
def test_compact_states_never_clip_their_detail_text():
    """Idle/Listening/Processing used to pin the detail area to the minimum
    height, which silently clipped anything longer than two lines. The startup
    hotkey notice — the text that explains a fallback binding — was exactly
    such a case, and could only be read by scrolling a two-line box."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()

    long_notice = (
        "Hotkey: Ctrl+Alt+F9 - 'Ctrl+Alt+Space' is used by another program; "
        "taken back automatically once it is free. | Cancel: Ctrl+Alt+F12 | "
        "Overlay: Ctrl+Alt+F11 | Re-paste: Ctrl+Alt+F10"
    )
    try:
        for state in ("Idle", "Listening", "Processing"):
            overlay.set_state(state, long_notice)
            app.processEvents()
            needed = overlay._detail_label.sizeHint().height()
            shown = overlay._detail_scroll.height()
            assert needed <= shown, (
                f"{state}: detail clipped, needs {needed}px but shows {shown}px"
            )
    finally:
        overlay.close()


@pytest.mark.pixel_exact
def test_a_short_compact_status_keeps_the_compact_size():
    """Growing to fit must not make the ordinary idle overlay bigger."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()
    try:
        overlay.set_state("Idle", "Idle")
        app.processEvents()
        short_height = overlay.height()

        overlay.set_state("Idle", "Hotkey: Ctrl+Alt+Space")
        app.processEvents()
        assert overlay.height() == short_height
    finally:
        overlay.close()

@pytest.mark.pixel_exact
def test_the_compact_baseline_never_absorbs_the_grow_to_fit_overflow(monkeypatch):
    """The baseline must be the structural height, at every font size.

    Compact states grow to fit. If the baseline is *measured* after any
    state has been applied, whatever that state needed beyond
    OVERLAY_DETAIL_MIN_HEIGHT is baked in -- and the height update then adds
    the same overflow again, so every compact state renders too tall.
    Measuring a deliberately short line only moves the threshold: the
    minimum is a fixed 42 px, so at the larger sizes Windows offers under
    Accessibility > Text size even "Ready." overflows it.

    Two independent checks, because either alone can pass while the bug
    stands: the baseline must equal the structural height (no absorption),
    and it must not depend on how long the initial detail happens to be.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    original_font = QtGui.QFont(app.font())
    long_detail = (
        "Hotkey: Ctrl+Alt+F9 - Ctrl+Alt+Space is used by another program; "
        "taken back automatically once it is free. | Cancel: Ctrl+Alt+F12 | "
        "Overlay: Ctrl+Alt+F11 | Re-paste: Ctrl+Alt+F10"
    )

    def _build(initial_detail, point_size):
        font = QtGui.QFont(original_font)
        font.setPointSize(point_size)
        app.setFont(font)
        monkeypatch.setattr(
            overlay_ui_module, "OVERLAY_INITIAL_DETAIL", initial_detail
        )
        overlay = OverlayUI()
        overlay.show()
        app.processEvents()
        return overlay

    try:
        # 24 pt matters: below it even a long first line happens to fit, so
        # a test that stops at 16 pt passes while the defect is present.
        for point_size in (9, 12, 16, 20, 24):
            for initial_detail in ("Ready.", long_detail):
                overlay = _build(initial_detail, point_size)
                try:
                    baseline = overlay._initial_compact_size.height()
                    structural = overlay._compact_window_height()
                    assert baseline == structural, (
                        f"{point_size}pt: the baseline is {baseline}px "
                        f"against a {structural}px structural height, so it "
                        "absorbed the first line's overflow"
                    )
                finally:
                    overlay.close()
    finally:
        app.setFont(original_font)


def test_the_no_action_error_state_shows_neither_retry_nor_insert():
    """"No action" needs its own value; None is not it.

    The slot treats anything that is not Insert as Retry, so passing None gave
    the user a Retry button on a transcript that had already been inserted --
    and Retry re-transcribes the last *failed* recording, which may be an
    entirely different one, pasting it on top of what is already there.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.show()
    app.processEvents()
    try:
        overlay.set_state(
            "Error",
            "Inserted, but the clipboard could not be restored.",
            error_action=OVERLAY_ERROR_ACTION_NONE,
        )
        app.processEvents()
        assert overlay._retry_button.isHidden(), "Retry would re-transcribe"
        assert overlay._insert_button.isHidden(), "Insert would paste it twice"

        # The other two error shapes still offer their action.
        overlay.set_state("Error", "boom", error_action=OVERLAY_ERROR_ACTION_INSERT)
        app.processEvents()
        assert not overlay._insert_button.isHidden()

        overlay.set_state("Error", "boom")
        app.processEvents()
        assert not overlay._retry_button.isHidden()
    finally:
        overlay.close()


def _painted_text_span(label: QtWidgets.QLabel) -> tuple[int, int]:
    """Bounding x-range of the label's rendered glyphs, in label coordinates.

    `DrawChildren` alone, because `render()`'s default flags include
    `DrawWindowBackground`, which fills the whole region with an opaque
    palette brush. With the default flags every pixel passes the alpha test
    and this returns `(0, width - 1)` for any content -- so it would measure
    the label's rectangle, not its text, and could not tell a centred label
    from a left-aligned one (measured: both `(0, 111)`).
    """
    image = QtGui.QImage(label.size(), QtGui.QImage.Format_ARGB32)
    image.fill(QtCore.Qt.transparent)
    label.render(
        image,
        QtCore.QPoint(),
        QtGui.QRegion(),
        QtWidgets.QWidget.RenderFlags(QtWidgets.QWidget.DrawChildren),
    )
    xs = [
        x
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 24
    ]
    assert xs, f"the state label painted nothing for {label.text()!r}"
    assert len(xs) < image.width() * image.height(), (
        "every pixel is opaque, so this is measuring the label rectangle "
        "rather than its glyphs"
    )
    return min(xs), max(xs)


def _painted_status_centre(overlay: OverlayUI) -> float:
    """Centre x of the painted status text, in overlay coordinates."""
    label = overlay._state_label
    left, right = _painted_text_span(label)
    origin = label.mapTo(overlay, QtCore.QPoint(0, 0)).x()
    # Pixel indices [left, right] cover the continuous span [left, right + 1).
    return origin + (left + right + 1) / 2.0


def _overlay_centre(overlay: OverlayUI) -> float:
    container = overlay._container
    origin = container.mapTo(overlay, QtCore.QPoint(0, 0)).x()
    return origin + container.width() / 2.0


def _state_label_box(overlay: OverlayUI) -> tuple[int, int, int, int]:
    label = overlay._state_label
    origin = label.mapTo(overlay, QtCore.QPoint(0, 0))
    return (origin.x(), origin.y(), label.width(), label.height())


@pytest.mark.pixel_exact
def test_the_status_text_is_centred_on_the_overlay_in_every_state():
    """The status word sits on the overlay's centre line, and never moves.

    The header is [Record][Pinned] <state label> [Clear][Copy] and the label
    is its only stretching item, so Qt hands it the span the four fixed-width
    buttons leave over and ``AlignCenter`` centres the text in *that span*.
    While the two button groups differed -- 78 + 6 + 74 = 158 px on the left
    against 64 + 6 + 64 = 134 px on the right -- that span's midpoint was
    12 px right of the header's, so every status word rendered 12 px off
    centre (7 px before the 78 px Record button replaced the 68 px History
    button as the first item).
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    overlay.set_language_options(("auto", "de", "en"), "de")
    overlay.show()
    app.processEvents()
    record = overlay._record_button
    hover = QtGui.QEnterEvent(
        QtCore.QPointF(record.rect().center()),
        QtCore.QPointF(record.rect().center()),
        QtCore.QPointF(record.mapToGlobal(record.rect().center())),
    )

    cases = [
        ("Idle", lambda: overlay.set_state("Idle", OVERLAY_INITIAL_DETAIL)),
        ("Listening", lambda: overlay.set_state("Listening", "Speak now.")),
        ("Processing", lambda: overlay.set_state("Processing", "Transcribing...")),
        ("Done, short", lambda: overlay.set_state("Done", "short result")),
        ("Done, long", lambda: overlay.set_state("Done", "transcribed word " * 60)),
        ("Error, retry", lambda: overlay.set_state("Error", "Transcription failed.")),
        (
            "Error, insert",
            lambda: overlay.set_state(
                "Error",
                "Insertion failed.",
                error_action=OVERLAY_ERROR_ACTION_INSERT,
                copy_text="the transcript",
            ),
        ),
        (
            "Error, no action",
            lambda: overlay.set_state(
                "Error",
                "Nothing to retry.",
                error_action=OVERLAY_ERROR_ACTION_NONE,
            ),
        ),
        ("Copy shows feedback", lambda: overlay._set_copy_button_feedback(True)),
        ("Copy feedback cleared", overlay._reset_copy_button_feedback),
        ("Record hovered", lambda: QtWidgets.QApplication.sendEvent(record, hover)),
        ("Record pressed", lambda: record.setDown(True)),
        ("Record released", lambda: record.setDown(False)),
        (
            "Queue shown",
            lambda: overlay.set_transcription_queue(
                [(index, f"recording-{index}.wav") for index in range(3)]
            ),
        ),
        ("Queue cleared", lambda: overlay.set_transcription_queue([])),
        (
            "Language fixed to Auto",
            lambda: overlay.set_language_options(("auto",), "auto"),
        ),
        ("Floating", lambda: overlay.set_always_on_top(False)),
        ("Pinned", lambda: overlay.set_always_on_top(True)),
    ]

    boxes: dict[str, tuple[int, int, int, int]] = {}
    try:
        for name, apply_case in cases:
            apply_case()
            app.processEvents()
            boxes[name] = _state_label_box(overlay)
            painted_centre = _painted_status_centre(overlay)
            overlay_centre = _overlay_centre(overlay)
            box = boxes[name]
            label_centre = box[0] + box[2] / 2.0

            # AlignCenter is assumed elsewhere; assert it instead.
            assert abs(painted_centre - label_centre) <= 1.0, (
                f"{name}: AlignCenter should paint the text centre on the "
                f"label's own centre {label_centre}, measured {painted_centre}"
            )
            assert abs(painted_centre - overlay_centre) <= 1.0, (
                f"{name}: the status text centre is {painted_centre}, which is "
                f"{painted_centre - overlay_centre:+.1f} px off the overlay's "
                f"centre line at {overlay_centre}"
            )

        # Nothing may jump: the label keeps one rectangle across every state,
        # button swap, hover, press, queue change and pin mode above.
        assert len(set(boxes.values())) == 1, (
            "the status label moved between states: "
            + ", ".join(f"{name}={box}" for name, box in boxes.items())
        )
    finally:
        overlay.close()


def test_the_header_button_groups_stay_equally_wide():
    """Equal flanks are the mechanism that centres the status label.

    The label is centred in the span the buttons leave over, so the two groups
    must have identical total widths; any difference moves the status text by
    half of it. ``_balance_header_flanks`` widens the narrower group at
    construction time, and this pins the result so a later width or caption
    change cannot quietly reintroduce the offset.
    """
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    try:
        header = overlay._header_widget
        header.layout().activate()
        spacing = header.layout().spacing()

        left = (
            overlay._record_button.width()
            + spacing
            + overlay._always_on_top_button.width()
        )
        right = overlay._clear_button.width() + spacing + overlay._copy_button.width()
        assert left == right, (
            f"header button groups differ: {left} px left, {right} px right, "
            f"so the status text sits {(left - right) / 2:+.1f} px off centre"
        )

        label = overlay._state_label
        label_left = label.mapTo(header, QtCore.QPoint(0, 0)).x()
        label_right = label_left + label.width()
        assert label_left + label_right == header.width(), (
            f"the label span [{label_left}, {label_right}] is not symmetric "
            f"within the {header.width()} px header"
        )
    finally:
        overlay.close()


def test_balancing_never_pins_a_header_button_under_its_own_caption(caplog):
    """The one failure the centring assertions structurally cannot see.

    `_balance_header_flanks` sizes each group from `minimumWidth()`, which is
    the width `setFixedWidth` pinned. A button that is *not* pinned reports
    the style minimum instead -- near zero -- so the group measures far too
    narrow and the deficit it is widened by is spread evenly over its members:
    the unpinned one then lands below the width its caption needs. The two
    flanks still come out equal, so both the centring and the no-jump
    assertions still pass while the caption is clipped.

    It takes a group of two to see it, which is the real header's shape: with
    one button per group the same wrong number appears in the group width and
    in the deficit and cancels out. Asserting the precondition afterwards
    cannot catch it either -- the balancing itself calls `setFixedWidth`, so
    by then every button looks pinned.

    The caption measured must be the *widest the button can ever show*, not
    the one it happens to carry at construction: the real pin button is still
    empty at that point and Copy still says "Copy", so a `sizeHint()` taken
    then would be far too small for "Floating" and "Copied".
    """
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    pinned = QtWidgets.QPushButton("Pinned")
    pinned.setFixedWidth(64)
    # Empty at measuring time, exactly like the real pin button.
    unpinned = QtWidgets.QPushButton("")
    captions = ("short", "A rather long caption")
    unpinned.setText(captions[-1])
    natural = unpinned.sizeHint().width()
    unpinned.setText("")
    assert unpinned.minimumWidth() < natural, "the stand-in is already pinned"
    # Derived, not hardcoded: the left flank has to be the wider one for the
    # right one to be widened at all, and `natural` moves with the system font.
    wide_left = QtWidgets.QPushButton("Left")
    wide_left.setFixedWidth(64 + 6 + natural + 20)

    with caplog.at_level(logging.WARNING, logger=overlay_ui_module.__name__):
        OverlayUI._balance_header_flanks(
            6, ((wide_left, ("Left",)),), ((pinned, ("Pinned",)), (unpinned, captions))
        )

    assert unpinned.minimumWidth() >= natural, (
        f"the widest caption needs {natural} px but the button was pinned to "
        f"{unpinned.minimumWidth()} px"
    )
    # The caption is restored, not left on whichever one measured widest.
    assert unpinned.text() == ""
    # The flanks are still equal -- which is exactly why nothing else catches
    # a clipped caption here.
    assert wide_left.minimumWidth() == (
        pinned.minimumWidth() + 6 + unpinned.minimumWidth()
    )
    assert "not fixed-width" in caplog.text
    # The warning has to name something: `button.text()` is empty here, which
    # is the very case that made the old message useless.
    assert "A rather long caption" in caplog.text


def test_every_runtime_caption_is_in_the_tuple_that_sizes_its_button():
    """The balancing sizes an unpinned button from these tuples.

    A caption a button can actually show but the tuple omits would be sized
    for the shorter one and clipped -- and the flanks would still come out
    equal, so neither the centring nor the no-jump assertion could see it.
    Nothing outside `overlay_ui.py` touches these buttons, so this drives
    every runtime path that sets one.
    """
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = OverlayUI()
    try:
        observed: dict[str, set[str]] = {
            "record": set(),
            "pin": set(),
            "copy": set(),
            "clear": {overlay._clear_button.text()},
        }
        for state in ("Idle", "Listening", "Processing", "Done", "Error"):
            overlay._sync_record_button(state)
            observed["record"].add(overlay._record_button.text())
        for pinned in (False, True, False):
            overlay._always_on_top = pinned
            overlay._sync_always_on_top_button()
            observed["pin"].add(overlay._always_on_top_button.text())
        for copied in (False, True, False):
            overlay._set_copy_button_feedback(copied)
            observed["copy"].add(overlay._copy_button.text())

        declared = {
            "record": set(overlay_ui_module.RECORD_BUTTON_CAPTIONS),
            "pin": set(overlay_ui_module.PIN_BUTTON_CAPTIONS),
            "copy": set(overlay_ui_module.COPY_BUTTON_CAPTIONS),
            "clear": {overlay_ui_module.CLEAR_BUTTON_TEXT},
        }
        for name, captions in observed.items():
            assert captions <= declared[name], (
                f"the {name} button shows {sorted(captions - declared[name])}, "
                "which the tuple that sizes it does not list"
            )
            assert captions, f"no caption was observed for the {name} button"
    finally:
        overlay.close()
