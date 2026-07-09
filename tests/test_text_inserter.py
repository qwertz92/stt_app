import ctypes

import pytest

from stt_app.text_inserter import (
    ClipboardContentionError,
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
        self.last_requested_mode = None

    def send_paste(self, target_hwnd=None):
        self.last_target_hwnd = target_hwnd
        self.calls.append(f"paste:{target_hwnd}")
        if self.raise_on_paste:
            raise RuntimeError("send failed")
        return self.paste_mode

    def send_paste_with_mode(self, mode, target_hwnd=None):
        self.last_requested_mode = mode
        return self.send_paste(target_hwnd=target_hwnd)


class SequencedPasteBackend(PasteBackend):
    def __init__(self, paste_mode="send_input"):
        super().__init__(paste_mode=paste_mode)
        self.sequence = 100
        self.pending_paste = False
        self.target_text = ""

    def set_clipboard_text(self, text):
        self.calls.append(f"set:{text}")
        self.state = {"has_text": True, "text": text}
        self.sequence += 1

    def restore_clipboard_state(self, state):
        self.calls.append("restore")
        if isinstance(state, dict):
            self.state = dict(state)
        else:
            self.state = {
                "has_text": bool(state.has_text),
                "text": state.text,
            }
        self.sequence += 1

    def get_clipboard_sequence_number(self):
        return self.sequence

    def get_clipboard_text(self):
        return self.state["text"] if self.state["has_text"] else None

    def send_paste(self, target_hwnd=None):
        self.last_target_hwnd = target_hwnd
        self.calls.append(f"paste:{target_hwnd}")
        if self.raise_on_paste:
            raise RuntimeError("send failed")
        self.pending_paste = True
        return self.paste_mode

    def consume_pending_paste(self):
        if not self.pending_paste:
            return
        self.pending_paste = False
        if self.state["has_text"] and self.state["text"] is not None:
            self.target_text += self.state["text"]

    def simulate_user_copy(self, text):
        self.state = {"has_text": True, "text": text}
        self.sequence += 1

    def simulate_sequence_bump(self):
        self.sequence += 1


class GatedPasteBackend(PasteBackend):
    """Backend faking the modifier-release and target-responsiveness gates."""

    def __init__(self, paste_mode="send_input", target_ready=True):
        super().__init__(paste_mode=paste_mode)
        self.target_ready = target_ready

    def wait_for_modifier_release(self):
        self.calls.append("wait_modifiers")
        return True

    def wait_for_paste_target_ready(self, target_hwnd=None):
        self.calls.append(f"wait_target:{target_hwnd}")
        return self.target_ready


def test_text_inserter_waits_for_modifier_release_before_touching_clipboard():
    backend = GatedPasteBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    assert inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
    )

    assert backend.calls == [
        "wait_modifiers",
        "capture",
        "set:hello",
        "paste:123",
        "wait_target:123",
        "restore",
    ]


def test_text_inserter_skips_gates_for_wm_paste_mode():
    """WM_PASTE is message-based: held modifiers cannot corrupt it and the
    synchronous SendMessageTimeout already proves the target processed it."""
    backend = GatedPasteBackend(paste_mode="wm_paste")
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    assert inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="wm_paste",
    )

    assert backend.calls == ["capture", "set:hello", "paste:123", "restore"]


def test_text_inserter_skips_restore_when_target_stays_unresponsive():
    """An unresponsive target has not read the clipboard yet; restoring would
    make its late Ctrl+V paste the previous clipboard content."""
    backend = GatedPasteBackend(target_ready=False)
    sleep_calls = []
    inserter = TextInserter(
        backend=backend,
        sleep_fn=sleep_calls.append,
        clipboard_settle_s=0.05,
        sendinput_restore_delay_s=0.2,
    )

    assert inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
    )

    assert "restore" not in backend.calls
    assert sleep_calls == [0.05]


def test_text_inserter_leaves_transcript_when_restore_disabled():
    backend = GatedPasteBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    assert inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
        restore_clipboard=False,
    )

    assert "restore" not in backend.calls
    assert backend.calls[-1] == "wait_target:123"


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

    result = inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="wm_paste",
    )

    assert result is True
    assert backend.calls == ["capture", "set:hello", "paste:123", "restore"]
    assert backend.last_target_hwnd == 123
    assert backend.last_requested_mode == "wm_paste"
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

    result = inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
    )

    assert result is True
    assert backend.calls == ["capture", "set:hello", "paste:123", "restore"]
    assert backend.last_requested_mode == "send_input"
    assert sleep_calls == [0.05, 0.2]


def test_text_inserter_aborts_if_clipboard_changes_before_paste():
    backend = SequencedPasteBackend()
    sleep_calls = []

    def sleep(value):
        sleep_calls.append(value)
        if len(sleep_calls) == 1:
            backend.simulate_user_copy("user text")

    inserter = TextInserter(
        backend=backend,
        sleep_fn=sleep,
        clipboard_settle_s=0.05,
        sendinput_restore_delay_s=0.2,
    )

    with pytest.raises(ClipboardContentionError) as error:
        inserter.insert_text_with_options(
            "hello",
            target_hwnd=123,
            paste_mode="send_input",
        )

    assert error.value.allow_clipboard_fallback is False
    assert backend.calls == ["capture", "set:hello"]
    assert backend.state["text"] == "user text"


def test_text_inserter_preserves_user_clipboard_change_during_paste_window():
    backend = SequencedPasteBackend()
    sleep_calls = []

    def sleep(value):
        sleep_calls.append(value)
        if len(sleep_calls) == 2:
            backend.simulate_user_copy("copied while pasting")

    inserter = TextInserter(
        backend=backend,
        sleep_fn=sleep,
        clipboard_settle_s=0.05,
        sendinput_restore_delay_s=0.2,
    )

    with pytest.raises(ClipboardContentionError) as error:
        inserter.insert_text_with_options(
            "hello",
            target_hwnd=123,
            paste_mode="send_input",
        )

    assert error.value.allow_clipboard_fallback is False
    assert backend.calls == ["capture", "set:hello", "paste:123"]
    assert backend.state["text"] == "copied while pasting"


def test_text_inserter_tolerates_sequence_change_when_text_is_unchanged():
    backend = SequencedPasteBackend()
    sleep_calls = []

    def sleep(value):
        sleep_calls.append(value)
        if len(sleep_calls) == 1:
            backend.simulate_sequence_bump()
        if len(sleep_calls) == 2:
            backend.consume_pending_paste()

    inserter = TextInserter(
        backend=backend,
        sleep_fn=sleep,
        clipboard_settle_s=0.05,
        sendinput_restore_delay_s=0.2,
    )

    assert inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
    )

    assert backend.target_text == "hello"
    assert backend.state["text"] == "old"


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
