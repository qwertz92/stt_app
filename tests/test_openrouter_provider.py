"""OpenRouter's transcription endpoint as an engine of its own: the request it
sends, the model list it fetches and how it reads errors, against a fake HTTP
layer. Nothing here reaches the network."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
from _rest_fakes import _parts, seconds_of, sent_fields, sent_file, wav_seconds

from stt_app.config import (
    CUSTOM_VOCABULARY_ENGINES,
    DEFAULT_OPENROUTER_MODEL,
    ENGINE_LABELS,
    OPENROUTER_API_BASE,
    STREAMING_ENGINES,
    VALID_ENGINES,
    VALID_LANGUAGE_MODES,
    language_modes_for_selection,
    remote_batch_part_limit,
    supports_custom_vocabulary,
)
from stt_app.transcriber import custom_endpoint_provider as provider_module
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.openrouter_provider import OpenRouterTranscriber

_TRANSCRIBE_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
_MODELS_URL = "https://openrouter.ai/api/v1/models?output_modalities=transcription"
_KEY_URL = "https://openrouter.ai/api/v1/key"
_KEY = "sk-or-v1-secret-key-value"


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, limit: int = -1) -> bytes:
        return self._payload if limit < 0 else self._payload[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://openrouter.ai/x", code, "Status", {}, io.BytesIO(body.encode())
    )


class _Server:
    """Stands in for `_open`: records requests, answers from a script."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.requests: list[object] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, (dict, list)):
            answer = json.dumps(answer)
        if isinstance(answer, str):
            answer = answer.encode()
        return _Response(answer)


@pytest.fixture
def server(monkeypatch):
    def install(*answers: object) -> _Server:
        fake = _Server(*answers)
        monkeypatch.setattr(provider_module, "_open", fake)
        return fake

    return install


def _fields(request) -> dict[str, str]:
    return dict(sent_fields(request))


def _model_list(count: int = 30) -> dict:
    """The shape of OpenRouter's reply (2026-11-26): `data` entries with an id
    and no `mode`, plus fields the app ignores."""
    return {
        "data": [
            {
                "id": f"vendor{index}/transcribe-{index}",
                "name": f"Vendor {index}: Transcribe {index}",
                "architecture": {"output_modalities": ["transcription"]},
                "pricing": {"prompt": "0.0000750"},
            }
            for index in range(count)
        ],
        "total_count": count,
    }


# -- the request ------------------------------------------------------------


def test_a_recording_is_posted_to_the_transcription_route_with_the_default_model(
    server,
):
    fake = server({"text": " Hallo Welt. ", "usage": {"seconds": 1}})
    audio = wav_seconds(1.0)

    text = OpenRouterTranscriber(_KEY).transcribe_batch(audio)

    assert text == "Hallo Welt."
    (request,) = fake.requests
    assert request.full_url == _TRANSCRIBE_URL
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {_KEY}"
    assert request.get_header("Accept") == "application/json"
    assert _fields(request) == {
        "model": DEFAULT_OPENROUTER_MODEL,
        "response_format": "json",
    }
    heads = [head for head, _body in _parts(request)]
    assert 'name="file"' in heads[-1] and "Content-Type: audio/wav" in heads[-1]
    name, filename, body = sent_file(request)
    assert (name, filename) == ("file", "audio.wav")
    assert body == audio


def test_the_default_model_is_openais_current_transcription_model():
    assert DEFAULT_OPENROUTER_MODEL == "openai/gpt-transcribe"
    assert OPENROUTER_API_BASE == "https://openrouter.ai/api/v1"


def test_a_blank_model_is_the_default_one(server):
    fake = server({"text": "x"})
    OpenRouterTranscriber(_KEY, model="  ").transcribe_batch(wav_seconds(1.0))
    assert _fields(fake.requests[0])["model"] == DEFAULT_OPENROUTER_MODEL


def test_a_typed_model_id_is_sent_as_it_is(server):
    fake = server({"text": "x"})
    model = "mistralai/voxtral-small-transcribe-3-0"
    OpenRouterTranscriber(_KEY, model=model).transcribe_batch(wav_seconds(1.0))
    assert _fields(fake.requests[0])["model"] == model


def test_a_chosen_language_is_sent_and_auto_is_not(server):
    fake = server({"text": "x"})
    OpenRouterTranscriber(_KEY, language_mode="de").transcribe_batch(wav_seconds(1.0))
    OpenRouterTranscriber(_KEY, language_mode="auto").transcribe_batch(wav_seconds(1.0))
    assert _fields(fake.requests[0])["language"] == "de"
    assert "language" not in _fields(fake.requests[1])


def test_the_vocabulary_is_sent_as_the_prompt_the_page_lists(server):
    # "prompt: Context to guide transcription on providers that support
    # contextual prompting" (create-transcription page, read 2026-11-26).
    fake = server({"text": "x"})
    OpenRouterTranscriber(
        _KEY, custom_vocabulary="Kubernetes; Grafana"
    ).transcribe_batch(wav_seconds(1.0))
    OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))
    assert _fields(fake.requests[0])["prompt"] == "Kubernetes, Grafana"
    assert "prompt" not in _fields(fake.requests[1])


def test_the_engine_is_registered_as_a_batch_only_engine_with_the_vocabulary():
    assert "openrouter" in VALID_ENGINES
    assert "openrouter" not in STREAMING_ENGINES
    assert ENGINE_LABELS["openrouter"] == "OpenRouter"
    assert "openrouter" in CUSTOM_VOCABULARY_ENGINES
    assert supports_custom_vocabulary("openrouter", DEFAULT_OPENROUTER_MODEL)
    assert language_modes_for_selection("openrouter") == VALID_LANGUAGE_MODES
    with pytest.raises(NotImplementedError):
        OpenRouterTranscriber(_KEY).start_stream()


def test_a_missing_key_is_refused_without_the_custom_endpoints_advice():
    with pytest.raises(TranscriptionError) as caught:
        OpenRouterTranscriber("")
    message = str(caught.value)
    assert message == (
        "OpenRouter API key is missing. Enter your key in Settings -> Providers."
    )


# -- parts ------------------------------------------------------------------


def test_the_part_limit_is_inside_the_documented_size_and_timeout():
    limit = remote_batch_part_limit("openrouter", DEFAULT_OPENROUTER_MODEL)
    assert limit is not None
    # 25 MB per upload; parts of the chat style's 300 s.
    assert limit.max_bytes <= 25_000_000
    assert limit.seconds == 300.0


def test_a_long_recording_goes_out_in_parts_within_the_limit(server):
    fake = server({"text": "part"})
    limit = remote_batch_part_limit("openrouter", DEFAULT_OPENROUTER_MODEL)

    text = OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(700.0))

    assert len(fake.requests) == 3
    assert text == "part part part"
    for request in fake.requests:
        body = sent_file(request)[2]
        assert seconds_of(body) <= limit.seconds
        assert len(body) <= limit.max_bytes


# -- errors -----------------------------------------------------------------


def test_an_invalid_key_is_named_and_the_key_is_not_shown(server):
    server(_http_error(401, json.dumps({"error": {"message": f"No key {_KEY}"}})))
    with pytest.raises(TranscriptionError) as caught:
        OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))
    message = str(caught.value)
    assert message.startswith("OpenRouter: authentication failed (HTTP 401)")
    assert _KEY not in message


def test_a_rate_limit_says_to_wait(server):
    server(_http_error(429, "{}"))
    with pytest.raises(TranscriptionError, match=r"OpenRouter: rate limit exceeded"):
        OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))


def test_a_payment_problem_shows_the_vendors_message(server):
    server(
        _http_error(
            402, json.dumps({"error": {"code": 402, "message": "Insufficient credits"}})
        )
    )
    with pytest.raises(TranscriptionError, match=r"HTTP 402.*Insufficient credits"):
        OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))


def test_a_server_error_shows_the_message_capped(server):
    server(_http_error(500, json.dumps({"error": {"message": "boom " * 200}})))
    with pytest.raises(TranscriptionError) as caught:
        OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))
    assert "HTTP 500" in str(caught.value)
    assert len(str(caught.value)) < 450


def test_an_upstream_timeout_shows_its_status(server):
    server(_http_error(504, json.dumps({"error": {"message": "upstream timeout"}})))
    with pytest.raises(TranscriptionError, match=r"HTTP 504.*upstream timeout"):
        OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))


def test_an_html_error_page_is_named_not_pasted(server):
    server(
        _http_error(
            413,
            "<html><head><title>413 Request Entity Too Large</title></head>"
            "<body>nginx</body></html>",
        )
    )
    with pytest.raises(TranscriptionError) as caught:
        OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))
    message = str(caught.value)
    assert 'HTML page titled "413 Request Entity Too Large"' in message
    assert "<html>" not in message
    # The address is built in: no advice about a base URL field.
    assert "base URL" not in message
    assert "parts of at most 300 s" in message


def test_a_wrong_route_does_not_send_the_user_to_a_url_field(server):
    server(_http_error(404, json.dumps({"error": {"message": "not found"}})))
    with pytest.raises(TranscriptionError) as caught:
        OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))
    assert "HTTP 404" in str(caught.value)
    assert "Base URL" not in str(caught.value)
    assert "base URL" not in str(caught.value)


def test_an_html_page_answering_200_is_not_a_transcript(server):
    server("<!DOCTYPE html><html>sign in</html>")
    with pytest.raises(TranscriptionError, match="HTML page"):
        OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))


def test_an_answer_without_text_is_an_error_not_silence(server):
    server({"usage": {"seconds": 1}})
    with pytest.raises(TranscriptionError, match="no 'text'"):
        OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))


def test_a_200_that_carries_an_error_object_shows_its_message(server):
    server({"error": {"code": 502, "message": "provider down"}})
    with pytest.raises(TranscriptionError, match="provider down"):
        OpenRouterTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))


# -- the model list ---------------------------------------------------------


def test_the_model_list_asks_for_transcription_models_only(server):
    fake = server(_model_list(30))

    models = OpenRouterTranscriber(_KEY).list_models()

    (request,) = fake.requests
    assert request.full_url == _MODELS_URL
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {_KEY}"
    assert len(models) == 30
    assert {model.id for model in models} == {
        f"vendor{index}/transcribe-{index}" for index in range(30)
    }


def test_the_model_list_needs_no_key_and_sends_none(server):
    fake = server(_model_list(3))

    models = OpenRouterTranscriber(listing_only=True).list_models()

    assert len(models) == 3
    assert fake.requests[0].get_header("Authorization") is None


def test_a_transcriber_cannot_be_built_without_a_key_even_if_listing_is_possible():
    with pytest.raises(TranscriptionError, match="API key is missing"):
        OpenRouterTranscriber(model="openai/gpt-transcribe")


def test_a_model_list_that_is_not_json_is_an_error_naming_openrouter(server):
    server("Gateway Timeout")
    with pytest.raises(TranscriptionError) as caught:
        OpenRouterTranscriber(_KEY).list_models()
    message = str(caught.value)
    assert message.startswith("OpenRouter: the model list is not JSON.")
    assert "base URL" not in message


@pytest.mark.parametrize("reply", [{"models": []}, {"data": "x"}, "5"])
def test_a_model_list_without_a_data_array_is_an_error(server, reply):
    server(reply)
    with pytest.raises(TranscriptionError, match="no 'data' array"):
        OpenRouterTranscriber(_KEY).list_models()


def test_a_failed_model_list_request_reports_the_status(server):
    server(_http_error(500, json.dumps({"error": {"message": "down"}})))
    with pytest.raises(TranscriptionError, match=r"model list failed \(HTTP 500\)"):
        OpenRouterTranscriber(_KEY).list_models()


# -- the connection test ------------------------------------------------------


def test_the_connection_test_checks_the_key_not_the_public_list(server):
    fake = server({"data": {"label": "sk-or-v1-xxx", "usage": 0}})

    ok, message = OpenRouterTranscriber(_KEY).test_connection()

    assert ok, message
    (request,) = fake.requests
    assert request.full_url == _KEY_URL
    assert request.get_header("Authorization") == f"Bearer {_KEY}"


def test_the_connection_test_reports_a_refused_key(server):
    server(_http_error(401, json.dumps({"error": {"message": "User not found."}})))
    ok, message = OpenRouterTranscriber(_KEY).test_connection()
    assert not ok
    assert "401" in message and "User not found" in message
    assert _KEY not in message


def test_the_connection_test_rejects_an_answer_that_is_not_key_info(server):
    server({"models": []})
    ok, message = OpenRouterTranscriber(_KEY).test_connection()
    assert not ok
    assert "unexpected" in message
