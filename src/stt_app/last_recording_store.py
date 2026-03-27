from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_paths import debug_audio_path, last_recording_state_path
from .persistence import (
    atomic_write_bytes,
    atomic_write_json,
    load_json_with_backup,
    quarantine_corrupt_file,
)

RECOVERABLE_RECORDING_STATUSES = {
    "captured",
    "transcribing",
    "failed",
    "canceled",
}
_CURRENT_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class LastRecordingState:
    schema_version: int = _CURRENT_SCHEMA_VERSION
    audio_path: str = ""
    created_at: str = ""
    status: str = "captured"
    keep_after_success: bool = False
    engine: str = ""
    model: str = ""
    mode: str = ""
    error: str = ""
    transcription_started_at: str = ""
    completed_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LastRecordingState":
        status = str(raw.get("status", "captured")).strip().lower() or "captured"
        if status not in RECOVERABLE_RECORDING_STATUSES | {"completed"}:
            status = "captured"
        return cls(
            schema_version=_CURRENT_SCHEMA_VERSION,
            audio_path=str(raw.get("audio_path", "")).strip(),
            created_at=str(raw.get("created_at", "")).strip(),
            status=status,
            keep_after_success=bool(raw.get("keep_after_success", False)),
            engine=str(raw.get("engine", "")).strip(),
            model=str(raw.get("model", "")).strip(),
            mode=str(raw.get("mode", "")).strip(),
            error=str(raw.get("error", "")).strip(),
            transcription_started_at=str(
                raw.get("transcription_started_at", "")
            ).strip(),
            completed_at=str(raw.get("completed_at", "")).strip(),
        )


class LastRecordingStore:
    def __init__(
        self,
        *,
        audio_path: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._audio_path = audio_path or debug_audio_path()
        self._state_path = state_path or last_recording_state_path()
        self._audio_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def audio_path(self) -> Path:
        return self._audio_path

    @property
    def state_path(self) -> Path:
        return self._state_path

    def load(self) -> LastRecordingState | None:
        if not self._state_path.exists():
            return self._orphaned_audio_state()
        payload, _source = load_json_with_backup(self._state_path, expected_type=dict)
        if payload is None:
            if self._state_path.exists():
                quarantine_corrupt_file(self._state_path)
            return self._orphaned_audio_state()
        state = LastRecordingState.from_dict(payload)
        if not state.audio_path:
            state.audio_path = str(self._audio_path)
        return state

    def save_recording(
        self,
        wav_bytes: bytes,
        *,
        keep_after_success: bool,
    ) -> LastRecordingState:
        atomic_write_bytes(self._audio_path, bytes(wav_bytes))
        state = LastRecordingState(
            audio_path=str(self._audio_path),
            created_at=_utc_now(),
            status="captured",
            keep_after_success=bool(keep_after_success),
        )
        self._save_state(state)
        return state

    def mark_transcribing(
        self,
        *,
        engine: str,
        model: str,
        mode: str,
    ) -> None:
        state = self.load()
        if state is None:
            if not self._audio_path.is_file():
                return
            state = LastRecordingState(
                audio_path=str(self._audio_path),
                created_at=_utc_now(),
            )
        state.status = "transcribing"
        state.engine = str(engine or "").strip()
        state.model = str(model or "").strip()
        state.mode = str(mode or "").strip()
        state.error = ""
        state.transcription_started_at = _utc_now()
        self._save_state(state)

    def mark_failed(self, error: str) -> None:
        state = self.load()
        if state is None:
            return
        state.status = "failed"
        state.error = str(error or "").strip()
        self._save_state(state)

    def mark_canceled(self, detail: str = "") -> None:
        state = self.load()
        if state is None:
            return
        state.status = "canceled"
        state.error = str(detail or "").strip()
        self._save_state(state)

    def mark_completed(self) -> None:
        state = self.load()
        if state is None:
            return
        if state.keep_after_success:
            state.status = "completed"
            state.error = ""
            state.completed_at = _utc_now()
            self._save_state(state)
            return
        self.clear()

    def selectable_path(self) -> Path | None:
        if not self._audio_path.is_file():
            return None
        return self._audio_path

    def has_recoverable_recording(self) -> bool:
        state = self.load()
        if state is None:
            return False
        if state.status not in RECOVERABLE_RECORDING_STATUSES:
            return False
        return self._audio_path.is_file()

    def is_managed_audio_path(self, path: str | Path) -> bool:
        try:
            candidate = Path(path).resolve()
        except OSError:
            candidate = Path(path)
        try:
            managed = self._audio_path.resolve()
        except OSError:
            managed = self._audio_path
        return candidate == managed

    def clear(self) -> None:
        try:
            self._audio_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self._state_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _save_state(self, state: LastRecordingState) -> None:
        atomic_write_json(
            self._state_path,
            asdict(state),
            ensure_ascii=True,
            keep_backup=True,
        )

    def _orphaned_audio_state(self) -> LastRecordingState | None:
        if not self._audio_path.is_file():
            return None
        return LastRecordingState(
            audio_path=str(self._audio_path),
            created_at="",
            status="captured",
        )
