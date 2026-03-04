from stt_app.settings_dialog import (
    _app_hotkey_to_qt_hotkey_text,
    _hotkeys_conflict,
    _qt_hotkey_text_to_app_hotkey,
)


def test_qt_hotkey_text_to_app_hotkey_maps_meta_to_win():
    assert (
        _qt_hotkey_text_to_app_hotkey("Ctrl+Meta+Shift+Space")
        == "Ctrl+Win+Shift+Space"
    )


def test_qt_hotkey_text_to_app_hotkey_uses_first_sequence_only():
    assert _qt_hotkey_text_to_app_hotkey("Ctrl+Alt+Space, Ctrl+K") == "Ctrl+Alt+Space"


def test_app_hotkey_to_qt_hotkey_text_maps_win_to_meta():
    assert _app_hotkey_to_qt_hotkey_text("Ctrl+Win+LShift") == "Ctrl+Meta+LShift"


def test_empty_hotkey_conversion():
    assert _qt_hotkey_text_to_app_hotkey("") == ""
    assert _app_hotkey_to_qt_hotkey_text("") == ""


def test_hotkeys_conflict_when_identical_or_subset():
    assert _hotkeys_conflict("Ctrl+Alt+Space", "Ctrl+Alt+Space") is True
    assert _hotkeys_conflict("Ctrl+Alt+Space", "Ctrl+Alt+Shift+Space") is True
    assert _hotkeys_conflict("Ctrl+Alt+Shift+Space", "Ctrl+Alt+Space") is True


def test_hotkeys_no_conflict_when_distinct():
    assert _hotkeys_conflict("Ctrl+Alt+Space", "Ctrl+Shift+F12") is False
