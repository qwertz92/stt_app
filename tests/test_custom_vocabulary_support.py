"""`supports_custom_vocabulary` must answer exactly what the factory does.

The General tab now tells the user whether the selected model uses the custom
vocabulary, and a note that says "uses it" for a runtime with no biasing input
is worse than no note at all. The claim can only be checked where the terms
are actually handed over, so every case here drives the real
``create_transcriber`` with every transcriber class replaced by a recorder and
compares "did this constructor receive ``custom_vocabulary``" with what
``supports_custom_vocabulary`` promised.
"""

from __future__ import annotations

import pytest

from stt_app import config
from stt_app.config import (
    CUSTOM_VOCABULARY_ENGINES,
    CUSTOM_VOCABULARY_SUPPORTED_SUMMARY,
    DEFAULT_ENGINE,
    LOCAL_MODEL_RUNTIME,
    VALID_ENGINES,
    VALID_MODEL_SIZES,
    supports_custom_vocabulary,
)
from stt_app.settings_store import AppSettings
from stt_app.transcriber import factory

# Every class `create_transcriber` may instantiate, by the name it is bound to
# in the factory's own module namespace -- that is what the call resolves, so
# patching there covers the two that are imported inside the function body.
_TRANSCRIBER_NAMES = (
    "LocalFasterWhisperTranscriber",
    "LocalNemotronTranscriber",
    "LocalOnnxWebGpuTranscriber",
    "AssemblyAITranscriber",
    "GroqTranscriber",
    "OpenAITranscriber",
    "DeepgramTranscriber",
    "ElevenLabsTranscriber",
    "AzureLlmSpeechTranscriber",
    "FunAsrTranscriber",
)


class _Recorder:
    """Stands in for a transcriber and keeps the kwargs it was built with."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


@pytest.fixture
def factory_kwargs(monkeypatch):
    """Build any engine/model through the real factory, return its kwargs."""
    for name in _TRANSCRIBER_NAMES:
        monkeypatch.setattr(factory, name, _Recorder, raising=True)
    # The two runtimes the factory imports inside the function body.
    import stt_app.transcriber.local_granite_ctc as granite_ctc
    import stt_app.transcriber.local_onnx_asr as onnx_asr

    monkeypatch.setattr(onnx_asr, "LocalOnnxAsrTranscriber", _Recorder)
    monkeypatch.setattr(granite_ctc, "LocalGraniteCtcTranscriber", _Recorder)

    class _SecretStore:
        def get_api_key(self, _provider: str) -> str:
            return "test-key"

    def build(engine: str, model: str = "") -> dict[str, object]:
        settings = AppSettings(engine=engine, custom_vocabulary="Kubernetes")
        if engine == DEFAULT_ENGINE and model:
            settings = AppSettings(
                engine=engine,
                model_size=model,
                custom_vocabulary="Kubernetes",
            )
        built = factory.create_transcriber(settings, secret_store=_SecretStore())
        assert isinstance(built, _Recorder), engine
        return built.kwargs

    return build


@pytest.mark.parametrize("model", VALID_MODEL_SIZES)
def test_the_local_answer_matches_what_the_factory_hands_the_runtime(
    factory_kwargs,
    model: str,
) -> None:
    kwargs = factory_kwargs(DEFAULT_ENGINE, model)
    received = "custom_vocabulary" in kwargs

    assert received is supports_custom_vocabulary(DEFAULT_ENGINE, model), model
    if received:
        assert kwargs["custom_vocabulary"] == "Kubernetes"


@pytest.mark.parametrize("engine", [e for e in VALID_ENGINES if e != DEFAULT_ENGINE])
def test_the_remote_answer_matches_what_the_factory_hands_the_provider(
    factory_kwargs,
    engine: str,
) -> None:
    kwargs = factory_kwargs(engine)
    received = "custom_vocabulary" in kwargs

    assert received is supports_custom_vocabulary(engine), engine
    if received:
        assert kwargs["custom_vocabulary"] == "Kubernetes"


def test_an_unknown_engine_is_answered_by_its_model_like_the_fallback(
    factory_kwargs,
) -> None:
    """An unknown engine falls back to `_create_local_transcriber`, so its
    answer is the *model's*, not a flat True or False -- with the stored model
    being Parakeet by default, a flat True would promise biasing to a runtime
    that has no input for it."""

    def kwargs_for(model: str) -> dict[str, object]:
        settings = AppSettings(
            engine="no-such-engine",
            model_size=model,
            custom_vocabulary="Kubernetes",
        )

        class _SecretStore:
            def get_api_key(self, _provider: str) -> str:
                return "test-key"

        return factory.create_transcriber(settings, secret_store=_SecretStore()).kwargs

    for model in ("small", "parakeet-tdt-0.6b-v3"):
        received = "custom_vocabulary" in kwargs_for(model)
        assert received is supports_custom_vocabulary("no-such-engine", model), model


def test_an_unknown_model_is_answered_like_the_factory_fallback(
    factory_kwargs,
) -> None:
    """A model with no runtime branch falls through to faster-whisper, which is
    sent the terms -- answering False would print "ignored" over a run that
    uses them."""
    assert "custom_vocabulary" in factory_kwargs(DEFAULT_ENGINE, "no-such-model")
    assert supports_custom_vocabulary(DEFAULT_ENGINE, "no-such-model") is True


def test_every_local_model_has_a_runtime_the_answer_can_be_read_from() -> None:
    """The function reads `LOCAL_MODEL_RUNTIME` and defaults a missing entry to
    faster-whisper. That default is right for a model the factory has no branch
    for, and wrong for one that gained a branch but no runtime entry."""
    missing = [model for model in VALID_MODEL_SIZES if model not in LOCAL_MODEL_RUNTIME]

    assert missing == []


def test_the_sentence_that_names_the_supported_set_names_every_engine() -> None:
    """The note offers this summary as the answer to "what should I switch
    to", so an engine missing from it is a supported engine the user is never
    told about."""
    labels = {
        "assemblyai": "AssemblyAI",
        "groq": "Groq",
        "openai": "OpenAI",
        "deepgram": "Deepgram",
    }

    assert set(labels) == set(CUSTOM_VOCABULARY_ENGINES)
    for engine, label in labels.items():
        assert label in CUSTOM_VOCABULARY_SUPPORTED_SUMMARY, engine
    # The local half: every runtime that takes the terms is a Whisper one.
    assert config.CUSTOM_VOCABULARY_LOCAL_RUNTIMES == ("faster-whisper",)
    assert "Whisper models" in CUSTOM_VOCABULARY_SUPPORTED_SUMMARY
