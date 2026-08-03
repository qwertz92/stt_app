"""Tests for the hand-registered Windows notification icon.

The Win32 layer is injected, so nothing here touches the real notification
area; what is verified is the message dispatch, the native menu rendering and
the lifecycle that Qt used to handle for us.
"""
from __future__ import annotations

from PySide6 import QtCore, QtTest, QtWidgets

from stt_app import win_tray_icon
from stt_app.win_tray_icon import WindowsTrayIcon, create_tray_icon

_HWND = 4242
_TASKBAR_CREATED = 0xC123


class FakeWin32TrayApi:
    def __init__(self) -> None:
        self.window_proc = None
        self.destroyed: list[int] = []
        self.added: list[tuple[int, int, str]] = []
        self.deleted: list[int] = []
        self.tooltips: list[str] = []
        self.balloons: list[tuple[str, str, int]] = []
        self.menus: list[list[tuple[str | None, bool]]] = []
        self.menu_choice = 0

    def create_window(self, window_proc):
        self.window_proc = window_proc
        return _HWND

    def destroy_window(self, hwnd):
        self.destroyed.append(hwnd)

    def register_window_message(self, name):
        assert name == "TaskbarCreated"
        return _TASKBAR_CREATED

    def load_icon(self, path):
        assert path
        return 7

    def add_icon(self, hwnd, icon, tooltip):
        self.added.append((hwnd, icon, tooltip))

    def update_tooltip(self, hwnd, tooltip):
        self.tooltips.append(tooltip)

    def show_balloon(self, hwnd, title, message, flags):
        self.balloons.append((title, message, flags))

    def delete_icon(self, hwnd):
        self.deleted.append(hwnd)

    def track_menu(self, hwnd, entries, x, y):
        self.menus.append((list(entries), x, y))
        return self.menu_choice


def _make_icon(api: FakeWin32TrayApi) -> WindowsTrayIcon:
    return WindowsTrayIcon(icon_path="icon.ico", tooltip="Tip", api=api)


def _send(icon: WindowsTrayIcon, event: int, x: int = 0, y: int = 0) -> None:
    """Deliver a notification-icon callback message the way the shell does."""
    icon._handle_message(
        win_tray_icon.TRAY_CALLBACK_MESSAGE,
        (y << 16) | x,
        event,
    )


def test_show_registers_the_icon_once_and_hide_removes_it():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)

    icon.show()
    icon.show()

    assert api.added == [(_HWND, 7, "Tip")]

    icon.hide()

    assert api.deleted == [_HWND]


def test_close_removes_the_icon_before_destroying_its_window():
    """A dead icon would otherwise stay in the tray until the user hovers."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)
    icon.show()

    icon.close()
    icon.close()

    assert api.deleted == [_HWND]
    assert api.destroyed == [_HWND]


def test_clicks_map_to_the_qt_activation_reasons():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)
    reasons: list[object] = []
    icon.activated.connect(reasons.append)

    _send(icon, win_tray_icon.NIN_SELECT)
    _send(icon, win_tray_icon.NIN_KEYSELECT)
    _send(icon, win_tray_icon.WM_LBUTTONDBLCLK)
    _send(icon, win_tray_icon.WM_MBUTTONUP)

    assert reasons == [
        QtWidgets.QSystemTrayIcon.Trigger,
        QtWidgets.QSystemTrayIcon.Trigger,
        QtWidgets.QSystemTrayIcon.DoubleClick,
        QtWidgets.QSystemTrayIcon.MiddleClick,
    ]


def test_right_click_renders_the_qt_menu_natively():
    """The QMenu stays the model; only its rendering is native."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)
    menu = QtWidgets.QMenu()
    menu.addAction("First")
    disabled = menu.addAction("Disabled")
    disabled.setEnabled(False)
    menu.addSeparator()
    menu.addAction("Last")
    icon.setContextMenu(menu)
    reasons: list[object] = []
    icon.activated.connect(reasons.append)

    _send(icon, win_tray_icon.WM_CONTEXTMENU, x=120, y=340)

    assert reasons == [QtWidgets.QSystemTrayIcon.Context]
    entries, x, y = api.menus[0]
    assert entries == [
        ("First", True),
        ("Disabled", False),
        (None, False),
        ("Last", True),
    ]
    assert (x, y) == (120, 340)


def test_chosen_menu_entry_triggers_its_action():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)
    menu = QtWidgets.QMenu()
    triggered: list[str] = []
    menu.addAction("First").triggered.connect(lambda: triggered.append("first"))
    menu.addSeparator()
    menu.addAction("Third").triggered.connect(lambda: triggered.append("third"))
    icon.setContextMenu(menu)
    api.menu_choice = 3

    _send(icon, win_tray_icon.WM_CONTEXTMENU)
    # The action runs once the menu has closed, not from inside its modal loop.
    assert triggered == []
    QtTest.QTest.qWait(30)

    assert triggered == ["third"]


def test_cancelled_menu_triggers_nothing():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)
    menu = QtWidgets.QMenu()
    triggered: list[str] = []
    menu.addAction("First").triggered.connect(lambda: triggered.append("first"))
    icon.setContextMenu(menu)
    api.menu_choice = 0

    _send(icon, win_tray_icon.WM_CONTEXTMENU)
    QtTest.QTest.qWait(30)

    assert triggered == []


def test_icon_is_restored_after_an_explorer_restart():
    """Qt re-added the icon for us; a hand-registered one must do it itself."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)
    icon.show()

    icon._handle_message(_TASKBAR_CREATED, 0, 0)

    assert len(api.added) == 2


def test_hidden_icon_is_not_restored_after_an_explorer_restart():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)

    icon._handle_message(_TASKBAR_CREATED, 0, 0)

    assert api.added == []


def test_show_message_maps_the_qt_icon_to_a_balloon_flag():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)

    icon.showMessage("Title", "Body", QtWidgets.QSystemTrayIcon.Warning)
    assert api.balloons == []  # nothing to attach a balloon to yet

    icon.show()
    icon.showMessage("Title", "Body", QtWidgets.QSystemTrayIcon.Warning)
    icon.showMessage("Other", "Text")

    assert api.balloons == [
        ("Title", "Body", win_tray_icon.NIIF_WARNING),
        ("Other", "Text", win_tray_icon.NIIF_INFO),
    ]


def test_tooltip_change_reaches_the_shell_only_while_visible():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)

    icon.setToolTip("Before")
    icon.show()
    icon.setToolTip("After")

    assert api.added == [(_HWND, 7, "Before")]
    assert api.tooltips == ["After"]


def test_unhandled_messages_fall_through_to_the_default_procedure():
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    api = FakeWin32TrayApi()
    icon = _make_icon(api)

    assert icon._handle_message(0x0007, 0, 0) is False
    assert icon._window_proc(_HWND, 0x0007, 0, 0) is None


def test_create_tray_icon_falls_back_when_the_native_path_fails(monkeypatch):
    """The worst case must be the previous behaviour, not a missing icon."""
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _explode(*_args, **_kwargs):
        raise OSError("no window for you")

    monkeypatch.setattr(win_tray_icon.sys, "platform", "win32")
    monkeypatch.setattr(win_tray_icon, "WindowsTrayIcon", _explode)

    tray = create_tray_icon(QtCore.QObject(), QtWidgets.QApplication.windowIcon())

    assert isinstance(tray, QtWidgets.QSystemTrayIcon)
