import stt_app.local_model_download_worker as worker


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
        lambda model, model_dir: calls.append((model, model_dir)),
    )

    result = worker.main(["--model", "small", "--model-dir", "/tmp/models"])

    assert result == 0
    assert calls == ["trust", "env", ("small", "/tmp/models")]


def test_download_worker_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(worker, "inject_system_trust_store", lambda: None)
    monkeypatch.setattr(worker, "sync_ca_bundle_env_vars", lambda: None)
    monkeypatch.setattr(
        worker,
        "download_model_snapshot",
        lambda _model, _model_dir: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    result = worker.main(["--model", "small"])

    assert result == 1
    assert capsys.readouterr().err == "failed\n"
