from __future__ import annotations

import json

from stt_app.local_model_inventory_store import LocalModelInventoryStore
from stt_app.persistence import backup_path


def test_local_model_inventory_store_roundtrip_multiple_model_dirs(tmp_path):
    store = LocalModelInventoryStore(tmp_path / "local_model_inventory.json")

    assert store.load_cached_models("") is None

    store.save_cached_models("", ["medium", "small", "small", "unknown"])
    store.save_cached_models("/tmp/models", ["tiny"])

    assert store.load_cached_models("") == ["small", "medium"]
    assert store.load_cached_models("/tmp/models") == ["tiny"]


def test_local_model_inventory_store_clear_cached_models(tmp_path):
    store = LocalModelInventoryStore(tmp_path / "local_model_inventory.json")
    store.save_cached_models("/tmp/models", ["small"])

    store.clear_cached_models("/tmp/models")

    assert store.load_cached_models("/tmp/models") is None


def test_local_model_inventory_store_invalid_json_is_quarantined(tmp_path):
    path = tmp_path / "local_model_inventory.json"
    path.write_text("{not-json", encoding="utf-8")
    store = LocalModelInventoryStore(path)

    assert store.load_cached_models("") is None
    assert path.exists() is False
    quarantined = list(tmp_path.glob("local_model_inventory.json.corrupt.*"))
    assert len(quarantined) == 1


def test_local_model_inventory_store_recovers_from_backup(tmp_path):
    path = tmp_path / "local_model_inventory.json"
    path.write_text("{not-json", encoding="utf-8")
    backup_payload = {
        "schema_version": 1,
        "entries": {
            "": {
                "cached_models": ["large-v3", "tiny", "invalid", "tiny"],
                "updated_at": "2026-04-08T12:00:00+00:00",
            }
        },
    }
    backup_path(path).write_text(json.dumps(backup_payload), encoding="utf-8")
    store = LocalModelInventoryStore(path)

    assert store.load_cached_models("") == ["tiny", "large-v3"]

    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored["entries"][""]["cached_models"] == ["tiny", "large-v3"]