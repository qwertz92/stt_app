"""Register a tray icon the way Electron does, to test one open question.

Opening this app's tray menu closes the Windows 11 "hidden icons" flyout while
Electron apps in the same flyout keep it open. Everything observable at menu
time was measured and ruled out (see docs/learning-log.md): the menu windows
have identical styles and no owner, both take the foreground, and activating
our notification-icon window first — which Electron does — changed nothing.

The last untested difference is the *icon registration* itself, which
``QSystemTrayIcon`` does not expose: Qt hosts the icon on a ``WS_CAPTION``
overlapped window, Electron on a bare ``WS_POPUP`` one, and the shell version
requested via ``NIM_SETVERSION`` may differ as well.

This script therefore registers an icon completely by hand, Electron-style:
a hidden ``WS_POPUP`` host window, ``NOTIFYICON_VERSION_4``, and a native
``TrackPopupMenu`` opened after ``SetForegroundWindow``. It touches nothing in
the app.

Usage (Windows)::

    python scripts/experiment_native_tray_icon.py [seconds]

A second tray icon appears (a generic application icon). Move it into the
hidden-icons flyout if it is not there already, open the flyout and right-click
it:

  * flyout stays open  -> Qt's icon registration is the cause, and only
    replacing ``QSystemTrayIcon`` with a hand-rolled implementation would fix
    the app.
  * flyout closes too  -> the cause is elsewhere entirely; stop looking here.

The icon is removed when the script exits.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys

DEFAULT_DURATION_SECONDS = 60

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

WS_POPUP = 0x80000000
WS_CLIPSIBLINGS = 0x04000000
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_CONTEXTMENU = 0x007B
WM_NULL = 0x0000
WM_APP = 0x8000
TRAY_CALLBACK_MESSAGE = WM_APP + 1

NIM_ADD = 0
NIM_DELETE = 2
NIM_SETVERSION = 4
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NOTIFYICON_VERSION_4 = 4

MF_STRING = 0x0000
TPM_RIGHTBUTTON = 0x0002
IDI_APPLICATION = 32512


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _low_word(value: int) -> int:
    return value & 0xFFFF


def _high_word(value: int) -> int:
    return (value >> 16) & 0xFFFF


def _show_menu(hwnd: int, x: int, y: int) -> None:
    """Native shortcut menu, opened the documented way."""
    menu = user32.CreatePopupMenu()
    if not menu:
        return
    user32.AppendMenuW(menu, MF_STRING, 1, "Experiment entry")
    user32.AppendMenuW(menu, MF_STRING, 2, "Another entry")
    # The documented notification-icon contract: take the foreground first so
    # the menu dismisses on an outside click, and wake the queue afterwards.
    user32.SetForegroundWindow(hwnd)
    user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, x, y, 0, hwnd, None)
    user32.PostMessageW(hwnd, WM_NULL, 0, 0)
    user32.DestroyMenu(menu)


def main() -> int:
    if sys.platform != "win32":
        print("This experiment only applies to Windows.")
        return 1

    duration = DEFAULT_DURATION_SECONDS
    if len(sys.argv) > 1:
        duration = int(float(sys.argv[1]))

    print(__doc__)
    print(f"Icon registered for {duration} seconds. Right-click it now.\n")

    def window_proc(hwnd, message, wparam, lparam):
        if message == TRAY_CALLBACK_MESSAGE:
            # NOTIFYICON_VERSION_4: the event is in the low word of lParam and
            # the screen coordinates are in wParam.
            if _low_word(lparam) == WM_CONTEXTMENU:
                _show_menu(hwnd, _low_word(wparam), _high_word(wparam))
            return 0
        if message == WM_TIMER:
            user32.PostQuitMessage(0)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    proc = WNDPROC(window_proc)
    class_name = "SttAppTrayExperimentWindow"
    instance = kernel32.GetModuleHandleW(None)

    window_class = WNDCLASSW()
    window_class.lpfnWndProc = proc
    window_class.hInstance = instance
    window_class.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        print("RegisterClassW failed:", ctypes.get_last_error())
        return 1

    # Electron's host window: a bare hidden popup, not an overlapped window.
    hwnd = user32.CreateWindowExW(
        0,
        class_name,
        "",
        WS_POPUP | WS_CLIPSIBLINGS,
        0,
        0,
        0,
        0,
        None,
        None,
        instance,
        None,
    )
    if not hwnd:
        print("CreateWindowExW failed:", ctypes.get_last_error())
        return 1

    data = NOTIFYICONDATAW()
    data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    data.hWnd = hwnd
    data.uID = 1
    data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    data.uCallbackMessage = TRAY_CALLBACK_MESSAGE
    data.hIcon = user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
    data.szTip = "stt_app tray experiment"

    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
        print("Shell_NotifyIconW(NIM_ADD) failed:", ctypes.get_last_error())
        return 1
    data.uVersion = NOTIFYICON_VERSION_4
    shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))

    user32.SetTimer(hwnd, 1, duration * 1000, None)
    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))

    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
    user32.DestroyWindow(hwnd)
    print("Icon removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
