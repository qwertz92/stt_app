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
        # Its own handle rather than the process-wide `ctypes.windll.user32`,
        # so the declarations below cannot redefine these functions for other
        # callers -- the same split `text_inserter` and `win_tray_icon` use.
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        wintypes = ctypes.wintypes
        self._user32.RegisterHotKey.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
        )
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.UnregisterHotKey.restype = wintypes.BOOL
        self._user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        # SHORT, not int: `text_inserter` already declares it this way, and
        # the bit this module tests (0x8000) is the sign bit of that SHORT.
        self._user32.GetAsyncKeyState.restype = ctypes.c_short

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
        # `ctypes.get_last_error()`, not `ctypes.GetLastError()`. The handle
        # above is opened with `use_last_error=True`, which saves the Windows
        # error into ctypes' own per-call slot and *restores* the thread's
        # `GetLastError` to what it was before the call -- so the thread
        # reader answers 0 and every hotkey failure reported "Unknown Windows
        # hotkey registration error". Measured against a real double
        # `RegisterHotKey`: thread reader 0, ctypes slot 1409, which is the
        # "another program holds this combination" code the fallback and
        # reclaim machinery exists for. `text_inserter` and `win_tray_icon`
        # already read the slot; this was the only thread reader left.
        return int(ctypes.get_last_error() or 0)

    def is_key_down(self, virtual_key: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(virtual_key) & KEY_STATE_DOWN_MASK)


# Virtual keys that are themselves modifiers; see parse_hotkey.
_MODIFIER_VIRTUAL_KEYS = frozenset(
    {
        0x10,  # VK_SHIFT
        0x11,  # VK_CONTROL
        0x12,  # VK_MENU (Alt)
        0x5B,  # VK_LWIN
        0x5C,  # VK_RWIN
        0xA0,  # VK_LSHIFT
        0xA1,  # VK_RSHIFT
        0xA2,  # VK_LCONTROL
        0xA3,  # VK_RCONTROL
        0xA4,  # VK_LMENU
        0xA5,  # VK_RMENU
    }
)


def _supported_key_names() -> str:
    """The accepted key names, for the rejection message.

    Derived from `_KEY_MAP` rather than written out, so the message cannot
    advertise a key the parser would then refuse -- which a hand-written list
    did, naming Insert, Delete, Home, End, PageUp and PageDown, none of which
    the map holds. Modifier names are left out because the check below rejects
    them anyway.
    """
    def is_function_key(name: str) -> bool:
        return name.startswith("F") and name[1:].isdigit()

    function_keys = sorted(
        (name for name in _KEY_MAP if is_function_key(name)),
        key=lambda name: int(name[1:]),
    )
    named = sorted(
        name.title()
        for name, virtual_key in _KEY_MAP.items()
        if len(name) > 1
        and virtual_key not in _MODIFIER_VIRTUAL_KEYS
        and not is_function_key(name)
    )
    parts = ["a letter", "a digit", *named]
    if function_keys:
        parts.append(f"{function_keys[0]}-{function_keys[-1]}")
    return ", ".join(parts[:-1]) + f", or {parts[-1]}"


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

    # `_KEY_MAP` is the whole supported set, letters and digits included.
    # There used to be an `ord(key_name)` fallback for any single character,
    # and because every letter and digit is already in the map, the only
    # characters it could ever reach were the ones for which `ord()` is *not*
    # the virtual-key code. Measured through the real Settings field, which
    # emits Qt's PortableText verbatim: "Ctrl+Alt+." registered VK_DELETE
    # (0x2E), "-" VK_INSERT, "#" VK_END, "'" VK_RIGHT, while ";" and "Ä" got
    # codes Windows assigns to nothing. Two different failures came out of
    # that, and all five characters are unmodified keys on a German keyboard.
    # One that lands on a real key steals it globally -- "Ctrl+Shift+." took
    # Ctrl+Shift+Delete away from every browser while doing nothing itself --
    # and one that lands on an unassigned code registers successfully and can
    # never fire, which is the same silent failure the modifier rejection
    # below exists to prevent.
    vk = _KEY_MAP.get(key_name)
    if vk is None:
        raise ValueError(
            f"Unsupported hotkey key: {parts[-1]}. Supported keys are "
            f"{_supported_key_names()}."
        )

    if vk in _MODIFIER_VIRTUAL_KEYS:
        # RegisterHotKey matches the modifier state *exactly*, and pressing a
        # modifier necessarily raises its own modifier bit. "Ctrl+Win+LShift"
        # therefore registers Ctrl+Win + key LSHIFT, while the actual keystroke
        # reports Ctrl+Win+Shift — a different hotkey, so it can never match.
        # Registration still succeeds, which is why this failed silently: the
        # app reported a working hotkey that could not fire.
        raise ValueError(
            f"'{parts[-1]}' is a modifier and cannot be the hotkey's key. "
            "Add a normal key such as a letter or a function key."
        )

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
