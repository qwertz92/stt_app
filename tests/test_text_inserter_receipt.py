"""The paste transaction with a read receipt (`clipboard_owner`).

A SendInput paste used to be "pasted" the moment the keystroke went out. The
owner's log of 2026-10-23..11-24 holds about fifteen pastes reported that way
whose text he pasted again by hand seconds later. With the transcript
announced instead of written, the transaction learns whether a program
fetched it, and reports that once per paste.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest
from test_text_inserter import (
    FakeClock,
    RecordingScheduler,
    SequencedGatedBackend,
    _FakeUser32,
)

from stt_app import text_inserter
from stt_app.clipboard_owner import (
    NO_RECEIPT,
    RECEIPT_READ,
    RECEIPT_UNKNOWN,
    RECEIPT_UNREAD,
    PasteReceipt,
)
from stt_app.text_inserter import (
    ClipboardMarker,
    TextInserter,
    TextMayHaveBeenPastedError,
    Win32ClipboardBackend,
)

RESTORE_DELAY_S = 1.5
RECEIPT_WAIT_S = 3.0


class ReceiptBackend(SequencedGatedBackend):
    """A clipboard whose transcript write is an offer with a scripted receipt."""

    def __init__(self, target_ready=True):
        super().__init__(paste_mode="send_input", target_ready=target_ready)
        self._serials = itertools.count(1)
        # The offer on the clipboard; None once anything else was written.
        self.offer = None
        self.receipts = {}
        self.reader_hwnds = []
        self.keystrokes = []
        self.released = []
        self.text_reads = 0
        self.closed = False
        self.foreground = 4242

    def get_foreground_window(self):
        return self.foreground

    def offer_clipboard_text(self, text, reader_hwnd):
        self.calls.append(f"offer:{text}")
        self.reader_hwnds.append(reader_hwnd)
        self.state = {"has_text": True, "text": text}
        self.sequence += 1
        self.offer = next(self._serials)
        self.receipts[self.offer] = PasteReceipt(RECEIPT_UNREAD, "no_request")
        return ClipboardMarker(sequence=self.sequence, offer=self.offer)

    def set_clipboard_text(self, text, exclude_from_history=False):
        super().set_clipboard_text(text, exclude_from_history=exclude_from_history)
        self.offer = None

    def restore_clipboard_state(self, state):
        super().restore_clipboard_state(state)
        self.offer = None

    def simulate_user_copy(self, text):
        super().simulate_user_copy(text)
        self.offer = None

    def get_clipboard_text(self):
        self.text_reads += 1
        return super().get_clipboard_text()

    def offer_intact(self, offer):
        return offer is not None and offer == self.offer

    def note_paste_keystroke(self, offer):
        self.keystrokes.append(offer)

    def paste_receipt(self, offer):
        return self.receipts.get(offer, NO_RECEIPT)

    def release_offer(self, offer):
        self.released.append(offer)

    def close(self):
        self.closed = True

    def target_fetches(self, offer=None):
        self.receipts[offer or self.offer] = PasteReceipt(
            RECEIPT_READ, "target", reader="editor.exe", read_ms=30
        )


def _inserter(backend=None, **kwargs):
    backend = backend or ReceiptBackend()
    scheduler = RecordingScheduler()
    clock = FakeClock()
    inserter = TextInserter(
        backend=backend,
        sleep_fn=lambda _s: None,
        schedule_fn=scheduler,
        clock=clock,
        restore_delay_s=RESTORE_DELAY_S,
        receipt_wait_s=RECEIPT_WAIT_S,
        **kwargs,
    )
    receipts = []
    inserter.set_paste_receipt_handler(
        lambda transaction_id, verdict, evidence: receipts.append(
            (transaction_id, verdict, evidence, list(backend.calls))
        )
    )
    return inserter, backend, scheduler, clock, receipts


def test_a_restoring_paste_announces_the_transcript_and_never_reads_it_back():
    inserter, backend, _scheduler, _clock, receipts = _inserter()

    assert inserter.insert_text_with_options("hello", target_hwnd=77) is True

    assert "offer:hello" in backend.calls and "set:hello" not in backend.calls
    # A SendInput keystroke goes to the foreground window, whatever window
    # the caller named.
    assert backend.reader_hwnds == [4242]
    assert backend.keystrokes == [1]
    assert backend.calls.index("offer:hello") < backend.calls.index("paste:77")
    assert backend.text_reads == 0, "reading it back uses the receipt up"
    assert inserter.pending_receipt_id() is not None
    assert receipts == []


def test_a_wm_paste_names_its_target_window_as_the_reader():
    inserter, backend, _scheduler, _clock, _receipts = _inserter()
    backend.paste_mode = "wm_paste"

    inserter.insert_text_with_options("hello", target_hwnd=77, paste_mode="wm_paste")

    assert backend.reader_hwnds == [77]
    assert inserter.pending_receipt_id() is None


def test_a_transcript_the_user_keeps_is_plain_text_without_a_receipt():
    inserter, backend, scheduler, _clock, receipts = _inserter()

    inserter.insert_text_with_options("hello", target_hwnd=77, restore_clipboard=False)

    assert "set:hello" in backend.calls and "offer:hello" not in backend.calls
    assert inserter.pending_receipt_id() is None
    assert scheduler.pending == [] and receipts == []


def test_a_fetched_transcript_is_restored_at_the_restore_delay_and_reported_read():
    inserter, backend, scheduler, clock, receipts = _inserter()
    inserter.insert_text_with_options("hello", target_hwnd=77)
    transaction_id = inserter.pending_receipt_id()
    backend.target_fetches()

    clock.advance(RESTORE_DELAY_S)
    scheduler.fire_pending()

    assert backend.calls[-1] == "restore"
    assert backend.text_reads == 0
    assert [(r[0], r[1]) for r in receipts] == [(transaction_id, RECEIPT_READ)]
    assert "reader=editor.exe read_ms=30" in receipts[0][2]
    assert inserter.pending_receipt_id() is None
    assert scheduler.pending == []


def test_an_unfetched_transcript_stays_until_the_receipt_wait_then_is_unread(caplog):
    """Putting the clipboard back at 1.5 s made a target that got to its
    paste later insert the user's old clipboard content."""
    inserter, backend, scheduler, clock, receipts = _inserter()
    inserter.insert_text_with_options("hello", target_hwnd=77)
    transaction_id = inserter.pending_receipt_id()

    clock.advance(RESTORE_DELAY_S)
    scheduler.fire_pending()

    assert "restore" not in backend.calls, "restored under a target still to read"
    assert receipts == []
    assert scheduler.delays == [RESTORE_DELAY_S, RECEIPT_WAIT_S - RESTORE_DELAY_S]
    assert inserter.paste_pace_remaining_s() == pytest.approx(
        RECEIPT_WAIT_S - RESTORE_DELAY_S
    ), "a queued paste would overwrite the transcript its reader has not fetched"

    clock.advance(RECEIPT_WAIT_S - RESTORE_DELAY_S)
    with caplog.at_level("INFO", logger="stt_app.text_inserter"):
        scheduler.fire_pending()

    assert backend.calls[-1] == "restore"
    assert [(r[0], r[1]) for r in receipts] == [(transaction_id, RECEIPT_UNREAD)]
    assert inserter.paste_pace_remaining_s() == 0.0
    assert f"paste_receipt id={transaction_id} verdict=unread" in caplog.text
    assert "hello" not in caplog.text


def test_a_transcript_fetched_during_the_receipt_wait_is_reported_read():
    inserter, backend, scheduler, clock, receipts = _inserter()
    inserter.insert_text_with_options("hello", target_hwnd=77)
    clock.advance(RESTORE_DELAY_S)
    scheduler.fire_pending()

    clock.advance(0.5)
    backend.target_fetches()
    assert inserter.paste_pace_remaining_s() == 0.0

    clock.advance(RECEIPT_WAIT_S - RESTORE_DELAY_S - 0.5)
    scheduler.fire_pending()

    assert backend.calls[-1] == "restore"
    assert [r[1] for r in receipts] == [RECEIPT_READ]


def test_the_next_paste_reports_the_receipt_of_the_one_it_replaces():
    """After its own write, so a fetch in the last moment still counts."""
    inserter, backend, scheduler, clock, receipts = _inserter()
    inserter.insert_text_with_options("first", target_hwnd=77)
    first_id = inserter.pending_receipt_id()

    clock.advance(RECEIPT_WAIT_S)
    inserter.insert_text_with_options("second", target_hwnd=77)

    assert [(r[0], r[1]) for r in receipts] == [(first_id, RECEIPT_UNREAD)]
    assert "offer:second" in receipts[0][3], "asked before the new write"
    assert inserter.pending_receipt_id() not in (None, first_id)
    assert backend.calls.count("capture") == 1, "captured our own transcript"
    assert backend.text_reads == 0

    backend.target_fetches()
    clock.advance(RESTORE_DELAY_S)
    scheduler.fire_pending()

    assert [r[1] for r in receipts] == [RECEIPT_UNREAD, RECEIPT_READ]
    assert backend.state == {"has_text": True, "text": "old"}


def test_a_copy_by_the_user_ends_the_receipt_as_it_stood():
    inserter, backend, scheduler, clock, receipts = _inserter()
    inserter.insert_text_with_options("hello", target_hwnd=77)
    backend.simulate_user_copy("copied by the user")

    clock.advance(RESTORE_DELAY_S)
    scheduler.fire_pending()

    assert "restore" not in backend.calls
    assert backend.state["text"] == "copied by the user"
    assert [r[1] for r in receipts] == [RECEIPT_UNREAD]
    assert scheduler.pending == []


def test_a_target_busy_to_the_end_keeps_the_transcript_and_gets_no_verdict():
    """It may still fetch the transcript, which stays for it: "unread" would
    offer a second paste of a text that can still arrive."""
    inserter, backend, scheduler, clock, receipts = _inserter(
        ReceiptBackend(target_ready=False), restore_max_wait_s=2.0
    )
    inserter.insert_text_with_options("hello", target_hwnd=77)

    clock.advance(RESTORE_DELAY_S)
    scheduler.fire_pending()
    clock.advance(RESTORE_DELAY_S)
    scheduler.fire_pending()

    assert "restore" not in backend.calls
    assert backend.released == [1]
    assert [r[1] for r in receipts] == [RECEIPT_UNKNOWN]
    assert "reason=transcript_stays" in receipts[0][2]


def test_shutdown_restores_at_once_and_closes_the_backend():
    inserter, backend, scheduler, _clock, receipts = _inserter()
    inserter.insert_text_with_options("hello", target_hwnd=77)

    inserter.close()

    assert backend.calls[-1] == "restore"
    assert [r[1] for r in receipts] == [RECEIPT_UNREAD]
    assert backend.closed is True
    assert scheduler.pending == []


def test_a_paste_refused_before_the_keystroke_has_no_receipt():
    inserter, backend, scheduler, _clock, receipts = _inserter()

    def switch_window(_seconds):
        backend.foreground = 999

    inserter._sleep_fn = switch_window
    with pytest.raises(text_inserter.TextInsertionError):
        inserter.insert_text_with_options("hello", target_hwnd=77)

    assert backend.keystrokes == []
    assert backend.calls[-1] == "restore"
    assert inserter.pending_receipt_id() is None
    assert receipts == [] and scheduler.pending == []


def test_a_keystroke_that_may_have_gone_out_leaves_the_text_for_every_reader():
    """No restore is to come, so no program is refused the transcript."""
    inserter, backend, scheduler, _clock, receipts = _inserter()

    def partial_keystroke(mode, target_hwnd=None):
        raise TextMayHaveBeenPastedError("Ctrl+V was only partly sent.")

    backend.send_paste_with_mode = partial_keystroke
    with pytest.raises(TextMayHaveBeenPastedError):
        inserter.insert_text_with_options("hello", target_hwnd=77)

    assert "restore" not in backend.calls
    assert backend.released == [1]
    assert receipts == [] and scheduler.pending == []


# -- The real backend ---------------------------------------------------------


class _FakeOwner:
    def __init__(self, *, starts=True, intact=True):
        self.hwnd = 9000 if starts else 0
        self._starts = starts
        self.intact = intact
        self.offers = []
        self.calls = None

    def start(self):
        return self._starts

    def begin_offer(self, text, target_pids):
        self.calls.append("begin_offer")
        self.offers.append((text, target_pids))
        return len(self.offers)

    def is_intact(self, offer):
        return self.intact and offer == len(self.offers)

    def receipt(self, _offer):
        return NO_RECEIPT


class _OfferingWin32Clipboard:
    """`win32clipboard` for an announced write: the open names its window."""

    def __init__(self):
        self.calls = []
        self.opened_for = []
        self.formats = set()

    def OpenClipboard(self, hwnd=None):
        self.calls.append("open")
        self.opened_for.append(hwnd)

    def CloseClipboard(self):
        self.calls.append("close")

    def EmptyClipboard(self):
        self.calls.append("empty")
        self.formats.clear()

    def SetClipboardText(self, _text, _format):
        self.calls.append("set_text")
        self.formats.add(13)

    def IsClipboardFormatAvailable(self, format_id):
        return format_id in self.formats

    def RegisterClipboardFormat(self, _name):
        return 0


class _Win32Function:
    """A `user32` function the backend may assign `argtypes`/`restype` to."""

    def __init__(self, function):
        self._function = function
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._function(*args)


class _OfferingUser32(_FakeUser32):
    def __init__(self, clipboard, *, pid_of):
        super().__init__(clipboard.calls, sequence=7)
        self._clipboard = clipboard
        self._pid_of = pid_of
        self.SetClipboardData = _Win32Function(self._set_clipboard_data)
        self.GetWindowThreadProcessId = _Win32Function(self._get_pid)
        self.EnumChildWindows = _Win32Function(self._enum_children)
        self.announced = []

    def _set_clipboard_data(self, format_id, handle):
        self.calls.append("announce" if handle is None else "set_data")
        if handle is None:
            self.announced.append(format_id)
            self._clipboard.formats.add(format_id)

    def _get_pid(self, hwnd, pid_pointer):
        pid_pointer._obj.value = self._pid_of.get(int(hwnd), 0)
        return 1

    def _enum_children(self, _hwnd, callback, lparam):
        for child in (501, 502):
            callback(child, lparam)
        return 1


def _offering_backend(monkeypatch, owner):
    clipboard = _OfferingWin32Clipboard()
    monkeypatch.setattr(text_inserter, "win32clipboard", clipboard)
    monkeypatch.setattr(text_inserter, "win32con", SimpleNamespace(CF_UNICODETEXT=13))
    backend = Win32ClipboardBackend(owner=owner)
    backend._user32 = _OfferingUser32(clipboard, pid_of={500: 11, 501: 11, 502: 12})
    if owner is not None:
        owner.calls = clipboard.calls
    return backend, clipboard


def test_the_backend_announces_the_text_for_its_owner_window(monkeypatch):
    owner = _FakeOwner()
    backend, clipboard = _offering_backend(monkeypatch, owner)

    marker = backend.offer_clipboard_text("transcript", 500)

    assert marker == ClipboardMarker(sequence=7, offer=1)
    assert clipboard.opened_for == [9000], "EmptyClipboard made nobody the owner"
    # The offer begins after the empty: that call ends the previous offer.
    assert [call for call in clipboard.calls if call != "sequence"] == [
        "open",
        "empty",
        "begin_offer",
        "announce",
        "close",
    ]
    assert backend._user32.announced == [13]
    assert owner.offers == [("transcript", frozenset({11, 12}))]


@pytest.mark.parametrize("owner", [None, _FakeOwner(starts=False)])
def test_without_an_owner_window_the_backend_writes_plain_text(monkeypatch, owner):
    backend, clipboard = _offering_backend(monkeypatch, owner)

    marker = backend.offer_clipboard_text("transcript", 500)

    assert marker == ClipboardMarker(sequence=7, offer=None)
    assert clipboard.opened_for == [None]
    assert "set_text" in clipboard.calls and "announce" not in clipboard.calls
    assert backend.offer_intact(1) is False
    assert backend.paste_receipt(1) is NO_RECEIPT


def test_a_plain_write_never_goes_through_the_owner_window(monkeypatch):
    """ "Keep transcript in clipboard": the text must outlive this app."""
    owner = _FakeOwner()
    backend, clipboard = _offering_backend(monkeypatch, owner)

    marker = backend.set_clipboard_text("transcript")

    assert marker.offer is None
    assert clipboard.opened_for == [None]
    assert owner.offers == []
