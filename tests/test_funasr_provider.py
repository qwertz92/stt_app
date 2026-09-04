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
    """A scripted stand-in for a `websocket-client` connection.

    `settimeout` is part of that interface and is what bounds the receive
    loop: the socket timeout has to be pulled in as the total budget runs
    out, because a server PING restarts it and is answered inside
    `recv_data_frame` without ever returning to the caller.
    """

    def __init__(self, events: list[str]):
        self._events = list(events)
        self.sent_text: list[str] = []
        self.sent_binary: list[bytes] = []
        self.timeouts: list[float] = []
        self.closed = False

    def send(self, data):
        self.sent_text.append(data)

    def send_binary(self, data):
        self.sent_binary.append(bytes(data))

    def settimeout(self, seconds):
        self.timeouts.append(seconds)

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
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError, match=r"task failed.*bad request"),
        ):
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


class TestFunAsrStreamEndsEarly:
    """Every exit other than `task-finished` used to lose what had arrived."""

    @staticmethod
    def _two_sentences():
        return [
            _event("task-started"),
            _event("result-generated", sentence_text="Erster Satz.", sentence_end=True),
            _event("result-generated", sentence_text="Zweiter Satz.", sentence_end=True),
        ]

    def _fail(self, tail_events, tail_exception=None):
        events = self._two_sentences() + list(tail_events)

        class _EndingWS(FakeWS):
            def recv(self):
                if self._events:
                    return self._events.pop(0)
                if tail_exception is not None:
                    raise tail_exception
                raise AssertionError("the loop asked for more after the end")

        ws = _EndingWS(events)
        t = FunAsrTranscriber(api_key="sk")
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError) as excinfo,
        ):
            t.transcribe_batch(_wav_bytes())
        return str(excinfo.value)

    def test_a_server_close_ends_the_loop_instead_of_spinning(self):
        """`recv()` answers "" for a CLOSE frame, not an exception.

        Treating that as "nothing yet, keep waiting" spun the loop until some
        later socket error, so a closed stream was reported as a connection
        fault -- measured at 202 receive calls after the close, still going.
        """
        message = self._fail([""])

        assert "closed the connection" in message, message
        assert "Erster Satz. Zweiter Satz." in message, message

    def test_a_dropped_connection_still_names_what_arrived(self):
        message = self._fail(
            [], tail_exception=ConnectionResetError("Connection to remote host was lost")
        )

        assert "Erster Satz. Zweiter Satz." in message, message

    def test_a_task_failure_still_names_what_arrived(self):
        message = self._fail([_event("task-failed", error_message="bad request")])

        assert "bad request" in message, message
        assert "Erster Satz. Zweiter Satz." in message, message

    def test_a_clean_finish_says_nothing_about_recovered_text(self):
        events = [*self._two_sentences(), _event("task-finished")]
        ws = FakeWS(events)
        t = FunAsrTranscriber(api_key="sk")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            text = t.transcribe_batch(_wav_bytes())

        assert text == "Erster Satz. Zweiter Satz."

    def test_a_failure_with_nothing_received_stays_plain(self):
        ws = FakeWS([_event("task-started"), ""])
        t = FunAsrTranscriber(api_key="sk")
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError) as excinfo,
        ):
            t.transcribe_batch(_wav_bytes())

        assert "Received before the failure" not in str(excinfo.value)

    def test_the_recovered_text_is_bounded(self):
        long_sentence = "wort " * 900
        events = [
            _event("task-started"),
            _event("result-generated", sentence_text=long_sentence, sentence_end=True),
            "",
        ]
        ws = FakeWS(events)
        t = FunAsrTranscriber(api_key="sk")
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError) as excinfo,
        ):
            t.transcribe_batch(_wav_bytes())

        message = str(excinfo.value)
        assert len(message) < 2400, len(message)
        assert message.endswith('..."'), message[-40:]


class TestFunAsrTotalBudget:
    """A service that keeps the socket alive without finishing."""

    def test_a_never_finishing_service_gives_the_worker_back(self, monkeypatch):
        """No budget existed: measured at 198 receive calls in 4 s.

        The receive loop holds the app's single `max_workers=1` transcription
        worker, and `ThreadPoolExecutor` joins its started workers from an
        exit handler, so an endless one also stops the process from exiting.
        """
        from stt_app.transcriber import funasr_provider as provider

        monkeypatch.setattr(provider, "FUNASR_BATCH_MAX_WAIT_S", 30.0)
        now = [0.0]
        monkeypatch.setattr(provider.time, "monotonic", lambda: now[0])

        class _PingingWS(FakeWS):
            """Never sends an event; each receive costs a little wall clock."""

            def settimeout(self, seconds):
                super().settimeout(seconds)
                now[0] += max(seconds, 0.001)

            def recv(self):
                return ""  # a keep-alive frame this loop cannot act on

        ws = _PingingWS([_event("task-started")])
        t = FunAsrTranscriber(api_key="sk")
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError) as excinfo,
        ):
            t.transcribe_batch(_wav_bytes())

        assert now[0] <= 30.0 + 5.0, f"it overran its budget: {now[0]}"
        assert "closed the connection" in str(excinfo.value) or "within" in str(
            excinfo.value
        )

    def test_the_socket_timeout_is_pulled_in_as_the_budget_runs_out(
        self, monkeypatch
    ):
        """A per-read timeout that a keep-alive restarts cannot bound anything.

        So the remaining budget is handed to the socket: the last read cannot
        outlive the deadline even if frames keep arriving.
        """
        from stt_app.transcriber import funasr_provider as provider

        monkeypatch.setattr(provider, "FUNASR_BATCH_MAX_WAIT_S", 4.0)
        now = [0.0]
        monkeypatch.setattr(provider.time, "monotonic", lambda: now[0])

        ws = FakeWS([_event("task-started"), _event("task-finished")])
        t = FunAsrTranscriber(api_key="sk", request_timeout_s=30)
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            t.transcribe_batch(_wav_bytes())
        first = ws.timeouts[0]

        now[0] = 3.5
        ws2 = FakeWS([_event("task-started"), _event("task-finished")])
        t2 = FunAsrTranscriber(api_key="sk", request_timeout_s=30)
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws2):
            t2.transcribe_batch(_wav_bytes())

        assert first == 4.0, ws.timeouts
        assert ws2.timeouts[0] == 4.0, "the deadline is set at the start of the request"
        assert ws2.timeouts[-1] == 4.0, ws2.timeouts


class TestFunAsrSentenceEnd:
    """An empty final must not throw away the partial that preceded it."""

    def test_an_empty_sentence_end_keeps_the_pending_partial(self):
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "Hallo", False),
            _event("result-generated", "", True),
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            out = t.transcribe_batch(_wav_bytes())

        assert out == "Hallo"

    def test_an_empty_sentence_end_mid_stream_keeps_the_order(self):
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "Hallo", False),
            _event("result-generated", "", True),
            _event("result-generated", "Welt.", True),
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            out = t.transcribe_batch(_wav_bytes())

        assert out == "Hallo Welt."

    def test_a_final_with_text_replaces_the_partial_it_refines(self):
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "Hallo", False),
            _event("result-generated", "Hallo Welt.", True),
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            out = t.transcribe_batch(_wav_bytes())

        assert out == "Hallo Welt."


class TestFunAsrFailureMessage:
    def test_a_vendor_failure_message_is_capped(self):
        """The HTTP providers cap a vendor's error body at 300 characters so it
        cannot fill the overlay; the WebSocket failure message had no cap."""
        from stt_app.transcriber import funasr_provider as provider

        ws = FakeWS([_event("task-failed", error_message="x" * 5000)])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError) as excinfo,
        ):
            t.transcribe_batch(_wav_bytes())
        message = str(excinfo.value)
        assert "Fun-ASR task failed: " + "x" * provider._FAILURE_DETAIL_MAX_CHARS in message
        assert "x" * (provider._FAILURE_DETAIL_MAX_CHARS + 1) not in message


class TestFunAsrFieldTypes:
    """`bool("false")` is True and `str(None)` is "None": JSON values are
    checked for their type before they are trusted, as the HTTP error reader
    already does."""

    def test_a_sentence_end_that_is_not_a_boolean_is_not_a_sentence_end(self):
        events = [
            _event("task-started"),
            _event("result-generated", "Hallo", False),
            json.dumps({
                "header": {"event": "result-generated", "task_id": "t"},
                "payload": {
                    "output": {"sentence": {"text": "", "sentence_end": "false"}}
                },
            }),
            _event("result-generated", "Hallo Welt.", True),
            _event("task-finished"),
        ]
        ws = FakeWS(events)
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            # Not "Hallo Hallo Welt.": the string closed no sentence.
            assert t.transcribe_batch(_wav_bytes()) == "Hallo Welt."

    def test_a_null_text_is_no_text(self):
        events = [
            _event("task-started"),
            _event("result-generated", "Hallo", False),
            json.dumps({
                "header": {"event": "result-generated", "task_id": "t"},
                "payload": {
                    "output": {"sentence": {"text": None, "sentence_end": True}}
                },
            }),
            _event("task-finished"),
        ]
        ws = FakeWS(events)
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            # The partial ends the sentence; the literal word "None" does not.
            assert t.transcribe_batch(_wav_bytes()) == "Hallo"


class TestFunAsrHeartbeat:
    """The vendor's server-events page: a `result-generated` whose
    `sentence.heartbeat` is true "is a heartbeat packet and can be ignored"
    (its `sentence_id` is always 0). This provider asks for heartbeats, so
    such packets are expected on a long pause; read as a sentence, one that
    carries `sentence_end` closes the pending partial early and the real
    final is then appended a second time."""

    @staticmethod
    def _sentence(fields: dict) -> str:
        return json.dumps({
            "header": {"event": "result-generated", "task_id": "t"},
            "payload": {"output": {"sentence": fields}},
        })

    @pytest.mark.parametrize(
        "heartbeat_fields",
        [
            {"text": "", "sentence_end": True, "heartbeat": True, "sentence_id": 0},
            {"text": "Hallo Welt.", "sentence_end": True, "heartbeat": True},
            {"text": "", "sentence_end": False, "heartbeat": True},
        ],
        ids=["empty-final", "text-final", "empty-partial"],
    )
    def test_a_heartbeat_packet_does_not_touch_the_transcript(self, heartbeat_fields):
        events = [
            _event("task-started"),
            _event("result-generated", "Hallo", False),
            self._sentence(heartbeat_fields),
            _event("result-generated", "Hallo Welt.", True),
            _event("task-finished"),
        ]
        ws = FakeWS(events)
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            # Not "Hallo Hallo Welt.": the heartbeat closed no sentence.
            assert t.transcribe_batch(_wav_bytes()) == "Hallo Welt."

    def test_only_a_boolean_true_marks_a_heartbeat(self):
        """Typed before trusted, like `sentence_end`: the string "false" is
        not a heartbeat, so that packet is an ordinary partial."""
        events = [
            _event("task-started"),
            _event("result-generated", "Hallo", False),
            self._sentence({"text": "Welt", "sentence_end": False, "heartbeat": "false"}),
            _event("task-finished"),
        ]
        ws = FakeWS(events)
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            assert t.transcribe_batch(_wav_bytes()) == "Welt"

    def test_a_heartbeat_packet_still_resets_the_frame_bound(self):
        """A heartbeat is the server saying it is alive; it must count as an
        event for the unusable-frame bound like any other."""
        from stt_app.transcriber import funasr_provider as provider

        limit = provider._MAX_UNUSABLE_FRAMES
        junk = ["not json"] * (limit - 1)
        events = [
            _event("task-started"),
            _event("result-generated", "Hallo", False),
            *junk,
            self._sentence({"text": "", "sentence_end": False, "heartbeat": True}),
            *junk,
            _event("result-generated", "Hallo Welt.", True),
            _event("task-finished"),
        ]
        ws = FakeWS(events)
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            assert t.transcribe_batch(_wav_bytes()) == "Hallo Welt."


class TestFunAsrUnusableFrames:
    """The receive loop skips frames that are not events; it may not spin on them."""

    def test_junk_frames_before_an_event_are_skipped(self):
        junk = [b"\x00\x01", "not json", "[1, 2]", "42"] * 50
        ws = FakeWS([
            _event("task-started"),
            *junk,
            _event("result-generated", "ok", True),
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            assert t.transcribe_batch(_wav_bytes()) == "ok"

    def test_a_flood_of_unusable_frames_fails_instead_of_pinning_a_core(self):
        from stt_app.transcriber import funasr_provider as provider

        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "so far", True),
            *(["not json"] * (provider._MAX_UNUSABLE_FRAMES + 1)),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError) as excinfo,
        ):
            t.transcribe_batch(_wav_bytes())

        message = str(excinfo.value)
        assert "frames" in message
        assert "so far" in message, "the text already received was discarded"
        assert ws.closed

    @pytest.mark.parametrize(
        "filler",
        [
            "{}",
            json.dumps({"header": {"event": "ping"}}),
        ],
        ids=["empty-object", "unknown-event"],
    )
    def test_an_object_that_is_not_an_event_does_not_reset_the_bound(self, filler):
        """The count lived inside `_recv_event` and restarted on every JSON
        object it returned; `_collect_transcript` then discarded objects that
        were not transcript events. One `{}` after every thousand junk frames
        therefore never tripped the bound and the loop spun for the whole
        thirty-minute budget (measured: 4,101,846 receive calls in 2 s)."""
        from stt_app.transcriber import funasr_provider as provider

        junk = ["not json"] * provider._MAX_UNUSABLE_FRAMES
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "so far", True),
            *junk,
            filler,
            *junk,
            filler,
            *junk,
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError) as excinfo,
        ):
            t.transcribe_batch(_wav_bytes())

        message = str(excinfo.value)
        assert "frames" in message, message
        assert "so far" in message
        # It tripped inside the second block, not after all three.
        assert len(ws._events) > 0

    def test_a_flood_of_empty_objects_fails_too(self):
        from stt_app.transcriber import funasr_provider as provider

        ws = FakeWS([
            _event("task-started"),
            *(["{}"] * (provider._MAX_UNUSABLE_FRAMES + 1)),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError) as excinfo,
        ):
            t.transcribe_batch(_wav_bytes())
        assert "frames" in str(excinfo.value)

    def test_a_real_event_resets_the_bound(self):
        from stt_app.transcriber import funasr_provider as provider

        junk = ["not json"] * (provider._MAX_UNUSABLE_FRAMES - 1)
        ws = FakeWS([
            _event("task-started"),
            *junk,
            _event("result-generated", "eins", False),
            *junk,
            _event("result-generated", "eins zwei", True),
            *junk,
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            assert t.transcribe_batch(_wav_bytes()) == "eins zwei"

    @pytest.mark.parametrize("terminal", ["task-finished", "result-generated"])
    def test_an_event_as_the_frame_after_the_bound_is_honoured(self, terminal):
        """The budget was spent one statement before the frame was looked
        at, so exactly `_MAX_UNUSABLE_FRAMES` junk frames followed by a real
        event discarded that event: a `task-finished` there was reported as
        "1001 frames in a row that were not events"."""
        from stt_app.transcriber import funasr_provider as provider

        junk = ["not json"] * provider._MAX_UNUSABLE_FRAMES
        if terminal == "task-finished":
            tail = [_event("task-finished")]
        else:
            tail = [_event("result-generated", "zwei", True), _event("task-finished")]
        events = [
            _event("task-started"),
            _event("result-generated", "eins", False),
            *junk,
            *tail,
        ]
        ws = FakeWS(events)
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            # A final with text replaces the partial of the same sentence; an
            # empty end (`task-finished` alone) keeps the last partial.
            expected = "eins" if terminal == "task-finished" else "zwei"
            assert t.transcribe_batch(_wav_bytes()) == expected

    @pytest.mark.parametrize(
        "frame",
        [b"\x00\x01", "[1, 2]", "42", "not json"],
        ids=["binary", "json-list", "json-number", "not-json"],
    )
    def test_every_kind_of_unusable_frame_is_counted(self, frame):
        """Each of the reader's three skip arms spends the budget, and the
        transcript loop's own skip does too: a flood of any one kind fails
        one frame past the bound and succeeds at the bound."""
        from stt_app.transcriber import funasr_provider as provider

        limit = provider._MAX_UNUSABLE_FRAMES
        at_the_bound = [_event("task-started"), *([frame] * limit), _event("task-finished")]
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=FakeWS(at_the_bound)):
            assert t.transcribe_batch(_wav_bytes()) == ""

        past_the_bound = [_event("task-started"), *([frame] * (limit + 1)), _event("task-finished")]
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=FakeWS(past_the_bound)),
            pytest.raises(TranscriptionError) as excinfo,
        ):
            t.transcribe_batch(_wav_bytes())
        assert f"{limit + 1} frames" in str(excinfo.value)

    def test_one_junk_frame_past_the_bound_still_fails(self):
        from stt_app.transcriber import funasr_provider as provider

        events = [
            _event("task-started"),
            *(["not json"] * (provider._MAX_UNUSABLE_FRAMES + 1)),
            _event("task-finished"),
        ]
        ws = FakeWS(events)
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError) as excinfo,
        ):
            t.transcribe_batch(_wav_bytes())
        assert f"{provider._MAX_UNUSABLE_FRAMES + 1} frames" in str(excinfo.value)

    def test_a_shutdown_ends_the_receive_loop(self):
        from stt_app.transcriber.base import (
            request_transcription_shutdown,
            reset_transcription_shutdown_for_tests,
        )

        class _ShutdownOnFirstRecv(FakeWS):
            def recv(self):
                request_transcription_shutdown()
                return super().recv()

        ws = _ShutdownOnFirstRecv([
            _event("task-started"),
            _event("result-generated", "so far", True),
            *(["not json"] * 50),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        try:
            with (
                patch.object(FunAsrTranscriber, "_connect", return_value=ws),
                pytest.raises(TranscriptionError) as excinfo,
            ):
                t.transcribe_batch(_wav_bytes())
        finally:
            reset_transcription_shutdown_for_tests()
        assert "shutting down" in str(excinfo.value)
        assert len(ws._events) > 40, "it went on reading after the shutdown"

    def test_the_budget_is_re_read_on_every_frame(self, monkeypatch):
        """A `remaining` computed once outside the loop is the hang B3 measured."""
        from stt_app.transcriber import funasr_provider as provider

        now = [0.0]

        def _monotonic():
            now[0] += 1.0
            return now[0]

        monkeypatch.setattr(provider.time, "monotonic", _monotonic)
        monkeypatch.setattr(provider, "FUNASR_BATCH_MAX_WAIT_S", 5.0)
        ws = FakeWS([
            _event("task-started"),
            *(["not json"] * 20),
            _event("result-generated", "late", True),
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with (
            patch.object(FunAsrTranscriber, "_connect", return_value=ws),
            pytest.raises(TranscriptionError, match="did not finish"),
        ):
            t.transcribe_batch(_wav_bytes())


class TestFunAsrRunTask:
    def test_the_run_task_asks_for_the_documented_heartbeat(self):
        ws = FakeWS([
            _event("task-started"),
            _event("result-generated", "ok", True),
            _event("task-finished"),
        ])
        t = FunAsrTranscriber(api_key="sk", language_mode="en")
        with patch.object(FunAsrTranscriber, "_connect", return_value=ws):
            t.transcribe_batch(_wav_bytes())

        run = json.loads(ws.sent_text[0])
        assert run["payload"]["parameters"]["heartbeat"] is True
