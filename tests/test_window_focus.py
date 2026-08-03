from __future__ import annotations

from stt_app.window_focus import Win32WindowFocusHelper

_WS_EX_TOOLWINDOW = 0x00000080


class _FakeUser32:
    """Minimal stand-in for the Win32 calls the helper makes."""

    def __init__(self, own_process_id: int) -> None:
        self.foreground = 0
        self.own_process_id = own_process_id
        self.own_windows: set[int] = set()
        self.tool_windows: set[int] = set()
        self.valid_windows: set[int] = set()

    def GetForegroundWindow(self) -> int:  # noqa: N802 - Win32 name
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd, lp) -> int:  # noqa: N802
        lp._obj.value = (
            self.own_process_id if hwnd in self.own_windows else 4242
        )
        return 1

    def GetWindowLongW(self, hwnd, _index) -> int:  # noqa: N802
        return _WS_EX_TOOLWINDOW if hwnd in self.tool_windows else 0

    def IsWindow(self, hwnd) -> int:  # noqa: N802
        return 1 if hwnd in self.valid_windows else 0


def _helper() -> tuple[Win32WindowFocusHelper, _FakeUser32]:
    helper = Win32WindowFocusHelper()
    fake = _FakeUser32(helper._own_process_id)
    helper._user32 = fake
    return helper, fake


def test_tray_menu_does_not_become_the_dictation_target():
    """Starting dictation from the tray menu must target the user's window.

    Our own menu holds the foreground at that moment, so without this the
    transcript would be aimed at a window that cannot take text.
    """
    helper, fake = _helper()
    editor = 1001
    tray_menu = 2002
    fake.valid_windows = {editor, tray_menu}
    fake.own_windows = {tray_menu}
    fake.tool_windows = {tray_menu}

    fake.foreground = editor
    assert helper.get_foreground_window() == editor

    fake.foreground = tray_menu
    assert helper.get_foreground_window() == editor


def test_own_normal_window_stays_a_valid_target():
    """The Settings dialog is a normal window; dictating into it is allowed."""
    helper, fake = _helper()
    editor = 1001
    settings_dialog = 3003
    fake.valid_windows = {editor, settings_dialog}
    fake.own_windows = {settings_dialog}

    fake.foreground = editor
    helper.get_foreground_window()
    fake.foreground = settings_dialog

    assert helper.get_foreground_window() == settings_dialog


def test_remembered_window_is_dropped_once_it_is_gone():
    helper, fake = _helper()
    editor = 1001
    tray_menu = 2002
    fake.valid_windows = {editor, tray_menu}
    fake.own_windows = {tray_menu}
    fake.tool_windows = {tray_menu}

    fake.foreground = editor
    helper.get_foreground_window()
    # The editor was closed while the menu is open.
    fake.valid_windows = {tray_menu}
    fake.foreground = tray_menu

    assert helper.get_foreground_window() == tray_menu
