"""An audio import must not deadlock against a live streaming session.

Everything that is not the stream runs on one `max_workers=1` executor, a
streaming session holds the shared transcriber lease for its whole life, and a
local stream's finalize is queued onto that same single worker. So an import
that waits for the shared lease is a cycle: the import occupies the worker and
waits for a lease only the finalize releases, and the finalize cannot start
until the import returns.

`_transcribe_worker` has allowed an isolated runtime for exactly this reason
since the lane existed; `_transcribe_import_worker` was the one path on that
executor that did not. Measured on the real controller before the fix: the
import never acquired, the finalize never ran, and only releasing the lease by
hand broke it.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest
from conftest import make_controller

from stt_app import controller as controller_module


class _FakeTranscriber:
    def transcribe_batch(self, *_args, **_kwargs):
        return "importierter text"

    def set_language_mode(self, _mode):
        return None

    def set_cancel_check(self, _check):
        return None

    def close(self):
        return None


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setattr(
        controller_module, "create_transcriber", lambda *a, **k: _FakeTranscriber()
    )
    ctrl, app = make_controller()
    monkeypatch.setattr(
        ctrl, "_get_or_create_transcriber", lambda *a, **k: _FakeTranscriber()
    )
    yield ctrl
    ctrl._executor.shutdown(wait=False)
    _ = app


def test_an_import_completes_while_a_stream_holds_the_shared_lease(controller):
    """The import takes an isolated runtime instead of queueing behind itself."""
    settings = controller._settings
    stream_lease = controller._acquire_transcriber_runtime(settings)
    assert stream_lease._owns_shared_lock is True, (
        "the stream did not take the shared lease, so this proves nothing"
    )

    done = threading.Event()
    text: list[str] = []

    def run_import():
        text.append(
            controller._transcribe_import_worker(b"RIFF0000WAVE", settings, None)
        )
        done.set()

    worker = threading.Thread(target=run_import, daemon=True)
    worker.start()

    try:
        finished = done.wait(timeout=10.0)
    finally:
        # Always hand the lease back, even on the failing path. A worker
        # blocked in `_acquire_transcriber_runtime` waits on a plain lock with
        # no timeout, and `ThreadPoolExecutor`'s exit handler joins its threads
        # -- so a regression here would hang the whole run at interpreter exit
        # instead of reporting a failure.
        stream_lease.release()

    assert finished, (
        "the import never finished: it is waiting for the lease the stream holds"
    )
    assert text == ["importierter text"]


def test_the_stream_finalize_is_not_stuck_behind_a_blocked_import(controller):
    """The whole cycle, on the controller's own single worker.

    The import is submitted first so it occupies the one thread, then a task
    standing in for the finalize -- whose only job is to release the stream's
    lease, exactly as `_finalize_stream_worker`'s `finally` does.
    """
    settings = controller._settings
    stream_lease = controller._acquire_transcriber_runtime(settings)
    finalize_ran = threading.Event()

    def finalize_like():
        finalize_ran.set()
        stream_lease.release()

    import_future = controller._executor.submit(
        controller._transcribe_import_worker, b"RIFF0000WAVE", settings, None
    )
    # Let the import reach its acquire before the finalize is queued behind it.
    time.sleep(0.2)
    controller._executor.submit(finalize_like)

    try:
        imported = import_future.exception(timeout=10.0)
        imported = None if imported else import_future.result(timeout=0)
        ran = finalize_ran.wait(timeout=10.0)
    except concurrent.futures.TimeoutError:
        imported, ran = None, False
    finally:
        # See the note in the test above: release unconditionally so a
        # regression fails the run rather than hanging it.
        stream_lease.release()

    assert imported == "importierter text", (
        "the import never returned: it is blocked on the stream's lease"
    )
    assert ran, "the finalize never ran, so the stream lease was never released"
