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

import importlib
import json
import logging
from collections.abc import Callable
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
from stt_app.persistence import (
    SOURCE_BACKUP_PRIMARY_UNREADABLE,
    SOURCE_UNREADABLE,
    StoreUnavailableError,
    backup_path,
    load_json_with_backup,
)
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


def _benchmark_entry(summary: str) -> BenchmarkHistoryEntry:
    return BenchmarkHistoryEntry.new(
        status="completed",
        summary=summary,
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
        # A run with no cases is dropped by `_entries_from_payload`, so with
        # `cases=[]` every assertion in this file compared `[]` against `[]`
        # for this store and could not fail. Three of them were passing
        # vacuously.
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


def _benchmark_history(tmp_path: Path):
    store = BenchmarkHistoryStore(path=tmp_path / "benchmark_history.json")
    store.save([_benchmark_entry(f"run {index}") for index in range(3)])
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


# -- A store that could not be read is not a store that is empty --------------
#
# `load_json_with_backup` answered `(None, "missing")` for two different
# states: no file to read, and a file whose read raised `OSError`. Every store
# treats the first as "nothing saved yet", so a transient lock -- an antivirus
# scan, a backup or a sync client holding both the file and its `.bak` --
# quarantined both files, returned the empty default, and the next write
# published a small new store over the top. Measured on the transcript history
# before the fix: five entries, one appended dictation, and the original five
# survived only as `.corrupt.*` copies.


#: The value each store's reader answers when it holds nothing. A store that
#: cannot be read has to answer exactly this and write nothing, rather than
#: answer it and then persist it.
_EMPTY: dict[str, object] = {
    "provider connection tests": [],
    "transcript history": [],
    "settings": (AppSettings().engine, AppSettings().model_size),
    "benchmark history": [],
    "local model inventory": None,
    # `load()` falls back to the orphaned-audio state, whose `recording_id` is
    # empty because no state file was read.
    "last recording": "",
}

#: One read-modify-write per store: it reads what is stored, changes it, and
#: writes the whole content back. That is the operation that destroys an
#: intact file when the read silently answered "empty".
_WRITES: dict[str, Callable[[object], object]] = {
    "provider connection tests": lambda store: store.save_result(
        "openai", ok=False, message="written while the file was locked"
    ),
    "transcript history": lambda store: store.add_entry(
        TranscriptHistoryEntry.new(
            text="dictated while the file was locked",
            engine="local",
            model="small",
            mode="batch",
        ),
        500,
    ),
    "settings": lambda store: store.save(
        replace(AppSettings(), engine="openai", model_size="tiny")
    ),
    "benchmark history": lambda store: store.add_entry(
        _benchmark_entry("run recorded while the file was locked")
    ),
    "local model inventory": lambda store: store.save_cached_models(
        "D:/models", ["tiny"]
    ),
    "last recording": lambda store: store.mark_completed(),
}


@pytest.mark.parametrize("name", sorted(_STORES))
def test_a_store_that_cannot_be_read_is_not_treated_as_empty(
    name, tmp_path, caplog, files_no_read_gets_past
):
    """Load answers the empty default, changes nothing, and says so once.

    Quarantining here is the destructive half: the file is intact and the
    only thing wrong with it is that nobody could open it this second, so
    renaming it to `.corrupt.*` moves the user's data out from under the name
    every later load looks at.
    """
    build = _STORES[name]
    store, path, read = build(tmp_path)
    _ = store
    saved = read()
    backup = backup_path(path)
    before = (path.read_bytes(), backup.read_bytes())

    with caplog.at_level(logging.WARNING), files_no_read_gets_past(path, backup):
        during_the_lock = read()

    assert during_the_lock == _EMPTY[name], (
        f"{name}: a file that could not be read answered something other than "
        "the empty default"
    )
    quarantined = sorted(p.name for p in tmp_path.glob("*.corrupt.*"))
    assert not quarantined, (
        f"{name}: an unreadable file was quarantined as damaged: {quarantined}"
    )
    assert (path.read_bytes(), backup.read_bytes()) == before, (
        f"{name}: the load rewrote a file it had not read"
    )
    assert "store_unreadable" in caplog.text, (
        f"{name}: nothing in the log says the store could not be read"
    )
    assert path.name in caplog.text, f"{name}: the log does not name the file"

    assert read() == saved, (
        f"{name}: the data did not come back once the file could be read again"
    )


@pytest.mark.parametrize("name", sorted(_STORES))
def test_a_store_that_cannot_be_read_refuses_to_write(
    name, tmp_path, files_no_read_gets_past
):
    """A read-modify-write whose read failed would write the empty default.

    It has no idea what the file holds, so the only safe answer is to refuse.
    `StoreUnavailableError` is an `OSError`, so every caller that already
    guards these calls against a failing disk catches it unchanged.
    """
    build = _STORES[name]
    store, path, read = build(tmp_path)
    saved = read()
    backup = backup_path(path)
    before = (path.read_bytes(), backup.read_bytes())

    with files_no_read_gets_past(path, backup), pytest.raises(StoreUnavailableError):
        _WRITES[name](store)

    assert (path.read_bytes(), backup.read_bytes()) == before, (
        f"{name}: the write landed on a file the store had not read"
    )
    assert read() == saved, f"{name}: the stored data did not survive the write"


@pytest.mark.parametrize("name", sorted(_STORES))
def test_damage_in_both_files_is_still_quarantined(name, tmp_path):
    """The other outcome must keep its behaviour.

    A file that opens and does not parse is damage, and moving it aside is
    what stops every later load failing on the same bytes. This one passes
    before the fix as well; it is here so the new "do not quarantine" branch
    cannot widen to cover damage too.
    """
    build = _STORES[name]
    store, path, read = build(tmp_path)
    _ = store
    path.write_text("{ this is not json", encoding="utf-8")
    backup_path(path).write_text("{ this is not json either", encoding="utf-8")

    assert read() == _EMPTY[name], f"{name}: damaged files did not read as empty"

    quarantined = sorted(p.name for p in tmp_path.glob("*.corrupt.*"))
    assert len(quarantined) == 2, (
        f"{name}: both damaged files should have been moved aside: {quarantined}"
    )


def test_a_settings_save_refuses_to_overwrite_a_file_it_cannot_read(
    tmp_path, files_no_read_gets_past
):
    """The settings dialog merges its edits onto a fresh `load()`.

    A load that could not read the file answers defaults, so saving that
    merge writes defaults-plus-edits over an intact `settings.json` -- every
    hotkey, device and provider choice replaced in one save. The refusal is
    in `save` itself rather than in the load, because this store is written
    by callers that never loaded through this instance (the overlay opacity
    slider and pin button among them).
    """
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(replace(AppSettings(), engine="groq", model_size="tiny"))
    before = path.read_bytes()

    never_loaded = SettingsStore(path)
    with (
        files_no_read_gets_past(path, backup_path(path)),
        pytest.raises(StoreUnavailableError),
    ):
        never_loaded.save(replace(AppSettings(), engine="openai"))

    assert path.read_bytes() == before, "the settings file was overwritten"
    assert store.load().engine == "groq"


def test_a_last_recording_that_cannot_be_read_keeps_its_audio(
    tmp_path, files_no_read_gets_past
):
    """`mark_completed` deletes the WAV unless the state says to keep it.

    With the state file unreadable, `load()` falls back to the orphaned-audio
    state, whose `keep_after_success` is `False` -- so completing a recording
    the user had asked to keep deleted the audio, the state file and its
    backup. Measured before the fix: `mark_completed()` returned True and
    `last_recording.wav` was gone.
    """
    state_path = tmp_path / "last_recording.json"
    audio_path = tmp_path / "last_recording.wav"
    store = LastRecordingStore(state_path=state_path, audio_path=audio_path)
    store.save_recording(b"RIFF" + b"\x00" * 40, keep_after_success=True)

    with (
        files_no_read_gets_past(state_path, backup_path(state_path)),
        pytest.raises(StoreUnavailableError),
    ):
        store.mark_completed()

    assert audio_path.is_file(), "the recording the state asked to keep was deleted"
    recovered = store.load()
    assert recovered is not None
    assert recovered.keep_after_success is True
    assert recovered.status == "captured"


def test_the_loader_separates_unreadable_from_missing_and_from_damage(
    tmp_path, files_no_read_gets_past
):
    """The three outcomes at their source, including the one in between.

    A read can fail in two unrelated ways and only one of them says anything
    about the content. `OSError` means nobody could open the file, so what it
    holds is unknown. `UnicodeDecodeError` -- which `read_text` raises for a
    file that is not UTF-8, and which is a `ValueError`, not an `OSError` --
    means the bytes were read and are not what this store writes, exactly
    like JSON that will not parse. Reporting the second as unreadable would
    leave a hand-damaged file quarantining nothing and refusing every write
    until someone repaired it by hand.
    """
    path = tmp_path / "store.json"
    backup = backup_path(path)

    assert load_json_with_backup(path, expected_type=dict) == (None, "missing")

    path.write_text("{ this is not json", encoding="utf-8")
    backup.write_bytes('{"engine": "gruß"}'.encode("cp1252"))
    assert load_json_with_backup(path, expected_type=dict) == (None, "missing")

    path.write_text('{"engine": "groq"}', encoding="utf-8")
    backup.write_text('{"engine": "groq"}', encoding="utf-8")
    with files_no_read_gets_past(path, backup):
        assert load_json_with_backup(path, expected_type=dict) == (
            None,
            SOURCE_UNREADABLE,
        )

    # And the state in between: the backup answers while the primary is there
    # and could not be opened. Its own source, because a store must leave that
    # primary alone rather than quarantine and republish it the way it answers
    # a primary that opened and turned out unusable.
    with files_no_read_gets_past(path):
        assert load_json_with_backup(path, expected_type=dict) == (
            {"engine": "groq"},
            SOURCE_BACKUP_PRIMARY_UNREADABLE,
        )
    # A locked *backup* is not that state. The primary is read first and
    # answered, so the backup was never opened and nothing about it is known.
    with files_no_read_gets_past(backup):
        assert load_json_with_backup(path, expected_type=dict) == (
            {"engine": "groq"},
            "primary",
        )


@pytest.mark.parametrize("name", sorted(_STORES))
def test_a_primary_that_cannot_be_read_still_falls_through_to_the_backup(
    name, tmp_path, files_no_read_gets_past
):
    """Unreadable is the answer only when no candidate was usable.

    A lock on the primary alone is the ordinary case -- a scanner opens the
    file that was just written -- and the backup beside it still answers, so
    neither the refusal nor the empty default may fire.
    """
    build = _STORES[name]
    store, path, read = build(tmp_path)
    _ = store
    saved = read()
    assert backup_path(path).is_file(), f"{name}: no backup was written beside it"

    with files_no_read_gets_past(path):
        assert read() == saved, (
            f"{name}: a locked primary hid a backup that could be read"
        )


# -- A primary that exists and cannot be opened is never touched --------------
#
# The backup answering is not permission to rewrite the primary. It was: the
# recovery republished the primary from the backup, and the transcript history
# additionally renamed the primary to `.corrupt.*` first -- so an intact file
# nobody could open for a moment was replaced by whatever the backup held, and
# if the backup was the older of the two (a `.bak` write that failed in an
# earlier save) the newer content was the copy moved aside.


def _record_store_writes(monkeypatch) -> list[str]:
    """Collect the file names every store module writes, write included.

    A republish writes the same logical content, so comparing bytes cannot
    see it: `json.dumps` of the recovered payload can equal the file that is
    already there. This records the attempt itself, which is the property --
    no store may write while the primary cannot be read.
    """
    written: list[str] = []
    for module_name in (
        "stt_app.persistence",
        "stt_app.transcript_history",
        "stt_app.benchmark_history",
        "stt_app.last_recording_store",
        "stt_app.settings_store",
        "stt_app.local_model_inventory_store",
        "stt_app.provider_connection_test_store",
    ):
        module = importlib.import_module(module_name)
        for symbol in ("atomic_write_json", "atomic_write_bytes"):
            real = getattr(module, symbol, None)
            if real is None:
                continue

            def record(path, *args, _real=real, **kwargs):
                written.append(Path(path).name)
                return _real(path, *args, **kwargs)

            monkeypatch.setattr(module, symbol, record)
    return written


@pytest.mark.parametrize("name", sorted(_STORES))
def test_a_locked_primary_is_neither_rewritten_nor_quarantined(
    name, tmp_path, monkeypatch, caplog, files_no_read_gets_past
):
    """The backup answers, and that is all that happens.

    Writing the recovered payload back over a file the store could not open
    is the same overwrite as writing the empty default over it: the store
    cannot tell which of the two copies is the newer one. The data is
    answered from memory, the writers refuse, and once the lock has lifted
    the next load reads the primary again.
    """
    build = _STORES[name]
    store, path, read = build(tmp_path)
    saved = read()
    backup = backup_path(path)
    before = (path.read_bytes(), backup.read_bytes())
    written = _record_store_writes(monkeypatch)

    with caplog.at_level(logging.WARNING), files_no_read_gets_past(path):
        during_the_lock = read()
        with pytest.raises(StoreUnavailableError):
            _WRITES[name](store)

    assert during_the_lock == saved, (
        f"{name}: the backup beside the locked primary was not read"
    )
    assert written == [], (
        f"{name}: {written} was written while the primary could not be read"
    )
    quarantined = sorted(p.name for p in tmp_path.glob("*.corrupt.*"))
    assert not quarantined, (
        f"{name}: a primary that could not be opened was quarantined: "
        f"{quarantined}"
    )
    assert (path.read_bytes(), backup.read_bytes()) == before, (
        f"{name}: a file changed while the primary could not be read"
    )
    # `SettingsStore.save` probes the file itself and refuses, so its
    # republish left the two copies alone whether or not it was attempted --
    # the attempt is visible only as this log line, and reverting the settings
    # store's branch is caught by nothing else.
    attempted = [
        record.getMessage()
        for record in caplog.records
        if "Could not rewrite" in record.getMessage()
        or "Could not republish" in record.getMessage()
    ]
    assert attempted == [], (
        f"{name}: a rewrite of the locked primary was attempted: {attempted}"
    )

    assert read() == saved, f"{name}: the primary did not answer once unlocked"
    _WRITES[name](store)


def test_a_settings_normalisation_is_not_written_over_a_locked_primary(
    tmp_path, caplog, files_no_read_gets_past
):
    """The rewrite is skipped, not attempted and refused.

    `SettingsStore.load` rewrites the file whenever the stored payload differs
    from the normalised one -- an older schema, a value stored as text -- and
    with the primary locked that write is refused by `save`'s own readability
    probe. The settings were read correctly either way, so the only thing the
    attempt produced was a "Could not rewrite" warning on every single load;
    it is not made at all now. The copy under the lock is the backup's, so
    nothing here says the primary's content is what was answered.
    """
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(replace(AppSettings(), engine="groq", model_size="tiny"))
    stored = json.loads(path.read_text(encoding="utf-8"))
    # A numeric string parses to the same number and is written back as one,
    # so every load of this file wants to rewrite it.
    stored["history_max_items"] = "20"
    text = json.dumps(stored)
    path.write_text(text, encoding="utf-8")
    backup_path(path).write_text(text, encoding="utf-8")

    with caplog.at_level(logging.WARNING), files_no_read_gets_past(path):
        settings = store.load()

    assert (settings.engine, settings.history_max_items) == ("groq", 20)
    assert "Could not rewrite" not in caplog.text, (
        "a rewrite of the locked primary was attempted"
    )
    assert path.read_text(encoding="utf-8") == text
    assert backup_path(path).read_text(encoding="utf-8") == text


def test_an_export_refuses_while_the_history_cannot_be_read(
    tmp_path, files_no_read_gets_past
):
    """Export writes the file the user picked in a Save dialog.

    Reading the history as empty there loses no transcript, but it replaces
    the destination -- routinely a previous export -- with `[]` and reports
    "Exported 0 entries". `run_history_export` already turns the refusal into
    its "Export failed" box.
    """
    store = TranscriptHistoryStore(path=tmp_path / "transcript_history.json")
    store.save(
        [
            TranscriptHistoryEntry.new(
                text=f"dictation number {index}",
                engine="local",
                model="small",
                mode="batch",
            )
            for index in range(5)
        ]
    )
    destination = tmp_path / "exported_history.json"
    destination.write_text('["an earlier export"]', encoding="utf-8")

    with (
        files_no_read_gets_past(store.path, backup_path(store.path)),
        pytest.raises(StoreUnavailableError),
    ):
        store.export_to_file(destination)

    assert destination.read_text(encoding="utf-8") == '["an earlier export"]'
    assert store.export_to_file(destination) == 5
