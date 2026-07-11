from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .app_paths import debug_audio_path, last_recording_state_path
from .persistence import (
    atomic_write_bytes,
    atomic_write_json,
    load_json_with_backup,
    lock_for_path,
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
    recording_id: str = ""
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
            recording_id=str(
                raw.get("recording_id", raw.get("created_at", ""))
            ).strip(),
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


@dataclass(frozen=True, slots=True)
class ManagedRecordingSnapshot:
    """Immutable audio and identity captured for a managed-file import."""

    audio_bytes: bytes
    recording_id: str


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
        self._lock = lock_for_path(self._state_path)

    @property
    def audio_path(self) -> Path:
        return self._audio_path

    @property
    def state_path(self) -> Path:
        return self._state_path

    def load(self) -> LastRecordingState | None:
        with self._lock:
            if not self._state_path.exists():
                return self._orphaned_audio_state()
            payload, _source = load_json_with_backup(
                self._state_path,
                expected_type=dict,
            )
            if payload is None:
                quarantine_corrupt_file(self._state_path, include_backup=True)
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
        with self._lock:
            atomic_write_bytes(self._audio_path, bytes(wav_bytes))
            state = LastRecordingState(
                audio_path=str(self._audio_path),
                recording_id=uuid4().hex,
                created_at=_utc_now(),
                status="captured",
                keep_after_success=bool(keep_after_success),
            )
            self._save_state(state)
            return state

    def snapshot_managed_recording(
        self,
        path: str | Path,
    ) -> ManagedRecordingSnapshot | None:
        """Snapshot managed audio and its ID before an asynchronous import."""
        if not self.is_managed_audio_path(path):
            return None
        with self._lock:
            state = self.load()
            if state is None or not self._audio_path.is_file():
                return None
            if not state.recording_id:
                state.recording_id = uuid4().hex
                if not state.created_at:
                    state.created_at = _utc_now()
                state.audio_path = str(self._audio_path)
                self._save_state(state)
            try:
                audio_bytes = self._audio_path.read_bytes()
            except OSError:
                return None
            return ManagedRecordingSnapshot(
                audio_bytes=audio_bytes,
                recording_id=state.recording_id,
            )

    def mark_transcribing(
        self,
        *,
        engine: str,
        model: str,
        mode: str,
        expected_recording_id: str | None = None,
    ) -> bool:
        with self._lock:
            state = self.load()
            if state is None:
                if expected_recording_id is not None or not self._audio_path.is_file():
                    return False
                state = LastRecordingState(
                    audio_path=str(self._audio_path),
                    recording_id=uuid4().hex,
                    created_at=_utc_now(),
                )
            elif expected_recording_id is not None and (
                state.recording_id != expected_recording_id
            ):
                return False
            elif not state.recording_id:
                state.recording_id = uuid4().hex
            state.status = "transcribing"
            state.engine = str(engine or "").strip()
            state.model = str(model or "").strip()
            state.mode = str(mode or "").strip()
            state.error = ""
            state.transcription_started_at = _utc_now()
            self._save_state(state)
            return True

    def mark_failed(
        self,
        error: str,
        *,
        expected_recording_id: str | None = None,
    ) -> bool:
        with self._lock:
            state = self.load()
            if state is None or (
                expected_recording_id is not None
                and state.recording_id != expected_recording_id
            ):
                return False
            state.status = "failed"
            state.error = str(error or "").strip()
            self._save_state(state)
            return True

    def mark_canceled(
        self,
        detail: str = "",
        *,
        expected_recording_id: str | None = None,
    ) -> bool:
        with self._lock:
            state = self.load()
            if state is None or (
                expected_recording_id is not None
                and state.recording_id != expected_recording_id
            ):
                return False
            state.status = "canceled"
            state.error = str(detail or "").strip()
            self._save_state(state)
            return True

    def mark_completed(
        self,
        *,
        expected_recording_id: str | None = None,
    ) -> bool:
        with self._lock:
            state = self.load()
            if state is None or (
                expected_recording_id is not None
                and state.recording_id != expected_recording_id
            ):
                return False
            if state.keep_after_success:
                state.status = "completed"
                state.error = ""
                state.completed_at = _utc_now()
                self._save_state(state)
                return True
            return self.clear(expected_recording_id=state.recording_id)

    def selectable_path(
        self,
        archived_recordings_dir: str | Path | None = None,
    ) -> Path | None:
        with self._lock:
            candidates: list[Path] = []
            if self._audio_path.is_file():
                if self._managed_recording_is_recoverable():
                    return self._audio_path
                candidates.append(self._audio_path)
            archived = self._latest_archived_recording(archived_recordings_dir)
            if archived is not None and not self.is_managed_audio_path(archived):
                candidates.append(archived)
            if not candidates:
                return None
            return max(candidates, key=self._recording_sort_key)

    def _managed_recording_is_recoverable(self) -> bool:
        state = self.load()
        if state is None:
            return False
        return state.status in RECOVERABLE_RECORDING_STATUSES

    @staticmethod
    def _latest_archived_recording(
        archived_recordings_dir: str | Path | None,
    ) -> Path | None:
        if archived_recordings_dir is None:
            return None
        root = Path(archived_recordings_dir)
        if not root.is_dir():
            return None
        try:
            candidates = [
                path
                for path in root.iterdir()
                if path.is_file() and path.suffix.lower() == ".wav"
            ]
        except OSError:
            return None
        if not candidates:
            return None
        return max(candidates, key=LastRecordingStore._recording_sort_key)

    @staticmethod
    def _recording_sort_key(path: Path) -> tuple[int, str]:
        try:
            stat = path.stat()
        except OSError:
            return (0, path.name)
        mtime_ns = getattr(
            stat,
            "st_mtime_ns",
            int(stat.st_mtime * 1_000_000_000),
        )
        return (mtime_ns, path.name)

    def has_recoverable_recording(self) -> bool:
        with self._lock:
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

    def clear(self, *, expected_recording_id: str | None = None) -> bool:
        with self._lock:
            if expected_recording_id is not None:
                state = self.load()
                if state is None or state.recording_id != expected_recording_id:
                    return False
            try:
                self._audio_path.unlink(missing_ok=True)
            except OSError:
                # Preserve the state file so the recording remains discoverable
                # and a later retry can finish the cleanup.
                return False
            try:
                self._state_path.unlink(missing_ok=True)
            except OSError:
                return False
            return True

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
            recording_id="",
            created_at="",
            status="captured",
        )
