"""Windows notification icon registered directly through ``Shell_NotifyIcon``.

Why this exists instead of ``QSystemTrayIcon``: opening the app's tray menu
closed Windows 11's "hidden icons" flyout, while other apps in the same flyout
kept it open. Every difference observable at menu time was measured and ruled
out (foreground handling, window styles, owner windows, activation order — see
``docs/learning-log.md``). A hand-registered icon, Electron-style, keeps the
flyout open, and comparing two such icons that differ only in their menu showed
that the menu has to be a native ``TrackPopupMenu`` as well: Qt's own popup
window closes the flyout even on a correctly registered icon.

The class mirrors the small part of the ``QSystemTrayIcon`` API this app uses
(``activated``, ``showMessage``, ``show``, ``setContextMenu``, ``setToolTip``)
so callers do not care which implementation they got. ``create_tray_icon``
falls back to ``QSystemTrayIcon`` on other platforms and whenever any Win32
step fails, so the worst case is the previous behaviour rather than no icon.

The context menu stays a ``QMenu``: it is the model (labels, order, enabled
state, callbacks) and is only *rendered* natively, so menu construction and its
tests are unaffected.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import sys

from PySide6 import QtCore, QtGui, QtWidgets

from .app_icon import app_icon_path
from .config import APP_DISPLAY_NAME, APP_LOGGER_NAME

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_CONTEXTMENU = 0x007B
WM_LBUTTONDBLCLK = 0x0203
WM_MBUTTONUP = 0x0208
WM_NULL = 0x0000
WM_APP = 0x8000
TRAY_CALLBACK_MESSAGE = WM_APP + 17

NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETVERSION = 4
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10
NOTIFYICON_VERSION_4 = 4

NIIF_NONE = 0x00
NIIF_INFO = 0x01
NIIF_WARNING = 0x02
NIIF_ERROR = 0x03

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_GRAYED = 0x0001
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

WS_POPUP = 0x80000000
WS_CLIPSIBLINGS = 0x04000000
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
SM_CXSMICON = 49
SM_CYSMICON = 50

_TRAY_ICON_ID = 1
_WINDOW_CLASS_NAME = "SttAppNotificationIconWindow"
_MAX_TIP_CHARS = 127
_MAX_INFO_CHARS = 255
_MAX_INFO_TITLE_CHARS = 63

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


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


def low_word(value: int) -> int:
    return int(value) & 0xFFFF


def high_word(value: int) -> int:
    return (int(value) >> 16) & 0xFFFF


class Win32TrayApi:
    """Thin wrapper over the Win32 calls, so the logic above stays testable."""

    # A window class is process-wide and keeps the window procedure it was
    # registered with. Registering it per instance would leave every later
    # window running the first instance's trampoline — and crash once that
    # instance is collected. One class, one dispatcher, per-window handlers.
    _class_registered = False
    _dispatcher: WNDPROC | None = None
    _handlers: dict[int, object] = {}

    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        """Declare every call's types.

        Without this ctypes assumes ``c_int`` for arguments and results: window
        handles get truncated on 64-bit, and a message's ``LPARAM`` raises
        "int too long to convert" the first time the shell sends a large one.
        """
        user32 = self._user32
        user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.DefWindowProcW.restype = LRESULT
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = (
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HANDLE,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        )
        user32.DestroyWindow.argtypes = (wintypes.HWND,)
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        user32.LoadImageW.argtypes = (
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.CreatePopupMenu.argtypes = ()
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = (
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        )
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.DestroyMenu.argtypes = (wintypes.HMENU,)
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = (
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.LPVOID,
        )
        user32.TrackPopupMenu.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostMessageW.restype = wintypes.BOOL
        self._kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self._shell32.Shell_NotifyIconW.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(NOTIFYICONDATAW),
        )
        self._shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    # -- window ----------------------------------------------------------

    def _ensure_window_class(self) -> None:
        if Win32TrayApi._class_registered:
            return
        user32 = self._user32

        def dispatch(hwnd, message, wparam, lparam):
            handler = Win32TrayApi._handlers.get(int(hwnd))
            if handler is not None:
                handled = handler(hwnd, message, wparam, lparam)
                if handled is not None:
                    return handled
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        dispatcher = WNDPROC(dispatch)
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = dispatcher
        window_class.hInstance = self._kernel32.GetModuleHandleW(None)
        window_class.lpszClassName = _WINDOW_CLASS_NAME
        if not self._user32.RegisterClassW(ctypes.byref(window_class)):
            raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")
        # Outlives every window, so the class procedure can never dangle.
        Win32TrayApi._dispatcher = dispatcher
        Win32TrayApi._class_registered = True

    def create_window(self, window_proc) -> int:
        """Hidden ``WS_POPUP`` host window, like other apps' icon windows.

        Qt hosts its icon on an overlapped window with a caption; the shell
        treats that differently, which is the whole reason for this module.
        """
        self._ensure_window_class()
        hwnd = self._user32.CreateWindowExW(
            0,
            _WINDOW_CLASS_NAME,
            "",
            WS_POPUP | WS_CLIPSIBLINGS,
            0,
            0,
            0,
            0,
            None,
            None,
            self._kernel32.GetModuleHandleW(None),
            None,
        )
        if not hwnd:
            raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
        Win32TrayApi._handlers[int(hwnd)] = window_proc
        return int(hwnd)

    def destroy_window(self, hwnd: int) -> None:
        Win32TrayApi._handlers.pop(int(hwnd), None)
        self._user32.DestroyWindow(hwnd)

    def register_window_message(self, name: str) -> int:
        return int(self._user32.RegisterWindowMessageW(name))

    # -- icon ------------------------------------------------------------

    def load_icon(self, path: str) -> int:
        handle = self._user32.LoadImageW(
            None,
            ctypes.c_wchar_p(path),
            IMAGE_ICON,
            self._user32.GetSystemMetrics(SM_CXSMICON),
            self._user32.GetSystemMetrics(SM_CYSMICON),
            LR_LOADFROMFILE | LR_DEFAULTSIZE,
        )
        if not handle:
            raise OSError(f"LoadImageW failed: {ctypes.get_last_error()}")
        return int(handle)

    def add_icon(self, hwnd: int, icon: int, tooltip: str) -> None:
        data = self._icon_data(hwnd)
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = TRAY_CALLBACK_MESSAGE
        data.hIcon = icon
        data.szTip = tooltip[:_MAX_TIP_CHARS]
        if not self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            raise OSError(
                f"Shell_NotifyIconW(NIM_ADD) failed: {ctypes.get_last_error()}"
            )
        # Version 4 is what makes the shell send WM_CONTEXTMENU with screen
        # coordinates instead of the legacy button messages.
        data.uVersion = NOTIFYICON_VERSION_4
        self._shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))

    def update_tooltip(self, hwnd: int, tooltip: str) -> None:
        data = self._icon_data(hwnd)
        data.uFlags = NIF_TIP
        data.szTip = tooltip[:_MAX_TIP_CHARS]
        self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def show_balloon(self, hwnd: int, title: str, message: str, flags: int) -> None:
        data = self._icon_data(hwnd)
        data.uFlags = NIF_INFO
        data.szInfoTitle = title[:_MAX_INFO_TITLE_CHARS]
        data.szInfo = message[:_MAX_INFO_CHARS]
        data.dwInfoFlags = flags
        self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def delete_icon(self, hwnd: int) -> None:
        data = self._icon_data(hwnd)
        self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))

    @staticmethod
    def _icon_data(hwnd: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = _TRAY_ICON_ID
        return data

    # -- menu ------------------------------------------------------------

    def track_menu(self, hwnd: int, entries, x: int, y: int) -> int:
        """Show a native shortcut menu and return the chosen 1-based index.

        ``entries`` is a list of ``(label, enabled)`` pairs where a ``None``
        label is a separator. Returns 0 when nothing was chosen.
        """
        menu = self._user32.CreatePopupMenu()
        if not menu:
            return 0
        try:
            for index, (label, enabled) in enumerate(entries, start=1):
                if label is None:
                    self._user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                flags = MF_STRING if enabled else MF_STRING | MF_GRAYED
                self._user32.AppendMenuW(menu, flags, index, label)
            # Documented notification-icon contract: take the foreground so
            # the menu dismisses on an outside click, and wake the queue after.
            self._user32.SetForegroundWindow(hwnd)
            chosen = self._user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                x,
                y,
                0,
                hwnd,
                None,
            )
            self._user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            return int(chosen)
        finally:
            self._user32.DestroyMenu(menu)


class WindowsTrayIcon(QtCore.QObject):
    """``QSystemTrayIcon``-compatible notification icon for Windows."""

    activated = QtCore.Signal(object)

    def __init__(
        self,
        icon_path: str,
        tooltip: str = APP_DISPLAY_NAME,
        parent: QtCore.QObject | None = None,
        api: Win32TrayApi | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(parent)
        self._api = api or Win32TrayApi()
        self._logger = logger or logging.getLogger(APP_LOGGER_NAME)
        self._tooltip = str(tooltip or "")
        self._context_menu: QtWidgets.QMenu | None = None
        self._visible = False
        self._closed = False
        self._hwnd = self._api.create_window(self._window_proc)
        self._icon = self._api.load_icon(icon_path)
        # Explorer re-broadcasts this after a restart; without re-adding the
        # icon it would silently disappear for the rest of the session.
        self._taskbar_created_message = self._api.register_window_message(
            "TaskbarCreated"
        )

    # -- QSystemTrayIcon-compatible surface -------------------------------

    def show(self) -> None:
        if self._visible or self._closed:
            return
        self._api.add_icon(self._hwnd, self._icon, self._tooltip)
        self._visible = True

    def hide(self) -> None:
        if not self._visible:
            return
        self._api.delete_icon(self._hwnd)
        self._visible = False

    def setToolTip(self, tooltip: str) -> None:  # noqa: N802 - Qt API name
        self._tooltip = str(tooltip or "")
        if self._visible:
            self._api.update_tooltip(self._hwnd, self._tooltip)

    def setContextMenu(self, menu: QtWidgets.QMenu) -> None:  # noqa: N802
        self._context_menu = menu

    def contextMenu(self) -> QtWidgets.QMenu | None:  # noqa: N802
        return self._context_menu

    def showMessage(  # noqa: N802 - Qt API name
        self,
        title: str,
        message: str,
        icon=None,
        msecs: int = 10000,
    ) -> None:
        """Balloon notification. Windows controls the duration, so ``msecs``
        is accepted for API compatibility and ignored."""
        del msecs
        if not self._visible:
            return
        self._api.show_balloon(
            self._hwnd,
            str(title or ""),
            str(message or ""),
            self._balloon_flags(icon),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Order matters: remove the icon before its window goes away, or a
        # dead icon stays in the tray until the user hovers over it.
        try:
            self.hide()
        finally:
            self._api.destroy_window(self._hwnd)

    # -- message handling --------------------------------------------------

    def _window_proc(self, _hwnd, message, wparam, lparam):
        """Returns ``None`` for messages we do not consume, so the class
        dispatcher passes them to ``DefWindowProc``."""
        try:
            if self._handle_message(int(message), int(wparam), int(lparam)):
                return 0
        except Exception:
            self._logger.exception("Tray icon message handling failed")
            return 0
        return None

    def _handle_message(self, message: int, wparam: int, lparam: int) -> bool:
        """Return True when the message was consumed. Kept free of ctypes so
        the whole dispatch table is directly testable."""
        if message == self._taskbar_created_message:
            if self._visible:
                self._visible = False
                self.show()
            return True
        if message == WM_DESTROY:
            return True
        if message != TRAY_CALLBACK_MESSAGE:
            return False

        # NOTIFYICON_VERSION_4: the event is in the low word of lParam and the
        # screen coordinates are in wParam.
        event = low_word(lparam)
        if event in (NIN_SELECT, NIN_KEYSELECT):
            self.activated.emit(QtWidgets.QSystemTrayIcon.Trigger)
        elif event == WM_LBUTTONDBLCLK:
            self.activated.emit(QtWidgets.QSystemTrayIcon.DoubleClick)
        elif event == WM_MBUTTONUP:
            self.activated.emit(QtWidgets.QSystemTrayIcon.MiddleClick)
        elif event == WM_CONTEXTMENU:
            self.activated.emit(QtWidgets.QSystemTrayIcon.Context)
            self._show_context_menu(low_word(wparam), high_word(wparam))
        return True

    def _show_context_menu(self, x: int, y: int) -> None:
        menu = self._context_menu
        if menu is None:
            return
        actions = list(menu.actions())
        entries = [
            (None, False) if action.isSeparator() else (
                action.text(),
                action.isEnabled(),
            )
            for action in actions
        ]
        chosen = self._api.track_menu(self._hwnd, entries, x, y)
        if not 1 <= chosen <= len(actions):
            return
        action = actions[chosen - 1]
        # Let the menu finish closing before the action runs: some entries open
        # dialogs, and doing that from inside the menu's modal loop is asking
        # for trouble.
        QtCore.QTimer.singleShot(0, action.trigger)

    @staticmethod
    def _balloon_flags(icon) -> int:
        if icon == QtWidgets.QSystemTrayIcon.Warning:
            return NIIF_WARNING
        if icon == QtWidgets.QSystemTrayIcon.Critical:
            return NIIF_ERROR
        if icon == QtWidgets.QSystemTrayIcon.NoIcon:
            return NIIF_NONE
        return NIIF_INFO


def create_tray_icon(
    parent: QtCore.QObject,
    icon: QtGui.QIcon,
    tooltip: str = APP_DISPLAY_NAME,
    logger: logging.Logger | None = None,
):
    """Native icon on Windows, ``QSystemTrayIcon`` everywhere (and whenever)
    the native path is not available."""
    log = logger or logging.getLogger(APP_LOGGER_NAME)
    if sys.platform == "win32":
        try:
            return WindowsTrayIcon(
                icon_path=str(app_icon_path()),
                tooltip=tooltip,
                parent=parent,
                logger=log,
            )
        except Exception:
            log.exception(
                "Native tray icon unavailable, falling back to QSystemTrayIcon"
            )
    tray_icon = QtWidgets.QSystemTrayIcon(icon, parent)
    tray_icon.setToolTip(tooltip)
    return tray_icon
