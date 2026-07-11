from __future__ import annotations

import pytest

from stt_app.csv_safety import spreadsheet_safe_cell, spreadsheet_safe_mapping


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
