"""Tests for the pure-Python onnx-asr local engine (Parakeet TDT, Canary)."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from stt_app.config import (
    CANARY_MODEL_SIZE,
    LOCAL_BATCH_ONLY_MODELS,
    LOCAL_EXPLICIT_LANGUAGE_MODELS,
    LOCAL_ONNX_ASR_MODEL_SIZES,
    LOCAL_ONNX_MODEL_SIZES,
    MODEL_REPO_MAP,
    PARAKEET_MODEL_SIZE,
    language_modes_for_selection,
    supports_streaming,
)
from stt_app.settings_store import AppSettings
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.factory import create_transcriber
from stt_app.transcriber.local_onnx_asr import LocalOnnxAsrTranscriber


def _wav_bytes(samples: np.ndarray, sample_rate: int = 16000, channels: int = 1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.astype("<i2").tobytes())
    return buffer.getvalue()


class _FakeModel:
    def __init__(self):
        self.calls: list[dict] = []

    def recognize(self, waveform, **kwargs):
        self.calls.append({"waveform": waveform, **kwargs})
        return "  recognized text  "


def _transcriber_with_fake_model(model_size: str, language_mode: str = "auto"):
    transcriber = LocalOnnxAsrTranscriber(
        model_size=model_size, language_mode=language_mode
    )
    fake = _FakeModel()
    transcriber._model = fake
    return transcriber, fake


def test_both_models_are_registered_as_local_onnx_batch_only():
    for model_name in LOCAL_ONNX_ASR_MODEL_SIZES:
        assert model_name in LOCAL_ONNX_MODEL_SIZES
        assert model_name in LOCAL_BATCH_ONLY_MODELS
        assert model_name in MODEL_REPO_MAP
        assert supports_streaming("local", model_name) is False


def test_factory_routes_both_models_to_the_onnx_asr_runtime():
    for model_name in LOCAL_ONNX_ASR_MODEL_SIZES:
        transcriber = create_transcriber(
            AppSettings(engine="local", model_size=model_name, language_mode="de")
        )
        assert isinstance(transcriber, LocalOnnxAsrTranscriber)


def test_canary_can_never_select_auto():
    """onnx-asr hardcodes the <|en|> source/target token, so without an explicit
    language Canary *translates* German into English instead of transcribing."""
    assert CANARY_MODEL_SIZE in LOCAL_EXPLICIT_LANGUAGE_MODELS
    assert "auto" not in language_modes_for_selection("local", CANARY_MODEL_SIZE)

    transcriber = LocalOnnxAsrTranscriber(CANARY_MODEL_SIZE, language_mode="auto")
    assert transcriber._language_mode != "auto"

    transcriber.set_language_mode("auto")
    assert transcriber._language_mode != "auto"


def test_canary_sends_its_language_and_rejects_an_untrained_one():
    """An untrained ISO code raises KeyError deep inside onnx-asr, so it must be
    normalized away before the request."""
    transcriber, fake = _transcriber_with_fake_model(CANARY_MODEL_SIZE, "de")
    transcriber.transcribe_batch(_wav_bytes(np.zeros(1600, dtype=np.int16) + 100))
    assert fake.calls[0]["language"] == "de"

    transcriber.set_language_mode("zz")
    assert transcriber._language_mode in language_modes_for_selection(
        "local", CANARY_MODEL_SIZE
    )


def test_parakeet_sends_no_language_because_the_model_ignores_it():
    """Parakeet TDT v3 accepts `language=` but produces byte-identical output
    for any value, so passing one would only fake control."""
    assert language_modes_for_selection("local", PARAKEET_MODEL_SIZE) == ("auto",)

    transcriber, fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    transcriber.transcribe_batch(_wav_bytes(np.zeros(1600, dtype=np.int16) + 100))
    assert "language" not in fake.calls[0]


def test_transcribe_accepts_wav_bytes_raw_pcm_and_a_path(tmp_path):
    samples = (np.sin(np.arange(1600) / 10.0) * 3000).astype(np.int16)
    transcriber, fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)

    assert transcriber.transcribe_batch(_wav_bytes(samples)) == "recognized text"
    assert transcriber.transcribe_batch(samples.tobytes()) == "recognized text"
    wav_path = tmp_path / "clip.wav"
    wav_path.write_bytes(_wav_bytes(samples))
    assert transcriber.transcribe_batch(str(wav_path)) == "recognized text"

    for call in fake.calls:
        waveform = call["waveform"]
        assert waveform.dtype == np.float32
        assert waveform.size == samples.size
        # 16-bit PCM scaled into [-1, 1), not left as raw integers.
        assert np.abs(waveform).max() <= 1.0
        assert call["sample_rate"] == 16000


def test_stereo_wav_is_downmixed_to_mono():
    left = np.full(800, 1000, dtype=np.int16)
    right = np.full(800, 3000, dtype=np.int16)
    interleaved = np.empty(1600, dtype=np.int16)
    interleaved[0::2] = left
    interleaved[1::2] = right

    transcriber, fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    transcriber.transcribe_batch(_wav_bytes(interleaved, channels=2))

    waveform = fake.calls[0]["waveform"]
    assert waveform.size == 800
    assert waveform.max() == pytest.approx(2000 / 32768.0, rel=1e-3)


def test_empty_audio_returns_empty_without_calling_the_model():
    transcriber, fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    assert transcriber.transcribe_batch(b"") == ""
    assert fake.calls == []


def test_streaming_is_not_offered():
    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    with pytest.raises(NotImplementedError):
        transcriber.start_stream()


def test_unknown_model_is_rejected():
    with pytest.raises(TranscriptionError):
        LocalOnnxAsrTranscriber("not-a-model")


def test_offline_mode_reports_a_missing_model_instead_of_downloading(monkeypatch):
    transcriber = LocalOnnxAsrTranscriber(
        PARAKEET_MODEL_SIZE, offline_mode=True, model_dir=""
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_webgpu_asr.resolve_cached_webgpu_model_path",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(TranscriptionError, match="not cached locally"):
        transcriber._resolve_model_path()
