"""Tests for ElevenLabs transcription provider."""

from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.elevenlabs_provider import (
    DEFAULT_ELEVENLABS_MODEL,
    ELEVENLABS_API_BASE,
    ElevenLabsTranscriber,
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


class TestElevenLabsInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(TranscriptionError, match="API key is missing"):
            ElevenLabsTranscriber(api_key="")

    def test_default_model(self):
        t = ElevenLabsTranscriber(api_key="key")
        assert t._model == DEFAULT_ELEVENLABS_MODEL

    def test_custom_model(self):
        t = ElevenLabsTranscriber(api_key="key", model="scribe_v1")
        assert t._model == "scribe_v1"

    def test_invalid_language_mode_falls_back_to_auto(self):
        t = ElevenLabsTranscriber(api_key="key", language_mode="zz")
        assert t._language_mode == "auto"

    def test_scribe_specific_language_is_preserved(self):
        t = ElevenLabsTranscriber(api_key="key", language_mode="ast")
        assert t._language_mode == "ast"

    def test_language_outside_scribe_list_falls_back_to_auto(self):
        t = ElevenLabsTranscriber(api_key="key", language_mode="ba")
        assert t._language_mode == "auto"


class TestElevenLabsBatchTranscription:
    @patch("stt_app.transcriber.elevenlabs_provider.urllib.request.urlopen")
    def test_imported_m4a_uses_matching_multipart_content_type(
        self, mock_urlopen, tmp_path
    ):
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "ok"}))
        audio_path = tmp_path / "recording.m4a"
        audio_path.write_bytes(b"m4a-data")

        ElevenLabsTranscriber(api_key="key").transcribe_batch(audio_path)

        request = mock_urlopen.call_args[0][0]
        assert b"Content-Type: audio/mp4\r\n" in request.data

    @patch("stt_app.transcriber.elevenlabs_provider.urllib.request.urlopen")
    def test_transcribe_json_payload(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "Hallo Welt"}))
        t = ElevenLabsTranscriber(
            api_key="xi-key",
            language_mode="de",
            model="scribe_v1",
        )

        result = t.transcribe_batch(b"RIFF fake")

        assert result == "Hallo Welt"
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == f"{ELEVENLABS_API_BASE}/speech-to-text"
        headers = {key.lower(): value for key, value in req.header_items()}
        assert headers["xi-api-key"] == "xi-key"
        assert "multipart/form-data" in headers["content-type"]
        body = req.data.decode("utf-8", errors="ignore")
        assert 'name="model_id"' in body
        assert "scribe_v1" in body
        assert 'name="language_code"' in body
        assert "deu" in body

    @patch("stt_app.transcriber.elevenlabs_provider.urllib.request.urlopen")
    def test_transcribe_plain_text_fallback(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response("plain transcript")
        t = ElevenLabsTranscriber(api_key="xi-key")

        result = t.transcribe_batch(b"RIFF fake")

        assert result == "plain transcript"

    @patch("stt_app.transcriber.elevenlabs_provider.urllib.request.urlopen")
    def test_progress_callback_reports_remote_wait(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "done"}))
        progress: list[str] = []
        t = ElevenLabsTranscriber(api_key="xi-key")
        t.set_progress_callback(progress.append)

        result = t.transcribe_batch(b"RIFF fake")

        assert result == "done"
        assert progress == [
            "Uploading audio to ElevenLabs and waiting for transcription..."
        ]

    @patch("stt_app.transcriber.elevenlabs_provider.urllib.request.urlopen")
    def test_http_401_maps_to_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        t = ElevenLabsTranscriber(api_key="bad-key")
        with pytest.raises(TranscriptionError, match="Authentication failed.*401"):
            t.transcribe_batch(b"RIFF fake")

    @patch("stt_app.transcriber.elevenlabs_provider.urllib.request.urlopen")
    def test_http_429_maps_to_rate_limit(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=429, msg="Too Many Requests", hdrs={}, fp=None
        )
        t = ElevenLabsTranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match="Rate limit exceeded.*429"):
            t.transcribe_batch(b"RIFF fake")

    @patch("stt_app.transcriber.elevenlabs_provider.urllib.request.urlopen")
    def test_ssl_error_message_contains_proxy_hint(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("ssl: certificate_verify_failed")
        t = ElevenLabsTranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match="SSL.*proxy"):
            t.transcribe_batch(b"RIFF fake")

    def test_missing_file_path_maps_to_friendly_error(self):
        t = ElevenLabsTranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match="missing file path"):
            t.transcribe_batch("missing.wav")


class TestElevenLabsConnectionTest:
    @patch("stt_app.transcriber.elevenlabs_provider.urllib.request.urlopen")
    def test_connection_success(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response("{}", status=200)
        t = ElevenLabsTranscriber(api_key="k")

        ok, msg = t.test_connection()

        assert ok is True
        assert "valid" in msg.lower()

    @patch("stt_app.transcriber.elevenlabs_provider.urllib.request.urlopen")
    def test_connection_auth_failure(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        t = ElevenLabsTranscriber(api_key="k")

        ok, msg = t.test_connection()

        assert ok is False
        assert "401" in msg


class TestElevenLabsFactoryRouting:
    def test_factory_creates_elevenlabs_transcriber(self):
        from stt_app.transcriber.factory import create_transcriber

        class FakeSecretStore:
            def get_api_key(self, provider: str) -> str | None:
                return "test-key" if provider == "elevenlabs" else None

        settings = SimpleNamespace(
            engine="elevenlabs",
            language_mode="de",
            elevenlabs_model="scribe_v1",
        )

        transcriber = create_transcriber(settings, secret_store=FakeSecretStore())

        assert isinstance(transcriber, ElevenLabsTranscriber)
        assert transcriber._api_key == "test-key"
        assert transcriber._language_mode == "de"
        assert transcriber._model == "scribe_v1"

    def test_factory_uses_default_model_when_missing(self):
        from stt_app.transcriber.factory import create_transcriber

        class FakeSecretStore:
            def get_api_key(self, provider: str) -> str | None:
                return "test-key" if provider == "elevenlabs" else None

        settings = SimpleNamespace(engine="elevenlabs", language_mode="auto")
        transcriber = create_transcriber(settings, secret_store=FakeSecretStore())

        assert isinstance(transcriber, ElevenLabsTranscriber)
        assert transcriber._model == DEFAULT_ELEVENLABS_MODEL
