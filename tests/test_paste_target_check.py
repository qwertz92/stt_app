"""The report-only paste target check: its verdict rules and its worker."""

from __future__ import annotations

import threading

import pytest

from stt_app.paste_target_check import (
    VERDICT_NOT_TEXT_FIELD,
    VERDICT_TEXT_FIELD,
    VERDICT_UNKNOWN,
    CaretReading,
    PasteTargetCheck,
    check_paste_target,
    read_focused_caret,
)


class _Reader:
    """The Win32 answers `read_focused_caret` combines."""

    def __init__(
        self,
        *,
        foreground=0x100,
        gui=(False, 0x200),
        msaa=(True, 0),
        window_class="Chrome_WidgetWin_1",
    ):
        self._foreground = foreground
        self._gui = gui
        self._msaa = msaa
        self._window_class = window_class
        self.msaa_windows = []

    def window_class(self, _hwnd):
        return self._window_class

    def foreground(self):
        return self._foreground

    def gui_caret(self, _foreground):
        return self._gui

    def msaa_caret(self, hwnd):
        self.msaa_windows.append(hwnd)
        return self._msaa


@pytest.mark.parametrize(
    ("label", "reader", "expected"),
    [
        # A Win32 caret (EDIT, RichEdit) decides without asking MSAA.
        ("win32 caret", _Reader(gui=(True, 0x200), msaa=None), VERDICT_TEXT_FIELD),
        # Chromium's focused input: no Win32 caret, MSAA caret visible, 1 px.
        ("chromium input", _Reader(msaa=(False, 1)), VERDICT_TEXT_FIELD),
        # Chromium's button, list item, body; a Win32 BUTTON.
        ("no caret anywhere", _Reader(msaa=(True, 0)), VERDICT_NOT_TEXT_FIELD),
        ("visible but zero wide", _Reader(msaa=(False, 0)), VERDICT_NOT_TEXT_FIELD),
        ("invisible but wide", _Reader(msaa=(True, 1)), VERDICT_NOT_TEXT_FIELD),
        # Anything the check cannot read is unknown, never "not a text field":
        # unknown keeps the paste reported as it was before the check.
        ("msaa unanswered", _Reader(msaa=None), VERDICT_UNKNOWN),
        ("gui unreadable", _Reader(gui=None), VERDICT_UNKNOWN),
        ("no foreground", _Reader(foreground=None), VERDICT_UNKNOWN),
        # Windows Terminal draws its own cursor: no Win32 caret, MSAA caret
        # invisible and 0 wide, and its prompt takes every paste (measured).
        (
            "own-drawn caret",
            _Reader(window_class="CASCADIA_HOSTING_WINDOW_CLASS"),
            VERDICT_UNKNOWN,
        ),
        # A Win32 caret counts in any window.
        (
            "win32 caret elsewhere",
            _Reader(gui=(True, 0x200), window_class="Notepad"),
            VERDICT_TEXT_FIELD,
        ),
    ],
)
def test_both_sources_must_say_no_caret(label, reader, expected):
    assert read_focused_caret(reader).verdict == expected, label


def test_msaa_is_asked_about_the_focus_window_not_the_top_level():
    reader = _Reader(foreground=0x100, gui=(False, 0x200), msaa=(True, 0))
    read_focused_caret(reader)
    assert reader.msaa_windows == [0x200]


def test_another_foreground_than_the_pasted_one_is_unknown():
    """The user switched windows after the paste: its caret says nothing."""
    reading = read_focused_caret(_Reader(foreground=0x999), expected_foreground=0x100)
    assert reading.verdict == VERDICT_UNKNOWN


def _scripted(*verdicts):
    calls = []

    def read(expected):
        calls.append(expected)
        return CaretReading(verdicts[len(calls) - 1], 0x100, "scripted")

    return read, calls


def test_no_caret_stands_only_when_every_reading_agrees():
    read, calls = _scripted(
        VERDICT_NOT_TEXT_FIELD, VERDICT_NOT_TEXT_FIELD, VERDICT_TEXT_FIELD
    )
    reading = check_paste_target(
        read,
        expected_foreground=None,
        recheck_delays_s=(0.1, 0.25),
        sleep=lambda _s: None,
    )
    assert reading.verdict == VERDICT_TEXT_FIELD
    # The rechecks hold the first reading's foreground, so a switch in
    # between reads as "unknown" rather than as the other window's caret.
    assert calls == [None, 0x100, 0x100]


def test_a_caret_in_the_first_reading_decides_at_once():
    read, calls = _scripted(VERDICT_TEXT_FIELD)
    slept = []
    reading = check_paste_target(
        read,
        expected_foreground=0x100,
        recheck_delays_s=(0.1, 0.25),
        sleep=slept.append,
    )
    assert reading.verdict == VERDICT_TEXT_FIELD
    assert len(calls) == 1 and slept == []


class _BlockingReader(_Reader):
    def __init__(self, release):
        super().__init__(msaa=(False, 1))
        self._release = release

    def foreground(self):
        self._release.wait(5)
        return 0x100


def test_a_second_request_while_one_runs_is_refused_not_queued():
    """A hung target holds the worker inside its WM_GETOBJECT; later pastes
    must not wait behind it."""
    release = threading.Event()
    done = threading.Event()
    readings = []
    check = PasteTargetCheck(
        recheck_delays_s=(), reader_factory=lambda: _BlockingReader(release)
    )
    try:
        assert check.request(lambda r: (readings.append(r), done.set()))
        assert not check.request(readings.append)
        release.set()
        assert done.wait(5)
        assert [r.verdict for r in readings] == [VERDICT_TEXT_FIELD]
    finally:
        release.set()
        check.close()


def test_a_reader_that_raises_answers_unknown():
    done = threading.Event()
    readings = []

    class _Raising(_Reader):
        def gui_caret(self, _foreground):
            raise OSError("access denied")

    check = PasteTargetCheck(recheck_delays_s=(), reader_factory=_Raising)
    try:
        assert check.request(lambda r: (readings.append(r), done.set()))
        assert done.wait(5)
        assert readings[0].verdict == VERDICT_UNKNOWN
    finally:
        check.close()
