from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# XML 1.0 permits #x9, #xA, #xD, #x20-#xD7FF, #xE000-#xFFFD and
# #x10000-#x10FFFF and nothing else -- not even escaped -- while
# `saxutils.escape` only rewrites `&`, `<` and `>`. One control byte anywhere
# in an exported row therefore produced a worksheet that will not parse,
# inside a `.xlsx` written without error that Excel then refuses to open.
#
# The same set is what the other formats need, which is why this is not
# XML-only: a lone surrogate cannot be encoded as UTF-8 at all, so the CSV and
# Markdown writers raised `UnicodeEncodeError` part-way through writing. It
# lives here rather than beside one exporter because every export format needs
# it, the CLI's CSV writer included.
_EXPORTABLE_RANGES = (
    (0x09, 0x09),
    (0x0A, 0x0A),
    (0x0D, 0x0D),
    (0x20, 0xD7FF),
    (0xE000, 0xFFFD),
    (0x10000, 0x10FFFF),
)
_UNEXPORTABLE_CHARACTERS = re.compile(
    "[^" + "".join(f"{chr(low)}-{chr(high)}" for low, high in _EXPORTABLE_RANGES) + "]"
)
_REPLACEMENT_CHARACTER = chr(0xFFFD)


def export_safe_text(value: str) -> str:
    """Replace what an export cannot carry, rather than writing a broken file.

    Exactly those characters and nothing else: stripping non-ASCII instead
    would trade an unopenable file for a silently mangled German transcript.
    """
    return _UNEXPORTABLE_CHARACTERS.sub(_REPLACEMENT_CHARACTER, value)


def spreadsheet_safe_cell(value: Any) -> Any:
    """Prevent user-controlled CSV text from becoming a spreadsheet formula."""
    if not isinstance(value, str):
        return value
    if value.lstrip(" ").startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def spreadsheet_safe_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: spreadsheet_safe_cell(value) for key, value in values.items()}
