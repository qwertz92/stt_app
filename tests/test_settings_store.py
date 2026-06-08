import json

from stt_app.config import (
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_ENGINE,
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_HISTORY_MAX_ITEMS,
    DEFAULT_HOTKEY,
    DEFAULT_KEEP_ONNX_MODEL_LOADED,
    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_MODEL_SIZE,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OVERLAY_ALWAYS_ON_TOP,
    DEFAULT_OVERLAY_OPACITY_PERCENT,
    DEFAULT_OVERLAY_CORNER,
    DEFAULT_PASTE_MODE,
    DEFAULT_RECORDINGS_MAX_COUNT,
    DEFAULT_START_BEEP_TONE,
    DEFAULT_VAD_ENERGY_THRESHOLD,
)
from stt_app.persistence import backup_path
from stt_app.settings_store import CURRENT_SCHEMA_VERSION, SettingsStore


def test_load_defaults_creates_file(tmp_path):
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)

    settings = store.load()

    assert settings.schema_version == CURRENT_SCHEMA_VERSION
    assert settings.hotkey == DEFAULT_HOTKEY
    assert settings.cancel_hotkey == DEFAULT_CANCEL_HOTKEY
    assert settings.model_size == DEFAULT_MODEL_SIZE
    assert settings.language_mode == DEFAULT_LANGUAGE_MODE
    assert settings.vad_enabled is False
    assert settings.vad_energy_threshold == DEFAULT_VAD_ENERGY_THRESHOLD
    assert settings.save_last_wav is False
    assert settings.save_all_recordings is False
    assert settings.recordings_max_count == DEFAULT_RECORDINGS_MAX_COUNT
    assert settings.history_max_items == DEFAULT_HISTORY_MAX_ITEMS
    assert settings.overlay_opacity_percent == DEFAULT_OVERLAY_OPACITY_PERCENT
    assert settings.overlay_always_on_top == DEFAULT_OVERLAY_ALWAYS_ON_TOP
    assert settings.start_beep_enabled is False
    assert settings.start_beep_tone == DEFAULT_START_BEEP_TONE
    assert settings.overlay_corner == DEFAULT_OVERLAY_CORNER
    assert settings.engine == DEFAULT_ENGINE
    assert settings.mode == DEFAULT_MODE
    assert settings.paste_mode == DEFAULT_PASTE_MODE
    assert (
        settings.keep_transcript_in_clipboard
        == DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD
    )
    assert settings.keep_onnx_model_loaded == DEFAULT_KEEP_ONNX_MODEL_LOADED
    assert settings.has_openai_key is False
    assert settings.has_deepgram_key is False
    assert settings.has_elevenlabs_key is False
    assert settings.openai_model == DEFAULT_OPENAI_MODEL
    assert settings.deepgram_model == DEFAULT_DEEPGRAM_MODEL
    assert settings.assemblyai_model == DEFAULT_ASSEMBLYAI_MODEL
    assert settings.elevenlabs_model == DEFAULT_ELEVENLABS_MODEL
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


def test_legacy_default_history_limit_migrates_to_current_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"schema_version": 15, "history_max_items": 20}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.history_max_items == DEFAULT_HISTORY_MAX_ITEMS

    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == CURRENT_SCHEMA_VERSION
    assert persisted["history_max_items"] == DEFAULT_HISTORY_MAX_ITEMS


def test_custom_legacy_history_limit_is_preserved(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"schema_version": 15, "history_max_items": 100}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.history_max_items == 100


def test_overlay_always_on_top_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"overlay_always_on_top": False}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.overlay_always_on_top is False


def test_invalid_json_falls_back_to_defaults(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not-json", encoding="utf-8")

    settings = SettingsStore(settings_path).load()

    assert settings.schema_version == CURRENT_SCHEMA_VERSION
    assert settings.hotkey == DEFAULT_HOTKEY
    assert settings_path.exists() is False
    quarantined = list(tmp_path.glob("settings.json.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not-json"


def test_invalid_primary_settings_recovers_from_backup(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not-json", encoding="utf-8")
    backup = {
        "hotkey": "Ctrl+Shift+D",
        "engine": "openai",
    }
    backup_path(settings_path).write_text(json.dumps(backup), encoding="utf-8")

    settings = SettingsStore(settings_path).load()

    assert settings.hotkey == "Ctrl+Shift+D"
    assert settings.engine == "openai"
    restored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert restored["hotkey"] == "Ctrl+Shift+D"
    assert restored["engine"] == "openai"


def test_invalid_enum_values_fall_back_to_defaults(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "model_size": "xxl",
                "engine": "unknown-provider",
                "mode": "live",
                "language_mode": "zz",
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


def test_supported_multilingual_language_is_preserved(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"language_mode": "fr"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.language_mode == "fr"


def test_openai_engine_is_valid(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"engine": "openai"}),
        encoding="utf-8",
    )
    settings = SettingsStore(settings_path).load()
    assert settings.engine == "openai"


def test_webgpu_local_model_is_valid(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"engine": "local", "model_size": "cohere-transcribe-03-2026"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.engine == "local"
    assert settings.model_size == "cohere-transcribe-03-2026"


def test_granite_4_1_int8_local_model_is_valid(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"engine": "local", "model_size": "granite-speech-4.1-2b-nar"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.engine == "local"
    assert settings.model_size == "granite-speech-4.1-2b-nar"


def test_elevenlabs_engine_is_valid(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"engine": "elevenlabs"}),
        encoding="utf-8",
    )
    settings = SettingsStore(settings_path).load()
    assert settings.engine == "elevenlabs"


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


def test_remote_provider_models_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "deepgram_model": "nova-2",
                "assemblyai_model": "universal-2",
                "elevenlabs_model": "scribe_v1",
            }
        ),
        encoding="utf-8",
    )
    settings = SettingsStore(settings_path).load()
    assert settings.deepgram_model == "nova-2"
    assert settings.assemblyai_model == "universal-2"
    assert settings.elevenlabs_model == "scribe_v1"


def test_legacy_assemblyai_model_falls_back_to_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"assemblyai_model": "nano"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.assemblyai_model == DEFAULT_ASSEMBLYAI_MODEL


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
