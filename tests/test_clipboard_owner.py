"""The clipboard owner's read receipt, against a scripted Windows.

The window procedure is called directly, as Windows calls it when a program
asks for the announced transcript; no real window and no real clipboard are
involved (the real ones are covered by
`scripts/release_check_clipboard_paste.py`).
"""

from __future__ import annotations

import threading

from stt_app import clipboard_owner
from stt_app.clipboard_owner import (
    NO_RECEIPT,
    RECEIPT_READ,
    RECEIPT_UNKNOWN,
    RECEIPT_UNREAD,
    ClipboardOwner,
)

OWNER_HWND = 4242
TARGET_PID = 100
OTHER_PID = 200
SYNC_PID = 300
RENDER = clipboard_owner._WM_RENDERFORMAT
RENDER_ALL = clipboard_owner._WM_RENDERALLFORMATS
DESTROY = clipboard_owner._WM_DESTROYCLIPBOARD
TEXT_FORMAT = clipboard_owner._CF_UNICODETEXT


class FakeOwnerApi:
    """Windows as the owner sees it: one reader at a time, scripted."""

    def __init__(self, *, hwnd: int = OWNER_HWND) -> None:
        self._hwnd = hwnd
        self._quit = threading.Event()
        self.images = {
            TARGET_PID: "editor.exe",
            OTHER_PID: "manager.exe",
            SYNC_PID: "msrdc.exe",
        }
        self.reader = 0
        self.owner_window = hwnd
        self.rendered: list[tuple[int, str]] = []
        self.opened: list[int] = []
        self.destroyed: list[int] = []
        self.defaulted: list[int] = []

    @staticmethod
    def wndproc_type(function):
        return function

    def create_window(self, _wndproc) -> int:
        return self._hwnd

    def current_thread_id(self) -> int:
        return 7

    def pump_messages(self) -> None:
        self._quit.wait(5.0)

    def post_quit(self, _thread_id: int) -> None:
        self._quit.set()

    def destroy_window(self, hwnd: int) -> None:
        self.destroyed.append(hwnd)

    def default_window_proc(self, _hwnd, message, _wparam, _lparam):
        self.defaulted.append(message)
        return 0

    def clipboard_owner(self) -> int:
        return self.owner_window

    def reader_pid(self) -> int:
        return self.reader

    def process_image(self, pid: int) -> str:
        return self.images.get(pid, "")

    def set_text(self, format_id: int, text: str) -> bool:
        self.rendered.append((format_id, text))
        return True

    def open_clipboard(self, hwnd: int) -> bool:
        self.opened.append(hwnd)
        return True

    def close_clipboard(self) -> None:
        self.opened.append(0)


class Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


def _started(api: FakeOwnerApi | None = None):
    api = api or FakeOwnerApi()
    clock = Clock()
    owner = ClipboardOwner(api_factory=lambda: api, clock=clock)
    assert owner.start() is True
    return owner, api, clock


def _request(owner: ClipboardOwner, api: FakeOwnerApi, pid: int) -> None:
    """A program of `pid` calls `GetClipboardData` for the text."""
    api.reader = pid
    owner._window_proc(OWNER_HWND, RENDER, TEXT_FORMAT, 0)


def _offer(owner: ClipboardOwner, clock: Clock, text: str = "dictated") -> int:
    serial = owner.begin_offer(text, frozenset({TARGET_PID}))
    clock.now += 0.02
    owner.note_keystroke(serial)
    return serial


def test_an_offer_nobody_asked_for_is_unread():
    owner, api, clock = _started()
    try:
        serial = _offer(owner, clock)
        clock.now += 1.5

        receipt = owner.receipt(serial)

        assert (receipt.verdict, receipt.reason) == (RECEIPT_UNREAD, "no_request")
        assert api.rendered == []
    finally:
        owner.close()


def test_the_target_fetching_the_text_after_the_keystroke_is_a_read():
    owner, api, clock = _started()
    try:
        serial = _offer(owner, clock)
        clock.now += 0.035
        _request(owner, api, TARGET_PID)

        receipt = owner.receipt(serial)

        assert api.rendered == [(TEXT_FORMAT, "dictated")]
        assert (receipt.verdict, receipt.reason) == (RECEIPT_READ, "target")
        assert receipt.reader == "editor.exe"
        assert receipt.read_ms == 35
        assert "reader=editor.exe read_ms=35 refused=0" in receipt.evidence()
    finally:
        owner.close()


def test_a_request_before_the_keystroke_proves_nothing_about_the_paste():
    owner, api, clock = _started()
    try:
        serial = owner.begin_offer("dictated", frozenset({TARGET_PID}))
        clock.now += 0.003
        _request(owner, api, TARGET_PID)
        clock.now += 0.017
        owner.note_keystroke(serial)

        receipt = owner.receipt(serial)

        assert api.rendered == [(TEXT_FORMAT, "dictated")], "the reader got no data"
        assert (receipt.verdict, receipt.reason) == (
            RECEIPT_UNKNOWN,
            "before_keystroke",
        )
        assert receipt.read_ms == -17
    finally:
        owner.close()


def test_another_program_fetching_the_text_first_proves_nothing():
    owner, api, clock = _started()
    try:
        serial = _offer(owner, clock)
        _request(owner, api, OTHER_PID)
        # Served once, the text is ordinary clipboard data: the target's own
        # read can no longer be seen, so nothing later turns this into a read.
        assert (owner.receipt(serial).verdict, owner.receipt(serial).reason) == (
            RECEIPT_UNKNOWN,
            "other_program",
        )
        assert api.rendered == [(TEXT_FORMAT, "dictated")]
    finally:
        owner.close()


def test_a_reader_that_names_no_window_is_served_and_proves_nothing():
    owner, api, clock = _started()
    try:
        serial = _offer(owner, clock)
        _request(owner, api, 0)

        receipt = owner.receipt(serial)

        assert api.rendered == [(TEXT_FORMAT, "dictated")]
        assert (receipt.verdict, receipt.reason) == (
            RECEIPT_UNKNOWN,
            "unidentified_reader",
        )
    finally:
        owner.close()


def test_a_clipboard_sync_program_is_refused_and_the_target_still_counts():
    """WSLg's `msrdc.exe` asked 3 ms after every write (2026-11-24): served,
    it made every receipt read "before the keystroke"."""
    owner, api, clock = _started()
    try:
        serial = owner.begin_offer("dictated", frozenset({TARGET_PID}))
        clock.now += 0.003
        _request(owner, api, SYNC_PID)
        assert api.rendered == [], "the sync program was handed the transcript"
        clock.now += 0.017
        owner.note_keystroke(serial)
        unread = owner.receipt(serial)
        assert (unread.verdict, unread.refused) == (RECEIPT_UNREAD, 1)

        clock.now += 0.04
        _request(owner, api, TARGET_PID)

        receipt = owner.receipt(serial)
        assert api.rendered == [(TEXT_FORMAT, "dictated")]
        assert (receipt.verdict, receipt.reason, receipt.refused) == (
            RECEIPT_READ,
            "target",
            1,
        )
    finally:
        owner.close()


def test_a_sync_program_that_is_the_paste_target_is_served():
    """A Linux application's window belongs to `msrdc.exe`."""
    owner, api, clock = _started()
    try:
        serial = owner.begin_offer("dictated", frozenset({SYNC_PID}))
        owner.note_keystroke(serial)
        clock.now += 0.05
        _request(owner, api, SYNC_PID)

        assert api.rendered == [(TEXT_FORMAT, "dictated")]
        assert owner.receipt(serial).verdict == RECEIPT_READ
    finally:
        owner.close()


def test_a_released_offer_serves_the_sync_program_too():
    """A transcript that stays on the clipboard is ordinary content."""
    owner, api, clock = _started()
    try:
        serial = _offer(owner, clock)
        owner.release(serial)
        _request(owner, api, SYNC_PID)

        assert api.rendered == [(TEXT_FORMAT, "dictated")]
    finally:
        owner.close()


def test_this_apps_own_read_is_served_and_is_no_receipt():
    owner, api, clock = _started()
    try:
        serial = _offer(owner, clock)
        with owner.own_read():
            _request(owner, api, TARGET_PID)
        _request(owner, api, TARGET_PID)

        receipt = owner.receipt(serial)

        assert (receipt.verdict, receipt.reason) == (RECEIPT_UNKNOWN, "own_read")
    finally:
        owner.close()


def test_another_format_is_not_answered_with_the_text():
    owner, api, clock = _started()
    try:
        serial = _offer(owner, clock)
        api.reader = TARGET_PID
        owner._window_proc(OWNER_HWND, RENDER, 49999, 0)

        assert api.rendered == []
        assert owner.receipt(serial).verdict == RECEIPT_UNREAD
    finally:
        owner.close()


def test_an_emptied_clipboard_ends_the_offer_but_not_its_receipt():
    owner, api, clock = _started()
    try:
        serial = _offer(owner, clock)
        assert owner.is_intact(serial) is True

        owner._window_proc(OWNER_HWND, DESTROY, 0, 0)

        assert owner.is_intact(serial) is False
        assert owner.receipt(serial).verdict == RECEIPT_UNREAD
        _request(owner, api, TARGET_PID)
        assert api.rendered == [], "a request for an ended offer was answered"
    finally:
        owner.close()


def test_a_clipboard_someone_else_owns_is_not_intact():
    owner, api, clock = _started()
    try:
        serial = _offer(owner, clock)
        api.owner_window = 999

        assert owner.is_intact(serial) is False
    finally:
        owner.close()


def test_an_ended_offer_keeps_its_receipt_for_the_paste_that_replaced_it():
    """The next paste asks for it after its own write."""
    owner, api, clock = _started()
    try:
        first = _offer(owner, clock)
        second = _offer(owner, clock, "next")
        _request(owner, api, TARGET_PID)

        assert api.rendered == [(TEXT_FORMAT, "next")]
        assert owner.is_intact(first) is False
        assert owner.receipt(first).verdict == RECEIPT_UNREAD
        assert owner.receipt(second).verdict == RECEIPT_READ
        assert owner.receipt(None) is NO_RECEIPT
        assert owner.receipt(second + 50) is NO_RECEIPT
    finally:
        owner.close()


def test_only_the_last_few_ended_offers_are_kept():
    owner, _api, clock = _started()
    try:
        serials = [_offer(owner, clock) for _ in range(6)]

        assert owner.receipt(serials[0]) is NO_RECEIPT
        assert owner.receipt(serials[-4]).verdict == RECEIPT_UNREAD
    finally:
        owner.close()


def test_a_window_that_goes_away_writes_the_text_it_still_owes():
    owner, api, clock = _started()
    try:
        _offer(owner, clock)

        owner._window_proc(OWNER_HWND, RENDER_ALL, 0, 0)

        assert api.rendered == [(TEXT_FORMAT, "dictated")]
        assert api.opened == [OWNER_HWND, 0], "the clipboard was not opened and closed"
    finally:
        owner.close()


def test_a_window_that_goes_away_writes_nothing_once_the_text_was_served():
    owner, api, clock = _started()
    try:
        _offer(owner, clock)
        _request(owner, api, TARGET_PID)

        owner._window_proc(OWNER_HWND, RENDER_ALL, 0, 0)

        assert api.rendered == [(TEXT_FORMAT, "dictated")]
        assert api.opened == []
    finally:
        owner.close()


def test_other_messages_go_to_the_default_window_procedure():
    owner, api, _clock = _started()
    try:
        owner._window_proc(OWNER_HWND, 0x0001, 0, 0)

        assert api.defaulted == [0x0001]
    finally:
        owner.close()


def test_messages_sent_while_the_window_is_created_are_answered():
    """Windows calls the procedure from inside `CreateWindowEx`. Answered
    with an exception, the first real window was never created."""

    class CreatingApi(FakeOwnerApi):
        creation_answer = None

        def create_window(self, wndproc) -> int:
            self.creation_answer = wndproc(self._hwnd, 0x0081, 0, 0)
            return self._hwnd

        def default_window_proc(self, _hwnd, message, _wparam, _lparam):
            self.defaulted.append(message)
            return 1

    api = CreatingApi()
    owner = ClipboardOwner(api_factory=lambda: api)
    try:
        assert owner.start() is True
        assert api.creation_answer == 1
        assert api.defaulted == [0x0081]
    finally:
        owner.close()


def test_a_failing_request_is_logged_and_answered(caplog):
    """An exception leaving the window procedure would be printed by ctypes
    and leave no line in the log."""
    owner, api, clock = _started()
    try:
        _offer(owner, clock)

        def broken() -> int:
            raise OSError("no opener")

        api.reader_pid = broken
        with caplog.at_level("ERROR", logger="stt_app.clipboard_owner"):
            answer = owner._window_proc(OWNER_HWND, RENDER, TEXT_FORMAT, 0)

        assert answer == 0
        assert "window procedure failed" in caplog.text
    finally:
        owner.close()


def test_without_a_window_the_owner_reports_that_it_cannot_start():
    owner = ClipboardOwner(api_factory=lambda: FakeOwnerApi(hwnd=0))

    assert owner.start() is False
    assert owner.hwnd == 0
    assert owner.is_intact(1) is False
    owner.close()


def test_close_ends_the_thread_and_destroys_the_window():
    owner, api, _clock = _started()
    thread = owner._thread

    owner.close()

    assert thread is not None and not thread.is_alive()
    assert api.destroyed == [OWNER_HWND]
    assert owner.start() is False, "a closed owner was started again"


def test_an_image_name_cannot_break_the_log_line():
    receipt = clipboard_owner.PasteReceipt(
        RECEIPT_UNKNOWN, "other_program", reader="my tool\n.exe", read_ms=5
    )

    assert "reader=my_tool_.exe read_ms=5" in receipt.evidence()
