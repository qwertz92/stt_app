from __future__ import annotations

import csv
import importlib.util
import queue
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from stt_app import local_benchmark


def _load_benchmark_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "benchmark_local.py"
    spec = importlib.util.spec_from_file_location("benchmark_local", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["benchmark_local"] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def test_benchmark_csv_writer_creates_run_and_summary_rows(tmp_path):
    module = _load_benchmark_module()
    run = module.BenchmarkRun(
        run_index=1,
        seconds=1.2,
        audio_duration_seconds=2.0,
        real_time_factor=0.6,
        transcript_chars=12,
        transcript_words=2,
        detected_language="en",
        language_probability=0.98,
    )
    case = module.BenchmarkCase(
        model="small",
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.5,
        runs=[run],
    )
    out_path = tmp_path / "bench.csv"

    module._write_csv(out_path, [case])

    rows = list(csv.DictReader(out_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0]["row_type"] == "run"
    assert rows[0]["model"] == "small"
    assert rows[0]["device"] == "cpu"
    assert rows[0]["compute_type"] == "int8"
    assert rows[0]["run_index"] == "1"
    assert rows[1]["row_type"] == "summary"
    assert rows[1]["model"] == "small"


def test_benchmark_csv_writer_neutralizes_spreadsheet_formulas(tmp_path):
    module = _load_benchmark_module()
    case = module.BenchmarkCase(
        model="@SUM(1,1)",
        device="auto",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.1,
        runs=[],
        runtime_details="-danger",
        error="=1+1",
    )
    out_path = tmp_path / "bench.csv"

    module._write_csv(out_path, [case])

    row = next(csv.DictReader(out_path.read_text(encoding="utf-8").splitlines()))
    assert row["model"] == "'@SUM(1,1)"
    assert row["runtime_details"] == "'-danger"
    assert row["error"] == "'=1+1"


def test_successful_cases_filters_errors():
    module = _load_benchmark_module()
    ok_case = module.BenchmarkCase(
        model="small",
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.5,
        runs=[
            module.BenchmarkRun(
                run_index=1,
                seconds=1.0,
                audio_duration_seconds=2.0,
                real_time_factor=0.5,
                transcript_chars=10,
                transcript_words=2,
                detected_language="en",
                language_probability=0.9,
            )
        ],
    )
    bad_case = module.BenchmarkCase(
        model="medium",
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.6,
        runs=[],
        error="failed",
    )

    successful = module._successful_cases([ok_case, bad_case])
    assert successful == [ok_case]


def test_normalize_webgpu_benchmark_devices_expands_groups():
    module = _load_benchmark_module()

    assert module.normalize_webgpu_benchmark_devices("gpu,cpu") == ["gpu", "cpu"]
    assert module.normalize_webgpu_benchmark_devices("all") == [
        "webgpu",
        "dml",
        "cpu",
    ]


def test_run_benchmark_cases_expands_webgpu_device_targets(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")

    def fake_webgpu_case(**kwargs):
        return local_benchmark.BenchmarkCase(
            model=kwargs["model_name"],
            device=kwargs["device"],
            compute_type="onnx-q4",
            download_seconds=0.0,
            load_seconds=0.1,
            runs=[],
        )

    monkeypatch.setattr(local_benchmark, "_run_webgpu_case", fake_webgpu_case)

    cases = local_benchmark.run_benchmark_cases(
        audio_path=audio_path,
        model_names=["cohere-transcribe-03-2026"],
        webgpu_devices="gpu,cpu",
    )

    assert [case.device for case in cases] == ["gpu", "cpu"]


def test_run_benchmark_cases_routes_nemotron_to_onnx_runtime(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    calls = []

    def fake_onnx_case(**kwargs):
        calls.append(kwargs)
        return local_benchmark.BenchmarkCase(
            model=kwargs["model_name"],
            device=kwargs["device"],
            compute_type="onnx-int4",
            download_seconds=0.0,
            load_seconds=0.1,
            runs=[],
        )

    monkeypatch.setattr(local_benchmark, "_run_onnx_case", fake_onnx_case)

    cases = local_benchmark.run_benchmark_cases(
        audio_path=audio_path,
        model_names=["nemotron-3.5-asr-streaming-0.6b-int4"],
        device="dml",
        webgpu_devices="all",
    )

    # Routed to the ORT GenAI case, and measured on the ONNX device targets:
    # "all" is webgpu/dml/cpu, and Nemotron has no WebGPU provider, so it runs
    # on DirectML and CPU rather than twice on DirectML.
    assert [call["device"] for call in calls] == ["dml", "cpu"]
    assert [case.compute_type for case in cases] == ["onnx-int4", "onnx-int4"]


def test_nemotron_is_measurable_on_a_pinned_device():
    """Settings lets Nemotron be pinned to a device, so it must be comparable.

    Before this the benchmark always ran it on `auto`, so the General tab
    offered a choice no measurement could support.
    """
    targets = local_benchmark.benchmark_device_targets

    assert targets("onnxruntime-genai", ["cpu"], "auto") == ["cpu"]
    assert targets("onnxruntime-genai", ["auto"], "auto") == ["auto"]
    # No WebGPU provider in ORT GenAI: every GPU flavour is DirectML, and the
    # duplicate is dropped instead of reporting one run twice.
    assert targets("onnxruntime-genai", ["webgpu", "dml", "cpu"], "auto") == [
        "dml",
        "cpu",
    ]
    assert targets("onnxruntime-genai", ["gpu", "cpu"], "auto") == ["dml", "cpu"]
    # Unchanged for the other runtimes.
    assert targets("onnx-webgpu", ["webgpu", "dml", "cpu"], "auto") == [
        "webgpu",
        "dml",
        "cpu",
    ]
    assert targets("faster-whisper", ["webgpu", "cpu"], "auto") == ["auto"]
    assert targets("onnx-asr", ["webgpu", "cpu"], "auto") == ["auto"]


def test_run_benchmark_cases_does_not_route_unknown_model_to_faster_whisper(
    monkeypatch,
    tmp_path,
):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    faster_calls = []
    monkeypatch.setattr(
        local_benchmark,
        "_run_case",
        lambda **kwargs: faster_calls.append(kwargs),
    )

    cases = local_benchmark.run_benchmark_cases(
        audio_path=audio_path,
        model_names=["future-local-model"],
    )

    assert faster_calls == []
    assert "Benchmark runtime" in str(cases[0].error)
    assert "Restart the app" in str(cases[0].error)


def test_nemotron_benchmark_defaults_to_auto_and_can_force_dml(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    instances = []

    class FakeNemotronTranscriber:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.runtime_device = "dml"
            self.runtime_details_text = "Fallback attempts: webgpu: unsupported"
            instances.append(self)

        def preload_model(self):
            pass

        def transcribe_batch(self, _audio_path):
            return "hello"

        def close(self):
            pass

    monkeypatch.setattr(
        "stt_app.transcriber.local_nemotron.LocalNemotronTranscriber",
        FakeNemotronTranscriber,
    )
    monkeypatch.setattr(local_benchmark, "_audio_duration_seconds", lambda _path: 1.0)

    case = local_benchmark._run_onnx_case(
        audio_path=audio_path,
        model_name="nemotron-3.5-asr-streaming-0.6b-int4",
        runs=1,
        language=None,
        warmup=False,
        device="dml",
        vad_filter=True,
    )

    assert instances[0].kwargs["language_mode"] == "auto"
    assert instances[0].kwargs["provider_order"] == ("dml",)
    assert instances[0].kwargs["use_runtime_vad"] is True
    assert case.runs[0].detected_language == "auto"
    assert case.runtime_details == "Fallback attempts: webgpu: unsupported"


def test_benchmark_summary_includes_runtime_fallback_details():
    case = local_benchmark.BenchmarkCase(
        model="granite-speech-4.1-2b",
        device="cpu",
        compute_type="onnx-int8",
        download_seconds=0.0,
        load_seconds=1.0,
        runs=[],
        runtime_details="Fallback attempts: webgpu: operator unsupported",
    )

    summary = local_benchmark.format_benchmark_summary([case])

    assert "runtime: Fallback attempts: webgpu: operator unsupported" in summary


def test_benchmark_summary_lists_individual_runs_when_multiple():
    def _run(index, seconds, rtf):
        return local_benchmark.BenchmarkRun(
            run_index=index,
            seconds=seconds,
            audio_duration_seconds=10.0,
            real_time_factor=rtf,
            transcript_chars=0,
            transcript_words=0,
            detected_language="de",
            language_probability=float("nan"),
        )

    case = local_benchmark.BenchmarkCase(
        model="small",
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=1.0,
        runs=[_run(1, 2.0, 0.2), _run(2, 3.0, 0.3)],
    )

    summary = local_benchmark.format_benchmark_summary([case])

    assert "run 1: 2.00s, rtf=0.200" in summary
    assert "run 2: 3.00s, rtf=0.300" in summary


def test_benchmark_summary_omits_run_list_for_single_run():
    case = local_benchmark.BenchmarkCase(
        model="small",
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=1.0,
        runs=[
            local_benchmark.BenchmarkRun(
                run_index=1,
                seconds=2.0,
                audio_duration_seconds=10.0,
                real_time_factor=0.2,
                transcript_chars=0,
                transcript_words=0,
                detected_language="de",
                language_probability=float("nan"),
            )
        ],
    )

    summary = local_benchmark.format_benchmark_summary([case])

    assert "run 1:" not in summary


def test_run_benchmark_cases_can_cancel_between_cases(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    completed = []

    def fake_case(**kwargs):
        return local_benchmark.BenchmarkCase(
            model=kwargs["model_name"],
            device=kwargs["device"],
            compute_type=kwargs["compute_type"],
            download_seconds=0.0,
            load_seconds=0.1,
            runs=[],
        )

    monkeypatch.setattr(local_benchmark, "_run_case", fake_case)

    with pytest.raises(local_benchmark.BenchmarkCancelled):
        local_benchmark.run_benchmark_cases(
            audio_path=audio_path,
            model_names=["tiny", "base"],
            case_callback=completed.append,
            cancel_check=lambda: bool(completed),
        )

    assert [case.model for case in completed] == ["tiny"]


def test_faster_whisper_case_reports_resolved_runtime_device(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    progress: list[str] = []

    class FakeWhisperModel:
        def __init__(self, _model_name, **_kwargs):
            self.model = SimpleNamespace(device="cpu")

        def transcribe(self, _audio_path, **_kwargs):
            segments = [SimpleNamespace(text="hello world")]
            info = SimpleNamespace(
                duration=2.0,
                language="en",
                language_probability=0.99,
            )
            return segments, info

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    monkeypatch.setattr(local_benchmark, "_audio_duration_seconds", lambda _path: 2.0)

    case = local_benchmark._run_case(
        audio_path=audio_path,
        model_name="tiny",
        device="auto",
        compute_type="int8",
        runs=1,
        beam_size=5,
        language=None,
        vad_filter=False,
        warmup=False,
        threads=0,
        progress_callback=progress.append,
    )

    assert case.device == "cpu"
    assert case.runs[0].transcript == "hello world"
    assert any("loaded on cpu" in message for message in progress)


def test_webgpu_benchmark_case_closes_transcriber_when_preload_fails(
    monkeypatch,
    tmp_path,
):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    instances = []

    class FakeWebGpuTranscriber:
        def __init__(self, **kwargs):
            self.closed = False
            instances.append(self)

        def preload_model(self):
            raise RuntimeError("load failed")

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        "stt_app.transcriber.local_webgpu_asr.LocalOnnxWebGpuTranscriber",
        FakeWebGpuTranscriber,
    )

    with pytest.raises(RuntimeError, match="load failed"):
        local_benchmark._run_webgpu_case(
            audio_path=audio_path,
            model_name="cohere-transcribe-03-2026",
            runs=1,
            language="en",
            warmup=False,
        )

    assert instances
    assert instances[0].closed is True


# --- BenchmarkCase download_seconds tests ---


class TestBenchmarkDownloadSeconds:
    def test_benchmark_case_has_download_seconds(self):
        module = _load_benchmark_module()
        case = module.BenchmarkCase(
            model="tiny",
            device="cpu",
            compute_type="int8",
            download_seconds=3.14,
            load_seconds=1.0,
            runs=[],
        )
        assert case.download_seconds == pytest.approx(3.14)

    def test_case_from_dict_parses_download_seconds(self):
        module = _load_benchmark_module()
        data = {
            "model": "small",
            "device": "cpu",
            "compute_type": "int8",
            "download_seconds": 5.5,
            "load_seconds": 2.0,
            "runs": [],
        }
        case = module._case_from_dict(data)
        assert case.download_seconds == pytest.approx(5.5)

    def test_case_from_dict_defaults_download_to_zero(self):
        module = _load_benchmark_module()
        data = {
            "model": "small",
            "device": "cpu",
            "compute_type": "int8",
            "load_seconds": 2.0,
            "runs": [],
        }
        case = module._case_from_dict(data)
        assert case.download_seconds == pytest.approx(0.0)

    def test_csv_includes_download_seconds_column(self, tmp_path):
        module = _load_benchmark_module()
        run = module.BenchmarkRun(
            run_index=1,
            seconds=1.0,
            audio_duration_seconds=2.0,
            real_time_factor=0.5,
            transcript_chars=10,
            transcript_words=2,
            detected_language="en",
            language_probability=0.9,
        )
        case = module.BenchmarkCase(
            model="tiny",
            device="cpu",
            compute_type="int8",
            download_seconds=4.2,
            load_seconds=0.5,
            runs=[run],
        )
        out_path = tmp_path / "bench.csv"
        module._write_csv(out_path, [case])

        text = out_path.read_text(encoding="utf-8")
        assert "download_seconds" in text
        assert "4.2" in text


def test_every_local_model_is_dispatchable_by_the_benchmark(monkeypatch, tmp_path):
    """Drive the real dispatcher, not a second copy of the runtime table.

    `LOCAL_MODEL_RUNTIME` gained a new value when the onnx-asr engine landed and
    `run_benchmark_cases` did not learn it, so benchmarking either new model
    always failed with "Benchmark runtime ... is unknown". Asserting the table
    against a hardcoded set in the test could never catch that: it was the same
    table written twice.
    """
    from stt_app import local_benchmark
    from stt_app.config import CANARY_MODEL_SIZE, VALID_MODEL_SIZES

    def fake_case(**kwargs) -> local_benchmark.BenchmarkCase:
        return local_benchmark.BenchmarkCase(
            model=kwargs.get("model_name", "?"),
            device=kwargs.get("device", "cpu"),
            compute_type="stub",
            download_seconds=0.0,
            load_seconds=0.0,
            runs=[],
            error=None,
            runtime_details="",
        )

    monkeypatch.setattr(local_benchmark, "_run_case", lambda **kw: fake_case(**kw))
    monkeypatch.setattr(local_benchmark, "_run_onnx_case", lambda **kw: fake_case(**kw))
    monkeypatch.setattr(
        local_benchmark, "_run_webgpu_case", lambda **kw: fake_case(**kw)
    )

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")

    for model_name in VALID_MODEL_SIZES:
        # Canary deliberately refuses to guess a language; give it one.
        language = "en" if model_name == CANARY_MODEL_SIZE else None
        cases = local_benchmark.run_benchmark_cases(
            audio_path=audio,
            model_names=[model_name],
            runs=1,
            warmup=False,
            device="cpu",
            language=language,
        )
        assert cases, model_name
        for case in cases:
            assert "is unknown" not in str(case.error or ""), (
                f"{model_name}: {case.error}"
            )


def test_canary_refuses_to_benchmark_without_a_language(tmp_path):
    """Defaulting a language cannot be right: the sample's language is not
    knowable, and a wrong one makes Canary translate, so the benchmark would
    store the translation as the transcript."""
    import pytest as _pytest

    from stt_app.config import CANARY_MODEL_SIZE
    from stt_app.local_benchmark import _run_onnx_case

    with _pytest.raises(ValueError, match="cannot detect the language"):
        _run_onnx_case(
            audio_path=tmp_path / "clip.wav",
            model_name=CANARY_MODEL_SIZE,
            runs=1,
            language=None,
            warmup=False,
        )


def test_a_case_runs_off_the_main_thread_so_ctrl_c_can_reach_the_model():
    """The in-process path must not block Ctrl+C inside the model call.

    `--no-isolated-case` used to call `_run_case` on the main thread, where a
    signal handler cannot run while the process sits in `InferenceSession.run`.
    """
    module = _load_benchmark_module()
    threads: list[str] = []

    def _fake_run_case(**_kwargs):
        threads.append(threading.current_thread().name)
        return local_benchmark.BenchmarkCase(
            model="parakeet-tdt-0.6b-v3",
            device="cpu",
            compute_type="int8",
            download_seconds=0.0,
            load_seconds=0.5,
            runs=[],
        )

    module._run_case = _fake_run_case
    case = module._run_case_threaded({"model_name": "parakeet-tdt-0.6b-v3"})

    assert case.model == "parakeet-tdt-0.6b-v3"
    assert threads == ["benchmark-case"], (
        "the case ran on the main thread, where Ctrl+C is not delivered until "
        "the blocking model call returns"
    )


def test_ctrl_c_during_a_case_sets_the_flag_the_model_polls():
    """The interrupt must reach the running model, not just the shell."""
    module = _load_benchmark_module()
    module._cancel_requested.clear()
    entered = threading.Event()
    observed: list[bool] = []

    def _fake_run_case(**_kwargs):
        entered.set()
        # Stand in for the blocking model call: poll the same check the real
        # transcribers are handed.
        for _ in range(200):
            if module._cancel_check():
                observed.append(True)
                raise local_benchmark.BenchmarkCancelled("Benchmark canceled.")
            time.sleep(0.01)
        raise AssertionError("the cancel flag never reached the case")

    module._run_case = _fake_run_case
    real_join = threading.Thread.join

    def _join_that_interrupts(self, timeout=None):
        real_join(self, timeout)
        # Scoped to the case thread by name: this patch is on the class, so
        # without the check any other thread joined while it is installed --
        # a pytest or Qt internal -- would take a KeyboardInterrupt meant for
        # the benchmark.
        if (
            self.name == "benchmark-case"
            and entered.is_set()
            and not module._cancel_requested.is_set()
        ):
            raise KeyboardInterrupt

    threading.Thread.join = _join_that_interrupts
    try:
        with pytest.raises(KeyboardInterrupt):
            module._run_case_threaded({"model_name": "canary-1b-v2"})
    finally:
        threading.Thread.join = real_join
        module._cancel_requested.clear()

    assert observed == [True], "the running case never saw the cancel"


def test_a_case_failure_is_reported_from_the_worker_thread():
    """An exception on the worker thread must not be swallowed."""
    module = _load_benchmark_module()

    def _fake_run_case(**_kwargs):
        raise RuntimeError("model load failed")

    module._run_case = _fake_run_case
    with pytest.raises(RuntimeError, match="model load failed"):
        module._run_case_threaded({"model_name": "small"})


def test_the_case_hands_its_cancel_check_to_the_benchmark_run(tmp_path):
    """Without this the flag Ctrl+C sets never reaches the transcriber."""
    module = _load_benchmark_module()
    seen: dict[str, object] = {}

    def _fake_run(**kwargs):
        seen.update(kwargs)
        return [
            local_benchmark.BenchmarkCase(
                model="parakeet-tdt-0.6b-v3",
                device="cpu",
                compute_type="int8",
                download_seconds=0.0,
                load_seconds=0.5,
                runs=[],
            )
        ]

    module._shared_run_benchmark_cases = _fake_run
    module._run_case(
        audio_path=tmp_path / "sample.wav",
        model_name="parakeet-tdt-0.6b-v3",
        device="cpu",
        compute_type="int8",
        runs=1,
        beam_size=1,
        language=None,
        vad_filter=False,
        warmup=False,
        threads=0,
    )

    module._cancel_requested.clear()
    check = seen.get("cancel_check")
    assert callable(check), "the benchmark run was started without a cancel check"
    assert check() is False
    module._cancel_requested.set()
    try:
        assert check() is True, "the cancel check is not wired to the Ctrl+C flag"
    finally:
        module._cancel_requested.clear()


def test_a_failing_cancel_hook_still_closes_the_constructed_runtime(
    monkeypatch, tmp_path
):
    """The install used to sit outside the `try` that owns `close()`.

    The runtime is already constructed at that point -- for a local model that
    is a multi-gigabyte object and, for the Node runtime, a child process -- so
    a setter that raised leaked both for the life of the benchmark.
    """
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"")
    closed: list[bool] = []

    class FakeTranscriber:
        runtime_device = "cpu"
        runtime_details_text = ""

        def __init__(self, **_kwargs):
            pass

        def set_cancel_check(self, _cancel_check):
            raise RuntimeError("a subclass setter that raises")

        def preload_model(self):
            raise AssertionError("the load must not start after a failed install")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "stt_app.transcriber.local_onnx_asr.LocalOnnxAsrTranscriber",
        FakeTranscriber,
    )

    with pytest.raises(RuntimeError, match="setter that raises"):
        local_benchmark._run_onnx_case(
            audio_path=audio_path,
            model_name="parakeet-tdt-0.6b-v3",
            runs=1,
            language=None,
            warmup=False,
            device="cpu",
            vad_filter=False,
            cancel_check=lambda: False,
        )

    assert closed == [True], "the constructed runtime was leaked"


def test_a_long_faster_whisper_run_can_be_canceled_between_segments(
    monkeypatch, tmp_path
):
    """The run loop polls between runs, which is no help inside one recording.

    `segments` is a generator, so decoding happens as it is consumed -- the
    same place the app's own faster-whisper cancel checks.
    """
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"")
    consumed: list[int] = []
    stop_after = 2

    class FakeSegment:
        def __init__(self, index):
            self.text = f"segment {index}"

    class FakeInfo:
        duration = 10.0
        language = "de"
        language_probability = 0.99

    class FakeWhisperModel:
        model = None

        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, *_args, **_kwargs):
            def _segments():
                for index in range(100):
                    consumed.append(index)
                    yield FakeSegment(index)

            return _segments(), FakeInfo()

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )

    with pytest.raises(local_benchmark.BenchmarkCancelled):
        local_benchmark._run_case(
            audio_path=audio_path,
            model_name="small",
            device="cpu",
            compute_type="int8",
            runs=1,
            beam_size=1,
            language="de",
            vad_filter=False,
            warmup=False,
            threads=0,
            cancel_check=lambda: len(consumed) > stop_after,
        )

    assert len(consumed) <= stop_after + 2, (
        f"the whole recording was decoded before the cancel: {len(consumed)} "
        "segments"
    )


def test_the_download_script_default_follows_the_app_default():
    """A hardcoded model here would silently fetch the wrong one offline.

    `scripts/download_model.py` is the documented way to pre-fetch for an
    air-gapped install, so its default has to be the model that install will
    actually try to use. Driven through `--help` rather than the parser
    object, because that is the text the offline instructions point at.
    """
    from stt_app.config import DEFAULT_MODEL_SIZE, MODEL_REPO_MAP

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "download_model.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        cwd=root,
    )

    assert f"default: {DEFAULT_MODEL_SIZE}" in result.stdout, result.stdout
    assert DEFAULT_MODEL_SIZE in MODEL_REPO_MAP, (
        f"the default model {DEFAULT_MODEL_SIZE!r} is not one the script can "
        "download"
    )


def test_a_second_ctrl_c_is_not_swallowed_by_the_wind_down_wait():
    """One long `join(budget)` would discard every further interrupt.

    On Windows CPython's lock acquire ignores the interrupt flag, so a signal
    is only delivered once the call returns: measured, `join(6.0)` held a
    Ctrl+C sent at 0.5 s until 6.0 s. That is the same deferred-signal defect
    the case was moved off the main thread to avoid, and it was reintroduced
    in the cancel handler.
    """
    module = _load_benchmark_module()
    joins: list[float | None] = []
    real_join = threading.Thread.join

    def _recording_join(self, timeout=None):
        if self.name == "wind-down":
            joins.append(timeout)
        return real_join(self, timeout)

    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, name="wind-down", daemon=True)
    worker.start()
    threading.Thread.join = _recording_join
    try:
        module._join_case_worker(worker, 0.5)
    finally:
        threading.Thread.join = real_join
        stop.set()
        real_join(worker, 5.0)

    assert len(joins) >= 2, f"the wait was not a poll loop: {joins}"
    assert all(
        timeout is not None and timeout <= 0.2 for timeout in joins
    ), f"a single long join swallows further interrupts: {joins}"


def test_a_case_that_finished_as_the_interrupt_arrived_is_kept():
    """It is a measured result; discarding it contradicts what main() prints.

    The ordering is forced rather than raced: the patched join releases the
    case, waits for the worker to store its result, and only then raises.
    """
    module = _load_benchmark_module()
    module._cancel_requested.clear()
    finished = local_benchmark.BenchmarkCase(
        model="small",
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.5,
        runs=[],
    )
    release = threading.Event()

    def _fake_run_case(**_kwargs):
        release.wait(5.0)
        return finished

    module._run_case = _fake_run_case
    real_join = threading.Thread.join
    interrupted = {"done": False}

    def _join_that_interrupts(self, timeout=None):
        if self.name == "benchmark-case" and not interrupted["done"]:
            interrupted["done"] = True
            release.set()
            real_join(self, 5.0)
            raise KeyboardInterrupt
        return real_join(self, timeout)

    threading.Thread.join = _join_that_interrupts
    try:
        case = module._run_case_threaded({"model_name": "small"})
    finally:
        threading.Thread.join = real_join
        release.set()
        cancel_state = module._cancel_requested.is_set()
        module._cancel_requested.clear()

    assert interrupted["done"] is True
    assert case is finished, "a measured case was thrown away with the interrupt"
    # ...and the run still stops: the flag stays set, so the next case is
    # canceled at its first poll.
    assert cancel_state is True


def test_the_isolated_worker_reports_a_cancel_as_an_interrupt():
    """`BenchmarkCancelled` subclasses `RuntimeError`.

    The generic branch would record the user's own cancel as a failed
    benchmark case, which is written to the persistent history.
    """
    module = _load_benchmark_module()

    def _cancel(**_kwargs):
        raise local_benchmark.BenchmarkCancelled("Benchmark canceled.")

    module._run_case = _cancel

    class Queue:
        def __init__(self):
            self.items = []

        def put(self, item):
            self.items.append(item)

    queue = Queue()
    module._run_case_worker({"model_name": "small"}, queue)

    assert queue.items == [{"ok": False, "error": "Interrupted by user."}]


def test_a_new_run_does_not_inherit_a_previous_cancel(monkeypatch):
    """`main()` in one interpreter twice: the flag must not survive."""
    module = _load_benchmark_module()
    module._cancel_requested.set()
    monkeypatch.setattr(sys, "argv", ["benchmark_local.py", "--list-models"])

    module.main()

    assert module._cancel_requested.is_set() is False


def test_the_cli_model_fallback_follows_the_faster_whisper_default():
    """`--models` unset must measure the app's own faster-whisper default."""
    from stt_app.config import DEFAULT_FASTER_WHISPER_MODEL_SIZE

    module = _load_benchmark_module()
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "fallback=[DEFAULT_FASTER_WHISPER_MODEL_SIZE]" in source
    assert '_parse_csv(args.models, fallback=["small"])' not in source
    assert module._parse_csv(None, fallback=[DEFAULT_FASTER_WHISPER_MODEL_SIZE]) == [
        DEFAULT_FASTER_WHISPER_MODEL_SIZE
    ]


def test_an_interrupt_right_after_start_still_cancels_the_worker():
    """The window between `start()` and the poll loop is a few bytecodes wide.

    With `start()` outside the `try`, an interrupt landing there escapes
    without ever setting the cancel flag -- so the worker keeps loading a
    model that nothing will stop, and the flag stays clear for the rest of
    the process.
    """
    module = _load_benchmark_module()
    module._cancel_requested.clear()
    observed: list[bool] = []
    running = threading.Event()

    def _fake_run_case(**_kwargs):
        running.set()
        for _ in range(200):
            if module._cancel_check():
                observed.append(True)
                raise local_benchmark.BenchmarkCancelled("Benchmark canceled.")
            time.sleep(0.01)
        raise AssertionError("the cancel flag never reached the case")

    module._run_case = _fake_run_case
    real_start = threading.Thread.start

    def _start_then_interrupt(self):
        real_start(self)
        if self.name == "benchmark-case":
            running.wait(5.0)
            raise KeyboardInterrupt

    threading.Thread.start = _start_then_interrupt
    try:
        with pytest.raises(KeyboardInterrupt):
            module._run_case_threaded({"model_name": "small"})
    finally:
        threading.Thread.start = real_start
        module._cancel_requested.clear()

    assert observed == [True], (
        "the interrupt escaped without setting the cancel flag, so the worker "
        "was left running"
    )


class _PipeBoundChild:
    """A child that cannot exit until its payload has been read.

    This is the coupling that makes the real deadlock, modelled exactly: a
    `multiprocessing.Queue.put` hands the pickled payload to a feeder thread
    that writes it into an OS pipe, and the child blocks at exit until the
    pipe is drained. Measured with the real classes on this machine, a
    payload of 8 KB still completed and 16 KB never did.
    """

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload: dict[str, object] | None = payload
        self.terminated = False

    # -- the process half ------------------------------------------------
    def is_alive(self) -> bool:
        return self._payload is not None and not self.terminated

    def terminate(self) -> None:
        self.terminated = True

    def join(self, timeout=None) -> None:
        return None

    # -- the queue half --------------------------------------------------
    def get(self, timeout=None):
        if self._payload is None:
            raise queue.Empty
        payload, self._payload = self._payload, None
        return payload


def test_a_large_case_payload_is_read_before_the_child_is_waited_for():
    """Reading the queue only after the child exits deadlocks forever.

    The payload carries every run's full transcript, so a few minutes of audio
    -- or a shorter clip with `--runs 3` -- outgrows the OS pipe buffer, the
    child blocks at exit until the parent drains it, and the parent will not
    drain it until the child exits. The wait had no budget, so the CLI hung
    with no output and no way out but Ctrl+C.
    """
    module = _load_benchmark_module()
    expected = {"ok": True, "case": {"transcript": "x" * 64_000}}
    child = _PipeBoundChild(expected)

    result: dict[str, object] = {}

    def collect() -> None:
        result["payload"] = module._collect_worker_payload(child, child)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    reader.join(timeout=10.0)

    assert not reader.is_alive(), (
        "the payload was never read: the parent is waiting for a child that "
        "cannot exit until the parent reads"
    )
    assert result["payload"] == expected


def test_a_child_that_exits_without_a_payload_reports_no_result():
    """A crashed worker must fall through to the exit-code error, not hang."""
    module = _load_benchmark_module()
    dead = SimpleNamespace(
        is_alive=lambda: False,
        terminate=lambda: None,
        join=lambda timeout=None: None,
    )
    empty_queue = SimpleNamespace(get=_raise_empty)

    assert module._collect_worker_payload(dead, empty_queue) is None


def _raise_empty(timeout=None):
    raise queue.Empty


def test_an_interrupt_while_collecting_terminates_and_reaps_the_child():
    """Ctrl+C must stop the child, not leave it holding a model."""
    module = _load_benchmark_module()
    calls: list[str] = []

    def _interrupting_get(timeout=None):
        raise KeyboardInterrupt

    child = SimpleNamespace(
        is_alive=lambda: True,
        terminate=lambda: calls.append("terminate"),
        join=lambda timeout=None: calls.append(f"join:{timeout}"),
    )

    with pytest.raises(KeyboardInterrupt):
        module._collect_worker_payload(child, SimpleNamespace(get=_interrupting_get))

    assert calls[0] == "terminate", f"the child was not stopped first: {calls}"
    assert len(calls) >= 2, f"the child was never reaped: {calls}"
    assert all(
        call.startswith("join:") and float(call.split(":")[1]) <= 0.2
        for call in calls[1:]
    ), f"the wind-down was a single long join, which defers a second Ctrl+C: {calls}"
