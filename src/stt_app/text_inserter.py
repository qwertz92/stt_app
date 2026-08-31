from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
from dataclasses import dataclass
from typing import ClassVar

from .config import (
    CLIPBOARD_SETTLE_S,
    PASTE_MODIFIER_POLL_INTERVAL_S,
    PASTE_MODIFIER_RELEASE_TIMEOUT_S,
    PASTE_TARGET_RESPONSIVE_POLL_INTERVAL_S,
    PASTE_TARGET_RESPONSIVE_PROBE_MS,
    PASTE_TARGET_RESPONSIVE_TIMEOUT_S,
    SENDINPUT_RESTORE_DELAY_S,
    SENDINPUT_RETRY_ATTEMPTS,
    SENDINPUT_RETRY_SLEEP_S,
    WM_PASTE_TIMEOUT_MS,
)

try:
    import win32clipboard  # type: ignore
    import win32con  # type: ignore
except Exception:  # pragma: no cover - import guarded for testability
    win32clipboard = None
    win32con = None


class TextInsertionError(RuntimeError):
    def __init__(
        self,
        message: str = "",
        *,
        allow_clipboard_fallback: bool = True,
    ) -> None:
        super().__init__(message)
        self.allow_clipboard_fallback = allow_clipboard_fallback


class TextMayHaveBeenPastedError(TextInsertionError):
    """The paste keystroke was already delivered; only cleanup then failed.

    The caller must not retry on this. Streaming live insertion takes a failed
    insert as "those words are not in the document" and offers them again on
    the next partial -- correct when the paste never happened (a held modifier
    turns the injected Ctrl+V into Ctrl+Alt+V), but a duplicate paste when the
    text did land. Pasting the transcript twice is worse than stopping early.
    """


class ClipboardEmptiedError(TextInsertionError):
    """`EmptyClipboard` succeeded and writing the new content did not.

    Setting the clipboard is two Win32 calls, and only the *first* of them is
    destructive. Failing between them leaves the clipboard empty, so it is
    ours to put back -- unlike a failure to open it at all, where the
    clipboard still holds whatever it held and restoring would replace an
    image or a file selection with plain text this app never touched. One
    flag cannot express both, which is what this class is for.
    """


class ClipboardContentionError(TextInsertionError):
    def __init__(self, message: str) -> None:
        super().__init__(message, allow_clipboard_fallback=False)



class _ClipboardContentionAfterPaste(
    ClipboardContentionError, TextMayHaveBeenPastedError
):
    """Clipboard contention detected *after* the paste keystroke went out.

    Keeps the existing contention handling (no clipboard fallback, leave the
    user's clipboard alone) while telling the caller the text may already be in
    the document, so a retry would duplicate it.
    """

    def __init__(self, message: str) -> None:
        ClipboardContentionError.__init__(self, message)


@dataclass(slots=True)
class ClipboardState:
    has_text: bool
    text: str | None


_UNAVAILABLE_CLIPBOARD_TEXT = object()


class Win32ClipboardBackend:
    def __init__(self, retry_count: int = 10, retry_sleep_s: float = 0.01) -> None:
        self._retry_count = retry_count
        self._retry_sleep_s = retry_sleep_s
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

    def capture_clipboard_state(self) -> ClipboardState:
        with self._clipboard_opened():
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                return ClipboardState(has_text=True, text=str(text))
            return ClipboardState(has_text=False, text=None)

    def set_clipboard_text(self, text: str) -> None:
        with self._clipboard_opened():
            win32clipboard.EmptyClipboard()
            try:
                win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
            except Exception as exc:
                raise ClipboardEmptiedError(
                    f"The clipboard was emptied but could not be written: {exc}"
                ) from exc

    def restore_clipboard_state(self, state: ClipboardState) -> None:
        with self._clipboard_opened():
            win32clipboard.EmptyClipboard()
            if state.has_text and state.text is not None:
                win32clipboard.SetClipboardText(state.text, win32con.CF_UNICODETEXT)

    def get_clipboard_sequence_number(self) -> int | None:
        getter = getattr(self._user32, "GetClipboardSequenceNumber", None)
        if getter is None:
            return None
        getter.restype = ctypes.wintypes.DWORD
        return int(getter() or 0)

    def get_clipboard_text(self) -> str | None:
        with self._clipboard_opened():
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
            return None

    def send_ctrl_v(self) -> None:
        _send_ctrl_v_input()

    def wait_for_modifier_release(
        self,
        timeout_s: float = PASTE_MODIFIER_RELEASE_TIMEOUT_S,
        poll_interval_s: float = PASTE_MODIFIER_POLL_INTERVAL_S,
    ) -> bool:
        """Wait until no physical modifier key is held down.

        Inserts are often triggered straight from a WM_HOTKEY press, so the
        user's Ctrl/Alt/Shift/Win keys can still be down when Ctrl+V is
        injected; the target would then receive e.g. Ctrl+Alt+V (AltGr+V on
        German layouts), which is not a paste. Returns False when a modifier
        is still held after the timeout; the caller then aborts the paste.
        """
        get_state = getattr(self._user32, "GetAsyncKeyState", None)
        if get_state is None:
            return True
        get_state.argtypes = (ctypes.c_int,)
        get_state.restype = ctypes.c_short
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            if not any(
                int(get_state(vk)) & 0x8000 for vk in _MODIFIER_VIRTUAL_KEYS
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.001, poll_interval_s))

    def wait_for_paste_target_ready(
        self,
        target_hwnd: int | None = None,
        timeout_s: float = PASTE_TARGET_RESPONSIVE_TIMEOUT_S,
        probe_timeout_ms: int = PASTE_TARGET_RESPONSIVE_PROBE_MS,
        poll_interval_s: float = PASTE_TARGET_RESPONSIVE_POLL_INTERVAL_S,
    ) -> bool:
        """Wait until the paste target's thread answers WM_NULL again.

        A busy target has not processed the injected Ctrl+V yet; restoring the
        previous clipboard on a fixed delay would make its late clipboard read
        paste the old content instead of the transcript. Returns False when
        the target stays unresponsive past the budget.

        This runs on the Qt main thread, so both bounds below matter: the
        sleep, because `SMTO_ABORTIFHUNG` returns instantly for a hung target
        and the probe throttles nothing then, and the window check, because a
        handle that names no window can never answer.
        """
        hwnd = int(target_hwnd or self._get_focused_hwnd() or 0)
        if hwnd == 0 or not self._is_window(hwnd):
            # The probe target is the caret *child* control, and an application
            # may have recreated it (a closed editor tab) while the top-level
            # window that actually received the injected Ctrl+V is alive. There
            # is nothing to wait for, and spending the whole budget on it would
            # report an unresponsive target for an application that is not.
            return True
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            if self._send_message_timeout(hwnd, WM_NULL, int(probe_timeout_ms)):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.001, poll_interval_s))

    def _is_window(self, hwnd: int) -> bool:
        is_window = getattr(self._user32, "IsWindow", None)
        if is_window is None:
            return True
        is_window.argtypes = (ctypes.wintypes.HWND,)
        is_window.restype = ctypes.wintypes.BOOL
        return bool(is_window(hwnd))

    def send_paste(self, target_hwnd: int | None = None) -> str:
        return self.send_paste_with_mode("auto", target_hwnd=target_hwnd)

    def send_paste_with_mode(self, mode: str, target_hwnd: int | None = None) -> str:
        normalized = (mode or "auto").strip().lower()
        if normalized == "wm_paste":
            if self._send_wm_paste(target_hwnd):
                return "wm_paste"
            raise TextInsertionError("WM_PASTE failed for target window.")

        if normalized == "send_input":
            self.send_ctrl_v()
            return "send_input"

        send_input_error: Exception | None = None
        try:
            self.send_ctrl_v()
            return "send_input"
        except TextMayHaveBeenPastedError:
            # A partial `SendInput` already delivered Ctrl-down and V-down, so
            # the target pasted on the key-down. Falling through to WM_PASTE
            # would paste the transcript a second time inside this one call --
            # and `auto` is the default mode, so that was the shipped path.
            raise
        except Exception as exc:
            send_input_error = exc

        if self._send_wm_paste(target_hwnd):
            return "wm_paste"

        raise TextInsertionError(
            f"Auto paste failed: SendInput error: {send_input_error}; WM_PASTE failed."
        )

    class _ClipboardContext:
        def __init__(self, backend: Win32ClipboardBackend) -> None:
            self._backend = backend

        def __enter__(self):
            if win32clipboard is None or win32con is None:
                raise TextInsertionError(
                    "pywin32 is required for clipboard insertion on Windows."
                )

            for _ in range(self._backend._retry_count):
                try:
                    win32clipboard.OpenClipboard()
                    return self
                except Exception:
                    time.sleep(self._backend._retry_sleep_s)

            raise TextInsertionError("Failed to open clipboard.")

        def __exit__(self, exc_type, exc, tb):
            win32clipboard.CloseClipboard()
            return False

    def _clipboard_opened(self) -> Win32ClipboardBackend._ClipboardContext:
        return self._ClipboardContext(self)

    def _send_wm_paste(self, target_hwnd: int | None = None) -> bool:
        hwnd = int(target_hwnd or self._get_focused_hwnd() or 0)
        if hwnd == 0:
            return False
        sent, last_error = self._send_message_timeout_result(
            hwnd, WM_PASTE, WM_PASTE_TIMEOUT_MS
        )
        if sent:
            return True
        if last_error == ERROR_TIMEOUT:
            # A timeout is NOT a clean failure: the target may be inside its
            # paste handler right now, just slower than our 250 ms budget.
            # Measured against a real window whose WM_PASTE handler takes 1 s:
            # `SendMessageTimeoutW` returned 0 with ERROR_TIMEOUT after 250 ms
            # while the handler had already been entered, and it read the
            # clipboard a second later. Reported as a clean failure, that made
            # the caller restore the previous clipboard -- so the user's own
            # old clipboard content was pasted into their document instead of
            # the transcript, and the streaming path additionally rolled the
            # words back and pasted them again on the next partial.
            # `TextMayHaveBeenPastedError` is the existing class for exactly
            # this: the caller keeps the transcript on the clipboard and does
            # not retry. (A target that never entered the handler yields the
            # identical return value and error, so the two cannot be told
            # apart; the same measurement showed the message is then simply
            # discarded, which only costs a paste that did not happen.)
            raise TextMayHaveBeenPastedError(
                "WM_PASTE timed out; the target may still paste the transcript."
            )
        return False

    def _send_message_timeout(self, hwnd: int, message: int, timeout_ms: int) -> bool:
        sent, _last_error = self._send_message_timeout_result(
            hwnd, message, timeout_ms
        )
        return sent

    def _send_message_timeout_result(
        self,
        hwnd: int,
        message: int,
        timeout_ms: int,
    ) -> tuple[bool, int]:
        send_message_timeout = self._user32.SendMessageTimeoutW
        send_message_timeout.argtypes = (
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
            ctypes.wintypes.UINT,
            ctypes.wintypes.UINT,
            ctypes.POINTER(ULONG_PTR),
        )
        send_message_timeout.restype = ctypes.wintypes.LPARAM

        result = ULONG_PTR(0)
        ctypes.set_last_error(0)
        ok = send_message_timeout(
            hwnd,
            message,
            0,
            0,
            SMTO_ABORTIFHUNG,
            timeout_ms,
            ctypes.byref(result),
        )
        return bool(ok), 0 if ok else ctypes.get_last_error()

    def _get_focused_hwnd(self) -> int | None:
        foreground = int(self._user32.GetForegroundWindow() or 0)
        if foreground == 0:
            return None

        thread_id = int(self._user32.GetWindowThreadProcessId(foreground, None) or 0)
        if thread_id == 0:
            return foreground

        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        ok = bool(self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)))
        if not ok:
            return foreground
        focus = int(info.hwndFocus or 0)
        return focus or foreground


class TextInserter:
    def __init__(
        self,
        backend: Win32ClipboardBackend | None = None,
        sleep_fn=time.sleep,
        clipboard_settle_s: float = CLIPBOARD_SETTLE_S,
        sendinput_restore_delay_s: float = SENDINPUT_RESTORE_DELAY_S,
    ) -> None:
        self._backend = backend or Win32ClipboardBackend()
        self._sleep_fn = sleep_fn
        self._clipboard_settle_s = clipboard_settle_s
        self._sendinput_restore_delay_s = sendinput_restore_delay_s
        self._insert_lock = threading.RLock()

    def insert_text(self, text: str, target_hwnd: int | None = None) -> bool:
        return self.insert_text_with_options(
            text=text,
            target_hwnd=target_hwnd,
            paste_mode="auto",
        )

    def _paste_text_with_options(
        self,
        text: str,
        *,
        target_hwnd: int | None,
        paste_mode: str,
        restore_clipboard: bool = True,
    ) -> bool:
        with self._insert_lock:
            requested_mode = (paste_mode or "auto").strip().lower()
            if requested_mode != "wm_paste":
                # A held hotkey modifier would turn the injected Ctrl+V into
                # e.g. Ctrl+Alt+V for the target; wait for release first.
                self._wait_for_modifier_release()
            # The one backend call outside the guarded region below, and its
            # `IsClipboardFormatAvailable` / `GetClipboardData` are unwrapped.
            # `pywintypes.error` derives straight from `Exception`, not
            # `OSError`, so a failed read -- a delayed-rendering format whose
            # owner has exited, a clipboard manager or RDP redirection failing
            # mid-read -- escaped as something no caller catches. Measured
            # through `insert_text_with_options`: it came out as a bare
            # `error`, past every `except TextInsertionError`, so the streaming
            # partial handler never ran `rollback_commit` and those words could
            # never be offered again, while the overlay showed no error at all.
            #
            # Nothing has been pasted at this point, so it is a plain
            # `TextInsertionError` and the streaming retry may safely try again.
            try:
                previous_state = self._backend.capture_clipboard_state()
            except TextInsertionError:
                raise
            except Exception as exc:
                raise TextInsertionError(
                    f"The current clipboard contents could not be read: {exc}"
                ) from exc
            clipboard_marker: int | None = None
            restore_previous_state = restore_clipboard
            actual_mode = "send_input"
            paste_error: Exception | None = None
            restore_error: Exception | None = None
            # Everything raised after this becomes True may already be in
            # the target document. One flag rather than one exception class
            # per raise site: the previous approach missed the most likely
            # site of all (a clipboard read that fails because another
            # program has the clipboard open), and the streaming retry then
            # pasted the same words a second time.
            paste_sent = False
            # Whether the clipboard is ours to put back. `restore_clipboard_state`
            # empties the clipboard and returns only `CF_UNICODETEXT`, so running
            # it after a *failed* set destroyed an image or a file selection this
            # app had never touched.
            clipboard_was_set = False
            try:
                try:
                    self._backend.set_clipboard_text(text)
                except ClipboardEmptiedError:
                    # Destructive half done, write half not: the clipboard is
                    # empty and putting it back is the only way the user gets
                    # their content again.
                    clipboard_was_set = True
                    # And that holds even when the caller asked us NOT to
                    # restore. `restore_clipboard=False` comes from "Keep
                    # transcript in clipboard", i.e. "leave what we wrote in
                    # place" -- and we wrote nothing. Without this line that
                    # one setting turned the case this class exists for into
                    # the loss it exists to prevent: the clipboard left empty,
                    # the user's content gone and no transcript in its place.
                    restore_previous_state = True
                    raise
                clipboard_was_set = True
                clipboard_marker = self._clipboard_sequence_number()
                self._sleep_fn(self._clipboard_settle_s)

                if self._clipboard_changed_after_set(clipboard_marker, text):
                    restore_previous_state = False
                    raise ClipboardContentionError(
                        "Clipboard changed before paste; left the current "
                        "clipboard untouched."
                    )

                if hasattr(self._backend, "send_paste_with_mode"):
                    actual_mode = self._backend.send_paste_with_mode(
                        requested_mode,
                        target_hwnd=target_hwnd,
                    )
                elif hasattr(self._backend, "send_paste"):
                    actual_mode = self._backend.send_paste(target_hwnd=target_hwnd)
                else:
                    self._backend.send_ctrl_v()
                    actual_mode = "send_input"
                paste_sent = True

                if actual_mode == "send_input":
                    if self._wait_for_paste_target_ready(target_hwnd):
                        # Give target app enough time to read clipboard
                        # before restore.
                        self._sleep_fn(self._sendinput_restore_delay_s)
                    else:
                        # The target never became responsive, so its Ctrl+V is
                        # still queued. Keep the transcript on the clipboard;
                        # restoring now would make the late paste insert the
                        # previous clipboard content instead.
                        restore_previous_state = False

                if self._clipboard_changed_after_set(clipboard_marker, text):
                    restore_previous_state = False
                    # Past the paste keystroke, so the target may already have
                    # read the transcript. Not retryable.
                    raise _ClipboardContentionAfterPaste(
                        "Clipboard changed during paste; left the current "
                        "clipboard untouched."
                    )
            except Exception as exc:
                paste_error = exc
                if isinstance(
                    exc, (ClipboardContentionError, TextMayHaveBeenPastedError)
                ):
                    # Contention: leave the user's clipboard alone. A paste that
                    # may already be in flight: restoring now would make its late
                    # read take the previous content instead of the transcript,
                    # which is the same trade the unresponsive-target branch
                    # above makes.
                    restore_previous_state = False
                elif clipboard_marker is not None:
                    try:
                        if self._clipboard_changed_after_set(clipboard_marker, text):
                            restore_previous_state = False
                            paste_error = ClipboardContentionError(
                                "Paste failed after the clipboard changed; left "
                                "the current clipboard untouched."
                            )
                    except ClipboardContentionError as contention:
                        restore_previous_state = False
                        paste_error = contention
            finally:
                if restore_previous_state and clipboard_was_set:
                    try:
                        self._backend.restore_clipboard_state(previous_state)
                    except Exception as exc:
                        restore_error = exc

            combined_error = (
                TextMayHaveBeenPastedError if paste_sent else TextInsertionError
            )
            # Every re-raise below builds a NEW exception, and a new one starts
            # with the permissive default. `ClipboardContentionError` is the
            # only refusal there is, and the handler above *constructs* one --
            # so a non-contention failure after the paste keystroke that then
            # found the clipboard changed arrived at the controller saying the
            # clipboard may be overwritten, which is the one thing that error
            # exists to forbid. Measured through `insert_text`: cause
            # `ClipboardContentionError(allow_clipboard_fallback=False)`,
            # re-raise `TextMayHaveBeenPastedError(allow_clipboard_fallback=
            # True)`. AGENTS.md recorded this as unreachable "because no caller
            # combines the two"; the two are combined inside this function.
            fallback_allowed = getattr(
                paste_error, "allow_clipboard_fallback", True
            )
            # The combined branch below cannot currently observe a False flag,
            # and mutation testing cannot tell it apart: every path that
            # produces one -- the pre-paste check, the post-paste check, and
            # the handler's own construction -- sets `restore_previous_state =
            # False` first, so `restore_error` stays None. Passed anyway, so a
            # later False-flag path that leaves the restore enabled does not
            # have to rediscover this.
            if paste_error is not None and restore_error is not None:
                raise combined_error(
                    f"Failed to paste text ({paste_error}) and failed to restore clipboard ({restore_error}).",
                    allow_clipboard_fallback=fallback_allowed,
                ) from paste_error
            if paste_error is not None:
                if isinstance(paste_error, TextInsertionError) and (
                    not paste_sent
                    or isinstance(paste_error, TextMayHaveBeenPastedError)
                ):
                    raise paste_error
                if paste_sent:
                    # Re-raise under the after-paste class so a caller that
                    # retries cannot duplicate text that already landed.
                    raise TextMayHaveBeenPastedError(
                        str(paste_error)
                        or f"Failed to insert transcribed text: {paste_error}",
                        allow_clipboard_fallback=fallback_allowed,
                    ) from paste_error
                raise TextInsertionError(
                    f"Failed to insert transcribed text: {paste_error}",
                    allow_clipboard_fallback=fallback_allowed,
                ) from paste_error
            if restore_error is not None:
                raise TextMayHaveBeenPastedError(
                    f"Text pasted but clipboard restore failed: {restore_error}"
                ) from restore_error

            return True

    def _wait_for_modifier_release(self) -> None:
        waiter = getattr(self._backend, "wait_for_modifier_release", None)
        if not callable(waiter):
            return
        try:
            released = waiter()
            if released is False:
                raise TextInsertionError(
                    "Paste canceled because a Ctrl, Alt, Shift, or Windows key "
                    "remained held. Release the keys and retry."
                )
        except TextInsertionError:
            raise
        except Exception:
            # Never let modifier probing break the paste itself.
            pass

    def _wait_for_paste_target_ready(self, target_hwnd: int | None) -> bool:
        checker = getattr(self._backend, "wait_for_paste_target_ready", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(target_hwnd))
        except Exception:
            return True

    def _clipboard_sequence_number(self) -> int | None:
        getter = getattr(self._backend, "get_clipboard_sequence_number", None)
        if not callable(getter):
            return None
        # `int()` inside the guard, not after it. Every caller treats this as
        # "returns a number or None, never raises", and
        # `_clipboard_changed_after_set` is called from inside the paste
        # block's own `except` arm -- where the only handler is
        # `except ClipboardContentionError`, so anything else escapes the
        # classification entirely and leaves a Qt slot with a raw exception
        # after the paste keystroke has already gone out.
        try:
            sequence = getter()
            if sequence is None:
                return None
            return int(sequence)
        except Exception:
            return None

    def _clipboard_text(self):
        getter = getattr(self._backend, "get_clipboard_text", None)
        if not callable(getter):
            return _UNAVAILABLE_CLIPBOARD_TEXT
        try:
            return getter()
        except Exception as exc:
            raise ClipboardContentionError(
                "Clipboard could not be verified; left the current clipboard "
                "untouched."
            ) from exc

    def _clipboard_changed_after_set(
        self,
        marker: int | None,
        expected_text: str,
    ) -> bool:
        current_marker = self._clipboard_sequence_number()
        if marker is not None and current_marker is not None:
            if current_marker == marker:
                return False
            current_text = self._clipboard_text()
            if current_text is not _UNAVAILABLE_CLIPBOARD_TEXT:
                return current_text != expected_text
            return True
        current_text = self._clipboard_text()
        if current_text is not _UNAVAILABLE_CLIPBOARD_TEXT:
            return current_text != expected_text
        return False

    def insert_text_with_options(
        self,
        text: str,
        target_hwnd: int | None = None,
        paste_mode: str = "auto",
        restore_clipboard: bool = True,
    ) -> bool:
        if not text or not text.strip():
            return False
        return self._paste_text_with_options(
            text,
            target_hwnd=target_hwnd,
            paste_mode=paste_mode,
            restore_clipboard=restore_clipboard,
        )

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56
# Physical modifiers that corrupt an injected Ctrl+V when still held down.
_MODIFIER_VIRTUAL_KEYS = (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)
WIN_WORD = ctypes.c_uint16
WIN_DWORD = ctypes.c_uint32
WIN_LONG = ctypes.c_int32
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32
WM_NULL = 0x0000
WM_PASTE = 0x0302
# `SendMessageTimeoutW` sets this when it gave up waiting. It does NOT mean
# the target ignored the message -- see `_send_wm_paste`.
ERROR_TIMEOUT = 1460
SMTO_ABORTIFHUNG = 0x0002


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("hwndActive", ctypes.wintypes.HWND),
        ("hwndFocus", ctypes.wintypes.HWND),
        ("hwndCapture", ctypes.wintypes.HWND),
        ("hwndMenuOwner", ctypes.wintypes.HWND),
        ("hwndMoveSize", ctypes.wintypes.HWND),
        ("hwndCaret", ctypes.wintypes.HWND),
        ("rcCaret", ctypes.wintypes.RECT),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WIN_WORD),
        ("wScan", WIN_WORD),
        ("dwFlags", WIN_DWORD),
        ("time", WIN_DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", WIN_LONG),
        ("dy", WIN_LONG),
        ("mouseData", WIN_DWORD),
        ("dwFlags", WIN_DWORD),
        ("time", WIN_DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", WIN_DWORD),
        ("wParamL", WIN_WORD),
        ("wParamH", WIN_WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # ctypes' metaclass reads this plain class-level list from the class dict.
    _fields_: ClassVar = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", WIN_DWORD),
        ("union", _INPUTUNION),
    ]


def _keyboard_input(vk: int, keyup: bool = False) -> INPUT:
    flags = KEYEVENTF_KEYUP if keyup else 0
    return INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(ki=KEYBDINPUT(vk, 0, flags, 0, 0)))


def _send_input_batch(
    events: list[INPUT],
    *,
    cleanup_events: list[INPUT] | None = None,
    committed_after: int | None = None,
) -> None:
    """Send one input batch, retrying only while nothing at all was delivered.

    `committed_after` is how many delivered events mean the batch has already
    had its effect on the target. Past that count the failure is reported as
    `TextMayHaveBeenPastedError`, because a caller that retries it -- or falls
    back to another paste mechanism -- duplicates what already landed.
    """
    if not events:
        return

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    send_input = user32.SendInput
    send_input.argtypes = (
        ctypes.wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
    )
    send_input.restype = ctypes.wintypes.UINT
    inputs = (INPUT * len(events))(*events)
    expected = len(inputs)

    last_error = 0
    last_sent = 0
    for _ in range(SENDINPUT_RETRY_ATTEMPTS):
        ctypes.set_last_error(0)
        sent = send_input(
            expected,
            ctypes.cast(inputs, ctypes.POINTER(INPUT)),
            ctypes.sizeof(INPUT),
        )
        last_sent = int(sent)
        if last_sent == expected:
            return
        last_error = int(ctypes.get_last_error() or 0)
        if last_sent > 0:
            # Some key-down events may already have reached the target. Replaying
            # the full batch can duplicate input and leave keys logically held.
            # Send key-up cleanup once, then report the indeterminate paste.
            if cleanup_events:
                cleanup_inputs = (INPUT * len(cleanup_events))(*cleanup_events)
                try:
                    send_input(
                        len(cleanup_inputs),
                        ctypes.cast(cleanup_inputs, ctypes.POINTER(INPUT)),
                        ctypes.sizeof(INPUT),
                    )
                except Exception:
                    pass
            break
        time.sleep(SENDINPUT_RETRY_SLEEP_S)

    detail = _format_sendinput_failure(last_sent, expected, last_error)
    if committed_after is not None and last_sent >= committed_after:
        raise TextMayHaveBeenPastedError(detail)
    raise TextInsertionError(detail)


def _modified_key_inputs(modifier_vk: int, key_vk: int) -> list[INPUT]:
    return [
        _keyboard_input(modifier_vk, keyup=False),
        _keyboard_input(key_vk, keyup=False),
        _keyboard_input(key_vk, keyup=True),
        _keyboard_input(modifier_vk, keyup=True),
    ]


def _send_ctrl_v_input() -> None:
    _send_input_batch(
        _modified_key_inputs(VK_CONTROL, VK_V),
        cleanup_events=[
            _keyboard_input(VK_V, keyup=True),
            _keyboard_input(VK_CONTROL, keyup=True),
        ],
        # The batch is `[Ctrl-down, V-down, V-up, Ctrl-up]` and applications
        # paste on the key-down, so two delivered events are already a paste.
        # One is Ctrl-down alone, which is not, and stays retryable.
        committed_after=2,
    )


def _format_sendinput_failure(sent: int, expected: int, error_code: int) -> str:
    if error_code == 5:
        return (
            "SendInput failed (Access denied / UIPI). "
            "Run this app with the same privileges as the target window."
        )
    if error_code != 0:
        return (
            f"SendInput failed (sent {sent}/{expected}, WinError {error_code}). "
            "Ensure the target window is focused and accepts keyboard input."
        )
    if sent == 0:
        return (
            "SendInput failed (sent 0 events). "
            "Target window may be elevated, secure, or blocking synthetic input."
        )
    return (
        f"SendInput partially failed (sent {sent}/{expected}). "
        "The text may already have been pasted; check the target window."
    )
