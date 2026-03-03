import json

from tts_app.config import (
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_ENGINE,
    DEFAULT_HISTORY_MAX_ITEMS,
    DEFAULT_HOTKEY,
    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_MODEL_SIZE,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OVERLAY_OPACITY_PERCENT,
    DEFAULT_OVERLAY_CORNER,
    DEFAULT_PASTE_MODE,
    DEFAULT_RECORDINGS_MAX_COUNT,
    DEFAULT_START_BEEP_TONE,
    DEFAULT_VAD_ENERGY_THRESHOLD,
)
from tts_app.settings_store import CURRENT_SCHEMA_VERSION, SettingsStore


def test_load_defaults_creates_file(tmp_path):
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)

    settings = store.load()

    assert settings.schema_version == CURRENT_SCHEMA_VERSION
    assert settings.hotkey == DEFAULT_HOTKEY
    assert settings.cancel_hotkey == DEFAULT_CANCEL_HOTKEY
    assert settings.model_size == DEFAULT_MODEL_SIZE
    assert settings.language_mode == DEFAULT_LANGUAGE_MODE
    assert settings.vad_enabled is True
    assert settings.vad_energy_threshold == DEFAULT_VAD_ENERGY_THRESHOLD
    assert settings.save_last_wav is False
    assert settings.save_all_recordings is False
    assert settings.recordings_max_count == DEFAULT_RECORDINGS_MAX_COUNT
    assert settings.history_max_items == DEFAULT_HISTORY_MAX_ITEMS
    assert settings.overlay_opacity_percent == DEFAULT_OVERLAY_OPACITY_PERCENT
    assert settings.start_beep_enabled is True
    assert settings.start_beep_tone == DEFAULT_START_BEEP_TONE
    assert settings.overlay_corner == DEFAULT_OVERLAY_CORNER
    assert settings.engine == DEFAULT_ENGINE
    assert settings.mode == DEFAULT_MODE
    assert settings.paste_mode == DEFAULT_PASTE_MODE
    assert (
        settings.keep_transcript_in_clipboard
        == DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD
    )
    assert settings.has_openai_key is False
    assert settings.has_deepgram_key is False
    assert settings.openai_model == DEFAULT_OPENAI_MODEL
    assert settings_path.exists()

    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == CURRENT_SCHEMA_VERSION
    assert "openai_api_key" not in raw
    assert "deepgram_api_key" not in raw


def test_load_fills_missing_values_with_defaults(tmp_path):
    settings_path = tmp_path / "settings.json"
    legacy = {
        "hotkey": "Ctrl+Shift+D",
        "model_size": "base",
        "language_mode": "de",
        "vad_enabled": False,
    }
    settings_path.write_text(json.dumps(legacy), encoding="utf-8")

    store = SettingsStore(settings_path)
    settings = store.load()

    assert settings.schema_version == CURRENT_SCHEMA_VERSION
    assert settings.hotkey == "Ctrl+Shift+D"
    assert settings.model_size == "base"
    assert settings.language_mode == "de"
    assert settings.vad_enabled is False
    assert settings.mode == DEFAULT_MODE
    assert settings.engine == DEFAULT_ENGINE
    assert settings.cancel_hotkey == DEFAULT_CANCEL_HOTKEY

    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == CURRENT_SCHEMA_VERSION
    assert persisted["mode"] == DEFAULT_MODE
    assert persisted["engine"] == DEFAULT_ENGINE


def test_invalid_json_falls_back_to_defaults(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not-json", encoding="utf-8")

    settings = SettingsStore(settings_path).load()

    assert settings.schema_version == CURRENT_SCHEMA_VERSION
    assert settings.hotkey == DEFAULT_HOTKEY


def test_invalid_enum_values_fall_back_to_defaults(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "model_size": "xxl",
                "engine": "unknown-provider",
                "mode": "live",
                "language_mode": "fr",
                "paste_mode": "invalid",
                "start_beep_tone": "ring",
                "overlay_corner": "middle",
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.model_size == DEFAULT_MODEL_SIZE
    assert settings.engine == DEFAULT_ENGINE
    assert settings.mode == DEFAULT_MODE
    assert settings.language_mode == DEFAULT_LANGUAGE_MODE
    assert settings.paste_mode == DEFAULT_PASTE_MODE
    assert settings.start_beep_tone == DEFAULT_START_BEEP_TONE
    assert settings.overlay_corner == DEFAULT_OVERLAY_CORNER


def test_openai_engine_is_valid(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"engine": "openai"}),
        encoding="utf-8",
    )
    settings = SettingsStore(settings_path).load()
    assert settings.engine == "openai"


def test_openai_model_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"openai_model": "gpt-4o-transcribe"}),
        encoding="utf-8",
    )
    settings = SettingsStore(settings_path).load()
    assert settings.openai_model == "gpt-4o-transcribe"


def test_openai_model_invalid_falls_back_to_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"openai_model": "bad-model"}),
        encoding="utf-8",
    )
    settings = SettingsStore(settings_path).load()
    assert settings.openai_model == DEFAULT_OPENAI_MODEL


def test_invalid_hotkey_falls_back_to_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"hotkey": "TotallyInvalid"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.hotkey == DEFAULT_HOTKEY


def test_invalid_cancel_hotkey_falls_back_to_cancel_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"cancel_hotkey": "TotallyInvalid"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.cancel_hotkey == DEFAULT_CANCEL_HOTKEY


def test_keep_transcript_in_clipboard_flag_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"keep_transcript_in_clipboard": False}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.keep_transcript_in_clipboard is False


def test_model_dir_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"model_dir": "C:\\whisper-models"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.model_dir == "C:\\whisper-models"

    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    assert persisted["model_dir"] == "C:\\whisper-models"


def test_numeric_limits_are_clamped_and_invalid_values_fall_back(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "recordings_max_count": "not-an-int",
                "history_max_items": -50,
                "overlay_opacity_percent": 0,
                "vad_energy_threshold": 999,
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.recordings_max_count == DEFAULT_RECORDINGS_MAX_COUNT
    assert settings.history_max_items == 0
    assert settings.overlay_opacity_percent == 25
    assert settings.vad_energy_threshold <= 0.1


def test_keep_transcript_in_clipboard_defaults_to_false():
    """Clipboard should NOT keep transcript by default (opt-in, not opt-out)."""
    assert DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD is False
