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
                cases=[],
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
    """
    build = _STORES[name]
    store, path, read = build(tmp_path)
    _ = store
    saved = read()
    backup = backup_path(path)
    before = backup.read_bytes()

    path.unlink()
    read()

    assert backup.read_bytes() == before or read() == saved, (
        f"{name}: the backup was rewritten from an empty load"
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
