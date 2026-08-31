from __future__ import annotations

import ctypes
import ctypes.wintypes
import time
from typing import Protocol

FocusSignature = tuple[int | None, int | None, int | None]


class WindowFocusHelper(Protocol):
    def capture_target_window(self) -> int | None: ...

    def get_foreground_window(self) -> int | None: ...

    def get_focus_window(self) -> int | None: ...

    def get_caret_window(self) -> int | None: ...

    def capture_target_signature(self) -> FocusSignature: ...

    def get_focus_signature(self) -> FocusSignature: ...

    def restore_target_window(self, hwnd: int | None) -> bool: ...


_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
# Windows shell surfaces that can hold the foreground and can never take
# dictated text. They matter because clicking the tray icon activates the
# taskbar itself: without this, the foreground sampled at that moment is
# `Shell_TrayWnd`, it is remembered as the last foreign window, and
# `restore_target_window` then raises the taskbar and pastes into nothing --
# after overwriting the editor handle that was correctly remembered before.
# The own-process tool-window test cannot catch them: it returns early for a
# foreign PID, and every one of these belongs to explorer.exe. Measured on
# this Windows 11 desktop, all nine were accepted as valid paste targets.
_SHELL_SURFACE_CLASSES = frozenset(
    {
        "Shell_TrayWnd",  # the taskbar
        "Shell_SecondaryTrayWnd",  # the taskbar on further monitors
        "NotifyIconOverflowWindow",  # the classic hidden-icons flyout
        "TopLevelWindowForOverflowXamlIsland",  # its Windows 11 replacement
        "Progman",  # the desktop
        "WorkerW",  # the desktop's wallpaper host
        "XamlExplorerHostIslandWindow",  # Task View / Alt+Tab
        "MultitaskingViewFrame",  # Task View, older builds
        "ForegroundStaging",  # transient, during foreground animations
    }
)
_WINDOW_CLASS_BUFFER_CHARS = 256


def _declare_user32(user32) -> None:
    """Declare every signature this module calls.

    Two reasons, and only the second is about this module's own correctness:

    - `ctypes.windll.user32` is a process-wide cached handle, so declaring on
      it would silently redefine the same function objects for every other
      caller in the process. `text_inserter` and `win_tray_icon` each take
      their own `WinDLL`; this module now does too, and the declarations
      cannot leak out of it.
    - Without `restype`, ctypes reads a 32-bit signed int, so an `HWND` at or
      above 0x8000_0000 comes back negative and is then passed back in
      sign-extended -- a different window. Measured on this machine, real
      handles are far below that (0x30766) and declared and undeclared calls
      agree exactly, so this is hardening rather than a fix for anything
      observed; a 64-bit handle is the one case that already fails outright
      ("int too long to convert"), and Windows does not produce one.
    """
    wintypes = ctypes.wintypes
    signatures = {
        "GetForegroundWindow": ((), wintypes.HWND),
        "SetForegroundWindow": ((wintypes.HWND,), wintypes.BOOL),
        "IsWindow": ((wintypes.HWND,), wintypes.BOOL),
        "IsWindowVisible": ((wintypes.HWND,), wintypes.BOOL),
        "ShowWindow": ((wintypes.HWND, ctypes.c_int), wintypes.BOOL),
        "GetWindowLongW": ((wintypes.HWND, ctypes.c_int), wintypes.LONG),
        "GetWindowThreadProcessId": (
            (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)),
            wintypes.DWORD,
        ),
        "GetGUIThreadInfo": ((wintypes.DWORD, ctypes.c_void_p), wintypes.BOOL),
        "GetClassNameW": (
            (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int),
            ctypes.c_int,
        ),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(user32, name, None)
        if function is None:
            continue
        function.argtypes = argtypes
        function.restype = restype


class Win32WindowFocusHelper:
    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        _declare_user32(self._user32)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcessId.argtypes = ()
        kernel32.GetCurrentProcessId.restype = ctypes.wintypes.DWORD
        self._own_process_id = int(kernel32.GetCurrentProcessId())
        self._last_foreign_window: int | None = None

    def capture_target_window(self) -> int | None:
        return self.get_foreground_window()

    def capture_target_signature(self) -> FocusSignature:
        return self.get_focus_signature()

    def get_foreground_window(self) -> int | None:
        """Foreground window, skipping our own popups.

        Opening the tray menu activates our notification-icon window and then
        our menu, so at that moment the foreground belongs to us and the
        transcript would be aimed at a window that cannot take text. Remember
        the last window that belonged to another application and use it
        instead. Only our *popups and hidden helper windows* are skipped: the
        Settings dialog is a normal visible window and stays a valid dictation
        target.
        """
        hwnd = int(self._user32.GetForegroundWindow() or 0)
        if not hwnd:
            return self._remembered_foreign_window()
        if not self._is_possible_target_window(hwnd):
            # No `or hwnd`. Handing our own window back made it the dictation
            # target whenever nothing foreign had been remembered yet, which
            # on a fresh session is every path that has not started a
            # recording -- the first dictation started from the tray menu, for
            # one, since the notification-icon contract has us call
            # `SetForegroundWindow` on the hidden host window before the menu
            # opens. Worse than losing that paste: `restore_target_window`
            # then calls `ShowWindow(SW_SHOW)` on the helper window, which
            # makes it visible and therefore a *valid* target, and it is
            # cached as the last foreign window for the rest of the session.
            # `None` means "no remembered target", which the insert path
            # reports rather than pasting into nothing.
            return self._remembered_foreign_window()
        self._last_foreign_window = hwnd
        return hwnd

    def note_foreground_window(self) -> None:
        """Remember the current foreground window while it is still someone's.

        Called before we deliberately take the foreground ourselves -- opening
        the tray menu does exactly that -- so the window the user was working
        in is available afterwards. Best-effort: an own or missing window is
        simply not recorded.
        """
        try:
            hwnd = int(self._user32.GetForegroundWindow() or 0)
            if hwnd and self._is_possible_target_window(hwnd):
                self._last_foreign_window = hwnd
        except Exception:
            return

    def _is_possible_target_window(self, hwnd: int) -> bool:
        """Could this window plausibly receive a pasted transcript?"""
        return not self._is_own_non_target_window(
            hwnd
        ) and not self._is_shell_surface(hwnd)

    def _window_class_name(self, hwnd: int) -> str:
        get_class_name = getattr(self._user32, "GetClassNameW", None)
        if get_class_name is None:
            return ""
        buffer = ctypes.create_unicode_buffer(_WINDOW_CLASS_BUFFER_CHARS)
        try:
            get_class_name(hwnd, buffer, _WINDOW_CLASS_BUFFER_CHARS)
        except Exception:
            return ""
        return buffer.value

    def _is_shell_surface(self, hwnd: int) -> bool:
        return self._window_class_name(hwnd) in _SHELL_SURFACE_CLASSES

    def _is_own_non_target_window(self, hwnd: int) -> bool:
        process_id = ctypes.wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if int(process_id.value) != self._own_process_id:
            return False
        if not self._user32.IsWindowVisible(hwnd):
            # The tray icon's helper window, activated while the menu opens.
            return True
        style = int(self._user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) or 0)
        return bool(style & _WS_EX_TOOLWINDOW)

    def _remembered_foreign_window(self) -> int | None:
        remembered = self._last_foreign_window
        if remembered and self._user32.IsWindow(remembered):
            return remembered
        self._last_foreign_window = None
        return None

    def get_focus_signature(self) -> FocusSignature:
        foreground = self.get_foreground_window()
        focus, caret = self._read_gui_thread_info(foreground)
        effective_focus = focus or foreground
        effective_caret = caret or effective_focus
        return foreground, effective_focus, effective_caret

    def get_focus_window(self) -> int | None:
        foreground = self.get_foreground_window()
        focus, _caret = self._read_gui_thread_info(foreground)
        return focus or foreground

    def get_caret_window(self) -> int | None:
        foreground = self.get_foreground_window()
        focus, caret = self._read_gui_thread_info(foreground)
        return caret or focus or foreground

    def _read_gui_thread_info(self, foreground: int | None) -> tuple[int | None, int | None]:
        if not foreground:
            return None, None

        thread_id = int(self._user32.GetWindowThreadProcessId(foreground, None) or 0)
        if thread_id == 0:
            return foreground, foreground

        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        ok = bool(self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)))
        if not ok:
            return foreground, foreground
        focus = int(info.hwndFocus or 0) or foreground
        caret = int(info.hwndCaret or 0) or focus
        return focus, caret

    def restore_target_window(self, hwnd: int | None) -> bool:
        if not hwnd:
            return False

        if not self._user32.IsWindow(hwnd):
            return False

        current = int(self._user32.GetForegroundWindow() or 0)
        if current == hwnd:
            return True

        # Best-effort foreground restore before pasting.
        self._user32.ShowWindow(hwnd, 5)  # SW_SHOW
        ok = bool(self._user32.SetForegroundWindow(hwnd))
        time.sleep(0.03)
        return ok


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("hwndActive", ctypes.wintypes.HWND),
        ("hwndFocus", ctypes.wintypes.HWND),
        ("hwndCapture", ctypes.wintypes.HWND),
        ("hwndMenuOwner", ctypes.wintypes.HWND),
        ("hwndMoveSize", ctypes.wintypes.HWND),
        ("hwndCaret", ctypes.wintypes.HWND),
        ("rcCaret", ctypes.wintypes.RECT),
    ]

