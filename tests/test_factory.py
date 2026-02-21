"""Tests for transcriber factory — all engine branches."""

from __future__ import annotations

from tts_app.settings_store import AppSettings
from tts_app.transcriber.factory import create_transcriber
from tts_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber
from tts_app.transcriber.assemblyai_provider import AssemblyAITranscriber
from tts_app.transcriber.deepgram_provider import DeepgramTranscriber


def test_factory_local_returns_local_transcriber():
    settings = AppSettings(engine="local")
    t = create_transcriber(settings)
    assert isinstance(t, LocalFasterWhisperTranscriber)


def test_factory_assemblyai_returns_assemblyai_transcriber():
    settings = AppSettings(engine="assemblyai")

    class FakeSecretStore:
        def get_api_key(self, name):
            return "test-key"

    t = create_transcriber(settings, secret_store=FakeSecretStore())
    assert isinstance(t, AssemblyAITranscriber)


def test_factory_openai_falls_back_to_local():
    settings = AppSettings(engine="openai")
    t = create_transcriber(settings)
    assert isinstance(t, LocalFasterWhisperTranscriber)


def test_factory_azure_falls_back_to_local():
    settings = AppSettings(engine="azure")
    t = create_transcriber(settings)
    assert isinstance(t, LocalFasterWhisperTranscriber)


def test_factory_deepgram_returns_deepgram_transcriber():
    settings = AppSettings(engine="deepgram")

    class FakeSecretStore:
        def get_api_key(self, name):
            return "test-key"

    t = create_transcriber(settings, secret_store=FakeSecretStore())
    assert isinstance(t, DeepgramTranscriber)


def test_factory_unknown_engine_falls_back_to_local():
    settings = AppSettings(engine="unknown_provider_xyz")
    t = create_transcriber(settings)
    assert isinstance(t, LocalFasterWhisperTranscriber)
