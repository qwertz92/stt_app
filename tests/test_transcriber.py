import io
import wave

import pytest

from tts_app.transcriber.base import TranscriptionError
from tts_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber


class Segment:
    def __init__(self, text):
        self.text = text


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio_source, language=None, vad_filter=True):
        self.calls.append(
            {
                "audio_source": audio_source,
                "language": language,
                "vad_filter": vad_filter,
            }
        )
        return [Segment("hello"), Segment("world")], {"language": "en"}


class ExplodingModel:
    def transcribe(self, audio_source, language=None, vad_filter=True):
        raise RuntimeError("model failed")


class MissingDependencyModel:
    def transcribe(self, audio_source, language=None, vad_filter=True):
        exc = ModuleNotFoundError("No module named 'requests'")
        exc.name = "requests"
        raise exc


def _build_wav_bytes(sample_rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * 160)
    return buffer.getvalue()


def test_local_transcriber_transcribe_batch_from_bytes():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: model,
    )

    text = transcriber.transcribe_batch(_build_wav_bytes())

    assert text == "hello world"
    assert len(model.calls) == 1
    assert model.calls[0]["language"] is None


def test_local_transcriber_sets_language_when_explicit():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="de",
        model_factory=lambda *args, **kwargs: model,
    )

    transcriber.transcribe_batch(_build_wav_bytes())

    assert model.calls[0]["language"] == "de"


def test_local_transcriber_wraps_model_errors():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: ExplodingModel(),
    )

    with pytest.raises(TranscriptionError):
        transcriber.transcribe_batch(_build_wav_bytes())


def test_local_transcriber_missing_dependency_message_contains_fix_hint():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: MissingDependencyModel(),
    )

    with pytest.raises(TranscriptionError) as error:
        transcriber.transcribe_batch(_build_wav_bytes())

    message = str(error.value)
    assert "requests" in message
    assert "uv sync --group dev" in message


def test_local_transcriber_reuses_model_instance_between_calls():
    model = FakeModel()
    create_calls = {"count": 0}

    def factory(*args, **kwargs):
        create_calls["count"] += 1
        return model

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=factory,
    )

    transcriber.transcribe_batch(_build_wav_bytes())
    transcriber.transcribe_batch(_build_wav_bytes())

    assert create_calls["count"] == 1


def test_local_transcriber_streaming_methods_not_implemented():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        model_factory=lambda *args, **kwargs: model,
    )

    with pytest.raises(NotImplementedError):
        transcriber.start_stream()

    with pytest.raises(NotImplementedError):
        transcriber.push_audio_chunk(b"abc")
