import pytest

from stt_app.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_WIN,
    HotkeyManager,
    HotkeyRegistrationError,
    parse_hotkey,
)


class FakeWin32HotkeyApi:
    def __init__(self, register_ok=True, unregister_ok=True, last_error=0):
        self.register_ok = register_ok
        self.unregister_ok = unregister_ok
        self.last_error = last_error
        self.register_calls = []
        self.unregister_calls = []

    def register_hotkey(self, hwnd, hotkey_id, modifiers, virtual_key):
        self.register_calls.append((hwnd, hotkey_id, modifiers, virtual_key))
        return self.register_ok

    def unregister_hotkey(self, hwnd, hotkey_id):
        self.unregister_calls.append((hwnd, hotkey_id))
        return self.unregister_ok

    def get_last_error(self):
        return self.last_error


def test_parse_hotkey_ctrl_alt_space():
    modifiers, vk = parse_hotkey("Ctrl+Alt+Space")

    assert modifiers & MOD_CONTROL
    assert modifiers & MOD_ALT
    assert modifiers & MOD_NOREPEAT
    assert vk == 0x20


def test_parse_hotkey_ctrl_win_lshift():
    modifiers, vk = parse_hotkey("Ctrl+Win+LShift")

    assert modifiers & MOD_CONTROL
    assert modifiers & MOD_WIN
    assert modifiers & MOD_NOREPEAT
    assert vk == 0xA0


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
