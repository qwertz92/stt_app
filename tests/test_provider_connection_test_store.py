from __future__ import annotations

from stt_app.provider_connection_test_store import ProviderConnectionTestStore


def test_provider_connection_test_store_round_trips_results(tmp_path):
    store = ProviderConnectionTestStore(tmp_path / "provider_connection_tests.json")

    store.save_result(
        "openai",
        ok=True,
        message="OpenAI OK",
        checked_at="2026-06-19 17:30:00",
    )

    reopened = ProviderConnectionTestStore(store.path)
    results = reopened.load_all()

    assert set(results) == {"openai"}
    assert results["openai"].ok is True
    assert results["openai"].message == "OpenAI OK"
    assert results["openai"].checked_at == "2026-06-19 17:30:00"


def test_provider_connection_test_store_ignores_unknown_providers(tmp_path):
    store = ProviderConnectionTestStore(tmp_path / "provider_connection_tests.json")

    store.save_result(
        "local",
        ok=True,
        message="Should be ignored",
        checked_at="2026-06-19 17:30:00",
    )

    assert store.load_all() == {}


def test_provider_connection_test_store_clears_result(tmp_path):
    store = ProviderConnectionTestStore(tmp_path / "provider_connection_tests.json")
    store.save_result(
        "openai",
        ok=True,
        message="OpenAI OK",
        checked_at="2026-06-19 17:30:00",
    )

    store.clear_result("openai")

    assert store.load_all() == {}
