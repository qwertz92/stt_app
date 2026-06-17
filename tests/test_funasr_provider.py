"""Tests for the Alibaba Fun-ASR (DashScope WebSocket) transcription provider."""

from __future__ import annotations

import io
import json
import wave
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.funasr_provider import (
    DEFAULT_FUNASR_MODEL,
    FunAsrTranscriber,
)


def _wav_bytes(pcm: bytes = b"\x00\x00" * 1600, sample_rate: int = 16000,
               channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _event(event: str, sentence_text: str | None = None,
           sentence_end: bool = False, error_message: str | None = None) -> str:
    header: dict = {"event": event, "task_id": "t"}
    if error_message is not None:
        header["error_message"] = error_message
    payload: dict = {}
    if sentence_text is not None:
        payload = {
            "output": {
                "sentence": {"text": sentence_text, "sentence_end": sentence_end}
            }
        }
    return json.dumps({"header": header, "payload": payload})


class FakeWS:
    def __init__(self, events: list[str]):
        self._events = list(events)
        self.sent_text: list[str] = []
        self.sent_binary: list[bytes] = []
        self.closed = False

    def send(self, data):
        self.sent_text.append(data)

    def send_binary(self, data):
        self.sent_binary.append(bytes(data))

    def recv(self):
        if not self._events:
            raise AssertionError("recv() called with no more scripted events")
        return self._events.pop(0)

    def close(self):
        self.closed = True


class TestFunAsrInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(TranscriptionError, match="key is missing"):
            FunAsrTranscriber(api_key="")

    def test_default_model(self):
        assert FunAsrTranscriber(api_key="k")._model == DEFAULT_FUNASR_MODEL

    def test_unknown_model_falls_back(self):
        assert FunAsrTranscriber(api_key="k", model="nope")._model == (
            DEFAULT_FUNASR_MODEL
        )

    def test_invalid_language_falls_back_to_auto(self):
        assert FunAsrTranscriber(api_key="k", language_mode="zz")._language_mode == (
            "auto"
        )

    def test_german_not_supported_falls_back_to_auto(self):
        # Fun-ASR does not support German; "de" must not be accepted as a hint.
        assert FunAsrTranscriber(api_key="k", language_mode="de")._language_mode == (
            "auto"
        )

    def test_supported_language_preserved(self):
        assert FunAsrTranscriber(api_key="k", language_mode="zh")._language_mode == (
            "zh"
        )


class TestFunAsrBatch:
    def test_transcribe_combines_finalized_sentences(self):
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "Hello", True),
            _event("result-generated", "world", True),
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            out = t.transcribe_batch(_wav_bytes())

        assert out == "Hello world"
        run = json.loads(ws.sent_text[0])
        assert run["header"]["action"] == "run-task"
        assert run["payload"]["model"] == DEFAULT_FUNASR_MODEL
        assert run["payload"]["parameters"]["format"] == "pcm"
        assert run["payload"]["parameters"]["sample_rate"] == 16000
        assert run["payload"]["parameters"]["language_hints"] == ["en"]
        finish = json.loads(ws.sent_text[-1])
        assert finish["header"]["action"] == "finish-task"
        assert ws.sent_binary  # audio frames were streamed
        assert ws.closed

    def test_auto_language_omits_hint(self):
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "ok", True),
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="auto")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            t.transcribe_batch(_wav_bytes())
        run = json.loads(ws.sent_text[0])
        assert "language_hints" not in run["payload"]["parameters"]

    def test_partial_then_final_sentence(self):
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "Hel", False),
            _event("result-generated", "Hello", True),
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            assert t.transcribe_batch(_wav_bytes()) == "Hello"

    def test_unfinished_current_sentence_still_returned(self):
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "partial only", False),
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            assert t.transcribe_batch(_wav_bytes()) == "partial only"

    def test_task_failed_raises(self):
        ws = FakeWS([_event("task-failed", error_message="bad request")])
        t = FunAsrTranscriber(api_key="sk")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            with pytest.raises(TranscriptionError, match="task failed.*bad request"):
                t.transcribe_batch(_wav_bytes())

    def test_progress_callback(self):
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "hi", True),
            _event("task-finished"),
        ])
        progress: list[str] = []
        t = FunAsrTranscriber(api_key="sk")
        t.set_progress_callback(progress.append)
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            t.transcribe_batch(_wav_bytes())
        assert progress and "Fun-ASR" in progress[0]

    def test_stereo_input_is_downmixed(self):
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "ok", True),
            _event("task-finished"),
        ])
        stereo = _wav_bytes(pcm=b"\x01\x00\x02\x00" * 800, channels=2)
        t = FunAsrTranscriber(api_key="sk")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            assert t.transcribe_batch(stereo) == "ok"
        # Mono downmix => half the bytes of the stereo frames.
        assert sum(len(b) for b in ws.sent_binary) == 800 * 2

    def test_non_wav_input_raises(self):
        t = FunAsrTranscriber(api_key="sk")
        with pytest.raises(TranscriptionError, match="WAV/PCM"):
            t.transcribe_batch(b"this is not a wav file")


class TestFunAsrConnectionTest:
    def test_connection_success(self):
        ws = FakeWS([_event("task-started")])
        t = FunAsrTranscriber(api_key="k")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            ok, msg = t.test_connection()
        assert ok is True
        assert "valid" in msg.lower()

    def test_connection_auth_failure(self):
        class Boom(Exception):
            status_code = 401

        t = FunAsrTranscriber(api_key="k")
        with patch.object(FunAsrTranscriber, "_connect", side_effect=Boom("401")):
            ok, msg = t.test_connection()
        assert ok is False
        assert "401" in msg


class TestFunAsrFactoryRouting:
    def test_factory_creates_funasr_transcriber(self):
        from stt_app.transcriber.factory import create_transcriber

        class FakeSecretStore:
            def get_api_key(self, provider: str) -> str | None:
                return "test-key" if provider == "funasr" else None

        settings = SimpleNamespace(
            engine="funasr",
            language_mode="zh",
            funasr_model="fun-asr-realtime",
        )
        t = create_transcriber(settings, secret_store=FakeSecretStore())
        assert isinstance(t, FunAsrTranscriber)
        assert t._api_key == "test-key"
        assert t._language_mode == "zh"
        assert t._model == "fun-asr-realtime"
