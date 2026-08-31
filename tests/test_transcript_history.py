from __future__ import annotations

import json
import threading
from pathlib import Path

from stt_app.persistence import backup_path
from stt_app.transcript_history import (
    TranscriptHistoryEntry,
    TranscriptHistoryStore,
    join_recent_entries_for_clipboard,
    map_recent_entry_rows,
    recent_entries_change_plan,
)


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


def test_source_audio_path_round_trips_and_old_entries_remain_compatible(tmp_path):
    path = tmp_path / "history.json"
    audio = tmp_path / "recording.wav"
    entry = TranscriptHistoryEntry.new(
        text="retained audio",
        engine="local",
        model="cohere-transcribe-03-2026",
        mode="batch",
        source_recording_id="recording-id",
        source_audio_path=str(audio),
    )
    store = TranscriptHistoryStore(path=path)
    store.save([entry])

    loaded = store.load()[0]
    legacy = TranscriptHistoryEntry.from_dict(
        {
            "created_at": "2026-01-01T00:00:00+00:00",
            "text": "legacy",
            "engine": "local",
            "model": "small",
            "mode": "batch",
        }
    )

    assert loaded.source_recording_id == "recording-id"
    assert loaded.source_audio_path == str(audio)
    assert legacy.source_audio_path == ""


def test_concurrent_store_instances_do_not_lose_read_modify_write_updates(tmp_path):
    path = tmp_path / "history.json"
    first_store = TranscriptHistoryStore(path=path)
    second_store = TranscriptHistoryStore(path=path)
    first_loaded = threading.Event()
    release_first = threading.Event()
    original_first_load = first_store.load

    def _paused_first_load():
        entries = original_first_load()
        first_loaded.set()
        assert release_first.wait(timeout=2)
        return entries

    first_store.load = _paused_first_load  # type: ignore[method-assign]
    first_entry = TranscriptHistoryEntry.new(
        text="first concurrent entry",
        engine="local",
        model="small",
        mode="batch",
    )
    second_entry = TranscriptHistoryEntry.new(
        text="second concurrent entry",
        engine="local",
        model="small",
        mode="batch",
    )
    first_thread = threading.Thread(
        target=first_store.add_entry,
        args=(first_entry, 10),
    )
    second_thread = threading.Thread(
        target=second_store.add_entry,
        args=(second_entry, 10),
    )

    first_thread.start()
    assert first_loaded.wait(timeout=2)
    second_thread.start()
    second_thread.join(timeout=0.1)
    assert second_thread.is_alive()

    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert [entry.text for entry in TranscriptHistoryStore(path=path).load()] == [
        "first concurrent entry",
        "second concurrent entry",
    ]


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


def test_recent_entries_with_count_returns_limited_entries_and_total(tmp_path):
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

    recent, total = store.recent_entries_with_count(limit=2)

    assert total == 3
    assert [entry.text for entry in recent] == ["third", "second"]


def test_join_recent_entries_for_clipboard_uses_oldest_first_order():
    entries = [
        TranscriptHistoryEntry(
            created_at="2026-01-01T00:00:02+00:00",
            text="third",
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
    ]

    text = join_recent_entries_for_clipboard(entries)

    assert text == "second\n\nthird"


def test_recent_entries_change_plan_detects_prepend_delete_and_update():
    first = TranscriptHistoryEntry(
        created_at="2026-01-01T00:00:00+00:00",
        text="first",
        engine="local",
        model="small",
        mode="batch",
    )
    second = TranscriptHistoryEntry(
        created_at="2026-01-01T00:00:01+00:00",
        text="second",
        engine="local",
        model="small",
        mode="batch",
    )
    third = TranscriptHistoryEntry(
        created_at="2026-01-01T00:00:02+00:00",
        text="third",
        engine="local",
        model="small",
        mode="batch",
    )

    prepend = recent_entries_change_plan([second, first], [third, second, first])

    assert [
        (change.kind, change.previous_start, change.current_start)
        for change in prepend
    ] == [("insert", 0, 0)]
    assert map_recent_entry_rows(prepend, [0, 1]) == [1, 2]

    delete = recent_entries_change_plan([third, second, first], [third, first])

    assert [
        (change.kind, change.previous_start, change.previous_stop)
        for change in delete
    ] == [("delete", 1, 2)]
    assert map_recent_entry_rows(delete, [0, 1, 2]) == [0, 1]

    edited_second = TranscriptHistoryEntry(
        created_at="2026-01-01T00:00:01+00:00",
        text="second edited",
        engine="local",
        model="small",
        mode="batch",
    )
    update = recent_entries_change_plan([second, first], [edited_second, first])

    assert [
        (change.kind, change.previous_start, change.current_start)
        for change in update
    ] == [("update", 0, 0)]
    assert map_recent_entry_rows(update, [0, 1]) == [0, 1]


def test_recent_entries_change_plan_replaces_identity_changes():
    first = TranscriptHistoryEntry(
        created_at="2026-01-01T00:00:00+00:00",
        text="first",
        engine="local",
        model="small",
        mode="batch",
    )
    second = TranscriptHistoryEntry(
        created_at="2026-01-01T00:00:01+00:00",
        text="second",
        engine="local",
        model="small",
        mode="batch",
    )
    different_entry = TranscriptHistoryEntry(
        created_at="2026-01-01T00:00:02+00:00",
        text="different",
        engine="local",
        model="small",
        mode="batch",
    )

    plan = recent_entries_change_plan([second, first], [different_entry, first])

    assert [
        (change.kind, change.previous_start, change.current_start)
        for change in plan
    ] == [("replace", 0, 0)]
    assert map_recent_entry_rows(plan, [0, 1]) == [1]


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


def test_update_entry_text_replaces_selected_item(tmp_path):
    path = tmp_path / "history.json"
    store = TranscriptHistoryStore(path=path)
    entries = [
        TranscriptHistoryEntry.new(
            text="original",
            engine="local",
            model="small",
            mode="batch",
        ),
        TranscriptHistoryEntry.new(
            text="keep",
            engine="local",
            model="base",
            mode="batch",
        ),
    ]
    store.save(entries)

    updated = store.update_entry_text(entries[0], " corrected text ")

    assert updated == 1
    loaded = store.load()
    assert loaded[0].text == "corrected text"
    assert loaded[0].engine == "local"
    assert loaded[1].text == "keep"


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


def test_importing_a_file_that_is_not_utf8_says_so_in_words(tmp_path):
    """The caller shows this message verbatim, so it has to be readable.

    `UnicodeDecodeError` is a `ValueError`, so it already reached the dialog
    -- but as `'utf-8' codec can't decode byte 0xdf in position 20: invalid
    continuation byte`, which names neither the file nor what to do about it.
    A history export re-saved in Notepad's ANSI encoding is the ordinary way
    to get one.
    """
    store = TranscriptHistoryStore(path=tmp_path / "history.json")
    export = tmp_path / "history_export.json"
    export.write_bytes(
        # `ensure_ascii=False` is the point: the default escapes the
        # umlauts back to ASCII, and those bytes decode as UTF-8 fine.
        json.dumps([{"text": "Grüße"}], ensure_ascii=False).encode(
            "cp1252"
        )
    )

    try:
        store.import_from_file(export)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("a file that is not UTF-8 was imported anyway")

    assert "UTF-8" in message, f"the message does not name the problem: {message}"
    assert "codec" not in message, (
        f"the raw decoder error reached the user: {message}"
    )


def _dated(created_at: str, text: str) -> TranscriptHistoryEntry:
    return TranscriptHistoryEntry(
        created_at=created_at,
        text=text,
        engine="local",
        model="small",
        mode="batch",
    )


def test_a_trim_after_an_import_deletes_the_oldest_by_time_not_by_position(tmp_path):
    """Position stood in for time, and an import breaks that proxy.

    Imported entries are appended at the end whatever their timestamps say, so
    the front of the list -- what the trim deletes -- stopped being the oldest.
    Measured before this: twelve August-2026 dictations, a 40-entry export from
    March 2024 imported back, limit lowered to 40, and **all twelve dictations
    were deleted while all forty imports were kept** -- behind a prompt that
    says "will delete N oldest entries", and with `recent_entries` reporting
    the 2024 entries as the newest.
    """
    store = TranscriptHistoryStore(path=tmp_path / "history.json")
    for day in range(15, 27):
        store.add_entry(_dated(f"2026-08-{day:02d}T09:00:00+00:00", f"neu {day}"), 500)
    old = [_dated(f"2024-03-{n:02d}T09:00:00+00:00", f"alt {n}") for n in range(1, 41)]
    store.append_entries(old, max_items=500)

    assert [e.text for e in store.recent_entries(3)] == ["neu 26", "neu 25", "neu 24"]

    removed = store.apply_max_items(40)

    kept = store.load()
    assert removed == 12
    assert sum(1 for e in kept if e.created_at.startswith("2026")) == 12, (
        "real dictations were deleted in favour of older imported entries"
    )
    assert sum(1 for e in kept if e.created_at.startswith("2024")) == 28
    assert kept == sorted(kept, key=lambda e: e.created_at), "stored out of order"


def test_entries_sharing_a_timestamp_keep_their_insertion_order(tmp_path):
    """The app stamps whole seconds, so ties are ordinary, not exotic."""
    store = TranscriptHistoryStore(path=tmp_path / "history.json")
    same = "2026-08-31T09:00:00+00:00"
    for index in range(4):
        store.add_entry(_dated(same, f"satz {index}"), 500)

    assert [e.text for e in store.load()] == [f"satz {i}" for i in range(4)]
    assert [e.text for e in store.recent_entries(2)] == ["satz 3", "satz 2"]


def test_an_entry_with_an_unreadable_timestamp_is_trimmed_before_a_real_one(tmp_path):
    """Undatable sorts oldest, which is the safer of the two directions.

    Only an imported or hand-edited file can carry one, and treating it as the
    newest would have it push a real dictation out instead.
    """
    store = TranscriptHistoryStore(path=tmp_path / "history.json")
    store.append_entries(
        [
            _dated("", "kein datum"),
            _dated("not a timestamp", "kaputtes datum"),
            _dated("2026-08-30T09:00:00+00:00", "echte diktat"),
        ],
        max_items=0,
    )

    store.apply_max_items(1)

    assert [e.text for e in store.load()] == ["echte diktat"]


def test_a_list_primary_that_holds_no_entry_falls_through_to_the_backup(tmp_path):
    """`expected_type=list` was too weak a test for "the primary survived".

    Any JSON list satisfied it, so external damage that still parses as a list
    counted as the good copy, the intact `.bak` was never opened, and the next
    dictation saved that emptiness over it too. Measured: five transcripts
    became one.
    """
    for index, damaged in enumerate((["a", 1, None], [{"engine": "local"}], [{}, {}])):
        # One directory per case, so a `.corrupt.` file left by an earlier
        # iteration cannot satisfy the assertion for a later one.
        folder = tmp_path / f"case{index}"
        folder.mkdir()
        path = folder / "history.json"
        store = TranscriptHistoryStore(path=path)
        store.save(
            [_dated(f"2026-08-{n + 1:02d}T09:00:00+00:00", f"t{n}") for n in range(5)]
        )
        assert backup_path(path).is_file()

        path.write_text(json.dumps(damaged), encoding="utf-8")

        assert len(store.load()) == 5, f"{damaged}: the backup was never opened"
        store.add_entry(_dated("2026-09-01T09:00:00+00:00", "danach"), 500)
        assert len(store.load()) == 6, f"{damaged}: the recovery did not stick"
        assert [item.name for item in folder.iterdir() if ".corrupt." in item.name], (
            f"{damaged}: the damaged primary was overwritten instead of kept"
        )


def test_a_cleared_history_is_not_mistaken_for_damage(tmp_path):
    """An empty list is a user who pressed Clear, and must beat the backup.

    Asserting only that `load()` returns `[]` is not enough: a predicate that
    calls an empty store damaged also ends at `[]`, having found the backup
    equally "damaged" and quarantined *both* files on the way. So the files
    themselves are what this checks.
    """
    path = tmp_path / "history.json"
    store = TranscriptHistoryStore(path=path)
    store.save([_dated("2026-08-30T09:00:00+00:00", "vorher")])

    store.clear()

    assert store.load() == []
    assert store.load() == [], "the second load resurrected the cleared entries"
    assert path.is_file(), "a cleared history was quarantined as damaged"
    assert backup_path(path).is_file(), "the backup of a cleared history was moved aside"
    assert not [item for item in tmp_path.iterdir() if ".corrupt." in item.name]
