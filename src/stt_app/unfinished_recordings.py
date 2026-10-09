"""Recordings an earlier session did not transcribe, kept for the next start.

The managed last recording (`last_recording_store`) is one slot that every new
recording overwrites, so it can carry the newest unfinished recording at most.
A quit with several recordings in the queue, or with failed recordings waiting
for Retry, used to drop all the others: their audio lived only in memory.

This store is a directory of WAV files and nothing else. Each file name
carries the recording's time and id, and the duration and size come from the
file itself, so there is no index file that could be unreadable, damaged or
out of step with the directory. A save always writes a new file and never
replaces one, so nothing written earlier can be lost to a later write.
"""

from __future__ import annotations

import re
import shutil
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .app_paths import unfinished_recordings_dir
from .persistence import atomic_write_bytes

_STAMP_FORMAT = "%Y%m%d_%H%M%S"
_NAME_RE = re.compile(
    r"^unfinished_(?P<stamp>[0-9]{8}_[0-9]{6})_(?P<id>[A-Za-z0-9-]+)\.wav$",
    re.IGNORECASE,
)
_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9-]+")


@dataclass(frozen=True, slots=True)
class UnfinishedRecording:
    path: Path
    recording_id: str
    # Local wall-clock time the recording ended, as the queue rows show it.
    recorded_at: datetime | None
    duration_s: float | None
    size_bytes: int | None


def safe_recording_id(recording_id: str) -> str:
    """The id as it can stand in a file name ("" when nothing is left)."""
    return _ID_UNSAFE_RE.sub("-", str(recording_id or "").strip()).strip("-")


def wav_duration_seconds(path: Path) -> float | None:
    """The WAV header's length, or None for a file that is not a readable WAV."""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
    except (OSError, EOFError, wave.Error):
        return None
    if rate <= 0:
        return None
    return frames / float(rate)


class UnfinishedRecordingStore:
    def __init__(self, directory: Path | None = None) -> None:
        # Resolved lazily: building a controller must not create folders.
        self._directory = directory

    @property
    def directory(self) -> Path:
        if self._directory is None:
            self._directory = unfinished_recordings_dir()
        return self._directory

    def save(
        self,
        wav_bytes: bytes,
        *,
        recording_id: str,
        recorded_at: datetime,
    ) -> Path | None:
        """Keep one recording; None when the same recording is already kept.

        Raises `OSError` when the file cannot be written.
        """
        if not wav_bytes:
            return None
        safe_id = safe_recording_id(recording_id)
        if not safe_id:
            raise ValueError("a recording id is required")
        if self.find(safe_id) is not None:
            return None
        stamp = recorded_at.strftime(_STAMP_FORMAT)
        path = self.directory / f"unfinished_{stamp}_{safe_id}.wav"
        atomic_write_bytes(path, bytes(wav_bytes))
        return path

    def find(self, recording_id: str) -> UnfinishedRecording | None:
        safe_id = safe_recording_id(recording_id)
        if not safe_id:
            return None
        try:
            recordings = self.list_recordings()
        except OSError:
            return None
        for recording in recordings:
            if recording.recording_id == safe_id:
                return recording
        return None

    def list_recordings(self) -> list[UnfinishedRecording]:
        """Every kept recording, oldest first.

        An absent directory is an empty store; one that exists and cannot be
        listed raises `OSError`, because "nothing kept" would be a wrong answer.
        """
        directory = self.directory
        if not directory.exists():
            return []
        recordings = []
        for path in directory.iterdir():
            match = _NAME_RE.fullmatch(path.name)
            if match is None or not path.is_file():
                continue
            recordings.append(_describe(path, match))
        recordings.sort(key=lambda item: item.path.name.lower())
        return recordings

    def discard(self, recording: UnfinishedRecording) -> None:
        """Delete one kept recording. Raises `OSError` when that fails."""
        recording.path.unlink(missing_ok=True)

    def move_to(self, recording: UnfinishedRecording, directory: Path) -> Path:
        """Move one kept recording out of the store, never over another file.

        Returns where it went. Raises `OSError` when the move fails; the file
        then stays where it was.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / recording.path.name
        counter = 1
        while target.exists():
            counter += 1
            target = directory / f"{recording.path.stem}_{counter}.wav"
        return Path(shutil.move(str(recording.path), str(target)))


def _describe(path: Path, match: re.Match[str]) -> UnfinishedRecording:
    try:
        recorded_at = datetime.strptime(  # noqa: DTZ007 (local time on purpose: the name is the user's wall clock, like the archive's)
            match.group("stamp"), _STAMP_FORMAT
        )
    except ValueError:
        recorded_at = None
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = None
    return UnfinishedRecording(
        path=path,
        recording_id=match.group("id"),
        recorded_at=recorded_at,
        duration_s=wav_duration_seconds(path),
        size_bytes=size_bytes,
    )
