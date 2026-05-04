import json

import stt_app.local_model_scan as local_model_scan


def test_scan_cached_models_reads_subprocess_output(monkeypatch):
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        output_path = command[command.index("--output") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump({"cached_models": ["small", 42, "base"]}, handle)
        return _Result()

    monkeypatch.setattr(local_model_scan.subprocess, "run", fake_run)

    assert local_model_scan.scan_cached_models_out_of_process("/tmp/models") == [
        "small",
        "base",
    ]

    assert calls
    assert calls[0][:3] == [
        local_model_scan.sys.executable,
        "-m",
        "stt_app.local_model_scan_worker",
    ]
    assert "--output" in calls[0]


def test_scan_cached_models_returns_none_when_worker_fails(monkeypatch):
    class _Result:
        returncode = 1

    monkeypatch.setattr(
        local_model_scan.subprocess,
        "run",
        lambda *_args, **_kwargs: _Result(),
    )

    assert local_model_scan.scan_cached_models_out_of_process("/tmp/models") is None


def test_scan_cached_models_command_uses_frozen_worker_arg(monkeypatch, tmp_path):
    monkeypatch.setattr(local_model_scan.sys, "frozen", True, raising=False)
    output_path = tmp_path / "scan.json"

    command = local_model_scan.scan_cached_models_command(
        "/tmp/models",
        output_path,
        {},
    )

    assert command == [
        local_model_scan.sys.executable,
        local_model_scan.LOCAL_MODEL_SCAN_WORKER_ARG,
        "--model-dir",
        "/tmp/models",
        "--output",
        str(output_path),
    ]
