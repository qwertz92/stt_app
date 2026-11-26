"""The one way this app copies text to the clipboard outside a paste.

Every Copy button, the tray's copy actions and the copy-on-error fallback
call `copy_text`. None of them may use `QClipboard` from Python: PySide keeps
the interpreter lock for the whole of `QClipboard.setText`, and while a
pasted transcript is announced on the clipboard (`clipboard_owner`) that
call's `EmptyClipboard` sends the owner window a message whose procedure is
a Python callback on another thread. That thread cannot get the lock, so the
Qt thread stood still until Windows gave up: 5.0 s, measured three times out
of three (2026-11-25), and a `QClipboard.text()` in the same state did not
return within 40 s. The writer installed here (`TextInserter.copy_text`)
goes through the clipboard backend, whose Win32 calls release the lock, and
settles a paste that still holds the clipboard first.

`tests/test_clipboard_copy.py` fails when a module of this package reaches
for the Qt clipboard again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

# Set once at startup (`main.run`); the test suite installs a recorder
# instead, so no test writes the real clipboard.
_writer: Callable[[str], None] | None = None


def set_text_writer(writer: Callable[[str], None] | None) -> None:
    """Who writes the text: `TextInserter.copy_text` in the running app."""
    global _writer
    _writer = writer


def copy_text(text: str) -> bool:
    """Copy `text` to the clipboard; False when it could not be written.

    Never raises: a Copy button whose clipboard is held by another program
    must leave the dialog standing, and its caller shows "Copied" only for
    True.
    """
    writer = _writer
    if writer is None:
        _LOGGER.warning("clipboard_copy outcome=no_writer chars=%d", len(text))
        return False
    try:
        writer(text)
    except Exception:
        _LOGGER.warning(
            "clipboard_copy outcome=failed chars=%d", len(text), exc_info=True
        )
        return False
    return True
