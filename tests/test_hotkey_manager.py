import ctypes

import pytest

from stt_app.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    PBT_APMRESUMEAUTOMATIC,
    VK_RMENU,
    WM_HOTKEY,
    WM_POWERBROADCAST,
    HotkeyManager,
    HotkeyRegistrationError,
    QtPowerResumeEventFilter,
    parse_hotkey,
)


class FakeWin32HotkeyApi:
    def __init__(self, register_ok=True, unregister_ok=True, last_error=0):
        self.register_ok = register_ok
        self.unregister_ok = unregister_ok
        self.last_error = last_error
        self.register_calls = []
        self.unregister_calls = []
        self.down_keys: set[int] = set()

    def register_hotkey(self, hwnd, hotkey_id, modifiers, virtual_key):
        self.register_calls.append((hwnd, hotkey_id, modifiers, virtual_key))
        return self.register_ok

    def unregister_hotkey(self, hwnd, hotkey_id):
        self.unregister_calls.append((hwnd, hotkey_id))
        return self.unregister_ok

    def get_last_error(self):
        return self.last_error

    def is_key_down(self, virtual_key):
        return int(virtual_key) in self.down_keys


def test_parse_hotkey_ctrl_alt_space():
    modifiers, vk = parse_hotkey("Ctrl+Alt+Space")

    assert modifiers & MOD_CONTROL
    assert modifiers & MOD_ALT
    assert modifiers & MOD_NOREPEAT
    assert vk == 0x20


def test_parse_hotkey_rejects_a_modifier_as_the_key():
    """RegisterHotKey matches the modifier state exactly, and pressing a
    modifier raises its own modifier bit. "Ctrl+Win+LShift" registers
    Ctrl+Win + key LSHIFT while the real keystroke reports Ctrl+Win+Shift, so
    it can never fire — and registration *succeeds*, so the app reported a
    working hotkey that silently did nothing."""
    for combination in ("Ctrl+Win+LShift", "Ctrl+Alt+RShift", "Ctrl+Shift+LCtrl"):
        with pytest.raises(ValueError, match="modifier"):
            parse_hotkey(combination)
    # Names the key map does not know are rejected too, just with the
    # unknown-key message.
    with pytest.raises(ValueError):
        parse_hotkey("Alt+Shift+LWin")


def test_parse_hotkey_accepts_every_shipped_fallback():
    from stt_app.config import FALLBACK_HOTKEYS

    for combination in FALLBACK_HOTKEYS:
        modifiers, vk = parse_hotkey(combination)
        assert modifiers & MOD_NOREPEAT
        assert vk not in {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3}


def test_parse_hotkey_without_modifier_raises():
    with pytest.raises(ValueError):
        parse_hotkey("LShift")


def test_register_unregister_calls_win32_api():
    api = FakeWin32HotkeyApi()
    manager = HotkeyManager(api=api, hotkey_id=42)

    manager.register("Ctrl+Alt+Space")

    assert manager.is_registered is True
    assert api.register_calls == [
        (None, 42, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x20)
    ]

    manager.unregister()

    assert manager.is_registered is False
    assert api.unregister_calls == [(None, 42)]


def test_register_raises_on_failure():
    api = FakeWin32HotkeyApi(register_ok=False, last_error=1409)
    manager = HotkeyManager(api=api, hotkey_id=99)

    with pytest.raises(HotkeyRegistrationError) as error:
        manager.register("Ctrl+Alt+Space")
    assert "1409" in str(error.value)


def test_re_register_unregisters_previous_first():
    api = FakeWin32HotkeyApi()
    manager = HotkeyManager(api=api, hotkey_id=7)

    manager.register("Ctrl+Alt+Space")
    manager.register("Ctrl+Shift+A")

    assert api.unregister_calls == [(None, 7)]
    assert len(api.register_calls) == 2


def test_failed_unregister_preserves_registration_state_and_blocks_replacement():
    api = FakeWin32HotkeyApi(unregister_ok=False, last_error=5)
    manager = HotkeyManager(api=api, hotkey_id=7)
    manager.register("Ctrl+Alt+Space")

    with pytest.raises(HotkeyRegistrationError, match="error code: 5"):
        manager.register("Ctrl+Shift+A")

    assert manager.is_registered is True
    assert api.unregister_calls == [(None, 7)]
    assert len(api.register_calls) == 1


def test_ctrl_alt_hotkey_ignores_altgr_alias():
    api = FakeWin32HotkeyApi()
    api.down_keys.add(VK_RMENU)
    manager = HotkeyManager(api=api, hotkey_id=42)
    manager.register("Ctrl+Alt+Space")

    assert manager.matches_message(WM_HOTKEY, 42) is False


def test_ctrl_alt_hotkey_matches_without_altgr():
    api = FakeWin32HotkeyApi()
    manager = HotkeyManager(api=api, hotkey_id=42)
    manager.register("Ctrl+Alt+Space")

    assert manager.matches_message(WM_HOTKEY, 42) is True


def test_non_ctrl_alt_hotkey_does_not_check_altgr():
    api = FakeWin32HotkeyApi()
    api.down_keys.add(VK_RMENU)
    manager = HotkeyManager(api=api, hotkey_id=42)
    manager.register("Ctrl+Shift+Space")

    assert manager.matches_message(WM_HOTKEY, 42) is True


def test_power_resume_event_filter_calls_callback():
    calls = []
    event_filter = QtPowerResumeEventFilter(lambda: calls.append(True))
    message = ctypes.wintypes.MSG()
    message.message = WM_POWERBROADCAST
    message.wParam = PBT_APMRESUMEAUTOMATIC

    handled, result = event_filter.nativeEventFilter(
        b"windows_generic_MSG",
        ctypes.addressof(message),
    )

    assert handled is False
    assert result == 0
    assert calls == [True]
