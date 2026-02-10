from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .app_paths import settings_path
from .config import (
    DEFAULT_ENGINE,
    DEFAULT_HOTKEY,
    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_SIZE,
    DEFAULT_OFFLINE_MODE,
    DEFAULT_PASTE_MODE,
    DEFAULT_SAVE_LAST_WAV,
    DEFAULT_VAD_ENABLED,
    LEGACY_DEFAULT_HOTKEY,
    PREVIOUS_DEFAULT_HOTKEY,
    SCHEMA_VERSION,
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
    "model_size": DEFAULT_MODEL_SIZE,
    "language_mode": DEFAULT_LANGUAGE_MODE,
    "vad_enabled": DEFAULT_VAD_ENABLED,
    "save_last_wav": DEFAULT_SAVE_LAST_WAV,
    "engine": DEFAULT_ENGINE,
    "mode": DEFAULT_MODE,
    "paste_mode": DEFAULT_PASTE_MODE,
    "keep_transcript_in_clipboard": DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
    "offline_mode": DEFAULT_OFFLINE_MODE,
    "model_dir": DEFAULT_MODEL_DIR,
    "has_openai_key": False,
    "has_azure_key": False,
    "has_deepgram_key": False,
    "has_assemblyai_key": False,
}


@dataclass(slots=True)
class AppSettings:
    schema_version: int = CURRENT_SCHEMA_VERSION
    hotkey: str = DEFAULT_HOTKEY
    model_size: str = DEFAULT_MODEL_SIZE
    language_mode: str = DEFAULT_LANGUAGE_MODE
    vad_enabled: bool = DEFAULT_VAD_ENABLED
    save_last_wav: bool = DEFAULT_SAVE_LAST_WAV
    engine: str = DEFAULT_ENGINE
    mode: str = DEFAULT_MODE
    paste_mode: str = DEFAULT_PASTE_MODE
    keep_transcript_in_clipboard: bool = DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD
    offline_mode: bool = DEFAULT_OFFLINE_MODE
    model_dir: str = DEFAULT_MODEL_DIR
    has_openai_key: bool = False
    has_azure_key: bool = False
    has_deepgram_key: bool = False
    has_assemblyai_key: bool = False

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
        hotkey = _normalize_hotkey(hotkey)

        return cls(
            schema_version=CURRENT_SCHEMA_VERSION,
            hotkey=hotkey,
            model_size=model_size,
            language_mode=language_mode,
            vad_enabled=bool(merged.get("vad_enabled", DEFAULT_VAD_ENABLED)),
            save_last_wav=bool(merged.get("save_last_wav", DEFAULT_SAVE_LAST_WAV)),
            engine=engine,
            mode=mode,
            paste_mode=paste_mode,
            keep_transcript_in_clipboard=bool(
                merged.get(
                    "keep_transcript_in_clipboard",
                    DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD,
                )
            ),
            offline_mode=bool(merged.get("offline_mode", DEFAULT_OFFLINE_MODE)),
            model_dir=str(merged.get("model_dir", DEFAULT_MODEL_DIR)).strip(),
            has_openai_key=bool(merged.get("has_openai_key", False)),
            has_azure_key=bool(merged.get("has_azure_key", False)),
            has_deepgram_key=bool(merged.get("has_deepgram_key", False)),
            has_assemblyai_key=bool(merged.get("has_assemblyai_key", False)),
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

        raw: dict[str, Any]
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, json.JSONDecodeError):
            raw = {}

        migrated = self._migrate(raw)
        settings = AppSettings.from_dict(migrated)

        if raw != migrated or migrated != settings.to_dict():
            self.save(settings)

        return settings

    def save(self, settings: AppSettings) -> None:
        payload = settings.to_dict()

        for secret_key in (
            "openai_api_key",
            "azure_api_key",
            "deepgram_api_key",
            "assemblyai_api_key",
        ):
            payload.pop(secret_key, None)

        self._path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def _migrate(self, raw: dict[str, Any]) -> dict[str, Any]:
        migrated: dict[str, Any] = dict(raw)
        old_schema = int(raw.get("schema_version", 0) or 0)

        if "language" in migrated and "language_mode" not in migrated:
            migrated["language_mode"] = migrated["language"]

        migrated.pop("language", None)
        migrated.pop("openai_api_key", None)
        migrated.pop("azure_api_key", None)
        migrated.pop("deepgram_api_key", None)
        migrated.pop("assemblyai_api_key", None)

        for key, value in DEFAULTS.items():
            migrated.setdefault(key, value)

        # Migrate historical default hotkey to current default.
        # We only auto-change when old schema is detected and value matches the old default.
        if old_schema < CURRENT_SCHEMA_VERSION and str(
            migrated.get("hotkey", "")
        ).strip() in {LEGACY_DEFAULT_HOTKEY, PREVIOUS_DEFAULT_HOTKEY}:
            migrated["hotkey"] = DEFAULT_HOTKEY

        migrated["schema_version"] = CURRENT_SCHEMA_VERSION
        return migrated


def _normalize_hotkey(value: str) -> str:
    hotkey = (value or "").strip()
    if not hotkey:
        return DEFAULT_HOTKEY
    try:
        parse_hotkey(hotkey)
    except ValueError:
        return DEFAULT_HOTKEY
    return hotkey
