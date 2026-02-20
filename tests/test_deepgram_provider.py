"""Tests for Deepgram transcription provider."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from tts_app.transcriber.deepgram_provider import (
    DEEPGRAM_API_BASE,
    DEFAULT_DEEPGRAM_MODEL,
    DeepgramTranscriber,
)
from tts_app.transcriber.base import TranscriptionError


# ---------------------------------------------------------------------------
# Helpers: fake HTTP responses
# ---------------------------------------------------------------------------


def _make_fake_response(body: dict, status: int = 200):
    """Create a fake urllib response context manager."""
    encoded = json.dumps(body).encode("utf-8")

    class FakeResponse:
        def __init__(self):
            self.status = status

        def read(self):
            return encoded

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    return FakeResponse()


def _deepgram_response(transcript: str = "hello world", confidence: float = 0.99):
    """Build a minimal Deepgram API response body."""
    return {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": transcript,
                            "confidence": confidence,
                        }
                    ]
                }
            ]
        }
    }


# ---------------------------------------------------------------------------
# Tests: constructor validation
# ---------------------------------------------------------------------------


class TestDeepgramTranscriberInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(TranscriptionError, match="API key is missing"):
            DeepgramTranscriber(api_key="")

    def test_none_api_key_raises(self):
        with pytest.raises(TranscriptionError, match="API key is missing"):
            DeepgramTranscriber(api_key=None)

    def test_valid_api_key_accepted(self):
        t = DeepgramTranscriber(api_key="test-key")
        assert t._api_key == "test-key"

    def test_default_model(self):
        t = DeepgramTranscriber(api_key="key")
        assert t._model == DEFAULT_DEEPGRAM_MODEL

    def test_custom_model(self):
        t = DeepgramTranscriber(api_key="key", model="nova-2")
        assert t._model == "nova-2"

    def test_default_language_mode(self):
        t = DeepgramTranscriber(api_key="key")
        assert t._language_mode == "auto"

    def test_custom_language_mode(self):
        t = DeepgramTranscriber(api_key="key", language_mode="de")
        assert t._language_mode == "de"


# ---------------------------------------------------------------------------
# Tests: batch transcription
# ---------------------------------------------------------------------------


class TestDeepgramTranscribeBatch:
    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_transcribe_bytes(self, mock_urlopen):
        """Transcription with raw WAV bytes."""
        mock_urlopen.return_value = _make_fake_response(
            _deepgram_response("Hallo Welt")
        )
        t = DeepgramTranscriber(api_key="test-key", language_mode="de")

        result = t.transcribe_batch(b"RIFF fake wav data")
        assert result == "Hallo Welt"

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_transcribe_file_path(self, mock_urlopen, tmp_path):
        """Transcription with a file path reads the file."""
        mock_urlopen.return_value = _make_fake_response(
            _deepgram_response("hello world")
        )
        t = DeepgramTranscriber(api_key="test-key")

        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF fake wav data")

        result = t.transcribe_batch(str(wav))
        assert result == "hello world"

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_transcribe_path_object(self, mock_urlopen, tmp_path):
        """Transcription with a Path object."""
        mock_urlopen.return_value = _make_fake_response(
            _deepgram_response("test output")
        )
        t = DeepgramTranscriber(api_key="test-key")

        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF fake wav data")

        result = t.transcribe_batch(wav)
        assert result == "test output"

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_transcribe_empty_result(self, mock_urlopen):
        """Empty transcript text returns empty string."""
        mock_urlopen.return_value = _make_fake_response(
            _deepgram_response("")
        )
        t = DeepgramTranscriber(api_key="test-key")
        result = t.transcribe_batch(b"RIFF fake")
        assert result == ""

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_transcribe_strips_whitespace(self, mock_urlopen):
        """Result text is stripped of whitespace."""
        mock_urlopen.return_value = _make_fake_response(
            _deepgram_response("  trimmed text  ")
        )
        t = DeepgramTranscriber(api_key="test-key")
        result = t.transcribe_batch(b"RIFF fake")
        assert result == "trimmed text"

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_auto_language_sends_detect_language(self, mock_urlopen):
        """Auto language mode sends detect_language=true query param."""
        mock_urlopen.return_value = _make_fake_response(
            _deepgram_response("ok")
        )
        t = DeepgramTranscriber(api_key="key", language_mode="auto")
        t.transcribe_batch(b"RIFF fake")

        # Inspect the URL passed to urlopen.
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "detect_language=true" in req.full_url

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_explicit_language_sends_language_param(self, mock_urlopen):
        """Explicit language mode sends language=<code> query param."""
        mock_urlopen.return_value = _make_fake_response(
            _deepgram_response("ok")
        )
        t = DeepgramTranscriber(api_key="key", language_mode="de")
        t.transcribe_batch(b"RIFF fake")

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "language=de" in req.full_url
        assert "detect_language" not in req.full_url

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_model_sent_in_query_params(self, mock_urlopen):
        """The selected model is sent as a query parameter."""
        mock_urlopen.return_value = _make_fake_response(
            _deepgram_response("ok")
        )
        t = DeepgramTranscriber(api_key="key", model="nova-2")
        t.transcribe_batch(b"RIFF fake")

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "model=nova-2" in req.full_url

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_authorization_header_set(self, mock_urlopen):
        """Authorization header uses 'Token <key>' format."""
        mock_urlopen.return_value = _make_fake_response(
            _deepgram_response("ok")
        )
        t = DeepgramTranscriber(api_key="my-secret-key")
        t.transcribe_batch(b"RIFF fake")

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("Authorization") == "Token my-secret-key"

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_content_type_header_set(self, mock_urlopen):
        """Content-Type header is set to audio/wav."""
        mock_urlopen.return_value = _make_fake_response(
            _deepgram_response("ok")
        )
        t = DeepgramTranscriber(api_key="key")
        t.transcribe_batch(b"RIFF fake")

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("Content-type") == "audio/wav"


# ---------------------------------------------------------------------------
# Tests: response parsing
# ---------------------------------------------------------------------------


class TestDeepgramResponseParsing:
    def test_extract_transcript_normal(self):
        body = _deepgram_response("hello world")
        assert DeepgramTranscriber._extract_transcript(body) == "hello world"

    def test_extract_transcript_empty_channels(self):
        body = {"results": {"channels": []}}
        assert DeepgramTranscriber._extract_transcript(body) == ""

    def test_extract_transcript_empty_alternatives(self):
        body = {"results": {"channels": [{"alternatives": []}]}}
        assert DeepgramTranscriber._extract_transcript(body) == ""

    def test_extract_transcript_missing_results(self):
        body = {}
        assert DeepgramTranscriber._extract_transcript(body) == ""

    def test_extract_transcript_malformed(self):
        body = {"results": "not a dict"}
        assert DeepgramTranscriber._extract_transcript(body) == ""

    def test_extract_transcript_no_transcript_field(self):
        body = {"results": {"channels": [{"alternatives": [{"confidence": 0.9}]}]}}
        assert DeepgramTranscriber._extract_transcript(body) == ""


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------


class TestDeepgramErrorHandling:
    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_http_401_gives_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        t = DeepgramTranscriber(api_key="bad-key")
        with pytest.raises(TranscriptionError, match="Authentication failed.*401"):
            t.transcribe_batch(b"RIFF fake")

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_http_402_gives_credits_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=402, msg="Payment Required", hdrs={}, fp=None
        )
        t = DeepgramTranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match="Insufficient credits.*402"):
            t.transcribe_batch(b"RIFF fake")

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_http_429_gives_rate_limit_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=429, msg="Too Many Requests", hdrs={}, fp=None
        )
        t = DeepgramTranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match="Rate limit.*429"):
            t.transcribe_batch(b"RIFF fake")

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_http_500_gives_generic_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=500, msg="Internal Server Error", hdrs={}, fp=None
        )
        t = DeepgramTranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match="HTTP 500"):
            t.transcribe_batch(b"RIFF fake")

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_ssl_error_gives_actionable_message(self, mock_urlopen):
        """SSL errors produce a message mentioning Zscaler/proxy."""
        mock_urlopen.side_effect = Exception("ssl: certificate_verify_failed")
        t = DeepgramTranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match="SSL.*Zscaler"):
            t.transcribe_batch(b"RIFF fake")

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_generic_exception_wrapped(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("Network unreachable")
        t = DeepgramTranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match="Network unreachable"):
            t.transcribe_batch(b"RIFF fake")


# ---------------------------------------------------------------------------
# Tests: connection test
# ---------------------------------------------------------------------------


class TestDeepgramConnectionTest:
    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_successful_connection(self, mock_urlopen):
        mock_urlopen.return_value = _make_fake_response(
            {"projects": []}, status=200
        )
        t = DeepgramTranscriber(api_key="key")
        ok, msg = t.test_connection()
        assert ok is True
        assert "valid" in msg.lower()

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_auth_failure(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        t = DeepgramTranscriber(api_key="bad-key")
        ok, msg = t.test_connection()
        assert ok is False
        assert "401" in msg

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("timeout")
        t = DeepgramTranscriber(api_key="key")
        ok, msg = t.test_connection()
        assert ok is False
        assert "timeout" in msg.lower()

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_ssl_error_in_connection_test(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("certificate_verify_failed")
        t = DeepgramTranscriber(api_key="key")
        ok, msg = t.test_connection()
        assert ok is False
        assert "ssl" in msg.lower()

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_connection_test_url(self, mock_urlopen):
        """Connection test uses the /projects endpoint."""
        mock_urlopen.return_value = _make_fake_response({"projects": []})
        t = DeepgramTranscriber(api_key="key")
        t.test_connection()

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "/projects" in req.full_url

    @patch("tts_app.transcriber.deepgram_provider.urllib.request.urlopen")
    def test_connection_test_auth_header(self, mock_urlopen):
        """Connection test sends correct Authorization header."""
        mock_urlopen.return_value = _make_fake_response({"projects": []})
        t = DeepgramTranscriber(api_key="my-key")
        t.test_connection()

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("Authorization") == "Token my-key"


# ---------------------------------------------------------------------------
# Tests: streaming stubs
# ---------------------------------------------------------------------------


class TestDeepgramStreamingStubs:
    def test_start_stream_not_implemented(self):
        t = DeepgramTranscriber(api_key="key")
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            t.start_stream()

    def test_push_audio_chunk_not_implemented(self):
        t = DeepgramTranscriber(api_key="key")
        with pytest.raises(NotImplementedError):
            t.push_audio_chunk(b"data")

    def test_stop_stream_not_implemented(self):
        t = DeepgramTranscriber(api_key="key")
        with pytest.raises(NotImplementedError):
            t.stop_stream()

    def test_abort_stream_not_implemented(self):
        t = DeepgramTranscriber(api_key="key")
        with pytest.raises(NotImplementedError):
            t.abort_stream()


# ---------------------------------------------------------------------------
# Tests: factory routing
# ---------------------------------------------------------------------------


class TestFactoryDeepgram:
    def test_factory_creates_deepgram_transcriber(self):
        """create_transcriber routes engine='deepgram' correctly."""
        from tts_app.settings_store import AppSettings
        from tts_app.transcriber.factory import create_transcriber

        class FakeSecretStore:
            def get_api_key(self, provider):
                if provider == "deepgram":
                    return "test-factory-key"
                return None

        settings = AppSettings(engine="deepgram", language_mode="de")
        t = create_transcriber(settings, secret_store=FakeSecretStore())
        assert isinstance(t, DeepgramTranscriber)
        assert t._api_key == "test-factory-key"
        assert t._language_mode == "de"

    def test_factory_deepgram_no_secret_store(self):
        """create_transcriber with no secret_store gives empty API key."""
        from tts_app.settings_store import AppSettings
        from tts_app.transcriber.factory import create_transcriber

        with pytest.raises(TranscriptionError, match="API key is missing"):
            settings = AppSettings(engine="deepgram")
            create_transcriber(settings, secret_store=None)


# ---------------------------------------------------------------------------
# Tests: settings_store deepgram fields
# ---------------------------------------------------------------------------


class TestSettingsStoreDeepgram:
    def test_has_deepgram_key_default_false(self):
        from tts_app.settings_store import AppSettings

        s = AppSettings()
        assert s.has_deepgram_key is False

    def test_has_deepgram_key_from_dict(self):
        from tts_app.settings_store import AppSettings

        s = AppSettings.from_dict({"has_deepgram_key": True})
        assert s.has_deepgram_key is True

    def test_deepgram_in_valid_engines(self):
        from tts_app.config import VALID_ENGINES

        assert "deepgram" in VALID_ENGINES

    def test_deepgram_engine_validated(self):
        from tts_app.settings_store import AppSettings

        s = AppSettings.from_dict({"engine": "deepgram"})
        assert s.engine == "deepgram"
