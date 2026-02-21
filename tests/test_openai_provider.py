"""Tests for OpenAI transcription provider."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

import pytest

from tts_app.transcriber.base import TranscriptionError
from tts_app.transcriber.openai_provider import (
    OPENAI_API_BASE,
    OpenAITranscriber,
)


def _fake_response(payload: bytes | str, status: int = 200):
    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")

    class _Resp:
        def __init__(self):
            self.status = status

        def read(self):
            return data

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return _Resp()


class TestOpenAIProviderInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(TranscriptionError, match="API key is missing"):
            OpenAITranscriber(api_key="")

    def test_invalid_language_mode_falls_back_to_auto(self):
        t = OpenAITranscriber(api_key="k", language_mode="zz")
        assert t._language_mode == "auto"


class TestOpenAIBatchTranscription:
    @patch("tts_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_transcribe_json_response(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"text": "hello world"}.__str__())
        t = OpenAITranscriber(api_key="key")

        # __str__ on dict isn't JSON; provider should still return something non-empty
        result = t.transcribe_batch(b"RIFF fake")
        assert isinstance(result, str)

    @patch("tts_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_transcribe_plain_text_fallback(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response("plain transcript")
        t = OpenAITranscriber(api_key="key")
        result = t.transcribe_batch(b"RIFF fake")
        assert result == "plain transcript"

    @patch("tts_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_transcribe_json_payload(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "Hallo Welt"}))
        t = OpenAITranscriber(api_key="key", language_mode="de")
        result = t.transcribe_batch(b"RIFF fake")
        assert result == "Hallo Welt"

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == f"{OPENAI_API_BASE}/audio/transcriptions"
        assert req.get_header("Authorization") == "Bearer key"
        body = req.data.decode("utf-8", errors="ignore")
        assert 'name="model"' in body
        assert 'name="language"' in body
        assert "gpt-4o-mini-transcribe" in body
        assert "de" in body

    @patch("tts_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_http_401_maps_to_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        t = OpenAITranscriber(api_key="bad-key")
        with pytest.raises(TranscriptionError, match="Authentication failed.*401"):
            t.transcribe_batch(b"RIFF fake")

    @patch("tts_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_http_429_maps_to_rate_limit(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=429, msg="Too Many Requests", hdrs={}, fp=None
        )
        t = OpenAITranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match="Rate limit exceeded.*429"):
            t.transcribe_batch(b"RIFF fake")

    @patch("tts_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_ssl_error_message_contains_proxy_hint(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("ssl: certificate_verify_failed")
        t = OpenAITranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match="SSL.*proxy"):
            t.transcribe_batch(b"RIFF fake")


class TestOpenAIConnectionTest:
    @patch("tts_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_connection_success(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response("{}", status=200)
        t = OpenAITranscriber(api_key="k")
        ok, msg = t.test_connection()
        assert ok is True
        assert "valid" in msg.lower()

    @patch("tts_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_connection_auth_failure(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        t = OpenAITranscriber(api_key="k")
        ok, msg = t.test_connection()
        assert ok is False
        assert "401" in msg


class TestOpenAIStreaming:
    def test_streaming_start_is_not_supported(self):
        t = OpenAITranscriber(api_key="k")
        with pytest.raises(NotImplementedError, match="disabled"):
            t.start_stream()

    def test_streaming_methods_are_not_supported(self):
        t = OpenAITranscriber(api_key="k")
        with pytest.raises(NotImplementedError, match="disabled"):
            t.push_audio_chunk(b"chunk")
        with pytest.raises(NotImplementedError, match="disabled"):
            t.stop_stream()
        with pytest.raises(NotImplementedError, match="disabled"):
            t.abort_stream()
