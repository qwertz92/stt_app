import subprocess
from types import SimpleNamespace

import stt_app.local_model_download as local_model_download


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


def test_start_model_download_process_disables_worker_progress(monkeypatch):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(local_model_download.subprocess, "Popen", fake_popen)

    local_model_download.start_model_download_process("small")

    assert captured["env"]["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"].readable() is True
    assert captured["stderr"].writable() is True


def test_model_download_process_error_reads_and_closes_spooled_log():
    class _Process:
        def __init__(self):
            self._stt_error_log = local_model_download.tempfile.TemporaryFile(
                mode="w+t",
                encoding="utf-8",
            )
            self._stt_error_log.write("first line\nlast useful detail\n")

        def communicate(self, timeout=None):
            return None, None

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

    An unbounded `communicate()` on a child that survived terminate and kill
    blocked it permanently: the Settings cancel never completed, the slot was
    never handed back, and every later download in this process -- plus the
    benchmark worker and scripts/download_model.py, which share the
    machine-wide lock -- waited on it until the app was restarted.
    """
    calls: list[object] = []

    class _WedgedProcess:
        def __init__(self):
            self._killed = False

        def communicate(self, timeout=None):
            calls.append(timeout)
            if not self._killed:
                raise subprocess.TimeoutExpired("worker", timeout)
            return None, "worker gave up\n"

        def kill(self):
            calls.append("kill")
            self._killed = True

    process = _WedgedProcess()

    assert (
        local_model_download.model_download_process_error(process)
        == "worker gave up"
    )
    assert calls == [5.0, "kill", 5.0], calls
