import dataclasses
import json
import logging
import math

import pytest

from stt_app.config import (
    DEFAULT_ALLOW_INSECURE_KEY_STORAGE,
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_DISPLAY_TIMEZONE,
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_ENGINE,
    DEFAULT_HISTORY_MAX_ITEMS,
    DEFAULT_HOTKEY,
    DEFAULT_KEEP_ONNX_MODEL_LOADED,
    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_MODEL_SIZE,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OVERLAY_ALWAYS_ON_TOP,
    DEFAULT_OVERLAY_CORNER,
    DEFAULT_OVERLAY_OPACITY_PERCENT,
    DEFAULT_PASTE_MODE,
    DEFAULT_RECORDINGS_MAX_COUNT,
    DEFAULT_SAVE_LAST_WAV,
    DEFAULT_SHOW_OVERLAY_HOTKEY,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    DEFAULT_START_BEEP_TONE,
    DEFAULT_VAD_ENERGY_THRESHOLD,
    parse_custom_vocabulary,
)
from stt_app.persistence import backup_path
from stt_app.settings_store import CURRENT_SCHEMA_VERSION, AppSettings, SettingsStore


def test_load_defaults_creates_file(tmp_path):
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)

    settings = store.load()

    assert settings.schema_version == CURRENT_SCHEMA_VERSION
    assert settings.hotkey == DEFAULT_HOTKEY
    assert settings.cancel_hotkey == DEFAULT_CANCEL_HOTKEY
    assert settings.show_overlay_hotkey == DEFAULT_SHOW_OVERLAY_HOTKEY
    assert settings.repaste_hotkey == ""
    assert settings.completion_beep_enabled is False
    assert settings.completion_beep_tone == "chime"
    assert settings.tray_middle_click_toggle is True
    assert settings.model_size == DEFAULT_MODEL_SIZE
    assert settings.language_mode == DEFAULT_LANGUAGE_MODE
    assert settings.vad_enabled is False
    assert settings.vad_energy_threshold == DEFAULT_VAD_ENERGY_THRESHOLD
    assert settings.save_last_wav is False
    assert settings.save_all_recordings is False
    assert settings.recordings_max_count == DEFAULT_RECORDINGS_MAX_COUNT
    assert settings.history_max_items == DEFAULT_HISTORY_MAX_ITEMS
    assert settings.display_timezone == DEFAULT_DISPLAY_TIMEZONE
    assert settings.overlay_opacity_percent == DEFAULT_OVERLAY_OPACITY_PERCENT
    assert settings.overlay_always_on_top == DEFAULT_OVERLAY_ALWAYS_ON_TOP
    assert settings.start_beep_enabled is False
    assert settings.start_beep_tone == DEFAULT_START_BEEP_TONE
    assert settings.overlay_corner == DEFAULT_OVERLAY_CORNER
    assert settings.engine == DEFAULT_ENGINE
    assert settings.mode == DEFAULT_MODE
    assert settings.concurrent_transcription_mode == "insert"
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


def test_concurrent_transcription_mode_round_trips(tmp_path):
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)

    for mode in ("insert", "history", "cancel"):
        store.save(AppSettings(concurrent_transcription_mode=mode))
        assert store.load().concurrent_transcription_mode == mode


def test_display_timezone_round_trips_and_invalid_falls_back(tmp_path):
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)

    store.save(AppSettings(display_timezone="utc"))
    assert store.load().display_timezone == "utc"

    settings_path.write_text(
        json.dumps({"display_timezone": "mars"}), encoding="utf-8"
    )
    assert store.load().display_timezone == DEFAULT_DISPLAY_TIMEZONE


def test_invalid_concurrent_transcription_mode_falls_back_to_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"concurrent_transcription_mode": "bogus"}), encoding="utf-8"
    )

    settings = SettingsStore(settings_path).load()

    assert settings.concurrent_transcription_mode == "insert"


def test_legacy_queue_boolean_migrates_to_mode(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"transcription_queue_enabled": False}), encoding="utf-8"
    )
    assert (
        SettingsStore(settings_path).load().concurrent_transcription_mode == "cancel"
    )

    settings_path.write_text(
        json.dumps({"transcription_queue_enabled": True}), encoding="utf-8"
    )
    assert (
        SettingsStore(settings_path).load().concurrent_transcription_mode == "insert"
    )


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


def test_a_retired_local_model_falls_back_to_the_default(tmp_path):
    """The two raw-graph Granite 4.1 variants were removed on 2026-08-26. A
    settings file that still names one must open on the default model rather
    than failing to build a transcriber."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"engine": "local", "model_size": "granite-speech-4.1-2b-nar"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.engine == "local"
    assert settings.model_size == DEFAULT_MODEL_SIZE


def test_a_retired_local_model_says_why_it_was_replaced(tmp_path, caplog):
    """The substitution above must be findable, not silent.

    A user whose selected model disappears sees a different model in Settings
    and nothing anywhere explaining it. The log line is what turns "the app
    changed my model" into an answerable question.
    """
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"engine": "local", "model_size": "granite-speech-4.1-2b-plus"}),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="stt_app.settings_store"):
        SettingsStore(settings_path).load()

    messages = [record.getMessage() for record in caplog.records]
    assert any("granite-speech-4.1-2b-plus" in message for message in messages)
    assert any(DEFAULT_MODEL_SIZE in message for message in messages)


def test_a_settings_file_without_a_model_logs_nothing(tmp_path, caplog):
    """A fresh file simply has no model yet; that is not worth a warning."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"engine": "local"}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="stt_app.settings_store"):
        settings = SettingsStore(settings_path).load()

    assert settings.model_size == DEFAULT_MODEL_SIZE
    assert caplog.records == []


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
                "elevenlabs_model": "scribe_v2",
            }
        ),
        encoding="utf-8",
    )
    settings = SettingsStore(settings_path).load()
    assert settings.deepgram_model == "nova-2"
    assert settings.assemblyai_model == "universal-2"
    assert settings.elevenlabs_model == "scribe_v2"


def test_removed_elevenlabs_model_falls_back_to_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"elevenlabs_model": "scribe_v1"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.elevenlabs_model == DEFAULT_ELEVENLABS_MODEL


def test_legacy_assemblyai_model_falls_back_to_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"assemblyai_model": "nano"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.assemblyai_model == DEFAULT_ASSEMBLYAI_MODEL


def test_universal_3_pro_migrates_to_universal_3_5_pro(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"assemblyai_model": "universal-3-pro"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.assemblyai_model == "universal-3-5-pro"


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


def test_show_overlay_hotkey_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    store.save(AppSettings(show_overlay_hotkey="Ctrl+Alt+F10"))

    settings = SettingsStore(settings_path).load()

    assert settings.show_overlay_hotkey == "Ctrl+Alt+F10"


def test_invalid_show_overlay_hotkey_falls_back_to_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "show_overlay_hotkey": "TotallyInvalid",
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.show_overlay_hotkey == DEFAULT_SHOW_OVERLAY_HOTKEY


def test_cleared_show_overlay_hotkey_stays_disabled(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "show_overlay_hotkey": "",
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    # An empty value at the current schema is a deliberate disable and must
    # not be re-defaulted to a key combo.
    assert settings.show_overlay_hotkey == ""


def test_legacy_empty_show_overlay_hotkey_migrates_to_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"schema_version": 20, "show_overlay_hotkey": ""}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    # Schema 20 briefly stored "" for "never configured"; it upgrades to the
    # new on-by-default combo.
    assert settings.show_overlay_hotkey == DEFAULT_SHOW_OVERLAY_HOTKEY


def test_repaste_hotkey_roundtrip_and_invalid_stays_disabled(tmp_path):
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    store.save(AppSettings(repaste_hotkey="Ctrl+Alt+F9"))
    assert SettingsStore(settings_path).load().repaste_hotkey == "Ctrl+Alt+F9"

    settings_path.write_text(
        json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "repaste_hotkey": "TotallyInvalid",
            }
        ),
        encoding="utf-8",
    )

    # The re-paste hotkey has no default combo: invalid values disable it.
    assert SettingsStore(settings_path).load().repaste_hotkey == ""


def test_completion_beep_and_tray_middle_click_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    store.save(
        AppSettings(
            completion_beep_enabled=True,
            completion_beep_tone="high",
            tray_middle_click_toggle=False,
        )
    )

    settings = SettingsStore(settings_path).load()

    assert settings.completion_beep_enabled is True
    assert settings.completion_beep_tone == "high"
    assert settings.tray_middle_click_toggle is False


def test_invalid_completion_beep_tone_falls_back_to_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"completion_beep_tone": "airhorn"}),
        encoding="utf-8",
    )

    assert SettingsStore(settings_path).load().completion_beep_tone == "chime"


def test_keep_transcript_in_clipboard_flag_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"keep_transcript_in_clipboard": False}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.keep_transcript_in_clipboard is False


def test_string_false_never_enables_insecure_key_storage(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "allow_insecure_key_storage": "false",
                "offline_mode": "false",
                "vad_enabled": "true",
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.allow_insecure_key_storage is False
    assert settings.offline_mode is False
    assert settings.vad_enabled is True


def test_invalid_boolean_values_fall_back_to_safe_defaults(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "allow_insecure_key_storage": "definitely",
                "save_last_wav": {"unexpected": "object"},
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.allow_insecure_key_storage is DEFAULT_ALLOW_INSECURE_KEY_STORAGE
    assert settings.save_last_wav is DEFAULT_SAVE_LAST_WAV


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


def test_streaming_full_final_transcript_defaults_to_false(tmp_path):
    """The streaming history re-transcription pass is opt-in."""
    settings = SettingsStore(tmp_path / "settings.json").load()

    assert settings.streaming_full_final_transcript is False


def test_streaming_full_final_transcript_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"streaming_full_final_transcript": True}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.streaming_full_final_transcript is True


def test_immediate_background_insert_defaults_to_false(tmp_path):
    """Continuous queued-insert delivery is opt-in."""
    settings = SettingsStore(tmp_path / "settings.json").load()

    assert settings.immediate_background_insert is False


def test_immediate_background_insert_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"immediate_background_insert": True}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.immediate_background_insert is True


def test_keep_microphone_warm_roundtrip_and_default(tmp_path):
    settings_path = tmp_path / "settings.json"
    assert SettingsStore(settings_path).load().keep_microphone_warm is False

    settings_path.write_text(
        json.dumps({"keep_microphone_warm": True}),
        encoding="utf-8",
    )
    assert SettingsStore(settings_path).load().keep_microphone_warm is True


def test_insert_target_roundtrip_and_validation(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"insert_target": "current_window"}),
        encoding="utf-8",
    )
    assert SettingsStore(settings_path).load().insert_target == "current_window"

    settings_path.write_text(
        json.dumps({"insert_target": "bogus"}),
        encoding="utf-8",
    )
    assert SettingsStore(settings_path).load().insert_target == "recording_window"


def test_corrupt_primary_and_backup_are_both_quarantined(tmp_path):
    settings_path = tmp_path / "settings.json"
    backup_path = tmp_path / "settings.json.bak"
    settings_path.write_text("{not-json", encoding="utf-8")
    backup_path.write_text("{also-not-json", encoding="utf-8")

    SettingsStore(settings_path).load()

    assert settings_path.exists() is False
    assert backup_path.exists() is False
    assert list(tmp_path.glob("settings.json.corrupt.*"))
    assert list(tmp_path.glob("settings.json.bak.corrupt.*"))


def test_custom_vocabulary_defaults_to_empty(tmp_path):
    settings = SettingsStore(tmp_path / "settings.json").load()

    assert settings.custom_vocabulary == ""


def test_custom_vocabulary_roundtrip(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"custom_vocabulary": "Kubernetes, Splunk SOAR"}),
        encoding="utf-8",
    )

    settings = SettingsStore(settings_path).load()

    assert settings.custom_vocabulary == "Kubernetes, Splunk SOAR"

    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    assert persisted["custom_vocabulary"] == "Kubernetes, Splunk SOAR"


def test_save_succeeds_when_backup_write_fails(tmp_path, monkeypatch):
    import stt_app.persistence as persistence_module
    from stt_app.persistence import atomic_write_text as real_atomic_write_text

    settings_path = tmp_path / "settings.json"

    def failing_backup_write(path, text, **kwargs):
        if path.name.endswith(".bak"):
            raise OSError("backup volume unavailable")
        real_atomic_write_text(path, text, **kwargs)

    monkeypatch.setattr(
        persistence_module,
        "atomic_write_text",
        failing_backup_write,
    )

    SettingsStore(settings_path).save(AppSettings())

    assert settings_path.exists() is True


class TestParseCustomVocabulary:
    def test_empty_string_gives_empty_list(self):
        assert parse_custom_vocabulary("") == []

    def test_none_gives_empty_list(self):
        assert parse_custom_vocabulary(None) == []

    def test_splits_on_commas(self):
        assert parse_custom_vocabulary("Kubernetes, Splunk SOAR") == [
            "Kubernetes",
            "Splunk SOAR",
        ]

    def test_splits_on_newlines(self):
        assert parse_custom_vocabulary("Kubernetes\nSplunk SOAR") == [
            "Kubernetes",
            "Splunk SOAR",
        ]

    def test_splits_on_semicolons(self):
        assert parse_custom_vocabulary("Kubernetes; Splunk SOAR") == [
            "Kubernetes",
            "Splunk SOAR",
        ]

    def test_splits_on_mixed_delimiters(self):
        assert parse_custom_vocabulary("Kubernetes,\nSplunk SOAR; Terraform") == [
            "Kubernetes",
            "Splunk SOAR",
            "Terraform",
        ]

    def test_strips_whitespace_around_terms(self):
        assert parse_custom_vocabulary("  Kubernetes  ,  Splunk SOAR  ") == [
            "Kubernetes",
            "Splunk SOAR",
        ]

    def test_drops_empty_entries(self):
        assert parse_custom_vocabulary("Kubernetes,,, Splunk SOAR,") == [
            "Kubernetes",
            "Splunk SOAR",
        ]

    def test_dedupes_case_insensitively_preserving_first_seen_casing(self):
        assert parse_custom_vocabulary("Kubernetes, kubernetes, KUBERNETES") == [
            "Kubernetes",
        ]

    def test_preserves_order(self):
        assert parse_custom_vocabulary("Splunk SOAR, Kubernetes, Terraform") == [
            "Splunk SOAR",
            "Kubernetes",
            "Terraform",
        ]

    def test_caps_at_100_terms(self):
        raw = ", ".join(f"term{i}" for i in range(150))
        result = parse_custom_vocabulary(raw)
        assert len(result) == 100
        assert result[0] == "term0"
        assert result[-1] == "term99"


class TestInputDeviceName:
    def test_defaults_to_system_default(self, tmp_path):
        store = SettingsStore(tmp_path / "settings.json")

        settings = store.load()

        assert settings.input_device_name == ""

    def test_round_trips_selected_device(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        store = SettingsStore(settings_path)
        settings = store.load()

        store.save(
            AppSettings(
                **{
                    **settings.to_dict(),
                    "input_device_name": "Headset Microphone (USB)",
                }
            )
        )

        reloaded = SettingsStore(settings_path).load()
        assert reloaded.input_device_name == "Headset Microphone (USB)"

    def test_strips_whitespace_and_tolerates_null(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"input_device_name": "  USB Mic  "}),
            encoding="utf-8",
        )
        assert SettingsStore(settings_path).load().input_device_name == "USB Mic"

        settings_path.write_text(
            json.dumps({"input_device_name": None}),
            encoding="utf-8",
        )
        assert SettingsStore(settings_path).load().input_device_name == ""


def test_older_settings_adopt_the_silence_gate_default(tmp_path, monkeypatch):
    """A stored "off" from before the default flip cannot be a real choice.

    Every older file carries it, and without the gate a silent recording is
    transcribed into invented text on engines that have no no-speech detection.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = tmp_path / "stt_app" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 21, "silence_gate_enabled": False}),
        encoding="utf-8",
    )

    settings = SettingsStore(path).load()

    assert settings.silence_gate_enabled is True


def test_deliberate_silence_gate_off_is_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = tmp_path / "stt_app" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "silence_gate_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(path).load()

    assert settings.silence_gate_enabled is False


def test_local_onnx_device_round_trips_and_rejects_unknown_values(tmp_path):
    """An unknown or hand-edited value must fall back to auto rather than fail
    the load, and a valid one must survive save/load."""
    from dataclasses import replace

    from stt_app.settings_store import normalize_local_onnx_device

    assert normalize_local_onnx_device("cpu") == "cpu"
    assert normalize_local_onnx_device("  DML ") == "dml"
    assert normalize_local_onnx_device("nonsense") == "auto"
    assert normalize_local_onnx_device(None) == "auto"

    store = SettingsStore(tmp_path / "settings.json")
    store.save(replace(store.load(), local_onnx_device="webgpu"))
    assert store.load().local_onnx_device == "webgpu"


def test_the_out_of_the_box_defaults_describe_a_runnable_combination():
    """A fresh install must work without opening Settings once.

    The default model is Parakeet: fastest local model by a wide margin, no
    GPU and no Node.js, and it detects its own language. It is also batch-only
    and offers no explicit language, so the other three defaults have to agree
    with that or the first run starts in a state the app itself rejects.
    """
    from stt_app.config import (
        DEFAULT_ENGINE,
        DEFAULT_LANGUAGE_MODE,
        DEFAULT_MODE,
        DEFAULT_MODEL_SIZE,
        PARAKEET_MODEL_SIZE,
        VALID_MODEL_SIZES,
        language_modes_for_selection,
        supports_streaming,
    )

    assert DEFAULT_MODEL_SIZE == PARAKEET_MODEL_SIZE
    assert DEFAULT_MODEL_SIZE in VALID_MODEL_SIZES
    # Parakeet cannot stream, so the default mode must not be streaming.
    assert DEFAULT_MODE == "batch"
    assert not supports_streaming(DEFAULT_ENGINE, DEFAULT_MODEL_SIZE)
    # ... and the default language must be one the model actually offers.
    assert DEFAULT_LANGUAGE_MODE in language_modes_for_selection(
        DEFAULT_ENGINE, DEFAULT_MODEL_SIZE
    )


def test_a_stored_model_is_never_replaced_by_the_new_default(tmp_path):
    """Changing `DEFAULT_MODEL_SIZE` must not touch an existing install.

    The default is only a fallback for an absent key, so someone who chose
    faster-whisper `small` keeps it across the version that moved the default
    to Parakeet.
    """
    from stt_app.config import DEFAULT_MODEL_SIZE

    assert DEFAULT_MODEL_SIZE != "small", "pick a different stand-in below"
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    store.save(dataclasses.replace(store.load(), model_size="small"))

    assert SettingsStore(settings_path).load().model_size == "small"


def test_faster_whisper_still_defaults_to_a_faster_whisper_model():
    """`LocalFasterWhisperTranscriber` had `DEFAULT_MODEL_SIZE` as its own
    default argument, which now names a model it cannot load."""
    import inspect

    from stt_app.config import FASTER_WHISPER_MODEL_SIZES
    from stt_app.transcriber.local_faster_whisper import (
        LocalFasterWhisperTranscriber,
    )

    default = inspect.signature(
        LocalFasterWhisperTranscriber.__init__
    ).parameters["model_size"].default

    assert default in FASTER_WHISPER_MODEL_SIZES


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("Infinity in an int field", '{"history_max_items": Infinity}'),
        ("an overflowing literal", '{"history_max_items": 1e400}'),
        ("negative Infinity", '{"recordings_max_count": -Infinity}'),
        ("Infinity in the schema version", '{"schema_version": Infinity}'),
        ("Infinity in the opacity", '{"overlay_opacity_percent": Infinity}'),
        ("NaN in an int field", '{"history_max_items": NaN}'),
        ("NaN in a float field", '{"silence_gate_threshold": NaN}'),
        ("NaN in the VAD threshold", '{"vad_energy_threshold": NaN}'),
        (
            "a 400-digit integer in a float field",
            '{"silence_gate_threshold": ' + "9" * 400 + "}",
        ),
        (
            "a 400-digit integer in the VAD threshold",
            '{"vad_energy_threshold": ' + "9" * 400 + "}",
        ),
    ],
)
def test_a_hostile_number_never_escapes_from_dict(label, payload):
    """`OverflowError` is an `ArithmeticError`, not a `ValueError`.

    Naming only the usual two let it out of `from_dict` -- and because the
    file *parsed*, the store had already accepted it as the primary payload,
    so the crash happened past the point where the backup would be read or the
    primary quarantined. `main` calls `load()` before any window exists, which
    makes a packaged build a double-click that does nothing.

    Both directions reach it: `int(float("inf"))` for the four integer fields,
    and `float(<400-digit integer>)` for the two float ones. NaN is the
    sibling case -- it survives `float()` and then defeats the clamp, because
    every comparison against it is False.
    """
    settings = AppSettings.from_dict(json.loads(payload))

    assert 0 <= settings.history_max_items <= 5000, label
    assert 1 <= settings.recordings_max_count <= 500, label
    assert 0 <= settings.overlay_opacity_percent <= 100, label
    assert math.isfinite(settings.silence_gate_threshold), label
    assert math.isfinite(settings.vad_energy_threshold), label
    assert isinstance(settings.schema_version, int), label
    # A clamp is not enough for NaN: it must land on the default, not on
    # whichever bound `max`/`min` happened to return first.
    if "NaN" in payload and "silence_gate" in payload:
        assert settings.silence_gate_threshold == DEFAULT_SILENCE_GATE_THRESHOLD
    if "NaN" in payload and "vad_energy" in payload:
        assert settings.vad_energy_threshold == DEFAULT_VAD_ENERGY_THRESHOLD


def test_an_older_build_neither_lowers_the_schema_nor_drops_newer_keys(tmp_path):
    """One `%APPDATA%` file is shared by the release and every source checkout.

    `to_dict` stamped `CURRENT_SCHEMA_VERSION` unconditionally, so running an
    older revision once -- bisecting, verifying a release, checking out `main`
    while a branch has bumped the schema -- rewrote the file with a *lower*
    number. The migrations keyed on `raw_schema_version <` then re-fired on the
    next run of the newer build, and both of them document themselves as
    one-time: a deliberately cleared overlay hotkey came back, and a
    deliberately disabled silence gate turned itself on.
    """
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION + 1,
                "a_setting_this_build_does_not_know": "kept",
                "show_overlay_hotkey": "",
                "silence_gate_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(path).load()
    on_disk = json.loads(path.read_text(encoding="utf-8"))

    assert on_disk["schema_version"] == CURRENT_SCHEMA_VERSION + 1, (
        "the recorded schema version went backwards"
    )
    assert on_disk["a_setting_this_build_does_not_know"] == "kept"
    assert settings.show_overlay_hotkey == ""
    assert settings.silence_gate_enabled is False

    # And the newer build reading it back gets the same answer.
    reloaded = SettingsStore(path).load()
    assert reloaded.show_overlay_hotkey == ""
    assert reloaded.silence_gate_enabled is False


def test_an_unknown_key_can_never_overwrite_a_known_field(tmp_path):
    """`extra` is built as `raw.keys() - payload.keys()`, so it cannot collide.

    Stated as a test because the preservation path writes caller-supplied keys
    into the payload, and a future caller passing a known field name would
    silently bypass every clamp and migration in `from_dict`.
    """
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    settings = AppSettings(history_max_items=42)

    store.save(settings, extra={"history_max_items": 999, "future_key": "kept"})
    on_disk = json.loads(path.read_text(encoding="utf-8"))

    assert on_disk["history_max_items"] == 42
    assert on_disk["future_key"] == "kept"
