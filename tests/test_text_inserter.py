import ctypes
import logging
import threading
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
        if self.raise_on_restore:
            raise RuntimeError("restore failed")
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

    def simulate_silent_overwrite(self, text):
        """A foreign write whose sequence bump this app never observes.

        The counter is read once per check, so a writer that lands between two
        reads moves it without the app seeing a difference. Only comparing the
        content finds this one.
        """
        self.state = {"has_text": True, "text": text}

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


class SequencedGatedBackend(SequencedPasteBackend):
    """A clipboard with a sequence counter plus both Win32 gates.

    The combination the deferred restore needs: it has to decide, at restore
    time, whether the clipboard still holds what this app wrote, and it asks
    the target whether it has caught up first.
    """

    def __init__(self, paste_mode="send_input", target_ready=True):
        super().__init__(paste_mode=paste_mode)
        self.target_ready = target_ready

    def wait_for_modifier_release(self):
        self.calls.append("wait_modifiers")
        return True

    def wait_for_paste_target_ready(self, target_hwnd=None):
        self.calls.append(f"wait_target:{target_hwnd}")
        return self.target_ready


class ForegroundAwareBackend(SequencedPasteBackend):
    """A backend that can answer which window is in the foreground.

    `foreground` is a plain attribute so a test can change it from inside the
    settle sleep -- which is exactly the window the re-validation covers.
    """

    def __init__(self, foreground=4242):
        super().__init__(paste_mode="send_input")
        self.foreground = foreground
        self.foreground_reads = []

    def get_foreground_window(self):
        self.foreground_reads.append(self.foreground)
        return self.foreground


class _ScheduledRestore:
    """One entry of `RecordingScheduler`, standing in for a `threading.Timer`."""

    def __init__(self, delay_s, callback):
        self.delay_s = delay_s
        self.callback = callback
        self.cancelled = False
        self.fired = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        self.fired = True
        self.callback()


class RecordingScheduler:
    """A `schedule_fn` that records instead of starting a thread.

    Tests drive the clipboard restore by hand with `fire_pending()`, so no
    timing is involved anywhere in this file.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, delay_s, callback):
        handle = _ScheduledRestore(delay_s, callback)
        self.calls.append(handle)
        return handle

    @property
    def delays(self):
        return [handle.delay_s for handle in self.calls]

    @property
    def pending(self):
        return [h for h in self.calls if not h.cancelled and not h.fired]

    def fire_pending(self):
        """Run every entry that is neither cancelled nor already fired.

        The pending list is snapshotted first, so an entry a callback
        reschedules waits for the next `fire_pending()` -- one call is one
        step of the timer, not a loop to exhaustion.
        """
        due = list(self.pending)
        for handle in due:
            handle.run()
        return len(due)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_text_inserter_waits_for_modifier_release_before_touching_clipboard():
    backend = GatedPasteBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    assert inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
    )

    # The readiness probe and the restore have moved behind the scheduler, so
    # the transaction itself ends at the keystroke. The order within each half
    # is what this test is about and is unchanged.
    assert backend.calls == [
        "wait_modifiers",
        "capture",
        "set:hello",
        "paste:123",
    ]

    scheduler.fire_pending()

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
    make its late Ctrl+V paste the previous clipboard content.

    The gate itself is unchanged; only where it runs is. It used to hold the
    Qt main thread for up to PASTE_TARGET_RESPONSIVE_TIMEOUT_S inside the
    transaction, and now runs on the deferred restore's own thread.
    """
    backend = SequencedGatedBackend(target_ready=False)
    scheduler = RecordingScheduler()
    clock = FakeClock()
    sleep_calls = []
    inserter = TextInserter(
        backend=backend,
        sleep_fn=sleep_calls.append,
        clipboard_settle_s=0.05,
        restore_delay_s=0.2,
        restore_max_wait_s=1.0,
        schedule_fn=scheduler,
        clock=clock,
    )

    assert inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
    )

    clock.advance(2.0)  # past the deadline, so the record is not rescheduled
    scheduler.fire_pending()

    assert "restore" not in backend.calls
    assert backend.state["text"] == "hello", (
        "the transcript must stay on the clipboard for the target's late read"
    )
    assert sleep_calls == [0.05]


def test_text_inserter_leaves_transcript_when_restore_disabled():
    backend = GatedPasteBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    assert inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
        restore_clipboard=False,
    )

    assert "restore" not in backend.calls
    # Nothing is armed either, so no thread and no readiness probe are spent
    # deciding not to restore.
    assert scheduler.calls == []
    assert backend.calls[-1] == "paste:123"


def test_text_inserter_saves_and_restores_clipboard():
    backend = LegacyBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    result = inserter.insert_text("hello world")

    assert result is True
    scheduler.fire_pending()
    assert backend.calls == ["capture", "set:hello world", "paste_ctrl_v", "restore"]
    assert backend.state["text"] == "old"


def test_text_inserter_restores_clipboard_when_paste_fails():
    backend = LegacyBackend(raise_on_paste=True)
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextInsertionError):
        inserter.insert_text("hello")

    assert backend.calls[-1] == "restore"


def test_text_inserter_raises_when_restore_fails_after_a_wm_paste():
    """The synchronous road is the only one that can still report this.

    `SendMessageTimeout(WM_PASTE)` returns after the target's handler ran, so
    the restore that follows it is inside the call and its failure has a
    caller to reach. After a SendInput paste the restore is deferred and the
    call has long returned success, so that half is logged instead -- see
    `test_a_restore_that_fails_after_a_sendinput_paste_is_logged_and_not_raised`.
    """
    backend = PasteBackend(paste_mode="wm_paste", raise_on_restore=True)
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextInsertionError) as error:
        inserter.insert_text_with_options(
            "hello", target_hwnd=123, paste_mode="wm_paste"
        )

    assert "clipboard restore failed" in str(error.value).lower()
    assert isinstance(error.value, TextMayHaveBeenPastedError), (
        "the text is in the document; a retry would paste it twice"
    )


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
    """`SendMessageTimeout(WM_PASTE)` returns after the handler ran.

    The target has demonstrably read the clipboard by then, so this road keeps
    restoring inside the call: there is nothing left to wait for, and deferring
    it would hold the transcript on the clipboard for no reason.
    """
    backend = PasteBackend(paste_mode="wm_paste")
    scheduler = RecordingScheduler()
    sleep_calls = []
    inserter = TextInserter(
        backend=backend,
        sleep_fn=sleep_calls.append,
        clipboard_settle_s=0.05,
        restore_delay_s=0.2,
        schedule_fn=scheduler,
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
    assert scheduler.calls == [], "the synchronous road armed a deferred restore"


def test_the_restore_after_a_sendinput_paste_is_deferred_off_the_calling_thread():
    """Replaces `test_text_inserter_waits_before_restore_after_sendinput_paste`.

    The old shape slept `SENDINPUT_RESTORE_DELAY_S` (160 ms) on the Qt main
    thread and restored inside the call. Electron/Chromium targets read the
    clipboard from their renderer process seconds later under CPU load, so
    160 ms was never enough, and raising it was not available: streaming
    inserts run every ~350 ms and the sleep froze the UI. The wait is now a
    scheduled callback and the transaction ends at the keystroke.
    """
    backend = PasteBackend(paste_mode="send_input")
    scheduler = RecordingScheduler()
    sleep_calls = []
    inserter = TextInserter(
        backend=backend,
        sleep_fn=sleep_calls.append,
        clipboard_settle_s=0.05,
        restore_delay_s=1.5,
        schedule_fn=scheduler,
    )

    result = inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
    )

    assert result is True
    assert backend.calls == ["capture", "set:hello", "paste:123"]
    assert backend.last_requested_mode == "send_input"
    assert sleep_calls == [0.05], (
        f"the calling thread slept for the restore delay: {sleep_calls}"
    )
    assert scheduler.delays == [1.5]

    assert scheduler.fire_pending() == 1
    assert backend.calls == ["capture", "set:hello", "paste:123", "restore"]


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
        restore_delay_s=0.2,
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
    """Same intent, new road: the paste window is now the deferred restore.

    The user's copy used to land during the 160 ms sleep and was caught by the
    check before the restore. There is no sleep any more, so the window it
    covered is the 1.5 s the transcript sits on the clipboard -- and the guard
    that closes it is the content check inside the deferred restore.
    """
    backend = SequencedGatedBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend,
        sleep_fn=lambda _s: None,
        clipboard_settle_s=0.05,
        restore_delay_s=1.5,
        schedule_fn=scheduler,
    )

    assert inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
    )

    backend.simulate_user_copy("copied while pasting")
    scheduler.fire_pending()

    assert "restore" not in backend.calls
    assert backend.state["text"] == "copied while pasting"


def test_text_inserter_tolerates_sequence_change_when_text_is_unchanged():
    """A bumped counter over identical text is not a foreign write.

    Clipboard managers and the app's own write can move the counter without
    changing what is on the clipboard, and refusing to restore then would
    leave the transcript in place for good.
    """
    backend = SequencedGatedBackend()
    scheduler = RecordingScheduler()
    sleep_calls = []

    def sleep(value):
        sleep_calls.append(value)
        backend.simulate_sequence_bump()

    inserter = TextInserter(
        backend=backend,
        sleep_fn=sleep,
        clipboard_settle_s=0.05,
        restore_delay_s=1.5,
        schedule_fn=scheduler,
    )

    assert inserter.insert_text_with_options(
        "hello",
        target_hwnd=123,
        paste_mode="send_input",
    )

    # The target reads the clipboard after the call returned -- which is the
    # case the deferred restore exists for -- and only then does the restore
    # run.
    backend.consume_pending_paste()
    scheduler.fire_pending()

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
        return "wm_paste"

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

    The road is WM_PASTE, because that is where a post-keystroke verification
    still happens inside the call. After a SendInput paste the same read
    failure now falls to the deferred restore, which leaves the clipboard
    alone and logs `outcome=skipped_changed` -- nothing to report to a caller
    that was told, correctly, that the paste succeeded.
    """
    inserter = TextInserter(
        backend=_ClipboardBusyAfterPaste(), sleep_fn=lambda seconds: None
    )

    with pytest.raises(TextInsertionError) as excinfo:
        inserter.insert_text_with_options(
            "hello", target_hwnd=123, paste_mode="wm_paste"
        )

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

    That measurement was taken while this ran inside the transaction; the
    deferred restore has since moved it onto its own thread, which changes
    which thread the pinned core is taken from and nothing else. Unbounded is
    still unbounded.
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


def test_a_wm_paste_that_timed_out_is_not_reported_as_a_clean_failure(monkeypatch):
    """The target may be inside its paste handler, just slower than 250 ms.

    Measured against a real window whose WM_PASTE handler takes 1 s:
    `SendMessageTimeoutW` returned 0 with ERROR_TIMEOUT after 250 ms while the
    handler had already been entered, and it read the clipboard a second later.
    Reported as a clean `TextInsertionError`, that made the caller restore the
    previous clipboard -- so the user's own old clipboard content went into
    their document instead of the transcript, and the streaming path rolled the
    words back and pasted them again on the next partial.
    """
    backend = Win32ClipboardBackend()
    monkeypatch.setattr(
        backend,
        "_send_message_timeout_result",
        lambda hwnd, message, timeout_ms: (False, text_inserter.ERROR_TIMEOUT),
    )

    with pytest.raises(TextMayHaveBeenPastedError):
        backend._send_wm_paste(4321)

    with pytest.raises(TextMayHaveBeenPastedError):
        backend.send_paste_with_mode("wm_paste", target_hwnd=4321)


def test_a_wm_paste_to_a_dead_window_is_still_a_clean_failure(monkeypatch):
    """Only ERROR_TIMEOUT is ambiguous; a rejected handle delivered nothing."""
    backend = Win32ClipboardBackend()
    monkeypatch.setattr(
        backend,
        "_send_message_timeout_result",
        lambda hwnd, message, timeout_ms: (False, 1400),  # INVALID_WINDOW_HANDLE
    )

    assert backend._send_wm_paste(4321) is False

    with pytest.raises(TextInsertionError) as excinfo:
        backend.send_paste_with_mode("wm_paste", target_hwnd=4321)
    assert not isinstance(excinfo.value, TextMayHaveBeenPastedError)


def test_auto_mode_does_not_fall_through_after_a_wm_paste_timeout(monkeypatch):
    """`auto` reaches WM_PASTE last, so a timeout there ends the attempt."""
    backend = Win32ClipboardBackend()

    def _refused_ctrl_v():
        raise TextInsertionError("SendInput failed (sent 0 events).")

    monkeypatch.setattr(backend, "send_ctrl_v", _refused_ctrl_v)
    monkeypatch.setattr(
        backend,
        "_send_message_timeout_result",
        lambda hwnd, message, timeout_ms: (False, text_inserter.ERROR_TIMEOUT),
    )

    with pytest.raises(TextMayHaveBeenPastedError):
        backend.send_paste_with_mode("auto", target_hwnd=4321)


class _TimingOutPasteBackend(PasteBackend):
    """A target that started pasting but did not answer inside the budget."""

    def send_paste_with_mode(self, mode, target_hwnd=None):
        self.last_requested_mode = mode
        self.calls.append(f"paste:{target_hwnd}")
        raise TextMayHaveBeenPastedError(
            "WM_PASTE timed out; the target may still paste the transcript."
        )


def test_a_timed_out_paste_leaves_the_transcript_on_the_clipboard():
    """The end of the chain: restoring is what actually loses the transcript."""
    backend = _TimingOutPasteBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextMayHaveBeenPastedError):
        inserter.insert_text_with_options(
            "hello",
            target_hwnd=123,
            paste_mode="wm_paste",
        )

    assert "restore" not in backend.calls, (
        "the previous clipboard was restored under a paste that may still land"
    )


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
    """`restore_clipboard_state` empties the clipboard and writes the capture.

    So running it after a *failed* set damaged whatever was on the clipboard
    although the app had not touched it and had pasted nothing. The capture
    keeps every copyable format now, but it is still not lossless -- a
    GDI-handle format, a private format and an oversized clipboard all come
    back short -- so the gratuitous restore is still a loss.
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


def test_a_restore_that_fails_after_a_sendinput_paste_is_logged_and_not_raised(
    caplog,
):
    """Replaces `test_a_restore_that_fails_after_the_paste_is_never_retryable`.

    Its intent -- the text is in the document, so a failed clipboard cleanup
    must never make the caller retry -- now holds by construction: the restore
    happens after the call returned success, and there is no caller left to
    raise to. The failure has to be visible somewhere, so it is a WARNING.
    """
    backend = SequencedGatedBackend()
    backend.raise_on_restore = True
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    assert (
        inserter.insert_text_with_options(
            "hello", target_hwnd=123, paste_mode="send_input"
        )
        is True
    )

    with caplog.at_level(logging.WARNING, logger="stt_app.text_inserter"):
        scheduler.fire_pending()

    failures = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "clipboard_restore" in record.getMessage()
        and "outcome=failed" in record.getMessage()
    ]
    assert len(failures) == 1, (
        f"the failed restore was swallowed: {[r.getMessage() for r in caplog.records]}"
    )


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


@pytest.mark.parametrize("keep_transcript_in_clipboard", [False, True])
def test_an_emptied_clipboard_is_restored_even_when_the_transcript_should_stay(
    keep_transcript_in_clipboard,
):
    """`restore_clipboard=False` means "keep what we wrote", not "keep nothing".

    With "Keep transcript in clipboard" enabled the controller passes
    `restore_clipboard=False`, so `restore_previous_state` was already False
    before the set was even attempted. The `ClipboardEmptiedError` handler
    only marked the clipboard as ours, and the restore is guarded by both
    flags -- so in that one configuration the destructive half of the set had
    run, the write half had not, and nothing put the user's clipboard back.
    It was left empty: their content destroyed and no transcript in its place.
    """
    backend = _EmptiedThenFailedBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(ClipboardEmptiedError):
        inserter.insert_text_with_options(
            "transcript",
            target_hwnd=None,
            paste_mode="wm_paste",
            restore_clipboard=not keep_transcript_in_clipboard,
        )

    assert "restore" in backend.calls, (
        "the clipboard we emptied was never put back "
        f"(keep_transcript_in_clipboard={keep_transcript_in_clipboard}): "
        f"{backend.calls}"
    )
    assert backend.state["text"] == "old"


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


class _RecordingWin32Call:
    """A stand-in for one `user32` function.

    A plain lambda cannot hold the `argtypes`/`restype` attributes the backend
    assigns before calling, which is why this is a class.
    """

    def __init__(self, name, calls, result):
        self._name = name
        self._calls = calls
        self._result = result
        self.argtypes = None
        self.restype = None

    def __call__(self, *_args):
        self._calls.append(self._name)
        return self._result


class _FakeUser32:
    """The module's own `user32` handle, recording into one shared call list."""

    def __init__(self, calls, *, sequence=0, foreground=0):
        self.calls = calls
        self.GetClipboardSequenceNumber = _RecordingWin32Call(
            "sequence", calls, sequence
        )
        self.GetForegroundWindow = _RecordingWin32Call(
            "foreground", calls, foreground
        )


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
    # The successful write reads the clipboard's sequence counter on its way
    # out; keep that off the real `user32` like every other call here.
    backend._user32 = _FakeUser32(fake.calls, sequence=7)

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

    clipboard_calls = [call for call in fake.calls if call != "sequence"]
    assert fake.calls[0] == "open" and clipboard_calls[-1] == "close", (
        f"{label}: the clipboard was left open: {fake.calls}"
    )


class _SequencedFailAfterPasteBackend(PasteBackend):
    """The paste keystroke goes out, and the call after it fails."""

    def __init__(self):
        super().__init__(paste_mode="send_input")
        self.sequence = 100

    def set_clipboard_text(self, text):
        self.calls.append(f"set:{text}")
        self.state = {"has_text": True, "text": text}
        self.sequence += 1

    def get_clipboard_sequence_number(self):
        return self.sequence


def test_a_post_paste_reraise_keeps_the_refusal_to_touch_the_clipboard():
    """The re-raise builds a NEW exception, and a new one is permissive.

    `ClipboardContentionError` is the only refusal there is, and the handler
    *constructs* one when a non-contention failure after the paste keystroke
    finds the clipboard changed -- so the refusal reached the controller as
    permission, and `copy_on_error` would have written the transcript over the
    clipboard the user had just filled. AGENTS.md recorded this as unreachable
    "because no caller combines the two"; the two are combined here, inside
    `insert_text`.
    """
    backend = _SequencedFailAfterPasteBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    seen = []

    def _clipboard_changed(marker, text):
        # Clean before the paste, changed by the time the failure is handled.
        seen.append(1)
        return len(seen) > 1

    def _arming_blows_up(**_kwargs):
        raise OSError("the restore could not be armed after the paste went out")

    # Arming the deferred restore is what now runs after the keystroke, so it
    # is the raise site that reaches the classification. (Its own scheduling
    # failure is caught inside; this forces the surrounding path.)
    inserter._clipboard_changed_after_set = _clipboard_changed
    inserter._arm_deferred_restore = _arming_blows_up

    with pytest.raises(TextMayHaveBeenPastedError) as caught:
        inserter.insert_text("der transkribierte satz")

    assert isinstance(caught.value.__cause__, ClipboardContentionError)
    assert caught.value.__cause__.allow_clipboard_fallback is False
    assert caught.value.allow_clipboard_fallback is False


class _UnreadableClipboardBackend(PasteBackend):
    """The clipboard read fails the way `pywintypes` fails: a bare `Exception`."""

    class Win32Error(Exception):
        pass

    def capture_clipboard_state(self):
        self.calls.append("capture")
        raise self.Win32Error("(5, 'GetClipboardData', 'Access is denied.')")


def test_a_clipboard_read_that_fails_is_a_TextInsertionError():
    """It was the one backend call outside the guarded region.

    `pywintypes.error` derives straight from `Exception`, not `OSError`
    (measured), so a failed `GetClipboardData` -- a delayed-rendering format
    whose owner has exited, a clipboard manager or RDP redirection failing
    mid-read -- escaped past every `except TextInsertionError`. The streaming
    partial handler then never ran `rollback_commit`, so words it had already
    marked committed could never be offered again, and no error was shown.

    Nothing has been pasted at that point, so it must be the plain class and
    not `TextMayHaveBeenPastedError`: the streaming retry may safely try again.
    """
    backend = _UnreadableClipboardBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with pytest.raises(TextInsertionError) as caught:
        inserter.insert_text("der transkribierte satz")

    assert not isinstance(caught.value, TextMayHaveBeenPastedError), (
        "a pre-paste failure must not block the streaming retry"
    )
    assert isinstance(caught.value.__cause__, _UnreadableClipboardBackend.Win32Error)
    assert backend.calls == ["capture"], (
        f"it went on to touch the clipboard after the read failed: {backend.calls}"
    )


class _ForeignWriteAfterOurSetBackend(GatedPasteBackend):
    """A foreign writer lands after our `CloseClipboard`, before any later read.

    `set_clipboard_text` reports the counter it read itself (100). Every read
    afterwards answers 101 and the clipboard holds someone else's text, which
    is precisely the state a second, later read cannot distinguish from our
    own write.
    """

    def __init__(self):
        super().__init__(paste_mode="send_input")
        self.our_sequence = 100

    def set_clipboard_text(self, text):
        self.calls.append(f"set:{text}")
        return text_inserter.ClipboardMarker(sequence=self.our_sequence)

    def get_clipboard_sequence_number(self):
        return self.our_sequence + 1

    def get_clipboard_text(self):
        return "a colleague's chat message"


def test_the_marker_is_the_sequence_number_our_own_write_read(caplog):
    """The write reports its own number; the caller must not read it again.

    Reading the counter in a second call adopts the number of whatever landed
    in between, and the "did the clipboard change" check then compares that
    number against itself and answers "unchanged" -- so the app pastes the
    foreign content and restores over it.
    """
    backend = _ForeignWriteAfterOurSetBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    with (
        caplog.at_level(logging.INFO, logger="stt_app.text_inserter"),
        pytest.raises(ClipboardContentionError),
    ):
        inserter.insert_text_with_options(
            "hello", target_hwnd=123, paste_mode="send_input"
        )

    assert not any(call.startswith("paste") for call in backend.calls), (
        f"a stranger's clipboard content was pasted: {backend.calls}"
    )
    assert "restore" not in backend.calls
    assert scheduler.calls == []

    transaction = _one_log_line(caplog, "paste_transaction ")
    assert "marker=100" in transaction, (
        f"the foreign writer's sequence number was adopted as ours: {transaction}"
    )


class _MarkerlessForeignWriteBackend(GatedPasteBackend):
    """The same race seen by a backend that reports no marker at all.

    Both reads of the counter answer the same number -- the foreign write
    happened before the first of them -- so only comparing the content can
    tell that the clipboard is no longer ours.
    """

    def __init__(self):
        super().__init__(paste_mode="send_input")

    def get_clipboard_sequence_number(self):
        return 101

    def get_clipboard_text(self):
        return "a colleague's chat message"


def test_an_equal_sequence_number_is_not_proof_that_the_clipboard_is_ours():
    """The counter is a hint; the content is the evidence.

    An equal sequence number used to end the check with "unchanged" and no
    content comparison at all, which is what let a foreign write be pasted and
    then overwritten by the restore.
    """
    backend = _MarkerlessForeignWriteBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    with pytest.raises(ClipboardContentionError):
        inserter.insert_text_with_options(
            "hello", target_hwnd=123, paste_mode="send_input"
        )

    assert not any(call.startswith("paste") for call in backend.calls), (
        f"a stranger's clipboard content was pasted: {backend.calls}"
    )


def test_a_foreground_change_before_the_keystroke_aborts_the_paste():
    """Alt+Tab between the transaction start and the keystroke.

    The modifier-release wait alone is up to 1.5 s, and `SendInput` goes to
    whatever holds the focus when it runs -- `target_hwnd` is not consulted on
    that road at all. The transcript went into a stranger's window and the app
    reported success.
    """
    backend = ForegroundAwareBackend(foreground=4242)
    scheduler = RecordingScheduler()

    def sleep(_seconds):
        backend.foreground = 9999  # the user switched windows

    inserter = TextInserter(backend=backend, sleep_fn=sleep, schedule_fn=scheduler)

    with pytest.raises(TextInsertionError) as raised:
        inserter.insert_text_with_options(
            "hello", target_hwnd=123, paste_mode="send_input"
        )

    assert "foreground window changed" in str(raised.value)
    assert not isinstance(raised.value, TextMayHaveBeenPastedError), (
        "nothing was pasted, so the streaming retry must be allowed to try again"
    )
    assert raised.value.allow_clipboard_fallback is True
    assert not any(call.startswith("paste:") for call in backend.calls), (
        f"the keystroke went out anyway: {backend.calls}"
    )
    assert scheduler.calls == []
    assert backend.calls[-1] == "restore", (
        "a pre-keystroke failure restores the clipboard inside the call"
    )
    assert backend.state["text"] == "old"


def test_an_unchanged_foreground_still_pastes():
    """The other side of the cut: the same transaction with nobody switching."""
    backend = ForegroundAwareBackend(foreground=4242)
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    assert inserter.insert_text_with_options(
        "hello", target_hwnd=123, paste_mode="send_input"
    )

    assert "paste:123" in backend.calls
    assert backend.foreground_reads == [4242, 4242], (
        "the foreground is read once at the start and once before the keystroke"
    )


def test_the_wm_paste_road_does_not_re_read_the_foreground():
    """WM_PASTE addresses a window handle, so the focus is irrelevant to it."""
    backend = ForegroundAwareBackend(foreground=4242)
    backend.paste_mode = "wm_paste"

    def sleep(_seconds):
        backend.foreground = 9999

    inserter = TextInserter(backend=backend, sleep_fn=sleep)

    assert inserter.insert_text_with_options(
        "hello", target_hwnd=123, paste_mode="wm_paste"
    )

    assert "paste:123" in backend.calls


def test_a_deferred_restore_waits_for_a_busy_target_and_gives_up_at_the_deadline():
    """A target that has not answered yet has not read the clipboard yet.

    Restoring under it is what turns its late paste into the user's old
    content, so the record is rescheduled while there is budget left and then
    abandoned with the transcript still on the clipboard.
    """
    backend = SequencedGatedBackend(target_ready=False)
    scheduler = RecordingScheduler()
    clock = FakeClock()
    inserter = TextInserter(
        backend=backend,
        sleep_fn=lambda _s: None,
        restore_delay_s=1.5,
        restore_max_wait_s=10.0,
        schedule_fn=scheduler,
        clock=clock,
    )

    assert inserter.insert_text_with_options(
        "hello", target_hwnd=123, paste_mode="send_input"
    )
    assert scheduler.delays == [1.5]

    clock.advance(1.5)
    assert scheduler.fire_pending() == 1
    assert scheduler.delays == [1.5, 1.5], "a busy target was not waited for again"
    assert "restore" not in backend.calls

    clock.advance(10.0)  # past armed_at + restore_max_wait_s
    assert scheduler.fire_pending() == 1
    assert scheduler.delays == [1.5, 1.5], "the wait never ended"
    assert "restore" not in backend.calls
    assert backend.state["text"] == "hello", (
        "the transcript must stay on the clipboard for the target's late read"
    )


@pytest.mark.parametrize(
    ("label", "change_the_clipboard"),
    [
        (
            "the sequence moved and the content differs",
            lambda backend: backend.simulate_user_copy("something of mine"),
        ),
        (
            "the sequence is unchanged and only the content differs",
            lambda backend: backend.simulate_silent_overwrite("something of mine"),
        ),
    ],
)
def test_a_deferred_restore_is_skipped_when_the_clipboard_is_no_longer_ours(
    label, change_the_clipboard
):
    """1.5 s is long enough for the user to copy something of their own.

    Both detections matter: a sequence number that moved is the obvious one,
    and an unchanged one is the microsecond gap between our `CloseClipboard`
    and the marker read -- covered only by comparing the content.
    """
    backend = SequencedGatedBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    assert inserter.insert_text_with_options(
        "hello", target_hwnd=123, paste_mode="send_input"
    )

    change_the_clipboard(backend)
    scheduler.fire_pending()

    assert "restore" not in backend.calls, f"{label}: {backend.calls}"
    assert backend.state["text"] == "something of mine", f"{label}"


def test_a_second_paste_during_a_pending_restore_keeps_the_users_clipboard():
    """Capturing afresh would capture our own previous transcript.

    A streaming dictation pastes every ~350 ms, well inside the restore delay,
    so the second transaction's "previous clipboard" is the first
    transcript -- and restoring that at the end of the dictation puts a
    transcript on the user's clipboard instead of what they had copied.
    """
    backend = SequencedGatedBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    assert inserter.insert_text_with_options(
        "one", target_hwnd=123, paste_mode="send_input"
    )
    assert inserter.insert_text_with_options(
        "two", target_hwnd=123, paste_mode="send_input"
    )

    assert scheduler.calls[0].cancelled is True, "the first timer still fires"
    assert backend.calls.count("capture") == 1, (
        f"the second paste captured our own transcript: {backend.calls}"
    )

    assert scheduler.fire_pending() == 1
    assert backend.state["text"] == "old", (
        f"the user's clipboard was replaced by a transcript: {backend.state}"
    )


def test_a_second_paste_after_the_user_copied_in_between_captures_the_new_clipboard():
    """The mirror image: the pending record's state is no longer the user's.

    Once they have copied something themselves, that is what has to come back
    after the next dictation -- and the older record is dropped rather than
    restored over it.
    """
    backend = SequencedGatedBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    assert inserter.insert_text_with_options(
        "one", target_hwnd=123, paste_mode="send_input"
    )
    backend.simulate_user_copy("mine")
    assert inserter.insert_text_with_options(
        "two", target_hwnd=123, paste_mode="send_input"
    )

    assert backend.calls.count("capture") == 2
    assert scheduler.fire_pending() == 1
    assert backend.state["text"] == "mine"


def test_flush_pending_restore_restores_at_once_without_the_readiness_wait():
    """The app is quitting and the timer thread is a daemon.

    `target_ready` is False here on purpose: the readiness wait would
    reschedule instead of restoring, and the scheduled callback would then
    never run, so the user's clipboard would keep the transcript for good.
    """
    backend = SequencedGatedBackend(target_ready=False)
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    assert inserter.insert_text_with_options(
        "hello", target_hwnd=123, paste_mode="send_input"
    )
    inserter.flush_pending_restore()

    assert backend.state["text"] == "old"
    assert not any(call.startswith("wait_target") for call in backend.calls), (
        f"the flush waited for the target anyway: {backend.calls}"
    )
    assert scheduler.calls[0].cancelled is True
    # And it is idempotent: nothing is left to restore a second time.
    inserter.flush_pending_restore()
    assert backend.calls.count("restore") == 1


def test_flush_pending_restore_leaves_a_clipboard_that_is_no_longer_ours():
    """The content check is the one guard the flush does keep."""
    backend = SequencedGatedBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    assert inserter.insert_text_with_options(
        "hello", target_hwnd=123, paste_mode="send_input"
    )
    backend.simulate_user_copy("mine")
    inserter.flush_pending_restore()

    assert "restore" not in backend.calls
    assert backend.state["text"] == "mine"


def test_flush_pending_restore_is_harmless_with_nothing_pending():
    backend = SequencedGatedBackend()
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    inserter.flush_pending_restore()

    assert backend.calls == []


def _log_lines(caplog, prefix):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "stt_app.text_inserter"
        and record.getMessage().startswith(prefix)
    ]


def _one_log_line(caplog, prefix):
    lines = _log_lines(caplog, prefix)
    assert len(lines) == 1, f"expected exactly one {prefix!r} line, got {lines}"
    return lines[0]


def test_a_successful_transaction_and_its_restore_each_log_one_line(caplog):
    """One line per transaction and one per restore outcome.

    A paste that goes wrong in the field leaves nothing else behind: the
    clipboard has moved on, the target window is gone, and the transcript is
    only in history. And the transcript itself is never part of the line.
    """
    transcript = "der transkribierte satz"
    backend = SequencedGatedBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    with caplog.at_level(logging.INFO, logger="stt_app.text_inserter"):
        assert inserter.insert_text_with_options(
            transcript, target_hwnd=123, paste_mode="send_input"
        )
        scheduler.fire_pending()

    transaction = _one_log_line(caplog, "paste_transaction ")
    assert "mode=send_input/send_input" in transaction
    assert "target_hwnd=123" in transaction
    assert f"chars={len(transcript)}" in transaction
    assert "outcome=pasted" in transaction
    assert "restore=deferred" in transaction

    restore = _one_log_line(caplog, "clipboard_restore ")
    assert "outcome=restored" in restore
    assert "delay_ms=" in restore

    for line in _log_lines(caplog, ""):
        assert transcript not in line, f"the transcript was logged: {line}"
        assert "transkribierte" not in line, f"the transcript was logged: {line}"


def test_a_failed_transaction_logs_its_line_too(caplog):
    """Whatever the outcome is the point: a silent failure explains nothing."""
    backend = GatedPasteBackend()
    backend.raise_on_paste = True
    inserter = TextInserter(backend=backend, sleep_fn=lambda _s: None)

    with (
        caplog.at_level(logging.INFO, logger="stt_app.text_inserter"),
        pytest.raises(TextInsertionError),
    ):
        inserter.insert_text_with_options(
            "hello", target_hwnd=123, paste_mode="send_input"
        )

    transaction = _one_log_line(caplog, "paste_transaction ")
    assert "outcome=failed:TextInsertionError" in transaction
    assert "restore=immediate" in transaction
    assert _log_lines(caplog, "clipboard_restore ") == []


def test_a_superseded_restore_says_which_of_the_two_reasons_it_was(caplog):
    """`superseded` and `superseded_changed` are different events.

    The first carried the user's clipboard over into the new transaction; the
    second dropped it because the user had copied something themselves. A log
    that cannot tell them apart cannot explain a clipboard that came back
    wrong.
    """
    backend = SequencedGatedBackend()
    scheduler = RecordingScheduler()
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=scheduler
    )

    with caplog.at_level(logging.INFO, logger="stt_app.text_inserter"):
        inserter.insert_text_with_options(
            "one", target_hwnd=123, paste_mode="send_input"
        )
        inserter.insert_text_with_options(
            "two", target_hwnd=123, paste_mode="send_input"
        )
        backend.simulate_user_copy("mine")
        inserter.insert_text_with_options(
            "three", target_hwnd=123, paste_mode="send_input"
        )

    outcomes = [
        line.split("outcome=")[1].split(" ")[0]
        for line in _log_lines(caplog, "clipboard_restore ")
    ]
    assert outcomes == ["superseded", "superseded_changed"]


def test_the_backend_reads_the_sequence_number_after_closing_the_clipboard(
    monkeypatch,
):
    """The marker is read inside the write, and after the close.

    Before the close the counter has not necessarily moved yet -- whether it
    increments on `SetClipboardData` or on `CloseClipboard` is not documented
    and the design must not depend on it -- and after returning to the caller
    a foreign writer can already have moved it.
    """
    fake = _FakeWin32Clipboard()
    monkeypatch.setattr(text_inserter, "win32clipboard", fake)
    monkeypatch.setattr(text_inserter, "win32con", SimpleNamespace(CF_UNICODETEXT=13))
    backend = Win32ClipboardBackend()
    backend._user32 = _FakeUser32(fake.calls, sequence=4711)

    marker = backend.set_clipboard_text("transcript")

    assert fake.calls == ["open", "empty", "set", "close", "sequence"], (
        f"the counter was not read after CloseClipboard: {fake.calls}"
    )
    assert marker == text_inserter.ClipboardMarker(sequence=4711)


def test_the_backend_reads_the_foreground_window_through_its_own_user32():
    """And a NULL foreground means unknown, never window 0."""
    calls = []
    backend = Win32ClipboardBackend()
    backend._user32 = _FakeUser32(calls, foreground=0x4242)

    assert backend.get_foreground_window() == 0x4242
    assert calls == ["foreground"]

    backend._user32 = _FakeUser32(calls, foreground=0)
    assert backend.get_foreground_window() is None


def test_the_default_scheduler_really_runs_the_callback_on_a_daemon_thread():
    """Every other test injects a scheduler, so this is the only cover it has.

    A `_schedule_on_a_daemon_timer` that forgot to start its timer would leave
    every shipped restore pending forever and no other test would notice --
    the clipboard would keep the transcript after every single dictation.
    Daemon matters just as much: a non-daemon timer waiting out a hung target
    would hold the interpreter open at exit.
    """
    fired = threading.Event()
    handle = text_inserter._schedule_on_a_daemon_timer(0.0, fired.set)
    try:
        assert fired.wait(timeout=5.0), "the scheduled callback never ran"
        assert handle.daemon is True, "a pending restore would outlive the app"
    finally:
        handle.cancel()


def test_the_inserter_uses_that_scheduler_unless_one_is_injected():
    assert (
        TextInserter(backend=SequencedGatedBackend())._schedule_fn
        is text_inserter._schedule_on_a_daemon_timer
    )


# --- Clipboard capture and restore of every copyable format (F12) ----------

CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_DIB = 8
CF_PALETTE = 9
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_LOCALE = 16
CF_OWNERDISPLAY = 0x80
CF_DSPBITMAP = 0x82
CF_DSPMETAFILEPICT = 0x83
CF_DSPENHMETAFILE = 0x8E
# What `RegisterClipboardFormat` hands out; the names below are among the nine
# formats the user's own clipboard carried on 2026-09-16.
HTML_FORMAT = 0xC09F
DATA_OBJECT_FORMAT = 0xC004
OLE_PRIVATE_DATA_FORMAT = 0xC005


class _Win32Stub:
    """One faked Win32 entry point that can carry `argtypes`/`restype`.

    The backend declares both on the function object before every call -- a
    handle at or above 0x8000_0000 comes back negative from the default
    32-bit signed restype otherwise -- and neither a bound method nor a lambda
    can hold an attribute.
    """

    def __init__(self, fn):
        self._fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._fn(*args)


class _FakeClipboard:
    """A clipboard of `{format_id: bytes}` served through real ctypes memory.

    `GlobalLock` and `GlobalSize` answer with the address and the length of a
    real `ctypes.create_string_buffer`, so the production copy runs
    `ctypes.string_at` and `ctypes.memmove` over memory that exists. A stub
    handing back a Python object instead would hide a wrong length, a wrong
    pointer, or a missing lock -- the three ways this copy can be wrong.

    One object serves all three handles the backend uses: itself as the
    `win32clipboard` module, `.user32` for `GetClipboardData` and
    `SetClipboardData`, and `.kernel32` for the `Global*` family.
    """

    class Win32Error(Exception):
        """What pywin32 raises: `pywintypes.error` derives from `Exception`."""

    def __init__(self, contents=(), names=None, text=None):
        self.order = [int(format_id) for format_id, _payload in contents]
        self.payloads = {
            int(format_id): bytes(payload) for format_id, payload in contents
        }
        self.names = {int(key): value for key, value in (names or {}).items()}
        self.text = text
        self.calls = []
        # Written by the restore, in the order SetClipboardData was called.
        self.set_formats = []
        self.freed = []
        self.locked = []
        self.registered = {}
        # Failure knobs, each keyed by format id.
        self.null_handles = set()
        self.zero_sized = set()
        self.reported_sizes = {}
        self.lock_refused = set()
        self.set_failures = set()
        self._buffers = {}
        self._allocations = {}
        self.user32 = SimpleNamespace(
            GetClipboardData=_Win32Stub(self._get_clipboard_data_handle),
            SetClipboardData=_Win32Stub(self._set_clipboard_data),
        )
        self.kernel32 = SimpleNamespace(
            GlobalLock=_Win32Stub(self._global_lock),
            GlobalUnlock=_Win32Stub(self._global_unlock),
            GlobalSize=_Win32Stub(self._global_size),
            GlobalAlloc=_Win32Stub(self._global_alloc),
            GlobalFree=_Win32Stub(self._global_free),
        )

    # -- the win32clipboard module: the Win32 spelling is the backend's,
    # so these method names are not PEP 8 and must not be.

    def OpenClipboard(self):
        self.calls.append("open")

    def CloseClipboard(self):
        self.calls.append("close")

    def EmptyClipboard(self):
        self.calls.append("empty")
        self.order = []
        self.payloads = {}

    def IsClipboardFormatAvailable(self, format_id):
        return format_id in self.payloads

    def GetClipboardData(self, format_id):
        """Only ever asked for CF_UNICODETEXT, and only for the text."""
        assert format_id == CF_UNICODETEXT, (
            "the raw bytes must come through ctypes, not through pywin32: "
            f"GetClipboardData({format_id}) decodes what it reads"
        )
        return self.text

    def SetClipboardText(self, text, format_id):
        self.calls.append(f"set_text:{text}")
        self.set_formats.append((format_id, str(text).encode("utf-16-le")))

    def EnumClipboardFormats(self, previous):
        if previous == 0:
            return self.order[0] if self.order else 0
        index = self.order.index(previous)
        return self.order[index + 1] if index + 1 < len(self.order) else 0

    def GetClipboardFormatName(self, format_id):
        self.calls.append(f"name:{format_id}")
        if format_id not in self.names:
            # Measured against the installed pywin32: a standard or unknown id
            # raises error (87, 'GetClipboardFormatName', ...).
            raise self.Win32Error("(87, 'GetClipboardFormatName', ...)")
        return self.names[format_id]

    def RegisterClipboardFormat(self, name):
        self.calls.append(f"register:{name}")
        return self.registered.get(name, 0)

    # -- user32 -------------------------------------------------------------

    def _get_clipboard_data_handle(self, format_id):
        if format_id in self.null_handles:
            return 0
        return self._hold(format_id, self.payloads[format_id])

    def _set_clipboard_data(self, format_id, handle):
        allocated = self._allocations[handle]
        if format_id in self.set_failures:
            return 0
        self.set_formats.append((format_id, bytes(allocated.raw)))
        return handle

    # -- kernel32 -----------------------------------------------------------

    def _hold(self, format_id, payload):
        buffer = ctypes.create_string_buffer(payload, max(1, len(payload)))
        handle = ctypes.addressof(buffer)
        self._buffers[handle] = (format_id, buffer)
        return handle

    def _global_size(self, handle):
        format_id, buffer = self._buffers[handle]
        if format_id in self.zero_sized:
            return 0
        if format_id in self.reported_sizes:
            # A size the buffer does not have. Copying it would read past the
            # buffer, which is why _global_lock refuses that format outright.
            return self.reported_sizes[format_id]
        return len(buffer)

    def _global_lock(self, handle):
        if handle in self._allocations:
            self.locked.append(handle)
            return ctypes.addressof(self._allocations[handle])
        format_id, buffer = self._buffers[handle]
        if format_id in self.lock_refused:
            raise self.Win32Error("(5, 'GlobalLock', 'Access is denied.')")
        assert format_id not in self.reported_sizes, (
            f"format {format_id} was copied before its size was checked "
            "against the capture cap"
        )
        self.locked.append(handle)
        return ctypes.addressof(buffer)

    def _global_unlock(self, handle):
        assert handle in self.locked, "GlobalUnlock without a matching lock"
        self.locked.remove(handle)
        return 0

    def _global_alloc(self, _flags, size):
        buffer = ctypes.create_string_buffer(max(1, int(size)))
        handle = ctypes.addressof(buffer)
        self._allocations[handle] = buffer
        return handle

    def _global_free(self, handle):
        self.freed.append(handle)
        self._allocations.pop(handle, None)
        return 0


def _backend_on(clipboard, monkeypatch):
    """A real Win32ClipboardBackend wired to `clipboard` and nothing else."""
    monkeypatch.setattr(text_inserter, "win32clipboard", clipboard)
    monkeypatch.setattr(
        text_inserter, "win32con", SimpleNamespace(CF_UNICODETEXT=CF_UNICODETEXT)
    )
    backend = Win32ClipboardBackend()
    backend._user32 = clipboard.user32
    backend._kernel32 = clipboard.kernel32
    return backend


_GUTEN_TAG = "Guten Tag".encode("utf-16-le") + b"\x00\x00"


def test_capture_keeps_every_copyable_format_with_its_bytes_in_order(monkeypatch):
    """A dictation must give the clipboard back as it found it.

    Measured on the user's machine on 2026-09-16, a clipboard filled by one
    ordinary copy carried nine formats; the capture kept CF_UNICODETEXT alone,
    so the restore left plain text where a screenshot (CF_DIB), a file
    selection (CF_HDROP) or formatted text (HTML Format) had been.
    """
    clipboard = _FakeClipboard(
        contents=[
            (CF_UNICODETEXT, _GUTEN_TAG),
            (CF_TEXT, b"Guten Tag\x00"),
            (HTML_FORMAT, b"<b>Guten Tag</b>"),
            (CF_DIB, b"\x28\x00\x00\x00 the screenshot"),
            (CF_HDROP, b"\x14\x00\x00\x00C:\\report.pdf\x00\x00"),
            (CF_LOCALE, b"\x07\x04\x00\x00"),
        ],
        names={HTML_FORMAT: "HTML Format"},
        text="Guten Tag",
    )
    backend = _backend_on(clipboard, monkeypatch)

    state = backend.capture_clipboard_state()

    assert state.formats == (
        (CF_UNICODETEXT, "", _GUTEN_TAG),
        (CF_TEXT, "", b"Guten Tag\x00"),
        (HTML_FORMAT, "HTML Format", b"<b>Guten Tag</b>"),
        (CF_DIB, "", b"\x28\x00\x00\x00 the screenshot"),
        (CF_HDROP, "", b"\x14\x00\x00\x00C:\\report.pdf\x00\x00"),
        (CF_LOCALE, "", b"\x07\x04\x00\x00"),
    )
    assert clipboard.locked == [], "a clipboard format was left locked"
    assert clipboard.calls[0] == "open" and clipboard.calls[-1] == "close"


def test_the_text_only_readers_still_answer_for_a_multi_format_state(monkeypatch):
    """has_text and text keep their meaning; every existing reader uses them.

    TextInserter._clipboard_still_holds and the "keep transcript in clipboard"
    path read those two and nothing else.
    """
    clipboard = _FakeClipboard(
        contents=[(CF_UNICODETEXT, _GUTEN_TAG), (CF_DIB, b"picture")],
        text="Guten Tag",
    )
    backend = _backend_on(clipboard, monkeypatch)

    state = backend.capture_clipboard_state()

    assert state.has_text is True
    assert state.text == "Guten Tag"
    assert len(state.formats) == 2
    # And a state built the way every earlier caller built one still works.
    assert text_inserter.ClipboardState(has_text=False, text=None).formats == ()


def test_capture_skips_what_cannot_be_copied_as_bytes(monkeypatch):
    """GDI handles, the private ranges and the OLE bookkeeping formats.

    CF_BITMAP and the metafiles are GDI object handles rather than memory; the
    private and GDIOBJ ranges belong to the copying application, which frees
    them itself; DataObject and Ole Private Data describe an IDataObject that
    will not exist any more when the restore runs. Each of them would be a
    handle this process cannot hand on, so what is kept is the format carrying
    the same content as bytes -- CF_DIB beside CF_BITMAP, HTML Format beside
    DataObject.
    """
    clipboard = _FakeClipboard(
        contents=[
            (DATA_OBJECT_FORMAT, b"an IDataObject pointer"),
            (CF_BITMAP, b"a GDI bitmap handle"),
            (CF_METAFILEPICT, b"a metafile handle"),
            (CF_PALETTE, b"a palette handle"),
            (CF_ENHMETAFILE, b"an enhanced metafile handle"),
            (CF_OWNERDISPLAY, b"owner drawn"),
            (CF_DSPBITMAP, b"owner drawn bitmap"),
            (CF_DSPMETAFILEPICT, b"owner drawn metafile"),
            (CF_DSPENHMETAFILE, b"owner drawn enhanced metafile"),
            (0x0200, b"the first private format"),
            (0x02FF, b"the last private format"),
            (0x0300, b"the first GDIOBJ format"),
            (0x03FF, b"the last GDIOBJ format"),
            (OLE_PRIVATE_DATA_FORMAT, b"ole bookkeeping"),
            (CF_DIB, b"the screenshot"),
            (HTML_FORMAT, b"<b>bold</b>"),
        ],
        names={
            DATA_OBJECT_FORMAT: "DataObject",
            OLE_PRIVATE_DATA_FORMAT: "Ole Private Data",
            HTML_FORMAT: "HTML Format",
        },
    )
    backend = _backend_on(clipboard, monkeypatch)

    state = backend.capture_clipboard_state()

    assert state.formats == (
        (CF_DIB, "", b"the screenshot"),
        (HTML_FORMAT, "HTML Format", b"<b>bold</b>"),
    )


def test_restore_empties_the_clipboard_and_sets_every_format_in_order(monkeypatch):
    clipboard = _FakeClipboard(
        contents=[(CF_UNICODETEXT, _GUTEN_TAG), (HTML_FORMAT, b"<b>bold</b>")],
        names={HTML_FORMAT: "HTML Format"},
        text="Guten Tag",
    )
    backend = _backend_on(clipboard, monkeypatch)
    state = backend.capture_clipboard_state()
    clipboard.calls.clear()

    backend.restore_clipboard_state(state)

    assert clipboard.set_formats == [
        (CF_UNICODETEXT, _GUTEN_TAG),
        (HTML_FORMAT, b"<b>bold</b>"),
    ]
    assert clipboard.calls[0] == "open"
    assert "empty" in clipboard.calls
    assert clipboard.calls[-1] == "close"
    assert clipboard.locked == []
    assert clipboard.freed == [], (
        "a block SetClipboardData took ownership of was freed again"
    )


def test_a_format_that_cannot_be_set_is_logged_and_the_others_still_are(
    monkeypatch, caplog
):
    """One refused format must not cost the user the rest of their clipboard."""
    clipboard = _FakeClipboard(
        contents=[
            (CF_UNICODETEXT, _GUTEN_TAG),
            (CF_DIB, b"the screenshot"),
            (HTML_FORMAT, b"<b>bold</b>"),
        ],
        names={HTML_FORMAT: "HTML Format"},
        text="Guten Tag",
    )
    backend = _backend_on(clipboard, monkeypatch)
    state = backend.capture_clipboard_state()
    clipboard.set_failures = {CF_DIB}

    with caplog.at_level(logging.WARNING, logger="stt_app.text_inserter"):
        backend.restore_clipboard_state(state)

    assert [format_id for format_id, _payload in clipboard.set_formats] == [
        CF_UNICODETEXT,
        HTML_FORMAT,
    ]
    assert _one_log_line(caplog, "clipboard_restore_partial ") == (
        "clipboard_restore_partial failed=1"
    )
    assert len(clipboard.freed) == 1, (
        f"the refused block was leaked: freed={clipboard.freed}"
    )


def test_a_registered_format_whose_id_no_longer_names_it_is_re_registered(
    monkeypatch,
):
    """The captured id is used while it still names the same format.

    A registered atom lives for the session, so it normally does. Setting data
    under an id that has come to mean something else would hand a stranger's
    format our bytes, so the name is what decides.
    """
    clipboard = _FakeClipboard(
        contents=[(HTML_FORMAT, b"<b>bold</b>")],
        names={HTML_FORMAT: "HTML Format"},
    )
    backend = _backend_on(clipboard, monkeypatch)
    state = backend.capture_clipboard_state()
    # The atom now names something else, and HTML Format answers elsewhere.
    clipboard.names[HTML_FORMAT] = "Some Other Format"
    clipboard.registered["HTML Format"] = 0xC0FF

    backend.restore_clipboard_state(state)

    assert clipboard.set_formats == [(0xC0FF, b"<b>bold</b>")]


def test_an_unreadable_format_is_skipped_and_the_rest_captured(monkeypatch, caplog):
    """A NULL handle, a zero size and a refused lock are all "skip it".

    A delayed-rendering format whose owner has exited answers NULL, and a
    clipboard manager holding the block can refuse the lock. Losing that one
    format is the cost; failing the whole capture would fail the paste, which
    is the transcript the user is waiting for.
    """
    clipboard = _FakeClipboard(
        contents=[
            (CF_UNICODETEXT, _GUTEN_TAG),
            (0xC101, b"delayed rendering"),
            (0xC102, b"held by a clipboard manager"),
            (0xC103, b"an empty block"),
            (CF_DIB, b"the screenshot"),
        ],
        names={0xC101: "Delayed", 0xC102: "Held", 0xC103: "Empty"},
        text="Guten Tag",
    )
    clipboard.null_handles = {0xC101}
    clipboard.lock_refused = {0xC102}
    clipboard.zero_sized = {0xC103}
    backend = _backend_on(clipboard, monkeypatch)

    with caplog.at_level(logging.INFO, logger="stt_app.text_inserter"):
        state = backend.capture_clipboard_state()

    assert state.formats == (
        (CF_UNICODETEXT, "", _GUTEN_TAG),
        (CF_DIB, "", b"the screenshot"),
    )
    assert state.text == "Guten Tag"
    assert _one_log_line(caplog, "clipboard_capture_unreadable ") == (
        "clipboard_capture_unreadable formats=3"
    )
    assert clipboard.locked == []


def test_one_oversized_format_truncates_the_capture_to_text_only(monkeypatch, caplog):
    """The cap is read off GlobalSize before anything is copied.

    A 200 MiB format copied first and rejected afterwards would already have
    cost the 200 MiB and the memcpy for it, on the Qt main thread.
    """
    assert text_inserter.CLIPBOARD_CAPTURE_MAX_FORMAT_BYTES == 128 * 1024 * 1024
    clipboard = _FakeClipboard(
        contents=[
            (CF_UNICODETEXT, _GUTEN_TAG),
            (CF_DIB, b"a video frame"),
        ],
        text="Guten Tag",
    )
    # Reported, not allocated: _FakeClipboard._global_lock asserts that a
    # format with a reported size is never copied.
    clipboard.reported_sizes = {CF_DIB: 200 * 1024 * 1024}
    backend = _backend_on(clipboard, monkeypatch)

    with caplog.at_level(logging.WARNING, logger="stt_app.text_inserter"):
        state = backend.capture_clipboard_state()

    assert state.formats == ()
    assert state.has_text is True and state.text == "Guten Tag"
    assert _one_log_line(caplog, "clipboard_capture_truncated ") == (
        f"clipboard_capture_truncated formats=2 bytes={200 * 1024 * 1024 + 20}"
    )


def test_the_running_total_cap_truncates_the_capture_to_text_only(monkeypatch, caplog):
    clipboard = _FakeClipboard(
        contents=[
            (CF_UNICODETEXT, _GUTEN_TAG),
            (CF_DIB, b"first"),
            (CF_TEXT, b"second"),
        ],
        text="Guten Tag",
    )
    monkeypatch.setattr(text_inserter, "CLIPBOARD_CAPTURE_MAX_FORMAT_BYTES", 1024)
    monkeypatch.setattr(text_inserter, "CLIPBOARD_CAPTURE_MAX_TOTAL_BYTES", 24)
    backend = _backend_on(clipboard, monkeypatch)

    with caplog.at_level(logging.WARNING, logger="stt_app.text_inserter"):
        state = backend.capture_clipboard_state()

    # 20 bytes of CF_UNICODETEXT plus 5 is already past the 24-byte total.
    assert state.formats == ()
    assert state.text == "Guten Tag"
    assert _one_log_line(caplog, "clipboard_capture_truncated ") == (
        "clipboard_capture_truncated formats=2 bytes=25"
    )


def test_an_empty_clipboard_restores_an_empty_clipboard(monkeypatch):
    clipboard = _FakeClipboard()
    backend = _backend_on(clipboard, monkeypatch)

    state = backend.capture_clipboard_state()
    backend.restore_clipboard_state(state)

    assert state.has_text is False and state.formats == ()
    assert clipboard.set_formats == []
    assert clipboard.calls.count("empty") == 1


def test_the_text_is_put_back_even_when_its_format_could_not_be_captured(
    monkeypatch,
):
    """The new path must never restore less text than the old one did.

    CF_UNICODETEXT is readable through pywin32 and unreadable through its
    handle only in a narrow race, but the promise this class has always made
    is that the text comes back.
    """
    clipboard = _FakeClipboard(
        contents=[(CF_UNICODETEXT, _GUTEN_TAG), (CF_DIB, b"the screenshot")],
        text="Guten Tag",
    )
    clipboard.null_handles = {CF_UNICODETEXT}
    backend = _backend_on(clipboard, monkeypatch)

    state = backend.capture_clipboard_state()
    backend.restore_clipboard_state(state)

    assert state.has_text is True
    assert clipboard.set_formats == [
        (CF_DIB, b"the screenshot"),
        (CF_UNICODETEXT, "Guten Tag".encode("utf-16-le")),
    ]


def test_an_enumeration_that_repeats_an_id_ends_instead_of_looping(monkeypatch):
    """The capture runs on the Qt main thread, so its one loop needs an end.

    `EnumClipboardFormats` terminates by answering 0, and Windows does not
    repeat an id -- but a `while True` over an external call with no second
    way out is the shape that froze this app once before (the readiness probe
    spun 953,446 times against a hung target). The enumerator here answers
    0x0008, 0x000D, 0x0008, ... for ever; the fake gives up after 50 calls so
    a missing guard fails this test instead of hanging the suite.
    """
    clipboard = _FakeClipboard(
        contents=[(CF_DIB, b"the screenshot"), (CF_UNICODETEXT, _GUTEN_TAG)],
        text="Guten Tag",
    )
    cycle = iter([CF_DIB, CF_UNICODETEXT] * 25)

    def _never_ending(_previous):
        return next(cycle)

    clipboard.EnumClipboardFormats = _never_ending
    backend = _backend_on(clipboard, monkeypatch)

    state = backend.capture_clipboard_state()

    assert state.formats == (
        (CF_DIB, "", b"the screenshot"),
        (CF_UNICODETEXT, "", _GUTEN_TAG),
    )


def test_a_failed_enumeration_costs_the_formats_and_not_the_transcript(monkeypatch):
    """Capture is on the path of every paste, so it must not gain a way to fail.

    `_run_paste_transaction` turns anything raised out of
    `capture_clipboard_state` into a failed insertion, and before this unit
    there was no enumeration to fail. A clipboard this app cannot enumerate
    therefore falls back to what it always did -- the text alone -- instead of
    costing the user the dictation.
    """
    clipboard = _FakeClipboard(
        contents=[(CF_UNICODETEXT, _GUTEN_TAG), (CF_DIB, b"the screenshot")],
        text="Guten Tag",
    )

    def _refused(_previous):
        raise clipboard.Win32Error("(1418, 'EnumClipboardFormats', ...)")

    clipboard.EnumClipboardFormats = _refused
    backend = _backend_on(clipboard, monkeypatch)
    inserter = TextInserter(
        backend=backend, sleep_fn=lambda _s: None, schedule_fn=RecordingScheduler()
    )
    monkeypatch.setattr(backend, "send_paste_with_mode", lambda *_a, **_k: "wm_paste")
    monkeypatch.setattr(backend, "get_clipboard_text", lambda: "the transcript")
    monkeypatch.setattr(backend, "wait_for_modifier_release", lambda: True)

    state = backend.capture_clipboard_state()

    assert state.formats == ()
    assert state.has_text is True and state.text == "Guten Tag"
    assert inserter.insert_text("the transcript") is True
