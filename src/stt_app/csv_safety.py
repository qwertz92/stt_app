from __future__ import annotations

from typing import Any, Mapping

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_safe_cell(value: Any) -> Any:
    """Prevent user-controlled CSV text from becoming a spreadsheet formula."""
    if not isinstance(value, str):
        return value
    if value.lstrip(" ").startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def spreadsheet_safe_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: spreadsheet_safe_cell(value) for key, value in values.items()}
