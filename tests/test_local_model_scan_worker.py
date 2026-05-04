import json

import stt_app.local_model_scan_worker as worker


def test_scan_cached_models_delegates_to_inventory(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        worker,
        "find_cached_models",
        lambda model_dir="": calls.append(model_dir) or ["small"],
    )

    assert worker.scan_cached_models(" /tmp/models ") == ["small"]
    assert calls == ["/tmp/models"]


def test_worker_main_writes_json_to_stdout(monkeypatch, capsys):
    monkeypatch.setattr(worker, "scan_cached_models", lambda _model_dir="": ["tiny"])

    assert worker.main(["--model-dir", "/tmp/models"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"cached_models": ["tiny"]}


def test_worker_main_writes_json_to_output_file(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "scan_cached_models", lambda _model_dir="": ["base"])
    output_path = tmp_path / "scan.json"

    assert worker.main(["--model-dir", "/tmp/models", "--output", str(output_path)]) == 0

    assert json.loads(output_path.read_text(encoding="utf-8")) == {
        "cached_models": ["base"]
    }
