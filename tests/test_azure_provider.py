"""Tests for the Azure LLM Speech (MAI-Transcribe) transcription provider."""

from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stt_app.transcriber.azure_provider import (
    AzureLlmSpeechTranscriber,
    DEFAULT_AZURE_SPEECH_MODEL,
    build_transcribe_url,
    normalize_azure_endpoint,
)
from stt_app.transcriber.base import TranscriptionError

_ENDPOINT = "https://my-res.cognitiveservices.azure.com"


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


def _http_error(code: int, body: bytes | None = None):
    fp = io.BytesIO(body) if body is not None else None
    return urllib.error.HTTPError(
        url="", code=code, msg="err", hdrs={}, fp=fp
    )


class TestEndpointNormalization:
    def test_full_url_is_preserved(self):
        assert normalize_azure_endpoint(_ENDPOINT) == _ENDPOINT

    def test_trailing_slash_stripped(self):
        assert normalize_azure_endpoint(_ENDPOINT + "/") == _ENDPOINT

    def test_bare_host_gets_https(self):
        assert normalize_azure_endpoint(
            "my-res.cognitiveservices.azure.com"
        ) == _ENDPOINT

    def test_resource_name_expands_to_full_host(self):
        assert normalize_azure_endpoint("my-res") == _ENDPOINT

    def test_regional_endpoint_is_allowed(self):
        endpoint = "https://westeurope.api.cognitive.microsoft.com"
        assert normalize_azure_endpoint(endpoint) == endpoint

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://my-res.cognitiveservices.azure.com",
            "https://attacker.example",
            "https://key@my-res.cognitiveservices.azure.com",
            "https://my-res.cognitiveservices.azure.com:8443",
            "https://my-res.cognitiveservices.azure.com/other/path",
            "https://my-res.cognitiveservices.azure.com#fragment",
            "https://my-res.cognitiveservices.azure.com?redirect=evil",
        ],
    )
    def test_untrusted_endpoint_shapes_are_rejected(self, endpoint):
        with pytest.raises(TranscriptionError, match="Azure endpoint"):
            normalize_azure_endpoint(endpoint)

    def test_full_transcription_path_and_api_version_are_allowed(self):
        endpoint = (
            f"{_ENDPOINT}/speechtotext/transcriptions:transcribe"
            "?api-version=2025-10-15"
        )
        assert normalize_azure_endpoint(endpoint) == endpoint

    def test_empty_endpoint_raises(self):
        with pytest.raises(TranscriptionError, match="endpoint is missing"):
            normalize_azure_endpoint("")

    def test_build_transcribe_url_appends_path_and_version(self):
        url = build_transcribe_url(_ENDPOINT)
        assert url.startswith(
            f"{_ENDPOINT}/speechtotext/transcriptions:transcribe"
        )
        assert "api-version=" in url


class TestAzureInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(TranscriptionError, match="key is missing"):
            AzureLlmSpeechTranscriber(api_key="", endpoint=_ENDPOINT)

    def test_missing_endpoint_raises(self):
        with pytest.raises(TranscriptionError, match="endpoint is missing"):
            AzureLlmSpeechTranscriber(api_key="key", endpoint="")

    def test_default_model(self):
        t = AzureLlmSpeechTranscriber(api_key="key", endpoint=_ENDPOINT)
        assert t._model == DEFAULT_AZURE_SPEECH_MODEL

    def test_custom_model(self):
        t = AzureLlmSpeechTranscriber(
            api_key="key", endpoint=_ENDPOINT, model="mai-transcribe-1"
        )
        assert t._model == "mai-transcribe-1"

    def test_unknown_model_falls_back_to_default(self):
        t = AzureLlmSpeechTranscriber(
            api_key="key", endpoint=_ENDPOINT, model="nope"
        )
        assert t._model == DEFAULT_AZURE_SPEECH_MODEL

    def test_invalid_language_mode_falls_back_to_auto(self):
        t = AzureLlmSpeechTranscriber(
            api_key="key", endpoint=_ENDPOINT, language_mode="zz"
        )
        assert t._language_mode == "auto"

    def test_norwegian_locale_override(self):
        t = AzureLlmSpeechTranscriber(
            api_key="key", endpoint=_ENDPOINT, language_mode="no"
        )
        assert t._azure_locale() == "nb"


class TestAzureBatchTranscription:
    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_transcribe_combined_phrases(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            json.dumps({"combinedPhrases": [{"text": "Hallo Welt"}]})
        )
        t = AzureLlmSpeechTranscriber(
            api_key="azure-key",
            endpoint=_ENDPOINT,
            language_mode="de",
            model="mai-transcribe-1.5",
        )

        result = t.transcribe_batch(b"RIFF fake")

        assert result == "Hallo Welt"
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.startswith(
            f"{_ENDPOINT}/speechtotext/transcriptions:transcribe"
        )
        headers = {key.lower(): value for key, value in req.header_items()}
        assert headers["ocp-apim-subscription-key"] == "azure-key"
        assert "multipart/form-data" in headers["content-type"]
        body = req.data.decode("utf-8", errors="ignore")
        assert 'name="definition"' in body
        assert "enhancedMode" in body
        assert "mai-transcribe-1.5" in body
        assert "locales" in body
        assert '"de"' in body
        assert 'name="audio"' in body

    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_auto_language_omits_locales(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            json.dumps({"combinedPhrases": [{"text": "ok"}]})
        )
        t = AzureLlmSpeechTranscriber(
            api_key="k", endpoint=_ENDPOINT, language_mode="auto"
        )

        t.transcribe_batch(b"RIFF fake")

        body = mock_urlopen.call_args[0][0].data.decode("utf-8", errors="ignore")
        assert "locales" not in body

    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_multiple_combined_phrases_joined(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            json.dumps(
                {"combinedPhrases": [{"text": "Hello"}, {"text": "world"}]}
            )
        )
        t = AzureLlmSpeechTranscriber(api_key="k", endpoint=_ENDPOINT)

        assert t.transcribe_batch(b"RIFF fake") == "Hello world"

    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_progress_callback_reports_remote_wait(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            json.dumps({"combinedPhrases": [{"text": "done"}]})
        )
        progress: list[str] = []
        t = AzureLlmSpeechTranscriber(api_key="k", endpoint=_ENDPOINT)
        t.set_progress_callback(progress.append)

        result = t.transcribe_batch(b"RIFF fake")

        assert result == "done"
        assert progress == [
            "Uploading audio to Azure LLM Speech and waiting for transcription..."
        ]

    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_http_401_maps_to_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(401)
        t = AzureLlmSpeechTranscriber(api_key="bad", endpoint=_ENDPOINT)
        with pytest.raises(TranscriptionError, match="Authentication failed.*401"):
            t.transcribe_batch(b"RIFF fake")

    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_http_404_maps_to_endpoint_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(404)
        t = AzureLlmSpeechTranscriber(api_key="k", endpoint=_ENDPOINT)
        with pytest.raises(TranscriptionError, match="Endpoint not found.*404"):
            t.transcribe_batch(b"RIFF fake")

    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_http_429_maps_to_rate_limit(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(429)
        t = AzureLlmSpeechTranscriber(api_key="k", endpoint=_ENDPOINT)
        with pytest.raises(TranscriptionError, match="Rate limit exceeded.*429"):
            t.transcribe_batch(b"RIFF fake")

    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_http_400_surfaces_detail(self, mock_urlopen):
        body = json.dumps(
            {"error": {"message": "Enhanced mode is currently not supported yet"}}
        ).encode("utf-8")
        mock_urlopen.side_effect = _http_error(400, body)
        t = AzureLlmSpeechTranscriber(api_key="k", endpoint=_ENDPOINT)
        with pytest.raises(TranscriptionError, match="Enhanced mode is currently"):
            t.transcribe_batch(b"RIFF fake")

    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_ssl_error_message_contains_proxy_hint(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("ssl: certificate_verify_failed")
        t = AzureLlmSpeechTranscriber(api_key="k", endpoint=_ENDPOINT)
        with pytest.raises(TranscriptionError, match="SSL.*proxy"):
            t.transcribe_batch(b"RIFF fake")

    def test_missing_file_path_maps_to_friendly_error(self):
        t = AzureLlmSpeechTranscriber(api_key="k", endpoint=_ENDPOINT)
        with pytest.raises(TranscriptionError, match="missing file path"):
            t.transcribe_batch("missing.wav")


class TestAzureConnectionTest:
    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_connection_success(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            json.dumps({"combinedPhrases": []}), status=200
        )
        t = AzureLlmSpeechTranscriber(api_key="k", endpoint=_ENDPOINT)

        ok, msg = t.test_connection()

        assert ok is True
        assert "valid" in msg.lower()

    @patch("stt_app.transcriber.azure_provider.urllib.request.urlopen")
    def test_connection_auth_failure(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(401)
        t = AzureLlmSpeechTranscriber(api_key="k", endpoint=_ENDPOINT)

        ok, msg = t.test_connection()

        assert ok is False
        assert "401" in msg


class TestAzureFactoryRouting:
    def test_factory_creates_azure_transcriber(self):
        from stt_app.transcriber.factory import create_transcriber

        class FakeSecretStore:
            def get_api_key(self, provider: str) -> str | None:
                return "test-key" if provider == "azure" else None

        settings = SimpleNamespace(
            engine="azure",
            language_mode="de",
            azure_speech_model="mai-transcribe-1",
            azure_endpoint=_ENDPOINT,
        )

        transcriber = create_transcriber(settings, secret_store=FakeSecretStore())

        assert isinstance(transcriber, AzureLlmSpeechTranscriber)
        assert transcriber._api_key == "test-key"
        assert transcriber._language_mode == "de"
        assert transcriber._model == "mai-transcribe-1"

    def test_factory_uses_default_model_when_missing(self):
        from stt_app.transcriber.factory import create_transcriber

        class FakeSecretStore:
            def get_api_key(self, provider: str) -> str | None:
                return "test-key" if provider == "azure" else None

        settings = SimpleNamespace(
            engine="azure",
            language_mode="auto",
            azure_endpoint=_ENDPOINT,
        )
        transcriber = create_transcriber(settings, secret_store=FakeSecretStore())

        assert isinstance(transcriber, AzureLlmSpeechTranscriber)
        assert transcriber._model == DEFAULT_AZURE_SPEECH_MODEL
