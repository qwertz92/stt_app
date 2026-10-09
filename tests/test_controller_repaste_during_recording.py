"""The re-paste hotkey during a streaming dictation (owner's request
2026-10-09): into another window at once, into the stream's own window held
until the stream has finished, and never answered with "not possible"."""

from __future__ import annotations

import time
from datetime import datetime

from conftest import (
    FakeCapture,
    FakeOverlay,
    FakeTextInserter,
    FakeWindowFocusHelper,
    make_controller,
)
from PySide6 import QtWidgets

_STREAM_WINDOW = 987


def _streaming_controller(*, capture_open: bool = True):
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    focus = FakeWindowFocusHelper()
    controller, app = make_controller(
        overlay=overlay, text_inserter=inserter, window_focus_helper=focus
    )
    controller._last_transcript = "hello again"
    controller._streaming_recording = True
    controller._target_window_handle = _STREAM_WINDOW
    controller._target_focus_signature = (_STREAM_WINDOW, 654, 321)
    if capture_open:
        controller._audio_capture = FakeCapture()
    tray: list[str] = []
    controller.busy_overlay_error.connect(tray.append)
    return controller, app, inserter, focus, tray


def _focus_another_window(focus: FakeWindowFocusHelper) -> None:
    focus.current = 555
    focus.current_focus = 556
    focus.current_caret = 557


def _end_stream(controller, app) -> None:
    controller._audio_capture = None
    controller._reset_streaming_state()
    for _ in range(20):
        app.processEvents()


def _wait_for_timer(app, controller) -> None:
    deadline = time.monotonic() + 5
    while controller._paste_pace_timer.isActive() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def test_into_another_window_the_re_paste_goes_out_during_the_stream():
    """The stream writes only into its own window (a focus change suspends
    its live inserts), so a paste elsewhere cannot land inside its words."""
    controller, app, inserter, focus, tray = _streaming_controller()
    _focus_another_window(focus)

    controller.repaste_last_transcript()

    assert [call[:2] for call in inserter.calls] == [("hello again", 557)]
    assert controller._pending_repaste is None
    assert not any("Finish the streaming" in message for message in tray)
    _end_stream(controller, app)
    controller.shutdown()


def test_into_the_streams_own_window_the_re_paste_waits_for_the_stream():
    """Live inserts write at that caret, so a paste now would land inside the
    streamed words: it is held, the tray says so, and it goes out once the
    stream has ended -- instead of the old "Finish the streaming recording"
    refusal, after which the user had to press it again."""
    controller, app, inserter, _focus, tray = _streaming_controller()

    controller.repaste_last_transcript()

    assert inserter.calls == []
    assert controller._pending_repaste is not None
    assert len(tray) == 1 and "streaming dictation" in tray[0], tray
    assert "when the streaming dictation" in tray[0], tray

    _end_stream(controller, app)
    _wait_for_timer(app, controller)

    assert [call[0] for call in inserter.calls] == ["hello again"]
    assert controller._pending_repaste is None
    controller.shutdown()


def test_a_pending_finalize_holds_a_re_paste_into_its_window_too():
    """The microphone is closed but the finalize still inserts its tail past
    the live text; the held paste goes out after it."""
    controller, app, inserter, _focus, tray = _streaming_controller(capture_open=False)

    controller.repaste_last_transcript()

    assert inserter.calls == []
    assert len(tray) == 1 and "when the streaming dictation" in tray[0], tray
    _end_stream(controller, app)
    _wait_for_timer(app, controller)
    assert [call[0] for call in inserter.calls] == ["hello again"]
    controller.shutdown()


def test_a_held_re_paste_stays_held_while_the_stream_runs():
    """The pace timer also runs for other pastes during the stream; a held
    re-paste waits for the stream's end, not for one of those ticks -- not
    even when the user has looked at another window meanwhile -- and is not
    announced a second time."""
    controller, app, inserter, focus, tray = _streaming_controller()
    controller.repaste_last_transcript()
    _focus_another_window(focus)

    controller._on_paste_pace_timeout()

    assert inserter.calls == []
    assert controller._pending_repaste is not None
    assert len(tray) == 1, tray
    _end_stream(controller, app)
    controller.shutdown()


def test_held_waiting_rows_are_pasted_as_they_are_after_the_stream():
    controller, app, inserter, _focus, _tray = _streaming_controller()
    created = datetime.now().astimezone()
    first = controller._record_undelivered_insert(
        "first", may_have_pasted=False, created_at=created, history_entry=None
    )
    second = controller._record_undelivered_insert(
        "second", may_have_pasted=False, created_at=created, history_entry=None
    )

    controller.repaste_last_transcript()
    # Dismissed while held: not pasted.
    controller._dismiss_undelivered(first.row_id)
    _end_stream(controller, app)
    _wait_for_timer(app, controller)

    assert [call[0] for call in inserter.calls] == ["second"]
    assert all(row is not second for row in controller._undelivered_inserts)
    controller.shutdown()


def test_after_a_re_paste_elsewhere_the_streams_next_live_insert_waits_for_it():
    """The stream's next live insert would overwrite the clipboard while the
    other window may still read the re-paste from it (the restore window
    every other paste respects)."""
    controller, app, _inserter, focus, _tray = _streaming_controller()
    controller._stream_text_state.committed_text = "already streamed"
    remaining = [0.0]
    controller._text_inserter.paste_pace_remaining_s = lambda: remaining[0]
    _focus_another_window(focus)

    controller.repaste_last_transcript()
    remaining[0] = 1.2

    assert controller._stream_live_insert_held() is True
    remaining[0] = 0.0
    assert controller._stream_live_insert_held() is False
    # Only the paste's own window: a later restore window of a live insert
    # does not hold the next one.
    remaining[0] = 1.2
    assert controller._stream_live_insert_held() is False
    _end_stream(controller, app)
    controller.shutdown()


def test_a_batch_recording_still_lets_the_re_paste_through():
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    controller, app = make_controller(
        overlay=overlay,
        text_inserter=inserter,
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._last_transcript = "hello again"
    controller._audio_capture = FakeCapture()

    controller.repaste_last_transcript()

    assert [call[0] for call in inserter.calls] == ["hello again"]
    controller._audio_capture = None
    controller.shutdown()
    _ = (app, QtWidgets)
