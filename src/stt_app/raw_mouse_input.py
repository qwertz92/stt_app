"""Mouse button presses anywhere on the desktop, read as raw input (Windows).

A floating overlay waits above the window being typed in until the user
clicks into that window (docs/agents/overlay.md). Windows reports no such
click to another process: clicking into the window that is already active
changes neither the foreground nor the focus, and a plain window produced no
WinEvent at all for it (measured 2026-10-09; an EDIT control produced
`EVENT_SYSTEM_CAPTURESTART` only because it captures the mouse).

Raw input with `RIDEV_INPUTSINK` posts a `WM_INPUT` message to one window of
this process for every mouse event on the desktop, whichever window the event
goes to. The message is a copy: the click is delivered to its window without
waiting for this process. A `WH_MOUSE_LL` hook would sit in the path of every
click on the desktop instead, and its Python callback needs the GIL, so a
busy interpreter would delay the user's clicks and Windows silently removes
a hook that misses `LowLevelHooksTimeout`.

One registration per device class exists per process: registering again
moves it to the new window, and nothing else in this process registers mouse
raw input.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import functools
import logging

logger = logging.getLogger(__name__)

WM_INPUT = 0x00FF

_RIDEV_REMOVE = 0x00000001
_RIDEV_INPUTSINK = 0x00000100
_HID_USAGE_PAGE_GENERIC = 0x01
_HID_USAGE_GENERIC_MOUSE = 0x02
_RID_INPUT = 0x10000003
_RIM_TYPEMOUSE = 0
# RI_MOUSE_BUTTON_1_DOWN .. RI_MOUSE_BUTTON_5_DOWN: left, right, middle, X1, X2.
_BUTTON_DOWN_FLAGS = 0x0001 | 0x0004 | 0x0010 | 0x0040 | 0x0100


class _RawInputDevice(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wt.USHORT),
        ("usUsage", wt.USHORT),
        ("dwFlags", wt.DWORD),
        ("hwndTarget", wt.HWND),
    ]


class _RawInputHeader(ctypes.Structure):
    _fields_ = [
        ("dwType", wt.DWORD),
        ("dwSize", wt.DWORD),
        ("hDevice", wt.HANDLE),
        ("wParam", wt.WPARAM),
    ]


class _RawMouseButtons(ctypes.Structure):
    _fields_ = [("usButtonFlags", wt.USHORT), ("usButtonData", wt.USHORT)]


class _RawMouseButtonsUnion(ctypes.Union):
    # The ULONG member aligns the union, and so `usButtonFlags`, at offset 4.
    _anonymous_ = ("buttons",)
    _fields_ = [("ulButtons", wt.ULONG), ("buttons", _RawMouseButtons)]


class _RawMouse(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("usFlags", wt.USHORT),
        ("u", _RawMouseButtonsUnion),
        ("ulRawButtons", wt.ULONG),
        ("lLastX", wt.LONG),
        ("lLastY", wt.LONG),
        ("ulExtraInformation", wt.ULONG),
    ]


class _RawMouseInput(ctypes.Structure):
    _fields_ = [("header", _RawInputHeader), ("mouse", _RawMouse)]


@functools.cache
def _user32():
    """This module's own `user32` handle (docs/agents/windows-platform.md)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterRawInputDevices.argtypes = (
        ctypes.POINTER(_RawInputDevice),
        wt.UINT,
        wt.UINT,
    )
    user32.RegisterRawInputDevices.restype = wt.BOOL
    user32.GetRawInputData.argtypes = (
        wt.HANDLE,
        wt.UINT,
        wt.LPVOID,
        ctypes.POINTER(wt.UINT),
        wt.UINT,
    )
    user32.GetRawInputData.restype = wt.UINT
    return user32


def _register(flags: int, hwnd: int | None) -> bool:
    device = _RawInputDevice(
        _HID_USAGE_PAGE_GENERIC, _HID_USAGE_GENERIC_MOUSE, flags, hwnd
    )
    if _user32().RegisterRawInputDevices(
        ctypes.byref(device), 1, ctypes.sizeof(_RawInputDevice)
    ):
        return True
    logger.warning(
        "RegisterRawInputDevices(flags=%#x) failed: error %s",
        flags,
        ctypes.get_last_error(),
    )
    return False


def watch_mouse_presses(hwnd: int) -> bool:
    """Send a `WM_INPUT` to `hwnd` for every mouse event on the desktop."""
    return _register(_RIDEV_INPUTSINK, hwnd)


def stop_watching_mouse_presses() -> bool:
    """Remove this process's mouse raw-input registration."""
    # RIDEV_REMOVE requires a null target window.
    return _register(_RIDEV_REMOVE, None)


def mouse_button_pressed(lparam: int) -> bool:
    """Does this `WM_INPUT` message (its `lParam`) carry a button press?"""
    data = _RawMouseInput()
    size = wt.UINT(ctypes.sizeof(data))
    read = _user32().GetRawInputData(
        lparam,
        _RID_INPUT,
        ctypes.byref(data),
        ctypes.byref(size),
        ctypes.sizeof(_RawInputHeader),
    )
    # (UINT)-1 is the failure value; a short read cannot hold the flags.
    if read == 0xFFFFFFFF or read < ctypes.sizeof(data):
        return False
    if data.header.dwType != _RIM_TYPEMOUSE:
        return False
    return bool(data.mouse.usButtonFlags & _BUTTON_DOWN_FLAGS)
