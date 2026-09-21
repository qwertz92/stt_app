import io
import json
import logging
import subprocess
import threading
from types import SimpleNamespace

import stt_app.local_model_download as local_model_download
from stt_app.model_download_progress import (
    DOWNLOAD_EVENT_PREFIX,
    DOWNLOAD_PROGRESS_UNKNOWN,
)


def _worker_stdout(*events: object) -> io.StringIO:
    lines = []
    for event in events:
        if isinstance(event, str):
            lines.append(event)
        else:
            lines.append(f"{DOWNLOAD_EVENT_PREFIX}{json.dumps(event)}")
    return io.StringIO("\n".join(lines) + "\n")


def _drain(stream) -> local_model_download._ProgressState:
    """Run the reader inline, as its thread would."""
    state = local_model_download._ProgressState()
    local_model_download._pump_progress(stream, state, "small")
    return state


def test_model_download_command_uses_module_worker(monkeypatch):
    monkeypatch.delattr(local_model_download.sys, "frozen", raising=False)
    env: dict[str, str] = {}

    command = local_model_download.model_download_command("small", "/tmp/models", env)

    assert command == [
        local_model_download.sys.executable,
        "-m",
        "stt_app.local_model_download_worker",
        "--model",
        "small",
        "--model-dir",
        "/tmp/models",
    ]
    assert "PYTHONPATH" in env


def test_model_download_command_uses_frozen_worker_arg(monkeypatch):
    monkeypatch.setattr(local_model_download.sys, "frozen", True, raising=False)

    command = local_model_download.model_download_command("small", "", {})

    assert command == [
        local_model_download.sys.executable,
        local_model_download.LOCAL_MODEL_DOWNLOAD_WORKER_ARG,
        "--model",
        "small",
        "--model-dir",
        "",
    ]


def test_start_model_download_process_pipes_and_drains_the_worker(monkeypatch):
    """stdout is a pipe now, because the worker reports its byte counts on it.

    A pipe nobody reads blocks the child once the OS buffer fills, and this
    one is written from inside hf_xet's progress callback -- so the reader
    thread is part of the contract, not an optimisation.
    """
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(stdout=_worker_stdout())

    monkeypatch.setattr(local_model_download.subprocess, "Popen", fake_popen)

    process = local_model_download.start_model_download_process("small")

    assert captured["env"]["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"].readable() is True
    assert captured["stderr"].writable() is True
    assert process._stt_progress_reader is not None
    process._stt_progress_reader.join(timeout=5)


def test_the_reader_publishes_the_latest_byte_sample():
    state = _drain(
        _worker_stdout(
            {"event": "bytes", "done": 0, "total": 552_442_697},
            {"event": "bytes", "done": 212_000_000, "total": 552_442_697},
        )
    )

    sample = state.get()
    assert sample is not None
    assert sample.downloaded_bytes == 212_000_000
    assert sample.total_bytes == 552_442_697


def test_library_noise_on_stdout_is_not_a_sample():
    state = _drain(
        _worker_stdout(
            "Downloading shards:  40%|####      | 2/5",
            {"event": "bytes", "done": 7, "total": 9},
            "",
        )
    )

    assert state.get().downloaded_bytes == 7


def test_a_worker_that_reports_nothing_leaves_the_caller_on_directory_growth(
    caplog,
):
    """Every build before this one reported nothing, and so does a
    huggingface_hub that stops feeding the hook. Both must keep working."""
    with caplog.at_level(logging.INFO, logger=local_model_download.__name__):
        state = _drain(_worker_stdout("no events here"))

    assert state.get() is None
    assert "model_download_progress_absent" in caplog.text


def _malformed_stdout():
    return _worker_stdout(
        {"event": "bytes", "done": 5, "total": 9},
        f"{DOWNLOAD_EVENT_PREFIX}{{not json",
        f"{DOWNLOAD_EVENT_PREFIX}{{\"event\": \"bytes\", \"done\": \"x\"}}",
        {"event": "bytes", "done": -7, "total": 9},
    )


def test_a_malformed_event_keeps_the_last_sample_and_is_logged_once(caplog):
    with caplog.at_level(logging.WARNING, logger=local_model_download.__name__):
        state = _drain(_malformed_stdout())

    assert state.get().downloaded_bytes == 5
    assert caplog.text.count("model_download_progress_event_malformed") == 1


def test_the_once_is_once_per_download_and_not_once_per_process(caplog):
    """The latch belongs to the child being read, not to the module.

    Held in a module-level set keyed by model name, the second download of
    the same model in one session was silently exempt -- and the only way to
    test the first one was to reach in and discard the key by hand.
    """
    with caplog.at_level(logging.WARNING, logger=local_model_download.__name__):
        _drain(_malformed_stdout())
        _drain(_malformed_stdout())

    assert caplog.text.count("model_download_progress_event_malformed") == 2


def test_the_unknown_sentinel_drops_the_sample():
    """The ModelScope mirror has no hook. Keeping the last figure would have
    frozen it on screen for the whole mirror transfer."""
    state = _drain(
        _worker_stdout(
            {"event": "bytes", "done": 212_000_000, "total": 552_442_697},
            {
                "event": "bytes",
                "done": DOWNLOAD_PROGRESS_UNKNOWN,
                "total": DOWNLOAD_PROGRESS_UNKNOWN,
            },
        )
    )

    assert state.get() is None
    assert state.ever_reported is True


def test_model_download_process_progress_tolerates_a_process_without_one():
    assert local_model_download.model_download_process_progress(None) is None
    assert (
        local_model_download.model_download_process_progress(SimpleNamespace())
        is None
    )


def test_model_download_process_error_reads_and_closes_spooled_log():
    class _Process:
        def __init__(self):
            self._stt_error_log = local_model_download.tempfile.TemporaryFile(
                mode="w+t",
                encoding="utf-8",
            )
            self._stt_error_log.write("first line\nlast useful detail\n")
            self.stdout = io.StringIO()

        def wait(self, timeout=None):
            return 0

    process = _Process()

    assert (
        local_model_download.model_download_process_error(process)
        == "last useful detail"
    )
    assert process._stt_error_log is None


def test_terminate_model_download_process_stops_running_process():
    calls: list[str] = []

    class _Process:
        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            calls.append(f"wait:{timeout}")

    local_model_download.terminate_model_download_process(_Process())

    assert calls == ["terminate", "wait:2.0"]


def test_a_child_that_ignores_terminate_is_waited_for_after_the_kill():
    """Returning while the child is alive races its own partial files.

    The callers go straight on to delete the `*.incomplete` files the child may
    still be writing. On Windows `terminate()` and `kill()` are the same
    `TerminateProcess` call, so a child that outlived the first wait will not
    fall over on the second either -- but the caller must at least find that
    out instead of assuming it.
    """
    calls: list[str] = []

    class _StubbornProcess:
        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

        def wait(self, timeout):
            calls.append(f"wait:{timeout}")
            if len(calls) == 2:
                raise subprocess.TimeoutExpired("worker", timeout)

    local_model_download.terminate_model_download_process(_StubbornProcess())

    assert calls == ["terminate", "wait:2.0", "kill", "wait:2.0"]


def test_reading_the_error_never_waits_on_the_child_for_ever():
    """This runs on the download queue worker, which holds the download slot.

    An unbounded wait on a child that survived terminate and kill blocked it
    permanently: the Settings cancel never completed, the slot was never
    handed back, and every later download in this process -- plus the
    benchmark worker and scripts/download_model.py, which share the
    machine-wide lock -- waited on it until the app was restarted.
    """
    calls: list[object] = []

    class _WedgedProcess:
        def __init__(self):
            self._killed = False
            self.stdout = io.StringIO()

        def wait(self, timeout=None):
            calls.append(timeout)
            if not self._killed:
                raise subprocess.TimeoutExpired("worker", timeout)
            return -9

        def kill(self):
            calls.append("kill")
            self._killed = True

    process = _WedgedProcess()

    assert local_model_download.model_download_process_error(process) == ""
    assert calls == [5.0, "kill", 5.0], calls


class _RecordingStream:
    def __init__(self) -> None:
        self.closed_by: list[str] = []

    def close(self) -> None:
        self.closed_by.append(threading.current_thread().name)


def test_a_reader_still_inside_a_read_owns_the_pipe_and_the_caller_leaves_it(
    monkeypatch, caplog
):
    """On Windows, closing a pipe another thread is reading blocks.

    Measured with a real child whose stdout handle a grandchild had
    inherited (`probe_reader_grandchild_breakdown.py`): after terminate and
    kill of the child, `reader.join(2.0)` returned with the reader still
    inside `readline()`, and `process.stdout.close()` then blocked for
    16.56 s. Without the grandchild both calls return in 0.00 s, which is
    what says the pipe -- not the dead child -- is what blocks. That close
    runs on the download queue worker thread, which holds the in-process and
    the machine-wide download slot; every other wait on that thread is
    bounded for exactly that reason. The reader closes the stream in its own
    `finally`, so leaving it alone loses nothing.
    """
    monkeypatch.setattr(local_model_download, "_READER_JOIN_TIMEOUT_S", 0.05)
    stream = _RecordingStream()
    still_reading = threading.Event()

    def _park() -> None:
        still_reading.wait(10.0)
        stream.close()

    reader = threading.Thread(
        target=_park, name="stt_app_model_download_progress", daemon=True
    )
    reader.start()
    process = SimpleNamespace(stdout=stream, _stt_progress_reader=reader)

    with caplog.at_level(logging.WARNING, logger=local_model_download.__name__):
        local_model_download._close_progress_reader(process)

    assert stream.closed_by == []
    assert "model_download_progress_reader_still_reading" in caplog.text

    still_reading.set()
    reader.join(timeout=5)
    assert stream.closed_by == ["stt_app_model_download_progress"]


def test_a_finished_reader_hands_the_pipe_back_to_the_caller():
    """The ordinary case: the child exited, the reader saw EOF and went."""
    stream = _RecordingStream()
    reader = threading.Thread(target=lambda: None, name="done-reader")
    reader.start()
    reader.join()
    process = SimpleNamespace(stdout=stream, _stt_progress_reader=reader)

    local_model_download._close_progress_reader(process)

    assert stream.closed_by == [threading.current_thread().name]


def test_releasing_a_canceled_download_gives_back_the_log_without_waiting():
    """The preload cancel runs on the Qt thread on five of its six sites.

    Nobody reads the error message of a download the user canceled, so there
    is nothing to wait for -- but the spooled stderr file and the reader
    reference are handles this process keeps until the `Popen` is collected.
    """
    error_log = local_model_download.tempfile.TemporaryFile(
        mode="w+t", encoding="utf-8"
    )
    never_ends = threading.Event()
    reader = threading.Thread(
        target=never_ends.wait, args=(30.0,), name="parked-reader", daemon=True
    )
    reader.start()
    process = SimpleNamespace(
        stdout=_RecordingStream(),
        _stt_progress_reader=reader,
        _stt_error_log=error_log,
    )

    local_model_download.release_model_download_process(process)

    assert error_log.closed is True
    assert process._stt_error_log is None
    assert process._stt_progress_reader is None
    assert reader.is_alive() is True, "it waited for the reader"
    never_ends.set()


def test_releasing_a_download_twice_is_harmless():
    process = SimpleNamespace(stdout=None, _stt_progress_reader=None)

    local_model_download.release_model_download_process(process)
    local_model_download.release_model_download_process(process)


def test_reading_the_error_does_not_read_the_pipe_a_second_time():
    """`communicate` would spawn a reader of its own for stdout, which the
    progress reader thread is already draining."""

    class _Process:
        def __init__(self):
            self.stdout = io.StringIO()
            self.communicated = False

        def wait(self, timeout=None):
            return 0

        def communicate(self, timeout=None):
            self.communicated = True
            return None, None

    process = _Process()
    local_model_download.model_download_process_error(process)

    assert process.communicated is False
    assert process.stdout.closed is True
