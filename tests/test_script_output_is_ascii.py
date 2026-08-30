"""Every string a script can print must be ASCII.

Fixing this one glyph at a time is the wrong shape, and doing it that way is
how it kept coming back:

* `import_model.py` printed U+2713/U+2717 on the `--validate-only` verdict.
  `sys.stdout` becomes cp1252 the moment output is redirected on Windows, and
  neither glyph exists there, so a *complete, valid* model crashed the script
  with `UnicodeEncodeError` and exit 1.
* Fixing those two left three em dashes in the same file. An em dash *is* in
  cp1252 (0x97), so nothing crashed -- it just wrote a byte that renders as
  U+FFFD in a UTF-8 editor, which is where a captured log gets opened.
* `download_model.py` drew its SSL-error box with 63 U+2550 characters. That
  one writes to stderr, whose error handler is `backslashreplace` rather than
  strict, so it did not crash either: it printed two 63-character walls of
  literal `\\u2550` escapes around the corporate-proxy guidance -- the text a
  user behind Zscaler pastes into a ticket.

Three files, three different symptoms, one cause. So the assertion is on the
cause: no non-ASCII in any string literal a script evaluates.

Docstrings are exempt because they are not printed -- *except* a module
docstring in a script that reads `__doc__`, which is then on its way to
stdout. Checking only for `ArgumentParser(description=__doc__)` was too
narrow and missed `print(__doc__)`, which is how four em dashes stayed in
`experiment_native_tray_icon.py`'s banner. Any load of the name is now
taken as printable: it subsumes the argparse case, cannot produce a false
negative, and the cost of a false positive is only that one docstring has
to stay ASCII. Comments are invisible to `ast` and are exempt for free;
they are the right place for a real em dash.
"""

from __future__ import annotations

import ast
import re
import subprocess
import unicodedata
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((PROJECT_ROOT / "scripts").glob("*.py"))


_DOCSTRING_OWNERS = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


def _reads_the_module_docstring(tree: ast.Module) -> bool:
    """Any read of `__doc__` puts the module docstring on its way to stdout.

    `ArgumentParser(description=__doc__)` puts it on `--help`, and a bare
    `print(__doc__)` puts it there directly. Matching the call shape caught
    only the first, so `experiment_native_tray_icon.py` kept four em dashes
    in the banner it prints on every run.
    """
    return any(
        isinstance(node, ast.Name)
        and node.id == "__doc__"
        and isinstance(node.ctx, ast.Load)
        for node in ast.walk(tree)
    )


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Docstrings this file can never print.

    The module docstring is only in that set when nothing reads `__doc__`;
    a nested docstring has no route to stdout either way.
    """
    printable_module_doc = _reads_the_module_docstring(tree)
    exempt = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS) or not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        if printable_module_doc and isinstance(node, ast.Module):
            continue
        exempt.add(id(first.value))
    return exempt


def test_the_scan_actually_found_the_scripts():
    """A glob that matches nothing would make every test below vacuous."""
    assert len(SCRIPTS) >= 10, [path.name for path in SCRIPTS]


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_no_script_prints_a_character_a_captured_log_cannot_show(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_ids(tree)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        bad = sorted({character for character in node.value if ord(character) > 127})
        if not bad:
            continue
        codepoints = ", ".join(f"U+{ord(character):04X}" for character in bad)
        try:
            "".join(bad).encode("cp1252")
        except UnicodeEncodeError:
            effect = "raises UnicodeEncodeError on a redirected stdout"
        else:
            effect = "renders as U+FFFD when the log is read as UTF-8"
        offenders.append(f"  line {node.lineno}: {codepoints} -- {effect}")

    assert not offenders, (
        f"{path.name} has non-ASCII in a string it can print:\n" + "\n".join(offenders)
    )


_NON_LATIN_SCRIPTS = re.compile(
    "["
    "\u0370-\u03ff"  # Greek
    "\u0400-\u04ff"  # Cyrillic
    "\u0590-\u05ff"  # Hebrew
    "\u0600-\u06ff"  # Arabic
    "\u3040-\u30ff"  # Kana
    "\u4e00-\u9fff"  # CJK
    "\uac00-\ud7af"  # Hangul
    "]"
)


def test_no_tracked_file_contains_a_word_from_another_writing_system():
    """`AGENTS.md`: all project content is English. This catches the slips.

    Not a style rule -- it is a real failure mode of dictated, multilingual
    work. Twice in one session a word from another alphabet reached the
    repository: a Russian word inside an English code comment, and another in
    a commit message. Both were invisible in review because the surrounding
    sentence read fine.

    Deliberately narrow. It does not forbid non-ASCII: German umlauts and
    accented names are legitimate here, and the em-dash rule above already
    covers the scripts that print. It forbids only whole other writing
    systems, which nothing in this project has any reason to contain, so it
    cannot fire on correct content.

    `stt-dictation-spec.md` is bilingual German/English by exception and is
    still covered -- German uses the Latin alphabet.
    """
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    offenders: list[str] = []
    scanned = 0
    for name in tracked:
        path = PROJECT_ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary, or a path this platform cannot open
        scanned += 1
        for number, line in enumerate(text.splitlines(), 1):
            found = _NON_LATIN_SCRIPTS.search(line)
            if found is None:
                continue
            character = found.group(0)
            offenders.append(
                f"{name}:{number}: {character!r} "
                f"({unicodedata.name(character, 'unnamed')}) in {line.strip()[:70]!r}"
            )

    assert scanned > 100, f"only {scanned} files scanned; the listing looks wrong"
    assert not offenders, "non-Latin script in tracked content:\n" + "\n".join(
        offenders[:20]
    )
