import io
import wave

import pytest

from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber


class Segment:
    def __init__(self, text):
        self.text = text


class FakeModel:
    def __init__(self):
        self.calls = []
        self.next_text = "hello world"

    def transcribe(self, audio_source, language=None, vad_filter=True):
        self.calls.append(
            {
                "audio_source": audio_source,
                "language": language,
                "vad_filter": vad_filter,
            }
        )
        words = self.next_text.split(" ")
        return [Segment(word) for word in words], {"language": "en"}


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


def _build_pcm16_chunk(sample_count=320):
    return b"\x01\x00" * sample_count


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


def test_local_transcriber_streaming_roundtrip_with_partial_callback():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        model_factory=lambda *args, **kwargs: model,
    )
    partials = []

    transcriber.start_stream(on_partial=partials.append)
    transcriber.push_audio_chunk(_build_pcm16_chunk())
    transcriber.push_audio_chunk(_build_pcm16_chunk())
    text = transcriber.stop_stream()

    assert text == "hello world"
    assert partials


def test_local_transcriber_streaming_requires_active_session():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        model_factory=lambda *args, **kwargs: FakeModel(),
    )

    with pytest.raises(TranscriptionError):
        transcriber.push_audio_chunk(b"abc")

    with pytest.raises(TranscriptionError):
        transcriber.stop_stream()


def test_local_transcriber_streaming_cannot_start_twice():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        model_factory=lambda *args, **kwargs: FakeModel(),
    )
    transcriber.start_stream()

    with pytest.raises(TranscriptionError):
        transcriber.start_stream()

    transcriber.stop_stream()


def test_local_transcriber_abort_stream_ends_session_without_error():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=10.0,
        stream_partial_min_audio_s=10.0,
        model_factory=lambda *args, **kwargs: FakeModel(),
    )
    transcriber.start_stream()
    transcriber.push_audio_chunk(_build_pcm16_chunk())

    transcriber.abort_stream()

    with pytest.raises(TranscriptionError):
        transcriber.stop_stream()


def test_stream_partial_uses_configured_window(monkeypatch):
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_partial_window_s=2.5,
        model_factory=lambda *args, **kwargs: FakeModel(),
    )
    calls = []

    def fake_transcribe(max_window_seconds=None):
        calls.append(max_window_seconds)
        return "partial text"

    monkeypatch.setattr(
        transcriber,
        "_transcribe_current_stream_buffer",
        fake_transcribe,
    )

    transcriber.start_stream(on_partial=lambda _text: None)
    transcriber.push_audio_chunk(_build_pcm16_chunk(1_600))
    transcriber.stop_stream()

    assert 2.5 in calls
    assert None in calls


def test_transcribe_current_stream_buffer_trims_to_window_size(monkeypatch):
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_sample_rate=16_000,
        model_factory=lambda *args, **kwargs: FakeModel(),
    )
    transcriber._stream_pcm_buffer = bytearray(
        _build_pcm16_chunk(sample_count=16_000 * 6)
    )
    observed = {"seconds": 0.0}

    def fake_batch(wav_bytes):
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            observed["seconds"] = wav_file.getnframes() / float(wav_file.getframerate())
        return "ok"

    monkeypatch.setattr(transcriber, "transcribe_batch", fake_batch)

    text = transcriber._transcribe_current_stream_buffer(max_window_seconds=2.0)

    assert text == "ok"
    assert observed["seconds"] == pytest.approx(2.0, rel=0.03)


class HubOfflineModel:
    def transcribe(self, audio_source, language=None, vad_filter=True):
        raise OSError(
            "An error happened while trying to locate the files on the Hub "
            "and we cannot find the appropriate snapshot folder for the "
            "specified revision on the local disk. Please check your internet "
            "connection and try again."
        )


def test_local_transcriber_hub_offline_message_is_actionable():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: HubOfflineModel(),
    )

    with pytest.raises(TranscriptionError) as error:
        transcriber.transcribe_batch(_build_wav_bytes())

    message = str(error.value)
    assert "not cached locally" in message
    assert "Offline mode" in message
    assert "restricted" in message.lower()


def test_offline_mode_passes_local_files_only():
    """offline_mode=True must pass local_files_only=True to WhisperModel."""
    captured_kwargs = {}

    def capturing_factory(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeModel()

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=capturing_factory,
        offline_mode=True,
    )
    transcriber.transcribe_batch(_build_wav_bytes())

    assert captured_kwargs.get("local_files_only") is True


def test_model_dir_passes_download_root():
    """model_dir must be forwarded as download_root to WhisperModel."""
    captured_kwargs = {}

    def capturing_factory(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeModel()

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=capturing_factory,
        model_dir="/tmp/my-models",
    )
    transcriber.transcribe_batch(_build_wav_bytes())

    assert captured_kwargs.get("download_root") == "/tmp/my-models"


def test_default_model_dir_omits_download_root():
    """When model_dir is empty, download_root should not be passed."""
    captured_kwargs = {}

    def capturing_factory(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeModel()

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=capturing_factory,
        model_dir="",
    )
    transcriber.transcribe_batch(_build_wav_bytes())

    assert "download_root" not in captured_kwargs
