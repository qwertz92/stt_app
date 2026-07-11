from __future__ import annotations

import ctypes
import ctypes.wintypes

from .config import DEFAULT_HOTKEY_ID

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
VK_RMENU = 0xA5
KEY_STATE_DOWN_MASK = 0x8000

_KEY_MAP = {
    "SPACE": 0x20,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "ESC": 0x1B,
    "LSHIFT": 0xA0,
    "RSHIFT": 0xA1,
    "LCTRL": 0xA2,
    "RCTRL": 0xA3,
    "LCONTROL": 0xA2,
    "RCONTROL": 0xA3,
    "LALT": 0xA4,
    "RALT": 0xA5,
    "LEFT": 0x25,
    "RIGHT": 0x27,
    "UP": 0x26,
    "DOWN": 0x28,
}
for i in range(1, 13):
    _KEY_MAP[f"F{i}"] = 0x6F + i
for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _KEY_MAP[letter] = ord(letter)
for digit in "0123456789":
    _KEY_MAP[digit] = ord(digit)


class HotkeyRegistrationError(RuntimeError):
    pass


class Win32HotkeyApi:
    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32

    def register_hotkey(
        self,
        hwnd,
        hotkey_id: int,
        modifiers: int,
        virtual_key: int,
    ) -> bool:
        return bool(
            self._user32.RegisterHotKey(hwnd, hotkey_id, modifiers, virtual_key)
        )

    def unregister_hotkey(self, hwnd, hotkey_id: int) -> bool:
        return bool(self._user32.UnregisterHotKey(hwnd, hotkey_id))

    def get_last_error(self) -> int:
        return int(ctypes.GetLastError() or 0)

    def is_key_down(self, virtual_key: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(virtual_key) & KEY_STATE_DOWN_MASK)


def parse_hotkey(value: str, include_norepeat: bool = True) -> tuple[int, int]:
    if not value:
        raise ValueError("Hotkey is empty.")

    parts = [part.strip() for part in value.split("+") if part.strip()]
    if len(parts) < 2:
        raise ValueError("Hotkey must include at least one modifier and one key.")

    key_name = parts[-1].upper()
    modifiers = 0

    for part in parts[:-1]:
        token = part.upper()
        if token in {"CTRL", "CONTROL"}:
            modifiers |= MOD_CONTROL
        elif token == "ALT":
            modifiers |= MOD_ALT
        elif token == "SHIFT":
            modifiers |= MOD_SHIFT
        elif token in {"WIN", "WINDOWS"}:
            modifiers |= MOD_WIN
        else:
            raise ValueError(f"Unknown hotkey modifier: {part}")

    if modifiers == 0:
        raise ValueError("Hotkey must include at least one modifier.")

    if include_norepeat:
        modifiers |= MOD_NOREPEAT

    vk = _KEY_MAP.get(key_name)
    if vk is None and len(key_name) == 1:
        vk = ord(key_name)

    if vk is None:
        raise ValueError(f"Unknown hotkey key: {parts[-1]}")

    return modifiers, vk


class HotkeyManager:
    def __init__(
        self,
        api: Win32HotkeyApi | None = None,
        hotkey_id: int = DEFAULT_HOTKEY_ID,
        hwnd=None,
    ) -> None:
        self._api = api or Win32HotkeyApi()
        self._hotkey_id = hotkey_id
        self._hwnd = hwnd
        self._is_registered = False
        self._registered_modifiers = 0
        self._registered_vk = 0

    @property
    def hotkey_id(self) -> int:
        return self._hotkey_id

    @property
    def is_registered(self) -> bool:
        return self._is_registered

    def register(self, hotkey: str) -> None:
        modifiers, vk = parse_hotkey(hotkey)

        if self._is_registered:
            self.unregister()

        if not self._api.register_hotkey(self._hwnd, self._hotkey_id, modifiers, vk):
            error_code = 0
            if hasattr(self._api, "get_last_error"):
                try:
                    error_code = int(self._api.get_last_error() or 0)
                except Exception:
                    error_code = 0
            detail = _format_register_hotkey_error(error_code)
            raise HotkeyRegistrationError(
                f"Failed to register hotkey: {hotkey}. {detail}"
            )

        self._is_registered = True
        self._registered_modifiers = modifiers
        self._registered_vk = vk

    def unregister(self) -> None:
        if not self._is_registered:
            return

        if not self._api.unregister_hotkey(self._hwnd, self._hotkey_id):
            error_code = 0
            if hasattr(self._api, "get_last_error"):
                try:
                    error_code = int(self._api.get_last_error() or 0)
                except Exception:
                    error_code = 0
            detail = (
                f"Windows error code: {error_code}."
                if error_code
                else "Unknown Windows hotkey unregistration error."
            )
            raise HotkeyRegistrationError(
                f"Failed to unregister hotkey ID {self._hotkey_id}. {detail}"
            )
        self._is_registered = False
        self._registered_modifiers = 0
        self._registered_vk = 0

    def matches_message(self, message_id: int, wparam: int) -> bool:
        if message_id != WM_HOTKEY or int(wparam) != self._hotkey_id:
            return False
        return not self._is_altgr_alias_active()

    def _is_altgr_alias_active(self) -> bool:
        if not (
            self._registered_modifiers & MOD_CONTROL
            and self._registered_modifiers & MOD_ALT
        ):
            return False
        key_down = getattr(self._api, "is_key_down", None)
        if not callable(key_down):
            return False
        try:
            return bool(key_down(VK_RMENU))
        except Exception:
            return False


def _format_register_hotkey_error(error_code: int) -> str:
    if error_code == 1409:
        return "Windows reported hotkey already registered (1409)."
    if error_code:
        return f"Windows error code: {error_code}."
    return "Unknown Windows hotkey registration error."


try:
    from PySide6 import QtCore
except Exception:  # pragma: no cover - covered in runtime smoke test
    QtCore = None


if QtCore is not None:

    class QtHotkeyEventFilter(QtCore.QAbstractNativeEventFilter):
        def __init__(self, hotkey_manager: HotkeyManager, callback) -> None:
            super().__init__()
            self._hotkey_manager = hotkey_manager
            self._callback = callback

        def nativeEventFilter(self, event_type, message):
            event_name = (
                event_type.decode("utf-8", errors="ignore")
                if isinstance(event_type, (bytes, bytearray))
                else str(event_type)
            )
            if "windows" not in event_name.lower():
                return False, 0

            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))
            except Exception:
                return False, 0

            if self._hotkey_manager.matches_message(msg.message, msg.wParam):
                self._callback()
                return True, 0

            return False, 0


    class QtPowerResumeEventFilter(QtCore.QAbstractNativeEventFilter):
        def __init__(self, callback) -> None:
            super().__init__()
            self._callback = callback

        def nativeEventFilter(self, event_type, message):
            event_name = (
                event_type.decode("utf-8", errors="ignore")
                if isinstance(event_type, (bytes, bytearray))
                else str(event_type)
            )
            if "windows" not in event_name.lower():
                return False, 0

            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))
            except Exception:
                return False, 0

            if (
                msg.message == WM_POWERBROADCAST
                and int(msg.wParam) in {
                    PBT_APMRESUMESUSPEND,
                    PBT_APMRESUMEAUTOMATIC,
                }
            ):
                self._callback()

            return False, 0

else:

    class QtHotkeyEventFilter:  # pragma: no cover - fallback outside Qt runtime
        def __init__(self, hotkey_manager: HotkeyManager, callback) -> None:
            self._hotkey_manager = hotkey_manager
            self._callback = callback

        def nativeEventFilter(self, event_type, message):
            return False, 0


    class QtPowerResumeEventFilter:  # pragma: no cover - fallback outside Qt runtime
        def __init__(self, callback) -> None:
            self._callback = callback

        def nativeEventFilter(self, event_type, message):
            return False, 0
