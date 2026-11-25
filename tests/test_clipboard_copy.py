"""The one copy function, and the rule that nothing else reaches the Qt clipboard.

`QClipboard.setText` called from Python froze the app for 5.0 s while a pasted
transcript was announced on the clipboard (`stt_app.clipboard_copy` says
why). The scan below is what keeps a new Copy button from bringing that back.
"""

from __future__ import annotations

import ast
from pathlib import Path

import stt_app
from stt_app import clipboard_copy

PACKAGE_ROOT = Path(stt_app.__file__).parent


def qt_clipboard_uses(root: Path) -> list[str]:
    """`file:line` of every Python-level use of Qt's clipboard under `root`."""
    uses = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.alias):
                name = node.name
            else:
                continue
            if name in {"clipboard", "QClipboard"}:
                line = getattr(node, "lineno", 0)
                uses.append(f"{path.relative_to(root).as_posix()}:{line}")
    return uses


def test_no_module_of_the_package_uses_the_qt_clipboard():
    assert qt_clipboard_uses(PACKAGE_ROOT) == []


def test_the_scan_finds_a_qt_clipboard_call_and_ignores_a_docstring(tmp_path):
    (tmp_path / "copy_button.py").write_text(
        '"""QGuiApplication.clipboard() is only mentioned here."""\n'
        "from PySide6 import QtGui\n"
        "\n"
        "\n"
        "def copy(text):\n"
        "    QtGui.QGuiApplication.clipboard().setText(text)\n",
        encoding="utf-8",
    )
    (tmp_path / "typed.py").write_text(
        "from PySide6.QtGui import QClipboard\n", encoding="utf-8"
    )

    assert qt_clipboard_uses(tmp_path) == ["copy_button.py:6", "typed.py:1"]


def test_copy_text_hands_the_text_to_the_installed_writer(monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(clipboard_copy, "_writer", None)
    clipboard_copy.set_text_writer(written.append)

    assert clipboard_copy.copy_text("hello") is True
    assert written == ["hello"]


def test_copy_text_reports_a_clipboard_that_could_not_be_written(monkeypatch):
    def refuse(_text: str) -> None:
        raise OSError("clipboard is held by another program")

    monkeypatch.setattr(clipboard_copy, "_writer", refuse)

    assert clipboard_copy.copy_text("hello") is False


def test_copy_text_without_a_writer_copies_nothing(monkeypatch):
    monkeypatch.setattr(clipboard_copy, "_writer", None)

    assert clipboard_copy.copy_text("hello") is False
