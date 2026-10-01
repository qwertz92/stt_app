"""Tests for the bounded job poll that REST batch providers share."""

from __future__ import annotations

import pytest

from stt_app.transcriber import _job_poll
from stt_app.transcriber.base import (
    TranscriptionError,
    request_transcription_shutdown,
)


class _TooManyFetches(BaseException):
    """Not an `Exception`, so the poll's retry arm cannot swallow it: a loop
    that stopped sleeping would otherwise spin on a fake clock forever."""


class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(_job_poll.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(_job_poll.time, "sleep", fake.sleep)
    return fake


def _fetcher(*answers, limit: int = 10_000, clock: _Clock | None = None):
    """Hands out `answers` in order (an exception instance is raised), then
    repeats the last one; past `limit` calls it stops the test. With a clock,
    `fetch.times` records when each call was made."""
    calls = {"count": 0}
    times: list[float] = []

    def fetch():
        calls["count"] += 1
        if clock is not None:
            times.append(clock.now - 1000.0)
        if calls["count"] > limit:
            raise _TooManyFetches
        answer = answers[min(calls["count"], len(answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    fetch.calls = calls
    fetch.times = times
    return fetch


def _poll(fetch, **overrides):
    kwargs = {
        "status_of": lambda job: job["status"],
        "terminal": ("done", "rejected"),
        "provider": "Speechmatics",
        "job_id": "job-42",
        "max_wait_s": 60.0,
        "interval_s": 2.0,
    }
    kwargs.update(overrides)
    return _job_poll.poll_job(fetch, **kwargs)


def test_a_terminal_answer_is_returned_without_sleeping_first(clock):
    fetch = _fetcher({"status": "done", "text": "ok"})

    assert _poll(fetch) == {"status": "done", "text": "ok"}
    assert fetch.calls["count"] == 1
    assert clock.sleeps == []


def test_an_unknown_status_is_waited_out_rather_than_taken_as_finished(clock):
    """Terminal is the positive test: a status the provider added after this
    code was written must not end the wait as if the job had finished."""
    fetch = _fetcher(
        {"status": "running"},
        {"status": "something-new"},
        {"status": "done"},
        clock=clock,
    )

    assert _poll(fetch)["status"] == "done"
    assert fetch.times == [0.0, 2.0, 4.0]


def test_the_wait_is_bounded_and_names_the_job(clock):
    fetch = _fetcher({"status": "running"}, limit=100)

    with pytest.raises(TranscriptionError) as caught:
        _poll(fetch, max_wait_s=60.0, interval_s=2.0)

    message = str(caught.value)
    assert "within 1 minutes" in message
    assert "last status: running" in message
    assert "job-42" in message
    assert clock.now - 1000.0 == pytest.approx(60.0)
    assert fetch.calls["count"] == 30


def test_the_last_sleep_is_clamped_to_the_remaining_budget(clock):
    fetch = _fetcher({"status": "running"}, limit=100, clock=clock)

    with pytest.raises(TranscriptionError):
        _poll(fetch, max_wait_s=5.0, interval_s=2.0)

    assert fetch.times == [0.0, 2.0, 4.0]
    assert clock.now - 1000.0 == pytest.approx(5.0)


def test_every_sleep_is_cut_into_slices_a_quit_can_end(clock):
    fetch = _fetcher({"status": "running"}, {"status": "done"}, clock=clock)

    _poll(fetch, interval_s=3.0)

    assert fetch.times == [0.0, 3.0]
    assert clock.sleeps and max(clock.sleeps) <= 0.5


def test_one_failed_fetch_is_retried_and_the_count_resets_on_success(clock):
    fetch = _fetcher(
        OSError("timed out"),
        OSError("timed out"),
        {"status": "running"},
        OSError("timed out"),
        OSError("timed out"),
        {"status": "done"},
    )

    assert _poll(fetch)["status"] == "done"
    assert fetch.calls["count"] == 6


def test_three_failed_fetches_in_a_row_fail_the_wait_with_the_job_id(clock):
    fetch = _fetcher(OSError("HTTP 503"), limit=10)

    with pytest.raises(TranscriptionError) as caught:
        _poll(fetch)

    message = str(caught.value)
    assert fetch.calls["count"] == 3
    assert "HTTP 503" in message
    assert "job-42" in message
    assert "may still complete" in message


def test_an_answer_without_a_status_counts_as_a_failed_fetch(clock):
    fetch = _fetcher({"unexpected": True}, limit=10)

    with pytest.raises(TranscriptionError) as caught:
        _poll(fetch)

    assert fetch.calls["count"] == 3
    assert "job-42" in str(caught.value)


def test_a_transcription_error_from_the_fetch_ends_the_wait_at_once(clock):
    """A provider maps a 401 to a `TranscriptionError`; retrying it three
    times only delays the one message that says what to fix."""
    fetch = _fetcher(TranscriptionError("Authentication failed"), limit=10)

    with pytest.raises(TranscriptionError, match="Authentication failed"):
        _poll(fetch)

    assert fetch.calls["count"] == 1


def test_a_job_without_an_id_fails_before_any_fetch(clock):
    fetch = _fetcher({"status": "running"}, limit=10)

    with pytest.raises(TranscriptionError, match="no job id"):
        _poll(fetch, job_id="")

    assert fetch.calls["count"] == 0


def test_a_quit_before_the_poll_ends_it_naming_the_job(clock):
    request_transcription_shutdown()
    fetch = _fetcher({"status": "running"}, limit=10)

    with pytest.raises(TranscriptionError) as caught:
        _poll(fetch, initial_status="submitted")

    message = str(caught.value)
    assert "shutting down" in message
    assert "job-42" in message
    assert "last status: submitted" in message
    assert fetch.calls["count"] == 0


def test_a_quit_during_a_sleep_ends_it_within_one_slice(clock):
    fetch = _fetcher({"status": "running"}, limit=10)
    clock.on_sleep = request_transcription_shutdown

    with pytest.raises(TranscriptionError, match="shutting down"):
        _poll(fetch, interval_s=10.0)

    assert clock.sleeps == [0.5]
    assert fetch.calls["count"] == 1


def _fetch_result(fetch, **overrides):
    kwargs = {
        "provider": "Speechmatics",
        "job_id": "job-42",
        "interval_s": 2.0,
    }
    kwargs.update(overrides)
    return _job_poll.fetch_with_retries(fetch, **kwargs)


def test_a_result_fetch_that_succeeds_is_returned_without_sleeping(clock):
    fetch = _fetcher("the transcript")

    assert _fetch_result(fetch) == "the transcript"
    assert fetch.calls["count"] == 1
    assert clock.sleeps == []


def test_a_failed_result_fetch_is_retried_one_interval_later(clock):
    """The job is finished at that point; one read timeout must not throw the
    transcript away when the next request would have fetched it."""
    fetch = _fetcher(OSError("timed out"), "the transcript", clock=clock)

    assert _fetch_result(fetch) == "the transcript"
    assert fetch.times == [0.0, 2.0]


def test_three_failed_result_fetches_name_the_job_and_the_note(clock):
    fetch = _fetcher(OSError("HTTP 502"), limit=10)

    with pytest.raises(TranscriptionError) as caught:
        _fetch_result(fetch, note="The service keeps it for 7 days.")

    message = str(caught.value)
    assert fetch.calls["count"] == 3
    assert "finished the job" in message
    assert "HTTP 502" in message
    assert "job-42" in message
    assert message.endswith("The service keeps it for 7 days.")


def test_a_transcription_error_from_the_result_fetch_is_not_retried(clock):
    fetch = _fetcher(TranscriptionError("Authentication failed"), limit=10)

    with pytest.raises(TranscriptionError, match="Authentication failed"):
        _fetch_result(fetch)

    assert fetch.calls["count"] == 1


def test_a_quit_between_result_fetches_ends_them(clock):
    fetch = _fetcher(OSError("timed out"), limit=10)
    clock.on_sleep = request_transcription_shutdown

    with pytest.raises(TranscriptionError) as caught:
        _fetch_result(fetch, interval_s=10.0)

    assert "shutting down" in str(caught.value)
    assert "job-42" in str(caught.value)
    assert fetch.calls["count"] == 1
