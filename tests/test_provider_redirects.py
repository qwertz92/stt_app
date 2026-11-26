"""No REST engine follows a redirect with its API key, and none prints a key
that cannot be an HTTP header value.

Real HTTP on the loopback interface: the engine's request is pointed at a
first server that answers every request with a redirect to a second server;
the second server must never receive a request. The engines' own URLs are
fixed to their vendors, so each module's `open_request` is wrapped to move
the request to the first server before the real helper runs.
"""

from __future__ import annotations

import importlib
import io
import re
import threading
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from stt_app.config import ENGINE_LABELS
from stt_app.transcriber import _http_utils
from stt_app.transcriber.base import TranscriptionError

KEY = "secret-key-0123456789"
UNSAFE_KEYS = (
    "secret\nkey-0123456789",
    "secret" + chr(0xA0) + "key-0123456789",  # a non-breaking space
    "s" + chr(0xE9) + "cret-key-01234",
)
REDIRECT_CODES = (301, 302, 303, 307, 308)


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x10\x20" * 16000)
    return buffer.getvalue()


WAV = _wav()


class _Servers:
    """A server that redirects everything and one that records what arrives."""

    def __init__(self) -> None:
        self.code = 302
        self.second_hits: list[tuple[str, str, dict[str, str]]] = []
        self.first_hits = 0
        outer = self

        class Second(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.second_hits.append((self.command, self.path, dict(self.headers)))
                body = b'{"text": "stolen", "transcript": "stolen", "data": {}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

        self.second = ThreadingHTTPServer(("127.0.0.1", 0), Second)
        self.second_port = self.second.server_address[1]

        class First(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.first_hits += 1
                self.send_response(outer.code)
                self.send_header(
                    "Location", f"http://127.0.0.1:{outer.second_port}/steal"
                )
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = _answer

        self.first = ThreadingHTTPServer(("127.0.0.1", 0), First)
        self.first_port = self.first.server_address[1]
        for server in (self.first, self.second):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def close(self) -> None:
        for server in (self.first, self.second):
            server.shutdown()
            server.server_close()


@pytest.fixture
def servers():
    started = _Servers()
    yield started
    started.close()


def _build(engine: str, key: str):
    """(module that holds `open_request`, the engine's transcriber)."""
    if engine == "openai":
        from stt_app.transcriber.openai_provider import OpenAITranscriber

        return "openai_provider", OpenAITranscriber(api_key=key)
    if engine == "deepgram":
        from stt_app.transcriber.deepgram_provider import DeepgramTranscriber

        return "deepgram_provider", DeepgramTranscriber(api_key=key)
    if engine == "elevenlabs":
        from stt_app.transcriber.elevenlabs_provider import ElevenLabsTranscriber

        return "elevenlabs_provider", ElevenLabsTranscriber(api_key=key)
    if engine == "azure":
        from stt_app.transcriber.azure_provider import AzureLlmSpeechTranscriber

        return "azure_provider", AzureLlmSpeechTranscriber(
            api_key=key, endpoint="https://res.cognitiveservices.azure.com"
        )
    if engine == "mistral":
        from stt_app.transcriber.mistral_provider import MistralTranscriber

        return "mistral_provider", MistralTranscriber(api_key=key)
    if engine == "meta":
        from stt_app.transcriber.meta_provider import MetaTranscriber

        return "meta_provider", MetaTranscriber(api_key=key)
    if engine == "speechmatics":
        from stt_app.transcriber.speechmatics_provider import SpeechmaticsTranscriber

        return "speechmatics_provider", SpeechmaticsTranscriber(api_key=key)
    if engine == "assemblyai":
        from stt_app.transcriber.assemblyai_provider import AssemblyAITranscriber

        return "assemblyai_provider", AssemblyAITranscriber(api_key=key)
    if engine == "openrouter":
        from stt_app.transcriber.openrouter_provider import OpenRouterTranscriber

        return "custom_endpoint_provider", OpenRouterTranscriber(key)
    from stt_app.transcriber.custom_endpoint_provider import CustomEndpointTranscriber

    return "custom_endpoint_provider", CustomEndpointTranscriber(
        api_key=key, endpoint="https://gw.example.com/v1", model="m"
    )


ENGINES = (
    "openai",
    "deepgram",
    "elevenlabs",
    "azure",
    "mistral",
    "meta",
    "speechmatics",
    "assemblyai",
    "openrouter",
    "custom",
)
# AssemblyAI's batch transcription goes through its SDK, which is not covered.
BATCH_ENGINES = tuple(engine for engine in ENGINES if engine != "assemblyai")
# What the engine calls itself in a message: its row title in the app.
NAMES = {
    "openai": "OpenAI",
    "deepgram": "Deepgram",
    "elevenlabs": "ElevenLabs",
    "azure": "Azure LLM Speech",
    "mistral": "Mistral",
    "meta": ENGINE_LABELS["meta"],
    "speechmatics": "Speechmatics",
    "assemblyai": "AssemblyAI",
    "openrouter": ENGINE_LABELS["openrouter"],
    "custom": ENGINE_LABELS["custom"],
}


def _point_at_first_server(monkeypatch, module_name: str, servers: _Servers) -> None:
    module = importlib.import_module(f"stt_app.transcriber.{module_name}")
    real = _http_utils.open_request

    def moved(request, **kwargs):
        parts = urllib.parse.urlsplit(request.full_url)
        target = f"http://127.0.0.1:{servers.first_port}{parts.path}"
        if parts.query:
            target += f"?{parts.query}"
        request.full_url = target
        return real(request, **kwargs)

    monkeypatch.setattr(module, "open_request", moved)


@pytest.mark.parametrize("code", REDIRECT_CODES)
@pytest.mark.parametrize("engine", BATCH_ENGINES)
def test_an_upload_is_never_redirected_and_the_key_stays_put(
    monkeypatch, servers, engine, code
):
    servers.code = code
    module_name, transcriber = _build(engine, KEY)
    _point_at_first_server(monkeypatch, module_name, servers)

    with pytest.raises(TranscriptionError) as raised:
        transcriber.transcribe_batch(WAV)

    message = str(raised.value)
    assert servers.second_hits == []
    assert servers.first_hits == 1
    assert NAMES[engine] in message
    assert "redirected" in message
    assert f"127.0.0.1:{servers.second_port}" in message
    assert KEY not in message


@pytest.mark.parametrize("code", REDIRECT_CODES)
@pytest.mark.parametrize("engine", ENGINES)
def test_a_key_check_is_never_redirected_to_another_host(
    monkeypatch, servers, engine, code
):
    servers.code = code
    module_name, transcriber = _build(engine, KEY)
    _point_at_first_server(monkeypatch, module_name, servers)

    ok, message = transcriber.test_connection()

    assert ok is False
    assert servers.second_hits == []
    assert NAMES[engine] in message
    assert "redirected" in message
    assert f"127.0.0.1:{servers.second_port}" in message
    assert KEY not in message


@pytest.mark.parametrize("key", UNSAFE_KEYS)
@pytest.mark.parametrize("engine", ENGINES)
def test_a_key_that_cannot_be_a_header_is_refused_before_a_request_and_not_printed(
    monkeypatch, servers, engine, key
):
    try:
        module_name, transcriber = _build(engine, key)
    except TranscriptionError as exc:
        # The custom endpoint's constructor has its own, older key check.
        assert engine in ("custom", "openrouter")
        assert key not in str(exc)
        return
    _point_at_first_server(monkeypatch, module_name, servers)

    ok, message = transcriber.test_connection()

    assert ok is False
    assert servers.first_hits == 0
    assert key not in message
    assert re.search(r"line break|header|character", message)
    if engine in BATCH_ENGINES:
        with pytest.raises(TranscriptionError) as raised:
            transcriber.transcribe_batch(WAV)
        assert key not in str(raised.value)
        assert servers.first_hits == 0


def test_a_redirect_inside_one_origin_is_followed_only_for_a_get():
    """Documented policy: a GET that stays on its origin keeps its key where
    the user already sent it; an upload is never redirected (the body would be
    dropped by the re-sent GET)."""
    keeps = _http_utils.redirect_keeps_origin
    assert keeps("https://api.example.com/a", "https://api.example.com/b")
    assert not keeps("https://api.example.com/a", "http://api.example.com/b")
    assert not keeps("https://api.example.com/a", "https://other.example.com/a")
