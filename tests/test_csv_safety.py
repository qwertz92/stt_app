from __future__ import annotations

import pytest

from stt_app.csv_safety import (
    export_safe_text,
    spreadsheet_safe_cell,
    spreadsheet_safe_mapping,
)


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_spreadsheet_safe_cell_neutralizes_formula_prefixes(prefix):
    assert spreadsheet_safe_cell(f"  {prefix}payload") == f"'  {prefix}payload"


def test_spreadsheet_safe_cell_preserves_safe_strings_and_non_strings():
    assert spreadsheet_safe_cell("plain text") == "plain text"
    assert spreadsheet_safe_cell(42) == 42
    assert spreadsheet_safe_mapping({"text": "=1+1", "count": 2}) == {
        "text": "'=1+1",
        "count": 2,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\x00b", "a\ufffdb"),
        ("a\x07b", "a\ufffdb"),
        ("a\x0bb", "a\ufffdb"),
        ("a\ud800b", "a\ufffdb"),
        ("a\tb", "a\tb"),
        ("a\nb", "a\nb"),
        ("a\rb", "a\rb"),
        ("Grusse aus \u00d6sterreich", "Grusse aus \u00d6sterreich"),
        ("\U0001f600", "\U0001f600"),
        ("\ufffd", "\ufffd"),
    ],
)
def test_export_safe_text_replaces_only_what_no_export_can_carry(raw, expected):
    """Exactly the characters XML 1.0 forbids, and nothing else.

    Stripping non-ASCII instead would trade an unopenable file for a silently
    mangled German transcript, so the umlaut and the emoji are as load-bearing
    here as the control bytes.
    """
    assert export_safe_text(raw) == expected


def test_export_safe_text_output_always_encodes_as_utf_8():
    """The point of the pass: the encode that follows it cannot raise."""
    hostile = "".join(chr(code) for code in (0x00, 0x07, 0x0B, 0xD800, 0xDFFF, 0xFFFE))
    export_safe_text(hostile).encode("utf-8")
