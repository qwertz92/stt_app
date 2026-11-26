"""The SDK and WebSocket engines never show their own API key either.

`tests/test_rest_key_scrub.py` covers the REST engines. Groq (an SDK on httpx),
AssemblyAI (an SDK, plus a streaming client) and Fun-ASR (a WebSocket) put the
text of an SDK exception or of a server message into the error they raise; a
server that echoes the credential it refused ("invalid key <key>") must not get
it into the overlay or the log.
"""

from __future__ import annotations

import logging
import time

import pytest
from _rest_fakes import wav_seconds
from test_assemblyai_provider import FakeStreamingClient, _make_fake_aai
from test_elevenlabs_realtime import _FakeModule as _ElevenLabsFakeModule
from test_elevenlabs_realtime import _FakeSocket
from test_funasr_provider import FakeWS, _event, _wav_bytes
from test_soniox_streaming import _FakeApp
from test_soniox_streaming import _FakeModule as _SonioxFakeModule

from stt_app.transcriber.assemblyai_provider import AssemblyAITranscriber
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.elevenlabs_provider import ElevenLabsTranscriber
from stt_app.transcriber.funasr_provider import FunAsrTranscriber
from stt_app.transcriber.groq_provider import GroqTranscriber
from stt_app.transcriber.soniox_provider import SonioxTranscriber

_KEYS = ["sk-live-0123456789abcdef", 'ab"cd\\ef-0123456789']


def _echo(key: str) -> str:
    return f"Error code: 400 - {{'error': {{'message': 'invalid key {key}'}}}}"


def _assert_clean(text: str, key: str, caplog) -> None:
    assert key not in text
    assert key not in caplog.text


class _FailingGroq:
    def __init__(self, message: str):
        class Transcriptions:
            def create(self, **_kwargs):
                raise RuntimeError(message)

        class Models:
            def list(self):
                raise RuntimeError(message)

        class Audio:
            transcriptions = Transcriptions()

        self.audio = Audio()
        self.models = Models()


@pytest.mark.parametrize("key", _KEYS, ids=["plain", "escaped"])
def test_groq_batch_and_key_check_never_show_the_key(caplog, key):
    caplog.set_level(logging.DEBUG)
    message = _echo(key)
    groq = GroqTranscriber(
        api_key=key, groq_client_class=lambda **_kwargs: _FailingGroq(message)
    )

    with pytest.raises(TranscriptionError) as raised:
        groq.transcribe_batch(wav_seconds(1.0))
    _assert_clean(str(raised.value), key, caplog)

    ok, text = groq.test_connection()
    assert not ok
    _assert_clean(text, key, caplog)


@pytest.mark.parametrize("key", _KEYS, ids=["plain", "escaped"])
def test_assemblyai_batch_never_shows_the_key(caplog, key):
    caplog.set_level(logging.DEBUG)
    message = _echo(key)

    # A job that finished with an error status carrying the key.
    t = AssemblyAITranscriber(api_key=key, aai_module=_make_fake_aai(error=message))
    with pytest.raises(TranscriptionError) as raised:
        t.transcribe_batch(b"RIFF fake")
    _assert_clean(str(raised.value), key, caplog)

    # An SDK exception carrying the key.
    fake = _make_fake_aai()

    class Exploding:
        def submit(self, audio_file, config=None):
            raise RuntimeError(message)

    fake.Transcriber = Exploding
    t = AssemblyAITranscriber(api_key=key, aai_module=fake)
    with pytest.raises(TranscriptionError) as raised:
        t.transcribe_batch(b"RIFF fake")
    _assert_clean(str(raised.value), key, caplog)


@pytest.mark.parametrize("key", _KEYS, ids=["plain", "escaped"])
def test_assemblyai_streaming_never_shows_the_key(caplog, key):
    caplog.set_level(logging.DEBUG)
    message = _echo(key)

    class FailingConnect(FakeStreamingClient):
        def connect(self, params):
            raise ConnectionError(message)

    t = AssemblyAITranscriber(
        api_key=key,
        aai_module=_make_fake_aai(),
        streaming_client_factory=lambda _key: FailingConnect(api_key=key),
    )
    with pytest.raises(TranscriptionError) as raised:
        t.start_stream()
    _assert_clean(str(raised.value), key, caplog)

    class ErrorViaHandler(FakeStreamingClient):
        def connect(self, params):
            self.emit("Error", RuntimeError(message))

    t = AssemblyAITranscriber(
        api_key=key,
        aai_module=_make_fake_aai(),
        streaming_client_factory=lambda _key: ErrorViaHandler(api_key=key),
    )
    with pytest.raises(TranscriptionError) as raised:
        t.start_stream()
    _assert_clean(str(raised.value), key, caplog)

    class FailingSend(FakeStreamingClient):
        def stream(self, chunk):
            raise ConnectionError(message)

    t = AssemblyAITranscriber(
        api_key=key,
        aai_module=_make_fake_aai(),
        streaming_client_factory=lambda _key: FailingSend(api_key=key),
    )
    t.start_stream()
    with pytest.raises(TranscriptionError) as raised:
        t.push_audio_chunk(b"\x00\x00" * 1600)
    _assert_clean(str(raised.value), key, caplog)


@pytest.mark.parametrize("key", _KEYS, ids=["plain", "escaped"])
def test_soniox_streaming_never_shows_the_key(monkeypatch, caplog, key):
    """The socket path: a handshake failure, an error frame and a send failure
    all carry the server's or the library's text, which may echo the key."""
    caplog.set_level(logging.DEBUG)
    message = _echo(key)
    _FakeApp.instances = []
    _FakeApp.end_reply = []
    _FakeApp.never_opens = False
    _FakeApp.block_binary = None

    def transcriber() -> SonioxTranscriber:
        t = SonioxTranscriber(api_key=key)
        monkeypatch.setattr(t, "_get_websocket_module", lambda: _SonioxFakeModule)
        return t

    _FakeApp.open_error = ConnectionError(message)
    with pytest.raises(TranscriptionError) as raised:
        transcriber().start_stream()
    _assert_clean(str(raised.value), key, caplog)
    _FakeApp.open_error = None

    errors: list[str] = []
    t = transcriber()
    t.start_stream(on_error=errors.append)
    _FakeApp.instances[-1].deliver(
        {
            "tokens": [],
            "error_code": 400,
            "error_type": "invalid_request",
            "error_message": message,
        }
    )
    _FakeApp.end_reply = None
    with pytest.raises(TranscriptionError) as raised:
        t.stop_stream()
    for text in (*errors, str(raised.value)):
        _assert_clean(text, key, caplog)
    assert errors

    errors = []
    t = transcriber()
    t.start_stream(on_error=errors.append)

    def failing_send(payload, opcode=None):
        raise ConnectionError(message)

    monkeypatch.setattr(_FakeApp.instances[-1], "send", failing_send)
    t.push_audio_chunk(b"\x00\x00" * 1600)
    deadline = time.monotonic() + 2.0
    while not errors and time.monotonic() < deadline:
        time.sleep(0.01)
    t.abort_stream()
    assert errors
    for text in errors:
        _assert_clean(text, key, caplog)


@pytest.mark.parametrize("key", _KEYS, ids=["plain", "escaped"])
def test_funasr_never_shows_the_key(monkeypatch, caplog, key):
    caplog.set_level(logging.DEBUG)
    message = _echo(key)
    t = FunAsrTranscriber(api_key=key)

    ws = FakeWS([_event("task-failed", error_message=message)])
    monkeypatch.setattr(FunAsrTranscriber, "_connect", lambda *_a, **_k: ws)
    with pytest.raises(TranscriptionError) as raised:
        t.transcribe_batch(_wav_bytes())
    _assert_clean(str(raised.value), key, caplog)

    def refuse(*_args, **_kwargs):
        raise ConnectionError(message)

    monkeypatch.setattr(FunAsrTranscriber, "_connect", refuse)
    with pytest.raises(TranscriptionError) as raised:
        t.transcribe_batch(_wav_bytes())
    _assert_clean(str(raised.value), key, caplog)

    ok, text = t.test_connection()
    assert not ok
    _assert_clean(text, key, caplog)

    ws = FakeWS([_event("task-failed", error_message=message)])
    monkeypatch.setattr(FunAsrTranscriber, "_connect", lambda *_a, **_k: ws)
    ok, text = t.test_connection()
    assert not ok
    _assert_clean(text, key, caplog)


@pytest.mark.parametrize("key", _KEYS, ids=["plain", "escaped"])
def test_elevenlabs_streaming_never_shows_the_key(caplog, key):
    caplog.set_level(logging.DEBUG)
    message = _echo(key)

    def make():
        t = ElevenLabsTranscriber(api_key=key)
        t._get_websocket_module = lambda: _ElevenLabsFakeModule
        return t

    # The server echoes the key in a refused handshake, in an error message
    # and in a close reason; a failed send echoes it in the socket error.
    _FakeSocket.on_run = staticmethod(
        lambda ws: ws.on_error(ws, ConnectionError(message))
    )
    t = make()
    with pytest.raises(TranscriptionError) as raised:
        t.start_stream()
    _assert_clean(str(raised.value), key, caplog)

    _FakeSocket.on_run = staticmethod(lambda ws: ws.say(message_type="session_started"))
    t = make()
    errors: list[str] = []
    t.start_stream(on_error=errors.append)
    _FakeSocket.instances[-1].say(message_type="invalid_request", error=message)
    assert errors
    _assert_clean(errors[0], key, caplog)
    with pytest.raises(TranscriptionError) as raised:
        t.stop_stream()
    _assert_clean(str(raised.value), key, caplog)

    t = make()
    errors = []
    t.start_stream(on_error=errors.append)
    ws = _FakeSocket.instances[-1]
    ws.on_close(ws, 1008, message)
    assert errors
    _assert_clean(errors[0], key, caplog)
    t.abort_stream()


def test_elevenlabs_key_that_straddles_the_cut_is_scrubbed_before_cutting(caplog):
    caplog.set_level(logging.DEBUG)
    key = "sk_" + "0123456789abcdef" * 3
    for padding in (270, 297, 299):  # the key starts before, at and after the cut
        t = ElevenLabsTranscriber(api_key=key)
        t._get_websocket_module = lambda: _ElevenLabsFakeModule
        errors: list[str] = []
        t.start_stream(on_error=errors.append)
        ws = _FakeSocket.instances[-1]
        ws.say(message_type="quota_exceeded", error="x" * padding + key)
        assert errors
        assert key[:12] not in errors[0]
        t.abort_stream()
    ws.say(message_type="warning", warning="y" * 290 + key)
    assert key[:12] not in caplog.text
