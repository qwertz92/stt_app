"""Meta Muse Voice Transcribe batch provider: the request it sends and how it
reads the answer, against a fake `urlopen`. Nothing here reaches the network."""

from __future__ import annotations

import io
import json
import wave

import numpy as np
import pytest
from _rest_fakes import (
    _parts,
    fake_response,
    http_error,
    json_response,
    seconds_of,
    sent_fields,
    sent_file,
    wav_seconds,
)

from stt_app.config import (
    DEFAULT_META_MODEL,
    META_LANGUAGE_MODES,
    META_LANGUAGE_NAMES,
    META_MODELS,
    STREAMING_ENGINES,
    VALID_ENGINES,
    language_modes_for_selection,
    remote_batch_part_limit,
)
from stt_app.transcriber import meta_provider as provider_module
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.meta_provider import MetaTranscriber

_URL = "https://api.meta.ai/v1/asr/transcribe"
_KEY = "meta-secret-key-123"


class _Recorder:
    def __init__(self, answer=None):
        self.answer = (
            answer
            if answer is not None
            else json_response({"sessionId": "s1", "transcript": " Hallo Welt. "})
        )
        self.requests = []

    def __call__(self, request, timeout=None, context=None, **_kwargs):
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def recorder(monkeypatch):
    fake = _Recorder()
    monkeypatch.setattr(provider_module, "open_request", fake)
    return fake


def _settings_part(request) -> dict:
    return json.loads(dict(sent_fields(request))["request"])


def _wav(seconds: float, *, rate: int, channels: int = 1, width: int = 2) -> bytes:
    frames = int(seconds * rate)
    tone = np.sin(np.arange(frames) / 12.0) * 6000.0
    if width == 2:
        data = np.repeat(tone.astype("<i2"), channels).tobytes()
    else:
        data = np.repeat((tone * 256).astype("<i4"), channels).tobytes()[
            : frames * channels * 3
        ]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(data)
    return buffer.getvalue()


def test_a_recording_is_sent_as_a_json_request_part_and_a_wav_audio_part(recorder):
    audio = wav_seconds(1.0)
    text = MetaTranscriber(_KEY).transcribe_batch(audio)

    assert text == "Hallo Welt."
    (request,) = recorder.requests
    assert request.full_url == _URL
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {_KEY}"
    assert request.get_header("Accept") == "application/json"
    heads = [head for head, _body in _parts(request)]
    assert 'name="request"' in heads[0]
    assert "Content-Type: application/json" in heads[0]
    assert 'name="audio"' in heads[1]
    assert "Content-Type: audio/wav" in heads[1]
    assert _settings_part(request) == {
        "model": DEFAULT_META_MODEL,
        "mode": "PUSH_TO_TALK",
        "audioEncoding": "WAV",
    }
    name, filename, body = sent_file(request)
    assert (name, filename) == ("audio", "audio.wav")
    # A 16 kHz mono 16-bit WAV is what the vendor takes: sent byte for byte.
    assert body == audio


def test_the_one_model_is_the_documented_id():
    assert META_MODELS == ("muse-voice-transcribe-1.0",)
    assert DEFAULT_META_MODEL == "muse-voice-transcribe-1.0"


def test_a_chosen_language_is_sent_as_its_english_name_and_auto_is_not(recorder):
    MetaTranscriber("k", language_mode="de").transcribe_batch(wav_seconds(1.0))
    MetaTranscriber("k", language_mode="zh").transcribe_batch(wav_seconds(1.0))
    MetaTranscriber("k", language_mode="auto").transcribe_batch(wav_seconds(1.0))
    assert _settings_part(recorder.requests[0])["languageBias"] == ["German"]
    assert _settings_part(recorder.requests[1])["languageBias"] == ["Mandarin Chinese"]
    assert "languageBias" not in _settings_part(recorder.requests[2])


def test_the_twenty_five_documented_languages_are_offered():
    assert META_LANGUAGE_MODES[0] == "auto"
    assert len(META_LANGUAGE_NAMES) == 25
    assert META_LANGUAGE_MODES[1:] == tuple(META_LANGUAGE_NAMES)
    assert language_modes_for_selection("meta") == META_LANGUAGE_MODES
    assert {"de", "en", "fr", "he", "tl"} <= set(META_LANGUAGE_MODES)
    assert "uk" not in META_LANGUAGE_MODES


def test_a_language_meta_does_not_offer_is_sent_as_auto(recorder):
    MetaTranscriber("k", language_mode="uk").transcribe_batch(wav_seconds(1.0))
    assert "languageBias" not in _settings_part(recorder.requests[0])


def test_the_vocabulary_is_the_keywords_list(recorder):
    MetaTranscriber("k", custom_vocabulary="Kubernetes; Splunk SOAR").transcribe_batch(
        wav_seconds(1.0)
    )
    assert _settings_part(recorder.requests[0])["keywords"] == [
        "Kubernetes",
        "Splunk SOAR",
    ]


def test_no_vocabulary_sends_no_keywords_field(recorder):
    MetaTranscriber("k").transcribe_batch(wav_seconds(1.0))
    assert "keywords" not in _settings_part(recorder.requests[0])


def test_an_import_in_another_format_is_converted_to_what_the_vendor_takes(recorder):
    # 48 kHz stereo is refused by Meta ("mono, 16 kHz or 24 kHz"); it goes out
    # as a 16 kHz mono mix of the same length.
    MetaTranscriber("k").transcribe_batch(_wav(2.0, rate=48_000, channels=2))
    _name, _filename, body = sent_file(recorder.requests[0])
    with wave.open(io.BytesIO(body), "rb") as handle:
        assert (handle.getnchannels(), handle.getsampwidth()) == (1, 2)
        assert handle.getframerate() == 16_000
    assert seconds_of(body) == pytest.approx(2.0, abs=0.01)


def test_a_24_khz_mono_recording_is_sent_as_it_is(recorder):
    audio = _wav(1.0, rate=24_000)
    MetaTranscriber("k").transcribe_batch(audio)
    assert sent_file(recorder.requests[0])[2] == audio


def test_audio_that_is_not_a_wav_is_refused_before_sending(recorder, tmp_path):
    path = tmp_path / "talk.mp3"
    path.write_bytes(b"ID3\x04\x00 not really audio")
    with pytest.raises(TranscriptionError, match=r"WAV.*talk\.mp3|talk\.mp3.*WAV"):
        MetaTranscriber("k").transcribe_batch(path)
    assert recorder.requests == []


def test_an_empty_transcript_is_a_valid_answer(recorder):
    recorder.answer = json_response({"sessionId": "s", "transcript": ""})
    assert MetaTranscriber("k").transcribe_batch(wav_seconds(1.0)) == ""


def test_an_answer_without_a_transcript_is_an_error_not_silence(recorder):
    recorder.answer = json_response({"sessionId": "s", "turns": []})
    with pytest.raises(TranscriptionError, match="no 'transcript'"):
        MetaTranscriber("k").transcribe_batch(wav_seconds(1.0))


def test_an_html_page_is_never_a_transcript(recorder):
    recorder.answer = fake_response("<!DOCTYPE html><html>sign in</html>")
    with pytest.raises(TranscriptionError, match="HTML"):
        MetaTranscriber("k").transcribe_batch(wav_seconds(1.0))


def test_a_plain_text_answer_is_an_error_naming_it(recorder):
    recorder.answer = fake_response("Internal Server Error")
    with pytest.raises(TranscriptionError, match=r"not JSON.*Internal Server Error"):
        MetaTranscriber("k").transcribe_batch(wav_seconds(1.0))


def test_an_invalid_key_says_so_without_the_key(recorder):
    recorder.answer = http_error(
        _URL,
        401,
        json.dumps(
            {
                "sessionId": "voyager/transcribe/ABC",
                "error": {"code": "invalid_api_key", "message": "Unauthorized"},
            }
        ),
    )
    with pytest.raises(TranscriptionError, match="Authentication failed") as caught:
        MetaTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))
    assert _KEY not in str(caught.value)


def test_a_rate_limit_says_to_wait(recorder):
    recorder.answer = http_error(_URL, 429, "{}")
    with pytest.raises(TranscriptionError, match=r"Rate limit exceeded \(HTTP 429\)"):
        MetaTranscriber("k").transcribe_batch(wav_seconds(1.0))


def test_a_server_error_shows_the_vendors_message(recorder):
    recorder.answer = http_error(
        _URL, 500, json.dumps({"error": {"message": "processing budget exceeded"}})
    )
    with pytest.raises(
        TranscriptionError, match=r"HTTP 500.*processing budget exceeded"
    ):
        MetaTranscriber("k").transcribe_batch(wav_seconds(1.0))


def test_an_error_body_that_echoes_the_key_does_not_show_it(recorder):
    recorder.answer = http_error(
        _URL, 400, json.dumps({"error": {"message": f"bad token {_KEY}"}})
    )
    with pytest.raises(TranscriptionError) as caught:
        MetaTranscriber(_KEY).transcribe_batch(wav_seconds(1.0))
    assert _KEY not in str(caught.value)
    assert "HTTP 400" in str(caught.value)


def test_a_long_error_body_is_cut(recorder):
    recorder.answer = http_error(_URL, 400, "x" * 5000)
    with pytest.raises(TranscriptionError) as caught:
        MetaTranscriber("k").transcribe_batch(wav_seconds(1.0))
    assert len(str(caught.value)) < 450


def test_a_proxy_html_error_page_is_named_not_pasted(recorder):
    recorder.answer = http_error(
        _URL,
        403,
        "<html><head><title>Access Denied</title></head><body>x</body></html>",
    )
    with pytest.raises(TranscriptionError, match=r"HTML page titled \"Access Denied\""):
        MetaTranscriber("k").transcribe_batch(wav_seconds(1.0))


def test_a_missing_key_is_refused():
    with pytest.raises(TranscriptionError, match="API key is missing"):
        MetaTranscriber("")


def test_the_part_limit_is_inside_the_documented_one():
    limit = remote_batch_part_limit("meta", DEFAULT_META_MODEL)
    assert limit is not None
    assert limit.seconds <= 600.0
    assert limit.max_bytes <= 32_000_000


def test_meta_is_batch_only():
    assert "meta" in VALID_ENGINES
    assert "meta" not in STREAMING_ENGINES
    with pytest.raises(NotImplementedError):
        MetaTranscriber("k").start_stream()


def test_a_recording_past_ten_minutes_goes_out_in_parts_within_the_limit(recorder):
    recorder.answer = json_response({"transcript": "part"})
    limit = remote_batch_part_limit("meta", DEFAULT_META_MODEL)

    text = MetaTranscriber("k").transcribe_batch(wav_seconds(1300.0))

    assert len(recorder.requests) == 3
    assert text == "part part part"
    for request in recorder.requests:
        body = sent_file(request)[2]
        assert seconds_of(body) <= limit.seconds
        assert len(body) <= limit.max_bytes


def test_the_silence_threshold_reaches_the_part_splitter(recorder, monkeypatch):
    seen = {}

    def fake_parts(audio, request_fn, **kwargs):
        seen.update(kwargs)
        return request_fn(audio, kwargs["progress_text"])

    monkeypatch.setattr(provider_module, "transcribe_in_parts", fake_parts)
    MetaTranscriber("k", silence_gate_threshold=0.03).transcribe_batch(wav_seconds(1.0))
    assert seen["silence_threshold"] == 0.03
    assert seen["limit"] == remote_batch_part_limit("meta", DEFAULT_META_MODEL)


def test_the_connection_test_sends_one_second_of_silence(recorder):
    recorder.answer = json_response({"sessionId": "s", "transcript": ""})
    ok, message = MetaTranscriber("k").test_connection()
    assert ok, message
    (request,) = recorder.requests
    assert request.full_url == _URL
    body = sent_file(request)[2]
    assert seconds_of(body) == pytest.approx(1.0, abs=0.01)
    assert not np.frombuffer(body[44:], dtype="<i2").any()


def test_the_connection_test_reports_an_invalid_key(recorder):
    recorder.answer = http_error(_URL, 401, "{}")
    ok, message = MetaTranscriber("k").test_connection()
    assert not ok
    assert "401" in message
