"""A bounded poll of a remote batch job, shared by REST providers.

A provider that submits a job and then asks for its status in a loop has to
bound that loop in every direction, or one job the service never finishes
holds the app's single transcription worker -- and, because
`ThreadPoolExecutor`'s exit handler joins its workers, keeps the process (and
its single-instance lock) alive after the user quit. The AssemblyAI provider
learnt each of these properties separately (`docs/agents/remote-providers.md`,
"No remote batch wait may be unbounded" and the entries after it); this is
that loop, used by AssemblyAI (through its SDK) and by Speechmatics (over
plain HTTP) since 2026-10-01, and `fetch_with_retries`
is its counterpart for the one request that fetches the finished result:

- a total budget (`max_wait_s`), plus one request in flight, which a
  deadline cannot interrupt;
- a bounded count of *consecutive* failed fetches, reset by a successful
  one, so one read timeout does not abort a job the service is still
  transcribing and a revoked key does not spend the whole budget;
- the app-wide shutdown flag, read at the top of the loop and between the
  slices each sleep is cut into, so a quit ends the wait within a slice;
- the job id in every message, because it is what lets the user recover a
  job that may still complete;
- a terminal status as the positive test, so a status the service adds later
  is waited out rather than mistaken for a finished job.

The poll deliberately does not honour the transcriber's cancel check: the
service finishes the job either way, and "a finished transcription is never
discarded" is the rule the controller keeps.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection
from typing import Any

from .base import TranscriptionError, transcription_shutdown_requested

MAX_CONSECUTIVE_FETCH_FAILURES = 3
SHUTDOWN_POLL_S = 0.5


def poll_job(
    fetch: Callable[[], Any],
    *,
    status_of: Callable[[Any], str],
    terminal: Collection[str],
    provider: str,
    job_id: str,
    max_wait_s: float,
    interval_s: float,
    initial_status: str = "unknown",
) -> Any:
    """Fetch until `status_of(answer)` is in `terminal` and return that answer.

    `fetch` performs one request. A `TranscriptionError` it raises (a 401 the
    provider already turned into a sentence) ends the wait at once; any other
    exception, and a `status_of` that raises on the answer, is a failed fetch.
    Fetches first and sleeps between fetches, never before the first one.
    """
    if not job_id:
        # Without an id there is nothing to poll, and looping would only spend
        # the whole budget asking about nothing.
        raise TranscriptionError(
            f"{provider} accepted the audio but returned no job id."
        )
    deadline = time.monotonic() + max_wait_s
    status = initial_status
    consecutive_failures = 0
    while True:
        if transcription_shutdown_requested():
            raise TranscriptionError(
                f"The application is shutting down; the {provider} job was "
                f"left running (last status: {status}, job id {job_id})."
            )
        if time.monotonic() >= deadline:
            raise TranscriptionError(
                f"{provider} did not finish the transcription within "
                f"{int(max_wait_s / 60)} minutes (last status: {status}). The "
                f"job may still complete; job id {job_id}."
            )
        try:
            answer = fetch()
            fetched_status = str(status_of(answer))
        except TranscriptionError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FETCH_FAILURES:
                raise TranscriptionError(
                    f"{provider} transcription failed: could not fetch the job "
                    f"status ({exc}). The job may still complete; job id "
                    f"{job_id}."
                ) from exc
        else:
            consecutive_failures = 0
            status = fetched_status
            if status in terminal:
                return answer
        _sleep_between_fetches(min(interval_s, max(deadline - time.monotonic(), 0.0)))


def fetch_with_retries(
    fetch: Callable[[], Any],
    *,
    provider: str,
    job_id: str,
    interval_s: float,
    note: str = "",
) -> Any:
    """Fetch a finished job's result, retrying a failed request.

    The job is done by the time this runs, so one read timeout or 5xx must
    not discard a transcript the next request would have fetched. The same
    bounds as the poll: `MAX_CONSECUTIVE_FETCH_FAILURES` attempts one
    interval apart, a `TranscriptionError` from `fetch` ends it at once, and
    the shutdown flag is read before every attempt and between sleep
    slices. `note` is appended to the final failure (where the result can
    still be found, for how long).
    """
    failures = 0
    while True:
        if transcription_shutdown_requested():
            raise TranscriptionError(
                f"The application is shutting down; the {provider} "
                f"transcript was not fetched (job id {job_id})."
            )
        try:
            return fetch()
        except TranscriptionError:
            raise
        except Exception as exc:
            failures += 1
            if failures >= MAX_CONSECUTIVE_FETCH_FAILURES:
                suffix = f" {note}" if note else ""
                raise TranscriptionError(
                    f"{provider} finished the job, but its transcript could "
                    f"not be fetched ({exc}). Job id {job_id}.{suffix}"
                ) from exc
        _sleep_between_fetches(interval_s)


def _sleep_between_fetches(seconds: float) -> None:
    """One polling interval, in slices, so a quit ends it within a slice."""
    end = time.monotonic() + seconds
    while not transcription_shutdown_requested():
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, SHUTDOWN_POLL_S))
