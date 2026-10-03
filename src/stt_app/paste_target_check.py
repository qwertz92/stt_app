"""Report-only check whether a paste went into a text field.

A SendInput Ctrl+V goes to whatever has the keyboard focus, and Windows never
says whether that element took the text: a Chromium page with a focused
button, list item or nothing at all fires its paste event, the transaction
reports success and the transcript reaches no document (the r27 paste
investigation, 2026-09-27). After the keystroke, `PasteTargetCheck` asks on a
worker thread whether the focused element shows a caret:

- `GetGUIThreadInfo` names the caret window of the foreground thread for
  every Win32 caret (`CreateCaret`): EDIT, RichEdit and their descendants.
  A caret there is `VERDICT_TEXT_FIELD` in any window.
- MSAA `OBJID_CARET` on the focus window, asked only of Chromium windows
  (Edge, Chrome and every Electron app): Chromium answers it for a focused
  input, textarea, contenteditable or EditContext element with a visible
  caret one pixel wide, and for anything else with an invisible caret of
  width 0, without switching on its accessibility tree. Both saying "no
  caret" there is `VERDICT_NOT_TEXT_FIELD`.

"No Win32 caret" proves nothing about any other window: Windows Terminal
draws its own cursor and answers MSAA with an invisible caret of width 0
while its prompt takes every paste (measured 2026-10-03), and so would any
application that draws its own caret. Those, and everything the check cannot
read -- an MSAA error, no foreground, a foreground that changed -- are
`VERDICT_UNKNOWN`, which the caller treats exactly as before the check
existed. The check never blocks or delays the paste, and never runs on the
Qt thread: the MSAA call is a cross-process `WM_GETOBJECT`, which a hung
target answers late or never.

UI Automation is deliberately not used: a fresh Edge answered its first
query with the wrong element, it calls a contenteditable a Group without a
value pattern and the page body an editable Document, and a UIA client can
switch an Electron app such as VS Code into its screen-reader mode.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .config import PASTE_TARGET_CHECK_RECHECK_DELAYS_S
from .window_focus import CHROMIUM_WINDOW_CLASSES, GUITHREADINFO, window_class_name

VERDICT_TEXT_FIELD = "text_field"
VERDICT_NOT_TEXT_FIELD = "not_text_field"
VERDICT_UNKNOWN = "unknown"

_LOGGER = logging.getLogger(__name__)

_OBJID_CARET = 0xFFFFFFF8
_CHILDID_SELF = 0
_STATE_SYSTEM_INVISIBLE = 0x00008000
_VT_I4 = 3
_COINIT_MULTITHREADED = 0x0
# IAccessible vtable slots (IUnknown 0-2, IDispatch 3-6, then oleacc.h order).
_VTBL_RELEASE = 2
_VTBL_GET_ACC_STATE = 14
_VTBL_ACC_LOCATION = 22


@dataclass(frozen=True, slots=True)
class CaretReading:
    """One reading of the foreground's caret, for the verdict and the log."""

    verdict: str
    foreground: int | None
    # Short ASCII evidence for the log line, e.g. "gui=none msaa=invisible w=0".
    detail: str


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# {618736E0-3C3D-11CF-810C-00AA00389B71}
_IID_IACCESSIBLE = _GUID(
    0x618736E0,
    0x3C3D,
    0x11CF,
    (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71),
)


class _VARIANT_VALUE(ctypes.Union):
    # Sized like the real union: two pointers wide (BRECORD) on x64.
    _fields_ = [
        ("lVal", ctypes.c_long),
        ("llVal", ctypes.c_longlong),
        ("record", ctypes.c_void_p * 2),
    ]


class _VARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("reserved1", ctypes.c_ushort),
        ("reserved2", ctypes.c_ushort),
        ("reserved3", ctypes.c_ushort),
        ("value", _VARIANT_VALUE),
    ]


def _child_self() -> _VARIANT:
    variant = _VARIANT()
    variant.vt = _VT_I4
    variant.value.lVal = _CHILDID_SELF
    return variant


class _Win32CaretReader:
    """Reads the foreground's caret through user32 and oleacc.

    Everything Windows-only (`WinDLL`, `WINFUNCTYPE`, `HRESULT`) is built
    here rather than at import, so the module still imports where the
    controller is only linted or type-checked.
    """

    def __init__(self) -> None:
        wintypes = ctypes.wintypes
        self._release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        self._get_acc_state = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, _VARIANT, ctypes.POINTER(_VARIANT)
        )
        self._acc_location = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_long),
            ctypes.POINTER(ctypes.c_long),
            ctypes.POINTER(ctypes.c_long),
            ctypes.POINTER(ctypes.c_long),
            _VARIANT,
        )
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetForegroundWindow.argtypes = ()
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.c_void_p)
        self._user32.GetGUIThreadInfo.restype = wintypes.BOOL
        self._oleacc = ctypes.WinDLL("oleacc")
        self._oleacc.AccessibleObjectFromWindow.argtypes = (
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._oleacc.AccessibleObjectFromWindow.restype = ctypes.HRESULT
        self._oleaut32 = ctypes.WinDLL("oleaut32")
        self._oleaut32.VariantClear.argtypes = (ctypes.POINTER(_VARIANT),)
        self._oleaut32.VariantClear.restype = ctypes.HRESULT

    def foreground(self) -> int | None:
        return int(self._user32.GetForegroundWindow() or 0) or None

    def gui_caret(self, foreground: int) -> tuple[bool, int | None] | None:
        """(caret window present, focus window), or None when unreadable."""
        thread_id = int(self._user32.GetWindowThreadProcessId(foreground, None) or 0)
        if not thread_id:
            return None
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return None
        return bool(info.hwndCaret), int(info.hwndFocus or 0) or None

    def window_class(self, hwnd: int) -> str:
        return window_class_name(self._user32, hwnd)

    def msaa_caret(self, hwnd: int) -> tuple[bool, int] | None:
        """(caret invisible, caret width), or None when MSAA did not answer."""
        pointer = ctypes.c_void_p()
        try:
            self._oleacc.AccessibleObjectFromWindow(
                hwnd,
                _OBJID_CARET,
                ctypes.byref(_IID_IACCESSIBLE),
                ctypes.byref(pointer),
            )
        except OSError:
            return None
        if not pointer.value:
            return None
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[
            0
        ]
        try:
            state = _VARIANT()
            try:
                self._get_acc_state(vtable[_VTBL_GET_ACC_STATE])(
                    pointer, _child_self(), ctypes.byref(state)
                )
                if state.vt != _VT_I4:
                    return None
                invisible = bool(state.value.lVal & _STATE_SYSTEM_INVISIBLE)
            finally:
                self._oleaut32.VariantClear(ctypes.byref(state))
            left, top, width, height = (ctypes.c_long() for _ in range(4))
            self._acc_location(vtable[_VTBL_ACC_LOCATION])(
                pointer,
                ctypes.byref(left),
                ctypes.byref(top),
                ctypes.byref(width),
                ctypes.byref(height),
                _child_self(),
            )
            return invisible, int(width.value)
        except OSError:
            return None
        finally:
            self._release(vtable[_VTBL_RELEASE])(pointer)


def read_focused_caret(
    reader: _Win32CaretReader, expected_foreground: int | None = None
) -> CaretReading:
    """One reading: does the foreground's focused element show a caret?"""
    foreground = reader.foreground()
    if foreground is None:
        return CaretReading(VERDICT_UNKNOWN, None, "no foreground")
    if expected_foreground and foreground != expected_foreground:
        return CaretReading(VERDICT_UNKNOWN, foreground, "foreground changed")
    gui = reader.gui_caret(foreground)
    if gui is None:
        return CaretReading(VERDICT_UNKNOWN, foreground, "gui=unreadable")
    has_caret_window, focus = gui
    if has_caret_window:
        return CaretReading(VERDICT_TEXT_FIELD, foreground, "gui=caret")
    focus = focus or foreground
    window_class = reader.window_class(focus)
    if window_class not in CHROMIUM_WINDOW_CLASSES:
        # Its own caret, if it draws one, is invisible to both sources.
        return CaretReading(
            VERDICT_UNKNOWN, foreground, f"gui=none class={window_class[:40]}"
        )
    msaa = reader.msaa_caret(focus)
    if msaa is None:
        return CaretReading(VERDICT_UNKNOWN, foreground, "gui=none msaa=unanswered")
    invisible, width = msaa
    detail = f"gui=none msaa={'invisible' if invisible else 'visible'} w={width}"
    if invisible or width <= 0:
        return CaretReading(VERDICT_NOT_TEXT_FIELD, foreground, detail)
    return CaretReading(VERDICT_TEXT_FIELD, foreground, detail)


def check_paste_target(
    read: Callable[[int | None], CaretReading],
    *,
    expected_foreground: int | None,
    recheck_delays_s: Sequence[float],
    sleep: Callable[[float], None] = time.sleep,
) -> CaretReading:
    """Read the caret, and read again while it says "no caret".

    "Not a text field" stands only when every reading agrees on the same
    foreground; a caret in any reading, or any unknown one, decides at once.
    """
    reading = read(expected_foreground)
    reference = expected_foreground or reading.foreground
    for delay_s in recheck_delays_s:
        if reading.verdict != VERDICT_NOT_TEXT_FIELD:
            return reading
        sleep(delay_s)
        reading = read(reference)
    return reading


class PasteTargetCheck:
    """Runs `check_paste_target` on one worker thread, one check at a time.

    `request` never waits: it answers False when the platform cannot check
    or a check is still running -- a hung target holds the worker inside its
    `WM_GETOBJECT` -- and the caller then behaves as if no check existed.
    The callback runs on the worker thread with the final `CaretReading`.
    """

    def __init__(
        self,
        *,
        recheck_delays_s: Sequence[float] = PASTE_TARGET_CHECK_RECHECK_DELAYS_S,
        reader_factory: Callable[[], object] | None = None,
    ) -> None:
        self._recheck_delays_s = tuple(recheck_delays_s)
        self._reader_factory = reader_factory or _Win32CaretReader
        self._lock = threading.Lock()
        self._busy = False
        self._closed = False
        self._requests: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None

    def request(
        self,
        callback: Callable[[CaretReading], None],
        *,
        expected_foreground: int | None = None,
    ) -> bool:
        with self._lock:
            if self._closed or self._busy:
                return False
            if self._thread is None:
                thread = threading.Thread(
                    target=self._run, name="stt_app_paste_target_check", daemon=True
                )
                try:
                    thread.start()
                except RuntimeError:
                    _LOGGER.warning("paste_target_check_unavailable reason=thread")
                    return False
                self._thread = thread
            self._busy = True
        self._requests.put((callback, expected_foreground))
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._requests.put(None)

    def _run(self) -> None:
        initialized = _initialize_com_multithreaded()
        try:
            reader = self._reader_factory()
        except Exception:
            _LOGGER.exception("paste_target_check_unavailable reason=reader")
            reader = None
        try:
            while True:
                item = self._requests.get()
                if item is None:
                    return
                callback, expected_foreground = item
                try:
                    reading = self._check(reader, expected_foreground)
                finally:
                    with self._lock:
                        self._busy = False
                try:
                    callback(reading)
                except Exception:
                    _LOGGER.exception("paste_target_check callback failed")
        finally:
            if initialized:
                _uninitialize_com()

    def _check(self, reader, expected_foreground: int | None) -> CaretReading:
        if reader is None:
            return CaretReading(VERDICT_UNKNOWN, None, "reader unavailable")
        try:
            return check_paste_target(
                lambda expected: read_focused_caret(reader, expected),
                expected_foreground=expected_foreground,
                recheck_delays_s=self._recheck_delays_s,
            )
        except Exception as exc:
            # Report-only: whatever went wrong, the paste stands as reported.
            return CaretReading(VERDICT_UNKNOWN, None, f"error={type(exc).__name__}")


def _initialize_com_multithreaded() -> bool:
    """MTA for the worker: the MSAA object is marshalled from the target."""
    if sys.platform != "win32":
        return False
    try:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.wintypes.DWORD)
        ole32.CoInitializeEx.restype = ctypes.c_long
        return ole32.CoInitializeEx(None, _COINIT_MULTITHREADED) >= 0
    except Exception:
        _LOGGER.exception("paste_target_check CoInitializeEx failed")
        return False


def _uninitialize_com() -> None:
    try:
        ctypes.WinDLL("ole32").CoUninitialize()
    except Exception:
        _LOGGER.debug("CoUninitialize failed", exc_info=True)
