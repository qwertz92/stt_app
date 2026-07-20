from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .app_paths import settings_path
from .persistence import (
    atomic_write_json,
    load_json_with_backup,
    lock_for_path,
    parse_json_bool,
    quarantine_corrupt_file,
)
from .config import (
    ASSEMBLYAI_MODELS,
    AZURE_SPEECH_MODELS,
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_ALLOW_INSECURE_KEY_STORAGE,
    DEFAULT_AZURE_ENDPOINT,
    DEFAULT_AZURE_SPEECH_MODEL,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_ENGINE,
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_FUNASR_MODEL,
    DEFAULT_GROQ_MODEL,
    DEFAULT_HOTKEY,
    DEEPGRAM_MODELS,
    DEFAULT_HISTORY_MAX_ITEMS,
    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    DEFAULT_KEEP_ONNX_MODEL_LOADED,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_SIZE,
    DEFAULT_OFFLINE_MODE,
    DEFAULT_OVERLAY_ALWAYS_ON_TOP,
    DEFAULT_OVERLAY_OPACITY_PERCENT,
    DEFAULT_OVERLAY_CORNER,
    DEFAULT_PASTE_MODE,
    DEFAULT_RECORDINGS_DIR,
    DEFAULT_RECORDINGS_MAX_COUNT,
    DEFAULT_COMPLETION_BEEP_ENABLED,
    DEFAULT_COMPLETION_BEEP_TONE,
    DEFAULT_REPASTE_HOTKEY,
    DEFAULT_SAVE_ALL_RECORDINGS,
    DEFAULT_SAVE_LAST_WAV,
    DEFAULT_SHOW_OVERLAY_HOTKEY,
    DEFAULT_START_BEEP_ENABLED,
    DEFAULT_TRAY_MIDDLE_CLICK_TOGGLE,
    DEFAULT_STREAMING_FULL_FINAL_TRANSCRIPT,
    DEFAULT_CONCURRENT_TRANSCRIPTION_MODE,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_IMMEDIATE_BACKGROUND_INSERT,
    DEFAULT_INPUT_DEVICE_NAME,
    DEFAULT_INSERT_TARGET,
    DEFAULT_KEEP_MICROPHONE_WARM,
    DEFAULT_SILENCE_GATE_ENABLED,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    SILENCE_GATE_THRESHOLD_MAX,
    SILENCE_GATE_THRESHOLD_MIN,
    VALID_INSERT_TARGETS,
    CONCURRENT_TRANSCRIPTION_MODE_INSERT,
    CONCURRENT_TRANSCRIPTION_MODE_CANCEL,
    DEFAULT_DISPLAY_TIMEZONE,
    VALID_CONCURRENT_TRANSCRIPTION_MODES,
    VALID_DISPLAY_TIMEZONES,
    DEFAULT_START_BEEP_TONE,
    DEFAULT_VAD_ENERGY_THRESHOLD,
    DEFAULT_VAD_ENABLED,
    ELEVENLABS_MODELS,
    FUNASR_MODELS,
    GROQ_MODELS,
    HISTORY_MAX_ITEMS_MAX,
    DEFAULT_OPENAI_MODEL,
    OVERLAY_OPACITY_MAX_PERCENT,
    OVERLAY_OPACITY_MIN_PERCENT,
    OPENAI_MODELS,
    SCHEMA_VERSION,
    VALID_OVERLAY_CORNERS,
    VALID_START_BEEP_TONES,
    VAD_ENERGY_THRESHOLD_MAX,
    VAD_ENERGY_THRESHOLD_MIN,
    VALID_ENGINES,
    VALID_LANGUAGE_MODES,
    VALID_MODES,
    VALID_MODEL_SIZES,
    VALID_PASTE_MODES,
)
from .hotkey import parse_hotkey

CURRENT_SCHEMA_VERSION = SCHEMA_VERSION
_HISTORY_RETENTION_SCHEMA_VERSION = 16
_LEGACY_DEFAULT_HISTORY_MAX_ITEMS = 20

DEFAULTS = {
    "schema_version": CURRENT_SCHEMA_VERSION,
    "hotkey": DEFAULT_HOTKEY,
    "cancel_hotkey": DEFAULT_CANCEL_HOTKEY,
    "show_overlay_hotkey": DEFAULT_SHOW_OVERLAY_HOTKEY,
    "repaste_hotkey": DEFAULT_REPASTE_HOTKEY,
    "model_size": DEFAULT_MODEL_SIZE,
    "language_mode": DEFAULT_LANGUAGE_MODE,
    "custom_vocabulary": DEFAULT_CUSTOM_VOCABULARY,
    "vad_enabled": DEFAULT_VAD_ENABLED,
    "input_device_name": DEFAULT_INPUT_DEVICE_NAME,
    "keep_microphone_warm": DEFAULT_KEEP_MICROPHONE_WARM,
    "silence_gate_enabled": DEFAULT_SILENCE_GATE_ENABLED,
    "silence_gate_threshold": DEFAULT_SILENCE_GATE_THRESHOLD,
    "vad_energy_threshold": DEFAULT_VAD_ENERGY_THRESHOLD,
    "save_last_wav": DEFAULT_SAVE_LAST_WAV,
    "save_all_recordings": DEFAULT_SAVE_ALL_RECORDINGS,
    "recordings_dir": DEFAULT_RECORDINGS_DIR,
    "recordings_max_count": DEFAULT_RECORDINGS_MAX_COUNT,
    "history_max_items": DEFAULT_HISTORY_MAX_ITEMS,
    "display_timezone": DEFAULT_DISPLAY_TIMEZONE,
    "overlay_opacity_percent": DEFAULT_OVERLAY_OPACITY_PERCENT,
    "overlay_always_on_top": DEFAULT_OVERLAY_ALWAYS_ON_TOP,
    "engine": DEFAULT_ENGINE,
    "mode": DEFAULT_MODE,
    "streaming_full_final_transcript": DEFAULT_STREAMING_FULL_FINAL_TRANSCRIPT,
    "concurrent_transcription_mode": DEFAULT_CONCURRENT_TRANSCRIPTION_MODE,
    "immediate_background_insert": DEFAULT_IMMEDIATE_BACKGROUND_INSERT,
    "insert_target": DEFAULT_INSERT_TARGET,
    "paste_mode": DEFAULT_PASTE_MODE,
    "keep_transcript_in_clipboard": DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    "allow_insecure_key_storage": DEFAULT_ALLOW_INSECURE_KEY_STORAGE,
    "offline_mode": DEFAULT_OFFLINE_MODE,
    "keep_onnx_model_loaded": DEFAULT_KEEP_ONNX_MODEL_LOADED,
    "start_beep_enabled": DEFAULT_START_BEEP_ENABLED,
    "start_beep_tone": DEFAULT_START_BEEP_TONE,
    "completion_beep_enabled": DEFAULT_COMPLETION_BEEP_ENABLED,
    "completion_beep_tone": DEFAULT_COMPLETION_BEEP_TONE,
    "tray_middle_click_toggle": DEFAULT_TRAY_MIDDLE_CLICK_TOGGLE,
    "overlay_corner": DEFAULT_OVERLAY_CORNER,
    "model_dir": DEFAULT_MODEL_DIR,
    "has_openai_key": False,
    "has_deepgram_key": False,
    "has_assemblyai_key": False,
    "has_groq_key": False,
    "has_elevenlabs_key": False,
    "has_azure_key": False,
    "has_funasr_key": False,
    "groq_model": DEFAULT_GROQ_MODEL,
    "openai_model": DEFAULT_OPENAI_MODEL,
    "deepgram_model": DEFAULT_DEEPGRAM_MODEL,
    "assemblyai_model": DEFAULT_ASSEMBLYAI_MODEL,
    "elevenlabs_model": DEFAULT_ELEVENLABS_MODEL,
    "azure_speech_model": DEFAULT_AZURE_SPEECH_MODEL,
    "azure_endpoint": DEFAULT_AZURE_ENDPOINT,
    "funasr_model": DEFAULT_FUNASR_MODEL,
}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class AppSettings:
    schema_version: int = CURRENT_SCHEMA_VERSION
    hotkey: str = DEFAULT_HOTKEY
    cancel_hotkey: str = DEFAULT_CANCEL_HOTKEY
    show_overlay_hotkey: str = DEFAULT_SHOW_OVERLAY_HOTKEY
    repaste_hotkey: str = DEFAULT_REPASTE_HOTKEY
    model_size: str = DEFAULT_MODEL_SIZE
    language_mode: str = DEFAULT_LANGUAGE_MODE
    custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY
    vad_enabled: bool = DEFAULT_VAD_ENABLED
    input_device_name: str = DEFAULT_INPUT_DEVICE_NAME
    keep_microphone_warm: bool = DEFAULT_KEEP_MICROPHONE_WARM
    silence_gate_enabled: bool = DEFAULT_SILENCE_GATE_ENABLED
    silence_gate_threshold: float = DEFAULT_SILENCE_GATE_THRESHOLD
    vad_energy_threshold: float = DEFAULT_VAD_ENERGY_THRESHOLD
    save_last_wav: bool = DEFAULT_SAVE_LAST_WAV
    save_all_recordings: bool = DEFAULT_SAVE_ALL_RECORDINGS
    recordings_dir: str = DEFAULT_RECORDINGS_DIR
    recordings_max_count: int = DEFAULT_RECORDINGS_MAX_COUNT
    history_max_items: int = DEFAULT_HISTORY_MAX_ITEMS
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE
    overlay_opacity_percent: int = DEFAULT_OVERLAY_OPACITY_PERCENT
    overlay_always_on_top: bool = DEFAULT_OVERLAY_ALWAYS_ON_TOP
    engine: str = DEFAULT_ENGINE
    mode: str = DEFAULT_MODE
    streaming_full_final_transcript: bool = DEFAULT_STREAMING_FULL_FINAL_TRANSCRIPT
    concurrent_transcription_mode: str = DEFAULT_CONCURRENT_TRANSCRIPTION_MODE
    immediate_background_insert: bool = DEFAULT_IMMEDIATE_BACKGROUND_INSERT
    insert_target: str = DEFAULT_INSERT_TARGET
    paste_mode: str = DEFAULT_PASTE_MODE
    keep_transcript_in_clipboard: bool = DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD
    allow_insecure_key_storage: bool = DEFAULT_ALLOW_INSECURE_KEY_STORAGE
    offline_mode: bool = DEFAULT_OFFLINE_MODE
    keep_onnx_model_loaded: bool = DEFAULT_KEEP_ONNX_MODEL_LOADED
    start_beep_enabled: bool = DEFAULT_START_BEEP_ENABLED
    start_beep_tone: str = DEFAULT_START_BEEP_TONE
    completion_beep_enabled: bool = DEFAULT_COMPLETION_BEEP_ENABLED
    completion_beep_tone: str = DEFAULT_COMPLETION_BEEP_TONE
    tray_middle_click_toggle: bool = DEFAULT_TRAY_MIDDLE_CLICK_TOGGLE
    overlay_corner: str = DEFAULT_OVERLAY_CORNER
    model_dir: str = DEFAULT_MODEL_DIR
    has_openai_key: bool = False
    has_deepgram_key: bool = False
    has_assemblyai_key: bool = False
    has_groq_key: bool = False
    has_elevenlabs_key: bool = False
    has_azure_key: bool = False
    has_funasr_key: bool = False
    groq_model: str = DEFAULT_GROQ_MODEL
    openai_model: str = DEFAULT_OPENAI_MODEL
    deepgram_model: str = DEFAULT_DEEPGRAM_MODEL
    assemblyai_model: str = DEFAULT_ASSEMBLYAI_MODEL
    elevenlabs_model: str = DEFAULT_ELEVENLABS_MODEL
    azure_speech_model: str = DEFAULT_AZURE_SPEECH_MODEL
    azure_endpoint: str = DEFAULT_AZURE_ENDPOINT
    funasr_model: str = DEFAULT_FUNASR_MODEL

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppSettings":
        merged: dict[str, Any] = dict(DEFAULTS)
        merged.update(raw)
        raw_schema_version = _int_or_none(raw.get("schema_version")) or 0

        language_mode = str(merged.get("language_mode", DEFAULT_LANGUAGE_MODE)).lower()
        if language_mode not in VALID_LANGUAGE_MODES:
            language_mode = DEFAULT_LANGUAGE_MODE

        engine = str(merged.get("engine", DEFAULT_ENGINE)).lower()
        if engine not in VALID_ENGINES:
            engine = DEFAULT_ENGINE

        mode = str(merged.get("mode", DEFAULT_MODE)).lower()
        if mode not in VALID_MODES:
            mode = DEFAULT_MODE

        paste_mode = str(merged.get("paste_mode", DEFAULT_PASTE_MODE)).lower()
        if paste_mode not in VALID_PASTE_MODES:
            paste_mode = DEFAULT_PASTE_MODE

        try:
            silence_gate_threshold = float(
                merged.get(
                    "silence_gate_threshold",
                    DEFAULT_SILENCE_GATE_THRESHOLD,
                )
            )
        except (TypeError, ValueError):
            silence_gate_threshold = DEFAULT_SILENCE_GATE_THRESHOLD
        silence_gate_threshold = min(
            SILENCE_GATE_THRESHOLD_MAX,
            max(SILENCE_GATE_THRESHOLD_MIN, silence_gate_threshold),
        )

        insert_target = str(
            merged.get("insert_target", DEFAULT_INSERT_TARGET)
        ).strip().lower()
        if insert_target not in VALID_INSERT_TARGETS:
            insert_target = DEFAULT_INSERT_TARGET

        # Read from raw (not merged) so an absent key can fall back to the
        # earlier boolean before defaulting.
        concurrent_transcription_mode = str(
            raw.get("concurrent_transcription_mode", "")
        ).strip().lower()
        if concurrent_transcription_mode not in VALID_CONCURRENT_TRANSCRIPTION_MODES:
            # Migrate the earlier boolean: True meant "queue + insert", while
            # False meant the old discard-on-supersede behavior, now "cancel".
            legacy_queue = raw.get("transcription_queue_enabled")
            if legacy_queue is not None:
                concurrent_transcription_mode = (
                    CONCURRENT_TRANSCRIPTION_MODE_INSERT
                    if bool(legacy_queue)
                    else CONCURRENT_TRANSCRIPTION_MODE_CANCEL
                )
            else:
                concurrent_transcription_mode = DEFAULT_CONCURRENT_TRANSCRIPTION_MODE

        model_size = str(merged.get("model_size", DEFAULT_MODEL_SIZE)).lower()
        if model_size not in VALID_MODEL_SIZES:
            model_size = DEFAULT_MODEL_SIZE

        hotkey = str(merged.get("hotkey", DEFAULT_HOTKEY))
        hotkey = _normalize_hotkey(hotkey, default=DEFAULT_HOTKEY)
        cancel_hotkey = str(merged.get("cancel_hotkey", DEFAULT_CANCEL_HOTKEY))
        cancel_hotkey = _normalize_hotkey(
            cancel_hotkey, default=DEFAULT_CANCEL_HOTKEY
        )
        # Optional hotkeys: an empty stored value is a deliberate "disabled"
        # and must stay empty; only invalid non-empty values fall back to the
        # respective default.
        show_overlay_hotkey = _normalize_optional_hotkey(
            str(merged.get("show_overlay_hotkey", DEFAULT_SHOW_OVERLAY_HOTKEY)),
            default=DEFAULT_SHOW_OVERLAY_HOTKEY,
        )
        if raw_schema_version < 21 and not show_overlay_hotkey:
            # Schema 20 briefly stored "" for "never configured"; the hotkey
            # now defaults on, so only schema >= 21 empties mean "disabled".
            show_overlay_hotkey = DEFAULT_SHOW_OVERLAY_HOTKEY
        repaste_hotkey = _normalize_optional_hotkey(
            str(merged.get("repaste_hotkey", DEFAULT_REPASTE_HOTKEY)),
            default=DEFAULT_REPASTE_HOTKEY,
        )

        groq_model = str(merged.get("groq_model", DEFAULT_GROQ_MODEL))
        if groq_model not in GROQ_MODELS:
            groq_model = DEFAULT_GROQ_MODEL
        openai_model = str(merged.get("openai_model", DEFAULT_OPENAI_MODEL))
        if openai_model not in OPENAI_MODELS:
            openai_model = DEFAULT_OPENAI_MODEL
        deepgram_model = str(merged.get("deepgram_model", DEFAULT_DEEPGRAM_MODEL))
        if deepgram_model not in DEEPGRAM_MODELS:
            deepgram_model = DEFAULT_DEEPGRAM_MODEL
        assemblyai_model = str(
            merged.get("assemblyai_model", DEFAULT_ASSEMBLYAI_MODEL)
        )
        if assemblyai_model == "universal-3-pro":
            assemblyai_model = DEFAULT_ASSEMBLYAI_MODEL
        elif assemblyai_model not in ASSEMBLYAI_MODELS:
            assemblyai_model = DEFAULT_ASSEMBLYAI_MODEL
        elevenlabs_model = str(
            merged.get("elevenlabs_model", DEFAULT_ELEVENLABS_MODEL)
        )
        if elevenlabs_model not in ELEVENLABS_MODELS:
            elevenlabs_model = DEFAULT_ELEVENLABS_MODEL
        azure_speech_model = str(
            merged.get("azure_speech_model", DEFAULT_AZURE_SPEECH_MODEL)
        )
        if azure_speech_model not in AZURE_SPEECH_MODELS:
            azure_speech_model = DEFAULT_AZURE_SPEECH_MODEL
        azure_endpoint = str(
            merged.get("azure_endpoint", DEFAULT_AZURE_ENDPOINT)
        ).strip()
        funasr_model = str(merged.get("funasr_model", DEFAULT_FUNASR_MODEL))
        if funasr_model not in FUNASR_MODELS:
            funasr_model = DEFAULT_FUNASR_MODEL
        start_beep_tone = str(
            merged.get("start_beep_tone", DEFAULT_START_BEEP_TONE)
        ).strip().lower()
        if start_beep_tone not in VALID_START_BEEP_TONES:
            start_beep_tone = DEFAULT_START_BEEP_TONE
        completion_beep_tone = str(
            merged.get("completion_beep_tone", DEFAULT_COMPLETION_BEEP_TONE)
        ).strip().lower()
        if completion_beep_tone not in VALID_START_BEEP_TONES:
            completion_beep_tone = DEFAULT_COMPLETION_BEEP_TONE
        overlay_corner = str(
            merged.get("overlay_corner", DEFAULT_OVERLAY_CORNER)
        ).strip().lower()
        if overlay_corner not in VALID_OVERLAY_CORNERS:
            overlay_corner = DEFAULT_OVERLAY_CORNER
        try:
            recordings_max_count = int(
                merged.get("recordings_max_count", DEFAULT_RECORDINGS_MAX_COUNT)
            )
        except (TypeError, ValueError):
            recordings_max_count = DEFAULT_RECORDINGS_MAX_COUNT
        recordings_max_count = max(1, min(500, recordings_max_count))
        raw_history_max_items = merged.get(
            "history_max_items",
            DEFAULT_HISTORY_MAX_ITEMS,
        )
        parsed_history_max_items = _int_or_none(raw_history_max_items)
        if parsed_history_max_items is None:
            history_max_items = DEFAULT_HISTORY_MAX_ITEMS
        else:
            history_max_items = parsed_history_max_items
        history_max_items = max(0, min(HISTORY_MAX_ITEMS_MAX, history_max_items))
        if (
            raw_schema_version < _HISTORY_RETENTION_SCHEMA_VERSION
            and parsed_history_max_items == _LEGACY_DEFAULT_HISTORY_MAX_ITEMS
        ):
            history_max_items = DEFAULT_HISTORY_MAX_ITEMS
        display_timezone = str(
            merged.get("display_timezone", DEFAULT_DISPLAY_TIMEZONE)
        ).strip().lower()
        if display_timezone not in VALID_DISPLAY_TIMEZONES:
            display_timezone = DEFAULT_DISPLAY_TIMEZONE
        try:
            overlay_opacity_percent = int(
                merged.get(
                    "overlay_opacity_percent",
                    DEFAULT_OVERLAY_OPACITY_PERCENT,
                )
            )
        except (TypeError, ValueError):
            overlay_opacity_percent = DEFAULT_OVERLAY_OPACITY_PERCENT
        overlay_opacity_percent = max(
            OVERLAY_OPACITY_MIN_PERCENT,
            min(OVERLAY_OPACITY_MAX_PERCENT, overlay_opacity_percent),
        )
        try:
            vad_energy_threshold = float(
                merged.get("vad_energy_threshold", DEFAULT_VAD_ENERGY_THRESHOLD)
            )
        except (TypeError, ValueError):
            vad_energy_threshold = DEFAULT_VAD_ENERGY_THRESHOLD
        vad_energy_threshold = max(
            VAD_ENERGY_THRESHOLD_MIN,
            min(VAD_ENERGY_THRESHOLD_MAX, vad_energy_threshold),
        )

        return cls(
            schema_version=CURRENT_SCHEMA_VERSION,
            hotkey=hotkey,
            cancel_hotkey=cancel_hotkey,
            show_overlay_hotkey=show_overlay_hotkey,
            repaste_hotkey=repaste_hotkey,
            model_size=model_size,
            language_mode=language_mode,
            custom_vocabulary=str(
                merged.get("custom_vocabulary", DEFAULT_CUSTOM_VOCABULARY)
            ),
            vad_enabled=parse_json_bool(
                merged.get("vad_enabled"), default=DEFAULT_VAD_ENABLED
            ),
            input_device_name=str(
                merged.get("input_device_name", DEFAULT_INPUT_DEVICE_NAME) or ""
            ).strip(),
            vad_energy_threshold=vad_energy_threshold,
            save_last_wav=parse_json_bool(
                merged.get("save_last_wav"), default=DEFAULT_SAVE_LAST_WAV
            ),
            save_all_recordings=parse_json_bool(
                merged.get("save_all_recordings"),
                default=DEFAULT_SAVE_ALL_RECORDINGS,
            ),
            recordings_dir=str(
                merged.get("recordings_dir", DEFAULT_RECORDINGS_DIR)
            ).strip(),
            recordings_max_count=recordings_max_count,
            history_max_items=history_max_items,
            display_timezone=display_timezone,
            overlay_opacity_percent=overlay_opacity_percent,
            overlay_always_on_top=parse_json_bool(
                merged.get(
                    "overlay_always_on_top",
                    DEFAULT_OVERLAY_ALWAYS_ON_TOP,
                ),
                default=DEFAULT_OVERLAY_ALWAYS_ON_TOP,
            ),
            engine=engine,
            mode=mode,
            streaming_full_final_transcript=parse_json_bool(
                merged.get(
                    "streaming_full_final_transcript",
                    DEFAULT_STREAMING_FULL_FINAL_TRANSCRIPT,
                ),
                default=DEFAULT_STREAMING_FULL_FINAL_TRANSCRIPT,
            ),
            concurrent_transcription_mode=concurrent_transcription_mode,
            immediate_background_insert=parse_json_bool(
                merged.get(
                    "immediate_background_insert",
                    DEFAULT_IMMEDIATE_BACKGROUND_INSERT,
                ),
                default=DEFAULT_IMMEDIATE_BACKGROUND_INSERT,
            ),
            insert_target=insert_target,
            keep_microphone_warm=parse_json_bool(
                merged.get(
                    "keep_microphone_warm",
                    DEFAULT_KEEP_MICROPHONE_WARM,
                ),
                default=DEFAULT_KEEP_MICROPHONE_WARM,
            ),
            silence_gate_enabled=parse_json_bool(
                merged.get(
                    "silence_gate_enabled",
                    DEFAULT_SILENCE_GATE_ENABLED,
                ),
                default=DEFAULT_SILENCE_GATE_ENABLED,
            ),
            silence_gate_threshold=silence_gate_threshold,
            paste_mode=paste_mode,
            keep_transcript_in_clipboard=parse_json_bool(
                merged.get(
                    "keep_transcript_in_clipboard",
                    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
                ),
                default=DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
            ),
            allow_insecure_key_storage=parse_json_bool(
                merged.get(
                    "allow_insecure_key_storage",
                    DEFAULT_ALLOW_INSECURE_KEY_STORAGE,
                ),
                default=DEFAULT_ALLOW_INSECURE_KEY_STORAGE,
            ),
            offline_mode=parse_json_bool(
                merged.get("offline_mode"), default=DEFAULT_OFFLINE_MODE
            ),
            keep_onnx_model_loaded=parse_json_bool(
                merged.get(
                    "keep_onnx_model_loaded",
                    DEFAULT_KEEP_ONNX_MODEL_LOADED,
                ),
                default=DEFAULT_KEEP_ONNX_MODEL_LOADED,
            ),
            start_beep_enabled=parse_json_bool(
                merged.get("start_beep_enabled"),
                default=DEFAULT_START_BEEP_ENABLED,
            ),
            start_beep_tone=start_beep_tone,
            completion_beep_enabled=parse_json_bool(
                merged.get("completion_beep_enabled"),
                default=DEFAULT_COMPLETION_BEEP_ENABLED,
            ),
            completion_beep_tone=completion_beep_tone,
            tray_middle_click_toggle=parse_json_bool(
                merged.get(
                    "tray_middle_click_toggle",
                    DEFAULT_TRAY_MIDDLE_CLICK_TOGGLE,
                ),
                default=DEFAULT_TRAY_MIDDLE_CLICK_TOGGLE,
            ),
            overlay_corner=overlay_corner,
            model_dir=str(merged.get("model_dir", DEFAULT_MODEL_DIR)).strip(),
            has_openai_key=parse_json_bool(merged.get("has_openai_key")),
            has_deepgram_key=parse_json_bool(merged.get("has_deepgram_key")),
            has_assemblyai_key=parse_json_bool(merged.get("has_assemblyai_key")),
            has_groq_key=parse_json_bool(merged.get("has_groq_key")),
            has_elevenlabs_key=parse_json_bool(merged.get("has_elevenlabs_key")),
            has_azure_key=parse_json_bool(merged.get("has_azure_key")),
            has_funasr_key=parse_json_bool(merged.get("has_funasr_key")),
            groq_model=groq_model,
            openai_model=openai_model,
            deepgram_model=deepgram_model,
            assemblyai_model=assemblyai_model,
            elevenlabs_model=elevenlabs_model,
            azure_speech_model=azure_speech_model,
            azure_endpoint=azure_endpoint,
            funasr_model=funasr_model,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = CURRENT_SCHEMA_VERSION
        return data


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = lock_for_path(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AppSettings:
        with self._lock:
            if not self._path.exists():
                settings = AppSettings()
                self.save(settings)
                return settings

            payload, source = load_json_with_backup(
                self._path,
                expected_type=dict,
            )
            if payload is None:
                quarantine_corrupt_file(self._path, include_backup=True)
                settings = AppSettings()
                return settings

            raw = dict(payload)

            settings = AppSettings.from_dict(raw)
            if source == "backup" or raw != settings.to_dict():
                self.save(settings)

            return settings

    def save(self, settings: AppSettings) -> None:
        with self._lock:
            payload = settings.to_dict()

            for secret_key in (
                "openai_api_key",
                "deepgram_api_key",
                "assemblyai_api_key",
                "groq_api_key",
                "elevenlabs_api_key",
                "azure_api_key",
                "funasr_api_key",
            ):
                payload.pop(secret_key, None)

            atomic_write_json(
                self._path,
                payload,
                ensure_ascii=True,
                keep_backup=True,
            )


def _normalize_hotkey(value: str, *, default: str) -> str:
    hotkey = (value or "").strip()
    if not hotkey:
        return default
    try:
        parse_hotkey(hotkey)
    except ValueError:
        return default
    return hotkey


def _normalize_optional_hotkey(value: str, *, default: str) -> str:
    """Normalize a hotkey whose empty value means "deliberately disabled".

    Unlike ``_normalize_hotkey``, an empty value stays empty; only an invalid
    non-empty value falls back to the default combo.
    """
    hotkey = (value or "").strip()
    if not hotkey:
        return ""
    try:
        parse_hotkey(hotkey)
    except ValueError:
        return default
    return hotkey
