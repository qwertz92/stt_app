from __future__ import annotations

import ctypes
import ctypes.wintypes
import time
from typing import Protocol


class WindowFocusHelper(Protocol):
    def capture_target_window(self) -> int | None: ...

    def restore_target_window(self, hwnd: int | None) -> bool: ...


class Win32WindowFocusHelper:
    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32

    def capture_target_window(self) -> int | None:
        hwnd = int(self._user32.GetForegroundWindow() or 0)
        return hwnd or None

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

