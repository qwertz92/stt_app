"""Soniox realtime streaming: the handshake, the token semantics, the end of the
stream and every way it fails, against a fake `websocket` module.

Nothing here reaches the network; the protocol is the one Soniox documents
(read 2026-11-26), which `docs/agents/remote-providers.md` lists.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time

import pytest

from stt_app.config import STREAMING_ENGINES, VALID_ENGINES, supports_streaming
from stt_app.transcriber import soniox_stream
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.soniox_provider import SonioxTranscriber

_KEY = "sx-live-0123456789abcdef"
_PCM = b"\x01\x00" * 1600


class _FakeABNF:
    OPCODE_BINARY = 2


class _FakeApp:
    """Records what the session sends and plays a script back to it."""

    instances: list[_FakeApp] = []
    # Messages delivered when the empty end-of-stream frame arrives, then the
    # server closes; `None` means the server answers nothing at all.
    end_reply: list[dict] | None = []
    open_error: Exception | None = None
    never_opens = False
    block_binary: threading.Event | None = None

    def __init__(self, url, header, on_open, on_message, on_error, on_close):
        self.url = url
        self.header = header
        self.on_open = on_open
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.sent: list[tuple[object, object]] = []
        self.closed = False
        _FakeApp.instances.append(self)

    def run_forever(self):
        if _FakeApp.never_opens:
            return
        if _FakeApp.open_error is not None:
            self.on_error(self, _FakeApp.open_error)
            return
        self.on_open(self)

    def send(self, payload, opcode=None):
        self.sent.append((payload, opcode))
        if opcode == _FakeABNF.OPCODE_BINARY and _FakeApp.block_binary is not None:
            assert _FakeApp.block_binary.wait(timeout=3.0)
        if payload == "" and opcode is None and _FakeApp.end_reply is not None:
            for message in _FakeApp.end_reply:
                self.on_message(self, json.dumps(message))
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.on_close(self, 1000, "closed")

    def deliver(self, message: dict) -> None:
        self.on_message(self, json.dumps(message))

    def texts(self) -> list[str]:
        return [p for p, op in self.sent if op is None]

    def audio(self) -> list[bytes]:
        return [p for p, op in self.sent if op == _FakeABNF.OPCODE_BINARY]


class _FakeModule:
    ABNF = _FakeABNF
    WebSocketApp = _FakeApp


def _token(text: str, final: bool) -> dict:
    return {"text": text, "is_final": final}


def _tokens(*tokens: dict, **extra) -> dict:
    return {"tokens": list(tokens), **extra}


@pytest.fixture(autouse=True)
def _fresh_fake():
    _FakeApp.instances = []
    _FakeApp.end_reply = []
    _FakeApp.open_error = None
    _FakeApp.never_opens = False
    _FakeApp.block_binary = None


def _transcriber(monkeypatch, **kwargs) -> SonioxTranscriber:
    transcriber = SonioxTranscriber(kwargs.pop("api_key", _KEY), **kwargs)
    monkeypatch.setattr(transcriber, "_get_websocket_module", lambda: _FakeModule)
    return transcriber


def _wait_for(condition, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("Condition was not met within timeout.")


def _start(transcriber, **kwargs):
    transcriber.start_stream(**kwargs)
    return _FakeApp.instances[-1]


class TestSonioxStreamingStart:
    def test_soniox_is_a_streaming_engine_with_the_experimental_label(self):
        from stt_app.config import streaming_is_experimental

        assert "soniox" in VALID_ENGINES
        assert "soniox" in STREAMING_ENGINES
        assert supports_streaming("soniox")
        assert streaming_is_experimental("soniox")

    def test_the_key_is_in_the_header_and_nowhere_else(self, monkeypatch):
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber)
        try:
            assert ws.url == "wss://stt-rt.soniox.com/transcribe-websocket"
            assert ws.header == [f"Authorization: Bearer {_KEY}"]
            assert _KEY not in ws.url
            assert _KEY not in "".join(ws.texts())
        finally:
            transcriber.abort_stream()

    def test_the_start_message_is_the_first_frame_and_declares_the_audio(
        self, monkeypatch
    ):
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber)
        try:
            first_payload, first_opcode = ws.sent[0]
            assert first_opcode is None
            assert json.loads(first_payload) == {
                "model": "stt-rt-v6",
                "audio_format": "pcm_s16le",
                "sample_rate": 16000,
                "num_channels": 1,
            }
        finally:
            transcriber.abort_stream()

    def test_a_chosen_language_and_the_vocabulary_are_in_the_start_message(
        self, monkeypatch
    ):
        transcriber = _transcriber(
            monkeypatch, language_mode="de", custom_vocabulary="Kubernetes, Grafana"
        )
        ws = _start(transcriber)
        try:
            message = json.loads(ws.sent[0][0])
            assert message["language_hints"] == ["de"]
            assert message["language_hints_strict"] is True
            assert message["context"] == {"terms": ["Kubernetes", "Grafana"]}
            assert "api_key" not in message
        finally:
            transcriber.abort_stream()

    @pytest.mark.parametrize(
        ("region", "host"),
        [
            ("us", "stt-rt.soniox.com"),
            ("eu", "stt-rt.eu.soniox.com"),
            ("jp", "stt-rt.jp.soniox.com"),
            ("in", "stt-rt.in.soniox.com"),
        ],
    )
    def test_the_region_picks_the_realtime_host(self, monkeypatch, region, host):
        transcriber = _transcriber(monkeypatch, region=region)
        ws = _start(transcriber)
        try:
            assert ws.url == f"wss://{host}/transcribe-websocket"
        finally:
            transcriber.abort_stream()

    def test_a_second_start_is_refused_while_one_is_active(self, monkeypatch):
        transcriber = _transcriber(monkeypatch)
        _start(transcriber)
        try:
            with pytest.raises(TranscriptionError, match="already active"):
                transcriber.start_stream()
        finally:
            transcriber.abort_stream()

    def test_pushing_or_stopping_without_a_session_is_refused(self, monkeypatch):
        transcriber = _transcriber(monkeypatch)
        with pytest.raises(TranscriptionError, match="not active"):
            transcriber.push_audio_chunk(_PCM)
        with pytest.raises(TranscriptionError, match="not active"):
            transcriber.stop_stream()
        transcriber.abort_stream()

    def test_a_missing_websocket_client_is_named(self, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "websocket", None)
        with pytest.raises(TranscriptionError, match="websocket-client"):
            SonioxTranscriber(_KEY).start_stream()


class TestSonioxStreamingText:
    def test_final_tokens_are_kept_and_the_provisional_tail_is_replaced(
        self, monkeypatch
    ):
        seen: list[str] = []
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber, on_partial=seen.append)
        try:
            ws.deliver(_tokens(_token("Hel", False), _token("lo", False)))
            ws.deliver(_tokens(_token("Hello", True), _token(" wor", False)))
            ws.deliver(_tokens(_token(" world", True), _token(" and", False)))
            ws.deliver(_tokens(_token(" and", True), _token(" so", False)))
            assert seen == [
                "Hello",
                "Hello wor",
                "Hello world and",
                "Hello world and so",
            ]
        finally:
            transcriber.abort_stream()

    def test_a_message_without_tokens_changes_nothing_and_emits_nothing(
        self, monkeypatch
    ):
        seen: list[str] = []
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber, on_partial=seen.append)
        try:
            ws.deliver(_tokens(_token("Hallo", True)))
            ws.deliver(_tokens(final_audio_proc_ms=100, total_audio_proc_ms=100))
            ws.deliver({"unexpected": True})
            assert seen == ["Hallo"]
        finally:
            transcriber.abort_stream()

    def test_the_markers_the_model_may_add_are_never_text(self, monkeypatch):
        seen: list[str] = []
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber, on_partial=seen.append)
        try:
            ws.deliver(
                _tokens(
                    _token("Fertig.", True),
                    _token("<end>", True),
                    _token("<fin>", True),
                )
            )
            assert seen == ["Fertig."]
        finally:
            transcriber.abort_stream()

    def test_stop_sends_the_audio_then_the_empty_frame_and_returns_the_text(
        self, monkeypatch
    ):
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber)
        _FakeApp.end_reply = [
            _tokens(_token("Guten", True), _token(" Tag", True)),
            _tokens(finished=True, final_audio_proc_ms=1560, total_audio_proc_ms=1680),
        ]
        transcriber.push_audio_chunk(_PCM)
        transcriber.push_audio_chunk(_PCM)

        text = transcriber.stop_stream()

        assert text == "Guten Tag"
        assert ws.audio() == [_PCM, _PCM]
        # The empty text frame comes after every audio frame, and nothing is
        # sent after it.
        assert ws.sent[-1] == ("", None)
        assert ws.closed
        # A new session may start at once.
        _start(transcriber)
        transcriber.abort_stream()

    def test_the_provisional_tail_at_the_end_is_kept_when_finished_never_comes(
        self, monkeypatch, caplog
    ):
        caplog.set_level(logging.WARNING)
        monkeypatch.setattr(soniox_stream, "_FINISH_WAIT_S", 0.2)
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber)
        _FakeApp.end_reply = None
        ws.deliver(_tokens(_token("Hallo", True), _token(" Welt", False)))

        assert transcriber.stop_stream() == "Hallo Welt"
        assert "no 'finished' message" in caplog.text

    def test_a_partial_callback_that_raises_is_logged_once_and_the_text_survives(
        self, monkeypatch, caplog
    ):
        def explode(_text: str) -> None:
            raise RuntimeError("boom")

        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber, on_partial=explode)
        _FakeApp.end_reply = [_tokens(finished=True)]
        ws.deliver(_tokens(_token("Eins", True)))
        ws.deliver(_tokens(_token(" zwei", True)))

        assert transcriber.stop_stream() == "Eins zwei"
        assert caplog.text.count("partial callback failed") == 1

    def test_a_message_that_is_not_json_is_dropped_and_logged_once(
        self, monkeypatch, caplog
    ):
        caplog.set_level(logging.WARNING)
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber)
        try:
            ws.on_message(ws, "<html>proxy</html>")
            ws.on_message(ws, "nope")
            assert caplog.text.count("not JSON") == 1
        finally:
            transcriber.abort_stream()


class TestSonioxStreamingFailures:
    def _error_frame(self, code: int, kind: str, message: str) -> dict:
        return {
            "tokens": [],
            "error_code": code,
            "error_type": kind,
            "error_message": message,
        }

    def test_a_refused_key_reaches_the_error_callback_with_the_region(
        self, monkeypatch
    ):
        errors: list[str] = []
        transcriber = _transcriber(monkeypatch, region="eu")
        ws = _start(transcriber, on_error=errors.append)
        try:
            ws.deliver(self._error_frame(401, "unauthenticated", "Incorrect API key"))
            assert len(errors) == 1
            assert "authentication failed (HTTP 401)" in errors[0]
            assert (
                "stt-rt.eu.soniox.com" in errors[0] or "api.eu.soniox.com" in errors[0]
            )
            assert errors[0].startswith("Soniox streaming failed")
        finally:
            transcriber.abort_stream()

    @pytest.mark.parametrize(
        ("code", "kind", "shows"),
        [
            (402, "organization_balance_exhausted", "balance or budget"),
            (408, "request_timeout", "request_timeout, HTTP 408"),
            (413, "max_duration_reached", "max_duration_reached, HTTP 413"),
            (429, "max_concurrent_connections_reached", "HTTP 429"),
            (503, "service_unavailable", "HTTP 503"),
        ],
    )
    def test_every_documented_error_is_a_readable_sentence(
        self, monkeypatch, code, kind, shows
    ):
        errors: list[str] = []
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber, on_error=errors.append)
        try:
            ws.deliver(self._error_frame(code, kind, "Soniox says no."))
            assert len(errors) == 1
            assert shows in errors[0]
        finally:
            transcriber.abort_stream()

    @pytest.mark.parametrize("key", [_KEY, 'ab"cd\\ef-0123456789'])
    def test_a_key_echoed_by_the_server_never_reaches_a_message_or_the_log(
        self, monkeypatch, caplog, key
    ):
        caplog.set_level(logging.DEBUG)
        errors: list[str] = []
        transcriber = _transcriber(monkeypatch, api_key=key)
        ws = _start(transcriber, on_error=errors.append)
        ws.deliver(self._error_frame(400, "invalid_request", f"bad credential {key}"))
        _FakeApp.end_reply = None
        with pytest.raises(TranscriptionError) as raised:
            transcriber.stop_stream()

        for text in (*errors, str(raised.value), caplog.text):
            assert key not in text
            assert json.dumps(key)[1:-1] not in text

    @pytest.mark.parametrize("key", [_KEY, 'ab"cd\\ef-0123456789'])
    def test_a_key_in_a_connection_failure_never_reaches_the_message(
        self, monkeypatch, caplog, key
    ):
        caplog.set_level(logging.DEBUG)
        _FakeApp.open_error = ConnectionError(f"Handshake status 401 for {key}")
        transcriber = _transcriber(monkeypatch, api_key=key)
        with pytest.raises(TranscriptionError, match="failed to connect") as raised:
            transcriber.start_stream()
        assert key not in str(raised.value)
        assert key not in caplog.text
        # The failed start left nothing behind.
        _FakeApp.open_error = None
        _start(transcriber)
        transcriber.abort_stream()

    def test_a_certificate_failure_gets_the_shared_guidance(self, monkeypatch):
        _FakeApp.open_error = ssl.SSLCertVerificationError("certificate verify failed")
        transcriber = _transcriber(monkeypatch)
        with pytest.raises(TranscriptionError) as raised:
            transcriber.start_stream()
        assert "SSL_CERT_FILE" in str(raised.value)

    def test_a_server_that_never_answers_the_handshake_times_out(self, monkeypatch):
        monkeypatch.setattr(soniox_stream, "_CONNECT_TIMEOUT_S", 0.1)
        _FakeApp.never_opens = True
        transcriber = _transcriber(monkeypatch)
        with pytest.raises(TranscriptionError, match="timeout"):
            transcriber.start_stream()
        assert _FakeApp.instances[-1].closed

    def test_a_close_in_the_middle_of_a_dictation_is_reported(self, monkeypatch):
        errors: list[str] = []
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber, on_error=errors.append)
        ws.close()
        assert len(errors) == 1
        assert "closed unexpectedly" in errors[0]
        with pytest.raises(TranscriptionError):
            transcriber.stop_stream()

    def test_an_error_after_text_returns_the_text_and_not_the_error(self, monkeypatch):
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber)
        ws.deliver(_tokens(_token("Schon gesagt", True)))
        ws.deliver(self._error_frame(503, "service_unavailable", "later"))
        assert transcriber.stop_stream() == "Schon gesagt"

    def test_the_service_ending_the_stream_early_is_reported(self, monkeypatch):
        errors: list[str] = []
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber, on_error=errors.append)
        try:
            ws.deliver(_tokens(finished=True))
            assert len(errors) == 1
            assert "ended the stream" in errors[0]
        finally:
            transcriber.abort_stream()

    def test_a_full_queue_fails_the_callback_push_instead_of_waiting(self, monkeypatch):
        monkeypatch.setattr(soniox_stream, "_AUDIO_QUEUE_MAX_CHUNKS", 2)
        _FakeApp.block_binary = threading.Event()
        transcriber = _transcriber(monkeypatch)
        _start(transcriber)
        try:
            with pytest.raises(TranscriptionError, match="queue is full"):
                for _ in range(10):
                    transcriber.push_audio_chunk(_PCM)
        finally:
            _FakeApp.block_binary.set()
            transcriber.abort_stream()

    def test_a_pusher_that_may_wait_is_not_failed_by_a_burst(self, monkeypatch):
        monkeypatch.setattr(soniox_stream, "_AUDIO_QUEUE_MAX_CHUNKS", 2)
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber)
        try:
            for _ in range(20):
                transcriber.push_audio_chunk(_PCM, block_timeout_s=2.0)
            _wait_for(lambda: len(ws.audio()) == 20)
        finally:
            transcriber.abort_stream()

    def test_an_oversized_chunk_is_split_under_the_message_limit(self, monkeypatch):
        monkeypatch.setattr(soniox_stream, "_AUDIO_MESSAGE_MAX_BYTES", 4000)
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber)
        try:
            transcriber.push_audio_chunk(bytes(10_000))
            _wait_for(lambda: sum(len(a) for a in ws.audio()) == 10_000)
            assert all(len(a) <= 4000 for a in ws.audio())
        finally:
            transcriber.abort_stream()


class TestSonioxStreamingKeepalive:
    def test_a_gap_in_the_audio_is_bridged_with_keepalive_frames(self, monkeypatch):
        monkeypatch.setattr(soniox_stream, "_KEEPALIVE_INTERVAL_S", 0.05)
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber)
        try:
            _wait_for(lambda: ws.texts().count(json.dumps({"type": "keepalive"})) >= 2)
            assert ws.audio() == []
        finally:
            transcriber.abort_stream()

    def test_steady_audio_sends_no_keepalive(self, monkeypatch):
        monkeypatch.setattr(soniox_stream, "_KEEPALIVE_INTERVAL_S", 0.3)
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber)
        try:
            for _ in range(8):
                transcriber.push_audio_chunk(_PCM)
                time.sleep(0.03)
            assert json.dumps({"type": "keepalive"}) not in ws.texts()
        finally:
            transcriber.abort_stream()

    def test_a_microphone_that_stopped_ends_the_stream_with_its_cause(
        self, monkeypatch
    ):
        monkeypatch.setattr(soniox_stream, "_KEEPALIVE_INTERVAL_S", 0.02)
        monkeypatch.setattr(soniox_stream, "_KEEPALIVE_MAX_GAP_S", 0.1)
        errors: list[str] = []
        transcriber = _transcriber(monkeypatch)
        _start(transcriber, on_error=errors.append)
        try:
            _wait_for(lambda: bool(errors))
            assert "microphone delivered no audio" in errors[0]
        finally:
            transcriber.abort_stream()


class TestSonioxStreamingAbort:
    def test_abort_closes_the_socket_and_frees_the_provider(self, monkeypatch):
        errors: list[str] = []
        transcriber = _transcriber(monkeypatch)
        ws = _start(transcriber, on_error=errors.append)
        transcriber.abort_stream()
        assert ws.closed
        assert errors == []
        _start(transcriber)
        transcriber.abort_stream()

    def test_abort_without_a_session_is_harmless(self, monkeypatch):
        _transcriber(monkeypatch).abort_stream()

    def test_a_stop_during_the_handshake_leaves_no_open_session(self, monkeypatch):
        monkeypatch.setattr(soniox_stream, "_CONNECT_TIMEOUT_S", 3.0)
        _FakeApp.never_opens = True
        transcriber = _transcriber(monkeypatch)
        outcome: list[Exception] = []

        def connect() -> None:
            try:
                transcriber.start_stream()
            except TranscriptionError as exc:
                outcome.append(exc)

        worker = threading.Thread(target=connect, daemon=True)
        worker.start()
        _wait_for(lambda: bool(_FakeApp.instances))
        time.sleep(0.05)
        with pytest.raises(TranscriptionError, match="not active"):
            transcriber.stop_stream()
        worker.join(timeout=3.0)

        assert not worker.is_alive()
        assert outcome and "stopped while connecting" in str(outcome[0])
        assert _FakeApp.instances[-1].closed
        _FakeApp.never_opens = False
        _start(transcriber)
        transcriber.abort_stream()
