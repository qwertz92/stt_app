"""Tests for the bring-your-own OpenAI-compatible endpoint provider."""

from __future__ import annotations

import io
import json
import logging
import subprocess
import urllib.error

import pytest

from stt_app.config import (
    CUSTOM_CHAT_MAX_PART_SECONDS,
    CUSTOM_CHAT_MAX_REQUEST_BYTES,
    CUSTOM_KEY_COMMAND_TTL_S,
    OPENAI_MAX_PART_SECONDS,
    OPENAI_MAX_REQUEST_BYTES,
    remote_batch_part_limit,
)
from stt_app.transcriber import custom_endpoint_provider as provider_module
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.custom_endpoint_provider import (
    CustomEndpointModel,
    CustomEndpointTranscriber,
    normalize_custom_endpoint,
)

BASE = "https://llm-gateway.example.com/v1"
SECRET_TOKEN = "eyJ-secret-token-value"
WAV = b"RIFF-not-really-a-wav"


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
        "https://x.example", code, "Status", {}, io.BytesIO(body.encode())
    )


class _Server:
    """Stands in for `urlopen`: records requests, answers from a script."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.requests: list[object] = []

    def __call__(self, request, timeout=None, context=None):
        self.requests.append(request)
        answer = self.answers.pop(0)
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
        monkeypatch.setattr(provider_module.urllib.request, "urlopen", fake)
        return fake

    return install


def _multipart_fields(request) -> dict[str, str]:
    boundary = request.get_header("Content-type").split("boundary=", 1)[1]
    fields: dict[str, str] = {}
    for part in request.data.split(f"--{boundary}".encode())[1:-1]:
        head, _, body = part.partition(b"\r\n\r\n")
        headers = head.decode()
        if "filename=" in headers:
            continue
        name = headers.split('name="', 1)[1].split('"', 1)[0]
        fields[name] = body[: -len(b"\r\n")].decode()
    return fields


def _chat_reply(text: object) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _transcriber(**kwargs) -> CustomEndpointTranscriber:
    kwargs.setdefault("api_key", "stored-key")
    kwargs.setdefault("endpoint", BASE)
    kwargs.setdefault("model", "whisper-1")
    return CustomEndpointTranscriber(**kwargs)


# -- base URL ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("  https://llm-gateway.example.com/v1/  ", BASE),
        ("http://localhost:8000/v1", "http://localhost:8000/v1"),
        # Nothing is appended: a gateway may serve the routes at its root.
        ("https://llm-gateway.example.com", "https://llm-gateway.example.com"),
        ("https://llm-gateway.example.com/openai", "https://llm-gateway.example.com/openai"),
    ],
)
def test_the_base_url_is_used_as_given(given, expected):
    assert normalize_custom_endpoint(given) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "llm-gateway.example.com/v1", "ftp://host/v1", "https://host/v1?x=1"],
)
def test_an_unusable_base_url_is_refused_with_an_example(bad):
    with pytest.raises(TranscriptionError, match="https://"):
        normalize_custom_endpoint(bad)


def test_credentials_in_the_url_are_refused():
    with pytest.raises(TranscriptionError, match="user credentials"):
        normalize_custom_endpoint("https://u:p@host/v1")


def test_a_missing_key_and_command_is_refused():
    with pytest.raises(TranscriptionError, match="API key is missing"):
        CustomEndpointTranscriber(api_key="", endpoint=BASE)


# -- transcription API ------------------------------------------------------


def test_the_transcription_request_is_the_openai_shape(server):
    fake = server({"text": "  hallo   welt "})
    transcriber = _transcriber(language_mode="de", custom_vocabulary="Kubernetes, SOAR")

    assert transcriber.transcribe_batch(WAV) == "hallo welt"

    request = fake.requests[0]
    assert request.full_url == f"{BASE}/audio/transcriptions"
    assert request.get_header("Authorization") == "Bearer stored-key"
    assert _multipart_fields(request) == {
        "model": "whisper-1",
        "language": "de",
        "prompt": "Kubernetes, SOAR",
        "response_format": "json",
    }


def test_auto_sends_no_language_and_plain_text_is_accepted(server):
    fake = server("plain transcript")
    assert _transcriber().transcribe_batch(WAV) == "plain transcript"
    assert "language" not in _multipart_fields(fake.requests[0])


def test_no_model_is_refused_before_any_request(server):
    fake = server()
    with pytest.raises(TranscriptionError, match="No model is selected"):
        _transcriber(model="").transcribe_batch(WAV)
    assert fake.requests == []


def test_a_404_points_at_the_base_url_and_the_style(server):
    server(_http_error(404, '{"error": {"message": "no route"}}'))
    with pytest.raises(TranscriptionError) as raised:
        _transcriber().transcribe_batch(WAV)
    assert "no route" in str(raised.value)
    assert "API style" in str(raised.value)


# -- chat completions -------------------------------------------------------


def test_the_chat_request_carries_the_audio_and_the_instruction(server):
    fake = server(_chat_reply('"Hallo Welt."'))
    transcriber = _transcriber(
        api_mode="chat",
        model="gemini-2.5-flash",
        language_mode="de",
        custom_vocabulary="Kubernetes",
    )

    assert transcriber.transcribe_batch(WAV) == "Hallo Welt."

    request = fake.requests[0]
    assert request.full_url == f"{BASE}/chat/completions"
    body = json.loads(request.data)
    assert body["model"] == "gemini-2.5-flash"
    assert body["temperature"] == 0
    assert body["reasoning_effort"] == "low"
    system, user = body["messages"]
    assert system["role"] == "system"
    assert "verbatim" in system["content"]
    assert "German" in system["content"]
    assert "Spell these terms exactly as written: Kubernetes." in system["content"]
    part = user["content"][0]
    assert part["type"] == "input_audio"
    assert part["input_audio"]["format"] == "wav"
    import base64

    assert base64.b64decode(part["input_audio"]["data"]) == WAV


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("```\nHallo Welt.\n```", "Hallo Welt."),
        ("```text\nHallo Welt.\n```", "Hallo Welt."),
        ("  „Hallo Welt.“ ", "Hallo Welt."),
        ([{"type": "text", "text": "Hallo"}, {"type": "text", "text": "Welt."}], "Hallo Welt."),
        ("", ""),
        (None, ""),
    ],
)
def test_the_chat_reply_is_unwrapped(server, reply, expected):
    server(_chat_reply(reply))
    assert _transcriber(api_mode="chat").transcribe_batch(WAV) == expected


def test_a_chat_reply_without_content_is_an_error(server):
    server({"choices": []})
    with pytest.raises(TranscriptionError, match="no message content"):
        _transcriber(api_mode="chat").transcribe_batch(WAV)


def test_a_refused_reasoning_effort_is_dropped_once_and_remembered(server, caplog):
    fake = server(
        _http_error(400, '{"error": {"message": "reasoning_effort minimal is not supported"}}'),
        _chat_reply("eins"),
        _chat_reply("zwei"),
    )
    transcriber = _transcriber(api_mode="chat")

    with caplog.at_level(logging.INFO, logger=provider_module.__name__):
        assert transcriber.transcribe_batch(WAV) == "eins"
        assert transcriber.transcribe_batch(WAV) == "zwei"

    sent = [json.loads(request.data) for request in fake.requests]
    assert "reasoning_effort" in sent[0]
    assert "reasoning_effort" not in sent[1]
    assert "reasoning_effort" not in sent[2]
    assert caplog.text.count("custom_endpoint_reasoning_effort_disabled") == 1
    assert "not supported" not in caplog.text


def test_another_400_is_reported_with_its_detail_and_not_retried(server):
    fake = server(_http_error(400, '{"error": {"message": "Content blocks are expected"}}'))
    with pytest.raises(TranscriptionError, match="Content blocks are expected"):
        _transcriber(api_mode="chat").transcribe_batch(WAV)
    assert len(fake.requests) == 1


def test_the_part_limits_follow_the_api_style():
    transcriptions = remote_batch_part_limit("custom", "m")
    chat = remote_batch_part_limit("custom", "m", "chat")
    assert (transcriptions.seconds, transcriptions.max_bytes) == (
        OPENAI_MAX_PART_SECONDS,
        OPENAI_MAX_REQUEST_BYTES,
    )
    assert (chat.seconds, chat.max_bytes) == (
        CUSTOM_CHAT_MAX_PART_SECONDS,
        CUSTOM_CHAT_MAX_REQUEST_BYTES,
    )
    # Every other engine ignores the style argument.
    assert remote_batch_part_limit("openai", "gpt-transcribe", "chat") == (
        remote_batch_part_limit("openai", "gpt-transcribe")
    )


def test_the_transcriber_asks_for_the_limit_of_its_own_style(monkeypatch):
    seen: list[object] = []

    def fake_parts(audio, request, *, limit, progress_text, raise_if_canceled):
        seen.append(limit)
        return "ok"

    monkeypatch.setattr(provider_module, "transcribe_in_parts", fake_parts)
    _transcriber(api_mode="chat").transcribe_batch(WAV)
    assert seen == [remote_batch_part_limit("custom", "whisper-1", "chat")]


# -- key command ------------------------------------------------------------


class _Runs:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def runs(monkeypatch):
    def install(*results: object) -> _Runs:
        fake = _Runs(*results)
        monkeypatch.setattr(provider_module.subprocess, "run", fake)
        return fake

    return install


def test_the_command_token_wins_over_the_stored_key_and_is_cached(
    runs, server, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr(provider_module.time, "monotonic", lambda: clock[0])
    fake_runs = runs(_completed(f"notice line\n{SECRET_TOKEN}\n"), _completed("t2\n"))
    fake = server({"text": "a"}, {"text": "b"}, {"text": "c"})
    transcriber = _transcriber(key_command="token-helper --print")

    transcriber.transcribe_batch(WAV)
    clock[0] += CUSTOM_KEY_COMMAND_TTL_S - 1
    transcriber.transcribe_batch(WAV)
    clock[0] += 2
    transcriber.transcribe_batch(WAV)

    tokens = [request.get_header("Authorization") for request in fake.requests]
    assert tokens == [f"Bearer {SECRET_TOKEN}", f"Bearer {SECRET_TOKEN}", "Bearer t2"]
    args, kwargs = fake_runs.calls[0]
    assert args == ["token-helper", "--print"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 30
    assert "shell" not in kwargs


def test_a_long_token_is_sent_whole(runs, server):
    """A JWT runs to 800+ characters; the error-tail cap must not touch it.

    Measured against a real gateway: a helper printing an 861-character JWT
    was cut to 200 characters and every request answered HTTP 401.
    """
    long_token = "eyJ" + "a" * 900 + ".sig"
    runs(_completed(f"{long_token}\n"))
    fake = server({"text": "ok"})

    assert _transcriber(key_command="helper").transcribe_batch(WAV) == "ok"

    assert fake.requests[0].get_header("Authorization") == f"Bearer {long_token}"


def test_a_401_reruns_the_command_once_and_retries(runs, server):
    fake_runs = runs(_completed("old\n"), _completed("new\n"))
    fake = server(_http_error(401), {"text": "ok"})

    assert _transcriber(key_command="helper").transcribe_batch(WAV) == "ok"

    assert len(fake_runs.calls) == 2
    assert [r.get_header("Authorization") for r in fake.requests] == [
        "Bearer old",
        "Bearer new",
    ]


def test_a_second_401_fails_without_the_token_in_the_message(runs, server):
    runs(_completed(f"{SECRET_TOKEN}\n"), _completed(f"{SECRET_TOKEN}\n"))
    server(_http_error(401), _http_error(401))
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(key_command="helper").transcribe_batch(WAV)
    assert "HTTP 401" in str(raised.value)
    assert SECRET_TOKEN not in str(raised.value)


def test_a_stored_key_is_not_retried_after_a_401(server):
    fake = server(_http_error(401))
    with pytest.raises(TranscriptionError, match="HTTP 401"):
        _transcriber().transcribe_batch(WAV)
    assert len(fake.requests) == 1


def test_a_failing_command_names_its_exit_code_and_last_stderr_line(runs, server):
    runs(_completed(f"{SECRET_TOKEN}\n", returncode=3, stderr="warn\nlogin expired\n"))
    fake = server()
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(key_command="helper").transcribe_batch(WAV)
    message = str(raised.value)
    assert "exit code 3" in message
    assert "login expired" in message
    assert SECRET_TOKEN not in message
    assert fake.requests == []


def test_a_command_that_prints_nothing_is_an_error(runs, server):
    runs(_completed("\n  \n"))
    server()
    with pytest.raises(TranscriptionError, match="printed no token"):
        _transcriber(key_command="helper").transcribe_batch(WAV)


def test_a_command_that_hangs_is_bounded(runs, server):
    runs(subprocess.TimeoutExpired(["helper"], 30))
    server()
    with pytest.raises(TranscriptionError, match="did not finish within 30 s"):
        _transcriber(key_command="helper").transcribe_batch(WAV)


def test_a_missing_command_is_named(runs, server):
    runs(FileNotFoundError())
    server()
    with pytest.raises(TranscriptionError, match="not found: no-such-helper"):
        _transcriber(key_command="no-such-helper --x").transcribe_batch(WAV)


def test_the_token_is_never_logged(runs, server, caplog):
    runs(_completed(f"{SECRET_TOKEN}\n"))
    server({"text": "ok"})
    with caplog.at_level(logging.DEBUG):
        _transcriber(key_command="helper").transcribe_batch(WAV)
    assert SECRET_TOKEN not in caplog.text


def test_windows_quoting_keeps_backslashes(monkeypatch):
    monkeypatch.setattr(provider_module.os, "name", "nt")
    assert provider_module._command_arguments(
        r'wsl.exe -e "C:\tools\token helper.exe" --print'
    ) == ["wsl.exe", "-e", r"C:\tools\token helper.exe", "--print"]


# -- model list -------------------------------------------------------------


def test_the_model_list_is_filtered_and_sorted(server):
    fake = server(
        {
            "data": [
                {"id": "text-embedding-3", "mode": "embedding"},
                {"id": "gpt-4.1", "mode": "chat"},
                {"id": "dall-e", "mode": "image_generation"},
                {"id": "whisper-1", "mode": "audio_transcription"},
                {"id": "Anything-else"},
                {"id": "gemini-2.5-flash", "mode": "chat"},
                {"id": ""},
                "not-an-object",
                {"id": "whisper-1", "mode": "audio_transcription"},
            ]
        }
    )

    models = _transcriber().list_models()

    assert fake.requests[0].full_url == f"{BASE}/models"
    assert fake.requests[0].get_method() == "GET"
    assert models == [
        CustomEndpointModel("whisper-1", "audio_transcription"),
        CustomEndpointModel("gemini-2.5-flash", "chat"),
        CustomEndpointModel("gpt-4.1", "chat"),
        CustomEndpointModel("Anything-else", ""),
    ]


def test_an_oversized_model_list_is_refused(server, monkeypatch):
    monkeypatch.setattr(provider_module, "_MAX_MODELS_RESPONSE_BYTES", 100)
    server(json.dumps({"data": [{"id": "x" * 200}]}))
    with pytest.raises(TranscriptionError, match="larger than"):
        _transcriber().list_models()


def test_a_model_list_that_is_not_json_points_at_the_url(server):
    server("<html>login</html>")
    with pytest.raises(TranscriptionError, match="not JSON"):
        _transcriber().list_models()


def test_the_connection_test_checks_the_chosen_model(server):
    server({"data": [{"id": "whisper-1"}]}, {"data": [{"id": "other"}]})
    ok, message = _transcriber().test_connection()
    assert ok, message
    ok, message = _transcriber().test_connection()
    assert not ok
    assert "does not offer the model 'whisper-1'" in message


def test_streaming_is_not_offered():
    with pytest.raises(NotImplementedError):
        _transcriber().start_stream()
