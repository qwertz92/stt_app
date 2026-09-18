"""Hold real exclusive Windows file locks on the app's stores and drive them.

WHAT IT CHECKS
An antivirus scan, a backup tool or a sync client opens a file without
sharing it; every other open then fails with ERROR_SHARING_VIOLATION
(winerror 32). Every store in this app is read-modify-write, so a read that
answers "nothing there" while the file is merely unreadable is how a locked
file gets overwritten with an empty one. The rule the app follows is: while
the last read did not reach the file, nothing is written, nothing is moved
aside and nothing is deleted -- and everything is intact and writable again
once the lock is gone. This script checks that rule on the transcript
history, the settings file and the last-recording state, 23 checks in total.

WHY A FAKE CANNOT REPLACE IT
The repository's own tests imitate the lock by patching `Path.open` to raise.
That proves the code's reaction to an exception it was handed; it cannot
prove that a real Windows share-mode-0 handle produces that exception, at
that call, on every path the store takes -- including `os.replace`, the
backup read and the quarantine rename, none of which go through `Path.open`.
This script takes the real lock with `CreateFileW(..., dwShareMode=0, ...)`
in this very process, so the failure is the operating system's.

WHAT IT TOUCHES
Nothing outside a throwaway folder under %TEMP%. `APPDATA` and
`LOCALAPPDATA` are pointed there before the first `stt_app` import, so the
real `%APPDATA%\\stt_app` is unreachable for the whole run. No network, no
foreground window, no clipboard. It needs Windows and nothing else.

HOW LONG IT TAKES
A few seconds.

COMMAND LINE
    .venv\\Scripts\\python.exe scripts\\release_check_file_locks.py
    .venv\\Scripts\\python.exe scripts\\release_check_file_locks.py --report out.json
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_check_common as common

if sys.platform != "win32":
    sys.stdout.write("PREREQUISITE this check needs Windows file locking\n")
    raise SystemExit(common.EXIT_PREREQUISITE)

SANDBOX = common.make_sandbox("stt_release_locks_")
common.redirect_this_process_to(SANDBOX)
common.add_src_to_sys_path()

from stt_app.last_recording_store import LastRecordingStore  # noqa: E402
from stt_app.persistence import StoreUnavailableError, backup_path  # noqa: E402
from stt_app.settings_store import AppSettings, SettingsStore  # noqa: E402
from stt_app.transcript_history import (  # noqa: E402
    TranscriptHistoryEntry,
    TranscriptHistoryStore,
)

LOG_LINES: list[str] = []


class _Collector(logging.Handler):
    """Keep the app's own log so the report can show what it said."""

    def emit(self, record: logging.LogRecord) -> None:
        LOG_LINES.append(f"{record.levelname} {record.getMessage()[:240]}")


logging.getLogger().addHandler(_Collector())
logging.getLogger().setLevel(logging.INFO)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class ExclusiveLock:
    """Open every path with share mode 0: no other handle can be opened.

    This is the shape an antivirus scan or a backup tool holds a file in, and
    it is the whole point of the script, so it stays here rather than in the
    shared module: nothing else needs it.
    """

    def __init__(self, *paths: Path) -> None:
        self._paths = paths
        self._handles: list[int] = []

    def __enter__(self) -> ExclusiveLock:
        for path in self._paths:
            handle = kernel32.CreateFileW(
                str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None
            )
            if handle == INVALID_HANDLE_VALUE or handle is None:
                raise OSError(ctypes.get_last_error(), f"cannot lock {path}")
            self._handles.append(handle)
        return self

    def __exit__(self, *_exc: object) -> None:
        for handle in self._handles:
            kernel32.CloseHandle(handle)
        self._handles.clear()


def listing(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir())


def raised(call) -> str:
    """Run `call` and describe what came out, without letting it escape."""
    try:
        call()
    except StoreUnavailableError as exc:
        return f"StoreUnavailableError: {exc}"[:300]
    except Exception as exc:  # the probe reports whatever came out
        return f"{type(exc).__name__}: {exc}"[:300]
    return "no exception"


def entry(index: int) -> TranscriptHistoryEntry:
    return TranscriptHistoryEntry.new(
        text=f"probe transcript {index}", engine="local", model="probe", mode="batch"
    )


def history_part(checks: common.Checks) -> None:
    folder = SANDBOX / "history"
    folder.mkdir()
    path = folder / "transcript_history.json"
    store = TranscriptHistoryStore(path)
    for index in range(5):
        store.add_entry(entry(index), max_items=500)
    before_bytes = path.read_bytes()
    before_backup = backup_path(path).read_bytes()
    before_listing = listing(folder)
    checks.verdict(
        "history.five_entries_stored_with_a_backup_beside_them",
        store.count() == 5 and backup_path(path).exists(),
        before_listing,
    )

    # 1. Primary AND backup locked: the content is unknown, so nothing may be
    #    written, moved aside or added.
    with ExclusiveLock(path, backup_path(path)):
        direct = raised(path.read_bytes)
        checks.verdict(
            "history.locked.the_os_really_refuses_a_second_open",
            direct.startswith("PermissionError"),
            direct,
        )
        fresh = TranscriptHistoryStore(path)
        loaded = fresh.load()
        checks.verdict(
            "history.locked.load_answers_empty_in_memory", loaded == [], len(loaded)
        )
        outcome = raised(lambda: fresh.add_entry(entry(99), max_items=500))
        checks.verdict(
            "history.locked.add_entry_is_refused",
            outcome.startswith("StoreUnavailableError"),
            outcome,
        )
        outcome = raised(fresh.clear)
        checks.verdict(
            "history.locked.clear_is_refused",
            outcome.startswith("StoreUnavailableError"),
            outcome,
        )
        export_target = folder / "export.json"
        export_target.write_text('["previous export"]', encoding="utf-8")
        outcome = raised(lambda: fresh.export_to_file(export_target))
        checks.verdict(
            "history.locked.export_is_refused_and_the_old_export_survives",
            outcome.startswith("StoreUnavailableError")
            and export_target.read_text(encoding="utf-8") == '["previous export"]',
            outcome,
        )
        export_target.unlink()
        checks.verdict(
            "history.locked.no_file_was_moved_aside_or_added",
            listing(folder) == before_listing,
            listing(folder),
        )

    checks.verdict(
        "history.unlocked.bytes_of_primary_and_backup_unchanged",
        path.read_bytes() == before_bytes
        and backup_path(path).read_bytes() == before_backup,
        f"{len(before_bytes)} bytes primary",
    )
    again = TranscriptHistoryStore(path)
    again.add_entry(entry(6), max_items=500)
    checks.verdict(
        "history.unlocked.all_five_are_back_and_a_sixth_can_be_added",
        again.count() == 6,
        again.count(),
    )

    # 2. Only the primary locked: the backup answers, and nothing is
    #    republished or moved while the primary's content is unknown.
    before_bytes = path.read_bytes()
    before_listing = listing(folder)
    with ExclusiveLock(path):
        fresh = TranscriptHistoryStore(path)
        loaded = fresh.load()
        checks.verdict(
            "history.primary_locked.the_backups_entries_are_answered",
            len(loaded) >= 5,
            len(loaded),
        )
        outcome = raised(lambda: fresh.add_entry(entry(7), max_items=500))
        checks.verdict(
            "history.primary_locked.a_write_is_refused",
            outcome.startswith("StoreUnavailableError"),
            outcome,
        )
        checks.verdict(
            "history.primary_locked.nothing_moved_aside",
            listing(folder) == before_listing,
            listing(folder),
        )
    checks.verdict(
        "history.primary_unlocked.primary_bytes_unchanged",
        path.read_bytes() == before_bytes,
        f"{len(before_bytes)} bytes",
    )


def settings_part(checks: common.Checks) -> None:
    folder = SANDBOX / "settings"
    folder.mkdir()
    path = folder / "settings.json"
    store = SettingsStore(path)
    custom = replace(AppSettings(), overlay_opacity_percent=55, history_max_items=777)
    store.save(custom)
    before_bytes = path.read_bytes()
    before_listing = listing(folder)
    with ExclusiveLock(path, backup_path(path)):
        fresh = SettingsStore(path)
        loaded = fresh.load()
        checks.verdict(
            "settings.locked.load_answers_defaults_so_the_app_can_start",
            loaded.history_max_items == AppSettings().history_max_items,
            loaded.history_max_items,
        )
        outcome = raised(
            lambda: fresh.save(replace(loaded, overlay_opacity_percent=90))
        )
        checks.verdict(
            "settings.locked.save_is_refused",
            outcome.startswith("StoreUnavailableError"),
            outcome,
        )
        checks.verdict(
            "settings.locked.nothing_moved_aside_or_added",
            listing(folder) == before_listing,
            listing(folder),
        )
    checks.verdict(
        "settings.unlocked.file_bytes_unchanged",
        path.read_bytes() == before_bytes,
        f"{len(before_bytes)} bytes",
    )
    reloaded = SettingsStore(path).load()
    checks.verdict(
        "settings.unlocked.the_users_values_are_back",
        (reloaded.overlay_opacity_percent, reloaded.history_max_items) == (55, 777),
        (reloaded.overlay_opacity_percent, reloaded.history_max_items),
    )


def last_recording_part(checks: common.Checks) -> None:
    folder = SANDBOX / "recording"
    folder.mkdir()
    audio = folder / "last_recording.wav"
    state_path = folder / "last_recording.json"
    store = LastRecordingStore(audio_path=audio, state_path=state_path)
    state = store.save_recording(b"RIFF" + b"\x00" * 4000, keep_after_success=True)
    before_listing = listing(folder)
    with ExclusiveLock(state_path, backup_path(state_path)):
        fresh = LastRecordingStore(audio_path=audio, state_path=state_path)
        outcome = raised(
            lambda: fresh.mark_completed(expected_recording_id=state.recording_id)
        )
        checks.verdict(
            "last_recording.locked.mark_completed_is_refused",
            outcome.startswith("StoreUnavailableError"),
            outcome,
        )
        checks.verdict(
            "last_recording.locked.the_audio_file_is_still_there",
            audio.exists() and listing(folder) == before_listing,
            listing(folder),
        )
    fresh = LastRecordingStore(audio_path=audio, state_path=state_path)
    completed = fresh.mark_completed(expected_recording_id=state.recording_id)
    loaded = fresh.load()
    checks.verdict(
        "last_recording.unlocked.completion_works_and_keeps_the_audio",
        completed
        and loaded is not None
        and loaded.status == "completed"
        and audio.exists(),
        None if loaded is None else loaded.status,
    )

    # The audio file itself held by another program while a new recording is
    # saved: the save must fail loudly rather than report a recording it did
    # not keep, and it must not leave its temp file behind.
    with ExclusiveLock(audio):
        outcome = raised(
            lambda: fresh.save_recording(
                b"RIFF" + b"\x01" * 4000, keep_after_success=True
            )
        )
        checks.verdict(
            "last_recording.audio_held_by_another_program.save_fails_loudly",
            outcome != "no exception",
            outcome,
        )
    leftovers = [name for name in listing(folder) if name.endswith(".tmp")]
    checks.verdict(
        "last_recording.no_temp_file_left_behind_after_the_failed_save",
        not leftovers,
        listing(folder),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common.add_report_argument(parser)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    checks = common.Checks("store behaviour under real file locks")
    checks.details["sandbox"] = str(SANDBOX)
    sys.stdout.write(f"sandbox: {SANDBOX}\n")
    for part in (history_part, settings_part, last_recording_part):
        try:
            part(checks)
        except Exception as exc:
            checks.crashed(f"{part.__name__}.crashed", exc)
    checks.details["log_lines"] = [
        common.ascii_safe(line) for line in LOG_LINES if "unreadable" in line.lower()
    ][:20]
    return checks.finish(args.report)


if __name__ == "__main__":
    common.run_main(main)
