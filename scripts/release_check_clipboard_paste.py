"""Exercise the clipboard backend and the paste transaction on the real desktop.

WHAT IT CHECKS
Five things, in this order:

  A. capture -> set transcript -> restore on a real clipboard holding text,
     HTML, a DIB image, a file list and "Preferred DropEffect": every format
     must come back byte for byte, and only the transcript may be on the
     clipboard while the paste is in flight.
  B. the same round trip followed by Explorer's own paste through Shell COM,
     for a CUT file (which must still move) and a COPIED file (which must
     still copy). This is what proves the restored clipboard is not merely
     equal byte for byte but still means the same thing to the shell.
  C. `TextInserter` on the WM_PASTE road into a real EDIT window.
  D. `TextInserter` on the SendInput road into a real foreground EDIT window,
     including the deferred clipboard restore 1.5 s after the keystroke.
  E. the foreground re-check: the foreground moves to a second probe window
     between the transaction opening and the keystroke, and the paste must be
     refused with the clipboard put back rather than typed into the wrong
     window.

WHY A FAKE CANNOT REPLACE IT
Every clipboard test in the repository drives `Win32ClipboardBackend` against
a fake `win32clipboard` / `user32` / `kernel32`. Those fakes prove the code's
own arithmetic -- the copy, the sizes, the lock pairing -- and they cannot
prove what Windows does with it: whether the shell still treats a restored
CF_HDROP plus DropEffect as a pending cut, whether Windows synthesizes
CF_BITMAP again from a restored CF_DIB, or whether an injected Ctrl+V reaches
the window that had the focus. Only the real desktop answers those.

WHAT IT TOUCHES
* The clipboard. The user's own clipboard is captured before anything else
  and put back in a `finally`, and the run ends with a check that it came
  back. Its CONTENT is never printed or written to the report -- only format
  ids, format names and sizes.
* The foreground, for parts D and E only. Two small EDIT windows appear, take
  the focus for a few seconds and close again. Because that steals the
  keyboard, those two parts wait until the user has been idle for 12 seconds
  (up to 10 minutes) and are skipped entirely with `--skip-focus`. Parts A to
  C never take the focus.
* A throwaway folder under %TEMP% for the files Explorer moves and copies.
  `APPDATA` and `LOCALAPPDATA` point there for the whole run, so the real
  `%APPDATA%\\stt_app` is unreachable. No network.

HOW LONG IT TAKES
About 15 seconds with `--skip-focus`; about 25 seconds plus the idle wait
without it.

COMMAND LINE
    .venv\\Scripts\\python.exe scripts\\release_check_clipboard_paste.py
    .venv\\Scripts\\python.exe scripts\\release_check_clipboard_paste.py \\
        --skip-focus --report out.json

Needs Windows and pywin32.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import struct
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_check_common as common

if sys.platform != "win32":
    sys.stdout.write("PREREQUISITE this check needs the real Windows clipboard\n")
    raise SystemExit(common.EXIT_PREREQUISITE)

SANDBOX = common.make_sandbox("stt_release_clipboard_")
common.redirect_this_process_to(SANDBOX)
common.add_src_to_sys_path()

try:
    import pythoncom
    import pywintypes
    import win32clipboard
    import win32com.client
except ImportError as _exc:
    sys.stdout.write(f"PREREQUISITE pywin32 is not importable: {_exc}\n")
    raise SystemExit(common.EXIT_PREREQUISITE) from None

from stt_app.text_inserter import (  # noqa: E402
    TextInserter,
    TextInsertionError,
    Win32ClipboardBackend,
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetTickCount.restype = wintypes.DWORD


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]

WS_OVERLAPPEDWINDOW = 0x00CF0000
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
SW_SHOWNOACTIVATE = 4
WM_QUIT = 0x0012
CF_UNICODETEXT = 13
CF_DIB = 8
CF_BITMAP = 2
CF_HDROP = 15
DROPEFFECT_COPY_LINK = 5
DROPEFFECT_MOVE = 2

# Seconds the user has to have been idle before the two focus-taking parts
# run, and how long the script is willing to wait for that.
IDLE_REQUIRED_S = 12.0
IDLE_WAIT_LIMIT_S = 600.0

# German dictation is the real case, so umlauts are the payload -- built at
# runtime because no string a script prints may contain a character a
# redirected Windows console cannot encode.
UMLAUTS = "".join(chr(code) for code in (0xE4, 0xF6, 0xFC))
SHARP_S = chr(0xDF)
PREVIOUS_TEXT = f"the user's previous clipboard {UMLAUTS}"

SEND_INPUT_CHECKS = (
    "send_input.transcript_lands_exactly_once",
    "send_input.the_clipboard_still_holds_the_transcript_at_return",
    "send_input.the_deferred_restore_puts_the_users_clipboard_back",
)
FOREGROUND_GUARD_CHECKS = (
    "foreground_guard.the_paste_is_refused",
    "foreground_guard.the_clipboard_is_restored_at_once",
)
FOCUS_CHECKS = (*SEND_INPUT_CHECKS, *FOREGROUND_GUARD_CHECKS)

LOG_LINES: list[str] = []


class _Collector(logging.Handler):
    """Keep the app's own paste/clipboard log lines for the report."""

    def emit(self, record: logging.LogRecord) -> None:
        LOG_LINES.append(f"{record.levelname} {record.getMessage()}")


_stt_logger = logging.getLogger("stt_app")
_stt_logger.setLevel(logging.DEBUG)
_stt_logger.addHandler(_Collector())


def idle_seconds() -> float:
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def _open_clipboard() -> None:
    last: Exception | None = None
    for _ in range(50):
        try:
            win32clipboard.OpenClipboard(0)
            return
        except pywintypes.error as exc:
            last = exc
            time.sleep(0.02)
    raise RuntimeError(f"clipboard stayed busy: {last}")


def put_clipboard(entries: list[tuple[int, object]]) -> None:
    _open_clipboard()
    try:
        win32clipboard.EmptyClipboard()
        for format_id, payload in entries:
            win32clipboard.SetClipboardData(format_id, payload)
    finally:
        win32clipboard.CloseClipboard()


def read_clipboard(format_ids: list[int]) -> dict[int, object]:
    """Read through pywin32, never through the app's own ctypes backend.

    An independent reader is the point: a backend that captured and restored
    its own mistake consistently would pass a check written against itself.
    """
    result: dict[int, object] = {}
    _open_clipboard()
    try:
        for format_id in format_ids:
            if not win32clipboard.IsClipboardFormatAvailable(format_id):
                result[format_id] = None
                continue
            result[format_id] = win32clipboard.GetClipboardData(format_id)
    finally:
        win32clipboard.CloseClipboard()
    return result


def same_bytes(original: object, got: object) -> bool:
    """A global block may come back rounded up; the payload must be a prefix."""
    if isinstance(original, bytes) and isinstance(got, bytes):
        return got[: len(original)] == original and not any(got[len(original) :])
    return original == got


def dropfiles(paths: list[Path]) -> bytes:
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    body = "".join(f"{path}\0" for path in paths) + "\0"
    return header + body.encode("utf-16-le")


def tiny_dib() -> bytes:
    header = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 32, 0, 16, 2835, 2835, 0, 0)
    return header + bytes(range(16))


HTML_PAYLOAD = (
    b"Version:0.9\r\nStartHTML:00000097\r\nEndHTML:00000170\r\n"
    b"StartFragment:00000131\r\nEndFragment:00000134\r\n"
    b"<html><body><!--StartFragment--><b>x</b><!--EndFragment--></body></html>\0"
)


def describe_state(state) -> list[dict[str, object]]:
    """Format ids, names and sizes only -- never the content behind them."""
    return [
        {"id": format_id, "name": name, "bytes": len(payload)}
        for format_id, name, payload in state.formats
    ]


def part_a(checks: common.Checks, backend: Win32ClipboardBackend) -> dict:
    """Capture, overwrite with a transcript, restore -- five formats."""
    html_format = win32clipboard.RegisterClipboardFormat("HTML Format")
    drop_effect = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
    names = {
        CF_UNICODETEXT: "CF_UNICODETEXT",
        html_format: "HTML_Format",
        CF_DIB: "CF_DIB",
        CF_HDROP: "CF_HDROP",
        drop_effect: "Preferred_DropEffect",
    }
    sample_file = SANDBOX / "listed file.txt"
    sample_file.write_text("x", encoding="utf-8")
    original: dict[int, object] = {
        CF_UNICODETEXT: f"previous clipboard text {UMLAUTS}",
        html_format: HTML_PAYLOAD,
        CF_DIB: tiny_dib(),
        CF_HDROP: dropfiles([sample_file]),
        drop_effect: struct.pack("<I", DROPEFFECT_MOVE),
    }
    put_clipboard(list(original.items()))
    before = read_clipboard(list(original))
    state = backend.capture_clipboard_state()
    transcript = f"stt_app probe transcript {UMLAUTS} {SHARP_S}"
    marker = backend.set_clipboard_text(transcript)
    during = read_clipboard([CF_UNICODETEXT, html_format, CF_DIB, CF_HDROP])
    backend.restore_clipboard_state(state)
    after = read_clipboard(list(original))
    _open_clipboard()
    try:
        bitmap_again = bool(win32clipboard.IsClipboardFormatAvailable(CF_BITMAP))
    finally:
        win32clipboard.CloseClipboard()

    captured = describe_state(state)
    captured_ids = {int(item["id"]) for item in captured}
    missing = [label for fid, label in names.items() if fid not in captured_ids]
    checks.verdict(
        "clipboard.capture_reads_every_planted_format",
        not missing,
        f"{len(captured)} formats captured, missing: {missing or 'none'}",
    )
    checks.verdict(
        "clipboard.only_the_transcript_is_there_during_the_paste",
        during[CF_UNICODETEXT] == transcript
        and all(during[key] is None for key in (html_format, CF_DIB, CF_HDROP)),
        f"marker={isinstance(getattr(marker, 'sequence', None), int)} "
        f"other_formats_gone="
        f"{all(during[key] is None for key in (html_format, CF_DIB, CF_HDROP))}",
    )
    for format_id, label in names.items():
        checks.verdict(
            f"clipboard.restores.{label}",
            before[format_id] is not None and before[format_id] == after[format_id],
            "identical to the independent read taken before"
            if before[format_id] == after[format_id]
            else "differs from the independent read taken before",
        )
    checks.verdict(
        "clipboard.cf_bitmap_is_synthesized_again_from_the_restored_dib",
        bitmap_again,
        f"CF_BITMAP available again: {bitmap_again}",
    )
    return {
        "captured_formats": captured,
        "hdrop_paths_after": [str(item) for item in (after[CF_HDROP] or ())],
        "drop_effect_after": (
            struct.unpack("<I", after[drop_effect][:4])[0]
            if isinstance(after[drop_effect], bytes)
            else None
        ),
        "cf_bitmap_synthesized_again": bitmap_again,
    }


def shell_paste_into(folder: Path, expected: Path, timeout_s: float = 15.0) -> bool:
    shell = win32com.client.Dispatch("Shell.Application")
    namespace = shell.NameSpace(str(folder))
    if namespace is None:
        raise RuntimeError(f"the shell cannot open {folder}")
    namespace.Self.InvokeVerb("paste")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pythoncom.PumpWaitingMessages()
        if expected.exists():
            time.sleep(0.3)
            pythoncom.PumpWaitingMessages()
            return True
        time.sleep(0.05)
    return False


def part_b(checks: common.Checks, backend: Win32ClipboardBackend) -> dict:
    """Explorer's own paste, before and after a transcript round trip."""
    pythoncom.CoInitialize()
    drop_effect = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
    results: dict[str, dict] = {}
    cases = (
        ("cut_direct_control", DROPEFFECT_MOVE, False, "moves"),
        ("cut_after_a_round_trip", DROPEFFECT_MOVE, True, "moves"),
        ("copy_after_a_round_trip", DROPEFFECT_COPY_LINK, True, "copies"),
    )
    for label, effect, round_trip, wanted in cases:
        source_dir = SANDBOX / f"{label}_src"
        target_dir = SANDBOX / f"{label}_dst"
        source_dir.mkdir()
        target_dir.mkdir()
        source = source_dir / "probe file.txt"
        source.write_text(f"payload of {label}", encoding="utf-8")
        put_clipboard(
            [
                (CF_HDROP, dropfiles([source])),
                (drop_effect, struct.pack("<I", effect)),
            ]
        )
        if round_trip:
            state = backend.capture_clipboard_state()
            backend.set_clipboard_text("a transcript in between")
            backend.restore_clipboard_state(state)
        expected = target_dir / source.name
        try:
            arrived = shell_paste_into(target_dir, expected)
        except Exception as exc:
            results[label] = {"error": f"{type(exc).__name__}: {exc}"}
            checks.crashed(f"explorer.{label}_{wanted}_the_file", exc)
            continue
        still_there = source.exists()
        content_ok = arrived and expected.read_text(encoding="utf-8") == (
            f"payload of {label}"
        )
        results[label] = {
            "arrived_in_target": arrived,
            "still_in_source": still_there,
            "content_ok": content_ok,
        }
        wanted_source = wanted == "copies"
        checks.verdict(
            f"explorer.{label}_{wanted}_the_file",
            arrived and content_ok and still_there is wanted_source,
            f"arrived={arrived} still_in_source={still_there} content_ok={content_ok}",
        )
    return results


class ProbeWindows:
    """Two real EDIT windows on their own thread with their own message loop."""

    def __init__(self) -> None:
        self.first = 0
        self.second = 0
        self.thread_id = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("the probe windows did not come up")

    def _create(self, title: str, left: int) -> int:
        hwnd = user32.CreateWindowExW(
            0,
            "EDIT",
            title,
            WS_OVERLAPPEDWINDOW | ES_MULTILINE | ES_AUTOVSCROLL,
            left,
            120,
            460,
            140,
            None,
            None,
            kernel32.GetModuleHandleW(None),
            None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        user32.SetWindowTextW(hwnd, "")
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        return int(hwnd)

    def _run(self) -> None:
        self.thread_id = int(kernel32.GetCurrentThreadId())
        self.first = self._create("stt_app paste probe 1 (closes by itself)", 120)
        self.second = self._create("stt_app paste probe 2 (closes by itself)", 620)
        self._ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        user32.DestroyWindow(self.first)
        user32.DestroyWindow(self.second)

    def stop(self) -> None:
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
            self._thread.join(5)


def window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def wait_for_text(hwnd: int, expected: str, timeout_s: float) -> tuple[bool, float]:
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        if window_text(hwnd) == expected:
            return True, time.monotonic() - started
        time.sleep(0.02)
    return False, time.monotonic() - started


def take_foreground(hwnd: int) -> bool:
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return True
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return True
    foreground = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    if fg_thread and target_thread and fg_thread != target_thread:
        user32.AttachThreadInput(target_thread, fg_thread, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(target_thread, fg_thread, False)
    time.sleep(0.1)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def previous_clipboard() -> None:
    html_format = win32clipboard.RegisterClipboardFormat("HTML Format")
    put_clipboard([(CF_UNICODETEXT, PREVIOUS_TEXT), (html_format, HTML_PAYLOAD)])


def clipboard_is_previous() -> dict[str, bool]:
    html_format = win32clipboard.RegisterClipboardFormat("HTML Format")
    got = read_clipboard([CF_UNICODETEXT, html_format])
    return {
        "text_restored": got[CF_UNICODETEXT] == PREVIOUS_TEXT,
        "html_restored": same_bytes(HTML_PAYLOAD, got[html_format]),
    }


def part_c(checks: common.Checks, windows: ProbeWindows) -> dict:
    """The WM_PASTE road: synchronous, and it needs no foreground window."""
    inserter = TextInserter()
    previous_clipboard()
    transcript = f"wm_paste road {UMLAUTS} {SHARP_S} 123"
    user32.SetWindowTextW(windows.first, "")
    started = time.monotonic()
    try:
        returned = inserter.insert_text_with_options(
            transcript,
            target_hwnd=windows.first,
            paste_mode="wm_paste",
            restore_clipboard=True,
        )
        error = ""
    except TextInsertionError as exc:
        returned, error = False, f"{type(exc).__name__}: {exc}"
    call_seconds = round(time.monotonic() - started, 3)
    landed, _ = wait_for_text(windows.first, transcript, 2.0)
    restored = clipboard_is_previous()
    inserter.flush_pending_restore()
    checks.verdict(
        "wm_paste.transcript_lands_exactly_once",
        returned and landed,
        f"returned={returned} landed={landed} call={call_seconds}s "
        f"chars_in_window={len(window_text(windows.first))} error={error[:120]}",
    )
    checks.verdict(
        "wm_paste.the_clipboard_is_back_when_the_call_returns",
        all(restored.values()),
        f"text_restored={restored['text_restored']} "
        f"html_restored={restored['html_restored']}",
    )
    return {
        "returned": returned,
        "error": error,
        "call_seconds": call_seconds,
        **restored,
    }


def part_d(checks: common.Checks, windows: ProbeWindows) -> dict:
    """The SendInput road, including the restore deferred past the keystroke."""
    inserter = TextInserter()
    previous_clipboard()
    transcript = f"send_input road {UMLAUTS} {SHARP_S} 456"
    user32.SetWindowTextW(windows.first, "")
    if not take_foreground(windows.first):
        reason = "Windows refused the foreground, so the SendInput road did not run"
        for name in SEND_INPUT_CHECKS:
            checks.skipped(name, reason)
        return {"foreground_obtained": False}

    started = time.monotonic()
    try:
        returned = inserter.insert_text_with_options(
            transcript,
            target_hwnd=windows.first,
            paste_mode="send_input",
            restore_clipboard=True,
        )
        error = ""
    except TextInsertionError as exc:
        returned, error = False, f"{type(exc).__name__}: {exc}"
    call_seconds = round(time.monotonic() - started, 3)
    landed, landed_after = wait_for_text(windows.first, transcript, 3.0)
    at_return = read_clipboard([CF_UNICODETEXT])[CF_UNICODETEXT]
    still_transcript = at_return == transcript
    checks.verdict(
        "send_input.transcript_lands_exactly_once",
        returned and landed,
        f"returned={returned} landed={landed} after={round(landed_after, 3)}s "
        f"call={call_seconds}s error={error[:120]}",
    )
    checks.verdict(
        "send_input.the_clipboard_still_holds_the_transcript_at_return",
        still_transcript,
        "the restore is deferred past the call, as it must be"
        if still_transcript
        else "the clipboard was already restored when the call returned",
    )
    time.sleep(2.6)
    restored = clipboard_is_previous()
    after_text = window_text(windows.first)
    checks.verdict(
        "send_input.the_deferred_restore_puts_the_users_clipboard_back",
        all(restored.values()) and after_text == transcript,
        f"text_restored={restored['text_restored']} "
        f"html_restored={restored['html_restored']} "
        f"window_unchanged={after_text == transcript}",
    )
    return {
        "foreground_obtained": True,
        "returned": returned,
        "error": error,
        "call_seconds": call_seconds,
        "landed_after_seconds": round(landed_after, 3),
        "clipboard_still_transcript_right_after": still_transcript,
        **restored,
    }


def part_e(checks: common.Checks, windows: ProbeWindows) -> dict:
    """The foreground moves to the second window while the transaction settles."""
    names = FOREGROUND_GUARD_CHECKS
    previous_clipboard()
    user32.SetWindowTextW(windows.first, "")
    user32.SetWindowTextW(windows.second, "")
    if not take_foreground(windows.first):
        for name in names:
            checks.skipped(name, "Windows refused the foreground")
        return {"foreground_obtained": False}
    switched = {"done": False, "obtained": False}

    def sleeping_switch(seconds: float) -> None:
        """Move the foreground away inside the transaction's own settle sleep."""
        if not switched["done"]:
            switched["done"] = True
            switched["obtained"] = take_foreground(windows.second)
        time.sleep(seconds)

    inserter = TextInserter(sleep_fn=sleeping_switch)
    transcript = "must not land anywhere 789"
    try:
        returned = inserter.insert_text_with_options(
            transcript,
            target_hwnd=windows.first,
            paste_mode="send_input",
            restore_clipboard=True,
        )
        error = ""
    except TextInsertionError as exc:
        returned, error = False, f"{type(exc).__name__}: {exc}"
    time.sleep(0.6)
    first_text = window_text(windows.first)
    second_text = window_text(windows.second)
    restored = clipboard_is_previous()
    inserter.flush_pending_restore()
    if not switched["obtained"]:
        for name in names:
            checks.skipped(name, "the foreground switch itself was refused")
        return {"foreground_obtained": True, "switch_obtained": False}
    checks.verdict(
        names[0],
        not returned and bool(error) and not first_text and not second_text,
        f"returned={returned} first_window_chars={len(first_text)} "
        f"second_window_chars={len(second_text)} error={error[:160]}",
    )
    checks.verdict(
        names[1],
        restored["text_restored"],
        f"text_restored={restored['text_restored']} "
        f"html_restored={restored['html_restored']}",
    )
    return {
        "foreground_obtained": True,
        "switch_obtained": True,
        "returned": returned,
        "error": error[:300],
        "first_window_chars": len(first_text),
        "second_window_chars": len(second_text),
        **restored,
    }


def wait_until_idle(checks: common.Checks) -> bool:
    waited = 0.0
    while idle_seconds() < IDLE_REQUIRED_S and waited < IDLE_WAIT_LIMIT_S:
        if waited == 0.0:
            sys.stdout.write(
                f"waiting for {IDLE_REQUIRED_S:.0f} s of idle before the two "
                f"focus-taking parts (up to {IDLE_WAIT_LIMIT_S:.0f} s)\n"
            )
            sys.stdout.flush()
        time.sleep(1.0)
        waited += 1.0
    checks.details["waited_for_idle_seconds"] = waited
    return idle_seconds() >= IDLE_REQUIRED_S


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--skip-focus",
        action="store_true",
        help=(
            "Leave out parts D and E, the only two that take the keyboard "
            "focus. Everything else still runs."
        ),
    )
    common.add_report_argument(parser)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    checks = common.Checks("clipboard and paste on the real desktop")
    checks.details["sandbox"] = str(SANDBOX)
    sys.stdout.write(f"sandbox: {SANDBOX}\n")
    parts: dict[str, object] = {}
    checks.details["parts"] = parts

    backend = Win32ClipboardBackend()
    user_state = backend.capture_clipboard_state()
    checks.details["user_clipboard_formats_before"] = describe_state(user_state)
    checks.details["user_clipboard_had_text"] = bool(user_state.has_text)
    windows = ProbeWindows()
    previous_foreground = int(user32.GetForegroundWindow() or 0)
    try:
        for label, runner in (("A_round_trip", part_a), ("B_explorer_paste", part_b)):
            try:
                parts[label] = runner(checks, backend)
            except Exception as exc:
                parts[label] = {"crash": f"{type(exc).__name__}: {exc}"}
                checks.crashed(f"clipboard.{label}", exc)
        windows.start()
        try:
            parts["C_wm_paste"] = part_c(checks, windows)
        except Exception as exc:
            parts["C_wm_paste"] = {"crash": f"{type(exc).__name__}: {exc}"}
            checks.crashed("wm_paste.part_crashed", exc)
        if args.skip_focus:
            for name in FOCUS_CHECKS:
                checks.skipped(name, "--skip-focus was given")
        elif not wait_until_idle(checks):
            for name in FOCUS_CHECKS:
                checks.skipped(name, "the user never paused long enough")
        else:
            previous_foreground = int(user32.GetForegroundWindow() or 0)
            for label, runner in (
                ("D_send_input", part_d),
                ("E_foreground_guard", part_e),
            ):
                try:
                    parts[label] = runner(checks, windows)
                except Exception as exc:
                    parts[label] = {"crash": f"{type(exc).__name__}: {exc}"}
                    checks.crashed(f"clipboard.{label}", exc)
    except Exception as exc:  # the user's clipboard still has to come back
        checks.crashed("clipboard.probe_crashed", exc)
    finally:
        windows.stop()
        try:
            backend.restore_clipboard_state(user_state)
            again = backend.capture_clipboard_state()
            checks.details["user_clipboard_formats_after"] = describe_state(again)
            before_ids = [
                (item["id"], item["bytes"]) for item in describe_state(user_state)
            ]
            after_ids = [(item["id"], item["bytes"]) for item in describe_state(again)]
            checks.verdict(
                "user_clipboard.is_back_exactly_as_it_was",
                before_ids == after_ids,
                f"{len(before_ids)} formats before, {len(after_ids)} after",
            )
        except Exception as exc:
            checks.crashed("user_clipboard.is_back_exactly_as_it_was", exc)
        if previous_foreground and user32.IsWindow(previous_foreground):
            take_foreground(previous_foreground)
        checks.details["log_lines"] = [
            common.ascii_safe(line)
            for line in LOG_LINES
            if "paste_transaction" in line or "clipboard_" in line
        ]
    return checks.finish(args.report)


if __name__ == "__main__":
    common.run_main(main)
