from __future__ import annotations

import pytest

from stt_app.history_ui_actions import (
    format_history_count_label,
    history_engine_label,
    history_model_label,
)
from stt_app.settings_dialog_helpers import local_model_short_label


@pytest.mark.parametrize(
    ("total", "limit", "expected"),
    [
        (1, 3000, "1 entry stored (limit 3000)"),
        (0, 3000, "0 entries stored (limit 3000)"),
        (2, 3, "2 entries stored (limit 3)"),
        (5, 3, "5 entries stored (limit 3); showing only the latest"),
        (1, 0, "1 entry stored (no limit)"),
        (40, 0, "40 entries stored (no limit)"),
    ],
)
def test_the_count_label_uses_the_right_number_and_says_nothing_twice(
    total: int, limit: int, expected: str
) -> None:
    assert format_history_count_label(total, limit) == expected


def test_history_names_engine_and_model_the_way_the_pickers_do() -> None:
    assert history_engine_label("local") == "Local"
    assert history_engine_label("groq") == "Groq"
    assert history_engine_label("unknown-engine") == "unknown-engine"
    model = "parakeet-tdt-0.6b-v3"
    assert history_model_label("local", model) == local_model_short_label(model)
    # A remote model's id is what its picker shows.
    assert history_model_label("groq", "whisper-large-v3") == "whisper-large-v3"
