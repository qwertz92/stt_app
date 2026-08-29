import ctypes
from types import SimpleNamespace

import pytest

import stt_app.text_inserter as text_inserter
from stt_app.text_inserter import (
    INPUT,
    ClipboardContentionError,
    ClipboardEmptiedError,
    TextInserter,
    TextInsertionError,
    TextMayHaveBeenPastedError,
    Win32ClipboardBackend,
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


def test_text_inserter_aborts_before_clipboard_when_modifiers_stay_held():
    backend = GatedPasteBackend()
    backend.wait_for_modifier_release = lambda: False
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextInsertionError, match="remained held"):
        inserter.insert_text_with_options(
            "hello",
            target_hwnd=123,
            paste_mode="send_input",
        )

    assert backend.calls == []


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


def test_partial_sendinput_sends_keyup_cleanup_without_replaying(monkeypatch):
    import stt_app.text_inserter as text_inserter_module

    calls = []

    class FakeSendInput:
        argtypes = None
        restype = None

        def __call__(self, count, _inputs, _size):
            calls.append(int(count))
            return 2 if len(calls) == 1 else int(count)

    class FakeUser32:
        SendInput = FakeSendInput()

    monkeypatch.setattr(
        text_inserter_module.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: FakeUser32(),
        raising=False,
    )
    monkeypatch.setattr(
        text_inserter_module.ctypes,
        "set_last_error",
        lambda _value: None,
        raising=False,
    )
    monkeypatch.setattr(
        text_inserter_module.ctypes,
        "get_last_error",
        lambda: 0,
        raising=False,
    )

    with pytest.raises(TextInsertionError, match="sent 2/4"):
        text_inserter_module._send_ctrl_v_input()

    assert calls == [4, 2]


def test_format_sendinput_failure_nonzero_error():
    msg = _format_sendinput_failure(sent=0, expected=4, error_code=87)
    assert "WinError 87" in msg


def test_format_sendinput_failure_zero_error_zero_sent():
    msg = _format_sendinput_failure(sent=0, expected=4, error_code=0)
    assert "sent 0 events" in msg


def test_input_struct_size_matches_windows_expectation():
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(INPUT) == expected

class _ClipboardBusyAfterPaste:
    """A clipboard another program holds open once the paste has gone out."""

    def __init__(self):
        self.pasted = False
        self.sequence = 1

    def capture_clipboard_state(self):
        return object()

    def set_clipboard_text(self, text):
        self.sequence += 1

    def clipboard_sequence_number(self):
        return self.sequence

    def get_clipboard_text(self):
        if self.pasted:
            raise OSError("clipboard is open in another program")
        return "hello"

    def send_paste_with_mode(self, mode, target_hwnd=None):
        self.pasted = True
        return "send_input"

    def restore_clipboard_state(self, state):
        pass


def test_a_failure_after_the_paste_keystroke_is_never_retryable():
    """Classification has to follow the keystroke, not the raise site.

    Streaming live insertion reads a failed insert as "those words are not
    in the document" and offers them again on the next partial. That is
    right only while the paste has not been sent. Here it has: the text is
    in the target and only the verification read failed, because a clipboard
    manager had the clipboard open -- an ordinary thing to have running.
    Retrying pastes the phrase twice.
    """
    inserter = TextInserter(
        backend=_ClipboardBusyAfterPaste(), sleep_fn=lambda seconds: None
    )

    with pytest.raises(TextInsertionError) as excinfo:
        inserter.insert_text("hello")

    assert isinstance(excinfo.value, TextMayHaveBeenPastedError), (
        f"{type(excinfo.value).__name__} is retryable, so the streaming "
        "retry will paste the same words a second time"
    )


def test_a_backend_returning_a_nonnumeric_sequence_does_not_escape_unclassified():
    """`_clipboard_sequence_number` must honour its "never raises" contract.

    `int()` used to sit *outside* its own `try`, so a backend handing back
    anything non-numeric raised straight through. That matters because
    `_clipboard_changed_after_set` is also called from inside the paste
    block's `except` arm, whose only handler is `except
    ClipboardContentionError` -- so the exception would escape the whole
    classification, past a paste keystroke that has already gone out, and out
    of a Qt slot. Every caller treats the value as "a number or None".
    """
    backend = SequencedPasteBackend()
    backend.get_clipboard_sequence_number = lambda: "not a number"
    inserter = TextInserter(
        backend=backend,
        sleep_fn=lambda _seconds: None,
    )

    assert inserter._clipboard_sequence_number() is None

    # And the whole paste still completes rather than raising: with no usable
    # marker the check falls back to comparing the clipboard text.
    assert inserter.insert_text("hello") is True
    assert "paste:None" in backend.calls


def test_a_backend_whose_sequence_getter_raises_is_also_absorbed():
    backend = SequencedPasteBackend()

    def _boom():
        raise OSError("the clipboard is locked by another program")

    backend.get_clipboard_sequence_number = _boom
    inserter = TextInserter(backend=backend, sleep_fn=lambda _seconds: None)

    assert inserter._clipboard_sequence_number() is None
    assert inserter.insert_text("hello") is True


class _FakeSendInput:
    """A stand-in for `user32.SendInput` that also carries the ctypes attributes.

    `_send_input_batch` declares `argtypes`/`restype` on the function object
    before calling it, which a plain bound method or lambda cannot hold.
    """

    def __init__(self, deliver):
        self._deliver = deliver
        self.batches = []
        self.argtypes = None
        self.restype = None

    def __call__(self, count, _inputs, _size):
        self.batches.append(int(count))
        return self._deliver(int(count))


def _fake_user32(send_input):
    class _User32:
        def __init__(self, *_args, **_kwargs):
            self.SendInput = send_input

    return _User32


class _ProbeCountingBackend(Win32ClipboardBackend):
    """A real backend with only the two Win32 calls under test replaced."""

    def __init__(self, *, is_window=True, answers=False):
        super().__init__()
        self._is_window_result = is_window
        self._answers = answers
        self.probes = 0

    def _is_window(self, hwnd):
        return self._is_window_result

    def _send_message_timeout(self, hwnd, message, timeout_ms):
        self.probes += 1
        return self._answers


def test_the_readiness_wait_sleeps_between_probes_instead_of_spinning():
    """`SMTO_ABORTIFHUNG` returns instantly for exactly the target this waits on.

    The probe timeout throttles the loop only while the target is merely busy.
    Windows returns at once once it considers the thread hung -- which is the
    case the wait exists for -- so the loop had no bound at all. Measured
    against a handle that names no window, on the Qt main thread: 953,446
    probes in 1.994 s at 100% of one core, and a streaming dictation pastes
    every 0.35 s.
    """
    backend = _ProbeCountingBackend()

    ready = backend.wait_for_paste_target_ready(
        1234, timeout_s=0.2, poll_interval_s=0.05
    )

    assert ready is False, "an unresponsive target must still be reported"
    assert backend.probes <= 10, (
        f"the loop is still spinning: {backend.probes} probes in 0.2 s at a "
        "50 ms poll interval"
    )


def test_a_responsive_target_still_answers_on_the_first_probe():
    """The sleep must not cost anything in the case that happens every time."""
    backend = _ProbeCountingBackend(answers=True)

    assert backend.wait_for_paste_target_ready(1234) is True
    assert backend.probes == 1


def test_a_handle_that_names_no_window_is_not_waited_on():
    """The probe target is the caret child control, which can be stale.

    `_target_insert_window` hands over `GUITHREADINFO.hwndCaret`, and only the
    *top-level* window is validated before the paste. An application that
    recreated that control -- a closed editor tab -- left a handle that can
    never answer, and the wait then spent its whole budget deciding that an
    application which is running fine is unresponsive.
    """
    backend = _ProbeCountingBackend(is_window=False)

    assert backend.wait_for_paste_target_ready(1234) is True
    assert backend.probes == 0, "a dead handle was probed anyway"


def test_a_partially_delivered_ctrl_v_is_reported_as_maybe_pasted(monkeypatch):
    """`[Ctrl-down, V-down, V-up, Ctrl-up]`: two delivered events are a paste.

    Applications paste on the key-down, so a `SendInput` that placed two of the
    four events has already had its effect. It was reported as a plain
    `TextInsertionError`, the class that means "safe to retry" -- so streaming
    live insertion offered the same words up to three more times and the
    overlay offered Insert for a fourth.
    """
    send_input = _FakeSendInput(lambda count: 2 if count == 4 else count)
    monkeypatch.setattr(
        text_inserter.ctypes, "WinDLL", _fake_user32(send_input)
    )

    with pytest.raises(TextMayHaveBeenPastedError):
        text_inserter._send_ctrl_v_input()

    assert send_input.batches == [4, 2], (
        f"the key-up cleanup batch was not sent: {send_input.batches}"
    )


def test_nothing_delivered_at_all_stays_retryable(monkeypatch):
    """The other side of the same cut: no event landed, so nothing was pasted."""

    send_input = _FakeSendInput(lambda _count: 0)
    monkeypatch.setattr(
        text_inserter.ctypes, "WinDLL", _fake_user32(send_input)
    )

    with pytest.raises(TextInsertionError) as raised:
        text_inserter._send_ctrl_v_input()

    assert not isinstance(raised.value, TextMayHaveBeenPastedError)


def test_auto_mode_does_not_paste_again_after_a_partial_sendinput(monkeypatch):
    """`auto` is the default, so this was the shipped path.

    `send_paste_with_mode` caught every `SendInput` failure and fell through to
    WM_PASTE. After a partial send that is a second paste of the same
    transcript inside one call, with the insert then returning success.
    """
    backend = Win32ClipboardBackend()
    wm_paste_calls = []

    def _partial_ctrl_v():
        raise TextMayHaveBeenPastedError("SendInput partially failed (sent 2/4).")

    monkeypatch.setattr(backend, "send_ctrl_v", _partial_ctrl_v)
    monkeypatch.setattr(
        backend,
        "_send_wm_paste",
        lambda target_hwnd=None: wm_paste_calls.append(target_hwnd) or True,
    )

    with pytest.raises(TextMayHaveBeenPastedError):
        backend.send_paste_with_mode("auto", target_hwnd=4321)

    assert wm_paste_calls == [], (
        "the transcript was pasted a second time through WM_PASTE"
    )


def test_auto_mode_still_falls_back_when_nothing_was_delivered(monkeypatch):
    """The fallback is the reason `auto` exists and must survive the fix."""
    backend = Win32ClipboardBackend()
    wm_paste_calls = []

    def _refused_ctrl_v():
        raise TextInsertionError("SendInput failed (sent 0 events).")

    monkeypatch.setattr(backend, "send_ctrl_v", _refused_ctrl_v)
    monkeypatch.setattr(
        backend,
        "_send_wm_paste",
        lambda target_hwnd=None: wm_paste_calls.append(target_hwnd) or True,
    )

    assert backend.send_paste_with_mode("auto", target_hwnd=4321) == "wm_paste"
    assert wm_paste_calls == [4321]


class _UnwritableClipboardBackend(GatedPasteBackend):
    """The clipboard cannot be opened, so this app never changed it."""

    def set_clipboard_text(self, text):
        self.calls.append(f"set:{text}")
        raise TextInsertionError("Failed to open clipboard.")


def test_a_clipboard_this_app_never_set_is_never_restored_over():
    """`restore_clipboard_state` empties the clipboard and returns text only.

    So running it after a *failed* set destroyed whatever was on the clipboard
    -- an image, a file selection copied in Explorer -- although the app had
    not touched it and had pasted nothing. The documented "Unicode text only"
    limitation is the price of a restore that was actually needed; this one
    was gratuitous.
    """
    backend = _UnwritableClipboardBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextInsertionError) as raised:
        inserter.insert_text_with_options(
            "hello", target_hwnd=123, paste_mode="send_input"
        )

    assert "restore" not in backend.calls, (
        f"a clipboard this app never set was restored over: {backend.calls}"
    )
    assert not isinstance(raised.value, TextMayHaveBeenPastedError), (
        "nothing was pasted, so this must stay retryable"
    )


def test_a_restore_that_fails_after_the_paste_is_never_retryable():
    """The second of the two after-paste paths, and it was unpinned.

    The text is in the document and only the clipboard cleanup failed. Reported
    as a plain `TextInsertionError` the streaming retry pastes it again, and the
    overlay offers Insert for a third copy.
    """
    backend = GatedPasteBackend()
    backend.raise_on_restore = True
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextMayHaveBeenPastedError) as raised:
        inserter.insert_text_with_options(
            "hello", target_hwnd=123, paste_mode="send_input"
        )

    assert "clipboard restore failed" in str(raised.value)


def test_one_delivered_event_is_a_held_ctrl_and_stays_retryable(monkeypatch):
    """The boundary the count is chosen at.

    One delivered event is Ctrl-down on its own: no V reached the target, so
    nothing was pasted and the words must still be offered again. Two is the
    key-down applications paste on.
    """
    send_input = _FakeSendInput(lambda count: 1 if count == 4 else count)
    monkeypatch.setattr(
        text_inserter.ctypes, "WinDLL", _fake_user32(send_input)
    )

    with pytest.raises(TextInsertionError) as raised:
        text_inserter._send_ctrl_v_input()

    assert not isinstance(raised.value, TextMayHaveBeenPastedError), (
        "a Ctrl-down that never became a paste was reported as maybe-pasted"
    )


class _MaybePastedBackend(GatedPasteBackend):
    """The paste keystroke went out and then the send reported a partial."""

    def send_paste_with_mode(self, mode, target_hwnd=None):
        self.last_requested_mode = mode
        self.calls.append(f"paste:{target_hwnd}")
        raise TextMayHaveBeenPastedError("SendInput partially failed (sent 2/4).")


def test_a_paste_that_may_have_landed_leaves_the_transcript_on_the_clipboard():
    """Restoring would make the target's late read take the old content.

    Same trade the unresponsive-target branch makes: once the keystroke is out
    and the target has not demonstrably consumed it, putting the previous
    clipboard back is what turns a late paste into the wrong text.
    """
    backend = _MaybePastedBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextMayHaveBeenPastedError):
        inserter.insert_text_with_options(
            "hello", target_hwnd=123, paste_mode="send_input"
        )

    assert "restore" not in backend.calls, (
        f"the previous clipboard was restored under a live paste: {backend.calls}"
    )


class _EmptiedThenFailedBackend(LegacyBackend):
    """`EmptyClipboard` worked, `SetClipboardText` did not."""

    def set_clipboard_text(self, text):
        self.calls.append(f"set:{text}")
        self.state = {"has_text": False, "text": None}
        raise ClipboardEmptiedError(
            "The clipboard was emptied but could not be written: access denied"
        )


class _CouldNotOpenBackend(LegacyBackend):
    """`OpenClipboard` failed, so nothing was touched."""

    def set_clipboard_text(self, text):
        self.calls.append(f"set:{text}")
        raise TextInsertionError("OpenClipboard failed")


@pytest.mark.parametrize(
    ("label", "backend_class", "expect_restore"),
    [
        ("emptied, then the write failed", _EmptiedThenFailedBackend, True),
        ("the clipboard could not be opened", _CouldNotOpenBackend, False),
    ],
)
def test_a_failed_set_restores_only_what_we_actually_destroyed(
    label, backend_class, expect_restore
):
    """Setting the clipboard is two calls and only the first one destroys.

    Skipping the restore for every failed set was half right. It protects the
    case where `OpenClipboard` failed and the clipboard still holds an image
    or a file selection this app never touched -- restoring there would
    replace them with plain text. But when `EmptyClipboard` succeeded and the
    write did not, the clipboard is already empty, and skipping the restore is
    what finally loses the user's content.
    """
    backend = backend_class()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextInsertionError):
        inserter.insert_text("transcript")

    restored = "restore" in backend.calls
    assert restored is expect_restore, f"{label}: calls were {backend.calls}"
    if expect_restore:
        assert backend.state["text"] == "old", (
            f"{label}: the clipboard was left empty"
        )
    assert "paste_ctrl_v" not in backend.calls, (
        f"{label}: a failed set must not be followed by a paste"
    )


class _FakeWin32Clipboard:
    """Just enough of `win32clipboard` to drive the two-call set."""

    def __init__(self, *, empty_raises=False, set_raises=False):
        self._empty_raises = empty_raises
        self._set_raises = set_raises
        self.calls = []

    def OpenClipboard(self):
        self.calls.append("open")

    def CloseClipboard(self):
        self.calls.append("close")

    def EmptyClipboard(self):
        self.calls.append("empty")
        if self._empty_raises:
            raise OSError("EmptyClipboard failed")

    def SetClipboardText(self, _text, _format):
        self.calls.append("set")
        if self._set_raises:
            raise OSError("SetClipboardText failed")


@pytest.mark.parametrize(
    ("label", "empty_raises", "set_raises", "expected"),
    [
        ("the write failed after the empty", False, True, ClipboardEmptiedError),
        ("the empty itself failed", True, False, OSError),
        ("both succeeded", False, False, None),
    ],
)
def test_the_backend_reports_an_emptied_clipboard_distinctly(
    monkeypatch, label, empty_raises, set_raises, expected
):
    """Only the real backend can decide which half of the set went through.

    A test that raises `ClipboardEmptiedError` from a fake backend proves the
    caller handles it, not that anything ever produces it -- the production
    `set_clipboard_text` could raise a plain `TextInsertionError` and stay
    green. `EmptyClipboard` failing is the opposite case and must NOT be
    reported as emptied: nothing was destroyed there.
    """
    fake = _FakeWin32Clipboard(empty_raises=empty_raises, set_raises=set_raises)
    monkeypatch.setattr(text_inserter, "win32clipboard", fake)
    monkeypatch.setattr(text_inserter, "win32con", SimpleNamespace(CF_UNICODETEXT=13))
    backend = Win32ClipboardBackend()

    if expected is None:
        backend.set_clipboard_text("transcript")
    else:
        with pytest.raises(expected) as excinfo:
            backend.set_clipboard_text("transcript")
        if empty_raises:
            assert not isinstance(excinfo.value, ClipboardEmptiedError), (
                "a failed EmptyClipboard destroyed nothing, so reporting it as "
                "emptied would restore plain text over an untouched clipboard"
            )

    assert fake.calls[0] == "open" and fake.calls[-1] == "close", (
        f"{label}: the clipboard was left open: {fake.calls}"
    )
