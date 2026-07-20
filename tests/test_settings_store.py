import json

from stt_app.config import (
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_ALLOW_INSECURE_KEY_STORAGE,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_SHOW_OVERLAY_HOTKEY,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_ENGINE,
    DEFAULT_DISPLAY_TIMEZONE,
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
    DEFAULT_SAVE_LAST_WAV,
    DEFAULT_START_BEEP_TONE,
    DEFAULT_VAD_ENERGY_THRESHOLD,
)
from stt_app.config import parse_custom_vocabulary
from stt_app.persistence import backup_path
from stt_app.settings_store import AppSettings, CURRENT_SCHEMA_VERSION, SettingsStore


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
