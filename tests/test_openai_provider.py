"""Tests for OpenAI transcription provider."""

from __future__ import annotations

import json
import logging
import urllib.error
from unittest.mock import patch

import pytest

from stt_app.config import (
    DEFAULT_OPENAI_MODEL,
    OPENAI_ARRAY_FIELD_MODELS,
    OPENAI_MODELS,
)
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.openai_provider import (
    OPENAI_API_BASE,
    OpenAITranscriber,
)

# The three ids OpenAI notified for deprecation on 2026-08-26 and removes from
# the API on 2027-02-26. Their request must not change while they still work.
LEGACY_OPENAI_MODELS = ("gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1")


def _sent_fields(request) -> list[tuple[str, str]]:
    """The non-file multipart fields of a request, in the order they were sent.

    Parsed out of the encoded body rather than read off the provider, because
    what OpenAI receives is the body: a repeated field is only repeated if the
    encoder actually wrote it twice.
    """
    boundary = request.get_header("Content-type").split("boundary=", 1)[1]
    fields: list[tuple[str, str]] = []
    for part in request.data.split(f"--{boundary}".encode())[1:-1]:
        head, _, body = part.partition(b"\r\n\r\n")
        headers = head.decode("utf-8", errors="replace")
        if "filename=" in headers:
            continue
        name = headers.split('name="', 1)[1].split('"', 1)[0]
        fields.append((name, body[: -len(b"\r\n")].decode("utf-8")))
    return fields


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

    def test_language_outside_openai_documented_list_falls_back_to_auto(self):
        t = OpenAITranscriber(api_key="k", language_mode="am")
        assert t._language_mode == "auto"

    def test_custom_vocabulary_builds_prompt(self):
        t = OpenAITranscriber(
            api_key="k",
            model="gpt-4o-mini-transcribe",
            custom_vocabulary="Kubernetes, Splunk SOAR",
        )
        assert t._prompt == "Kubernetes, Splunk SOAR"

    def test_empty_custom_vocabulary_gives_empty_prompt(self):
        t = OpenAITranscriber(api_key="k", model="gpt-4o-mini-transcribe")
        assert t._prompt == ""

    def test_an_unknown_model_id_falls_back_to_the_default(self):
        """`gpt-live-transcribe` is the replacement OpenAI's deprecation
        notice names, and it answers on the realtime endpoint only -- the same
        shape as any id a newer build could leave in the settings file."""
        t = OpenAITranscriber(api_key="k", model="gpt-live-transcribe")
        assert t._model == DEFAULT_OPENAI_MODEL


class TestOpenAIBatchTranscription:
    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_imported_mp3_uses_matching_multipart_content_type(
        self, mock_urlopen, tmp_path
    ):
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "ok"}))
        audio_path = tmp_path / "recording.mp3"
        audio_path.write_bytes(b"mp3-data")

        OpenAITranscriber(api_key="key").transcribe_batch(audio_path)

        request = mock_urlopen.call_args[0][0]
        assert b"Content-Type: audio/mpeg\r\n" in request.data

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_transcribe_json_response(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"text": "hello world"}.__str__())
        t = OpenAITranscriber(api_key="key")

        # __str__ on dict isn't JSON; provider should still return something non-empty
        result = t.transcribe_batch(b"RIFF fake")
        assert isinstance(result, str)

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_transcribe_plain_text_fallback(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response("plain transcript")
        t = OpenAITranscriber(api_key="key")
        result = t.transcribe_batch(b"RIFF fake")
        assert result == "plain transcript"

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_transcribe_json_payload(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "Hallo Welt"}))
        t = OpenAITranscriber(
            api_key="key", language_mode="de", model="gpt-4o-mini-transcribe"
        )
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

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_custom_vocabulary_included_in_request(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "ok"}))
        t = OpenAITranscriber(
            api_key="key",
            model="gpt-4o-mini-transcribe",
            custom_vocabulary="Kubernetes, Splunk SOAR",
        )
        t.transcribe_batch(b"RIFF fake")

        req = mock_urlopen.call_args[0][0]
        body = req.data.decode("utf-8", errors="ignore")
        assert 'name="prompt"' in body
        assert "Kubernetes, Splunk SOAR" in body

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_empty_custom_vocabulary_omits_prompt_field(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "ok"}))
        t = OpenAITranscriber(api_key="key", model="gpt-4o-mini-transcribe")
        t.transcribe_batch(b"RIFF fake")

        req = mock_urlopen.call_args[0][0]
        body = req.data.decode("utf-8", errors="ignore")
        assert 'name="prompt"' not in body

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_progress_callback_reports_remote_wait(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "done"}))
        progress: list[str] = []
        t = OpenAITranscriber(api_key="key")
        t.set_progress_callback(progress.append)

        result = t.transcribe_batch(b"RIFF fake")

        assert result == "done"
        assert progress == [
            "Uploading audio to OpenAI and waiting for transcription..."
        ]

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_http_401_maps_to_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        t = OpenAITranscriber(api_key="bad-key")
        with pytest.raises(TranscriptionError, match=r"Authentication failed.*401"):
            t.transcribe_batch(b"RIFF fake")

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_http_429_maps_to_rate_limit(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=429, msg="Too Many Requests", hdrs={}, fp=None
        )
        t = OpenAITranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match=r"Rate limit exceeded.*429"):
            t.transcribe_batch(b"RIFF fake")

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_ssl_error_message_contains_proxy_hint(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("ssl: certificate_verify_failed")
        t = OpenAITranscriber(api_key="key")
        with pytest.raises(TranscriptionError, match=r"SSL.*proxy"):
            t.transcribe_batch(b"RIFF fake")


class TestOpenAIModelRoster:
    """What the app offers, against OpenAI's own pages (read 2026-09-21)."""

    def test_the_current_model_is_first_and_is_the_default(self):
        assert OPENAI_MODELS[0] == "gpt-transcribe"
        assert DEFAULT_OPENAI_MODEL == "gpt-transcribe"
        # The diarize id the endpoint also accepts is deliberately absent:
        # the app has no speaker UI.
        assert set(OPENAI_MODELS) == {"gpt-transcribe", *LEGACY_OPENAI_MODELS}

    def test_only_known_models_take_the_array_fields(self):
        assert set(OPENAI_ARRAY_FIELD_MODELS) <= set(OPENAI_MODELS)
        assert set(OPENAI_ARRAY_FIELD_MODELS) == {"gpt-transcribe"}

    def test_all_four_models_offer_the_same_languages(self):
        """OpenAI's guide enumerates no language list for `gpt-transcribe`,
        only the code formats it accepts, so every model keeps the engine's
        one list. A list narrowed for a single model would silently take
        languages away from the others."""
        from stt_app.config import OPENAI_LANGUAGE_MODES, language_modes_for_selection

        for model in OPENAI_MODELS:
            assert language_modes_for_selection("openai", model) == (
                OPENAI_LANGUAGE_MODES
            )
        assert {"auto", "de", "en"} <= set(OPENAI_LANGUAGE_MODES)

    def test_every_model_has_a_label_and_the_deprecated_ones_carry_the_date(
        self,
    ):
        from stt_app.settings_dialog_helpers import _REMOTE_MODEL_LABELS

        for model in OPENAI_MODELS:
            assert model in _REMOTE_MODEL_LABELS
        assert "current" in _REMOTE_MODEL_LABELS["gpt-transcribe"]
        for model in LEGACY_OPENAI_MODELS:
            assert "2027-02-26" in _REMOTE_MODEL_LABELS[model]


class TestGptTranscribeRequestFields:
    """`gpt-transcribe` takes repeated array fields, never the singular ones.

    "For gpt-transcribe, languages replaces the singular language field.
    Don't send both fields." -- OpenAI's speech-to-text guide.
    """

    @staticmethod
    def _fields(mock_urlopen, **kwargs) -> list[tuple[str, str]]:
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "ok"}))
        transcriber = OpenAITranscriber(
            api_key="key", model="gpt-transcribe", **kwargs
        )
        assert transcriber.transcribe_batch(b"RIFF fake") == "ok"
        return _sent_fields(mock_urlopen.call_args[0][0])

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_auto_language_sends_no_language_field_at_all(self, mock_urlopen):
        assert self._fields(mock_urlopen) == [
            ("model", "gpt-transcribe"),
            ("response_format", "json"),
        ]

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_a_selected_language_goes_out_as_the_array_field(self, mock_urlopen):
        fields = self._fields(mock_urlopen, language_mode="de")

        assert fields == [
            ("model", "gpt-transcribe"),
            ("languages[]", "de"),
            ("response_format", "json"),
        ]
        assert ("language", "de") not in fields

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_each_vocabulary_term_is_its_own_keywords_field(self, mock_urlopen):
        fields = self._fields(
            mock_urlopen,
            language_mode="de",
            custom_vocabulary="Kubernetes, Splunk SOAR, Zscaler",
        )

        assert fields == [
            ("model", "gpt-transcribe"),
            ("languages[]", "de"),
            ("keywords[]", "Kubernetes"),
            ("keywords[]", "Splunk SOAR"),
            ("keywords[]", "Zscaler"),
            ("response_format", "json"),
        ]
        # Never the comma-joined prompt beside them.
        assert [name for name, _ in fields].count("prompt") == 0

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_no_vocabulary_sends_no_keywords_field(self, mock_urlopen):
        fields = self._fields(mock_urlopen, custom_vocabulary="   ")

        assert [name for name, _ in fields] == ["model", "response_format"]

    @pytest.mark.parametrize(
        "term",
        ["<angle>", "less < than", "greater > than", "carriage\rreturn"],
    )
    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_a_term_openai_would_refuse_is_dropped_and_the_rest_are_sent(
        self, mock_urlopen, term, caplog
    ):
        """One forbidden character costs the whole request -- "The API rejects
        the entire request when it encounters one of these characters" -- so
        the term goes and the dictation stays."""
        with caplog.at_level(logging.INFO, logger="stt_app.transcriber.openai_provider"):
            fields = self._fields(
                mock_urlopen,
                custom_vocabulary=f"Kubernetes; {term}; Zscaler",
            )

        assert fields == [
            ("model", "gpt-transcribe"),
            ("keywords[]", "Kubernetes"),
            ("keywords[]", "Zscaler"),
            ("response_format", "json"),
        ]
        messages = [record.getMessage() for record in caplog.records]
        assert any("openai_keywords_dropped count=1" in text for text in messages)
        # The count, never the user's own text.
        assert not any(term in text for text in messages)

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_nothing_is_logged_when_no_term_was_dropped(
        self, mock_urlopen, caplog
    ):
        with caplog.at_level(logging.INFO, logger="stt_app.transcriber.openai_provider"):
            self._fields(mock_urlopen, custom_vocabulary="Kubernetes")

        assert not [
            record
            for record in caplog.records
            if "openai_keywords_dropped" in record.getMessage()
        ]


class TestLegacyOpenAIRequestFields:
    @pytest.mark.parametrize("model", LEGACY_OPENAI_MODELS)
    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_the_deprecated_models_send_exactly_what_they_always_sent(
        self, mock_urlopen, model
    ):
        """They work until 2027-02-26 and have no `keywords[]`/`languages[]`
        field, so the new shape must not leak into their request."""
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "ok"}))
        transcriber = OpenAITranscriber(
            api_key="key",
            model=model,
            language_mode="de",
            custom_vocabulary="Kubernetes, Splunk SOAR",
        )

        transcriber.transcribe_batch(b"RIFF fake")

        assert _sent_fields(mock_urlopen.call_args[0][0]) == [
            ("model", model),
            ("language", "de"),
            ("prompt", "Kubernetes, Splunk SOAR"),
            ("response_format", "json"),
        ]

    @pytest.mark.parametrize("model", LEGACY_OPENAI_MODELS)
    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_a_term_with_an_angle_bracket_still_reaches_the_prompt(
        self, mock_urlopen, model
    ):
        """The character ban is a `keywords[]` rule; dropping the term from a
        `prompt` would remove biasing these models accept."""
        mock_urlopen.return_value = _fake_response(json.dumps({"text": "ok"}))
        transcriber = OpenAITranscriber(
            api_key="key", model=model, custom_vocabulary="<div>"
        )

        transcriber.transcribe_batch(b"RIFF fake")

        assert ("prompt", "<div>") in _sent_fields(mock_urlopen.call_args[0][0])


class TestOpenAIConnectionTest:
    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
    def test_connection_success(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response("{}", status=200)
        t = OpenAITranscriber(api_key="k")
        ok, msg = t.test_connection()
        assert ok is True
        assert "valid" in msg.lower()

    @patch("stt_app.transcriber.openai_provider.urllib.request.urlopen")
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
