"""Soniox batch provider: the four calls it makes, the poll, the clean-up and the
errors on the way, against a fake `open_request` that routes by method and URL.

Nothing here reaches the network; the request shapes follow Soniox's own pages
and OpenAPI schema (read 2026-11-26), which `docs/agents/remote-providers.md`
lists.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest
from _rest_fakes import (
    fake_response,
    http_error,
    json_response,
    seconds_of,
    sent_file,
    wav_seconds,
)

from stt_app.config import (
    DEFAULT_SONIOX_MODEL,
    DEFAULT_SONIOX_REGION,
    SONIOX_API_HOSTS,
    SONIOX_CONTEXT_MAX_CHARS,
    SONIOX_LANGUAGE_MODES,
    SONIOX_MODELS,
    VALID_LANGUAGE_MODES,
    RemotePartLimit,
    language_modes_for_selection,
    remote_batch_part_limit,
)
from stt_app.transcriber import _job_poll
from stt_app.transcriber import soniox_provider as provider_module
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.soniox_provider import SonioxTranscriber, fit_terms

_FILE_ID = "11111111-aaaa-4bbb-8ccc-000000000001"
_JOB_ID = "22222222-aaaa-4bbb-8ccc-000000000002"
_KEY = "sx-live-0123456789abcdef"


def join_cleanup(timeout_s: float = 5.0) -> None:
    """Wait for the deletions, which run on a daemon thread after the text."""
    for thread in threading.enumerate():
        if thread.name == "stt_app_soniox_cleanup":
            thread.join(timeout=timeout_s)


class _FakeService:
    """Answers the routes the provider uses and records every request."""

    def __init__(
        self, statuses=("queued", "processing", "completed"), text="Hallo Welt."
    ):
        self.statuses = list(statuses)
        self.text = text
        self.requests = []
        self.job = {}
        self.errors: dict[str, list[Exception]] = {}

    def _raise_queued(self, route: str) -> None:
        queue = self.errors.get(route)
        if queue:
            raise queue.pop(0)

    def __call__(self, request, timeout=None, **_kwargs):
        self.requests.append(request)
        path = request.full_url.split("/v1", 1)[1]
        method = request.get_method()
        route = f"{method} {path}"
        if method == "POST" and path == "/files":
            self._raise_queued("upload")
            return json_response({"id": _FILE_ID, "filename": "audio.wav"}, 201)
        if method == "POST" and path == "/transcriptions":
            self._raise_queued("create")
            return json_response({"id": _JOB_ID, "status": "queued"}, 201)
        if method == "GET" and path == f"/transcriptions/{_JOB_ID}":
            self._raise_queued("status")
            status = (
                self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            )
            return json_response({"id": _JOB_ID, "status": status, **self.job})
        if method == "GET" and path == f"/transcriptions/{_JOB_ID}/transcript":
            self._raise_queued("transcript")
            return json_response({"id": _JOB_ID, "text": self.text, "tokens": []})
        if method == "GET" and path == "/models":
            self._raise_queued("models")
            return json_response({"models": [{"id": "stt-async-v6"}]})
        if method == "DELETE":
            self._raise_queued("delete")
            return fake_response(b"", 204)
        raise AssertionError(f"unexpected request {route}")

    def routes(self) -> list[str]:
        join_cleanup()
        return self.routes_now()

    def routes_now(self) -> list[str]:
        """The routes seen so far, without waiting for the cleanup thread."""
        return [
            f"{r.get_method()} {r.full_url.split('/v1', 1)[1]}" for r in self.requests
        ]

    def created(self) -> dict:
        (create,) = [r for r in self.requests if r.full_url.endswith("/transcriptions")]
        return json.loads(create.data)


@pytest.fixture
def service(monkeypatch):
    fake = _FakeService()
    monkeypatch.setattr(provider_module, "open_request", fake)
    return fake


def _transcriber(**kwargs) -> SonioxTranscriber:
    kwargs.setdefault("poll_interval_s", 0.0)
    return SonioxTranscriber(_KEY, **kwargs)


class TestSonioxBatch:
    def test_a_recording_goes_through_upload_job_poll_transcript_and_deletion(
        self, service
    ):
        text = _transcriber().transcribe_batch(wav_seconds(1.0))

        assert text == "Hallo Welt."
        assert service.routes() == [
            "POST /files",
            "POST /transcriptions",
            "GET /transcriptions/" + _JOB_ID,
            "GET /transcriptions/" + _JOB_ID,
            "GET /transcriptions/" + _JOB_ID,
            f"GET /transcriptions/{_JOB_ID}/transcript",
            f"DELETE /transcriptions/{_JOB_ID}",
            f"DELETE /files/{_FILE_ID}",
        ]
        upload = service.requests[0]
        host = SONIOX_API_HOSTS[DEFAULT_SONIOX_REGION]
        assert upload.full_url == f"https://{host}/v1/files"
        assert upload.get_header("Authorization") == f"Bearer {_KEY}"
        name, filename, body = sent_file(upload)
        assert (name, filename) == ("file", "audio.wav")
        assert seconds_of(body) == pytest.approx(1.0)
        assert {r.host for r in service.requests} == {host}
        assert all(_KEY not in r.full_url for r in service.requests)

    def test_the_job_names_the_model_and_the_uploaded_file_and_nothing_else_by_default(
        self, service
    ):
        _transcriber().transcribe_batch(wav_seconds(1.0))
        assert service.created() == {"model": "stt-async-v6", "file_id": _FILE_ID}
        create = [r for r in service.requests if r.full_url.endswith("/transcriptions")]
        assert create[0].get_header("Content-type") == "application/json"

    def test_a_chosen_language_is_a_strict_hint_and_auto_sends_none(self, service):
        _transcriber(language_mode="de").transcribe_batch(wav_seconds(1.0))
        assert service.created()["language_hints"] == ["de"]
        assert service.created()["language_hints_strict"] is True

    def test_the_custom_vocabulary_is_the_context_terms(self, service):
        _transcriber(custom_vocabulary="Kubernetes, Grafana").transcribe_batch(
            wav_seconds(1.0)
        )
        assert service.created()["context"] == {"terms": ["Kubernetes", "Grafana"]}

    def test_terms_beyond_the_context_size_are_dropped_from_the_end(self):
        terms = [f"{index:04d}" + "x" * 96 for index in range(100)]
        kept = fit_terms(terms)
        assert 0 < len(kept) < len(terms)
        assert kept == terms[: len(kept)]
        assert sum(len(term) + 3 for term in kept) <= SONIOX_CONTEXT_MAX_CHARS

    @pytest.mark.parametrize("region", ["eu", "jp", "in"])
    def test_the_region_picks_the_host(self, service, region):
        _transcriber(region=region).transcribe_batch(wav_seconds(1.0))
        assert {r.host for r in service.requests} == {SONIOX_API_HOSTS[region]}

    def test_an_unknown_region_is_the_default(self, service):
        _transcriber(region="mars").transcribe_batch(wav_seconds(1.0))
        assert {r.host for r in service.requests} == {
            SONIOX_API_HOSTS[DEFAULT_SONIOX_REGION]
        }

    def test_silence_is_an_empty_transcript_not_an_error(self, service):
        service.text = ""
        assert _transcriber().transcribe_batch(wav_seconds(1.0)) == ""

    def test_only_the_newest_generation_is_offered(self):
        assert SONIOX_MODELS == ("stt-async-v6",)
        assert DEFAULT_SONIOX_MODEL == "stt-async-v6"
        assert _transcriber(model="stt-async-v5")._model == "stt-async-v6"

    def test_every_language_is_an_app_language(self):
        assert set(SONIOX_LANGUAGE_MODES) <= set(VALID_LANGUAGE_MODES)
        assert len(SONIOX_LANGUAGE_MODES) == 61
        assert language_modes_for_selection("soniox") == SONIOX_LANGUAGE_MODES
        assert _transcriber(language_mode="yue")._language_mode == "auto"

    def test_a_missing_key_is_refused(self):
        with pytest.raises(TranscriptionError, match="API key is missing"):
            SonioxTranscriber("")

    def test_a_long_recording_is_cut_by_the_engines_limit(self, service, monkeypatch):
        seen = {}

        def fake_parts(audio, request_fn, **kwargs):
            seen.update(kwargs)
            return request_fn(audio, kwargs["progress_text"])

        monkeypatch.setattr(provider_module, "transcribe_in_parts", fake_parts)
        _transcriber(silence_gate_threshold=0.02).transcribe_batch(wav_seconds(1.0))
        assert seen["silence_threshold"] == 0.02
        assert seen["limit"] == RemotePartLimit(seconds=3600.0, max_bytes=250_000_000)
        assert seen["limit"] == remote_batch_part_limit("soniox", "stt-async-v6")

    def test_each_part_is_its_own_job_that_is_deleted(self, service, monkeypatch):
        monkeypatch.setattr(
            provider_module,
            "remote_batch_part_limit",
            lambda *_a, **_k: RemotePartLimit(seconds=1.0, max_bytes=250_000_000),
        )
        text = _transcriber().transcribe_batch(wav_seconds(2.5))
        parts = service.routes().count("POST /files")
        assert parts >= 3
        assert text.count("Hallo Welt.") == parts
        assert service.routes().count(f"DELETE /files/{_FILE_ID}") == parts
        assert service.routes().count(f"DELETE /transcriptions/{_JOB_ID}") == parts


class TestSonioxPoll:
    @pytest.mark.parametrize("status", ["error", "failed"])
    def test_a_failed_job_names_its_reason_and_id_and_is_deleted(self, service, status):
        service.statuses = [status]
        service.job = {
            "error_type": "invalid_audio_file",
            "error_message": "The audio cannot be decoded.",
        }
        with pytest.raises(TranscriptionError) as caught:
            _transcriber().transcribe_batch(wav_seconds(1.0))
        message = str(caught.value)
        assert "invalid_audio_file" in message
        assert "cannot be decoded" in message
        assert f"Job id {_JOB_ID}" in message
        assert service.routes()[-2:] == [
            f"DELETE /transcriptions/{_JOB_ID}",
            f"DELETE /files/{_FILE_ID}",
        ]

    def test_one_failed_status_fetch_does_not_abort_the_wait(self, service):
        service.errors["status"] = [http_error("https://x", 503), OSError("reset")]
        assert _transcriber().transcribe_batch(wav_seconds(1.0)) == "Hallo Welt."

    @pytest.mark.parametrize("code", [401, 403, 404])
    def test_a_status_answer_that_cannot_change_ends_the_wait_and_keeps_the_job(
        self, service, code
    ):
        service.errors["status"] = [http_error("https://x", code)] * 5
        with pytest.raises(TranscriptionError, match=_JOB_ID) as caught:
            _transcriber().transcribe_batch(wav_seconds(1.0))
        assert f"HTTP {code}" in str(caught.value)
        assert [r for r in service.routes() if r.startswith("DELETE")] == []

    def test_a_job_that_never_finishes_is_bounded_and_left_alone(self, service):
        service.statuses = ["processing"]
        with pytest.raises(TranscriptionError, match="did not finish") as caught:
            _transcriber(max_wait_s=0.0).transcribe_batch(wav_seconds(1.0))
        assert _JOB_ID in str(caught.value)
        # "Deleting it earlier fails the transcription": the job may still
        # complete, so neither object is deleted.
        assert [r for r in service.routes() if r.startswith("DELETE")] == []

    def test_a_quit_ends_the_wait_and_leaves_the_job_alone(self, service, monkeypatch):
        monkeypatch.setattr(_job_poll, "transcription_shutdown_requested", lambda: True)
        with pytest.raises(TranscriptionError, match="shutting down") as caught:
            _transcriber().transcribe_batch(wav_seconds(1.0))
        assert _JOB_ID in str(caught.value)
        assert [r for r in service.routes() if r.startswith("DELETE")] == []

    @pytest.mark.parametrize(
        "bad", ["<html><body>Sign in</body></html>", "", '{"x": 1}']
    )
    def test_one_unreadable_status_answer_does_not_cost_the_job(
        self, service, monkeypatch, bad
    ):
        real = service.__call__
        sent = {"bad": False}

        def answer(request, timeout=None, **kwargs):
            path = request.full_url.split("/v1", 1)[1]
            if path == f"/transcriptions/{_JOB_ID}" and not sent["bad"]:
                sent["bad"] = True
                service.requests.append(request)
                return fake_response(bad)
            return real(request, timeout=timeout, **kwargs)

        monkeypatch.setattr(provider_module, "open_request", answer)
        assert _transcriber().transcribe_batch(wav_seconds(1.0)) == "Hallo Welt."

    def test_a_refused_redirect_while_polling_names_the_job(self, service):
        service.errors["status"] = [
            TranscriptionError("Soniox: the server redirected (HTTP 302) away.")
        ]
        with pytest.raises(TranscriptionError) as caught:
            _transcriber().transcribe_batch(wav_seconds(1.0))
        assert str(caught.value).endswith(f"Job id {_JOB_ID}.")


class TestSonioxStrictHints:
    """The vendor does not document `language_hints_strict` for v6."""

    @staticmethod
    def _refusal(message: str):
        body = json.dumps(
            {
                "status_code": 400,
                "error_type": "invalid_request",
                "message": message,
                "validation_errors": [],
            }
        )
        return http_error("https://x", 400, body)

    def test_a_refused_flag_is_repeated_once_without_it_and_the_hint_stays(
        self, service, caplog
    ):
        caplog.set_level(logging.WARNING)
        service.errors["create"] = [
            self._refusal("Model does not support language_hints_strict")
        ]

        text = _transcriber(language_mode="de").transcribe_batch(wav_seconds(1.0))

        assert text == "Hallo Welt."
        creates = [
            json.loads(r.data)
            for r in service.requests
            if r.get_method() == "POST" and r.full_url.endswith("/transcriptions")
        ]
        assert len(creates) == 2
        assert creates[0]["language_hints_strict"] is True
        assert "language_hints_strict" not in creates[1]
        assert creates[1]["language_hints"] == ["de"]
        assert caplog.text.count("refused language_hints_strict") == 1

    def test_a_second_refusal_is_not_retried_again(self, service):
        message = "Model does not support language_hints_strict"
        service.errors["create"] = [self._refusal(message), self._refusal(message)]
        with pytest.raises(TranscriptionError):
            _transcriber(language_mode="de").transcribe_batch(wav_seconds(1.0))
        creates = [
            r
            for r in service.requests
            if r.get_method() == "POST" and r.full_url.endswith("/transcriptions")
        ]
        assert len(creates) == 2

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            (400, "Unknown model"),
            (400, "Invalid language hint"),
            (429, "Model does not support language_hints_strict"),
        ],
    )
    def test_any_other_refusal_is_not_retried(self, service, code, message):
        body = json.dumps({"status_code": code, "message": message})
        service.errors["create"] = [http_error("https://x", code, body)]
        with pytest.raises(TranscriptionError):
            _transcriber(language_mode="de").transcribe_batch(wav_seconds(1.0))
        creates = [
            r
            for r in service.requests
            if r.get_method() == "POST" and r.full_url.endswith("/transcriptions")
        ]
        assert len(creates) == 1

    def test_auto_language_sends_no_flag_and_is_never_retried(self, service):
        service.errors["create"] = [
            self._refusal("Model does not support language_hints_strict")
        ]
        with pytest.raises(TranscriptionError):
            _transcriber().transcribe_batch(wav_seconds(1.0))
        creates = [
            r
            for r in service.requests
            if r.get_method() == "POST" and r.full_url.endswith("/transcriptions")
        ]
        assert len(creates) == 1


class TestSonioxErrors:
    @pytest.mark.parametrize("step", ["upload", "create"])
    @pytest.mark.parametrize(
        ("code", "shows"),
        [
            (401, "Authentication failed"),
            (402, "balance or budget"),
            (429, "limit exceeded"),
            (400, "HTTP 400"),
        ],
    )
    def test_a_refused_request_says_what_the_service_said(
        self, service, step, code, shows
    ):
        body = json.dumps(
            {
                "status_code": code,
                "error_type": "invalid_request",
                "message": "Soniox says no.",
                "validation_errors": [],
            }
        )
        service.errors[step] = [http_error("https://x", code, body)]
        with pytest.raises(TranscriptionError) as caught:
            _transcriber().transcribe_batch(wav_seconds(1.0))
        message = str(caught.value)
        assert shows in message
        if code != 401:
            assert "Soniox says no." in message
        else:
            assert "region" in message

    def test_a_slow_delete_does_not_delay_the_text(self, service, monkeypatch):
        """The deletes ran before the text was returned: a server that answered
        DELETE slowly held a 0.2 s dictation back by 30 s."""
        release = threading.Event()
        real = service.__call__

        def slow_delete(request, timeout=None, **kwargs):
            if request.get_method() == "DELETE":
                release.wait(timeout=5.0)
            return real(request, timeout=timeout, **kwargs)

        monkeypatch.setattr(provider_module, "open_request", slow_delete)
        started = time.monotonic()
        try:
            text = _transcriber().transcribe_batch(wav_seconds(1.0))
            took = time.monotonic() - started
            assert text == "Hallo Welt."
            assert took < 2.0
            assert not any(r.startswith("DELETE") for r in service.routes_now())
        finally:
            release.set()
        join_cleanup()
        assert f"DELETE /files/{_FILE_ID}" in service.routes()

    def test_a_refused_delete_is_logged_and_never_reaches_the_dictation(
        self, service, caplog
    ):
        caplog.set_level(logging.WARNING)
        service.errors["delete"] = [http_error("https://x", 500, "{}")]
        assert _transcriber().transcribe_batch(wav_seconds(1.0)) == "Hallo Welt."
        join_cleanup()
        assert "soniox_cleanup_failed" in caplog.text

    def test_a_job_that_cannot_be_created_deletes_the_uploaded_file(self, service):
        service.errors["create"] = [http_error("https://x", 402, "{}")]
        with pytest.raises(TranscriptionError):
            _transcriber().transcribe_batch(wav_seconds(1.0))
        assert service.routes()[-1] == f"DELETE /files/{_FILE_ID}"

    @pytest.mark.parametrize("step", ["upload", "create"])
    def test_an_html_page_for_a_post_is_named(self, service, monkeypatch, step):
        page = "<!DOCTYPE html><html><head><title>Sign in</title></head></html>"
        real = service.__call__

        def answer(request, timeout=None, **kwargs):
            path = request.full_url.split("/v1", 1)[1]
            if request.get_method() == "POST" and (
                (step == "upload") == (path == "/files")
            ):
                service.requests.append(request)
                return fake_response(page)
            return real(request, timeout=timeout, **kwargs)

        monkeypatch.setattr(provider_module, "open_request", answer)
        with pytest.raises(TranscriptionError, match="HTML page") as caught:
            _transcriber().transcribe_batch(wav_seconds(1.0))
        assert "Expecting value" not in str(caught.value)
        assert str(caught.value).startswith("Soniox ")

    def test_an_answer_without_an_id_is_an_error(self, service, monkeypatch):
        monkeypatch.setattr(
            provider_module,
            "open_request",
            lambda *_a, **_k: json_response({"status": "ok"}, 201),
        )
        with pytest.raises(TranscriptionError, match="no 'id' field"):
            _transcriber().transcribe_batch(wav_seconds(1.0))

    def test_a_transcript_that_cannot_be_fetched_names_the_job_and_keeps_it(
        self, service
    ):
        service.errors["transcript"] = [http_error("https://x", 404)] * 5
        with pytest.raises(TranscriptionError, match=_JOB_ID) as caught:
            _transcriber().transcribe_batch(wav_seconds(1.0))
        assert "30 days" in str(caught.value)
        assert [r for r in service.routes() if r.startswith("DELETE")] == []

    def test_a_failed_transcript_fetch_is_retried(self, service):
        service.errors["transcript"] = [http_error("https://x", 500)]
        assert _transcriber().transcribe_batch(wav_seconds(1.0)) == "Hallo Welt."

    @pytest.mark.parametrize(
        "body",
        [
            "<!DOCTYPE html><html><body>Sign in</body></html>",
            '{"id": "x", "tokens": []}',
            '{"error": {"message": "quota"}}',
        ],
    )
    def test_a_transcript_answer_that_is_not_one_names_the_job(
        self, service, monkeypatch, body
    ):
        real = service.__call__

        def answer(request, timeout=None, **kwargs):
            if request.full_url.endswith("/transcript"):
                service.requests.append(request)
                return fake_response(body)
            return real(request, timeout=timeout, **kwargs)

        monkeypatch.setattr(provider_module, "open_request", answer)
        with pytest.raises(TranscriptionError) as caught:
            _transcriber().transcribe_batch(wav_seconds(1.0))
        assert f"Job id {_JOB_ID}." in str(caught.value)
        assert [r for r in service.routes() if r.startswith("DELETE")] == []

    def test_a_refused_deletion_never_fails_the_dictation_and_is_logged(
        self, service, caplog
    ):
        caplog.set_level(logging.WARNING)
        service.errors["delete"] = [
            http_error("https://x", 500, f"boom {_KEY}"),
            OSError(f"gone {_KEY}"),
        ]
        assert _transcriber().transcribe_batch(wav_seconds(1.0)) == "Hallo Welt."
        join_cleanup()
        assert caplog.text.count("soniox_cleanup_failed") == 2
        assert _KEY not in caplog.text


class TestSonioxConnection:
    def test_the_key_check_lists_models_in_the_region_and_sends_no_audio(self, service):
        ok, message = _transcriber(region="eu").test_connection()
        assert ok, message
        (request,) = service.requests
        assert request.get_method() == "GET"
        assert request.full_url == "https://api.eu.soniox.com/v1/models"
        assert request.get_header("Authorization") == f"Bearer {_KEY}"

    @pytest.mark.parametrize("code", [401, 403])
    def test_a_refused_key_reads_as_one_and_names_the_region(self, service, code):
        service.errors["models"] = [http_error("https://x", code)]
        ok, message = _transcriber().test_connection()
        assert not ok
        assert f"HTTP {code}" in message
        assert "region" in message

    def test_a_proxy_page_is_not_a_valid_key(self, monkeypatch):
        monkeypatch.setattr(
            provider_module,
            "open_request",
            lambda *_a, **_k: fake_response("<html><body>Sign in</body></html>"),
        )
        ok, message = _transcriber().test_connection()
        assert not ok
        assert "HTML page" in message

    def test_a_reply_without_models_is_not_a_valid_key(self, monkeypatch):
        monkeypatch.setattr(
            provider_module,
            "open_request",
            lambda *_a, **_k: json_response({"status": "ok"}),
        )
        ok, message = _transcriber().test_connection()
        assert not ok
        assert "Unexpected response" in message
