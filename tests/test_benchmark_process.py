from __future__ import annotations

import json
import queue

import pytest

from stt_app import benchmark_process, benchmark_worker
from stt_app.local_benchmark import BenchmarkCancelled, BenchmarkCase, BenchmarkRun


def _sample_audio() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sample = root / "samples" / "benchmark_sample.wav"
    return str(sample if sample.is_file() else root / "nonexistent.wav")


# --- Worker event emission (in-process, no subprocess) ---------------------


def test_worker_emits_progress_case_and_done(monkeypatch, capsys):
    def fake_run_benchmark_cases(**kwargs):
        kwargs["progress_callback"]("loading")
        case = BenchmarkCase(
            model="small",
            device="cpu",
            compute_type="int8",
            download_seconds=0.0,
            load_seconds=0.4,
            runs=[
                BenchmarkRun(
                    run_index=1,
                    seconds=1.0,
                    audio_duration_seconds=2.0,
                    real_time_factor=0.5,
                    transcript_chars=5,
                    transcript_words=1,
                    detected_language="en",
                    language_probability=0.9,
                )
            ],
        )
        kwargs["case_callback"](case)
        return [case]

    monkeypatch.setattr(
        benchmark_worker, "run_benchmark_cases", fake_run_benchmark_cases
    )

    exit_code = benchmark_worker.run_from_options(
        {"audio_path": "x.wav", "model_names": ["small"]}
    )
    assert exit_code == 0

    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(benchmark_worker.BENCHMARK_EVENT_PREFIX)
    ]
    events = [
        json.loads(line[len(benchmark_worker.BENCHMARK_EVENT_PREFIX):])
        for line in lines
    ]
    kinds = [event["event"] for event in events]
    assert kinds == ["progress", "case", "done"]
    assert events[1]["case"]["model"] == "small"
    assert events[1]["case"]["runs"][0]["run_index"] == 1


def test_worker_emits_error_event_on_failure(monkeypatch, capsys):
    def boom(**_kwargs):
        raise RuntimeError("worker blew up")

    monkeypatch.setattr(benchmark_worker, "run_benchmark_cases", boom)

    exit_code = benchmark_worker.run_from_options(
        {"audio_path": "x.wav", "model_names": ["small"]}
    )
    assert exit_code == 1
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(benchmark_worker.BENCHMARK_EVENT_PREFIX)
    ]
    events = [
        json.loads(line[len(benchmark_worker.BENCHMARK_EVENT_PREFIX):])
        for line in lines
    ]
    assert events == [{"event": "error", "message": "worker blew up"}]


def test_worker_parses_explicit_string_booleans(monkeypatch, capsys):
    captured = {}

    def fake_run_benchmark_cases(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        benchmark_worker, "run_benchmark_cases", fake_run_benchmark_cases
    )

    assert benchmark_worker.run_from_options(
        {
            "audio_path": "x.wav",
            "model_names": ["small"],
            "vad_filter": "false",
            "warmup": "true",
        }
    ) == 0

    assert captured["vad_filter"] is False
    assert captured["warmup"] is True
    _ = capsys.readouterr()


# --- Event stream parsing --------------------------------------------------


def test_pump_events_ignores_non_prefixed_lines():
    class _Stream:
        def __init__(self, lines):
            self._lines = lines

        def __iter__(self):
            return iter(self._lines)

    stream = _Stream(
        [
            "faster-whisper noise on stdout\n",
            f"{benchmark_worker.BENCHMARK_EVENT_PREFIX}"
            + json.dumps({"event": "progress", "text": "hi"})
            + "\n",
            "garbage\n",
            f"{benchmark_worker.BENCHMARK_EVENT_PREFIX}not-json\n",
            f"{benchmark_worker.BENCHMARK_EVENT_PREFIX}"
            + json.dumps({"event": "done"})
            + "\n",
        ]
    )
    events: "queue.Queue" = queue.Queue()
    benchmark_process._pump_events(stream, events)

    collected = []
    while True:
        item = events.get_nowait()
        if item is benchmark_process._EOF:
            break
        collected.append(item)
    assert [event["event"] for event in collected] == ["progress", "done"]


# --- Command building ------------------------------------------------------


def test_benchmark_command_uses_module_in_source_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark_process.sys, "frozen", False, raising=False)
    env: dict[str, str] = {}
    options_path = tmp_path / "options.json"
    command = benchmark_process.benchmark_command(options_path, env)

    assert command[1:3] == ["-m", "stt_app.benchmark_worker"]
    assert command[-2:] == ["--options", str(options_path)]
    # The package source dir is put on PYTHONPATH so the child can import stt_app.
    assert env["PYTHONPATH"].startswith(str(benchmark_process._package_source_dir()))


def test_benchmark_command_uses_worker_arg_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark_process.sys, "frozen", True, raising=False)
    command = benchmark_process.benchmark_command(tmp_path / "options.json", {})
    assert benchmark_process.BENCHMARK_WORKER_ARG in command


# --- Full subprocess round-trip -------------------------------------------


def test_run_benchmark_cases_streams_unknown_model_case_from_subprocess():
    progress: list[str] = []
    cases: list[BenchmarkCase] = []

    result = benchmark_process.run_benchmark_cases(
        audio_path=_sample_audio(),
        model_names=["future-local-model"],
        progress_callback=progress.append,
        case_callback=cases.append,
    )

    assert len(result) == 1
    assert "Benchmark runtime" in (result[0].error or "")
    assert len(cases) == 1
    assert cases[0].model == "future-local-model"
    assert progress  # at least the per-case progress line


def test_run_benchmark_cases_raises_when_canceled_immediately():
    with pytest.raises(BenchmarkCancelled):
        benchmark_process.run_benchmark_cases(
            audio_path=_sample_audio(),
            model_names=["future-local-model"],
            cancel_check=lambda: True,
        )
