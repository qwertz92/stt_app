from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from stt_app.config import DEFAULT_ENGINE, STREAMING_ENGINES, VALID_ENGINES


def _load_plan():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / ("_release_check_provider_plan.py")
    )
    spec = importlib.util.spec_from_file_location("_release_check_provider_plan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_release_check_provider_plan"] = module
    spec.loader.exec_module(module)
    return module


plan = _load_plan()
REMOTE = tuple(engine for engine in VALID_ENGINES if engine != DEFAULT_ENGINE)


def test_every_remote_engine_is_selected_by_default():
    assert plan.select_engines(None, REMOTE) == list(REMOTE)
    assert plan.select_engines([], REMOTE) == list(REMOTE)


def test_engine_selection_keeps_the_apps_order_and_ignores_case_and_repeats():
    assert plan.select_engines(["mistral", "Groq", "groq"], REMOTE) == [
        "groq",
        "mistral",
    ]


def test_an_unknown_engine_is_an_error_not_a_silent_skip():
    with pytest.raises(ValueError, match="zzz"):
        plan.select_engines(["groq", "zzz"], REMOTE)


def test_the_local_engine_is_not_a_remote_choice():
    assert DEFAULT_ENGINE not in REMOTE
    assert set(STREAMING_ENGINES) - {DEFAULT_ENGINE} <= set(REMOTE)


def test_an_engine_without_a_key_is_skipped_with_the_reason():
    reason = plan.skip_reason("openai", False, {})
    assert reason is not None and "openai" in reason and "key" in reason


def test_azure_needs_an_endpoint_and_custom_needs_a_base_url():
    assert "endpoint" in plan.skip_reason("azure", True, {})
    assert plan.skip_reason("azure", True, {"azure_endpoint": "https://x"}) is None
    assert "base URL" in plan.skip_reason("custom", True, {})
    assert plan.skip_reason("custom", True, {"custom_endpoint": "https://x"}) is None


def test_other_engines_with_a_key_run_without_any_setting():
    for engine in set(REMOTE) - {"azure", "custom"}:
        assert plan.skip_reason(engine, True, {}) is None


def _write_settings(appdata: Path, folder: str, data: object) -> Path:
    path = appdata / folder / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_only_the_named_routing_fields_are_read_from_the_real_settings(tmp_path):
    path = _write_settings(
        tmp_path,
        "stt_app",
        {
            "azure_endpoint": " https://res.cognitiveservices.azure.com ",
            "deepgram_region": "eu",
            "custom_key_command": "print-a-token",
            "engine": "azure",
            "azure_api_key": "must-not-be-read",
            "speechmatics_region": "",
            "assemblyai_region": 3,
        },
    )
    before = path.read_bytes()

    fields = plan.real_settings_fields(str(tmp_path), ("stt_app", "tts_app"))

    assert fields == {
        "azure_endpoint": "https://res.cognitiveservices.azure.com",
        "deepgram_region": "eu",
    }
    assert path.read_bytes() == before


def test_the_legacy_folder_is_read_without_being_renamed(tmp_path):
    _write_settings(tmp_path, "tts_app", {"deepgram_region": "eu"})

    fields = plan.real_settings_fields(str(tmp_path), ("stt_app", "tts_app"))

    assert fields == {"deepgram_region": "eu"}
    assert (tmp_path / "tts_app").is_dir()
    assert not (tmp_path / "stt_app").exists()


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"text"'])
def test_a_missing_or_damaged_settings_file_gives_the_defaults(tmp_path, content):
    assert plan.real_settings_fields(None, ("stt_app",)) == {}
    assert plan.real_settings_fields(str(tmp_path), ("stt_app",)) == {}
    (tmp_path / "stt_app").mkdir()
    (tmp_path / "stt_app" / "settings.json").write_text(content, encoding="utf-8")
    assert plan.real_settings_fields(str(tmp_path), ("stt_app",)) == {}


def test_every_routing_field_is_an_app_setting():
    from dataclasses import fields

    from stt_app.settings_store import AppSettings

    names = {field.name for field in fields(AppSettings)}
    assert set(plan.REAL_SETTINGS_FIELDS) <= names
