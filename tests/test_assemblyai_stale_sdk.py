"""A source checkout whose Python packages were not updated after `git pull`."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from stt_app.config import ASSEMBLYAI_MIN_SDK_VERSION
from stt_app.transcriber.assemblyai_provider import (
    AssemblyAITranscriber,
    sdk_version_problem,
)
from stt_app.transcriber.base import TranscriptionError

_INSTALLED = "stt_app.transcriber.assemblyai_provider.installed_version"


@pytest.fixture
def stale(monkeypatch):
    monkeypatch.setattr(_INSTALLED, lambda _name: "1.6.1")


def _assert_one_clear_sentence(text: str) -> None:
    assert "1.6.1" in text
    assert ASSEMBLYAI_MIN_SDK_VERSION in text
    assert "uv sync" in text
    assert "pip install -r requirements-win.txt" in text
    assert "validation error" not in text


def test_the_minimum_is_the_pinned_version():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    dependencies = tomllib.loads(pyproject.read_text("utf-8"))["project"][
        "dependencies"
    ]
    assert f"assemblyai=={ASSEMBLYAI_MIN_SDK_VERSION}" in dependencies


def test_batch_names_both_versions_and_both_commands(stale):
    with pytest.raises(TranscriptionError) as caught:
        AssemblyAITranscriber(api_key="k").transcribe_batch(b"RIFF fake")
    _assert_one_clear_sentence(str(caught.value))


def test_streaming_names_both_versions_and_both_commands(stale):
    with pytest.raises(TranscriptionError) as caught:
        AssemblyAITranscriber(api_key="k").start_stream()
    _assert_one_clear_sentence(str(caught.value))


def test_the_connection_test_says_it_without_calling_the_service(stale):
    with patch("urllib.request.urlopen") as urlopen:
        ok, message = AssemblyAITranscriber(api_key="k").test_connection()
    assert ok is False
    _assert_one_clear_sentence(message)
    urlopen.assert_not_called()


def test_the_pinned_version_or_newer_is_accepted(monkeypatch):
    for version in ("1.10.11", "1.10.12", "1.11.0", "2.0.0"):
        monkeypatch.setattr(_INSTALLED, lambda _name, version=version: version)
        assert sdk_version_problem() is None
    monkeypatch.setattr(_INSTALLED, lambda _name: "1.10.10")
    assert sdk_version_problem() is not None
