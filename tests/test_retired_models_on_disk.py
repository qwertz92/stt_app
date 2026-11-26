"""Retired local models whose files are still on disk.

The Models tab lists them as "no longer offered" with Delete as their only
action; these tests pin the part that decides which folders belong to them and
that a delete takes exactly that folder.
"""

import pytest

from stt_app import local_model_scan_worker
from stt_app.config import RETIRED_LOCAL_MODEL_REPOS
from stt_app.transcriber import local_faster_whisper
from stt_app.transcriber.local_faster_whisper import (
    delete_cached_model,
    find_retired_models_on_disk,
)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """An empty default cache, so no test reads or touches the real one."""
    default = tmp_path / "default-cache"
    default.mkdir()
    monkeypatch.setattr(
        local_faster_whisper, "default_hf_cache_dir", lambda: str(default)
    )
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    return model_dir


def _folders(model_dir, *names):
    for name in names:
        (model_dir / name).mkdir()
        (model_dir / name / "file.bin").write_bytes(b"x")


def test_every_retired_model_folder_is_found_by_its_exact_name(cache):
    _folders(
        cache,
        "granite-4.0-1b-speech-ONNX",
        "granite-speech-4.1-2b-ONNX",
        "parakeet-tdt-0.6b-v3-ultra-onnx",
        "models--Systran--faster-whisper-tiny",
        "models--Systran--faster-whisper-base",
        "models--Systran--faster-whisper-medium",
    )

    assert find_retired_models_on_disk(str(cache)) == list(RETIRED_LOCAL_MODEL_REPOS)


def test_neighbouring_folders_are_not_retired_models(cache):
    _folders(
        cache,
        "granite-speech-5.0-2b-ONNX",
        "models--Systran--faster-whisper-small",
        "models--Systran--faster-whisper-medium.en",
        "models--Systran--faster-whisper-tiny.en",
        "fluid-speech-pro-onnx",
    )

    assert find_retired_models_on_disk(str(cache)) == []


@pytest.mark.parametrize(
    ("retired", "folder"),
    [
        ("granite-speech-4.1-2b", "granite-speech-4.1-2b-ONNX"),
        ("medium", "models--Systran--faster-whisper-medium"),
        ("parakeet-tdt-0.6b-v3-ultra", "parakeet-tdt-0.6b-v3-ultra-onnx"),
    ],
)
def test_deleting_a_retired_model_removes_exactly_its_folder(cache, retired, folder):
    neighbours = (
        "granite-speech-5.0-2b-ONNX",
        "models--Systran--faster-whisper-medium.en",
        "models--Systran--faster-whisper-small",
        "fluid-speech-pro-onnx",
    )
    _folders(cache, folder, *neighbours)

    assert delete_cached_model(retired, str(cache)) == 1

    assert not (cache / folder).exists()
    for neighbour in neighbours:
        assert (cache / neighbour / "file.bin").exists()


def test_the_scan_lists_the_runnable_models_and_then_the_retired_ones(monkeypatch):
    monkeypatch.setattr(
        local_model_scan_worker, "find_cached_models", lambda _dir: ["small"]
    )
    monkeypatch.setattr(
        local_model_scan_worker, "find_retired_models_on_disk", lambda _dir: ["tiny"]
    )

    assert local_model_scan_worker.scan_cached_models("") == ["small", "tiny"]
