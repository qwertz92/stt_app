"""Tests for transcriber factory — all engine branches."""

from __future__ import annotations

from stt_app.settings_store import AppSettings
from stt_app.transcriber.factory import create_transcriber
from stt_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber
from stt_app.transcriber.assemblyai_provider import AssemblyAITranscriber
from stt_app.transcriber.azure_provider import AzureLlmSpeechTranscriber
from stt_app.transcriber.deepgram_provider import DeepgramTranscriber
from stt_app.transcriber.openai_provider import OpenAITranscriber
from stt_app.transcriber.local_nemotron import LocalNemotronTranscriber
from stt_app.transcriber.local_webgpu_asr import LocalOnnxWebGpuTranscriber


def test_factory_local_returns_local_transcriber():
    settings = AppSettings(engine="local")
    t = create_transcriber(settings)
    assert isinstance(t, LocalFasterWhisperTranscriber)


def test_factory_local_webgpu_model_returns_onnx_webgpu_transcriber():
    settings = AppSettings(engine="local", model_size="cohere-transcribe-03-2026")
    t = create_transcriber(settings)
    assert isinstance(t, LocalOnnxWebGpuTranscriber)


def test_factory_local_nemotron_model_returns_nemotron_transcriber():
    settings = AppSettings(
        engine="local",
        model_size="nemotron-3.5-asr-streaming-0.6b-int4",
        vad_enabled=True,
    )

    transcriber = create_transcriber(settings)

    assert isinstance(transcriber, LocalNemotronTranscriber)
    assert transcriber.use_runtime_vad is True


def test_factory_assemblyai_returns_assemblyai_transcriber():
    settings = AppSettings(engine="assemblyai", assemblyai_model="universal-2")

    class FakeSecretStore:
        def get_api_key(self, name):
            return "test-key"

    t = create_transcriber(settings, secret_store=FakeSecretStore())
    assert isinstance(t, AssemblyAITranscriber)
    assert t._model == "universal-2"


def test_factory_openai_returns_openai_transcriber():
    settings = AppSettings(engine="openai")
    class FakeSecretStore:
        def get_api_key(self, name):
            return "openai-test-key"

    t = create_transcriber(settings, secret_store=FakeSecretStore())
    assert isinstance(t, OpenAITranscriber)


def test_factory_azure_returns_azure_transcriber():
    settings = AppSettings(
        engine="azure",
        azure_speech_model="mai-transcribe-1.5",
        azure_endpoint="https://my-res.cognitiveservices.azure.com",
    )

    class FakeSecretStore:
        def get_api_key(self, name):
            return "azure-test-key" if name == "azure" else None

    t = create_transcriber(settings, secret_store=FakeSecretStore())
    assert isinstance(t, AzureLlmSpeechTranscriber)
    assert t._model == "mai-transcribe-1.5"


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


def test_factory_unknown_engine_with_webgpu_model_preserves_webgpu_runtime():
    settings = AppSettings(
        engine="unknown_provider_xyz",
        model_size="granite-4.0-1b-speech",
    )
    t = create_transcriber(settings)
    assert isinstance(t, LocalOnnxWebGpuTranscriber)


def test_factory_unknown_engine_with_nemotron_preserves_nemotron_runtime():
    settings = AppSettings(
        engine="unknown_provider_xyz",
        model_size="nemotron-3.5-asr-streaming-0.6b-int4",
    )

    transcriber = create_transcriber(settings)

    assert isinstance(transcriber, LocalNemotronTranscriber)


def test_factory_local_passes_stream_final_full_pass():
    settings = AppSettings(engine="local", streaming_full_final_transcript=True)
    t = create_transcriber(settings)
    assert t.stream_final_full_pass is True

    settings = AppSettings(engine="local")
    t = create_transcriber(settings)
    assert t.stream_final_full_pass is False
