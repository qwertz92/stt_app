from __future__ import annotations

import ctypes
import ctypes.wintypes
import time
from dataclasses import dataclass

from .config import (
    CLIPBOARD_SETTLE_S,
    SENDINPUT_RESTORE_DELAY_S,
    SENDINPUT_RETRY_ATTEMPTS,
    SENDINPUT_RETRY_SLEEP_S,
    WM_PASTE_TIMEOUT_MS,
)

try:
    import win32clipboard  # type: ignore
    import win32con  # type: ignore
except Exception:  # pragma: no cover - import guarded for testability
    win32clipboard = None
    win32con = None


class TextInsertionError(RuntimeError):
    pass


@dataclass(slots=True)
class ClipboardState:
    has_text: bool
    text: str | None


class Win32ClipboardBackend:
    def __init__(self, retry_count: int = 10, retry_sleep_s: float = 0.01) -> None:
        self._retry_count = retry_count
        self._retry_sleep_s = retry_sleep_s
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

    def capture_clipboard_state(self) -> ClipboardState:
        with self._clipboard_opened():
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                return ClipboardState(has_text=True, text=str(text))
            return ClipboardState(has_text=False, text=None)

    def set_clipboard_text(self, text: str) -> None:
        with self._clipboard_opened():
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)

    def restore_clipboard_state(self, state: ClipboardState) -> None:
        with self._clipboard_opened():
            win32clipboard.EmptyClipboard()
            if state.has_text and state.text is not None:
                win32clipboard.SetClipboardText(state.text, win32con.CF_UNICODETEXT)

    def send_ctrl_v(self) -> None:
        _send_ctrl_v_input()

    def send_paste(self, target_hwnd: int | None = None) -> str:
        if self._send_wm_paste(target_hwnd):
            return "wm_paste"

        self.send_ctrl_v()
        return "send_input"

    class _ClipboardContext:
        def __init__(self, backend: "Win32ClipboardBackend") -> None:
            self._backend = backend

        def __enter__(self):
            if win32clipboard is None or win32con is None:
                raise TextInsertionError(
                    "pywin32 is required for clipboard insertion on Windows."
                )

            for _ in range(self._backend._retry_count):
                try:
                    win32clipboard.OpenClipboard()
                    return self
                except Exception:
                    time.sleep(self._backend._retry_sleep_s)

            raise TextInsertionError("Failed to open clipboard.")

        def __exit__(self, exc_type, exc, tb):
            win32clipboard.CloseClipboard()
            return False

    def _clipboard_opened(self) -> "Win32ClipboardBackend._ClipboardContext":
        return self._ClipboardContext(self)

    def _send_wm_paste(self, target_hwnd: int | None = None) -> bool:
        hwnd = int(target_hwnd or self._get_focused_hwnd() or 0)
        if hwnd == 0:
            return False

        send_message_timeout = self._user32.SendMessageTimeoutW
        send_message_timeout.argtypes = (
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
            ctypes.wintypes.UINT,
            ctypes.wintypes.UINT,
            ctypes.POINTER(ULONG_PTR),
        )
        send_message_timeout.restype = ctypes.wintypes.LPARAM

        result = ULONG_PTR(0)
        ctypes.set_last_error(0)
        ok = send_message_timeout(
            hwnd,
            WM_PASTE,
            0,
            0,
            SMTO_ABORTIFHUNG,
            WM_PASTE_TIMEOUT_MS,
            ctypes.byref(result),
        )
        return bool(ok)

    def _get_focused_hwnd(self) -> int | None:
        foreground = int(self._user32.GetForegroundWindow() or 0)
        if foreground == 0:
            return None

        thread_id = int(self._user32.GetWindowThreadProcessId(foreground, None) or 0)
        if thread_id == 0:
            return foreground

        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        ok = bool(self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)))
        if not ok:
            return foreground
        focus = int(info.hwndFocus or 0)
        return focus or foreground


class TextInserter:
    def __init__(
        self,
        backend: Win32ClipboardBackend | None = None,
        sleep_fn=time.sleep,
        clipboard_settle_s: float = CLIPBOARD_SETTLE_S,
        sendinput_restore_delay_s: float = SENDINPUT_RESTORE_DELAY_S,
    ) -> None:
        self._backend = backend or Win32ClipboardBackend()
        self._sleep_fn = sleep_fn
        self._clipboard_settle_s = clipboard_settle_s
        self._sendinput_restore_delay_s = sendinput_restore_delay_s

    def insert_text(self, text: str, target_hwnd: int | None = None) -> bool:
        if not text or not text.strip():
            return False

        previous_state = self._backend.capture_clipboard_state()
        paste_mode = "send_input"
        paste_error: Exception | None = None
        restore_error: Exception | None = None
        try:
            self._backend.set_clipboard_text(text)
            self._sleep_fn(self._clipboard_settle_s)

            if hasattr(self._backend, "send_paste"):
                paste_mode = self._backend.send_paste(target_hwnd=target_hwnd)
            else:
                self._backend.send_ctrl_v()
                paste_mode = "send_input"

            if paste_mode == "send_input":
                # Give target app enough time to read clipboard before restore.
                self._sleep_fn(self._sendinput_restore_delay_s)
        except Exception as exc:
            paste_error = exc
        finally:
            try:
                self._backend.restore_clipboard_state(previous_state)
            except Exception as exc:
                restore_error = exc

        if paste_error is not None and restore_error is not None:
            raise TextInsertionError(
                f"Failed to paste text ({paste_error}) and failed to restore clipboard ({restore_error})."
            ) from paste_error
        if paste_error is not None:
            raise TextInsertionError(f"Failed to insert transcribed text: {paste_error}") from paste_error
        if restore_error is not None:
            raise TextInsertionError(
                f"Text pasted but clipboard restore failed: {restore_error}"
            ) from restore_error

        return True


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
WM_PASTE = 0x0302
SMTO_ABORTIFHUNG = 0x0002


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


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.wintypes.DWORD),
        ("wParamL", ctypes.wintypes.WORD),
        ("wParamH", ctypes.wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("union", _INPUTUNION),
    ]


def _keyboard_input(vk: int, keyup: bool = False) -> INPUT:
    flags = KEYEVENTF_KEYUP if keyup else 0
    return INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(ki=KEYBDINPUT(vk, 0, flags, 0, 0)))


def _send_ctrl_v_input() -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    send_input = user32.SendInput
    send_input.argtypes = (
        ctypes.wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
    )
    send_input.restype = ctypes.wintypes.UINT
    inputs = (INPUT * 4)(
        _keyboard_input(VK_CONTROL, keyup=False),
        _keyboard_input(VK_V, keyup=False),
        _keyboard_input(VK_V, keyup=True),
        _keyboard_input(VK_CONTROL, keyup=True),
    )
    expected = len(inputs)

    last_error = 0
    last_sent = 0
    for _ in range(SENDINPUT_RETRY_ATTEMPTS):
        ctypes.set_last_error(0)
        sent = send_input(
            expected,
            ctypes.cast(inputs, ctypes.POINTER(INPUT)),
            ctypes.sizeof(INPUT),
        )
        last_sent = int(sent)
        if last_sent == expected:
            return
        last_error = int(ctypes.get_last_error() or 0)
        time.sleep(SENDINPUT_RETRY_SLEEP_S)

    detail = _format_sendinput_failure(last_sent, expected, last_error)
    raise TextInsertionError(detail)


def _format_sendinput_failure(sent: int, expected: int, error_code: int) -> str:
    if error_code == 5:
        return (
            "SendInput failed (Access denied / UIPI). "
            "Run this app with the same privileges as the target window."
        )
    if error_code != 0:
        return (
            f"SendInput failed (sent {sent}/{expected}, WinError {error_code}). "
            "Ensure the target window is focused and accepts keyboard input."
        )
    if sent == 0:
        return (
            "SendInput failed (sent 0 events). "
            "Target window may be elevated, secure, or blocking synthetic input."
        )
    return (
        f"SendInput partially failed (sent {sent}/{expected}). "
        "Try again with the target window focused."
    )
