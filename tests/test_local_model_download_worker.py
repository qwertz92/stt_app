import json

import stt_app.local_model_download_worker as worker
from stt_app.model_download_progress import (
    DOWNLOAD_EVENT_PREFIX,
    DOWNLOAD_PROGRESS_UNKNOWN,
)


def _events(captured: str) -> list[dict]:
    return [
        json.loads(line[len(DOWNLOAD_EVENT_PREFIX) :])
        for line in captured.splitlines()
        if line.startswith(DOWNLOAD_EVENT_PREFIX)
    ]


def test_download_worker_initializes_trust_and_downloads(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        worker,
        "inject_system_trust_store",
        lambda: calls.append("trust"),
    )
    monkeypatch.setattr(
        worker,
        "sync_ca_bundle_env_vars",
        lambda: calls.append("env"),
    )
    monkeypatch.setattr(
        worker,
        "download_model_snapshot",
        lambda model, model_dir, progress_hook=None: calls.append(
            (model, model_dir, callable(progress_hook))
        ),
    )

    result = worker.main(["--model", "small", "--model-dir", "/tmp/models"])

    assert result == 0
    assert calls == ["trust", "env", ("small", "/tmp/models", True)]


def test_download_worker_streams_the_downloaders_byte_counts(monkeypatch, capsys):
    """The parent shows these numbers instead of sizing the destination.

    Directory growth is only a faithful proxy while the downloader writes
    sequentially, and the Xet-backed repos do not.
    """
    monkeypatch.setattr(worker, "inject_system_trust_store", lambda: None)
    monkeypatch.setattr(worker, "sync_ca_bundle_env_vars", lambda: None)

    def fake_download(_model, _model_dir, progress_hook=None):
        progress_hook(0, 552_442_697)
        progress_hook(100_000_000, 552_442_697)
        progress_hook(552_442_697, 552_442_697)

    monkeypatch.setattr(worker, "download_model_snapshot", fake_download)
    monkeypatch.setattr(worker, "_MIN_EVENT_INTERVAL_S", 0.0)

    assert worker.main(["--model", "granite-speech-5.0-470m-turboctc"]) == 0

    events = _events(capsys.readouterr().out)
    assert events[-1] == {
        "event": "bytes",
        "done": 552_442_697,
        "total": 552_442_697,
    }
    assert [event["done"] for event in events] == [0, 100_000_000, 552_442_697]


def test_the_reporter_throttles_but_publishes_a_changed_total_at_once(capsys):
    """hf_xet reports every 200 ms and http_get once per 10 MiB chunk, so the
    throttle costs nothing there -- but the total is what the percentage is
    measured against, and it settles in the first second."""
    reporter = worker.ProgressReporter(min_interval_s=1_000.0)

    reporter.report(1_000, 1_000)
    reporter.report(2_000, 1_000)  # throttled: same total, too soon
    reporter.report(3_000, 552_000_000)  # the weight file's metadata landed

    events = _events(capsys.readouterr().out)
    assert [(event["done"], event["total"]) for event in events] == [
        (1_000, 1_000),
        (3_000, 552_000_000),
    ]


def test_the_reporter_flushes_the_sample_the_throttle_held_back(capsys):
    reporter = worker.ProgressReporter(min_interval_s=1_000.0)
    reporter.report(1_000, 552_000_000)
    reporter.report(552_000_000, 552_000_000)
    capsys.readouterr()

    reporter.flush()

    events = _events(capsys.readouterr().out)
    assert [(event["done"], event["total"]) for event in events] == [
        (552_000_000, 552_000_000)
    ]


def test_the_reported_count_never_goes_backwards(capsys):
    reporter = worker.ProgressReporter(min_interval_s=0.0)
    reporter.report(100, 1_000)
    reporter.report(50, 1_000)

    events = _events(capsys.readouterr().out)
    assert [event["done"] for event in events] == [100]


def test_the_unknown_sentinel_reaches_the_parent(capsys):
    """Sent when the download changes hands to the ModelScope mirror, which
    has no hook: the parent drops the sample rather than freezing the last
    figure on screen for the whole mirror transfer."""
    reporter = worker.ProgressReporter(min_interval_s=1_000.0)
    reporter.report(100, 1_000)
    capsys.readouterr()

    reporter.report(DOWNLOAD_PROGRESS_UNKNOWN, DOWNLOAD_PROGRESS_UNKNOWN)

    events = _events(capsys.readouterr().out)
    assert events == [
        {
            "event": "bytes",
            "done": DOWNLOAD_PROGRESS_UNKNOWN,
            "total": DOWNLOAD_PROGRESS_UNKNOWN,
        }
    ]


def test_a_failing_stdout_never_breaks_the_download(monkeypatch):
    """`report` runs inside huggingface_hub's and hf_xet's own callbacks, and
    the parent may have closed its read end of the pipe."""

    class _Broken:
        def write(self, _text):
            raise OSError("pipe closed")

        def flush(self):
            raise OSError("pipe closed")

    monkeypatch.setattr(worker.sys, "stdout", _Broken())

    worker.ProgressReporter(min_interval_s=0.0).report(1, 2)


def test_download_worker_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(worker, "inject_system_trust_store", lambda: None)
    monkeypatch.setattr(worker, "sync_ca_bundle_env_vars", lambda: None)
    monkeypatch.setattr(
        worker,
        "download_model_snapshot",
        lambda _model, _model_dir, progress_hook=None: (_ for _ in ()).throw(
            RuntimeError("failed")
        ),
    )

    result = worker.main(["--model", "small"])

    assert result == 1
    assert capsys.readouterr().err == "failed\n"
