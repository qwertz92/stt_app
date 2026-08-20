"""Tests for transcriber factory — all engine branches."""

from __future__ import annotations

from dataclasses import replace

from stt_app.config import (
    LOCAL_ONNX_MODEL_SIZES,
    VALID_ENGINES,
    language_modes_for_selection,
)
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


def test_every_engine_can_change_its_language_without_being_recreated():
    """The transcriber cache applies the language instead of rebuilding.

    The controller deliberately keeps ``language_mode`` out of its cache key so
    a language switch never reloads a local model. That only holds if every
    engine really accepts a language change on a live instance.
    """

    class FakeSecretStore:
        def get_api_key(self, name):
            return "test-key"

    for engine in VALID_ENGINES:
        settings = AppSettings(
            engine=engine,
            language_mode="auto",
            # Azure refuses to build without a resource endpoint.
            azure_endpoint="https://example.cognitiveservices.azure.com",
        )
        transcriber = create_transcriber(settings, secret_store=FakeSecretStore())

        assert callable(getattr(transcriber, "set_language_mode", None)), engine

        supported = language_modes_for_selection(
            engine,
            settings.model_size,
            settings.mode,
        )
        target = "de" if "de" in supported else supported[-1]
        transcriber.set_language_mode(target)

        assert transcriber._language_mode == target, engine


def test_every_local_runtime_can_change_its_language_without_being_recreated():
    """Same guard as above for the local runtimes, which are the expensive ones.

    Reloading a local model for a language switch costs seconds and gigabytes,
    which is exactly why the controller applies the language to the live
    instance instead of rebuilding it.
    """
    for model_size in LOCAL_ONNX_MODEL_SIZES + ("small",):
        settings = AppSettings(engine="local", model_size=model_size)
        transcriber = create_transcriber(settings)

        supported = language_modes_for_selection("local", model_size, settings.mode)
        target = "de" if "de" in supported else supported[-1]
        transcriber.set_language_mode(target)

        assert transcriber._language_mode == target, model_size


def test_local_onnx_device_setting_reaches_the_transcriber():
    """The device is baked into the loaded runtime, so the setting has to reach
    the transcriber; the Benchmark tab could already pin a device but daily
    dictation always ran on `auto`."""
    base = AppSettings(engine="local", model_size="granite-speech-4.1-2b")

    for device in ("auto", "cpu", "webgpu", "dml", "gpu"):
        transcriber = create_transcriber(replace(base, local_onnx_device=device))
        assert transcriber.device == device


def test_local_onnx_device_auto_keeps_the_per_model_cpu_preference():
    nar = AppSettings(
        engine="local", model_size="granite-speech-4.1-2b-nar", local_onnx_device="auto"
    )
    explicit = replace(nar, local_onnx_device="webgpu")

    assert create_transcriber(nar).device == "cpu"
    assert create_transcriber(explicit).device == "webgpu"


def test_device_policy_reaches_nemotron_as_a_provider_order():
    """Nemotron is offered in the ONNX Device picker, so the choice must reach
    it. ORT GenAI has DirectML and CPU only, so every GPU policy means DML."""
    base = AppSettings(
        engine="local", model_size="nemotron-3.5-asr-streaming-0.6b-int4"
    )
    expected = {
        "auto": ("dml", "cpu"),
        "gpu": ("dml",),
        "dml": ("dml",),
        "webgpu": ("dml",),
        "cpu": ("cpu",),
    }
    for device, providers in expected.items():
        transcriber = create_transcriber(replace(base, local_onnx_device=device))
        assert transcriber.provider_order == providers, device
