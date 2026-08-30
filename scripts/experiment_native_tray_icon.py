"""Register tray icons by hand to isolate why the flyout closes.

Opening this app's tray menu closes the Windows 11 "hidden icons" flyout while
Electron apps in the same flyout keep it open. Everything observable at menu
time was measured and ruled out (see docs/learning-log.md). A first run of this
experiment showed that an icon registered by hand -- Electron-style: a bare
hidden ``WS_POPUP`` host window, ``NOTIFYICON_VERSION_4``, native
``TrackPopupMenu`` -- keeps the flyout open. So the *icon registration* is the
cause, not anything the menu does.

That first run changed two things at once, though: the registration **and** the
menu type. This version therefore registers TWO icons on the same hand-made
window and differs only in the menu:

  * icon 1 -- native ``TrackPopupMenu`` (what Electron uses)
  * icon 2 -- a Qt ``QMenu``, the app's existing menu type

It runs inside a real Qt event loop, exactly like the app does.

If icon 2 also keeps the flyout open, only the registration has to be replaced
and the app's whole menu stays as it is. If only icon 1 keeps it open, the menu
has to become native too.

Usage (Windows)::

    python scripts/experiment_native_tray_icon.py [seconds]

Both icons appear in the tray (generic application icon; their tooltips say
which is which). Move them into the hidden-icons flyout if needed, then
right-click each one and watch whether the flyout stays open.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys

from PySide6 import QtCore, QtWidgets

DEFAULT_DURATION_SECONDS = 90

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

NATIVE_MENU_ICON_ID = 1
QT_MENU_ICON_ID = 2


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


def _signed_word(value: int) -> int:
    """Read a packed 16-bit field as signed, like ``GET_X_LPARAM``.

    Mirrors ``win_tray_icon.signed_word``. A monitor left of the
    primary gives negative screen coordinates, and read unsigned an
    x of -1200 arrives as 64336, putting the menu off-screen.
    """
    packed = int(value) & 0xFFFF
    return packed - 0x10000 if packed & 0x8000 else packed


def _show_native_menu(hwnd: int, x: int, y: int) -> None:
    menu = user32.CreatePopupMenu()
    if not menu:
        return
    user32.AppendMenuW(menu, MF_STRING, 1, "Native menu entry")
    user32.AppendMenuW(menu, MF_STRING, 2, "Another entry")
    # The documented notification-icon contract.
    user32.SetForegroundWindow(hwnd)
    user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, x, y, 0, hwnd, None)
    user32.PostMessageW(hwnd, WM_NULL, 0, 0)
    user32.DestroyMenu(menu)


def _add_icon(hwnd: int, icon_id: int, tip: str) -> NOTIFYICONDATAW:
    data = NOTIFYICONDATAW()
    data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    data.hWnd = hwnd
    data.uID = icon_id
    data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    data.uCallbackMessage = TRAY_CALLBACK_MESSAGE
    data.hIcon = user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
    data.szTip = tip
    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
        raise OSError(
            f"Shell_NotifyIconW(NIM_ADD) failed: {ctypes.get_last_error()}"
        )
    data.uVersion = NOTIFYICON_VERSION_4
    shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
    return data


def main() -> int:
    if sys.platform != "win32":
        print("This experiment only applies to Windows.")
        return 1

    duration = DEFAULT_DURATION_SECONDS
    if len(sys.argv) > 1:
        duration = int(float(sys.argv[1]))

    print(__doc__)
    print(f"Both icons registered for {duration} seconds.\n")

    app = QtWidgets.QApplication(sys.argv[:1])
    qt_menu = QtWidgets.QMenu()
    qt_menu.addAction("Qt menu entry")
    qt_menu.addAction("Another entry")

    def window_proc(hwnd, message, wparam, lparam):
        if message == TRAY_CALLBACK_MESSAGE:
            # NOTIFYICON_VERSION_4: event in the low word of lParam, icon id in
            # its high word, screen coordinates in wParam.
            if _low_word(lparam) == WM_CONTEXTMENU:
                x, y = _signed_word(wparam), _signed_word(int(wparam) >> 16)
                if _high_word(lparam) == QT_MENU_ICON_ID:
                    user32.SetForegroundWindow(hwnd)
                    qt_menu.popup(QtCore.QPoint(x, y))
                    user32.PostMessageW(hwnd, WM_NULL, 0, 0)
                else:
                    _show_native_menu(hwnd, x, y)
            return 0
        if message == WM_DESTROY:
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

    icons = [
        _add_icon(hwnd, NATIVE_MENU_ICON_ID, "1 - experiment: native menu"),
        _add_icon(hwnd, QT_MENU_ICON_ID, "2 - experiment: Qt menu"),
    ]

    QtCore.QTimer.singleShot(duration * 1000, app.quit)
    app.exec()

    for data in icons:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
    user32.DestroyWindow(hwnd)
    print("Icons removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
