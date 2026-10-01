"""Tests for Groq transcription provider."""

from __future__ import annotations

import io
import wave
from unittest.mock import patch

import numpy as np
import pytest

from stt_app.transcriber.base import TranscriptionCanceled, TranscriptionError
from stt_app.transcriber.groq_provider import GroqTranscriber

# ---------------------------------------------------------------------------
# Fake Groq client for injection
# ---------------------------------------------------------------------------


class FakeTranscription:
    """Mimics the Groq transcription response object."""

    def __init__(self, text: str = "hello world"):
        self.text = text


class FakeTranscriptions:
    """Mimics client.audio.transcriptions."""

    def __init__(self, text: str = "hello world"):
        self.calls: list[dict] = []
        self._text = text

    def create(self, **kwargs):
        self.calls.append(kwargs)
        # response_format="text" returns a plain string in the real SDK.
        if kwargs.get("response_format") == "text":
            return self._text
        return FakeTranscription(self._text)


class FakeAudio:
    def __init__(self, text: str = "hello world"):
        self.transcriptions = FakeTranscriptions(text)


class FakeModelsData:
    def __init__(self, model_id: str):
        self.id = model_id


class FakeModelsList:
    def __init__(self, ids: list[str] | None = None):
        self.data = [FakeModelsData(mid) for mid in (ids or ["whisper-large-v3"])]


class FakeModels:
    def __init__(self, ids: list[str] | None = None):
        self._ids = ids

    def list(self):
        return FakeModelsList(self._ids)


class FakeGroqClient:
    """Fake Groq client injected via groq_client_class."""

    def __init__(self, api_key: str = "", **kwargs):
        self.api_key = api_key
        self.audio = FakeAudio()
        self.models = FakeModels()


def _make_fake_groq_class(
    text: str = "hello world",
    model_ids: list[str] | None = None,
):
    """Build a fake Groq class that returns a FakeGroqClient."""

    class CustomFakeGroqClient(FakeGroqClient):
        def __init__(self, api_key: str = "", **kwargs):
            super().__init__(api_key=api_key)
            self.audio = FakeAudio(text)
            self.models = FakeModels(model_ids)

    return CustomFakeGroqClient


# ---------------------------------------------------------------------------
# Tests: constructor validation
# ---------------------------------------------------------------------------


class TestGroqTranscriberInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(TranscriptionError, match="API key is missing"):
            GroqTranscriber(api_key="")

    def test_none_api_key_raises(self):
        with pytest.raises(TranscriptionError, match="API key is missing"):
            GroqTranscriber(api_key=None)

    def test_valid_api_key_accepted(self):
        cls = _make_fake_groq_class()
        t = GroqTranscriber(api_key="test-key", groq_client_class=cls)
        assert t._api_key == "test-key"

    def test_default_model(self):
        cls = _make_fake_groq_class()
        t = GroqTranscriber(api_key="key", groq_client_class=cls)
        assert t._model == "whisper-large-v3-turbo"

    def test_custom_model(self):
        cls = _make_fake_groq_class()
        t = GroqTranscriber(
            api_key="key", model="whisper-large-v3", groq_client_class=cls
        )
        assert t._model == "whisper-large-v3"

    def test_non_whisper_language_falls_back_to_auto(self):
        cls = _make_fake_groq_class()
        t = GroqTranscriber(
            api_key="key",
            language_mode="ast",
            groq_client_class=cls,
        )

        assert t._language_mode == "auto"


# ---------------------------------------------------------------------------
# Tests: batch transcription
# ---------------------------------------------------------------------------


class TestGroqTranscribeBatch:
    def test_reuses_client_across_transcriptions(self):
        created = []
        base_class = _make_fake_groq_class(text="hello")

        def factory(**kwargs):
            client = base_class(**kwargs)
            created.append(client)
            return client

        t = GroqTranscriber(api_key="test-key", groq_client_class=factory)

        assert t.transcribe_batch(b"RIFF first") == "hello"
        assert t.transcribe_batch(b"RIFF second") == "hello"
        assert len(created) == 1

    def test_transcribe_file_path(self, tmp_path):
        """Transcription with a file path passes through correctly."""
        cls = _make_fake_groq_class(text="Hallo Welt")
        t = GroqTranscriber(
            api_key="test-key",
            language_mode="de",
            groq_client_class=cls,
        )

        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF fake wav data")

        result = t.transcribe_batch(str(wav))
        assert result == "Hallo Welt"

    def test_transcribe_bytes_creates_temp_file(self):
        """Transcription with WAV bytes creates a temp file."""
        cls = _make_fake_groq_class(text="hello world")
        t = GroqTranscriber(api_key="test-key", groq_client_class=cls)

        result = t.transcribe_batch(b"RIFF fake wav data")
        assert result == "hello world"

    def test_progress_callback_reports_remote_wait(self):
        cls = _make_fake_groq_class(text="done")
        progress: list[str] = []
        t = GroqTranscriber(api_key="test-key", groq_client_class=cls)
        t.set_progress_callback(progress.append)

        result = t.transcribe_batch(b"RIFF fake wav data")

        assert result == "done"
        assert progress == ["Uploading audio to Groq and waiting for transcription..."]

    def test_transcribe_empty_result(self):
        """Empty transcript text returns empty string."""
        cls = _make_fake_groq_class(text="")
        t = GroqTranscriber(api_key="test-key", groq_client_class=cls)
        result = t.transcribe_batch(b"RIFF fake")
        assert result == ""

    def test_transcribe_strips_whitespace(self):
        """Result text is stripped of whitespace."""
        cls = _make_fake_groq_class(text="  trimmed text  ")
        t = GroqTranscriber(api_key="test-key", groq_client_class=cls)
        result = t.transcribe_batch(b"RIFF fake")
        assert result == "trimmed text"

    def test_model_passed_to_api(self, tmp_path):
        """The selected model name is forwarded to the API call -- a model
        other than the default, so a request that fell back to the default
        fails here as well."""
        cls = _make_fake_groq_class(text="ok")
        t = GroqTranscriber(
            api_key="key",
            model="whisper-large-v3",
            groq_client_class=cls,
        )
        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF fake")
        t.transcribe_batch(str(wav))

        calls = t._get_client().audio.transcriptions.calls
        assert len(calls) == 1
        assert calls[0]["model"] == "whisper-large-v3"

    def test_language_passed_when_not_auto(self, tmp_path):
        """Explicit language mode forwards language parameter; Auto sends
        none and leaves the detection to the service."""
        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF fake")
        sent: dict[str, dict] = {}
        for mode in ("de", "auto"):
            t = GroqTranscriber(
                api_key="key",
                language_mode=mode,
                groq_client_class=_make_fake_groq_class(text="ok"),
            )
            t.transcribe_batch(str(wav))
            sent[mode] = t._get_client().audio.transcriptions.calls[-1]

        assert sent["de"]["language"] == "de"
        assert "language" not in sent["auto"]

    def test_custom_vocabulary_passed_as_prompt(self, tmp_path):
        """custom_vocabulary is forwarded as the Whisper-compatible prompt."""
        cls = _make_fake_groq_class(text="ok")
        t = GroqTranscriber(
            api_key="key",
            groq_client_class=cls,
            custom_vocabulary="Kubernetes, Splunk SOAR",
        )
        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF fake")
        t.transcribe_batch(str(wav))

        client = t._get_client()
        assert client.audio.transcriptions.calls[-1]["prompt"] == (
            "Kubernetes, Splunk SOAR"
        )

    def test_empty_custom_vocabulary_omits_prompt(self, tmp_path):
        cls = _make_fake_groq_class(text="ok")
        t = GroqTranscriber(api_key="key", groq_client_class=cls)
        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF fake")
        t.transcribe_batch(str(wav))

        client = t._get_client()
        assert "prompt" not in client.audio.transcriptions.calls[-1]


# ---------------------------------------------------------------------------
# Tests: a recording past the per-file limit goes out in parts
# ---------------------------------------------------------------------------


def _wav_seconds(seconds: float) -> bytes:
    """A 16 kHz mono 16-bit WAV, the format the app records."""
    count = int(seconds * 16_000)
    tone = (np.sin(np.arange(count) / 12.0) * 6000.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(tone.tobytes())
    return buffer.getvalue()


class _RecordingTranscriptions:
    """Reads each uploaded file while the SDK call is still holding it open."""

    def __init__(self, answers: list[str]):
        self._answers = answers
        self.calls: list[dict] = []
        self.files: list[bytes] = []

    def create(self, **kwargs):
        _name, handle = kwargs["file"]
        self.files.append(handle.read())
        self.calls.append(
            {key: value for key, value in kwargs.items() if key != "file"}
        )
        return self._answers[len(self.calls) - 1]


def _recording_groq_class(transcriptions: _RecordingTranscriptions):
    class _Client(FakeGroqClient):
        def __init__(self, api_key: str = "", **kwargs):
            super().__init__(api_key=api_key)
            self.audio = type("Audio", (), {"transcriptions": transcriptions})()

    return _Client


class TestGroqLongRecordings:
    """Groq's free tier takes 25 MB per file (the developer tier 100 MB, and
    the app cannot tell which tier a key has). The bound is lowered here so
    the recording can be short; `test_audio_parts.py` pins the real one."""

    @pytest.fixture(autouse=True)
    def _short_bound(self, monkeypatch):
        from stt_app import config

        monkeypatch.setitem(config.REMOTE_BATCH_MAX_PART_SECONDS, "groq", 20.0)

    def test_every_part_is_sent_with_the_language_and_the_vocabulary(self):
        transcriptions = _RecordingTranscriptions(["eins", "zwei"])
        progress: list[str] = []
        t = GroqTranscriber(
            api_key="key",
            language_mode="de",
            groq_client_class=_recording_groq_class(transcriptions),
            custom_vocabulary="Kubernetes, Splunk SOAR",
        )
        t.set_progress_callback(progress.append)

        text = t.transcribe_batch(_wav_seconds(25.0))

        assert text == "eins zwei"
        assert len(transcriptions.files) == 2
        for part in transcriptions.files:
            with wave.open(io.BytesIO(part), "rb") as handle:
                assert handle.getnframes() <= 20 * 16_000
        for call in transcriptions.calls:
            assert call["language"] == "de"
            assert call["prompt"] == "Kubernetes, Splunk SOAR"
        assert progress == [
            f"Transcribing part {index} of 2. Uploading audio to Groq and "
            "waiting for transcription..."
            for index in (1, 2)
        ]

    def test_a_short_recording_is_sent_byte_identical_in_one_request(self):
        transcriptions = _RecordingTranscriptions(["kurz"])
        recording = _wav_seconds(1.5)
        t = GroqTranscriber(
            api_key="key", groq_client_class=_recording_groq_class(transcriptions)
        )

        assert t.transcribe_batch(recording) == "kurz"

        assert transcriptions.files == [recording]

    def test_a_cancel_between_parts_sends_no_further_request(self):
        transcriptions = _RecordingTranscriptions(["eins", "zwei"])
        t = GroqTranscriber(
            api_key="key", groq_client_class=_recording_groq_class(transcriptions)
        )
        t.set_cancel_check(lambda: bool(transcriptions.calls))

        with pytest.raises(TranscriptionCanceled):
            t.transcribe_batch(_wav_seconds(25.0))

        assert len(transcriptions.calls) == 1


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------


class TestGroqErrorHandling:
    def test_exception_during_transcribe_raises(self):
        """Unexpected exception during transcription raises TranscriptionError."""

        class ExplodingClient:
            def __init__(self, api_key="", **kwargs):
                self.audio = type(
                    "Audio",
                    (),
                    {
                        "transcriptions": type(
                            "T",
                            (),
                            {
                                "create": staticmethod(
                                    lambda **kw: (_ for _ in ()).throw(
                                        ConnectionError("Network unreachable")
                                    )
                                )
                            },
                        )()
                    },
                )()

        t = GroqTranscriber(api_key="key", groq_client_class=ExplodingClient)
        with pytest.raises(TranscriptionError, match="Network unreachable"):
            t.transcribe_batch(b"RIFF fake")

    def test_ssl_error_gives_actionable_message(self):
        """SSL errors produce a message mentioning Zscaler/proxy."""

        class SSLClient:
            def __init__(self, api_key="", **kwargs):
                self.audio = type(
                    "Audio",
                    (),
                    {
                        "transcriptions": type(
                            "T",
                            (),
                            {
                                "create": staticmethod(
                                    lambda **kw: (_ for _ in ()).throw(
                                        Exception("ssl: certificate_verify_failed")
                                    )
                                )
                            },
                        )()
                    },
                )()

        t = GroqTranscriber(api_key="key", groq_client_class=SSLClient)
        with pytest.raises(TranscriptionError, match=r"SSL.*Zscaler"):
            t.transcribe_batch(b"RIFF fake")

    def test_auth_error_gives_clear_message(self):
        """AuthenticationError type name results in clear message."""

        class AuthenticationError(Exception):
            pass

        class AuthClient:
            def __init__(self, api_key="", **kwargs):
                self.audio = type(
                    "Audio",
                    (),
                    {
                        "transcriptions": type(
                            "T",
                            (),
                            {
                                "create": staticmethod(
                                    lambda **kw: (_ for _ in ()).throw(
                                        AuthenticationError("invalid key")
                                    )
                                )
                            },
                        )()
                    },
                )()

        t = GroqTranscriber(api_key="key", groq_client_class=AuthClient)
        with pytest.raises(TranscriptionError, match="Authentication failed"):
            t.transcribe_batch(b"RIFF fake")

    def test_missing_groq_package(self):
        """Lazy import failure gives actionable error message."""
        t = GroqTranscriber.__new__(GroqTranscriber)
        t._api_key = "test-key"
        t._language_mode = "auto"
        t._model = "whisper-large-v3-turbo"
        t._groq_class = None

        with (
            patch.dict("sys.modules", {"groq": None}),
            pytest.raises(TranscriptionError, match=r"groq.*not installed"),
        ):
            t._get_groq_class()


# ---------------------------------------------------------------------------
# Tests: connection test
# ---------------------------------------------------------------------------


class TestGroqConnectionTest:
    def test_successful_connection(self):
        cls = _make_fake_groq_class(model_ids=["whisper-large-v3"])
        t = GroqTranscriber(api_key="key", groq_client_class=cls)
        ok, msg = t.test_connection()
        assert ok is True
        assert "valid" in msg.lower()

    def test_no_whisper_models_still_ok(self):
        cls = _make_fake_groq_class(model_ids=["llama-3"])
        t = GroqTranscriber(api_key="key", groq_client_class=cls)
        ok, _msg = t.test_connection()
        assert ok is True

    def test_connection_failure(self):
        class FailClient:
            def __init__(self, api_key="", **kwargs):
                self.models = type(
                    "M",
                    (),
                    {
                        "list": staticmethod(
                            lambda: (_ for _ in ()).throw(ConnectionError("timeout"))
                        )
                    },
                )()

        t = GroqTranscriber(api_key="key", groq_client_class=FailClient)
        ok, msg = t.test_connection()
        assert ok is False
        assert "timeout" in msg.lower()


# ---------------------------------------------------------------------------
# Tests: streaming stubs
# ---------------------------------------------------------------------------


class TestGroqStreamingStubs:
    def test_start_stream_not_implemented(self):
        cls = _make_fake_groq_class()
        t = GroqTranscriber(api_key="key", groq_client_class=cls)
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            t.start_stream()

    def test_push_audio_chunk_not_implemented(self):
        cls = _make_fake_groq_class()
        t = GroqTranscriber(api_key="key", groq_client_class=cls)
        with pytest.raises(NotImplementedError):
            t.push_audio_chunk(b"data")

    def test_stop_stream_not_implemented(self):
        cls = _make_fake_groq_class()
        t = GroqTranscriber(api_key="key", groq_client_class=cls)
        with pytest.raises(NotImplementedError):
            t.stop_stream()

    def test_abort_stream_not_implemented(self):
        cls = _make_fake_groq_class()
        t = GroqTranscriber(api_key="key", groq_client_class=cls)
        with pytest.raises(NotImplementedError):
            t.abort_stream()


# ---------------------------------------------------------------------------
# Tests: factory routing
# ---------------------------------------------------------------------------


class TestFactoryGroq:
    def test_factory_creates_groq_transcriber(self):
        """create_transcriber routes engine='groq' correctly."""
        from stt_app.settings_store import AppSettings
        from stt_app.transcriber.factory import create_transcriber

        class FakeSecretStore:
            def get_api_key(self, provider):
                if provider == "groq":
                    return "test-factory-key"
                return None

        settings = AppSettings(
            engine="groq",
            language_mode="de",
            groq_model="whisper-large-v3",
        )
        t = create_transcriber(settings, secret_store=FakeSecretStore())
        assert isinstance(t, GroqTranscriber)
        assert t._api_key == "test-factory-key"
        assert t._language_mode == "de"
        assert t._model == "whisper-large-v3"

    def test_factory_groq_no_secret_store(self):
        """create_transcriber with no secret_store gives empty API key."""
        from stt_app.settings_store import AppSettings
        from stt_app.transcriber.factory import create_transcriber

        with pytest.raises(TranscriptionError, match="API key is missing"):
            settings = AppSettings(engine="groq")
            create_transcriber(settings, secret_store=None)

    def test_factory_groq_default_model(self):
        """Default groq_model is used when not specified."""
        from stt_app.settings_store import AppSettings
        from stt_app.transcriber.factory import create_transcriber

        class FakeSecretStore:
            def get_api_key(self, provider):
                return "key" if provider == "groq" else None

        settings = AppSettings(engine="groq")
        t = create_transcriber(settings, secret_store=FakeSecretStore())
        assert t._model == "whisper-large-v3-turbo"


# ---------------------------------------------------------------------------
# Tests: settings_store groq fields
# ---------------------------------------------------------------------------


class TestSettingsStoreGroq:
    def test_has_groq_key_default_false(self):
        from stt_app.settings_store import AppSettings

        s = AppSettings()
        assert s.has_groq_key is False

    def test_has_groq_key_from_dict(self):
        from stt_app.settings_store import AppSettings

        s = AppSettings.from_dict({"has_groq_key": True})
        assert s.has_groq_key is True

    def test_groq_in_valid_engines(self):
        from stt_app.config import VALID_ENGINES

        assert "groq" in VALID_ENGINES

    def test_groq_engine_validated(self):
        from stt_app.settings_store import AppSettings

        s = AppSettings.from_dict({"engine": "groq"})
        assert s.engine == "groq"

    def test_groq_model_default(self):
        from stt_app.settings_store import AppSettings

        s = AppSettings()
        assert s.groq_model == "whisper-large-v3-turbo"

    def test_groq_model_from_dict(self):
        from stt_app.settings_store import AppSettings

        s = AppSettings.from_dict({"groq_model": "whisper-large-v3"})
        assert s.groq_model == "whisper-large-v3"

    def test_invalid_groq_model_falls_back(self):
        from stt_app.settings_store import AppSettings

        s = AppSettings.from_dict({"groq_model": "nonexistent"})
        assert s.groq_model == "whisper-large-v3-turbo"

    def test_groq_models_constant(self):
        from stt_app.config import GROQ_MODELS

        assert "whisper-large-v3" in GROQ_MODELS
        assert "whisper-large-v3-turbo" in GROQ_MODELS


# ---------------------------------------------------------------------------
# Groq SSL CA bundle passthrough
# ---------------------------------------------------------------------------


class TestGroqSSLBundle:
    def test_build_client_passes_httpx_verify_when_ssl_context_available(
        self, monkeypatch
    ):
        """When create_ssl_context() returns a context, _build_client should
        pass an httpx.Client with verify=<SSLContext> to the Groq constructor."""
        import ssl

        fake_ctx = ssl.create_default_context()
        monkeypatch.setattr(
            "stt_app.transcriber.groq_provider.create_ssl_context",
            lambda: fake_ctx,
        )

        captured_kwargs: list[dict] = []

        class SpyGroqClient:
            def __init__(self, **kwargs):
                captured_kwargs.append(kwargs)

        t = GroqTranscriber(
            api_key="test-key",
            groq_client_class=SpyGroqClient,
        )
        t._groq_class = SpyGroqClient
        t._build_client()

        assert len(captured_kwargs) == 1
        assert "http_client" in captured_kwargs[0]

    def test_build_client_uses_default_when_no_bundle(self, monkeypatch):
        """Without a CA bundle, _build_client should NOT pass http_client."""
        monkeypatch.setattr(
            "stt_app.transcriber.groq_provider.create_ssl_context",
            lambda: None,
        )

        captured_kwargs: list[dict] = []

        class SpyGroqClient:
            def __init__(self, **kwargs):
                captured_kwargs.append(kwargs)

        t = GroqTranscriber(
            api_key="test-key",
            groq_client_class=SpyGroqClient,
        )
        t._groq_class = SpyGroqClient
        t._build_client()

        assert len(captured_kwargs) == 1
        assert "http_client" not in captured_kwargs[0]
