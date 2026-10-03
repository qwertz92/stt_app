from __future__ import annotations

import ctypes
import ctypes.wintypes
import itertools
import logging
import threading
import time
from dataclasses import dataclass
from typing import ClassVar

from .config import (
    CLIPBOARD_CAPTURE_MAX_FORMAT_BYTES,
    CLIPBOARD_CAPTURE_MAX_TOTAL_BYTES,
    CLIPBOARD_RESTORE_DELAY_S,
    CLIPBOARD_RESTORE_MAX_WAIT_S,
    CLIPBOARD_RESTORE_RETRY_ATTEMPTS,
    CLIPBOARD_RESTORE_RETRY_DELAY_S,
    CLIPBOARD_SETTLE_S,
    PASTE_MODIFIER_POLL_INTERVAL_S,
    PASTE_MODIFIER_RELEASE_TIMEOUT_S,
    PASTE_TARGET_RESPONSIVE_POLL_INTERVAL_S,
    PASTE_TARGET_RESPONSIVE_PROBE_MS,
    PASTE_TARGET_RESPONSIVE_TIMEOUT_S,
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

_LOGGER = logging.getLogger(__name__)

# One id per paste transaction, so the `paste_transaction` line and the
# `clipboard_restore` line(s) that belong to it can be read together in a log
# where several dictations interleave. Process-wide and never reset: a
# duplicate id in one log file would defeat the only purpose it has.
_PASTE_TRANSACTION_IDS = itertools.count(1)


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


# `GetLastError` 1418, "Thread does not have a clipboard open". pywin32's
# `OpenClipboard()` opens with a NULL owner window, and a clipboard opened that
# way can be closed by another program's `CloseClipboard` -- clipboard
# managers do exactly that -- so the next call we make inside our own open
# fails with this code. It is transient the way contention is: the next open
# works. Opening with a real owner window would close the race itself, and is
# deliberately not done here (see `Win32ClipboardBackend._with_reopen`).
ERROR_CLIPBOARD_NOT_OPEN = 1418


class _WmPasteIgnoredError(TextInsertionError):
    """The target's window class ignores WM_PASTE; nothing was sent."""

    def __init__(self, window_class: str) -> None:
        super().__init__(
            f"{window_class} windows (Chromium and Electron apps) ignore "
            "WM_PASTE, so the transcript was not pasted. Set Paste mode to "
            "Auto to paste with Ctrl+V."
        )


class _ClipboardNotOpenError(TextInsertionError):
    """One call inside our open clipboard failed with 1418: reopen and retry.

    Private: `Win32ClipboardBackend._with_reopen` consumes it, and what leaves
    the backend once the attempts are spent is `ClipboardContentionError`.
    """


class _ClipboardOpenFailedError(TextInsertionError):
    """`OpenClipboard` kept failing through every one of its retries.

    A `TextInsertionError` to every caller outside the backend, as it always
    was; `Win32ClipboardBackend._with_reopen` tells it apart because on a
    later attempt it means something else (see there).
    """


class _ClipboardWriteUnconfirmedError(ClipboardContentionError):
    """Our write finished and its close was lost, and it could not be read back.

    Contention for the paste: nothing may go out. But the transcript is most
    likely on the clipboard in place of the user's content, so the transaction
    hands `marker` (the counter read before the failed read-back) to a later
    restore, which runs only while the clipboard still holds the transcript.
    """

    def __init__(self, message: str, marker: int | None) -> None:
        super().__init__(message)
        self.marker = marker


class _ClipboardRestoreRefusedError(ClipboardContentionError):
    """A restore found that someone else wrote to the clipboard, and stopped.

    Not a failure to retry: the content there is no longer ours to replace,
    so the restore is over (`TextInserter._finish_restore` logs it as
    `skipped_changed`). Every other restore error means "not done yet".
    """


def _is_clipboard_not_open(exc: BaseException) -> bool:
    """Is this a pywin32 error carrying 1418?

    `pywintypes.error` has `winerror` and `args == (code, function, message)`.
    Only an integer code counts -- a message that merely mentions 1418 is not
    evidence of anything.
    """
    code = getattr(exc, "winerror", None)
    if code is None:
        args = getattr(exc, "args", ())
        code = args[0] if args else None
    return (
        isinstance(code, int)
        and not isinstance(code, bool)
        and code == ERROR_CLIPBOARD_NOT_OPEN
    )


def _raise_if_clipboard_not_open(exc: BaseException) -> None:
    """Turn a 1418 into the reopen signal; return for anything else."""
    if _is_clipboard_not_open(exc):
        raise _ClipboardNotOpenError(str(exc)) from exc


@dataclass(slots=True)
class ClipboardState:
    """Everything the clipboard held, so a dictation can put all of it back.

    `has_text` and `text` are the `CF_UNICODETEXT` reading and keep the
    meaning every reader of this class already relies on -- the "is our
    transcript still on the clipboard" checks in `TextInserter` compare
    against `text`.

    `formats` is what the restore actually writes: `(format id, registered
    name or "", raw bytes)` per format, in the clipboard's own enumeration
    order. It is empty for an empty clipboard, for a state built by hand, and
    for one whose capture hit the size caps -- the restore then falls back to
    writing the text alone, which is what this class did before formats
    existed.
    """

    has_text: bool
    text: str | None
    formats: tuple[tuple[int, str, bytes], ...] = ()


@dataclass(frozen=True, slots=True)
class ClipboardMarker:
    """What the clipboard's sequence counter read right after *our own* write.

    Read inside `set_clipboard_text`, between `CloseClipboard` and the return,
    rather than by the caller in a second call afterwards. A foreign writer
    landing in the gap between those two calls used to make the app adopt that
    writer's number as its own -- after which the "did the clipboard change"
    check compared a number against itself, answered "unchanged", and the
    transaction pasted the stranger's content and then restored over it.

    `sequence` is None when there is no counter to read. The content
    comparison in `TextInserter._clipboard_changed_after_set` carries the
    check then, and covers the microseconds between the close and this read in
    every case.
    """

    sequence: int | None


@dataclass(slots=True)
class _TransactionRecord:
    """Everything one `paste_transaction` log line reports.

    Filled in as the transaction decides each field, so the line can be
    written from a `finally` and is complete whatever was raised.
    """

    transaction_id: int
    requested_mode: str
    target_hwnd: int | None
    foreground: int | None
    chars: int
    actual_mode: str | None = None
    marker: int | None = None
    outcome: str = "failed:unknown"
    restore: str = "none"


@dataclass(slots=True)
class _PendingRestore:
    """A clipboard restore waiting for the target to read what we pasted.

    `handle` is whatever the scheduler handed back and is replaced on every
    reschedule; everything else describes the transaction that armed it and
    never changes.
    """

    transaction_id: int
    previous_state: object
    # The clipboard's sequence counter right after our last write to it: the
    # transcript, or a restore attempt that failed after its `EmptyClipboard`.
    # An unchanged counter is how a retry knows nobody wrote since.
    marker: int | None
    # What our write left on the clipboard, or None when it left it emptied
    # or holding a partial restore: then only the counter can say "ours".
    text: str | None
    target_hwnd: int | None
    armed_at: float
    deadline: float
    handle: object | None = None
    # Restores of this record that failed or could not check the clipboard; at
    # `CLIPBOARD_RESTORE_RETRY_ATTEMPTS` past the first, it is reported.
    failed_attempts: int = 0
    # Runs on the retry timer (`_schedule_restore_retry`) rather than the
    # deferred one, which first waits for the paste target.
    retry: bool = False


def _schedule_on_a_daemon_timer(delay_s: float, callback):
    """Run `callback` once after `delay_s` on a throwaway daemon thread.

    Daemon, because a restore still waiting for a hung target must never keep
    the interpreter alive at exit; `DictationController.shutdown` calls
    `flush_pending_restore` so the user's clipboard is settled deliberately
    instead of by whoever wins that race.
    """
    timer = threading.Timer(max(0.0, delay_s), callback)
    timer.daemon = True
    timer.start()
    return timer


# Standard clipboard formats whose "data" is not memory: a GDI object handle
# (`CF_BITMAP`, `CF_METAFILEPICT`, `CF_PALETTE`, `CF_ENHMETAFILE`) or content
# the owning window draws itself (`CF_OWNERDISPLAY` and the three `CF_DSP*`
# handle formats). Values are fixed by the Win32 API and are written out here
# rather than read from `win32con`, so the decision does not depend on a
# module the tests replace.
_CLIPBOARD_FORMATS_NOT_COPYABLE_AS_BYTES = frozenset(
    {
        0x0002,  # CF_BITMAP -- Windows re-synthesizes it from a restored CF_DIB
        0x0003,  # CF_METAFILEPICT
        0x0009,  # CF_PALETTE
        0x000E,  # CF_ENHMETAFILE
        0x0080,  # CF_OWNERDISPLAY
        0x0082,  # CF_DSPBITMAP
        0x0083,  # CF_DSPMETAFILEPICT
        0x008E,  # CF_DSPENHMETAFILE
    }
)
# CF_PRIVATEFIRST..CF_PRIVATELAST and CF_GDIOBJFIRST..CF_GDIOBJLAST. The
# application that copied frees these itself -- a private format's memory is
# its own and a GDIOBJ entry is a GDI handle -- so neither means anything once
# another process sets it.
_CLIPBOARD_PRIVATE_FORMAT_RANGES = ((0x0200, 0x02FF), (0x0300, 0x03FF))
# Registered formats that describe the *live* IDataObject of the application
# that copied, rather than data. Restoring them would advertise an object that
# no longer exists. Both were on the user's clipboard when this was measured.
_CLIPBOARD_FORMAT_NAMES_NOT_RESTORABLE = frozenset({"DataObject", "Ole Private Data"})
# The registered formats that keep one clipboard write out of Windows'
# clipboard history (Win+V) and out of cloud clipboard sync. Microsoft,
# "Clipboard Formats", section "Cloud Clipboard and Clipboard History Formats"
# (https://learn.microsoft.com/en-us/windows/win32/dataxchg/clipboard-formats,
# read 2026-10-01): ExcludeClipboardContentFromMonitorProcessing takes "any
# data" and keeps every format of the write out of both;
# CanIncludeInClipboardHistory and CanUploadToCloudClipboard take "a serialized
# DWORD value of zero" to keep it out of the history and out of sync
# respectively, and each "does not affect" the other. All three are obtained
# through `RegisterClipboardFormat`. A DWORD 0 goes into each, which is also
# "any data" for the first. Set on a transcript the transaction will replace
# with the user's own content -- never on a restore, never on a transcript the
# user keeps -- and kept, like any other format, when the capture finds them
# on someone else's copy (a password manager marks a secret this way).
_CLIPBOARD_HISTORY_EXCLUSION_FORMAT_NAMES = (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard",
)
_CLIPBOARD_HISTORY_EXCLUSION_PAYLOAD = (0).to_bytes(4, "little")
# Formats Windows synthesizes on demand from another one (CF_TEXT, CF_BITMAP,
# CF_OEMTEXT, CF_DIB, CF_UNICODETEXT, CF_LOCALE, CF_DIBV5): after a partial
# restore of ours they may appear without our having written them.
_CLIPBOARD_SYNTHESIZED_FORMATS = frozenset({1, 2, 7, 8, 13, 16, 17})
# The range `RegisterClipboardFormat` hands out; below it a format has no name.
_FIRST_REGISTERED_CLIPBOARD_FORMAT = 0xC000
# What `SetClipboardData` requires of the block it is given.
GMEM_MOVEABLE = 0x0002

# Window classes whose window procedure ignores WM_PASTE. Measured into an
# Edge --app page with a focused textarea (r27 paste investigation,
# 2026-09-27; on 2026-10-03 to the top-level window and to its
# `Chrome_RenderWidgetHostHWND` child alike): `SendMessageTimeout(WM_PASTE)`
# succeeded, the page saw no paste event and nothing was inserted, so the
# transaction reported a paste that never happened. Electron apps (VS Code,
# Slack, ...) are Chromium windows of the same class. WM_PASTE is not sent to
# them at all: the paste fails cleanly before any keystroke, the clipboard is
# put back and the Insert offer can paste it once SendInput works again.
_WM_PASTE_IGNORING_WINDOW_CLASSES = frozenset(
    {"Chrome_WidgetWin_1", "Chrome_RenderWidgetHostHWND"}
)
_WINDOW_CLASS_BUFFER_CHARS = 256

_UNAVAILABLE_CLIPBOARD_TEXT = object()
# "No pending restore handed a previous clipboard state over to this
# transaction", which `None` cannot express: a backend may legitimately
# capture `None` as the state itself.
_NO_INHERITED_STATE = object()
# The three answers of `TextInserter._restore_check`.
_RESTORE_OURS = "ours"
_RESTORE_CHANGED = "changed"
_RESTORE_BUSY = "busy"


class Win32ClipboardBackend:
    def __init__(
        self,
        retry_count: int = 10,
        retry_sleep_s: float = 0.01,
        reopen_attempts: int = 3,
    ) -> None:
        self._retry_count = retry_count
        self._retry_sleep_s = retry_sleep_s
        # How often one clipboard operation is opened afresh after a 1418
        # before it gives up as contention. Each attempt may spend the open
        # retries above as well, so the worst case of one operation on the Qt
        # thread is about reopen_attempts x (retry_count x retry_sleep_s +
        # retry_sleep_s), 0.33 s at the defaults. One paste runs up to four
        # such operations -- the capture, the write, the read-back after a
        # lost close and the changed-after-set read -- so about 1.3 s.
        self._reopen_attempts = max(1, int(reopen_attempts))
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        # The clipboard's data blocks are HGLOBALs, so reading and writing
        # their bytes goes through kernel32. `use_last_error=True` on both
        # handles keeps these calls from overwriting the thread's own
        # `GetLastError`, which `hotkey.Win32HotkeyApi.get_last_error` reads.
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _with_reopen(
        self,
        operation: str,
        body,
        exhausted_error=None,
        *,
        reopen_on_lost_close: bool = False,
    ):
        """Run `body` inside an open clipboard, opening afresh after a 1418.

        Returns `(result, lost_close)`. `body` raises `_ClipboardNotOpenError`
        for a call that found the clipboard closed under it. A close refused
        with 1418 after a body that finished is not retried by default: the
        body's work is done, and what that means is the caller's to decide --
        a read whose every call raises on a closed clipboard has its answer, a
        write has to be read back (`set_clipboard_text`), which is what
        `lost_close` is for. `reopen_on_lost_close` makes that close a reason
        to run the body again instead, for a body whose reads do not all raise
        on a closed clipboard (`capture_clipboard_state`).

        The spent attempts raise `exhausted_error()`, by default
        `ClipboardContentionError`, which every caller already treats as "try
        again, and never write over the clipboard". So does an open that fails
        on a later attempt: by then an earlier attempt may have emptied the
        clipboard, and only `exhausted_error` knows whether it has to be put
        back. A first open that fails stays the plain open failure it always
        was, since nothing of ours has touched the clipboard yet.

        Each call can spend `reopen_attempts` opens, each with its own open
        retries; one paste runs several calls (capture, write, the read-back
        after a lost close, the changed-after-set read), so its worst case on
        the Qt thread is a multiple of one call's (see the 1418 entry in
        `docs/agents/text-insertion.md`).

        This survives the race rather than closing it: the race comes from
        pywin32 opening with a NULL owner window, and opening with a window of
        our own is a separate, larger change -- and an owner window without a
        message pump blocks every other program's `EmptyClipboard` (see the
        paste-transaction entry in `docs/agents/text-insertion.md`).
        """
        for attempt in range(1, self._reopen_attempts + 1):
            if attempt > 1:
                time.sleep(self._retry_sleep_s)
            context = self._clipboard_opened(tolerate_lost_close=True)
            try:
                with context:
                    result = body()
            except _ClipboardNotOpenError:
                _LOGGER.info(
                    "clipboard_not_open op=%s attempt=%s of %s",
                    operation,
                    attempt,
                    self._reopen_attempts,
                )
                continue
            except _ClipboardOpenFailedError:
                if attempt == 1:
                    raise
                _LOGGER.info(
                    "clipboard_reopen_failed op=%s attempt=%s", operation, attempt
                )
                break
            if context.lost_before_close and reopen_on_lost_close:
                _LOGGER.info(
                    "clipboard_close_not_open op=%s attempt=%s of %s",
                    operation,
                    attempt,
                    self._reopen_attempts,
                )
                continue
            if context.lost_before_close:
                _LOGGER.info("clipboard_close_not_open op=%s", operation)
            return result, context.lost_before_close
        if exhausted_error is not None:
            raise exhausted_error()
        raise ClipboardContentionError(
            "Another program kept closing the clipboard while it was in use "
            "(Windows error 1418); left the current clipboard untouched."
        )

    def _unicode_text_or_none(self) -> str | None:
        """`CF_UNICODETEXT` of the open clipboard, or None when there is none."""
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return None
        try:
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except Exception as exc:
            _raise_if_clipboard_not_open(exc)
            raise
        return str(text)

    def capture_clipboard_state(self) -> ClipboardState:
        # A lost close is a reason to read again, never a finished capture:
        # the byte copy reads through ctypes, whose `GetClipboardData` answers
        # NULL on a clipboard closed under us without raising, and the copy
        # counts NULL as an unreadable format. A capture that read nothing
        # because its open was taken would come back as an empty state, and the
        # restore would write that empty state over the user's clipboard. The
        # refused close is the one reliable sign: the open can only be taken
        # by another program's `CloseClipboard`, and once it is, ours fails.
        state, _lost_close = self._with_reopen(
            "capture", self._capture_open_clipboard, reopen_on_lost_close=True
        )
        return state

    def _capture_open_clipboard(self) -> ClipboardState:
        # The whole id list is read in one pass before anything else touches
        # the clipboard. `EnumClipboardFormats` is resumed from the id it is
        # given, so interleaving it with the reads below would depend on what
        # those reads do to the list -- and what `GetClipboardData` does to it
        # for a format Windows synthesizes on demand is not something this
        # code should have to know.
        formats = self._copy_clipboard_formats(self._enumerate_clipboard_formats())
        text = self._unicode_text_or_none()
        if text is not None:
            return ClipboardState(has_text=True, text=text, formats=formats)
        return ClipboardState(has_text=False, text=None, formats=formats)

    def _enumerate_clipboard_formats(self) -> list[int]:
        """Every format id on the clipboard, in the clipboard's own order.

        `EnumClipboardFormats` continues from the id it is handed and answers
        0 at the end. The `seen` guard is not decoration: this runs on the Qt
        main thread, and an id repeating would otherwise loop here forever.

        A failed enumeration returns what it has rather than raising. This
        call is new on the path of every paste, and the caller's caller turns
        anything raised out of a capture into a failed insertion -- so a
        clipboard this app cannot enumerate would cost the user the transcript
        they are waiting for, where today it costs them the formats beyond the
        text. Reading the text back is still the gate: it needs the clipboard
        open just as this does, and it still raises when it fails.
        """
        format_ids: list[int] = []
        seen: set[int] = set()
        current = 0
        while True:
            try:
                current = int(win32clipboard.EnumClipboardFormats(current) or 0)
            except Exception:
                _LOGGER.debug(
                    "The clipboard's formats could not be enumerated past %s",
                    current,
                    exc_info=True,
                )
                return format_ids
            if current == 0 or current in seen:
                return format_ids
            seen.add(current)
            format_ids.append(current)

    def _copy_clipboard_formats(
        self, format_ids: list[int]
    ) -> tuple[tuple[int, str, bytes], ...]:
        """Copy the bytes of every format that can be handed back later.

        Returns `()` when a size cap is hit: a partial clipboard is not
        better than the text-only restore this app has always done, and the
        caller keeps `CF_UNICODETEXT` either way.
        """
        collected: list[tuple[int, str, bytes]] = []
        total_bytes = 0
        unreadable = 0
        for format_id in format_ids:
            if self._format_belongs_to_its_owner(format_id):
                continue
            name = self._clipboard_format_name(format_id)
            if name in _CLIPBOARD_FORMAT_NAMES_NOT_RESTORABLE:
                continue
            try:
                handle = self._clipboard_data_handle(format_id)
                size = self._global_size(handle) if handle else 0
            except Exception:
                _LOGGER.debug(
                    "Clipboard format %s could not be measured",
                    format_id,
                    exc_info=True,
                )
                unreadable += 1
                continue
            if size <= 0:
                # A NULL handle is a delayed-rendering format whose owner has
                # exited; a zero-sized block carries nothing to put back.
                unreadable += 1
                continue
            if (
                size > CLIPBOARD_CAPTURE_MAX_FORMAT_BYTES
                or total_bytes + size > CLIPBOARD_CAPTURE_MAX_TOTAL_BYTES
            ):
                # Read off `GlobalSize` before the copy, so an oversized
                # format costs neither the memory nor the memcpy. The two
                # numbers describe everything read up to and including the
                # format that tripped the cap, and never its content.
                _LOGGER.warning(
                    "clipboard_capture_truncated formats=%s bytes=%s",
                    len(collected) + 1,
                    total_bytes + size,
                )
                return ()
            try:
                payload = self._copy_global_bytes(handle, size)
            except Exception:
                # A clipboard manager holding the block refuses the lock.
                _LOGGER.debug(
                    "Clipboard format %s could not be read",
                    format_id,
                    exc_info=True,
                )
                unreadable += 1
                continue
            collected.append((format_id, name, payload))
            total_bytes += size
        if unreadable:
            _LOGGER.info("clipboard_capture_unreadable formats=%s", unreadable)
        return tuple(collected)

    @staticmethod
    def _format_belongs_to_its_owner(format_id: int) -> bool:
        """Is this a format only the application that copied it can hand on?

        `SetClipboardData` for the GDI and owner-drawn formats takes an object
        handle rather than memory, and the private and GDIOBJ ranges are freed
        by the copying application itself -- so none of them survives being
        copied out and set again by this process. `CF_BITMAP` is no loss:
        Windows synthesizes it again from a restored `CF_DIB`.
        """
        if format_id in _CLIPBOARD_FORMATS_NOT_COPYABLE_AS_BYTES:
            return True
        return any(
            first <= format_id <= last
            for first, last in _CLIPBOARD_PRIVATE_FORMAT_RANGES
        )

    @staticmethod
    def _clipboard_format_name(format_id: int) -> str:
        """The registered name of `format_id`, or "" for a standard format.

        Only ids from 0xC000 up can have one -- that is the range
        `RegisterClipboardFormat` hands out -- and pywin32 raises
        `pywintypes.error` (87, 'GetClipboardFormatName') for every other id,
        measured against the installed pywin32 build 312. Asking only the ids
        that can answer keeps an exception off the common path.
        """
        if format_id < _FIRST_REGISTERED_CLIPBOARD_FORMAT:
            return ""
        try:
            return str(win32clipboard.GetClipboardFormatName(format_id) or "")
        except Exception:
            return ""

    def _clipboard_data_handle(self, format_id: int) -> int:
        """The HGLOBAL the clipboard holds for `format_id`, or 0.

        `win32clipboard.GetClipboardData` is deliberately not used here: it
        decodes what it reads -- text formats come back as `str`, `CF_HDROP`
        as a tuple of paths -- and the restore needs the bytes the clipboard
        actually holds. The handle stays the clipboard's and is only read.
        """
        get_clipboard_data = self._user32.GetClipboardData
        get_clipboard_data.argtypes = (ctypes.wintypes.UINT,)
        # Declared, so a handle at or above 0x8000_0000 does not come back
        # negative from the default 32-bit signed restype.
        get_clipboard_data.restype = ctypes.c_void_p
        return int(get_clipboard_data(format_id) or 0)

    def _global_size(self, handle: int) -> int:
        global_size = self._kernel32.GlobalSize
        global_size.argtypes = (ctypes.c_void_p,)
        global_size.restype = ctypes.c_size_t
        return int(global_size(handle) or 0)

    def _copy_global_bytes(self, handle: int, size: int) -> bytes:
        """`size` bytes out of the clipboard's own block, into ours."""
        address = self._global_lock(handle)
        if address == 0:
            raise TextInsertionError("GlobalLock failed for a clipboard format.")
        try:
            return ctypes.string_at(address, size)
        finally:
            self._global_unlock(handle)

    def _global_lock(self, handle: int) -> int:
        global_lock = self._kernel32.GlobalLock
        global_lock.argtypes = (ctypes.c_void_p,)
        global_lock.restype = ctypes.c_void_p
        return int(global_lock(handle) or 0)

    def _global_unlock(self, handle: int) -> None:
        global_unlock = self._kernel32.GlobalUnlock
        global_unlock.argtypes = (ctypes.c_void_p,)
        global_unlock.restype = ctypes.wintypes.BOOL
        global_unlock(handle)

    def set_clipboard_text(
        self, text: str, exclude_from_history: bool = False
    ) -> ClipboardMarker:
        """Write `text` as the clipboard's only content and return its marker.

        `exclude_from_history` adds the three registered formats that keep this
        write out of Windows' clipboard history and cloud clipboard (see
        `_CLIPBOARD_HISTORY_EXCLUSION_FORMAT_NAMES`), inside the same open, so
        no monitor ever sees the transcript without them. Best-effort: a format
        that cannot be registered or set is logged and costs the exclusion,
        never the write.
        """
        # Set once our `EmptyClipboard` has succeeded in any attempt: from then
        # on the clipboard is ours to put back if no write of ours lands, and a
        # retry must not empty a clipboard someone else has written to since.
        emptied = False
        # Registering needs no open clipboard, so it stays out of the open.
        exclusion_format_ids = (
            self._history_exclusion_format_ids() if exclude_from_history else []
        )
        exclusion_failures = 0

        def _write_into_open_clipboard() -> None:
            nonlocal emptied, exclusion_failures
            if emptied:
                self._refuse_if_written_since_our_empty()
            try:
                win32clipboard.EmptyClipboard()
            except Exception as exc:
                _raise_if_clipboard_not_open(exc)
                if emptied:
                    raise ClipboardEmptiedError(
                        f"The clipboard was emptied but could not be written: {exc}"
                    ) from exc
                raise
            emptied = True
            try:
                win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
            except Exception as exc:
                _raise_if_clipboard_not_open(exc)
                raise ClipboardEmptiedError(
                    f"The clipboard was emptied but could not be written: {exc}"
                ) from exc
            if exclude_from_history:
                exclusion_failures = self._set_history_exclusion_formats(
                    exclusion_format_ids
                )

        def _spent() -> TextInsertionError:
            message = (
                "Another program kept closing the clipboard while it was in "
                "use (Windows error 1418)"
            )
            if emptied:
                return ClipboardEmptiedError(
                    f"The clipboard was emptied but could not be written: {message}."
                )
            return ClipboardContentionError(
                f"{message}; left the current clipboard untouched."
            )

        _result, lost_close = self._with_reopen(
            "write", _write_into_open_clipboard, exhausted_error=_spent
        )
        if exclusion_failures:
            # The transcript is on the clipboard either way; without these
            # formats Windows may list it in Win+V as it did before they
            # existed. A warning, because the user sees an entry they did not
            # expect, never an error, because the paste is unaffected.
            _LOGGER.warning(
                "clipboard_history_exclusion_partial failed=%d", exclusion_failures
            )
        if lost_close:
            return self._confirm_write_landed(text)
        # The counter is read here -- after `CloseClipboard` and before
        # returning -- so the number the caller gets is the one our write
        # produced. A caller that reads it in a second call instead adopts the
        # number of any foreign writer that landed in between; see
        # `ClipboardMarker`. After the close because whether the counter
        # increments on `SetClipboardData` or on `CloseClipboard` is not
        # documented and nothing here may depend on the answer: "the value
        # right after our close" is true either way.
        return ClipboardMarker(sequence=self.get_clipboard_sequence_number())

    def _history_exclusion_format_ids(self) -> list[int]:
        """The registered ids of the exclusion formats, 0 for any refused."""
        format_ids = []
        for name in _CLIPBOARD_HISTORY_EXCLUSION_FORMAT_NAMES:
            try:
                format_id = int(win32clipboard.RegisterClipboardFormat(name) or 0)
            except Exception:
                _LOGGER.debug(
                    "Clipboard format %r could not be registered",
                    name,
                    exc_info=True,
                )
                format_id = 0
            format_ids.append(format_id)
        return format_ids

    def _set_history_exclusion_formats(self, format_ids: list[int]) -> int:
        """Set each exclusion format on the open clipboard; return the misses."""
        failures = 0
        for format_id in format_ids:
            if format_id == 0:
                failures += 1
                continue
            try:
                written = self._set_clipboard_bytes(
                    format_id, _CLIPBOARD_HISTORY_EXCLUSION_PAYLOAD
                )
            except Exception:
                _LOGGER.debug(
                    "Clipboard format %s could not be set",
                    format_id,
                    exc_info=True,
                )
                written = False
            if not written:
                failures += 1
        return failures

    def _refuse_if_written_since_our_empty(self) -> None:
        """A write retry must find the clipboard as our `EmptyClipboard` left it.

        Between our refused write and this retry, the program that closed the
        clipboard under us may have written to it. Emptying it again would
        erase that program's content, which contention never does.
        """
        try:
            first_format = int(win32clipboard.EnumClipboardFormats(0) or 0)
        except Exception as exc:
            _raise_if_clipboard_not_open(exc)
            raise ClipboardContentionError(
                "The clipboard could not be checked before writing to it again; "
                "left it untouched."
            ) from exc
        if first_format:
            raise ClipboardContentionError(
                "Another program wrote to the clipboard while this app was "
                "writing to it; left the current clipboard untouched."
            )

    def _confirm_write_landed(self, text: str) -> ClipboardMarker:
        """Our write finished, but another program closed the clipboard first.

        `SetClipboardData` handed the data over and only `CloseClipboard` was
        refused with 1418, so the transcript is on the clipboard unless that
        program wrote after us. The content read back is the evidence, never
        the handle: exactly our text means the write landed and the paste may
        go on; anything else is contention.

        The counter is read BEFORE the text. Read after it, a foreign write
        between the two reads would become "our" marker -- the gap
        `ClipboardMarker` exists to close. Read before, the marker can only
        describe a state at or before the one whose text was confirmed.
        """
        sequence = self.get_clipboard_sequence_number()
        try:
            current = self.get_clipboard_text()
        except Exception as exc:
            # Most likely our transcript is there and the user's content is
            # gone: the caller restores it later, once it can be read.
            raise _ClipboardWriteUnconfirmedError(
                "The clipboard was closed by another program during the write "
                "and could not be read back; it is put back once it can be read.",
                marker=sequence,
            ) from exc
        if current != text:
            raise ClipboardContentionError(
                "The clipboard was closed by another program during the write "
                "and no longer holds the transcript; left it untouched."
            )
        _LOGGER.info("clipboard_set_landed_despite_close_error")
        return ClipboardMarker(sequence=sequence)

    def get_foreground_window(self) -> int | None:
        """The window Windows reports as foreground right now, or None.

        Read again immediately before the paste keystroke: `SendInput` goes to
        whatever holds the focus at that moment and ignores `target_hwnd`
        entirely, so an Alt+Tab during the modifier-release wait delivered the
        transcript to a window the user never dictated into.

        None means "no answer", which every caller treats as "no evidence of a
        change" rather than as a change -- a NULL foreground is what Windows
        reports while no window is active at all, e.g. during a desktop switch.
        """
        getter = getattr(self._user32, "GetForegroundWindow", None)
        if getter is None:
            return None
        # Declared, so a handle at or above 0x8000_0000 does not come back
        # negative from the default 32-bit signed restype. This module owns its
        # own `user32` handle, so the declaration reaches no other caller's.
        getter.restype = ctypes.wintypes.HWND
        return int(getter() or 0) or None

    def restore_clipboard_state(self, state: ClipboardState) -> None:
        """Put back everything `capture_clipboard_state` was able to copy.

        A state with no formats -- an empty clipboard, a capture that hit the
        size caps, a state built by hand -- restores the text alone, which is
        what this method did before formats existed. The text is also written
        separately when `CF_UNICODETEXT` is not among the formats that were
        set, so this can never put back less text than it used to.

        Through `_with_reopen`, and a lost close writes again: the formats go
        in through ctypes `SetClipboardData`, which answers NULL on a
        clipboard closed under us without raising, so a manager closing our
        open after the `EmptyClipboard` left the user with nothing. A
        reattempt never empties what someone else wrote: before our first
        `EmptyClipboard` the counter must be unchanged since this call began,
        and after it the clipboard may hold nothing but our own partial
        restore (`_holds_only_our_restore`); either refusal raises
        `_ClipboardRestoreRefusedError`. A restore that wrote nothing it had
        to write raises `ClipboardEmptiedError`; the transaction retries it
        later (`TextInserter._finish_restore`).
        """
        emptied = False
        wrote_anything = False
        wants_text = bool(state.has_text and state.text is not None)
        # Read before the first open: a reattempt that has not emptied yet
        # must find nobody has written since.
        sequence_before = self.get_clipboard_sequence_number()

        def _restore_into_open_clipboard() -> None:
            nonlocal emptied, wrote_anything
            if emptied:
                if not self._holds_only_our_restore(state):
                    raise _ClipboardRestoreRefusedError(
                        "Another program wrote to the clipboard while it was "
                        "being restored; left its content untouched."
                    )
            elif self.get_clipboard_sequence_number() != sequence_before:
                raise _ClipboardRestoreRefusedError(
                    "Another program wrote to the clipboard before it could be "
                    "restored; left its content untouched."
                )
            try:
                win32clipboard.EmptyClipboard()
            except Exception as exc:
                _raise_if_clipboard_not_open(exc)
                raise
            emptied = True
            text_restored, written = self._restore_clipboard_formats(state.formats)
            if wants_text and not text_restored:
                try:
                    win32clipboard.SetClipboardText(state.text, win32con.CF_UNICODETEXT)
                except Exception as exc:
                    _raise_if_clipboard_not_open(exc)
                    raise ClipboardEmptiedError(
                        f"The clipboard was emptied but its text could not be "
                        f"written back: {exc}"
                    ) from exc
                written += 1
            wrote_anything = written > 0

        def _spent() -> TextInsertionError:
            message = (
                "Another program kept closing the clipboard while it was being "
                "restored (Windows error 1418)"
            )
            if emptied:
                return ClipboardEmptiedError(
                    f"The clipboard was emptied but could not be restored: {message}."
                )
            return ClipboardContentionError(f"{message}; left it untouched.")

        self._with_reopen(
            "restore",
            _restore_into_open_clipboard,
            exhausted_error=_spent,
            reopen_on_lost_close=True,
        )
        if not wrote_anything and (state.formats or wants_text):
            raise ClipboardEmptiedError(
                "The clipboard was emptied but none of its content could be "
                "written back."
            )

    def _holds_only_our_restore(self, state: ClipboardState) -> bool:
        """The open clipboard holds nothing, or only formats of `state`.

        The guard of a reattempt inside one `restore_clipboard_state` call,
        after our own `EmptyClipboard`: within that call's reopen budget
        (about 0.3 s) the clipboard is still ours while it holds nothing --
        our empty followed by a ctypes write answered NULL leaves exactly
        that -- or only what our partial restore wrote. Every present format
        must match a captured one byte for byte (trailing NULs aside:
        `GlobalSize` may round a block up), or be one Windows synthesizes
        from such a format (`_CLIPBOARD_SYNTHESIZED_FORMATS`), and at least
        one must match. A format that cannot be read counts only if `state`
        has it.

        Heuristic by nature, which is why a retry seconds later never uses it
        and goes by the sequence counter instead (`TextInserter._restore_check`):
        a clear, or a copy of byte-identical content, inside that 0.3 s window
        passes as ours.
        """
        format_ids = self._enumerate_clipboard_formats()
        if not format_ids:
            return True
        ours = {
            format_id: payload.rstrip(b"\x00")
            for format_id, _name, payload in state.formats
        }
        if state.has_text and state.text is not None:
            ours.setdefault(
                win32con.CF_UNICODETEXT,
                str(state.text).encode("utf-16-le").rstrip(b"\x00"),
            )
        present = {
            format_id: payload.rstrip(b"\x00")
            for format_id, _name, payload in self._copy_clipboard_formats(format_ids)
        }
        matched = False
        for format_id in format_ids:
            if format_id in present:
                if ours.get(format_id) == present[format_id]:
                    matched = True
                    continue
                if format_id in ours or format_id not in _CLIPBOARD_SYNTHESIZED_FORMATS:
                    return False
                continue
            if (
                format_id not in ours
                and format_id not in _CLIPBOARD_SYNTHESIZED_FORMATS
            ):
                return False
        return matched

    def _restore_clipboard_formats(
        self, formats: tuple[tuple[int, str, bytes], ...]
    ) -> tuple[bool, int]:
        """Set every captured format; answer (text among them, formats set).

        One format that cannot be set must not cost the user the rest of
        their clipboard, so a refusal is counted and the remaining formats are
        still written. The caller holds the clipboard open and has emptied it.
        """
        failed = 0
        written = 0
        text_restored = False
        for format_id, name, payload in formats:
            target_id = self._restore_target_format_id(format_id, name)
            if target_id and self._set_clipboard_bytes(target_id, payload):
                text_restored = text_restored or target_id == win32con.CF_UNICODETEXT
                written += 1
                continue
            failed += 1
        if failed:
            _LOGGER.warning("clipboard_restore_partial failed=%s", failed)
        return text_restored, written

    def _restore_target_format_id(self, format_id: int, name: str) -> int:
        """Under which id this format has to be set now, or 0 for "cannot".

        A registered format's name atom lives for the whole session, so the
        captured id is normally still the right one. It is verified rather
        than assumed because setting data under an id that has come to mean
        another format would hand that format our bytes; a standard format's
        id is fixed by the API and needs no check.
        """
        if not name:
            return format_id
        if self._clipboard_format_name(format_id) == name:
            return format_id
        try:
            return int(win32clipboard.RegisterClipboardFormat(name) or 0)
        except Exception:
            _LOGGER.debug(
                "Clipboard format %r could not be registered again",
                name,
                exc_info=True,
            )
            return 0

    def _set_clipboard_bytes(self, format_id: int, payload: bytes) -> bool:
        """Hand one format's bytes to the clipboard in an HGLOBAL of its own.

        `SetClipboardData` takes ownership of the block on success, so the
        handle is freed here only when the call failed: freeing it afterwards
        would free memory the clipboard now owns.
        """
        handle = self._allocate_global(payload)
        if handle == 0:
            return False
        set_clipboard_data = self._user32.SetClipboardData
        set_clipboard_data.argtypes = (ctypes.wintypes.UINT, ctypes.c_void_p)
        set_clipboard_data.restype = ctypes.c_void_p
        try:
            written = set_clipboard_data(format_id, handle)
        except Exception:
            _LOGGER.debug(
                "Clipboard format %s could not be set", format_id, exc_info=True
            )
            written = None
        if written:
            return True
        self._global_free(handle)
        return False

    def _allocate_global(self, payload: bytes) -> int:
        """A moveable block holding `payload`, or 0 when it cannot be made.

        `GMEM_MOVEABLE` is what `SetClipboardData` requires; a block allocated
        fixed is rejected by it.
        """
        global_alloc = self._kernel32.GlobalAlloc
        global_alloc.argtypes = (ctypes.wintypes.UINT, ctypes.c_size_t)
        global_alloc.restype = ctypes.c_void_p
        handle = int(global_alloc(GMEM_MOVEABLE, len(payload)) or 0)
        if handle == 0:
            return 0
        address = self._global_lock(handle)
        if address == 0:
            self._global_free(handle)
            return 0
        try:
            ctypes.memmove(address, payload, len(payload))
        finally:
            self._global_unlock(handle)
        return handle

    def _global_free(self, handle: int) -> None:
        global_free = self._kernel32.GlobalFree
        global_free.argtypes = (ctypes.c_void_p,)
        global_free.restype = ctypes.c_void_p
        global_free(handle)

    def get_clipboard_sequence_number(self) -> int | None:
        getter = getattr(self._user32, "GetClipboardSequenceNumber", None)
        if getter is None:
            return None
        getter.restype = ctypes.wintypes.DWORD
        return int(getter() or 0)

    def get_clipboard_text(self) -> str | None:
        text, _lost_close = self._with_reopen("read", self._unicode_text_or_none)
        return text

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
            if not any(int(get_state(vk)) & 0x8000 for vk in _MODIFIER_VIRTUAL_KEYS):
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

        This runs on the deferred restore's own thread, not the Qt main
        thread, and both bounds below still matter: the sleep, because
        `SMTO_ABORTIFHUNG` returns instantly for a hung target and the probe
        throttles nothing then -- measured at 953,446 probes in 1.994 s, one
        core pinned for the whole budget -- and the window check, because a
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

        try:
            if self._send_wm_paste(target_hwnd):
                return "wm_paste"
        except _WmPasteIgnoredError as ignored:
            raise TextInsertionError(
                f"Auto paste failed: SendInput error: {send_input_error}; {ignored}"
            ) from ignored

        raise TextInsertionError(
            f"Auto paste failed: SendInput error: {send_input_error}; WM_PASTE failed."
        )

    class _ClipboardContext:
        def __init__(
            self, backend: Win32ClipboardBackend, *, tolerate_lost_close: bool
        ) -> None:
            self._backend = backend
            self._tolerate_lost_close = tolerate_lost_close
            # True when the body finished and `CloseClipboard` was refused
            # with 1418 -- only reachable with `tolerate_lost_close`.
            self.lost_before_close = False

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

            raise _ClipboardOpenFailedError("Failed to open clipboard.")

        def __exit__(self, exc_type, exc, tb):
            try:
                win32clipboard.CloseClipboard()
            except Exception as close_error:
                if exc is not None:
                    # The body's exception says what happened, and a close that
                    # failed as well must not replace it. It did: a write
                    # refused after `EmptyClipboard` is `ClipboardEmptiedError`,
                    # the one error that makes the transaction put the user's
                    # clipboard back, and a 1418 from this close arrived in its
                    # place -- nothing restored, the clipboard left empty.
                    _LOGGER.debug(
                        "CloseClipboard failed after an error inside the open "
                        "clipboard",
                        exc_info=True,
                    )
                    return False
                if self._tolerate_lost_close and _is_clipboard_not_open(close_error):
                    self.lost_before_close = True
                    return False
                raise
            return False

    def _clipboard_opened(
        self, *, tolerate_lost_close: bool = False
    ) -> Win32ClipboardBackend._ClipboardContext:
        return self._ClipboardContext(self, tolerate_lost_close=tolerate_lost_close)

    def _send_wm_paste(self, target_hwnd: int | None = None) -> bool:
        hwnd = int(target_hwnd or self._get_focused_hwnd() or 0)
        if hwnd == 0:
            return False
        window_class = self._window_class_name(hwnd)
        if window_class in _WM_PASTE_IGNORING_WINDOW_CLASSES:
            raise _WmPasteIgnoredError(window_class)
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

    def _window_class_name(self, hwnd: int) -> str:
        get_class_name = getattr(self._user32, "GetClassNameW", None)
        if get_class_name is None:
            return ""
        get_class_name.argtypes = (
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPWSTR,
            ctypes.c_int,
        )
        get_class_name.restype = ctypes.c_int
        buffer = ctypes.create_unicode_buffer(_WINDOW_CLASS_BUFFER_CHARS)
        if not get_class_name(hwnd, buffer, _WINDOW_CLASS_BUFFER_CHARS):
            return ""
        return buffer.value

    def _send_message_timeout(self, hwnd: int, message: int, timeout_ms: int) -> bool:
        sent, _last_error = self._send_message_timeout_result(hwnd, message, timeout_ms)
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
        restore_delay_s: float = CLIPBOARD_RESTORE_DELAY_S,
        restore_max_wait_s: float = CLIPBOARD_RESTORE_MAX_WAIT_S,
        schedule_fn=None,
        clock=time.monotonic,
        restore_retry_attempts: int = CLIPBOARD_RESTORE_RETRY_ATTEMPTS,
        restore_retry_delay_s: float = CLIPBOARD_RESTORE_RETRY_DELAY_S,
    ) -> None:
        self._backend = backend or Win32ClipboardBackend()
        self._restore_retry_attempts = max(0, int(restore_retry_attempts))
        self._restore_retry_delay_s = max(0.0, float(restore_retry_delay_s))
        # Called with one user-facing sentence when a restore failed for good;
        # on the restore's timer thread, so the controller hands it to a Qt
        # signal, whose queued connection reaches the tray on the Qt thread.
        self._restore_failure_handler = None
        self._sleep_fn = sleep_fn
        self._clipboard_settle_s = clipboard_settle_s
        self._restore_delay_s = restore_delay_s
        self._restore_max_wait_s = restore_max_wait_s
        self._schedule_fn = schedule_fn or _schedule_on_a_daemon_timer
        self._clock = clock
        self._insert_lock = threading.RLock()
        # At most one restore is ever pending: a new paste takes the previous
        # record over rather than queueing behind it, because only the newest
        # transcript is on the clipboard and only one "previous clipboard" is
        # the user's.
        self._pending_restore: _PendingRestore | None = None
        # Clock reading of the last SendInput paste keystroke, or None after a
        # WM_PASTE (synchronous: the target has read the clipboard when the
        # call returns) and before any paste. Written under `_insert_lock` as
        # one assignment, so `paste_pace_remaining_s` reads it without the
        # lock -- a deferred restore holding the lock on its timer thread must
        # not stall the Qt thread asking this.
        self._last_paste_keystroke_at: float | None = None

    def set_restore_failure_handler(self, handler) -> None:
        """Who hears of a clipboard restore that failed for good."""
        self._restore_failure_handler = handler

    def insert_text(self, text: str, target_hwnd: int | None = None) -> bool:
        return self.insert_text_with_options(
            text=text,
            target_hwnd=target_hwnd,
            paste_mode="auto",
        )

    def paste_pace_remaining_s(self) -> float:
        """Seconds until a new paste stops racing the last keystroke's read.

        After a SendInput keystroke the target reads the clipboard whenever it
        gets to it -- an Electron renderer asynchronously, a busy application
        seconds later -- and any clipboard write inside that window hands the
        late reader the NEW text: the first transcript is lost and the second
        is pasted twice. That holds whichever window the next paste is aimed
        at, because the clipboard is one per session, so the answer is not per
        target. The window is the one the deferred restore already trusts,
        `restore_delay_s` from the keystroke, so the caller can hold a paste
        until then instead of guessing a second budget.

        Answers 0.0 before any paste, after a WM_PASTE and once the window
        has passed.
        """
        pasted_at = self._last_paste_keystroke_at
        if pasted_at is None:
            return 0.0
        remaining = self._restore_delay_s - (self._clock() - pasted_at)
        return max(0.0, remaining)

    def _paste_text_with_options(
        self,
        text: str,
        *,
        target_hwnd: int | None,
        paste_mode: str,
        restore_clipboard: bool = True,
    ) -> bool:
        with self._insert_lock:
            record = _TransactionRecord(
                transaction_id=next(_PASTE_TRANSACTION_IDS),
                requested_mode=(paste_mode or "auto").strip().lower(),
                target_hwnd=target_hwnd,
                # Read before the modifier wait, because the window the second
                # reading covers starts here: that wait alone runs up to
                # PASTE_MODIFIER_RELEASE_TIMEOUT_S, and the controller made its
                # own foreground check before calling.
                foreground=self._foreground_window(),
                chars=len(text),
            )
            try:
                self._run_paste_transaction(
                    text, record, restore_clipboard=restore_clipboard
                )
                record.outcome = "pasted"
                return True
            except BaseException as exc:
                record.outcome = f"failed:{type(exc).__name__}"
                raise
            finally:
                # One line per transaction whatever the outcome. A paste that
                # goes wrong in the field leaves nothing else behind: the
                # clipboard has moved on and the target window may be gone.
                _LOGGER.info(
                    "paste_transaction id=%s mode=%s/%s target_hwnd=%s "
                    "foreground=%s chars=%s marker=%s outcome=%s restore=%s",
                    record.transaction_id,
                    record.requested_mode,
                    record.actual_mode,
                    record.target_hwnd,
                    record.foreground,
                    record.chars,
                    record.marker,
                    record.outcome,
                    record.restore,
                )

    def _run_paste_transaction(
        self,
        text: str,
        record: _TransactionRecord,
        *,
        restore_clipboard: bool,
    ) -> None:
        """The transaction itself. The caller holds `_insert_lock` and logs.

        Everything the log line reports is written into `record` as it is
        decided, because the caller has to name all of it whatever this
        raises -- and an exception carries no return value.
        """
        requested_mode = record.requested_mode
        target_hwnd = record.target_hwnd
        foreground_at_start = record.foreground
        transaction_id = record.transaction_id
        if requested_mode != "wm_paste":
            # A held hotkey modifier would turn the injected Ctrl+V into
            # e.g. Ctrl+Alt+V for the target; wait for release first.
            self._wait_for_modifier_release()
        # A restore still waiting for the previous paste has to be settled
        # before this transaction overwrites the clipboard: its timer must not
        # fire into the middle of this one, and what it was going to put back
        # is what "the user's clipboard" still means.
        previous_state, inherited_record = self._take_over_pending_restore()
        if previous_state is _NO_INHERITED_STATE:
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
        # empties the clipboard and writes the captured state over it, and the
        # capture is not lossless -- a GDI-handle format, a private format or
        # an oversized clipboard comes back short. Running it after a *failed*
        # set therefore damaged a clipboard this app had never touched and had
        # pasted nothing from.
        clipboard_was_set = False
        # Whether our transcript reached the clipboard: a restore retried
        # later must then find it there; without it, only an unchanged
        # sequence counter says the clipboard is still ours (`_restore_check`).
        write_landed = False
        try:
            try:
                # A transcript the transaction will replace with the user's
                # own content is kept out of Win+V; one the user keeps
                # ("Keep transcript in clipboard") is an ordinary entry.
                written = self._backend.set_clipboard_text(
                    text, exclude_from_history=restore_clipboard
                )
            except _ClipboardWriteUnconfirmedError as unconfirmed:
                # Contention for the paste, which is refused. The transcript is
                # most likely where the user's content was, and the clipboard
                # cannot be read right now: the restore runs once it can, and
                # only while it still holds the transcript.
                if restore_clipboard:
                    self._arm_restore_retry(
                        transaction_id=transaction_id,
                        previous_state=previous_state,
                        marker=unconfirmed.marker,
                        text=text,
                        target_hwnd=target_hwnd,
                        failed_attempts=0,
                    )
                    record.restore = "retry"
                raise
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
            write_landed = True
            record.restore = "kept"
            clipboard_marker = self._marker_after_write(written)
            record.marker = clipboard_marker
            self._sleep_fn(self._clipboard_settle_s)

            if self._clipboard_changed_after_set(clipboard_marker, text):
                restore_previous_state = False
                raise ClipboardContentionError(
                    "Clipboard changed before paste; left the current "
                    "clipboard untouched."
                )

            if requested_mode != "wm_paste":
                # The last moment at which aborting still costs nothing.
                # WM_PASTE is exempt: it addresses `target_hwnd` itself, so
                # what happens to have the focus does not reach it.
                self._raise_if_the_foreground_changed(foreground_at_start)

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
            record.actual_mode = actual_mode
            self._note_paste_keystroke(actual_mode)

            if actual_mode == "send_input":
                # Nothing here waits for the target and nothing sleeps:
                # this runs on the Qt main thread, streaming inserts come
                # every ~350 ms, and the target may not read the clipboard
                # for seconds. The restore is handed to a timer instead,
                # and the readiness probe runs on that timer's thread.
                if restore_previous_state and clipboard_was_set:
                    self._arm_deferred_restore(
                        transaction_id=transaction_id,
                        previous_state=previous_state,
                        marker=clipboard_marker,
                        text=text,
                        target_hwnd=target_hwnd,
                    )
                    restore_previous_state = False
                    record.restore = "deferred"
            elif self._clipboard_changed_after_set(clipboard_marker, text):
                # The synchronous road only: `SendMessageTimeout(WM_PASTE)`
                # has returned, so the target has read the clipboard and
                # this check is about the window between our write and that
                # read. After a SendInput paste the equivalent check is the
                # one the deferred restore makes.
                restore_previous_state = False
                # Past the paste keystroke, so the target may already have
                # read the transcript. Not retryable.
                raise _ClipboardContentionAfterPaste(
                    "Clipboard changed during paste; left the current "
                    "clipboard untouched."
                )
        except Exception as exc:
            paste_error = exc
            if not paste_sent and isinstance(exc, TextMayHaveBeenPastedError):
                # A partial SendInput from inside the send itself: two of the
                # four events went out, so the target may read the clipboard
                # late exactly as after a whole keystroke.
                self._note_paste_keystroke("send_input")
            if isinstance(exc, (ClipboardContentionError, TextMayHaveBeenPastedError)):
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
                record.restore = "immediate"
                try:
                    self._backend.restore_clipboard_state(previous_state)
                except _ClipboardRestoreRefusedError as exc:
                    # Someone wrote after us: theirs now, never written over.
                    restore_error = exc
                    record.restore = "skipped_changed"
                except Exception as exc:
                    restore_error = exc
                    # Left emptied, or holding the transcript: written again
                    # later rather than left that way for good. Emptied -- by
                    # our write or by this restore -- leaves no content of ours
                    # to recognise, so the counter read now is the evidence.
                    record.restore = "retry"
                    emptied = not write_landed or isinstance(exc, ClipboardEmptiedError)
                    self._arm_restore_retry(
                        transaction_id=transaction_id,
                        previous_state=previous_state,
                        marker=(
                            self._clipboard_sequence_number()
                            if emptied
                            else clipboard_marker
                        ),
                        text=None if emptied else text,
                        target_hwnd=target_hwnd,
                        failed_attempts=1,
                    )
            if (
                inherited_record is not None
                and not clipboard_was_set
                and self._pending_restore is None
            ):
                # This transaction took the pending restore over and then
                # never touched the clipboard: what that record was going to
                # put back is still the user's, and still waiting.
                self._resume_pending_restore(inherited_record)

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
        fallback_allowed = getattr(paste_error, "allow_clipboard_fallback", True)
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
                not paste_sent or isinstance(paste_error, TextMayHaveBeenPastedError)
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
            # Only the synchronous roads reach this now. After a SendInput
            # paste the restore happens long after this call returned success,
            # so there is no caller to raise to and the failure is logged --
            # see `_finish_restore`.
            raise TextMayHaveBeenPastedError(
                f"Text pasted but clipboard restore failed: {restore_error}"
            ) from restore_error

    def _note_paste_keystroke(self, actual_mode: str) -> None:
        """Remember when a target may still read the clipboard late.

        The caller holds `_insert_lock`. A WM_PASTE clears the record rather
        than keeping the previous one: `SendMessageTimeout` returned after its
        target read the clipboard, and that write already replaced whatever a
        late reader of the paste before it would have found.
        """
        if actual_mode == "send_input":
            self._last_paste_keystroke_at = self._clock()
        else:
            self._last_paste_keystroke_at = None

    def _foreground_window(self) -> int | None:
        """Which window is in front, or None when that cannot be answered.

        None also covers a backend without the probe -- every test double and
        any older backend -- and every comparison treats an unknown reading as
        "no evidence of a change" rather than as a change. Refusing to paste
        because a probe is unavailable would break the far commoner case for
        the sake of the rarer one.
        """
        getter = getattr(self._backend, "get_foreground_window", None)
        if not callable(getter):
            return None
        try:
            hwnd = getter()
        except Exception:
            # Probing the foreground must never be what breaks a paste, the
            # same rule `_wait_for_modifier_release` follows.
            return None
        if hwnd is None:
            return None
        try:
            return int(hwnd) or None
        except (TypeError, ValueError):
            return None

    def _raise_if_the_foreground_changed(self, foreground_at_start: int | None) -> None:
        """Abort before the keystroke when the user has switched windows.

        Between the controller's own foreground check and the keystroke sit
        the modifier-release wait (up to PASTE_MODIFIER_RELEASE_TIMEOUT_S) and
        the clipboard settle sleep. An Alt+Tab inside that window sent Ctrl+V
        into whatever had come to the front -- the transcript pasted into a
        stranger's window while the app reported success. `target_hwnd` is no
        help on this road: `SendInput` addresses the focus, not a handle.

        Nothing has been pasted at this point, so this is a plain
        `TextInsertionError`: retryable, and the clipboard is restored by the
        caller's `finally` exactly as for every other pre-keystroke failure.
        """
        if foreground_at_start is None:
            return
        foreground_now = self._foreground_window()
        if foreground_now is None or foreground_now == foreground_at_start:
            return
        raise TextInsertionError(
            "The foreground window changed before the paste keystroke; "
            "nothing was pasted."
        )

    def _marker_after_write(self, written: object) -> int | None:
        """The sequence number of our own write, or the best stand-in for it.

        A backend that reports a `ClipboardMarker` read the counter inside its
        own write, which is the only place a foreign writer cannot get between
        the write and the reading. Backends that predate the marker -- and the
        test doubles -- return something else, and the read here is the old
        behaviour with the old gap; the content comparison in
        `_clipboard_changed_after_set` is what closes that gap in both cases.
        """
        if isinstance(written, ClipboardMarker):
            return written.sequence
        return self._clipboard_sequence_number()

    def _take_over_pending_restore(self) -> tuple[object, _PendingRestore | None]:
        """Settle a pending restore before this transaction overwrites it.

        Returns the previous clipboard state to carry over and the record it
        came from, or `(_NO_INHERITED_STATE, None)` when this transaction has
        to capture the current clipboard itself. A transaction that inherits
        and then never touches the clipboard hands the record back
        (`_resume_pending_restore`).

        Capturing afresh while our own previous transcript is still on the
        clipboard would capture THAT, and restore a transcript over the user's
        clipboard at the end of a streaming dictation -- which pastes every
        ~350 ms, well inside the restore delay. So while the clipboard still
        holds what the pending record wrote, the state that record was going
        to put back is still the user's and carries over unchanged.

        The caller holds `_insert_lock`.
        """
        record = self._pending_restore
        if record is None:
            return _NO_INHERITED_STATE, None
        verdict = self._restore_check(record)
        if verdict == _RESTORE_BUSY:
            # Unreadable right now. Only an unmoved counter still proves the
            # clipboard is ours: a user copy moves it, and inheriting then
            # restored the older state over what they had just copied
            # (review of a404479). With a moved counter nothing can be told
            # apart, so this paste is refused before it writes anything and
            # the record keeps its pending slot and its own timer: cancelling
            # and re-arming it started a second chain when the timer had
            # already fired, and pushed its check back on every refusal
            # (review of 4be13ec).
            current = self._clipboard_sequence_number()
            if record.marker is None or current != record.marker:
                self._log_restore_outcome(record, "busy_refused")
                if record.handle is None:
                    if record.retry:
                        self._schedule_restore_retry(record)
                    else:
                        self._schedule_restore(record)
                raise ClipboardContentionError(
                    "The clipboard is in use by another program."
                )
            verdict = _RESTORE_OURS
        self._pending_restore = None
        self._cancel_scheduled_restore(record)
        if verdict == _RESTORE_OURS:
            # A capture would take our own transcript for the user's
            # clipboard.
            self._log_restore_outcome(record, "superseded")
            return record.previous_state, record
        # The user copied something of their own in between, so the older
        # state is no longer what "the user's clipboard" means and restoring
        # it would overwrite what they just copied.
        self._log_restore_outcome(record, "superseded_changed")
        return _NO_INHERITED_STATE, None

    def _resume_pending_restore(self, record: _PendingRestore) -> None:
        """Make a taken-over record pending again. The caller holds the lock."""
        self._pending_restore = record
        self._log_restore_outcome(record, "resumed")
        if record.retry:
            self._schedule_restore_retry(record)
        else:
            self._schedule_restore(record)

    def _arm_deferred_restore(
        self,
        *,
        transaction_id: int,
        previous_state: object,
        marker: int | None,
        text: str,
        target_hwnd: int | None,
    ) -> None:
        """Hand the restore to the scheduler. The caller holds `_insert_lock`."""
        armed_at = self._clock()
        record = _PendingRestore(
            transaction_id=transaction_id,
            previous_state=previous_state,
            marker=marker,
            text=text,
            target_hwnd=target_hwnd,
            armed_at=armed_at,
            deadline=armed_at + max(0.0, self._restore_max_wait_s),
        )
        self._pending_restore = record
        self._schedule_restore(record)

    def _schedule_restore(self, record: _PendingRestore) -> None:
        try:
            record.handle = self._schedule_fn(
                self._restore_delay_s,
                lambda: self._run_deferred_restore(record),
            )
        except Exception:
            # A starved interpreter cannot start another thread. Raising here
            # would report a paste that already landed as a failure, and the
            # streaming retry would then paste it a second time. The record is
            # deliberately left pending instead: the next paste takes it over
            # and `flush_pending_restore` runs it at shutdown, so the user's
            # clipboard is held longer rather than lost.
            record.handle = None
            self._log_restore_outcome(record, "failed")

    def _run_deferred_restore(self, record: _PendingRestore) -> None:
        """Put the previous clipboard back once the target has had its chance.

        Runs on the scheduler's thread. The readiness probe deliberately runs
        BEFORE the lock is taken: it waits up to
        PASTE_TARGET_RESPONSIVE_TIMEOUT_S, and holding `_insert_lock` for that
        long would block the next live streaming insert on the Qt main thread,
        which is the thread this whole arrangement exists to keep free.
        """
        if self._pending_restore is not record:
            # A newer paste already took this record over and its timer fired
            # anyway, because a timer that has started cannot be cancelled.
            # Cheap early-out so a superseded record does not spend the probe
            # budget; the authoritative check is the one under the lock.
            return
        ready = self._wait_for_paste_target_ready(record.target_hwnd)
        with self._insert_lock:
            if self._pending_restore is not record:
                return
            if not ready:
                if self._clock() < record.deadline:
                    self._log_restore_outcome(record, "busy_rescheduled")
                    self._schedule_restore(record)
                    return
                # Out of budget. The transcript stays on the clipboard on
                # purpose: a target still busy after all this has not read it
                # yet, and restoring is what turns its late paste into the
                # user's old content.
                self._pending_restore = None
                self._log_restore_outcome(record, "abandoned_busy")
                return
            self._finish_restore(record)

    def _finish_restore(
        self, record: _PendingRestore, *, may_retry: bool = True
    ) -> None:
        """Restore, unless the clipboard is no longer ours to put back.

        The caller holds `_insert_lock`. The record is retired first, so a
        paste arriving right after this cannot take over a record that is
        being restored.

        A restore that fails, or cannot even check the clipboard because
        another program holds it, is written again from the captured state,
        up to `restore_retry_attempts` times `restore_retry_delay_s` apart on
        the scheduler -- never a sleep on the caller's thread -- and stays the
        pending record meanwhile, so a new paste takes its state over. Only
        then is it reported (`set_restore_failure_handler`). Nobody else is
        waiting for it: the paste was reported long ago. A clipboard someone
        else wrote to ends the record quietly: `skipped_changed`.
        """
        self._pending_restore = None
        verdict = self._restore_check(record)
        if verdict == _RESTORE_CHANGED:
            self._log_restore_outcome(record, "skipped_changed")
            return
        if verdict == _RESTORE_BUSY:
            self._restore_attempt_failed(record, may_retry=may_retry, why="busy")
            return
        try:
            self._backend.restore_clipboard_state(record.previous_state)
        except _ClipboardRestoreRefusedError:
            self._log_restore_outcome(record, "skipped_changed")
            return
        except ClipboardEmptiedError:
            # Our `EmptyClipboard` went through, so the transcript is gone and
            # at most a partial restore of ours is there: from now on only the
            # counter, read after our last write, can recognise it.
            record.text = None
            record.marker = self._clipboard_sequence_number()
            self._restore_attempt_failed(record, may_retry=may_retry, why="failed")
            return
        except Exception:
            # Nothing of ours touched the clipboard: the record stands as is.
            self._restore_attempt_failed(record, may_retry=may_retry, why="failed")
            return
        self._log_restore_outcome(record, "restored")

    def _restore_attempt_failed(
        self, record: _PendingRestore, *, may_retry: bool, why: str
    ) -> None:
        """Count one attempt; retry within the budget, report past it."""
        record.failed_attempts += 1
        if may_retry and record.failed_attempts <= self._restore_retry_attempts:
            self._pending_restore = record
            record.retry = True
            self._log_restore_outcome(record, f"{why}_retrying")
            self._schedule_restore_retry(record)
            return
        self._log_restore_outcome(record, "failed")
        self._report_restore_failure()

    def _arm_restore_retry(
        self,
        *,
        transaction_id: int,
        previous_state: object,
        marker: int | None,
        text: str | None,
        target_hwnd: int | None,
        failed_attempts: int,
    ) -> None:
        """Write the previous clipboard back later. The caller holds the lock."""
        armed_at = self._clock()
        record = _PendingRestore(
            transaction_id=transaction_id,
            previous_state=previous_state,
            marker=marker,
            text=text,
            target_hwnd=target_hwnd,
            armed_at=armed_at,
            deadline=armed_at,
            failed_attempts=failed_attempts,
            retry=True,
        )
        if failed_attempts > self._restore_retry_attempts:
            self._log_restore_outcome(record, "failed")
            self._report_restore_failure()
            return
        self._pending_restore = record
        self._schedule_restore_retry(record)

    def _schedule_restore_retry(self, record: _PendingRestore) -> None:
        try:
            record.handle = self._schedule_fn(
                self._restore_retry_delay_s,
                lambda: self._run_restore_retry(record),
            )
        except Exception:
            # Left pending, as `_schedule_restore` does: the next paste takes
            # it over and `flush_pending_restore` runs it at shutdown.
            record.handle = None
            self._log_restore_outcome(record, "failed")

    def _run_restore_retry(self, record: _PendingRestore) -> None:
        with self._insert_lock:
            if self._pending_restore is not record:
                return
            self._finish_restore(record)

    def _restore_check(self, record: _PendingRestore) -> str:
        """Is the clipboard still ours to restore: ours, changed or busy?

        A record that left our transcript there is ours while the clipboard
        reads exactly that text -- a clipboard manager may set the same
        content again, which moves the counter but changes nothing -- and
        changed when it reads anything else. A record whose clipboard was
        emptied, or holds a partial restore of ours, has no content to
        recognise: it is ours while Windows' sequence counter still reads
        what it read right after our last write (`_PendingRestore.marker`),
        and changed once it has moved. An empty clipboard is therefore never
        evidence on its own: a Win+V "Clear all" or a password manager's clear
        moves the counter, and a restore must not write over it.

        A clipboard that cannot be read is busy, never changed: another
        program holds it, and dropping the record then lost the user's content
        for good (`_finish_restore` retries instead).

        Residual: the user copying our exact transcript back out of the
        document while a retry is pending looks like ours.
        """
        if record.text is None:
            current = self._clipboard_sequence_number()
            if record.marker is not None and current == record.marker:
                return _RESTORE_OURS
            return _RESTORE_CHANGED
        try:
            changed = self._clipboard_changed_after_set(record.marker, record.text)
        except ClipboardContentionError:
            # `_clipboard_text` turns a failed read into contention.
            return _RESTORE_BUSY
        return _RESTORE_CHANGED if changed else _RESTORE_OURS

    def _report_restore_failure(self) -> None:
        handler = self._restore_failure_handler
        if not callable(handler):
            return
        try:
            handler(
                "Your clipboard could not be put back after a paste: it may "
                "hold the transcript, or nothing, instead of what you had "
                "copied. The transcript is in history."
            )
        except Exception:
            _LOGGER.exception("Could not report the failed clipboard restore")

    @staticmethod
    def _cancel_scheduled_restore(record: _PendingRestore) -> None:
        handle = record.handle
        record.handle = None
        cancel = getattr(handle, "cancel", None)
        if not callable(cancel):
            return
        try:
            cancel()
        except Exception:
            # A timer that has already started cannot be cancelled, and a
            # scheduler handle is not this module's to reason about. The
            # identity check in `_run_deferred_restore` is what actually stops
            # a superseded record, so a refused cancel costs nothing.
            _LOGGER.debug(
                "Could not cancel the pending clipboard restore", exc_info=True
            )

    def _log_restore_outcome(self, record: _PendingRestore, outcome: str) -> None:
        log = _LOGGER.warning if outcome.startswith("failed") else _LOGGER.info
        log(
            "clipboard_restore id=%s outcome=%s delay_ms=%s",
            record.transaction_id,
            outcome,
            int(max(0.0, self._clock() - record.armed_at) * 1000),
        )

    def flush_pending_restore(self) -> None:
        """Run a deferred clipboard restore now instead of on its timer.

        Called at shutdown: the scheduler's thread is a daemon and is killed
        at exit, so without this the user's clipboard keeps the last
        transcript for good -- and quitting right after a dictation is the
        ordinary way to end one.

        The readiness wait is skipped, because there is no later attempt to
        defer to. The content check is not: a clipboard the user has filled
        since must still be left alone.
        """
        with self._insert_lock:
            record = self._pending_restore
            if record is None:
                return
            self._cancel_scheduled_restore(record)
            # No retry: the timer thread dies with the process.
            self._finish_restore(record, may_retry=False)

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
                "Clipboard could not be verified; left the current clipboard untouched."
            ) from exc

    def _clipboard_changed_after_set(
        self,
        marker: int | None,
        expected_text: str,
    ) -> bool:
        """Is the clipboard still holding what this app last wrote?

        The content is the evidence and the sequence number is only a fallback
        for a backend that cannot read text back. An equal counter used to end
        the check on its own, with no comparison at all -- and that is exactly
        the state a foreign write produces when it lands between two reads of
        the counter, or inside the microseconds between our `CloseClipboard`
        and the marker read. Measured against the previous implementation: an
        equal number over a stranger's text was reported as "unchanged", so
        the transaction pasted the stranger's text and then restored over it.

        Comparing content costs one clipboard read that the equal-number case
        used to skip; `_clipboard_text` raises `ClipboardContentionError` when
        that read fails, which every caller treats as "not ours to write".
        """
        current_text = self._clipboard_text()
        if current_text is not _UNAVAILABLE_CLIPBOARD_TEXT:
            return current_text != expected_text
        # No way to read the content back at all. The counter is all there is,
        # and with no counter either there is no evidence of a change.
        current_marker = self._clipboard_sequence_number()
        if marker is not None and current_marker is not None:
            return current_marker != marker
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
    return INPUT(
        type=INPUT_KEYBOARD, union=_INPUTUNION(ki=KEYBDINPUT(vk, 0, flags, 0, 0))
    )


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
