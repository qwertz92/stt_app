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


class Win32WindowFocusHelper:
    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._own_process_id = int(ctypes.windll.kernel32.GetCurrentProcessId())
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
        if self._is_own_non_target_window(hwnd):
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
            if hwnd and not self._is_own_non_target_window(hwnd):
                self._last_foreign_window = hwnd
        except Exception:
            return

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

