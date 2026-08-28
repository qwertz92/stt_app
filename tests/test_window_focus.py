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
        self.hidden_windows: set[int] = set()

    def GetForegroundWindow(self) -> int:
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd, lp) -> int:
        lp._obj.value = (
            self.own_process_id if hwnd in self.own_windows else 4242
        )
        return 1

    def GetWindowLongW(self, hwnd, _index) -> int:
        return _WS_EX_TOOLWINDOW if hwnd in self.tool_windows else 0

    def IsWindow(self, hwnd) -> int:
        return 1 if hwnd in self.valid_windows else 0

    def IsWindowVisible(self, hwnd) -> int:
        return 0 if hwnd in self.hidden_windows else 1


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
    """A destroyed target must not be handed back -- and neither must ours.

    This used to answer with the tray menu itself, because the fallback was
    `self._remembered_foreign_window() or hwnd`. Our own popup can never take
    dictated text, and `restore_target_window` would call
    `ShowWindow(SW_SHOW)` on it, so it becomes visible, passes the
    own-non-target predicate from then on, and is cached as the last foreign
    window for the rest of the session. `None` says what is true: there is no
    remembered target.
    """
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

    assert helper.get_foreground_window() is None
    assert helper._last_foreign_window is None, "the dead handle was kept"


def test_the_foreground_can_be_noted_before_one_of_our_windows_takes_it():
    """The tray menu's activation signal fires while the user's window is up.

    Opening the notification-icon menu requires `SetForegroundWindow` on our
    hidden host window, so by the time a menu action runs the foreground is
    ours and nothing foreign has been remembered on a fresh session. Noting it
    first is what makes the first dictation started from the tray land in the
    window the user was working in.
    """
    helper, fake = _helper()
    editor = 1001
    tray_host = 4004
    fake.valid_windows = {editor, tray_host}
    fake.own_windows = {tray_host}
    fake.hidden_windows = {tray_host}

    fake.foreground = editor
    helper.note_foreground_window()
    fake.foreground = tray_host

    assert helper.get_foreground_window() == editor


def test_noting_our_own_window_records_nothing():
    """Otherwise the hint would poison the cache it exists to fill."""
    helper, fake = _helper()
    tray_host = 4004
    fake.valid_windows = {tray_host}
    fake.own_windows = {tray_host}
    fake.hidden_windows = {tray_host}
    fake.foreground = tray_host

    helper.note_foreground_window()

    assert helper._last_foreign_window is None
    assert helper.get_foreground_window() is None


def test_hidden_tray_helper_window_is_never_the_target():
    """Opening the tray menu activates our notification-icon window first."""
    helper, fake = _helper()
    editor = 1001
    tray_host = 4004
    fake.valid_windows = {editor, tray_host}
    fake.own_windows = {tray_host}
    fake.hidden_windows = {tray_host}

    fake.foreground = editor
    helper.get_foreground_window()
    fake.foreground = tray_host

    assert helper.get_foreground_window() == editor
