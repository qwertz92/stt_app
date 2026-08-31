"""Every persisted store must survive losing its primary file.

`atomic_write_json(keep_backup=True)` writes a `.bak` beside each store, and
`load_json_with_backup` reads it when the primary will not parse. Five stores
guarded the whole load with a bare `if not path.exists()`, which the backup
never got past -- so a *deleted* primary read as "nothing saved yet", and the
next write then overwrote the backup with that emptiness.

Measured on the transcript history before the fix: five entries, delete
`transcript_history.json`, load returns 0, one more dictation leaves the
backup holding 1. `settings_store` was worse still: a missing primary writes
defaults and refreshes the `.bak` in the same call, so every setting was reset
and the only remaining copy destroyed together.

The primary goes missing for ordinary reasons -- an antivirus quarantine, a
sync client, a user tidying `%APPDATA%`, a half-restored profile -- and it is
exactly the case the backup exists for.

`provider_connection_test_store` is the sixth store here and never had the
guard; its own comment explains why, and it is the shape the other five now
follow. It is covered anyway, because the point is the invariant, not the
diff. What it did share was the second half: a store that recovers from the
backup must republish the primary, or the data stays one loss away from gone.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from stt_app.benchmark_history import (
    BenchmarkHistoryEntry,
    BenchmarkHistoryStore,
    BenchmarkOptions,
)
from stt_app.last_recording_store import LastRecordingStore
from stt_app.local_benchmark import BenchmarkCase
from stt_app.local_model_inventory_store import LocalModelInventoryStore
from stt_app.persistence import backup_path
from stt_app.provider_connection_test_store import ProviderConnectionTestStore
from stt_app.settings_store import AppSettings, SettingsStore
from stt_app.transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore


def _transcript_history(tmp_path: Path):
    store = TranscriptHistoryStore(path=tmp_path / "transcript_history.json")
    entries = [
        TranscriptHistoryEntry.new(
            text=f"dictation number {index}",
            engine="local",
            model="small",
            mode="batch",
        )
        for index in range(5)
    ]
    store.save(entries)
    return store, store.path, lambda: [item.text for item in store.load()]


def _settings(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(replace(AppSettings(), engine="groq", model_size="tiny"))
    return store, store.path, lambda: (store.load().engine, store.load().model_size)


def _benchmark_history(tmp_path: Path):
    store = BenchmarkHistoryStore(path=tmp_path / "benchmark_history.json")
    store.save(
        [
            BenchmarkHistoryEntry.new(
                status="completed",
                summary=f"run {index}",
                options=BenchmarkOptions(
                    audio_path="C:/sample.wav",
                    audio_name="sample.wav",
                    model_names=["small"],
                    device="auto",
                    compute_type="int8",
                    webgpu_devices=["auto"],
                    runs=1,
                    beam_size=5,
                    language="auto",
                    vad_filter=False,
                    warmup=False,
                    threads=0,
                ),
                # A run with no cases is dropped by `_entries_from_payload`, so
                # with `cases=[]` every assertion in this file compared `[]`
                # against `[]` for this store and could not fail. Three of them
                # were passing vacuously.
                cases=[
                    BenchmarkCase(
                        model="small",
                        device="cpu",
                        compute_type="int8",
                        download_seconds=0.0,
                        load_seconds=0.5,
                        runs=[],
                    )
                ],
            )
            for index in range(3)
        ]
    )
    return store, store.path, lambda: [item.summary for item in store.load()]


def _local_model_inventory(tmp_path: Path):
    store = LocalModelInventoryStore(path=tmp_path / "local_model_inventory.json")
    store.save_cached_models("D:/models", ["small", "tiny"])
    return (
        store,
        store.path,
        lambda: store.load_cached_models("D:/models"),
    )


def _last_recording(tmp_path: Path):
    store = LastRecordingStore(
        audio_path=tmp_path / "last_recording.wav",
        state_path=tmp_path / "last_recording.json",
    )
    store.save_recording(b"RIFF" + b"\x00" * 40, keep_after_success=True)

    def read():
        state = store.load()
        return None if state is None else state.recording_id

    return store, store.state_path, read


def _provider_connection_tests(tmp_path: Path):
    store = ProviderConnectionTestStore(path=tmp_path / "provider_connection_tests.json")
    store.save_result("openai", ok=True, message="reachable")
    store.save_result("groq", ok=False, message="401")

    def read():
        return sorted(
            (name, result.ok) for name, result in store.load_all().items()
        )

    return store, store.path, read


_STORES = {
    "provider connection tests": _provider_connection_tests,
    "transcript history": _transcript_history,
    "settings": _settings,
    "benchmark history": _benchmark_history,
    "local model inventory": _local_model_inventory,
    "last recording": _last_recording,
}


@pytest.mark.parametrize("name", sorted(_STORES))
def test_a_deleted_primary_is_recovered_from_the_backup(name, tmp_path):
    """The `.bak` is the only copy left, so it has to be read."""
    build = _STORES[name]
    store, path, read = build(tmp_path)
    _ = store
    saved = read()
    backup = backup_path(path)
    assert path.is_file(), f"{name}: nothing was written"
    assert backup.is_file(), f"{name}: no backup was written beside it"

    path.unlink()

    assert read() == saved, (
        f"{name}: the backup was ignored, so a deleted primary reads as empty"
    )
    assert path.is_file(), (
        f"{name}: the recovered payload was not written back to the primary, "
        "so every later load pays the recovery again"
    )


@pytest.mark.parametrize("name", sorted(_STORES))
def test_a_deleted_primary_does_not_get_the_backup_overwritten(name, tmp_path):
    """Reading is not the only loss; the next write finished the job.

    With the load returning empty, the very next save wrote that emptiness
    over the backup too, so the data was gone rather than merely invisible.

    Stated as "survives the loss twice" rather than "the backup bytes did not
    change". The byte comparison was written as `unchanged or read() == saved`,
    and the recovery the test above already pins makes that second half true
    every time -- so the assertion held whatever the write did to the backup.
    """
    build = _STORES[name]
    store, path, read = build(tmp_path)
    _ = store
    saved = read()
    backup = backup_path(path)
    assert backup.is_file(), f"{name}: no backup was written beside it"

    path.unlink()
    read()

    assert backup.is_file(), f"{name}: the recovery removed the backup"
    # The same loss again: whatever the recovery wrote to the backup, it has to
    # still hold the data. Rewriting it with the recovered payload is fine;
    # rewriting it with an empty one is the defect.
    path.unlink()

    assert read() == saved, (
        f"{name}: the backup no longer holds the data after one recovery"
    )


@pytest.mark.parametrize("name", sorted(_STORES))
def test_a_genuinely_fresh_install_still_reads_as_empty(name, tmp_path):
    """The guard must widen, not disappear.

    With neither file present there is nothing to recover, and the store must
    return its empty default without quarantining anything or reporting a
    problem.
    """
    build = _STORES[name]
    store, path, read = build(tmp_path)
    _ = store

    path.unlink(missing_ok=True)
    backup_path(path).unlink(missing_ok=True)

    read()

    quarantined = sorted(p.name for p in tmp_path.glob("*.corrupt.*"))
    assert not quarantined, (
        f"{name}: a fresh install quarantined files that were never there: "
        f"{quarantined}"
    )


@pytest.mark.parametrize("name", sorted(_STORES))
def test_a_primary_in_the_wrong_encoding_is_recovered_from_the_backup(name, tmp_path):
    """`UnicodeDecodeError` is a `ValueError`, not a `json.JSONDecodeError`.

    `load_json_with_backup` caught only the latter, so a file that is not
    UTF-8 escaped the loader instead of falling through to the backup. For
    settings that is fatal rather than merely lossy: `main` calls
    `SettingsStore.load()` unprotected, so a `settings.json` re-saved by hand
    in the Windows ANSI code page stopped the app from starting at all, with a
    good backup lying beside it. Measured before the fix: `UnicodeDecodeError:
    'utf-8' codec can't decode byte 0xdf in position 20`.
    """
    build = _STORES[name]
    store, path, read = build(tmp_path)
    _ = store
    saved = read()
    assert backup_path(path).is_file(), f"{name}: no backup was written beside it"

    path.write_bytes('{"engine": "gru\u00df"}'.encode("cp1252"))

    assert read() == saved, (
        f"{name}: a primary that is not UTF-8 was not recovered from the backup"
    )


def test_clearing_the_last_recording_clears_its_backup_too(tmp_path):
    """A backup is only a recovery copy while there is something to recover.

    `clear()` deleted the state file and left the `.bak`, and `load()` reads the
    backup exactly when the primary is missing -- and republishes it. So the
    cleared state came straight back, pointing at a WAV that had been deleted a
    moment earlier, and stayed back for good. Measured: `clear()` returned True,
    the audio was gone, and the very next `load()` returned the same
    `recording_id` with the primary rewritten.
    """
    state_path = tmp_path / "last_recording.json"
    audio_path = tmp_path / "last_recording.wav"
    store = LastRecordingStore(state_path=state_path, audio_path=audio_path)

    store.save_recording(b"RIFF-first", keep_after_success=True)
    state = store.save_recording(b"RIFF-second", keep_after_success=True)
    assert backup_path(state_path).is_file(), "no backup was written beside it"

    assert store.clear() is True
    assert not audio_path.exists()
    assert not state_path.exists()
    assert not backup_path(state_path).exists()

    assert store.load() is None, (
        f"the cleared recording {state.recording_id} came back from its backup"
    )
    assert not state_path.exists(), "and the primary was rewritten from it"


def test_a_damaged_primary_does_not_take_a_healthy_backup_with_it(tmp_path):
    """`quarantine_corrupt_file(include_backup=True)` says in its own docstring
    that it is only for a backup already known to be unusable. The connection
    test store passed it for a payload that parsed but whose `results` key was
    not an object -- a state only external damage produces, which is precisely
    when the backup is the good copy. Measured before the fix: both files were
    moved aside and every later load returned nothing.
    """
    path = tmp_path / "provider_connection_tests.json"
    store = ProviderConnectionTestStore(path=path)
    store.save_result(
        "openai", ok=True, message="Connected.", checked_at="2026-08-30 12:00:00"
    )
    assert backup_path(path).is_file()

    path.write_text('{"schema_version": 1, "results": "not an object"}', encoding="utf-8")

    store.load_all()
    assert backup_path(path).is_file(), "the healthy backup was quarantined"

    recovered = store.load_all()
    assert "openai" in recovered, "the surviving backup was never used"
    assert path.is_file(), "a recovered store must republish its primary"


def test_a_damaged_backup_is_the_file_that_gets_quarantined(tmp_path):
    """The mirror case. Quarantining `path` unconditionally is a no-op when the
    primary is the file that is already gone, so the bad backup stayed and every
    later load failed on it identically.
    """
    path = tmp_path / "provider_connection_tests.json"
    store = ProviderConnectionTestStore(path=path)
    store.save_result(
        "groq", ok=True, message="Connected.", checked_at="2026-08-30 12:00:00"
    )
    path.unlink()
    backup_path(path).write_text(
        '{"schema_version": 1, "results": []}', encoding="utf-8"
    )

    assert store.load_all() == {}
    assert not backup_path(path).exists(), "the unusable backup was left in place"
    quarantined = sorted(q.name for q in tmp_path.glob("*.corrupt.*"))
    assert len(quarantined) == 1, quarantined
    assert quarantined[0].startswith("provider_connection_tests.json.bak.corrupt.")


def test_a_backup_that_cannot_be_removed_stops_the_clear(tmp_path):
    """Reporting success while the backup survives is the bug above with an
    extra step: the next load would republish the cleared state. `clear()`
    already treats a failed unlink of the audio or the state file as a refusal,
    and the backup is no different -- the state file stays, so a later retry can
    finish the job.
    """
    state_path = tmp_path / "last_recording.json"
    audio_path = tmp_path / "last_recording.wav"
    store = LastRecordingStore(state_path=state_path, audio_path=audio_path)
    store.save_recording(b"RIFF", keep_after_success=True)

    # A directory is the portable way to make `Path.unlink` raise `OSError`
    # without reaching into the store: `missing_ok` only swallows
    # `FileNotFoundError`.
    backup = backup_path(state_path)
    backup.unlink()
    backup.mkdir()

    assert store.clear() is False
    assert state_path.is_file(), "the state has to stay discoverable for a retry"


@pytest.mark.parametrize("name", sorted(_STORES))
def test_a_republish_that_cannot_write_still_returns_the_recovered_data(
    name, tmp_path, monkeypatch
):
    """The republish is a convenience; the data is already in hand.

    Letting its write escape threw the recovery away with it. Measured with
    the primary gone and the directory unwritable -- an antivirus quarantine
    on a locked-down profile is both at once -- `load()` raised
    `PermissionError` and returned nothing, and `SettingsDialog.__init__`
    calls these readers with no guard of its own.
    """
    build = _STORES[name]
    store, path, read = build(tmp_path)
    _ = store
    saved = read()
    assert backup_path(path).is_file(), f"{name}: no backup was written"
    path.unlink()

    def refuse(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    for module in (
        "stt_app.persistence",
        "stt_app.transcript_history",
        "stt_app.benchmark_history",
        "stt_app.last_recording_store",
        "stt_app.settings_store",
        "stt_app.local_model_inventory_store",
        "stt_app.provider_connection_test_store",
    ):
        for symbol in ("atomic_write_json", "atomic_write_bytes"):
            try:
                monkeypatch.setattr(f"{module}.{symbol}", refuse)
            except AttributeError:
                continue

    assert read() == saved, (
        f"{name}: a republish that could not write discarded the recovery"
    )
