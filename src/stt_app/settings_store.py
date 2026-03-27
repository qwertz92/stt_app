from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .app_paths import settings_path
from .persistence import (
    atomic_write_json,
    load_json_with_backup,
    quarantine_corrupt_file,
)
from .config import (
    DEFAULT_ALLOW_INSECURE_KEY_STORAGE,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_ENGINE,
    DEFAULT_GROQ_MODEL,
    DEFAULT_HOTKEY,
    DEFAULT_HISTORY_MAX_ITEMS,
    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_SIZE,
    DEFAULT_OFFLINE_MODE,
    DEFAULT_OVERLAY_OPACITY_PERCENT,
    DEFAULT_OVERLAY_CORNER,
    DEFAULT_PASTE_MODE,
    DEFAULT_RECORDINGS_DIR,
    DEFAULT_RECORDINGS_MAX_COUNT,
    DEFAULT_SAVE_ALL_RECORDINGS,
    DEFAULT_SAVE_LAST_WAV,
    DEFAULT_START_BEEP_ENABLED,
    DEFAULT_START_BEEP_TONE,
    DEFAULT_VAD_ENERGY_THRESHOLD,
    DEFAULT_VAD_ENABLED,
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

DEFAULTS = {
    "schema_version": CURRENT_SCHEMA_VERSION,
    "hotkey": DEFAULT_HOTKEY,
    "cancel_hotkey": DEFAULT_CANCEL_HOTKEY,
    "model_size": DEFAULT_MODEL_SIZE,
    "language_mode": DEFAULT_LANGUAGE_MODE,
    "vad_enabled": DEFAULT_VAD_ENABLED,
    "vad_energy_threshold": DEFAULT_VAD_ENERGY_THRESHOLD,
    "save_last_wav": DEFAULT_SAVE_LAST_WAV,
    "save_all_recordings": DEFAULT_SAVE_ALL_RECORDINGS,
    "recordings_dir": DEFAULT_RECORDINGS_DIR,
    "recordings_max_count": DEFAULT_RECORDINGS_MAX_COUNT,
    "history_max_items": DEFAULT_HISTORY_MAX_ITEMS,
    "overlay_opacity_percent": DEFAULT_OVERLAY_OPACITY_PERCENT,
    "engine": DEFAULT_ENGINE,
    "mode": DEFAULT_MODE,
    "paste_mode": DEFAULT_PASTE_MODE,
    "keep_transcript_in_clipboard": DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    "allow_insecure_key_storage": DEFAULT_ALLOW_INSECURE_KEY_STORAGE,
    "offline_mode": DEFAULT_OFFLINE_MODE,
    "start_beep_enabled": DEFAULT_START_BEEP_ENABLED,
    "start_beep_tone": DEFAULT_START_BEEP_TONE,
    "overlay_corner": DEFAULT_OVERLAY_CORNER,
    "model_dir": DEFAULT_MODEL_DIR,
    "has_openai_key": False,
    "has_deepgram_key": False,
    "has_assemblyai_key": False,
    "has_groq_key": False,
    "groq_model": DEFAULT_GROQ_MODEL,
    "openai_model": DEFAULT_OPENAI_MODEL,
}


@dataclass(slots=True)
class AppSettings:
    schema_version: int = CURRENT_SCHEMA_VERSION
    hotkey: str = DEFAULT_HOTKEY
    cancel_hotkey: str = DEFAULT_CANCEL_HOTKEY
    model_size: str = DEFAULT_MODEL_SIZE
    language_mode: str = DEFAULT_LANGUAGE_MODE
    vad_enabled: bool = DEFAULT_VAD_ENABLED
    vad_energy_threshold: float = DEFAULT_VAD_ENERGY_THRESHOLD
    save_last_wav: bool = DEFAULT_SAVE_LAST_WAV
    save_all_recordings: bool = DEFAULT_SAVE_ALL_RECORDINGS
    recordings_dir: str = DEFAULT_RECORDINGS_DIR
    recordings_max_count: int = DEFAULT_RECORDINGS_MAX_COUNT
    history_max_items: int = DEFAULT_HISTORY_MAX_ITEMS
    overlay_opacity_percent: int = DEFAULT_OVERLAY_OPACITY_PERCENT
    engine: str = DEFAULT_ENGINE
    mode: str = DEFAULT_MODE
    paste_mode: str = DEFAULT_PASTE_MODE
    keep_transcript_in_clipboard: bool = DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD
    allow_insecure_key_storage: bool = DEFAULT_ALLOW_INSECURE_KEY_STORAGE
    offline_mode: bool = DEFAULT_OFFLINE_MODE
    start_beep_enabled: bool = DEFAULT_START_BEEP_ENABLED
    start_beep_tone: str = DEFAULT_START_BEEP_TONE
    overlay_corner: str = DEFAULT_OVERLAY_CORNER
    model_dir: str = DEFAULT_MODEL_DIR
    has_openai_key: bool = False
    has_deepgram_key: bool = False
    has_assemblyai_key: bool = False
    has_groq_key: bool = False
    groq_model: str = DEFAULT_GROQ_MODEL
    openai_model: str = DEFAULT_OPENAI_MODEL

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppSettings":
        merged: dict[str, Any] = dict(DEFAULTS)
        merged.update(raw)

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

        model_size = str(merged.get("model_size", DEFAULT_MODEL_SIZE)).lower()
        if model_size not in VALID_MODEL_SIZES:
            model_size = DEFAULT_MODEL_SIZE

        hotkey = str(merged.get("hotkey", DEFAULT_HOTKEY))
        hotkey = _normalize_hotkey(hotkey, default=DEFAULT_HOTKEY)
        cancel_hotkey = str(merged.get("cancel_hotkey", DEFAULT_CANCEL_HOTKEY))
        cancel_hotkey = _normalize_hotkey(
            cancel_hotkey, default=DEFAULT_CANCEL_HOTKEY
        )

        groq_model = str(merged.get("groq_model", DEFAULT_GROQ_MODEL))
        if groq_model not in GROQ_MODELS:
            groq_model = DEFAULT_GROQ_MODEL
        openai_model = str(merged.get("openai_model", DEFAULT_OPENAI_MODEL))
        if openai_model not in OPENAI_MODELS:
            openai_model = DEFAULT_OPENAI_MODEL
        start_beep_tone = str(
            merged.get("start_beep_tone", DEFAULT_START_BEEP_TONE)
        ).strip().lower()
        if start_beep_tone not in VALID_START_BEEP_TONES:
            start_beep_tone = DEFAULT_START_BEEP_TONE
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
        try:
            history_max_items = int(
                merged.get("history_max_items", DEFAULT_HISTORY_MAX_ITEMS)
            )
        except (TypeError, ValueError):
            history_max_items = DEFAULT_HISTORY_MAX_ITEMS
        history_max_items = max(0, min(HISTORY_MAX_ITEMS_MAX, history_max_items))
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
            model_size=model_size,
            language_mode=language_mode,
            vad_enabled=bool(merged.get("vad_enabled", DEFAULT_VAD_ENABLED)),
            vad_energy_threshold=vad_energy_threshold,
            save_last_wav=bool(merged.get("save_last_wav", DEFAULT_SAVE_LAST_WAV)),
            save_all_recordings=bool(
                merged.get("save_all_recordings", DEFAULT_SAVE_ALL_RECORDINGS)
            ),
            recordings_dir=str(
                merged.get("recordings_dir", DEFAULT_RECORDINGS_DIR)
            ).strip(),
            recordings_max_count=recordings_max_count,
            history_max_items=history_max_items,
            overlay_opacity_percent=overlay_opacity_percent,
            engine=engine,
            mode=mode,
            paste_mode=paste_mode,
            keep_transcript_in_clipboard=bool(
                merged.get(
                    "keep_transcript_in_clipboard",
                    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
                )
            ),
            allow_insecure_key_storage=bool(
                merged.get(
                    "allow_insecure_key_storage",
                    DEFAULT_ALLOW_INSECURE_KEY_STORAGE,
                )
            ),
            offline_mode=bool(merged.get("offline_mode", DEFAULT_OFFLINE_MODE)),
            start_beep_enabled=bool(
                merged.get("start_beep_enabled", DEFAULT_START_BEEP_ENABLED)
            ),
            start_beep_tone=start_beep_tone,
            overlay_corner=overlay_corner,
            model_dir=str(merged.get("model_dir", DEFAULT_MODEL_DIR)).strip(),
            has_openai_key=bool(merged.get("has_openai_key", False)),
            has_deepgram_key=bool(merged.get("has_deepgram_key", False)),
            has_assemblyai_key=bool(merged.get("has_assemblyai_key", False)),
            has_groq_key=bool(merged.get("has_groq_key", False)),
            groq_model=groq_model,
            openai_model=openai_model,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = CURRENT_SCHEMA_VERSION
        return data


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AppSettings:
        if not self._path.exists():
            settings = AppSettings()
            self.save(settings)
            return settings

        payload, source = load_json_with_backup(self._path, expected_type=dict)
        if payload is None:
            quarantine_corrupt_file(self._path)
            settings = AppSettings()
            return settings

        raw = dict(payload)

        settings = AppSettings.from_dict(raw)
        if source == "backup" or raw != settings.to_dict():
            self.save(settings)

        return settings

    def save(self, settings: AppSettings) -> None:
        payload = settings.to_dict()

        for secret_key in (
            "openai_api_key",
            "deepgram_api_key",
            "assemblyai_api_key",
            "groq_api_key",
        ):
            payload.pop(secret_key, None)

        atomic_write_json(self._path, payload, ensure_ascii=True, keep_backup=True)


def _normalize_hotkey(value: str, *, default: str) -> str:
    hotkey = (value or "").strip()
    if not hotkey:
        return default
    try:
        parse_hotkey(hotkey)
    except ValueError:
        return default
    return hotkey
