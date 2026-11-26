"""ElevenLabs realtime streaming (Scribe v3 Live) against a fake WebSocket.

The wire format is the documented one (docs/agents/remote-providers.md,
"ElevenLabs realtime streaming"); nothing here has run against the live service.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.parse

import pytest

from stt_app.transcriber import elevenlabs_realtime
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.elevenlabs_provider import ElevenLabsTranscriber

KEY = "xi-secret-0123456789abcdef"


class _FakeSocket:
    """A WebSocketApp stand-in; `script` decides what the server says."""

    instances: list[_FakeSocket] = []
    on_run = None  # called with the socket when run_forever starts
    on_commit = None  # called with the socket when the final commit arrives
    on_chunk = None

    def __init__(self, url, header, on_open, on_message, on_error, on_close):
        self.url, self.header = url, header
        self.on_message, self.on_error, self.on_close = on_message, on_error, on_close
        self.sent: list[dict] = []
        self.closed = False
        _FakeSocket.instances.append(self)

    def run_forever(self):
        if _FakeSocket.on_run is not None:
            _FakeSocket.on_run(self)

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        if message["commit"] and not message["audio_base_64"]:
            if _FakeSocket.on_commit is not None:
                _FakeSocket.on_commit(self)
        elif _FakeSocket.on_chunk is not None:
            _FakeSocket.on_chunk(self)

    def close(self):
        if not self.closed:
            self.closed = True
            self.on_close(self, 1000, "bye")

    def say(self, **fields):
        self.on_message(self, json.dumps(fields))

    def server_closes(self, code, reason=""):
        """What websocket-client 1.9.2 does for a close frame of the server:
        `on_error` first, with an exception whose `status_code` is the close
        code, then `on_close` (`_app.py`, `closed()` and `handleDisconnect`)."""
        error = ConnectionError(f"Connection closed (code {code}): {reason}")
        error.status_code = code
        self.on_error(self, error)
        self.on_close(self, code, reason)


class _FakeModule:
    WebSocketApp = _FakeSocket


@pytest.fixture(autouse=True)
def _fresh_fake():
    _FakeSocket.instances = []
    _FakeSocket.on_run = _FakeSocket.on_commit = _FakeSocket.on_chunk = None
    _FakeSocket.on_run = staticmethod(lambda ws: ws.say(message_type="session_started"))
    yield


def _transcriber(language="auto", key=KEY):
    t = ElevenLabsTranscriber(api_key=key, language_mode=language)
    t._get_websocket_module = lambda: _FakeModule
    return t


def _wait_for(condition, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def _params(ws):
    return urllib.parse.parse_qs(urllib.parse.urlparse(ws.url).query)


def test_the_key_goes_in_a_header_and_the_url_names_the_live_model():
    t = _transcriber(language="de")
    t.start_stream()
    ws = _FakeSocket.instances[-1]
    assert f"xi-api-key: {KEY}" in ws.header
    assert KEY not in ws.url
    assert ws.url.startswith("wss://api.elevenlabs.io/v1/speech-to-text/realtime?")
    params = _params(ws)
    assert params["model_id"] == ["scribe_v3_live"]
    assert params["audio_format"] == ["pcm_16000"]
    assert params["commit_strategy"] == ["vad"]
    assert params["language_code"] == ["deu"]
    t.abort_stream()


def test_auto_sends_no_language_code():
    t = _transcriber()
    t.start_stream()
    assert "language_code" not in _params(_FakeSocket.instances[-1])
    t.abort_stream()


def test_a_partial_then_a_committed_transcript_reach_the_callback_in_order():
    t = _transcriber()
    seen: list[str] = []
    t.start_stream(on_partial=seen.append)
    ws = _FakeSocket.instances[-1]
    ws.say(message_type="partial_transcript", text="hello wor")
    ws.say(message_type="partial_transcript", text="hello world")
    ws.say(message_type="committed_transcript", text="hello world.")
    ws.say(message_type="partial_transcript", text="how are")
    assert seen == ["hello wor", "hello world", "hello world.", "hello world. how are"]
    t.abort_stream()


def test_audio_is_sent_as_base64_pcm_chunks_in_order():
    t = _transcriber()
    t.start_stream()
    ws = _FakeSocket.instances[-1]
    t.push_audio_chunk(b"\x01\x02" * 8)
    t.push_audio_chunk(b"\x03\x04" * 8)
    _wait_for(lambda: len(ws.sent) == 2)
    assert [base64.b64decode(m["audio_base_64"]) for m in ws.sent] == [
        b"\x01\x02" * 8,
        b"\x03\x04" * 8,
    ]
    assert all(
        m["message_type"] == "input_audio_chunk" and not m["commit"] for m in ws.sent
    )
    assert all(m["sample_rate"] == 16000 for m in ws.sent)
    t.abort_stream()


def test_stop_sends_the_audio_then_the_final_commit_and_returns_the_last_segment():
    t = _transcriber()
    seen: list[str] = []
    t.start_stream(on_partial=seen.append)
    ws = _FakeSocket.instances[-1]
    _FakeSocket.on_commit = lambda sock: sock.say(
        message_type="committed_transcript", text="the end"
    )
    ws.say(message_type="committed_transcript", text="first part")
    ws.say(message_type="partial_transcript", text="the en")
    t.push_audio_chunk(b"\x00\x00" * 8)

    assert t.stop_stream() == "first part the end"
    assert ws.sent[0]["audio_base_64"] and not ws.sent[0]["commit"]
    assert ws.sent[-1] == {
        "message_type": "input_audio_chunk",
        "audio_base_64": "",
        "commit": True,
        "sample_rate": 16000,
    }
    assert ws.closed
    with pytest.raises(TranscriptionError, match="not active"):
        t.push_audio_chunk(b"\x00\x00")


def test_stop_does_not_wait_forever_for_the_final_segment(monkeypatch):
    monkeypatch.setattr(elevenlabs_realtime, "_FINAL_COMMIT_WAIT_S", 0.2)
    t = _transcriber()
    t.start_stream()
    ws = _FakeSocket.instances[-1]
    ws.say(message_type="partial_transcript", text="unfinished words")
    started = time.monotonic()
    assert t.stop_stream() == "unfinished words"
    assert time.monotonic() - started < 2.0


def test_an_insufficient_audio_answer_to_the_final_commit_is_not_an_error():
    t = _transcriber()
    t.start_stream()
    ws = _FakeSocket.instances[-1]
    ws.say(message_type="committed_transcript", text="all said")
    _FakeSocket.on_commit = lambda sock: sock.say(
        message_type="insufficient_audio_activity", error="not enough speech"
    )
    assert t.stop_stream() == "all said"


def test_a_handshake_the_server_refuses_is_a_readable_error():
    class Refused(Exception):
        status_code = 401

    _FakeSocket.on_run = staticmethod(
        lambda ws: ws.on_error(ws, Refused("Handshake status 401"))
    )
    t = _transcriber()
    with pytest.raises(TranscriptionError, match="authentication failed"):
        t.start_stream()
    # The session is released: a retry is not refused as "already active".
    _FakeSocket.on_run = staticmethod(lambda ws: ws.say(message_type="session_started"))
    t.start_stream()
    t.abort_stream()


def test_a_server_that_never_sends_session_started_is_an_error(monkeypatch):
    monkeypatch.setattr(elevenlabs_realtime, "_CONNECT_TIMEOUT_S", 0.1)
    _FakeSocket.on_run = staticmethod(lambda ws: None)
    t = _transcriber()
    with pytest.raises(TranscriptionError, match="session_started"):
        t.start_stream()
    assert _FakeSocket.instances[-1].closed


def test_a_server_that_closes_during_the_handshake_is_an_error():
    _FakeSocket.on_run = staticmethod(lambda ws: ws.server_closes(1008, "policy"))
    t = _transcriber()
    with pytest.raises(TranscriptionError, match=r"closed by the service \(code 1008"):
        t.start_stream()


@pytest.mark.parametrize(
    ("message_type", "expected"),
    [
        ("quota_exceeded", "quota exceeded"),
        ("session_time_limit_exceeded", "maximum session time"),
        ("rate_limited", "rate limit"),
        ("auth_error", "authentication failed"),
    ],
)
def test_a_server_error_message_reaches_on_error_once_with_a_sentence(
    message_type, expected
):
    t = _transcriber()
    errors: list[str] = []
    t.start_stream(on_error=errors.append)
    ws = _FakeSocket.instances[-1]
    ws.say(message_type=message_type, error="details")
    ws.say(message_type="error", error="second")
    assert len(errors) == 1 and expected in errors[0]
    ws.say(message_type="committed_transcript", text="kept")
    assert t.stop_stream() == "kept"


def test_a_failed_session_without_text_raises_at_stop():
    t = _transcriber()
    t.start_stream(on_error=lambda _m: None)
    _FakeSocket.instances[-1].say(message_type="quota_exceeded", error="x")
    with pytest.raises(TranscriptionError, match="quota exceeded"):
        t.stop_stream()


def test_a_transcript_message_without_text_is_an_error_not_pasted_text():
    t = _transcriber()
    errors: list[str] = []
    seen: list[str] = []
    t.start_stream(on_partial=seen.append, on_error=errors.append)
    _FakeSocket.instances[-1].say(message_type="partial_transcript", text=None)
    assert seen == [] and "without text" in errors[0]
    t.abort_stream()


def test_a_socket_closed_mid_stream_is_reported_and_keeps_the_text():
    t = _transcriber()
    errors: list[str] = []
    t.start_stream(on_error=errors.append)
    ws = _FakeSocket.instances[-1]
    ws.say(message_type="committed_transcript", text="already said")
    ws.server_closes(1011, "internal")
    assert len(errors) == 1
    assert "closed by the service (code 1011)" in errors[0]
    assert "HTTP" not in errors[0]
    assert t.stop_stream() == "already said"


def test_a_stop_before_the_handshake_finished_leaves_no_orphan_session():
    gate = threading.Event()
    _FakeSocket.on_run = staticmethod(
        lambda ws: (gate.wait(2.0), ws.say(message_type="session_started"))
    )
    t = _transcriber()
    outcome: list[BaseException] = []

    def connect():
        try:
            t.start_stream()
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=connect)
    worker.start()
    _wait_for(lambda: _FakeSocket.instances)
    with pytest.raises(TranscriptionError, match="not active"):
        t.stop_stream()
    gate.set()
    worker.join(timeout=3.0)
    assert outcome and "stopped while connecting" in str(outcome[0])
    assert _FakeSocket.instances[-1].closed
    t.start_stream()  # not refused as "already active"
    t.abort_stream()


def test_abort_closes_the_socket_and_drops_the_text():
    t = _transcriber()
    t.start_stream()
    ws = _FakeSocket.instances[-1]
    t.abort_stream()
    assert ws.closed
    t.abort_stream()  # idempotent


def test_a_second_start_is_refused_while_a_session_runs():
    t = _transcriber()
    t.start_stream()
    with pytest.raises(TranscriptionError, match="already active"):
        t.start_stream()
    t.abort_stream()


def test_the_sender_keeps_a_quiet_session_open_with_silence(monkeypatch):
    monkeypatch.setattr(elevenlabs_realtime, "_KEEPALIVE_S", 0.05)
    t = _transcriber()
    t.start_stream()
    ws = _FakeSocket.instances[-1]
    _wait_for(lambda: ws.sent)
    first = ws.sent[0]
    assert set(base64.b64decode(first["audio_base_64"])) == {0}
    assert not first["commit"]
    t.abort_stream()


def test_a_microphone_that_stops_for_good_ends_the_stream_with_a_sentence(monkeypatch):
    monkeypatch.setattr(elevenlabs_realtime, "_KEEPALIVE_S", 0.02)
    monkeypatch.setattr(elevenlabs_realtime, "_MAX_GAP_S", 0.05)
    t = _transcriber()
    errors: list[str] = []
    t.start_stream(on_error=errors.append)
    _wait_for(lambda: errors)
    assert "no audio" in errors[0]
    t.abort_stream()


def test_a_send_that_fails_reports_once_without_the_key(caplog):
    caplog.set_level(logging.DEBUG)
    t = _transcriber()
    errors: list[str] = []
    t.start_stream(on_error=errors.append)
    ws = _FakeSocket.instances[-1]

    def broken(_text):
        raise ConnectionError(f"refused for key {KEY}")

    ws.send = broken
    t.push_audio_chunk(b"\x00\x00" * 8)
    _wait_for(lambda: errors)
    assert KEY not in errors[0] and KEY not in caplog.text
    t.abort_stream()


def test_an_audio_queue_that_is_full_fails_the_push_without_blocking(monkeypatch):
    monkeypatch.setattr(elevenlabs_realtime, "_QUEUE_MAX_CHUNKS", 1)
    gate = threading.Event()
    _FakeSocket.on_chunk = staticmethod(lambda _ws: gate.wait(2.0))
    t = _transcriber()
    t.start_stream()
    # `_QUEUE_MAX_CHUNKS` is read when the session is created, above.
    with pytest.raises(TranscriptionError, match="queue is full"):
        for _ in range(50):
            t.push_audio_chunk(b"\x00\x00" * 8)
    gate.set()
    t.abort_stream()


def test_a_late_vad_commit_before_the_answer_does_not_cut_off_the_last_words():
    t = _transcriber()
    t.start_stream()

    def answer(sock):
        def later():
            sock.say(message_type="committed_transcript", text="first segment")
            time.sleep(0.2)
            sock.say(message_type="committed_transcript", text="last words")

        threading.Thread(target=later, daemon=True).start()

    _FakeSocket.on_commit = staticmethod(answer)
    t.push_audio_chunk(b"\x00\x00" * 8)
    assert t.stop_stream() == "first segment last words"


def test_an_answer_with_nothing_after_it_ends_the_stop_after_the_quiet_gap():
    t = _transcriber()
    t.start_stream()
    _FakeSocket.on_commit = staticmethod(
        lambda sock: sock.say(message_type="committed_transcript", text="done")
    )
    started = time.monotonic()
    assert t.stop_stream() == "done"
    assert time.monotonic() - started < 1.5


def test_a_partial_after_the_first_commit_keeps_the_wait_open_until_its_commit():
    t = _transcriber()
    t.start_stream()

    def answer(sock):
        def later():
            sock.say(message_type="committed_transcript", text="one")
            time.sleep(0.1)
            sock.say(message_type="partial_transcript", text="tw")
            time.sleep(0.6)  # longer than the quiet gap: a partial is pending
            sock.say(message_type="committed_transcript", text="two")

        threading.Thread(target=later, daemon=True).start()

    _FakeSocket.on_commit = staticmethod(answer)
    assert t.stop_stream() == "one two"


def test_a_server_close_during_stop_is_not_an_error_and_ends_the_wait_at_once():
    t = _transcriber()
    errors: list[str] = []
    t.start_stream(on_error=errors.append)
    _FakeSocket.on_commit = staticmethod(lambda sock: sock.server_closes(1000, "bye"))
    started = time.monotonic()
    assert t.stop_stream() == ""
    assert time.monotonic() - started < 1.0
    assert errors == []


def test_a_close_that_only_calls_on_close_during_stop_ends_the_wait_at_once():
    t = _transcriber()
    t.start_stream()
    _FakeSocket.on_commit = staticmethod(lambda sock: sock.on_close(sock, 1000, "done"))
    started = time.monotonic()
    t.stop_stream()
    assert time.monotonic() - started < 1.0


def test_audio_not_sent_before_the_stop_is_logged_when_text_is_returned(
    monkeypatch, caplog
):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(elevenlabs_realtime, "_DRAIN_TIMEOUT_S", 0.2)
    _FakeSocket.on_chunk = staticmethod(lambda _ws: time.sleep(0.15))
    t = _transcriber()
    t.start_stream()
    _FakeSocket.instances[-1].say(message_type="committed_transcript", text="early")
    for _ in range(10):
        t.push_audio_chunk(b"\x01\x00" * 8)
    assert t.stop_stream() == "early"
    assert "could not be sent before the stop" in caplog.text
