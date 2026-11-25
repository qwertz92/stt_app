"""A clipboard owner that hands the transcript out on request: a read receipt.

A SendInput Ctrl+V reports nothing back. Whether the target took the text --
or the keystroke reached a button, was swallowed by a held key, was dropped
by an elevated window, or is still waiting in a busy application's queue --
cannot be read from any Windows call made after it. What Windows does offer
is delayed rendering: a clipboard format may be announced without its data
(`SetClipboardData(format, NULL)`), and the announcing window is then asked
for it (`WM_RENDERFORMAT`) at the moment a program calls `GetClipboardData`.
That request is the receipt. `ClipboardOwner` owns a message-only window on
its own thread, answers the request with the transcript and records when it
came and from which process, so a paste can be told apart three ways:

- `RECEIPT_READ`: the first program that fetched the transcript did so after
  the keystroke and belongs to the window the keystroke went to.
- `RECEIPT_UNREAD`: nobody has fetched it. The transcript never left this
  process, so it is in no document.
- `RECEIPT_UNKNOWN`: it was fetched, but that says nothing about the target:
  before the keystroke, by another or an unidentifiable program, or by this
  app's own read. Once served, the data is on the clipboard like any other
  and later reads are invisible. Callers treat this as "no evidence".

Only "unread" is proof of anything, and of one thing: the text is in no
window. A fetch does not mean an element took the text. Measured in Edge and
Chrome (2026-11-25): the browser fetches the clipboard text a few
milliseconds after every Ctrl+V, with the focus on a text field, a button or
the page body alike, and opens the clipboard without naming a window, so
its fetch is "unknown". An Electron window (a throwaway VS Code) asked only
for a paste that landed and not at all with the focus on a button or the
page body, so there "unread" finds the paste that reached no text field.
"read" is kept for the log; nobody acts on it.

The window lives on its own thread because the request is a *sent* message:
the reader's `GetClipboardData` blocks until the window procedure returns.
On the Qt thread a paste made with `SendMessageTimeout(WM_PASTE)` would wait
for a request that thread cannot answer, and any other reader would wait
behind every long slot. The procedure here does a table lookup and one
`SetClipboardData`; it needs the interpreter lock for that long.

One kind of reader is refused: a program that copies every clipboard change
to another machine and is not the paste's target (`CLIPBOARD_SYNC_PROGRAMS`).
Measured 2026-11-24: WSLg's `msrdc.exe` asked for the text 3 ms after every
write, before any keystroke, so each receipt read "before the keystroke" and
proved nothing. A refused request leaves the format unrendered, and the next
reader is asked again. What that program loses is a transcript that is on
the clipboard for the length of one paste and replaced by the user's own
content afterwards, which it then reads as before.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import itertools
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

RECEIPT_READ = "read"
RECEIPT_UNREAD = "unread"
RECEIPT_UNKNOWN = "unknown"

# Programs that mirror the clipboard to another machine, by image name, lower
# case. Refused only while they are not the paste's own target (a Linux GUI
# application or a remote desktop in `msrdc.exe` is served like any target).
CLIPBOARD_SYNC_PROGRAMS = frozenset({"msrdc.exe"})

_LOGGER = logging.getLogger(__name__)

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_WM_QUIT = 0x0012
_WM_RENDERFORMAT = 0x0305
_WM_RENDERALLFORMATS = 0x0306
_WM_DESTROYCLIPBOARD = 0x0307
_HWND_MESSAGE = -3
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOW_CLASS_NAME = "stt_app_clipboard_owner"
_WINDOW_CLASS_IDS = itertools.count(1)
_START_TIMEOUT_S = 2.0
_CLOSE_TIMEOUT_S = 2.0
# An ended offer keeps answering for its receipt: the paste that replaced it
# asks only after its own write. One would do; a write retried inside one
# paste begins an offer per attempt.
_OFFERS_KEPT = 4
_LOG_TOKEN_MAX_CHARS = 48


def _log_token(value: str) -> str:
    """One space-free ASCII token, so an image name cannot break a log line."""
    token = "".join(
        character
        if character.isascii() and character.isprintable() and not character.isspace()
        else "_"
        for character in value[:_LOG_TOKEN_MAX_CHARS]
    )
    return token or "-"


@dataclass(frozen=True, slots=True)
class PasteReceipt:
    """What is known about who fetched one offered transcript."""

    verdict: str
    # Why, as one token: "target", "no_request", "before_keystroke",
    # "other_program", "unidentified_reader", "own_read", "no_offer".
    reason: str
    # Image name of the program that was served; "" when none or unknown.
    reader: str = ""
    # Milliseconds from the keystroke to the served request (negative: before).
    read_ms: int | None = None
    # Requests refused to clipboard sync programs.
    refused: int = 0

    def evidence(self) -> str:
        """The log fields after the verdict: names and numbers, never text."""
        return (
            f"reason={self.reason} reader={_log_token(self.reader)} "
            f"read_ms={'-' if self.read_ms is None else self.read_ms} "
            f"refused={self.refused}"
        )


NO_RECEIPT = PasteReceipt(RECEIPT_UNKNOWN, "no_offer")


@dataclass(slots=True)
class _Request:
    at: float
    pid: int
    image: str
    own: bool
    served: bool


@dataclass(slots=True)
class _Offer:
    serial: int
    text: str
    target_pids: frozenset[int]
    keystroke_at: float | None = None
    # The transcript stays on the clipboard for good (an abandoned restore):
    # from then on it is ordinary content and every reader is served.
    released: bool = False
    # Someone emptied the clipboard: the offer is over.
    destroyed: bool = False
    requests: list[_Request] = field(default_factory=list)


class _Win32OwnerApi:
    """The user32 and kernel32 calls of the owner, on handles of its own.

    Built at `start`, not at import, so the module imports where the
    controller is only linted or type-checked.
    """

    def __init__(self) -> None:
        wintypes = ctypes.wintypes
        self._lresult = ctypes.c_ssize_t
        self.wndproc_type = ctypes.WINFUNCTYPE(
            self._lresult,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = user32
        self._kernel32 = kernel32

        class _WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", self.wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        self._wndclass_type = _WNDCLASSW
        signatures = {
            "DefWindowProcW": (
                (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
                self._lresult,
            ),
            "RegisterClassW": ((ctypes.POINTER(_WNDCLASSW),), wintypes.ATOM),
            "CreateWindowExW": (
                (
                    wintypes.DWORD,
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.HWND,
                    wintypes.HMENU,
                    wintypes.HINSTANCE,
                    wintypes.LPVOID,
                ),
                wintypes.HWND,
            ),
            "DestroyWindow": ((wintypes.HWND,), wintypes.BOOL),
            "GetMessageW": (
                (
                    ctypes.POINTER(wintypes.MSG),
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.UINT,
                ),
                wintypes.BOOL,
            ),
            "DispatchMessageW": ((ctypes.POINTER(wintypes.MSG),), self._lresult),
            "PostThreadMessageW": (
                (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
                wintypes.BOOL,
            ),
            "OpenClipboard": ((wintypes.HWND,), wintypes.BOOL),
            "CloseClipboard": ((), wintypes.BOOL),
            "SetClipboardData": ((wintypes.UINT, wintypes.HANDLE), wintypes.HANDLE),
            "GetClipboardOwner": ((), wintypes.HWND),
            "GetOpenClipboardWindow": ((), wintypes.HWND),
            "GetWindowThreadProcessId": (
                (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)),
                wintypes.DWORD,
            ),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(user32, name)
            function.argtypes = argtypes
            function.restype = restype
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetCurrentThreadId.argtypes = ()
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

    def current_thread_id(self) -> int:
        return int(self._kernel32.GetCurrentThreadId())

    def create_window(self, wndproc) -> int:
        """A message-only window with `wndproc`; 0 when Windows refuses."""
        instance = self._kernel32.GetModuleHandleW(None)
        # A class of its own per window: a class keeps the procedure it was
        # registered with, so a second owner in one process under one name
        # would be called through the first one's thunk, alive or freed.
        class_name = f"{_WINDOW_CLASS_NAME}_{next(_WINDOW_CLASS_IDS)}"
        window_class = self._wndclass_type()
        window_class.lpfnWndProc = wndproc
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        if not self._user32.RegisterClassW(ctypes.byref(window_class)):
            return 0
        return int(
            self._user32.CreateWindowExW(
                0,
                class_name,
                class_name,
                0,
                0,
                0,
                0,
                0,
                ctypes.wintypes.HWND(_HWND_MESSAGE),
                None,
                instance,
                None,
            )
            or 0
        )

    def pump_messages(self) -> None:
        message = ctypes.wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            self._user32.DispatchMessageW(ctypes.byref(message))

    def destroy_window(self, hwnd: int) -> None:
        self._user32.DestroyWindow(hwnd)

    def post_quit(self, thread_id: int) -> None:
        self._user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)

    def default_window_proc(self, hwnd, message, wparam, lparam):
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def clipboard_owner(self) -> int:
        return int(self._user32.GetClipboardOwner() or 0)

    def reader_pid(self) -> int:
        """The process that has the clipboard open; 0 when it named no window."""
        opener = self._user32.GetOpenClipboardWindow()
        if not opener:
            return 0
        pid = ctypes.wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(opener, ctypes.byref(pid))
        return int(pid.value)

    def process_image(self, pid: int) -> str:
        """The executable's file name in lower case; "" when unreadable."""
        if not pid:
            return ""
        handle = self._kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return ""
        try:
            size = ctypes.wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(1024)
            if not self._kernel32.QueryFullProcessImageNameW(
                handle, 0, path, ctypes.byref(size)
            ):
                return ""
            return path.value.rsplit("\\", 1)[-1].lower()
        finally:
            self._kernel32.CloseHandle(handle)

    def set_text(self, format_id: int, text: str) -> bool:
        """`SetClipboardData` with `text`; the clipboard is open already."""
        payload = (text + "\0").encode("utf-16-le")
        handle = self._kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(payload))
        if not handle:
            return False
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            self._kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, payload, len(payload))
        self._kernel32.GlobalUnlock(handle)
        if self._user32.SetClipboardData(format_id, handle):
            return True
        # Refused, so the block is still ours to free.
        self._kernel32.GlobalFree(handle)
        return False

    def open_clipboard(self, hwnd: int) -> bool:
        return bool(self._user32.OpenClipboard(hwnd))

    def close_clipboard(self) -> None:
        self._user32.CloseClipboard()


class ClipboardOwner:
    """Owns the clipboard for one offered transcript at a time.

    `start` is called before the first offer and answers whether the window
    exists; without it the caller writes the transcript as plain data and has
    no receipt. Every method may be called from any thread.
    """

    def __init__(
        self,
        *,
        api_factory: Callable[[], object] | None = None,
        clock: Callable[[], float] = time.perf_counter,
        sync_programs: frozenset[str] = CLIPBOARD_SYNC_PROGRAMS,
    ) -> None:
        self._api_factory = api_factory or _Win32OwnerApi
        self._api = None
        # `perf_counter`: a request a millisecond before the keystroke must
        # not read as after it, and `monotonic` ticks every 15.6 ms.
        self._clock = clock
        self._sync_programs = sync_programs
        self._lock = threading.Lock()
        # The offer on the clipboard, and the last few by serial.
        self._offer: _Offer | None = None
        self._offers: dict[int, _Offer] = {}
        self._serial = 0
        # Reads this app makes itself (a capture, a check, a restore): the
        # request they cause is no receipt.
        self._own_reads = 0
        self._hwnd = 0
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._started = False
        self._closed = False
        # Kept so the window procedure's thunk outlives the window.
        self._wndproc = None

    # -- Lifecycle -------------------------------------------------------------

    def start(self) -> bool:
        """Create the window on its own thread; False when that fails."""
        with self._lock:
            if self._closed:
                return False
            if not self._started:
                self._started = True
                thread = threading.Thread(
                    target=self._run, name="stt_app_clipboard_owner", daemon=True
                )
                try:
                    thread.start()
                except Exception:
                    _LOGGER.warning("clipboard_owner_unavailable reason=thread")
                    self._ready.set()
                else:
                    self._thread = thread
        self._ready.wait(_START_TIMEOUT_S)
        return bool(self._hwnd)

    @property
    def hwnd(self) -> int:
        return self._hwnd

    def close(self) -> None:
        """End the thread; a transcript still unrendered is written out first
        (`WM_RENDERALLFORMATS`, sent while the window is destroyed)."""
        with self._lock:
            self._closed = True
            thread, thread_id, api = self._thread, self._thread_id, self._api
        if thread is None or api is None or not thread_id:
            return
        try:
            api.post_quit(thread_id)
        except Exception:
            _LOGGER.debug("clipboard_owner post_quit failed", exc_info=True)
            return
        thread.join(_CLOSE_TIMEOUT_S)

    def _run(self) -> None:
        try:
            api = self._api_factory()
            # Before the window exists: Windows calls the procedure from
            # inside `CreateWindowEx`, and a creation message answered with
            # an exception left the owner without a window.
            self._api = api
            self._wndproc = api.wndproc_type(self._window_proc)
            hwnd = api.create_window(self._wndproc)
        except Exception:
            _LOGGER.exception("clipboard_owner_unavailable reason=window")
            self._ready.set()
            return
        if not hwnd:
            _LOGGER.warning("clipboard_owner_unavailable reason=create_window")
            self._ready.set()
            return
        with self._lock:
            self._thread_id = api.current_thread_id()
            self._hwnd = hwnd
        self._ready.set()
        try:
            api.pump_messages()
        finally:
            try:
                api.destroy_window(hwnd)
            except Exception:
                _LOGGER.debug("clipboard_owner destroy failed", exc_info=True)
            self._hwnd = 0

    def _window_proc(self, hwnd, message, wparam, lparam):
        # Nothing may leave this function as an exception: ctypes would print
        # it and answer 0, and the reader behind the request would get no
        # data without a line in the log saying why.
        try:
            if message == _WM_RENDERFORMAT:
                self._on_render(int(wparam))
                return 0
            if message == _WM_RENDERALLFORMATS:
                self._on_render_all()
                return 0
            if message == _WM_DESTROYCLIPBOARD:
                self._on_destroy()
                return 0
        except Exception:
            _LOGGER.exception("clipboard_owner window procedure failed")
            return 0
        return self._api.default_window_proc(hwnd, message, wparam, lparam)

    # -- Requests (the window's thread) ----------------------------------------

    def _on_render(self, format_id: int) -> None:
        """A program called `GetClipboardData` for the offered transcript."""
        if format_id != _CF_UNICODETEXT:
            return
        api = self._api
        now = self._clock()
        pid = api.reader_pid()
        image = api.process_image(pid)
        with self._lock:
            offer = self._offer
            if offer is None or offer.destroyed:
                return
            own = self._own_reads > 0
            served = (
                own
                or offer.released
                or not (image in self._sync_programs and pid not in offer.target_pids)
            )
            offer.requests.append(_Request(now, pid, image, own, served))
            text = offer.text
        if served and not api.set_text(format_id, text):
            _LOGGER.warning(
                "clipboard_owner_render_failed reader=%s", _log_token(image)
            )

    def _on_render_all(self) -> None:
        """The window is going away while it still owes the transcript."""
        api = self._api
        with self._lock:
            offer = self._offer
            if offer is None or offer.destroyed:
                return
            if any(request.served for request in offer.requests):
                return
            text = offer.text
        if not api.open_clipboard(self._hwnd):
            return
        try:
            if api.clipboard_owner() == self._hwnd:
                api.set_text(_CF_UNICODETEXT, text)
        finally:
            api.close_clipboard()

    def _on_destroy(self) -> None:
        """Someone -- this app included -- emptied the clipboard."""
        with self._lock:
            if self._offer is not None:
                self._offer.destroyed = True

    # -- Offers (any thread) ---------------------------------------------------

    def begin_offer(self, text: str, target_pids: frozenset[int]) -> int:
        """Announce `text`; call inside the open clipboard, after emptying it.

        After `EmptyClipboard`, because that call tells the previous owner
        -- this window, when the last transcript is still there -- that its
        clipboard is gone, and the message must find the previous offer.
        """
        with self._lock:
            if self._offer is not None:
                self._offer.destroyed = True
            self._serial += 1
            offer = _Offer(self._serial, text, frozenset(target_pids))
            self._offer = offer
            self._offers[offer.serial] = offer
            for ended in sorted(self._offers)[:-_OFFERS_KEPT]:
                del self._offers[ended]
            return offer.serial

    def _known(self, serial: int | None) -> _Offer | None:
        """The offer of `serial`, on the clipboard or ended; lock held."""
        return None if serial is None else self._offers.get(serial)

    def note_keystroke(self, serial: int | None) -> None:
        """The paste keystroke for this offer goes out now."""
        with self._lock:
            offer = self._known(serial)
            if offer is not None:
                offer.keystroke_at = self._clock()

    def release(self, serial: int | None) -> None:
        """The transcript stays on the clipboard for good: serve everyone."""
        with self._lock:
            offer = self._known(serial)
            if offer is not None:
                offer.released = True

    def is_intact(self, serial: int | None) -> bool:
        """Whether the clipboard still is this offer, untouched by others."""
        with self._lock:
            offer = self._known(serial)
            if offer is None or offer.destroyed:
                return False
            api, hwnd = self._api, self._hwnd
        if api is None or not hwnd:
            return False
        try:
            return api.clipboard_owner() == hwnd
        except Exception:
            return False

    def receipt(self, serial: int | None) -> PasteReceipt:
        """Who fetched the offer of `serial`, as far as it is known by now.

        Final once the offer has ended: nothing is recorded for it after the
        clipboard was emptied.
        """
        with self._lock:
            offer = self._known(serial)
            if offer is None:
                return NO_RECEIPT
            keystroke_at = offer.keystroke_at
            target_pids = offer.target_pids
            requests = list(offer.requests)
        refused = sum(1 for request in requests if not request.served)
        first = next((request for request in requests if request.served), None)
        if first is None:
            return PasteReceipt(RECEIPT_UNREAD, "no_request", refused=refused)
        read_ms = (
            None if keystroke_at is None else round((first.at - keystroke_at) * 1000)
        )
        if first.own:
            verdict, reason = RECEIPT_UNKNOWN, "own_read"
        elif keystroke_at is None or first.at < keystroke_at:
            verdict, reason = RECEIPT_UNKNOWN, "before_keystroke"
        elif first.pid in target_pids:
            verdict, reason = RECEIPT_READ, "target"
        elif not first.pid:
            verdict, reason = RECEIPT_UNKNOWN, "unidentified_reader"
        else:
            verdict, reason = RECEIPT_UNKNOWN, "other_program"
        return PasteReceipt(
            verdict, reason, reader=first.image, read_ms=read_ms, refused=refused
        )

    @contextlib.contextmanager
    def own_read(self) -> Iterator[None]:
        """Around this app's own clipboard reads: their request is no receipt."""
        with self._lock:
            self._own_reads += 1
        try:
            yield
        finally:
            with self._lock:
                self._own_reads -= 1
