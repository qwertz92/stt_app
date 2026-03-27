from __future__ import annotations

import json
from pathlib import Path

from stt_app.persistence import backup_path
from stt_app.transcript_history import TranscriptHistoryEntry, TranscriptHistoryStore


def test_add_entry_persists_and_respects_max_items(tmp_path):
    path = tmp_path / "history.json"
    store = TranscriptHistoryStore(path=path)

    store.add_entry(
        TranscriptHistoryEntry.new(
            text="one",
            engine="local",
            model="small",
            mode="batch",
        ),
        max_items=2,
    )
    store.add_entry(
        TranscriptHistoryEntry.new(
            text="two",
            engine="local",
            model="small",
            mode="batch",
        ),
        max_items=2,
    )
    store.add_entry(
        TranscriptHistoryEntry.new(
            text="three",
            engine="local",
            model="small",
            mode="batch",
        ),
        max_items=2,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload) == 2
    assert payload[0]["text"] == "two"
    assert payload[1]["text"] == "three"


def test_recent_entries_returns_newest_first(tmp_path):
    path = tmp_path / "history.json"
    store = TranscriptHistoryStore(path=path)
    store.save(
        [
            TranscriptHistoryEntry(
                created_at="2026-01-01T00:00:00+00:00",
                text="first",
                engine="local",
                model="small",
                mode="batch",
            ),
            TranscriptHistoryEntry(
                created_at="2026-01-01T00:00:01+00:00",
                text="second",
                engine="local",
                model="small",
                mode="batch",
            ),
            TranscriptHistoryEntry(
                created_at="2026-01-01T00:00:02+00:00",
                text="third",
                engine="local",
                model="small",
                mode="batch",
            ),
        ]
    )

    recent = store.recent_entries(limit=2)

    assert [entry.text for entry in recent] == ["third", "second"]


def test_load_ignores_invalid_payload(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(json.dumps([{"text": "ok"}, 123, "x", {}]), encoding="utf-8")
    store = TranscriptHistoryStore(path=path)

    entries = store.load()

    assert len(entries) == 1
    assert entries[0].text == "ok"


def test_history_recovers_from_backup_when_primary_is_invalid(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{not-json", encoding="utf-8")
    backup_entries = [
        {
            "created_at": "2026-01-01T00:00:00+00:00",
            "text": "recovered",
            "engine": "local",
            "model": "small",
            "mode": "batch",
        }
    ]
    backup_path(path).write_text(json.dumps(backup_entries), encoding="utf-8")
    store = TranscriptHistoryStore(path=path)

    entries = store.load()

    assert [entry.text for entry in entries] == ["recovered"]
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored[0]["text"] == "recovered"


def test_add_entry_with_zero_limit_keeps_all_entries(tmp_path):
    path = tmp_path / "history.json"
    store = TranscriptHistoryStore(path=path)

    for idx in range(5):
        store.add_entry(
            TranscriptHistoryEntry.new(
                text=f"entry-{idx}",
                engine="local",
                model="small",
                mode="batch",
            ),
            max_items=0,
        )

    loaded = store.load()
    assert len(loaded) == 5
    assert loaded[-1].text == "entry-4"


def test_apply_max_items_trims_oldest_entries(tmp_path):
    path = tmp_path / "history.json"
    store = TranscriptHistoryStore(path=path)
    store.save(
        [
            TranscriptHistoryEntry.new(
                text=f"entry-{idx}",
                engine="local",
                model="small",
                mode="batch",
            )
            for idx in range(6)
        ]
    )

    removed = store.apply_max_items(3)

    assert removed == 3
    assert [item.text for item in store.load()] == [
        "entry-3",
        "entry-4",
        "entry-5",
    ]


def test_delete_entry_removes_selected_item(tmp_path):
    path = tmp_path / "history.json"
    store = TranscriptHistoryStore(path=path)
    entries = [
        TranscriptHistoryEntry.new(
            text="keep",
            engine="local",
            model="small",
            mode="batch",
        ),
        TranscriptHistoryEntry.new(
            text="remove",
            engine="local",
            model="small",
            mode="batch",
        ),
    ]
    store.save(entries)

    removed = store.delete_entry(entries[1])

    assert removed == 1
    assert [item.text for item in store.load()] == ["keep"]


def test_export_and_import_roundtrip(tmp_path):
    source = TranscriptHistoryStore(path=tmp_path / "source.json")
    source.save(
        [
            TranscriptHistoryEntry.new(
                text="one",
                engine="local",
                model="small",
                mode="batch",
            ),
            TranscriptHistoryEntry.new(
                text="two",
                engine="local",
                model="small",
                mode="batch",
            ),
        ]
    )
    export_path = tmp_path / "exports" / "history.json"

    count = source.export_to_file(export_path)
    imported = source.import_from_file(Path(export_path))

    assert count == 2
    assert [item.text for item in imported] == ["one", "two"]
