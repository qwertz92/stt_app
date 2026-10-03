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
        monkeypatch.setattr(provider_module, "_open", fake)
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
        (
            "https://llm-gateway.example.com/openai",
            "https://llm-gateway.example.com/openai",
        ),
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


def test_auto_sends_no_language(server):
    fake = server({"text": "transcript"})
    assert _transcriber().transcribe_batch(WAV) == "transcript"
    assert "language" not in _multipart_fields(fake.requests[0])


# Bodies that are not the JSON the request asked for (`response_format=json`).
# Each was returned as the transcript and pasted (review of 2026-10-01).
_MARKUP_BODIES = [
    pytest.param("\ufeff<!DOCTYPE html><html><body>Sign in</body></html>", id="bom"),
    pytest.param("<!-- proxy --><html><body>Sign in</body></html>", id="comment"),
    pytest.param("<head><title>Sign in</title></head>", id="head"),
    pytest.param('<?xml version="1.0"?><html><body>Sign in</body></html>', id="xml"),
]


@pytest.mark.parametrize("page", _MARKUP_BODIES)
def test_any_markup_page_is_an_error_not_a_transcript(server, page):
    server(page)
    with pytest.raises(TranscriptionError) as raised:
        _transcriber().transcribe_batch(WAV)
    assert "HTML" in str(raised.value)
    assert "Sign in" not in str(raised.value)


def test_a_plain_text_answer_is_an_error_not_a_transcript(server):
    """The request asks for JSON; a text body is the server's error page."""
    server("Internal Server Error")
    with pytest.raises(TranscriptionError, match="not JSON") as raised:
        _transcriber().transcribe_batch(WAV)
    assert "Internal Server Error" in str(raised.value)


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


@pytest.mark.parametrize(
    ("mode", "size"), [("transcriptions", "25 MB"), ("chat", "15 MB")]
)
def test_a_413_names_the_fixed_part_size(server, mode, size):
    """A gateway or proxy with a lower body limit answers 413 ("Request Entity
    Too Large"); the part size is fixed, so the message says what is sent and
    what to change instead (review of 2026-10-03)."""
    server(
        _http_error(
            413, "<html><head><title>413 Request Entity Too Large</title></head></html>"
        )
    )
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(api_mode=mode).transcribe_batch(WAV)
    message = str(raised.value)
    assert "HTTP 413" in message
    assert size in message
    assert "body limit" in message


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


@pytest.mark.parametrize("suffix", [".m4a", ".flac", ".ogg", ".opus", ".webm", ".aac"])
def test_chat_audio_other_than_wav_or_mp3_is_refused_before_sending(
    server, tmp_path, suffix
):
    """`input_audio.format` is `wav` or `mp3` in the OpenAI shape; any other
    suffix was sent as its own name and answered with a 400 that does not
    say why (review of 2026-10-03)."""
    fake = server()
    clip = tmp_path / f"clip{suffix}"
    clip.write_bytes(b"not decoded")
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(api_mode="chat").transcribe_batch(str(clip))
    message = str(raised.value)
    assert suffix in message
    assert "WAV or MP3" in message
    assert "transcription API" in message
    assert fake.requests == []


def test_chat_audio_as_mp3_is_sent_as_mp3(server, tmp_path):
    fake = server(_chat_reply("ok"))
    clip = tmp_path / "clip.MP3"
    clip.write_bytes(b"ID3 not decoded")
    _transcriber(api_mode="chat").transcribe_batch(str(clip))
    part = json.loads(fake.requests[0].data)["messages"][1]["content"][0]
    assert part["input_audio"]["format"] == "mp3"


def test_transcription_style_sends_any_suffix_unchanged(server, tmp_path):
    fake = server({"text": "ok"})
    clip = tmp_path / "clip.m4a"
    clip.write_bytes(b"not decoded")
    assert _transcriber().transcribe_batch(str(clip)) == "ok"
    assert b'filename="clip.m4a"' in fake.requests[0].data


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("```\nHallo Welt.\n```", "Hallo Welt."),
        ("```text\nHallo Welt.\n```", "Hallo Welt."),
        ("  „Hallo Welt.“ ", "Hallo Welt."),
        (
            [{"type": "text", "text": "Hallo"}, {"type": "text", "text": "Welt."}],
            "Hallo Welt.",
        ),
        ("", ""),
    ],
)
def test_the_chat_reply_is_unwrapped(server, reply, expected):
    server(_chat_reply(reply))
    assert _transcriber(api_mode="chat").transcribe_batch(WAV) == expected


@pytest.mark.parametrize(
    ("message", "finish_reason", "expected"),
    [
        pytest.param(
            {
                "role": "assistant",
                "content": None,
                "refusal": "I can't help with that.",
            },
            "stop",
            "refused: I can't help with that.",
            id="refusal",
        ),
        pytest.param(
            {"role": "assistant", "content": None},
            "length",
            "finish reason 'length'",
            id="length",
        ),
        pytest.param(
            {"role": "assistant"}, "stop", "finish reason 'stop'", id="missing"
        ),
        pytest.param(
            {"role": "assistant", "content": [{"type": "refusal", "refusal": "No."}]},
            "stop",
            "refused: No.",
            id="refusal-part",
        ),
        pytest.param(
            {"role": "assistant", "content": 5},
            "stop",
            "content is not text (int)",
            id="number",
        ),
    ],
)
def test_a_chat_reply_with_null_content_is_an_error_not_silence(
    server, message, finish_reason, expected
):
    """`content: null` is a refusal or a cut-off answer, not "nothing said";
    read as silence, a part of a split recording vanished."""
    server({"choices": [{"message": message, "finish_reason": finish_reason}]})
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(api_mode="chat").transcribe_batch(WAV)
    assert expected in str(raised.value)


@pytest.mark.parametrize("content", ["The quick brown fox jumps over the", None])
def test_a_chat_reply_cut_off_at_the_output_limit_is_an_error(server, content):
    """`finish_reason: "length"` means the model ran out of output tokens: the
    text it carries is the start of the transcript, and pasted as a complete
    one it silently loses the end (review of 2026-10-03)."""
    server(
        {
            "choices": [
                {"message": {"content": content}, "finish_reason": "length"},
            ]
        }
    )
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(api_mode="chat").transcribe_batch(WAV)
    message = str(raised.value)
    assert "output limit" in message
    assert "transcription API" in message
    assert "quick brown fox" not in message


def test_a_long_refusal_is_shortened_in_the_message(server):
    server({"choices": [{"message": {"content": None, "refusal": "x" * 500}}]})
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(api_mode="chat").transcribe_batch(WAV)
    assert len(str(raised.value)) < 260


def test_a_chat_reply_without_content_is_an_error(server):
    server({"choices": []})
    with pytest.raises(TranscriptionError, match="no message content"):
        _transcriber(api_mode="chat").transcribe_batch(WAV)


def test_a_refused_reasoning_effort_is_dropped_once_and_remembered(server, caplog):
    fake = server(
        _http_error(
            400, '{"error": {"message": "reasoning_effort minimal is not supported"}}'
        ),
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
    fake = server(
        _http_error(400, '{"error": {"message": "Content blocks are expected"}}')
    )
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

    def fake_parts(audio, request, *, limit, **_kwargs):
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
        monkeypatch.setattr(provider_module, "run_bounded", fake)
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


def test_a_401_shows_the_reason_the_gateway_gave(server):
    server(_http_error(401, '{"error": {"message": "Key expired on 2026-09-01"}}'))
    with pytest.raises(TranscriptionError) as raised:
        _transcriber().transcribe_batch(WAV)
    message = str(raised.value)
    assert "HTTP 401" in message
    assert "Key expired on 2026-09-01" in message


def test_a_401_that_echoes_the_key_shows_no_reason(server):
    """LiteLLM answers "Invalid proxy server token passed. Received API Key =
    <the key>"; the key must not reach the overlay or the log."""
    key = "sk-secret-TOKEN-123"
    server(
        _http_error(
            401, json.dumps({"error": {"message": f"Invalid token. Received {key}"}})
        )
    )
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(api_key=key).transcribe_batch(WAV)
    message = str(raised.value)
    assert "HTTP 401" in message
    assert key not in message
    assert "Invalid token" not in message


@pytest.mark.parametrize("use_command", [False, True])
def test_a_secret_is_scrubbed_from_any_other_error_detail(runs, server, use_command):
    secret = "sk-secret-TOKEN-123"
    if use_command:
        runs(_completed(f"{secret}\n"))
        transcriber = _transcriber(api_key="", key_command="helper")
    else:
        transcriber = _transcriber(api_key=secret)
    server(
        _http_error(
            400, f'{{"error": {{"message": "Bad header Bearer {secret} sent"}}}}'
        )
    )
    with pytest.raises(TranscriptionError) as raised:
        transcriber.transcribe_batch(WAV)
    message = str(raised.value)
    assert secret not in message
    assert "Bad header Bearer [hidden] sent" in message


_CUT_KEY = "sk-secret-TOKEN-123abc"


# Visible ASCII a header accepts, but JSON writes escaped.
_ESCAPED_KEY = 'sk"-s\\ecret-123abc'


@pytest.mark.parametrize(
    ("build", "key"),
    [
        pytest.param(
            lambda key: _http_error(
                400, json.dumps({"error": {"message": "x" * 290 + key}})
            ),
            _CUT_KEY,
            id="json-error-capped-at-300",
        ),
        pytest.param(
            lambda key: _http_error(502, "y" * 295 + key + " more"),
            _CUT_KEY,
            id="text-error-capped-at-300",
        ),
        pytest.param(
            lambda key: _http_error(401, json.dumps({"error": "z" * 295 + key})),
            _CUT_KEY,
            id="401-reason-capped-at-300",
        ),
        pytest.param(
            lambda key: "w" * 70 + key,
            _CUT_KEY,
            id="200-not-json-excerpt-capped-at-80",
        ),
        pytest.param(
            lambda key: _http_error(
                400, json.dumps({"error": {"message": "q" * 290 + key}})
            ),
            _ESCAPED_KEY,
            id="key-that-json-escapes",
        ),
    ],
)
def test_a_credential_at_a_truncation_point_leaves_no_fragment(server, build, key):
    """The scrub ran after the 300-character cap in the HTTP reader and the
    80-character cap of an excerpt, so a key echoed near the cut left its
    first characters visible (review of 2026-10-03): redaction now comes
    before any cut."""
    server(build(key))
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(api_key=key).transcribe_batch(WAV)
    message = str(raised.value)
    assert key[:5] not in message, message
    assert json.dumps(key)[1:7] not in message, message


def test_a_refusal_with_a_credential_at_the_cut_leaves_no_fragment(server):
    server(
        {"choices": [{"message": {"content": None, "refusal": "r" * 70 + _CUT_KEY}}]}
    )
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(api_key=_CUT_KEY, api_mode="chat").transcribe_batch(WAV)
    assert "sk-" not in str(raised.value), str(raised.value)


def test_a_secret_is_scrubbed_from_the_model_list_error_too(server):
    secret = "sk-secret-TOKEN-123"
    server(_http_error(500, f'{{"error": "key {secret} broke"}}'))
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(api_key=secret).list_models()
    assert secret not in str(raised.value)
    assert "key [hidden] broke" in str(raised.value)


def test_a_short_placeholder_key_is_not_scrubbed(server):
    """`none` is a placeholder for a server without authentication, and as a
    word it would be cut out of every message."""
    server(_http_error(500, '{"error": "none of the backends answered"}'))
    with pytest.raises(TranscriptionError, match="none of the backends answered"):
        _transcriber(api_key="none").transcribe_batch(WAV)


def test_a_gateway_error_object_under_detail_is_rendered_as_its_message(server):
    server(
        _http_error(
            403, '{"detail": {"error": "user not allowed to access model whisper-1"}}'
        )
    )
    with pytest.raises(TranscriptionError) as raised:
        _transcriber().transcribe_batch(WAV)
    assert "user not allowed to access model whisper-1" in str(raised.value)
    assert "{" not in str(raised.value)


def test_a_chat_reply_that_is_an_error_object_shows_its_text(server):
    server({"error": {"message": "model gpt-x not found", "type": "invalid"}})
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(api_mode="chat").transcribe_batch(WAV)
    assert "model gpt-x not found" in str(raised.value)
    assert "no message content" not in str(raised.value)


def test_a_transcription_reply_that_is_an_error_object_shows_its_text(server):
    server({"error": {"message": "model whisper-1 not found"}})
    with pytest.raises(TranscriptionError, match="model whisper-1 not found"):
        _transcriber().transcribe_batch(WAV)


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


def test_a_failing_command_that_printed_a_message_reports_its_exit_code(runs, server):
    """The exit code is judged before the output is: a login helper that
    prints "Please run 'login' first" to stdout and exits 1 used to be
    reported as "printed a token with a space" (review of 2026-10-03)."""
    runs(
        _completed(
            "ERROR: Please run 'login' first\n", returncode=1, stderr="no session\n"
        )
    )
    server()
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(key_command="helper").transcribe_batch(WAV)
    message = str(raised.value)
    assert "exit code 1" in message
    assert "no session" in message
    assert "token with a space" not in message


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


def test_the_program_is_resolved_through_path_and_pathext(runs, server, monkeypatch):
    """`az`, `gcloud` and `npm` are `.cmd` shims on Windows, and CreateProcess
    appends only `.exe`: the command was "not found" although it ran in a
    console (review of 2026-10-03)."""
    monkeypatch.setattr(
        provider_module.shutil,
        "which",
        lambda name: r"C:\Tools\az.CMD" if name == "az" else None,
    )
    fake_runs = runs(_completed("tok\n"))
    server({"text": "ok"})

    _transcriber(key_command="az account get-access-token").transcribe_batch(WAV)

    assert fake_runs.calls[0][0] == [r"C:\Tools\az.CMD", "account", "get-access-token"]


def test_an_unresolvable_program_is_left_as_typed(runs, server, monkeypatch):
    monkeypatch.setattr(provider_module.shutil, "which", lambda name: None)
    fake_runs = runs(FileNotFoundError())
    server()
    with pytest.raises(TranscriptionError, match="not found: no-such-helper"):
        _transcriber(key_command="no-such-helper --x").transcribe_batch(WAV)
    assert fake_runs.calls[0][0] == ["no-such-helper", "--x"]


@pytest.mark.parametrize("argument", ["a&b", "a|b", "a<b", "a>b", "a^b", "100%"])
def test_a_batch_file_argument_cmd_would_interpret_is_refused(
    runs, server, monkeypatch, argument
):
    """cmd.exe runs a `.cmd`/`.bat` file and reads `&` as a command separator
    (reproduced: `tokcmd.cmd "a&b"` ran `b`) and `%VAR%` as an expansion, so
    such an argument is refused instead of run as something else. The
    argument itself is not echoed: it may be a secret."""
    monkeypatch.setattr(
        provider_module.shutil, "which", lambda name: r"C:\Tools\az.cmd"
    )
    fake_runs = runs()
    server()
    with pytest.raises(TranscriptionError) as raised:
        _transcriber(key_command=f'az --name "{argument}"').transcribe_batch(WAV)
    message = str(raised.value)
    assert "cmd.exe" in message
    assert argument not in message
    assert fake_runs.calls == []


@pytest.mark.skipif(
    provider_module.os.name != "nt", reason="`.cmd` shims exist only on Windows"
)
def test_a_cmd_shim_on_path_prints_its_token(tmp_path, monkeypatch):
    """The process runner and `PATHEXT` themselves: `shim` resolves to
    `shim.cmd`, which cmd.exe runs."""
    (tmp_path / "token-shim.cmd").write_text(
        "@echo off\r\necho shim-token-%1\r\n", encoding="ascii"
    )
    monkeypatch.setenv("PATH", f"{tmp_path};{provider_module.os.environ['PATH']}")
    transcriber = _transcriber(api_key="", key_command="token-shim A")

    assert transcriber._run_key_command() == "shim-token-A"


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


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(
            "wsl.exe -e bash -lc 'echo x'",
            ["wsl.exe", "-e", "bash", "-lc", "echo x"],
            id="single-quoted-wsl-script",
        ),
        pytest.param(
            "wsl.exe -e bash -lc 'my-helper --name \"a b\"'",
            ["wsl.exe", "-e", "bash", "-lc", 'my-helper --name "a b"'],
            id="double-quotes-inside-single-quotes-are-literal",
        ),
        pytest.param(
            r"'C:\tools\token helper.exe' --print",
            [r"C:\tools\token helper.exe", "--print"],
            id="single-quoted-path-keeps-backslashes",
        ),
        pytest.param(
            'helper --opt="a b" "C:\\p q\\t.exe"',
            ["helper", "--opt=a b", "C:\\p q\\t.exe"],
            id="a-quote-inside-a-word-groups",
        ),
        pytest.param(
            r"C:\Users\O'Brien\tok.exe --print",
            [r"C:\Users\O'Brien\tok.exe", "--print"],
            id="a-mid-word-apostrophe-is-literal",
        ),
        pytest.param("helper '' \"\"", ["helper", "", ""], id="empty-arguments"),
        # PowerShell's own string syntax: after -Command the quotes are the
        # script's, not the splitter's (regression of ca802d9, reported
        # 2026-10-03).
        pytest.param(
            r"powershell -NoProfile -Command Get-Content 'C:\a b\t.txt'",
            ["powershell", "-NoProfile", "-Command", "Get-Content", r"'C:\a b\t.txt'"],
            id="powershell-command-keeps-single-quotes",
        ),
        pytest.param(
            r"pwsh -c & 'C:\a b\get token.ps1'",
            ["pwsh", "-c", "&", r"'C:\a b\get token.ps1'"],
            id="pwsh-call-operator",
        ),
        pytest.param(
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -Command '$env:USERNAME'",
            [
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                "-Command",
                "'$env:USERNAME'",
            ],
            id="powershell-full-path-literal-string",
        ),
        pytest.param(
            "powershell -Command \"Write-Output 'a b'\"",
            ["powershell", "-Command", "Write-Output 'a b'"],
            id="powershell-double-quoted-script-keeps-inner-quotes",
        ),
        pytest.param(
            r"powershell -NoProfile -File 'C:\a b\get token.ps1'",
            ["powershell", "-NoProfile", "-File", r"C:\a b\get token.ps1"],
            id="powershell-file-is-a-path-not-a-script",
        ),
        pytest.param(
            "other.exe -Command 'a b'",
            ["other.exe", "-Command", "a b"],
            id="only-powershell-keeps-quotes",
        ),
    ],
)
def test_windows_quoting_groups_with_single_and_double_quotes(
    monkeypatch, command, expected
):
    """`wsl.exe -e bash -lc 'echo x'` passed `'echo x'` -- quotes included --
    to bash, and `--opt="a b"` was split in two (review of 2026-10-03).
    Backslashes stay literal, which keeps Windows paths intact."""
    monkeypatch.setattr(provider_module.os, "name", "nt")
    assert provider_module._command_arguments(command) == expected


@pytest.mark.parametrize("command", ["helper 'unfinished", 'helper "unfinished'])
def test_an_unclosed_quote_in_the_key_command_is_refused(monkeypatch, command):
    monkeypatch.setattr(provider_module.os, "name", "nt")
    with pytest.raises(TranscriptionError, match="cannot be parsed"):
        provider_module._command_arguments(command)


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
    # An HTML page has a message of its own (the HTML tests below).
    server("Service Unavailable")
    with pytest.raises(TranscriptionError, match="not JSON"):
        _transcriber().list_models()


def test_the_connection_test_checks_the_chosen_model(server):
    server({"data": [{"id": "whisper-1"}]}, {"data": [{"id": "other"}]})
    ok, message = _transcriber().test_connection()
    assert ok, message
    ok, message = _transcriber().test_connection()
    assert not ok
    assert "does not offer the model 'whisper-1'" in message


@pytest.mark.parametrize(
    "answer",
    [{"data": []}, {"data": ["a", "b"]}, {"data": [{"name": "llama3"}]}, []],
    ids=["empty", "bare-strings", "no-ids", "empty-top-level-list"],
)
def test_the_connection_test_does_not_claim_success_for_a_list_without_ids(
    server, answer
):
    """A reply with nothing parsable was reported as "Connection OK ... 0
    models" (review of 2026-10-03)."""
    server(answer)
    ok, message = _transcriber(model="").test_connection()
    assert not ok
    assert "no usable model ids" in message
    assert "0 models" not in message


def test_streaming_is_not_offered():
    with pytest.raises(NotImplementedError):
        _transcriber().start_stream()


# -- key command: a real process -------------------------------------------

_GRANDCHILD_SCRIPT = """
import os, sys, time
heartbeat = sys.argv[1]
deadline = time.monotonic() + 25
while time.monotonic() < deadline:
    with open(heartbeat, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} {time.monotonic()}")
    time.sleep(0.1)
"""

_CHILD_SCRIPT = """
import subprocess, sys, time
# Handed the child's own pipes, as `wsl.exe -e ...` or a `.cmd` wrapper hands
# them to the program it starts.
subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2]],
    stdout=sys.stdout,
    stderr=sys.stderr,
)
time.sleep(60)
"""


def _heartbeat(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _kill_leftover(heartbeat) -> None:
    text = _heartbeat(heartbeat)
    if not text:
        return
    pid = text.split()[0]
    if provider_module.os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", pid],
            capture_output=True,
            check=False,
        )
    else:
        import signal

        try:
            provider_module.os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


def test_a_key_command_whose_grandchild_holds_the_pipe_is_still_bounded(
    tmp_path, monkeypatch
):
    """The timeout bounds the whole call and takes the process tree with it.

    `subprocess.run(timeout=...)` kills the direct child only and then reads
    its pipes to the end, which a grandchild holding them keeps open:
    reproduced at 15.1 s on a 2 s timeout before this was fixed.
    """
    import sys
    import time

    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(_GRANDCHILD_SCRIPT, encoding="utf-8")
    child = tmp_path / "child.py"
    child.write_text(_CHILD_SCRIPT, encoding="utf-8")
    heartbeat = tmp_path / "heartbeat.txt"
    monkeypatch.setattr(provider_module, "CUSTOM_KEY_COMMAND_TIMEOUT_S", 1.0)
    command = (
        subprocess.list2cmdline(
            [sys.executable, str(child), str(grandchild), str(heartbeat)]
        )
        if provider_module.os.name == "nt"
        else " ".join([sys.executable, str(child), str(grandchild), str(heartbeat)])
    )
    transcriber = _transcriber(api_key="", key_command=command)

    started = time.monotonic()
    try:
        with pytest.raises(TranscriptionError, match="did not finish within 1 s"):
            transcriber._run_key_command()
        elapsed = time.monotonic() - started
        assert elapsed < 10.0, f"the timeout of 1 s took {elapsed:.1f} s"
        # The grandchild was ended with its parent, not left running.
        time.sleep(0.5)
        before = _heartbeat(heartbeat)
        time.sleep(0.6)
        assert _heartbeat(heartbeat) == before, "the grandchild is still running"
    finally:
        _kill_leftover(heartbeat)


@pytest.mark.skipif(
    provider_module.os.name != "nt", reason="`.cmd` shims exist only on Windows"
)
def test_a_cmd_shim_whose_grandchild_holds_the_pipe_is_bounded_and_ended(
    tmp_path, monkeypatch
):
    """cmd.exe sits between the key command and what it starts, so the job
    object must still reach the grandchild through it."""
    import sys
    import time

    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(_GRANDCHILD_SCRIPT, encoding="utf-8")
    child = tmp_path / "child.py"
    child.write_text(_CHILD_SCRIPT, encoding="utf-8")
    heartbeat = tmp_path / "heartbeat.txt"
    (tmp_path / "hang-shim.cmd").write_text(
        f'@"{sys.executable}" "{child}" "{grandchild}" "{heartbeat}"\r\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", f"{tmp_path};{provider_module.os.environ['PATH']}")
    # Long enough for cmd.exe, python and the grandchild to start on a busy
    # machine (2 s was not: the grandchild had not started when the tree was
    # ended, and the heartbeat check below saw nothing to compare).
    monkeypatch.setattr(provider_module, "CUSTOM_KEY_COMMAND_TIMEOUT_S", 6.0)
    transcriber = _transcriber(api_key="", key_command="hang-shim")

    started = time.monotonic()
    try:
        with pytest.raises(TranscriptionError, match="did not finish within 6 s"):
            transcriber._run_key_command()
        elapsed = time.monotonic() - started
        assert elapsed < 15.0, f"the timeout of 6 s took {elapsed:.1f} s"
        time.sleep(0.5)
        before = _heartbeat(heartbeat)
        assert before, "the grandchild never started"
        time.sleep(0.6)
        assert _heartbeat(heartbeat) == before, "the grandchild is still running"
    finally:
        _kill_leftover(heartbeat)


_EXITING_CHILD_SCRIPT = """
import subprocess, sys
# Starts a program that inherits the pipes, prints the token and exits 0: the
# token has arrived, but the pipes stay open as long as the grandchild runs.
subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2]],
    stdout=sys.stdout,
    stderr=sys.stderr,
)
print("child-token", flush=True)
"""


def test_a_key_command_that_exits_while_its_grandchild_holds_the_pipe(
    tmp_path, monkeypatch
):
    """The token arrived with exit code 0; the call must not wait for EOF.

    Before: `communicate` waited for the grandchild to close the inherited
    pipes, the call failed with the timeout although the token had arrived,
    and the grandchild -- an orphan once its parent had exited, which
    `taskkill /T` cannot reach -- kept running (review of 2026-10-01).
    """
    import sys
    import time

    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(_GRANDCHILD_SCRIPT, encoding="utf-8")
    child = tmp_path / "child.py"
    child.write_text(_EXITING_CHILD_SCRIPT, encoding="utf-8")
    heartbeat = tmp_path / "heartbeat.txt"
    monkeypatch.setattr(provider_module, "CUSTOM_KEY_COMMAND_TIMEOUT_S", 8.0)
    parts = [sys.executable, str(child), str(grandchild), str(heartbeat)]
    command = (
        subprocess.list2cmdline(parts)
        if provider_module.os.name == "nt"
        else " ".join(parts)
    )
    transcriber = _transcriber(api_key="", key_command=command)

    started = time.monotonic()
    try:
        assert transcriber._run_key_command() == "child-token"
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"the call waited {elapsed:.1f} s for the pipes"
        time.sleep(0.5)
        before = _heartbeat(heartbeat)
        time.sleep(0.6)
        assert _heartbeat(heartbeat) == before, "the grandchild is still running"
    finally:
        _kill_leftover(heartbeat)


def test_a_real_key_command_prints_its_token(monkeypatch):
    """The process runner itself, not a stand-in for it."""
    import sys

    script = "print('notice'); print('real-token')"
    if provider_module.os.name == "nt":
        command = subprocess.list2cmdline([sys.executable, "-c", script])
    else:
        import shlex

        command = shlex.join([sys.executable, "-c", script])
    transcriber = _transcriber(api_key="", key_command=command)

    assert transcriber._run_key_command() == "real-token"


# -- redirects (real HTTP servers on the loopback interface) ----------------


class _RecordingHandler:
    """Builds `BaseHTTPRequestHandler` classes for the two loopback servers."""

    @staticmethod
    def target(seen: list):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                seen.append(
                    (self.command, self.path, self.headers.get("Authorization"))
                )
                if self.path.endswith("/models") or self.path.endswith("/models/"):
                    body = json.dumps({"data": [{"id": "whisper-1"}]}).encode()
                else:
                    body = json.dumps({"text": "leaked"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _answer
            do_POST = _answer

            def log_message(self, *_args):
                pass

        return Handler

    @staticmethod
    def redirector(location_for):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                location = location_for(self.path)
                if location is None:
                    body = json.dumps({"data": [{"id": "whisper-1"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = _answer
            do_POST = _answer

            def log_message(self, *_args):
                pass

        return Handler


@pytest.fixture
def loopback(monkeypatch):
    """Start loopback HTTP servers; no proxy may stand in between."""
    import http.server
    import threading

    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    started: list = []

    def start(handler) -> int:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        started.append(httpd)
        return httpd.server_address[1]

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def test_a_redirect_to_another_origin_never_carries_the_key(loopback):
    """127.0.0.1:A -> localhost:B is another origin; the Bearer key stays home.

    urllib's default redirect handler copies every header, Authorization
    included, to whatever host the Location names -- and from https to http
    it sends the key in clear.
    """
    seen: list = []
    target_port = loopback(_RecordingHandler.target(seen))
    redirect_port = loopback(
        _RecordingHandler.redirector(
            lambda path: f"http://localhost:{target_port}{path}"
        )
    )
    transcriber = _transcriber(
        api_key=SECRET_TOKEN, endpoint=f"http://127.0.0.1:{redirect_port}/v1"
    )

    with pytest.raises(TranscriptionError) as raised:
        transcriber.list_models()
    with pytest.raises(TranscriptionError) as raised_upload:
        transcriber.transcribe_batch(WAV)

    assert seen == [], f"the other origin was contacted: {seen}"
    for message in (str(raised.value), str(raised_upload.value)):
        assert "redirect" in message.lower()
        assert f"localhost:{target_port}" in message
        assert SECRET_TOKEN not in message


def test_a_same_origin_redirect_of_the_model_list_is_followed(loopback):
    """A gateway that adds a trailing slash keeps working, key and all."""
    seen: list = []

    def location(path):
        return None if path.endswith("/") else f"{path}/"

    class Recording(_RecordingHandler.redirector(location)):
        def _answer(self):
            seen.append((self.command, self.path, self.headers.get("Authorization")))
            super()._answer()

        do_GET = _answer

    port = loopback(Recording)
    transcriber = _transcriber(api_key="k", endpoint=f"http://127.0.0.1:{port}/v1")

    assert transcriber.list_models() == [CustomEndpointModel("whisper-1", "")]
    assert seen == [
        ("GET", "/v1/models", "Bearer k"),
        ("GET", "/v1/models/", "Bearer k"),
    ]


def test_a_redirected_upload_is_refused_rather_than_resent_as_a_get(loopback):
    """urllib turns a redirected POST into a GET without its body.

    The endpoint then answered a request that carried no audio -- typically
    HTTP 405, which names nothing the user can act on.
    """
    seen: list = []

    def location(path):
        return None if path.endswith("/") else f"{path}/"

    class Recording(_RecordingHandler.redirector(location)):
        def _answer(self):
            seen.append((self.command, self.path))
            super()._answer()

        do_GET = _answer
        do_POST = _answer

    port = loopback(Recording)
    transcriber = _transcriber(api_key="k", endpoint=f"http://127.0.0.1:{port}/v1")

    with pytest.raises(TranscriptionError, match="redirected") as raised:
        transcriber.transcribe_batch(WAV)

    assert seen == [("POST", "/v1/audio/transcriptions")]
    assert f"127.0.0.1:{port}/v1/audio/transcriptions/" in str(raised.value)


@pytest.mark.parametrize(
    ("old", "new", "allowed"),
    [
        ("https://gw.example/v1/models", "https://gw.example/v1/models/", True),
        ("https://gw.example/v1/models", "https://GW.example:443/v1/x", True),
        ("http://gw.example/v1/models", "https://gw.example/v1/models", True),
        ("https://gw.example/v1/models", "http://gw.example/v1/models", False),
        ("https://gw.example/v1/models", "https://other.example/v1/models", False),
        ("https://gw.example/v1/models", "https://gw.example:8443/v1/models", False),
        ("http://127.0.0.1:8000/v1", "http://localhost:8000/v1", False),
        ("http://gw.example:8080/v1", "https://gw.example/v1", False),
    ],
)
def test_which_redirects_may_carry_the_key(old, new, allowed):
    assert provider_module._redirect_keeps_origin(old, new) is allowed


# -- a key a header cannot carry --------------------------------------------


def test_a_stored_key_with_a_line_break_is_refused_without_echoing_it(loopback, caplog):
    """`http.client` refuses the header with the whole value in its message.

    That message reached the overlay and the log: "Invalid header value
    b'Bearer <key>...'". The key is refused before any request instead, and
    nothing that names it is shown or logged.
    """
    seen: list = []
    port = loopback(_RecordingHandler.target(seen))
    key = "sk-first-half\nsecond-half-SECRET"

    with caplog.at_level(logging.DEBUG), pytest.raises(TranscriptionError) as raised:
        _transcriber(
            api_key=key, endpoint=f"http://127.0.0.1:{port}/v1"
        ).transcribe_batch(WAV)

    message = str(raised.value)
    assert "line break" in message
    for fragment in ("sk-first-half", "second-half-SECRET"):
        assert fragment not in message
        assert fragment not in caplog.text
    assert seen == []


def test_a_command_token_with_a_control_character_is_refused(runs, server):
    runs(_completed("tok\x00en-SECRET\n"))
    fake = server({"text": "ok"})

    with pytest.raises(TranscriptionError) as raised:
        _transcriber(key_command="helper").transcribe_batch(WAV)

    assert "en-SECRET" not in str(raised.value)
    assert fake.requests == []


def test_an_invalid_header_value_never_reaches_the_message(server):
    """Defence in depth: whatever builds the header, its value stays out."""
    server(ValueError("Invalid header value b'Bearer leaked-SECRET'"))

    with pytest.raises(TranscriptionError) as raised:
        _transcriber().transcribe_batch(WAV)

    assert "leaked-SECRET" not in str(raised.value)


# -- answers that are not a transcript --------------------------------------


_LOGIN_PAGE = (
    "<!DOCTYPE html>\n<html><head><title>Sign in</title></head>"
    "<body>Your session expired. Please sign in.</body></html>"
)


@pytest.mark.parametrize("page", [_LOGIN_PAGE, "  <html><body>Blocked</body></html>"])
def test_an_html_page_is_never_returned_as_the_transcript(server, page):
    """A proxy's login or block page answers 200 with HTML.

    Read as a plain-text transcript, the page was pasted into the user's
    document.
    """
    server(page)
    with pytest.raises(TranscriptionError) as raised:
        _transcriber().transcribe_batch(WAV)
    message = str(raised.value)
    assert "HTML" in message
    assert "Sign in" not in message and "Blocked" not in message


def test_an_html_model_list_names_the_page_rather_than_json(server):
    server(_LOGIN_PAGE)
    with pytest.raises(TranscriptionError, match="HTML"):
        _transcriber().list_models()


def test_a_json_answer_without_text_is_an_error_naming_its_keys(server):
    """A JSON shape without `text` is not "no speech". (An `error` member is
    shown by its own text instead: see the 200-with-error test.)"""
    server({"result": "x", "id": "x"})
    with pytest.raises(TranscriptionError) as raised:
        _transcriber().transcribe_batch(WAV)
    message = str(raised.value)
    assert "'text'" in message
    assert "result" in message and "id" in message


def test_an_empty_text_field_is_still_an_empty_transcript(server):
    """Silence is answered with `"text": ""`, which must stay a valid answer."""
    server({"text": ""})
    assert _transcriber().transcribe_batch(WAV) == ""


# -- a base URL pasted with its route ---------------------------------------


@pytest.mark.parametrize(
    "pasted",
    [
        f"{BASE}/audio/transcriptions",
        f"{BASE}/chat/completions/",
        f"{BASE}/models",
        f"{BASE}/Audio/Transcriptions",
    ],
)
def test_a_pasted_route_is_taken_off_the_base_url(pasted):
    """Users paste the request URL their gateway documents; the app then
    posted to `.../audio/transcriptions/audio/transcriptions` and got a 404."""
    assert normalize_custom_endpoint(pasted) == BASE


def test_a_prefix_that_only_resembles_a_route_is_kept():
    assert (
        normalize_custom_endpoint("https://gw.example/my-models")
        == "https://gw.example/my-models"
    )


# -- model list ordering ----------------------------------------------------


def test_speech_models_without_a_mode_are_listed_first(server):
    """OpenAI's own list carries no `mode`; whisper-1 sat behind dozens of
    chat and image models in alphabetical order."""
    server(
        {
            "data": [
                {"id": "babbage-002"},
                {"id": "dall-e-3"},
                {"id": "gpt-4o"},
                {"id": "gpt-4o-mini-tts"},
                {"id": "gpt-4o-transcribe"},
                {"id": "tts-1"},
                {"id": "whisper-1"},
                {"id": "Systran/faster-whisper-small"},
            ]
        }
    )
    ids = [model.id for model in _transcriber().list_models()]
    assert ids[:3] == ["gpt-4o-transcribe", "Systran/faster-whisper-small", "whisper-1"]
    assert set(ids) == {
        "babbage-002",
        "dall-e-3",
        "gpt-4o",
        "gpt-4o-mini-tts",
        "gpt-4o-transcribe",
        "tts-1",
        "whisper-1",
        "Systran/faster-whisper-small",
    }
