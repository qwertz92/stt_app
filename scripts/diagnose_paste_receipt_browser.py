"""Show the read receipt of real pastes into a Chromium browser page.

WHAT IT SHOWS
The read receipt (`clipboard_owner`) says whether a program fetched a pasted
transcript. In Chrome and Edge something fetches the clipboard text a few
milliseconds after every Ctrl+V, wherever the focus is, so the receipt
cannot tell a paste that landed from one onto a button
(`docs/agents/known-limitations.md`). This script repeats that measurement:
it opens a throwaway `--app` window on a local page and pastes with the
shipped `TextInserter` and the real owner window while the focus is in a
textarea, on a button and on the page body. One line per paste: whether the
textarea shows the text, the verdict, and the receipt's evidence (reason,
reading program, milliseconds after the keystroke) 0.7 s after the keystroke.

WHAT IT TOUCHES
The clipboard (captured first, put back at the end; the last line says
whether it came back format for format; its content is never printed) and
the foreground: the browser window takes the focus for about
`3 * --rounds * 1.5` seconds and receives real Ctrl+V keystrokes. The run
waits for 12 s without user input first (at most 60 s, then it exits with
code 75). The browser runs with a throwaway profile in a temporary folder
and a DevTools port on 127.0.0.1, and its process tree is ended afterwards.

COMMAND LINE
    .venv\\Scripts\\python.exe scripts\\diagnose_paste_receipt_browser.py
    .venv\\Scripts\\python.exe scripts\\diagnose_paste_receipt_browser.py \\
        --browser "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --rounds 5

Needs Windows, pywin32 and Edge (or the browser given with `--browser`).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The release check owns the sandbox redirect, the idle clock, the foreground
# helper, the throwaway browser page and the content-free clipboard
# description.
import release_check_clipboard_paste as desktop

from stt_app.clipboard_owner import ClipboardOwner
from stt_app.text_inserter import (
    CLIPBOARD_HISTORY_EXCLUSION_FORMAT_NAMES,
    TextInserter,
    TextInsertionError,
    Win32ClipboardBackend,
)

EXIT_USER_ACTIVE = 75
IDLE_WAIT_LIMIT_S = 60.0
FOCUS_SCRIPTS = {
    "textarea": "document.getElementById('ta').focus()",
    "button": "document.getElementById('btn').focus()",
    "page body": "document.activeElement.blur()",
}


def _formats(state) -> list[tuple[object, object]]:
    return [
        (item["id"], item["bytes"])
        for item in desktop.describe_state(state)
        if item["name"] not in CLIPBOARD_HISTORY_EXCLUSION_FORMAT_NAMES
    ]


def paste_once(inserter, receipts, page, focus: str, text: str) -> str:
    page.eval("document.getElementById('ta').value = ''")
    page.eval(FOCUS_SCRIPTS[focus])
    time.sleep(0.3)
    if not desktop.take_foreground(page.hwnd):
        return "Windows refused the foreground"
    receipts.clear()
    try:
        inserter.insert_text_with_options(
            text, target_hwnd=page.hwnd, paste_mode="send_input"
        )
    except TextInsertionError as exc:
        return f"refused: {type(exc).__name__}"
    time.sleep(0.7)
    landed = page.eval("document.getElementById('ta').value") == text
    inserter.flush_pending_restore()
    verdict, evidence = receipts[-1] if receipts else ("none", "")
    return f"landed={landed!s:<5} verdict={verdict:<7} {evidence}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--browser", type=Path, help="chrome.exe or msedge.exe")
    parser.add_argument("--rounds", type=int, default=3, help="pastes per focus")
    args = parser.parse_args()
    browser = args.browser or desktop.find_edge()
    if browser is None or not browser.is_file():
        sys.stdout.write("no browser found; pass --browser\n")
        return desktop.common.EXIT_PREREQUISITE

    waited = 0.0
    while desktop.idle_seconds() < desktop.IDLE_REQUIRED_S:
        if waited >= IDLE_WAIT_LIMIT_S:
            sys.stdout.write("the user never paused long enough; nothing was touched\n")
            return EXIT_USER_ACTIVE
        time.sleep(1.0)
        waited += 1.0

    saver = Win32ClipboardBackend()
    user_state = saver.capture_clipboard_state()
    previous_foreground = int(desktop.user32.GetForegroundWindow() or 0)
    inserter = TextInserter(backend=Win32ClipboardBackend(owner=ClipboardOwner()))
    receipts: list[tuple[str, str]] = []
    inserter.set_paste_receipt_handler(
        lambda _id, verdict, evidence: receipts.append((verdict, evidence))
    )
    page = desktop.EdgePage(browser)
    lines: list[str] = []
    try:
        page.start()
        time.sleep(1.0)
        for focus in FOCUS_SCRIPTS:
            for round_index in range(args.rounds):
                outcome = paste_once(
                    inserter, receipts, page, focus, f"receipt probe {round_index}"
                )
                lines.append(f"{focus:<10} {desktop.common.ascii_safe(outcome)}")
    finally:
        inserter.close()
        page.stop()
        saver.restore_clipboard_state(user_state)
        back = _formats(saver.capture_clipboard_state()) == _formats(user_state)
        if previous_foreground and desktop.user32.IsWindow(previous_foreground):
            desktop.take_foreground(previous_foreground)
    sys.stdout.write(f"browser: {browser.name}\n")
    for line in lines:
        sys.stdout.write(line + "\n")
    sys.stdout.write(f"user clipboard back as it was: {back}\n")
    return 0 if back else 1


if __name__ == "__main__":
    raise SystemExit(main())
