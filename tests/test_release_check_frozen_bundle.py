from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_script():
    sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "release_check_frozen_bundle", _SCRIPTS / "release_check_frozen_bundle.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_check_frozen_bundle"] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


def test_the_default_model_is_one_of_the_preferred_models():
    from stt_app.config import DEFAULT_MODEL_SIZE

    assert DEFAULT_MODEL_SIZE in script.PREFERRED


def test_plan_models_runs_the_cached_ones_and_names_the_missing_ones():
    models, missing = script.plan_models(["small", "parakeet-tdt-0.6b-v3"])

    assert models == ["parakeet-tdt-0.6b-v3", "small"]
    assert missing == [
        "nemotron-3.5-asr-streaming-0.6b-int4",
        "cohere-transcribe-03-2026",
        "granite-5.0-470m-turboctc-multi",
    ]


def test_a_missing_default_model_is_reported_and_not_silently_dropped(
    monkeypatch, tmp_path, capsys
):
    """The owner's Hugging Face cache lacks the multilingual TurboCTC, so the
    check ran without covering the default model and said nothing."""
    exe = tmp_path / "stt_app.exe"
    exe.write_bytes(b"")
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF")
    report = tmp_path / "report.json"
    ran: list[list[str]] = []
    monkeypatch.setattr(script, "scan_pass", lambda *_a, **_k: ["small"])
    monkeypatch.setattr(
        script,
        "run_benchmark_pass",
        lambda _checks, _name, _exe, _sandbox, options: ran.append(
            options["model_names"]
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_check_frozen_bundle.py",
            "--exe",
            str(exe),
            "--clip",
            str(clip),
            "--no-gui",
            "--report",
            str(report),
        ],
    )

    script.main()

    out = capsys.readouterr().out
    assert ran == [["small"]]
    assert "granite-5.0-470m-turboctc-multi is not on this PC" in out
    skipped = [
        item["name"]
        for item in json.loads(report.read_text(encoding="utf-8"))["checks"]
        if item["ok"] is None
    ]
    assert "frozen.benchmark.granite-5.0-470m-turboctc-multi" in skipped
    assert "frozen.benchmark.small" not in skipped
