"""Mistral Voxtral batch provider: the request it sends and how it reads the
answer, against a fake `urlopen`. Nothing here reaches the network."""

from __future__ import annotations

import json

import pytest
from _rest_fakes import (
    fake_response,
    http_error,
    json_response,
    seconds_of,
    sent_fields,
    sent_file,
    wav_seconds,
)

from stt_app.config import (
    DEFAULT_MISTRAL_MODEL,
    MISTRAL_LANGUAGE_MODES,
    MISTRAL_MODELS,
    language_modes_for_selection,
    remote_batch_part_limit,
)
from stt_app.transcriber import mistral_provider as provider_module
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.mistral_provider import MistralTranscriber

_URL = "https://api.mistral.ai/v1/audio/transcriptions"


class _Recorder:
    def __init__(self, answer=None):
        self.answer = answer if answer is not None else json_response({"text": " Hallo Welt. "})
        self.requests = []

    def __call__(self, request, timeout=None, context=None):
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def recorder(monkeypatch):
    fake = _Recorder()
    monkeypatch.setattr(provider_module.urllib.request, "urlopen", fake)
    return fake


def test_a_recording_is_sent_with_the_model_and_its_text_returned(recorder):
    text = MistralTranscriber("mi-key").transcribe_batch(wav_seconds(1.0))

    assert text == "Hallo Welt."
    (request,) = recorder.requests
    assert request.full_url == _URL
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer mi-key"
    assert sent_fields(request) == [("model", DEFAULT_MISTRAL_MODEL)]
    name, filename, body = sent_file(request)
    assert (name, filename) == ("file", "audio.wav")
    assert seconds_of(body) == pytest.approx(1.0)


def test_the_model_is_pinned_to_voxtral_mini_transcribe_2():
    # A pinned id, not `voxtral-mini-latest`: an alias moves under the user.
    assert DEFAULT_MISTRAL_MODEL == "voxtral-mini-2602"
    assert MISTRAL_MODELS == ("voxtral-mini-2602",)


def test_a_chosen_language_is_sent_and_auto_is_not(recorder):
    MistralTranscriber("k", language_mode="de").transcribe_batch(wav_seconds(1.0))
    MistralTranscriber("k", language_mode="auto").transcribe_batch(wav_seconds(1.0))
    assert ("language", "de") in sent_fields(recorder.requests[0])
    assert all(name != "language" for name, _ in sent_fields(recorder.requests[1]))


def test_the_thirteen_documented_languages_are_offered():
    assert MISTRAL_LANGUAGE_MODES == (
        "auto", "en", "zh", "hi", "es", "ar", "fr", "pt", "ru", "de", "ja", "ko",
        "it", "nl",
    )
    assert language_modes_for_selection("mistral") == MISTRAL_LANGUAGE_MODES


def test_a_language_mistral_does_not_offer_falls_back_to_auto(recorder):
    MistralTranscriber("k", language_mode="pl").transcribe_batch(wav_seconds(1.0))
    assert all(name != "language" for name, _ in sent_fields(recorder.requests[0]))


def test_each_vocabulary_term_is_its_own_context_bias_field(recorder):
    MistralTranscriber("k", custom_vocabulary="Kubernetes; Grafana").transcribe_batch(
        wav_seconds(1.0)
    )
    fields = sent_fields(recorder.requests[0])
    assert [value for name, value in fields if name == "context_bias"] == [
        "Kubernetes",
        "Grafana",
    ]


def test_an_answer_without_text_is_an_error_not_silence(recorder):
    recorder.answer = json_response({"object": "error", "message": "x"})
    with pytest.raises(TranscriptionError, match="no 'text'"):
        MistralTranscriber("k").transcribe_batch(wav_seconds(1.0))


def test_an_empty_text_is_an_empty_transcript(recorder):
    recorder.answer = json_response({"text": ""})
    assert MistralTranscriber("k").transcribe_batch(wav_seconds(1.0)) == ""


def test_an_html_page_is_never_a_transcript(recorder):
    recorder.answer = fake_response("<!DOCTYPE html><html>sign in</html>")
    with pytest.raises(TranscriptionError, match="HTML"):
        MistralTranscriber("k").transcribe_batch(wav_seconds(1.0))


def test_an_invalid_key_says_so(recorder):
    recorder.answer = http_error(_URL, 401)
    with pytest.raises(TranscriptionError, match="Authentication failed"):
        MistralTranscriber("k").transcribe_batch(wav_seconds(1.0))


def test_a_refused_request_says_what_the_service_said(recorder):
    recorder.answer = http_error(
        _URL, 422, json.dumps({"message": "file too long", "type": "invalid"})
    )
    with pytest.raises(TranscriptionError, match=r"HTTP 422.*file too long"):
        MistralTranscriber("k").transcribe_batch(wav_seconds(1.0))


def test_a_missing_key_is_refused():
    with pytest.raises(TranscriptionError, match="API key is missing"):
        MistralTranscriber("")


def test_the_part_limit_is_under_the_documented_one():
    limit = remote_batch_part_limit("mistral", DEFAULT_MISTRAL_MODEL)
    assert limit is not None
    assert limit.seconds <= 3600.0
    assert limit.max_bytes <= 500_000_000


def test_a_long_recording_is_split_with_the_threshold(recorder, monkeypatch):
    seen = {}

    def fake_parts(audio, request_fn, **kwargs):
        seen.update(kwargs)
        return request_fn(audio, kwargs["progress_text"])

    monkeypatch.setattr(provider_module, "transcribe_in_parts", fake_parts)
    MistralTranscriber("k", silence_gate_threshold=0.03).transcribe_batch(
        wav_seconds(1.0)
    )
    assert seen["silence_threshold"] == 0.03
    assert seen["limit"] == remote_batch_part_limit("mistral", DEFAULT_MISTRAL_MODEL)


def test_the_connection_test_reads_the_model_list(recorder):
    recorder.answer = json_response({"data": []})
    ok, message = MistralTranscriber("k").test_connection()
    assert ok, message
    (request,) = recorder.requests
    assert request.get_method() == "GET"
    assert request.full_url == "https://api.mistral.ai/v1/models"


def test_the_connection_test_reports_an_invalid_key(recorder):
    recorder.answer = http_error("https://api.mistral.ai/v1/models", 401)
    ok, message = MistralTranscriber("k").test_connection()
    assert not ok
    assert "401" in message
