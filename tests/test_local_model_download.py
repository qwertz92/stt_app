import subprocess

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
        return object()

    monkeypatch.setattr(local_model_download.subprocess, "Popen", fake_popen)

    local_model_download.start_model_download_process("small")

    assert captured["env"]["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE


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
