"""Time a clipboard write made while the paste owner window holds the clipboard.

WHAT IT SHOWS
While a pasted transcript is announced on the clipboard (`clipboard_owner`),
the next writer's `EmptyClipboard` sends the owner window a message, and that
window's procedure is a Python callback on another thread. Two writers are
timed, each right after a fresh announcement:

  qt    `QGuiApplication.clipboard().setText(...)` on the Qt thread. PySide
        keeps the interpreter lock for the whole call, the owner thread
        cannot answer, and Windows gives up after five seconds.
  app   `TextInserter.copy_text(...)`, the writer behind every Copy action of
        the app (`clipboard_copy`). Its Win32 calls release the lock.

Measured on 2026-11-25 (Windows 11, PySide6 6.11): see
`docs/agents/text-insertion.md`. `--skip-qt` leaves the slow writer out.

WHAT IT TOUCHES
The clipboard, for about seven seconds: it is captured first and put back in
a `finally`, and the last line says whether it came back format for format.
Its content is never printed. No window takes the focus and no key is sent;
the run still waits for 12 s without user input (at most 60 s, then it exits
with code 75) so that a dictation in progress does not meet a busy clipboard.
`APPDATA` and `LOCALAPPDATA` point at a throwaway folder.

COMMAND LINE
    .venv\\Scripts\\python.exe scripts\\diagnose_clipboard_copy_stall.py
    .venv\\Scripts\\python.exe scripts\\diagnose_clipboard_copy_stall.py --skip-qt

Needs Windows, pywin32 and PySide6.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The release check owns the sandbox redirect, the idle clock and the
# content-free clipboard description; importing it performs the redirect.
import release_check_clipboard_paste as desktop
from PySide6 import QtGui, QtWidgets

from stt_app.clipboard_owner import ClipboardOwner
from stt_app.text_inserter import (
    CLIPBOARD_HISTORY_EXCLUSION_FORMAT_NAMES,
    TextInserter,
    Win32ClipboardBackend,
)

EXIT_USER_ACTIVE = 75
IDLE_WAIT_LIMIT_S = 60.0


def _announce(backend: Win32ClipboardBackend, text: str) -> None:
    marker = backend.offer_clipboard_text(text, None)
    if marker.offer is None:
        raise RuntimeError("the owner window did not take the clipboard")


def _timed_ms(write) -> float:
    started = time.perf_counter()
    write()
    return (time.perf_counter() - started) * 1000.0


def _formats(state) -> list[tuple[object, object]]:
    return [
        (item["id"], item["bytes"])
        for item in desktop.describe_state(state)
        if item["name"] not in CLIPBOARD_HISTORY_EXCLUSION_FORMAT_NAMES
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--skip-qt", action="store_true", help="skip the slow writer")
    args = parser.parse_args()

    waited = 0.0
    while desktop.idle_seconds() < desktop.IDLE_REQUIRED_S:
        if waited >= IDLE_WAIT_LIMIT_S:
            sys.stdout.write("the user never paused long enough; nothing was touched\n")
            return EXIT_USER_ACTIVE
        time.sleep(1.0)
        waited += 1.0

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    backend = Win32ClipboardBackend(owner=ClipboardOwner())
    inserter = TextInserter(backend=backend)
    user_state = backend.capture_clipboard_state()
    try:
        if not args.skip_qt:
            _announce(backend, "announced for the qt writer")
            qt_ms = _timed_ms(
                lambda: QtGui.QGuiApplication.clipboard().setText("qt writer")
            )
            sys.stdout.write(f"qt   writer during a live offer: {qt_ms:8.1f} ms\n")
        _announce(backend, "announced for the app writer")
        app_ms = _timed_ms(lambda: inserter.copy_text("app writer"))
        sys.stdout.write(f"app  writer during a live offer: {app_ms:8.1f} ms\n")
        plain_ms = _timed_ms(lambda: inserter.copy_text("app writer, no offer"))
        sys.stdout.write(f"app  writer without an offer:    {plain_ms:8.1f} ms\n")
    finally:
        backend.restore_clipboard_state(user_state)
        back = _formats(backend.capture_clipboard_state()) == _formats(user_state)
        inserter.close()
        sys.stdout.write(f"user clipboard back as it was: {back}\n")
    del app
    return 0 if back else 1


if __name__ == "__main__":
    raise SystemExit(main())
