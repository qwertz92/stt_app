"""Speechmatics batch provider: the job it submits, the poll, the transcript
fetch and the errors on the way, against a fake `urlopen` that routes by URL.

Nothing here reaches the network; the request shapes follow the vendor's own
pages (read 2026-09-27 and 2026-10-01), which `docs/agents/remote-providers.md`
quotes.
"""

from __future__ import annotations

import json

import pytest
from _rest_fakes import (
    fake_response,
    http_error,
    json_response,
    seconds_of,
    sent_fields,
    sent_file,
    wav_seconds,
)

from stt_app.config import (
    DEFAULT_SPEECHMATICS_MODEL,
    DEFAULT_SPEECHMATICS_REGION,
    SPEECHMATICS_API_HOSTS,
    SPEECHMATICS_LANGUAGE_MODES,
    SPEECHMATICS_MELIA_MODEL,
    SPEECHMATICS_MODELS,
    language_modes_for_selection,
    remote_batch_part_limit,
)
from stt_app.transcriber import speechmatics_provider as provider_module
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.speechmatics_provider import SpeechmaticsTranscriber

_JOB_ID = "job-123"


class _FakeService:
    """Answers the three routes the provider uses, and records every request."""

    def __init__(self, statuses=("running", "done"), transcript="Hallo Welt.\n"):
        self.statuses = list(statuses)
        self.transcript = transcript
        self.requests = []
        self.job_errors: list[dict] = []
        self.submit_error = None
        self.status_errors: list[Exception] = []
        self.transcript_errors: list[Exception] = []
        self.list_answer = None

    def __call__(self, request, timeout=None, context=None):
        self.requests.append(request)
        url = request.full_url
        method = request.get_method()
        if method == "POST":
            if self.submit_error is not None:
                raise self.submit_error
            return json_response({"id": _JOB_ID}, status=201)
        if url.endswith("/transcript?format=txt"):
            if self.transcript_errors:
                raise self.transcript_errors.pop(0)
            return fake_response(self.transcript, headers={"Content-Type": "text/plain"})
        if f"/jobs/{_JOB_ID}" in url:
            if self.status_errors:
                raise self.status_errors.pop(0)
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            job = {"id": _JOB_ID, "status": status}
            if self.job_errors:
                job["errors"] = self.job_errors
            return json_response({"job": job})
        if self.list_answer is not None:
            if isinstance(self.list_answer, Exception):
                raise self.list_answer
            return self.list_answer
        return json_response({"jobs": []})

    def submitted(self):
        return [r for r in self.requests if r.get_method() == "POST"]

    def config(self) -> dict:
        (submit,) = self.submitted()
        fields = dict(sent_fields(submit))
        return json.loads(fields["config"])


@pytest.fixture
def service(monkeypatch):
    fake = _FakeService()
    monkeypatch.setattr(provider_module.urllib.request, "urlopen", fake)
    return fake


def _transcriber(**kwargs) -> SpeechmaticsTranscriber:
    kwargs.setdefault("poll_interval_s", 0.0)
    return SpeechmaticsTranscriber("sm-key", **kwargs)


class TestSpeechmaticsJob:
    def test_a_recording_is_submitted_polled_and_its_text_fetched(self, service):
        text = _transcriber().transcribe_batch(wav_seconds(1.0))

        assert text == "Hallo Welt."
        submit = service.submitted()[0]
        host = SPEECHMATICS_API_HOSTS[DEFAULT_SPEECHMATICS_REGION]
        assert submit.full_url == f"https://{host}/v2/jobs/"
        assert submit.get_header("Authorization") == "Bearer sm-key"
        name, filename, body = sent_file(submit)
        assert (name, filename) == ("data_file", "audio.wav")
        assert seconds_of(body) == pytest.approx(1.0)
        urls = [r.full_url for r in service.requests[1:]]
        assert urls[-1] == f"https://{host}/v2/jobs/{_JOB_ID}/transcript?format=txt"
        assert all(f"https://{host}/v2/jobs/{_JOB_ID}" in url for url in urls)

    def test_the_default_model_is_melia_and_the_default_region_is_the_eu(self):
        # Melia is the one model that transcribes without a language chosen
        # (the app's default is Auto), and eu1 is a region every model runs in.
        assert DEFAULT_SPEECHMATICS_MODEL == SPEECHMATICS_MELIA_MODEL
        assert DEFAULT_SPEECHMATICS_REGION == "eu1"
        assert SPEECHMATICS_API_HOSTS == {
            "eu1": "eu1.asr.api.speechmatics.com",
            "us1": "us1.asr.api.speechmatics.com",
            "au1": "au1.asr.api.speechmatics.com",
        }

    @pytest.mark.parametrize("region", ["us1", "au1"])
    def test_the_region_picks_the_host(self, service, region):
        _transcriber(region=region, model="enhanced", language_mode="en").transcribe_batch(
            wav_seconds(1.0)
        )
        assert {r.host for r in service.requests} == {SPEECHMATICS_API_HOSTS[region]}

    def test_an_unknown_region_is_the_default(self, service):
        _transcriber(region="mars").transcribe_batch(wav_seconds(1.0))
        assert {r.host for r in service.requests} == {
            SPEECHMATICS_API_HOSTS[DEFAULT_SPEECHMATICS_REGION]
        }

    def test_melia_is_refused_in_australia(self):
        # "Melia 1 is available for Batch transcription in the EU and US
        # regions only": a job sent there could only be rejected.
        with pytest.raises(TranscriptionError, match="Melia"):
            _transcriber(model=SPEECHMATICS_MELIA_MODEL, region="au1")

    def test_a_missing_key_is_refused(self):
        with pytest.raises(TranscriptionError, match="API key is missing"):
            SpeechmaticsTranscriber("")


class TestSpeechmaticsConfig:
    def test_melia_with_auto_sends_multi_and_no_hint(self, service):
        _transcriber(model="melia-1", language_mode="auto").transcribe_batch(
            wav_seconds(1.0)
        )
        assert service.config() == {
            "type": "transcription",
            "transcription_config": {"model": "melia-1", "language": "multi"},
        }

    def test_melia_with_a_language_sends_it_as_a_hint(self, service):
        _transcriber(model="melia-1", language_mode="zh").transcribe_batch(
            wav_seconds(1.0)
        )
        assert service.config()["transcription_config"] == {
            "model": "melia-1",
            "language": "multi",
            "language_hints": ["cmn"],
        }

    def test_melia_sends_no_vocabulary(self, service):
        # "Custom Dictionary: ... Melia-1 does 'Not yet.'"
        _transcriber(model="melia-1", custom_vocabulary="Kubernetes").transcribe_batch(
            wav_seconds(1.0)
        )
        assert "additional_vocab" not in service.config()["transcription_config"]

    @pytest.mark.parametrize("model", ["enhanced", "standard"])
    def test_a_language_model_sends_its_code_and_the_vocabulary(self, service, model):
        _transcriber(
            model=model, language_mode="de", custom_vocabulary="Kubernetes, Grafana"
        ).transcribe_batch(wav_seconds(1.0))
        assert service.config()["transcription_config"] == {
            "model": model,
            "language": "de",
            "additional_vocab": [{"content": "Kubernetes"}, {"content": "Grafana"}],
        }

    def test_chinese_is_sent_as_mandarin(self, service):
        _transcriber(model="enhanced", language_mode="zh").transcribe_batch(
            wav_seconds(1.0)
        )
        assert service.config()["transcription_config"]["language"] == "cmn"

    @pytest.mark.parametrize("model", ["enhanced", "standard"])
    def test_auto_is_not_offered_where_a_language_is_required(self, model):
        # Automatic identification needs "at least 60 seconds of speech" and
        # rejects the job by default otherwise -- the length of most
        # dictations -- so the two language models take a chosen language.
        modes = language_modes_for_selection("speechmatics", model)
        assert "auto" not in modes
        assert modes == SPEECHMATICS_LANGUAGE_MODES

    def test_melia_offers_auto_and_every_language(self):
        modes = language_modes_for_selection("speechmatics", SPEECHMATICS_MELIA_MODEL)
        assert modes == ("auto", *SPEECHMATICS_LANGUAGE_MODES)
        assert language_modes_for_selection("speechmatics") == modes

    def test_a_stored_auto_reaching_a_language_model_falls_back_to_german(self, service):
        _transcriber(model="enhanced", language_mode="auto").transcribe_batch(
            wav_seconds(1.0)
        )
        assert service.config()["transcription_config"]["language"] == "de"

    def test_an_unknown_model_is_the_default(self):
        assert _transcriber(model="nope")._model == DEFAULT_SPEECHMATICS_MODEL
        assert set(SPEECHMATICS_MODELS) == {"melia-1", "enhanced", "standard"}


class TestSpeechmaticsErrors:
    def test_a_rejected_job_names_its_status_reason_and_id(self, service):
        service.statuses = ["rejected"]
        service.job_errors = [{"timestamp": "t", "message": "Audio fetch error"}]
        with pytest.raises(TranscriptionError) as caught:
            _transcriber().transcribe_batch(wav_seconds(1.0))
        message = str(caught.value)
        assert "rejected" in message
        assert "Audio fetch error" in message
        assert _JOB_ID in message

    def test_a_rejected_submit_says_what_the_service_said(self, service):
        service.submit_error = http_error(
            "https://x/v2/jobs/",
            400,
            json.dumps({"code": 400, "error": "Job rejected", "detail": "bad config"}),
        )
        with pytest.raises(TranscriptionError, match=r"HTTP 400.*bad config"):
            _transcriber().transcribe_batch(wav_seconds(1.0))

    def test_an_invalid_key_fails_at_once(self, service):
        service.submit_error = http_error("https://x/v2/jobs/", 401)
        with pytest.raises(TranscriptionError, match="Authentication failed"):
            _transcriber().transcribe_batch(wav_seconds(1.0))

    def test_one_failed_status_fetch_does_not_abort_the_wait(self, service):
        service.status_errors = [http_error("https://x", 503)]
        assert _transcriber().transcribe_batch(wav_seconds(1.0)) == "Hallo Welt."

    @pytest.mark.parametrize("code", [401, 404, 410])
    def test_a_status_answer_that_cannot_change_ends_the_wait(self, service, code):
        service.status_errors = [http_error("https://x", code)] * 5
        with pytest.raises(TranscriptionError, match=_JOB_ID) as caught:
            _transcriber().transcribe_batch(wav_seconds(1.0))
        assert f"HTTP {code}" in str(caught.value)
        polls = [r for r in service.requests if r.full_url.endswith(_JOB_ID)]
        assert len(polls) == 1

    def test_a_failed_transcript_fetch_is_retried(self, service):
        service.transcript_errors = [http_error("https://x", 500)]
        assert _transcriber().transcribe_batch(wav_seconds(1.0)) == "Hallo Welt."

    def test_an_expired_transcript_names_the_job(self, service):
        service.transcript_errors = [http_error("https://x", 410)] * 5
        with pytest.raises(TranscriptionError, match=_JOB_ID):
            _transcriber().transcribe_batch(wav_seconds(1.0))

    def test_a_job_that_never_finishes_is_bounded(self, service):
        service.statuses = ["running"]
        with pytest.raises(TranscriptionError, match="did not finish") as caught:
            _transcriber(max_wait_s=0.0).transcribe_batch(wav_seconds(1.0))
        assert _JOB_ID in str(caught.value)

    def test_a_submit_answer_without_an_id_fails_at_once(self, service, monkeypatch):
        def no_id(request, timeout=None, context=None):
            service.requests.append(request)
            return json_response({"status": "ok"}, status=201)

        monkeypatch.setattr(provider_module.urllib.request, "urlopen", no_id)
        with pytest.raises(TranscriptionError, match="no job id"):
            _transcriber().transcribe_batch(wav_seconds(1.0))
        assert len(service.requests) == 1


class TestSpeechmaticsParts:
    def test_the_part_limit_stays_under_one_gigabyte(self):
        limit = remote_batch_part_limit("speechmatics", SPEECHMATICS_MELIA_MODEL)
        assert limit is not None
        assert limit.max_bytes < 1_000_000_000
        assert limit.seconds == 1800.0

    def test_a_long_recording_is_sent_in_parts_with_the_threshold(
        self, service, monkeypatch
    ):
        seen = {}

        def fake_parts(audio, request_fn, **kwargs):
            seen.update(kwargs)
            return request_fn(audio, kwargs["progress_text"])

        monkeypatch.setattr(provider_module, "transcribe_in_parts", fake_parts)
        _transcriber(silence_gate_threshold=0.02).transcribe_batch(wav_seconds(1.0))
        assert seen["silence_threshold"] == 0.02
        assert seen["limit"] == remote_batch_part_limit(
            "speechmatics", SPEECHMATICS_MELIA_MODEL
        )


class TestSpeechmaticsConnection:
    def test_the_connection_test_lists_jobs_in_the_region(self, service):
        ok, message = _transcriber(region="us1").test_connection()
        assert ok, message
        (request,) = service.requests
        assert request.get_method() == "GET"
        assert request.full_url == "https://us1.asr.api.speechmatics.com/v2/jobs/"
        assert request.get_header("Authorization") == "Bearer sm-key"

    def test_an_invalid_key_reads_as_one(self, service):
        service.list_answer = http_error("https://x", 401)
        ok, message = _transcriber().test_connection()
        assert not ok
        assert "401" in message
