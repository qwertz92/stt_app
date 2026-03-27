"""Tests for transcriber factory — all engine branches."""

from __future__ import annotations

from stt_app.settings_store import AppSettings
from stt_app.transcriber.factory import create_transcriber
from stt_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber
from stt_app.transcriber.assemblyai_provider import AssemblyAITranscriber
from stt_app.transcriber.deepgram_provider import DeepgramTranscriber
from stt_app.transcriber.openai_provider import OpenAITranscriber


def test_factory_local_returns_local_transcriber():
    settings = AppSettings(engine="local")
    t = create_transcriber(settings)
    assert isinstance(t, LocalFasterWhisperTranscriber)


def test_factory_assemblyai_returns_assemblyai_transcriber():
    settings = AppSettings(engine="assemblyai", assemblyai_model="nano")

    class FakeSecretStore:
        def get_api_key(self, name):
            return "test-key"

    t = create_transcriber(settings, secret_store=FakeSecretStore())
    assert isinstance(t, AssemblyAITranscriber)
    assert t._model == "nano"


def test_factory_openai_returns_openai_transcriber():
    settings = AppSettings(engine="openai")
    class FakeSecretStore:
        def get_api_key(self, name):
            return "openai-test-key"

    t = create_transcriber(settings, secret_store=FakeSecretStore())
    assert isinstance(t, OpenAITranscriber)


def test_factory_azure_falls_back_to_local():
    settings = AppSettings(engine="azure")
    t = create_transcriber(settings)
    assert isinstance(t, LocalFasterWhisperTranscriber)


def test_factory_deepgram_returns_deepgram_transcriber():
    settings = AppSettings(engine="deepgram", deepgram_model="nova-2")

    class FakeSecretStore:
        def get_api_key(self, name):
            return "test-key"

    t = create_transcriber(settings, secret_store=FakeSecretStore())
    assert isinstance(t, DeepgramTranscriber)
    assert t._model == "nova-2"


def test_factory_unknown_engine_falls_back_to_local():
    settings = AppSettings(engine="unknown_provider_xyz")
    t = create_transcriber(settings)
    assert isinstance(t, LocalFasterWhisperTranscriber)
