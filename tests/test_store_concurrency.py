from __future__ import annotations

import threading

from stt_app.benchmark_history import (
    BenchmarkHistoryEntry,
    BenchmarkHistoryStore,
    BenchmarkOptions,
)
from stt_app.last_recording_store import LastRecordingStore
from stt_app.local_benchmark import BenchmarkCase
from stt_app.local_model_inventory_store import LocalModelInventoryStore
from stt_app.provider_connection_test_store import ProviderConnectionTestStore


def _benchmark_entry(summary: str) -> BenchmarkHistoryEntry:
    return BenchmarkHistoryEntry.new(
        status="completed",
        summary=summary,
        options=BenchmarkOptions(
            audio_path="sample.wav",
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
        cases=[
            BenchmarkCase(
                model="small",
                device="auto",
                compute_type="int8",
                download_seconds=0.0,
                load_seconds=0.0,
                runs=[],
                error="not run",
            )
        ],
    )


def _assert_second_call_waits_for_first(
    first_call,
    second_call,
    *,
    first_loaded: threading.Event,
    release_first: threading.Event,
) -> None:
    first_thread = threading.Thread(target=first_call)
    second_thread = threading.Thread(target=second_call)

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


def test_benchmark_history_instances_serialize_read_modify_write(tmp_path):
    path = tmp_path / "benchmark_history.json"
    first_store = BenchmarkHistoryStore(path)
    second_store = BenchmarkHistoryStore(path)
    first_loaded = threading.Event()
    release_first = threading.Event()
    original_first_load = first_store.load

    def paused_first_load():
        entries = original_first_load()
        first_loaded.set()
        release_first.wait(timeout=2)
        return entries

    first_store.load = paused_first_load  # type: ignore[method-assign]
    _assert_second_call_waits_for_first(
        lambda: first_store.add_entry(_benchmark_entry("first")),
        lambda: second_store.add_entry(_benchmark_entry("second")),
        first_loaded=first_loaded,
        release_first=release_first,
    )

    assert [entry.summary for entry in BenchmarkHistoryStore(path).load()] == [
        "first",
        "second",
    ]


def test_local_inventory_instances_preserve_parallel_directories(tmp_path):
    path = tmp_path / "local_model_inventory.json"
    first_store = LocalModelInventoryStore(path)
    second_store = LocalModelInventoryStore(path)
    first_loaded = threading.Event()
    release_first = threading.Event()
    original_first_load = first_store._load_state

    def paused_first_load():
        state = original_first_load()
        first_loaded.set()
        release_first.wait(timeout=2)
        return state

    first_store._load_state = paused_first_load  # type: ignore[method-assign]
    _assert_second_call_waits_for_first(
        lambda: first_store.save_cached_models("first", ["small"]),
        lambda: second_store.save_cached_models("second", ["tiny"]),
        first_loaded=first_loaded,
        release_first=release_first,
    )

    reopened = LocalModelInventoryStore(path)
    assert reopened.load_cached_models("first") == ["small"]
    assert reopened.load_cached_models("second") == ["tiny"]


def test_provider_result_instances_preserve_parallel_providers(tmp_path):
    path = tmp_path / "provider_connection_tests.json"
    first_store = ProviderConnectionTestStore(path)
    second_store = ProviderConnectionTestStore(path)
    first_loaded = threading.Event()
    release_first = threading.Event()
    original_first_load = first_store.load_all

    def paused_first_load():
        results = original_first_load()
        first_loaded.set()
        release_first.wait(timeout=2)
        return results

    first_store.load_all = paused_first_load  # type: ignore[method-assign]
    _assert_second_call_waits_for_first(
        lambda: first_store.save_result("openai", ok=True, message="OpenAI OK"),
        lambda: second_store.save_result("groq", ok=True, message="Groq OK"),
        first_loaded=first_loaded,
        release_first=release_first,
    )

    assert set(ProviderConnectionTestStore(path).load_all()) == {"openai", "groq"}


def test_last_recording_instances_assign_one_shared_recording_id(tmp_path):
    audio_path = tmp_path / "last_recording.wav"
    state_path = tmp_path / "last_recording.json"
    audio_path.write_bytes(b"RIFF")
    first_store = LastRecordingStore(
        audio_path=audio_path,
        state_path=state_path,
    )
    second_store = LastRecordingStore(
        audio_path=audio_path,
        state_path=state_path,
    )
    first_loaded = threading.Event()
    release_first = threading.Event()
    original_first_load = first_store.load

    def paused_first_load():
        state = original_first_load()
        first_loaded.set()
        release_first.wait(timeout=2)
        return state

    first_store.load = paused_first_load  # type: ignore[method-assign]
    snapshots = []
    _assert_second_call_waits_for_first(
        lambda: snapshots.append(first_store.snapshot_managed_recording(audio_path)),
        lambda: snapshots.append(second_store.snapshot_managed_recording(audio_path)),
        first_loaded=first_loaded,
        release_first=release_first,
    )

    assert len(snapshots) == 2
    assert snapshots[0] is not None
    assert snapshots[1] is not None
    assert snapshots[0].recording_id == snapshots[1].recording_id
