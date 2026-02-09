import ctypes

import pytest

from tts_app.text_inserter import (
    INPUT,
    TextInserter,
    TextInsertionError,
    _format_sendinput_failure,
)


class LegacyBackend:
    def __init__(self, raise_on_paste=False, raise_on_restore=False):
        self.raise_on_paste = raise_on_paste
        self.raise_on_restore = raise_on_restore
        self.calls = []
        self.state = {"has_text": True, "text": "old"}

    def capture_clipboard_state(self):
        self.calls.append("capture")
        return dict(self.state)

    def set_clipboard_text(self, text):
        self.calls.append(f"set:{text}")

    def send_ctrl_v(self):
        self.calls.append("paste_ctrl_v")
        if self.raise_on_paste:
            raise RuntimeError("send failed")

    def restore_clipboard_state(self, state):
        self.calls.append("restore")
        if self.raise_on_restore:
            raise RuntimeError("restore failed")
        self.state = dict(state)


class PasteBackend(LegacyBackend):
    def __init__(self, paste_mode="wm_paste", raise_on_paste=False, raise_on_restore=False):
        super().__init__(raise_on_paste=raise_on_paste, raise_on_restore=raise_on_restore)
        self.paste_mode = paste_mode
        self.last_target_hwnd = None

    def send_paste(self, target_hwnd=None):
        self.last_target_hwnd = target_hwnd
        self.calls.append(f"paste:{target_hwnd}")
        if self.raise_on_paste:
            raise RuntimeError("send failed")
        return self.paste_mode


def test_text_inserter_saves_and_restores_clipboard():
    backend = LegacyBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    result = inserter.insert_text("hello world")

    assert result is True
    assert backend.calls == ["capture", "set:hello world", "paste_ctrl_v", "restore"]
    assert backend.state["text"] == "old"


def test_text_inserter_restores_clipboard_when_paste_fails():
    backend = LegacyBackend(raise_on_paste=True)
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextInsertionError):
        inserter.insert_text("hello")

    assert backend.calls[-1] == "restore"


def test_text_inserter_raises_when_restore_fails_after_paste():
    backend = LegacyBackend(raise_on_restore=True)
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextInsertionError) as error:
        inserter.insert_text("hello")

    assert "clipboard restore failed" in str(error.value).lower()


def test_text_inserter_raises_when_paste_and_restore_fail():
    backend = LegacyBackend(raise_on_paste=True, raise_on_restore=True)
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextInsertionError) as error:
        inserter.insert_text("hello")

    assert "failed to paste text" in str(error.value).lower()
    assert "failed to restore clipboard" in str(error.value).lower()


def test_text_inserter_ignores_empty_text():
    backend = LegacyBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    result = inserter.insert_text("   ")

    assert result is False
    assert backend.calls == []


def test_text_inserter_uses_wm_paste_without_restore_delay():
    backend = PasteBackend(paste_mode="wm_paste")
    sleep_calls = []
    inserter = TextInserter(
        backend=backend,
        sleep_fn=sleep_calls.append,
        clipboard_settle_s=0.05,
        sendinput_restore_delay_s=0.2,
    )

    result = inserter.insert_text("hello", target_hwnd=123)

    assert result is True
    assert backend.calls == ["capture", "set:hello", "paste:123", "restore"]
    assert backend.last_target_hwnd == 123
    assert sleep_calls == [0.05]


def test_text_inserter_waits_before_restore_after_sendinput_paste():
    backend = PasteBackend(paste_mode="send_input")
    sleep_calls = []
    inserter = TextInserter(
        backend=backend,
        sleep_fn=sleep_calls.append,
        clipboard_settle_s=0.05,
        sendinput_restore_delay_s=0.2,
    )

    result = inserter.insert_text("hello", target_hwnd=123)

    assert result is True
    assert backend.calls == ["capture", "set:hello", "paste:123", "restore"]
    assert sleep_calls == [0.05, 0.2]


def test_format_sendinput_failure_uipi_message():
    msg = _format_sendinput_failure(sent=0, expected=4, error_code=5)
    assert "UIPI" in msg


def test_format_sendinput_failure_nonzero_error():
    msg = _format_sendinput_failure(sent=0, expected=4, error_code=87)
    assert "WinError 87" in msg


def test_format_sendinput_failure_zero_error_zero_sent():
    msg = _format_sendinput_failure(sent=0, expected=4, error_code=0)
    assert "sent 0 events" in msg


def test_input_struct_size_matches_windows_expectation():
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(INPUT) == expected
