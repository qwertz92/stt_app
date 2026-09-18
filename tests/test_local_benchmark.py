"""Tests for the pure parts of `local_benchmark` that need no measurement."""

from __future__ import annotations

import math

import pytest

from stt_app.config import MEASURED_DEVICE_MIN_GAIN
from stt_app.local_benchmark import (
    BenchmarkCase,
    BenchmarkRun,
    measured_fastest_devices,
)

_COHERE = "cohere-transcribe-03-2026"
_GRANITE = "granite-speech-4.1-2b"
_NEMOTRON = "nemotron-3.5-asr-streaming-0.6b-int4"


def _case(
    model: str,
    device: str,
    *rtfs: float,
    error: str | None = None,
) -> BenchmarkCase:
    """One measured case whose `avg_rtf` is the mean of ``rtfs``."""
    return BenchmarkCase(
        model=model,
        device=device,
        compute_type="onnx-q4",
        download_seconds=0.0,
        load_seconds=1.0,
        runs=[
            BenchmarkRun(
                run_index=index + 1,
                seconds=rtf * 10.0,
                audio_duration_seconds=10.0,
                real_time_factor=rtf,
                transcript_chars=4,
                transcript_words=1,
                detected_language="de",
                language_probability=math.nan,
            )
            for index, rtf in enumerate(rtfs)
        ],
        error=error,
    )


def test_the_clearly_faster_device_wins():
    cases = [_case(_COHERE, "webgpu", 0.30), _case(_COHERE, "cpu", 0.10)]

    assert measured_fastest_devices(cases) == {_COHERE: "cpu"}


def test_a_difference_inside_the_noise_keeps_the_default_first_device():
    """Run-to-run noise on one machine measured about 2%, so a 5% gap says
    nothing about which device is quicker -- and acting on it would cost a
    multi-gigabyte model reload for a result the next run reverses."""
    cases = [_case(_COHERE, "webgpu", 0.100), _case(_COHERE, "cpu", 0.095)]

    assert measured_fastest_devices(cases) == {_COHERE: "webgpu"}


@pytest.mark.parametrize(
    ("factor", "expected"),
    [
        # Exactly the required gain counts, one step short of it does not.
        (1.0 - MEASURED_DEVICE_MIN_GAIN, "cpu"),
        (1.0 - MEASURED_DEVICE_MIN_GAIN - 0.01, "cpu"),
        (1.0 - MEASURED_DEVICE_MIN_GAIN + 0.01, "webgpu"),
    ],
)
def test_the_gain_threshold_is_checked_on_both_sides(factor, expected):
    incumbent = 0.200
    cases = [
        _case(_COHERE, "webgpu", incumbent),
        _case(_COHERE, "cpu", incumbent * factor),
    ]

    assert measured_fastest_devices(cases) == {_COHERE: expected}


def test_the_incumbent_is_the_first_measured_device_of_the_auto_chain():
    """`auto` tries WebGPU, then DirectML, then CPU. With WebGPU not measured
    the thing a preference would have to beat is DirectML, not CPU."""
    cases = [_case(_COHERE, "dml", 0.30), _case(_COHERE, "cpu", 0.10)]

    assert measured_fastest_devices(cases) == {_COHERE: "cpu"}

    # The same two numbers the other way round leave the chain alone.
    swapped = [_case(_COHERE, "dml", 0.10), _case(_COHERE, "cpu", 0.30)]

    assert measured_fastest_devices(swapped) == {_COHERE: "dml"}


def test_one_device_says_nothing_about_the_others():
    """A run on `auto` alone measures one device and cannot compare anything,
    which is why the note tells the user to pick the comparison target."""
    assert measured_fastest_devices([_case(_COHERE, "cpu", 0.10)]) == {}
    assert measured_fastest_devices([]) == {}


def test_the_same_device_measured_twice_is_still_one_device():
    """"All explicit targets" and a GPU comparison can both resolve onto CPU on
    a machine with no usable GPU, and two CPU rows are not a comparison."""
    cases = [_case(_COHERE, "cpu", 0.30), _case(_COHERE, "cpu", 0.10)]

    assert measured_fastest_devices(cases) == {}


def test_the_lowest_measurement_of_a_device_is_the_one_kept():
    """Several runs of one device (a GPU busy on the first pass, a cold cache)
    should be judged by what the device can do, not by its worst attempt."""
    cases = [
        _case(_COHERE, "webgpu", 0.30),
        _case(_COHERE, "webgpu", 0.05),
        _case(_COHERE, "cpu", 0.10),
    ]

    assert measured_fastest_devices(cases) == {_COHERE: "webgpu"}


def test_a_failed_case_never_decides_a_device():
    """An error case stores the *requested* target, not a resolved device, and
    it measured nothing -- so counting it would both invent a comparison and
    read the wrong device name."""
    cases = [
        _case(_COHERE, "webgpu", error="WebGPU session failed"),
        _case(_COHERE, "cpu", 0.10),
    ]

    assert measured_fastest_devices(cases) == {}


@pytest.mark.parametrize(
    ("label", "rtfs"),
    [
        ("no runs at all", ()),
        ("not a number", (math.nan,)),
        ("infinite", (math.inf,)),
        ("zero", (0.0,)),
        ("negative", (-0.5,)),
    ],
)
def test_a_case_without_a_usable_real_time_factor_is_not_a_measurement(label, rtfs):
    cases = [_case(_COHERE, "webgpu", *rtfs), _case(_COHERE, "cpu", 0.10)]

    assert measured_fastest_devices(cases) == {}, label


def test_nemotron_is_measured_on_the_two_devices_it_has():
    cases = [_case(_NEMOTRON, "dml", 0.30), _case(_NEMOTRON, "cpu", 0.10)]

    assert measured_fastest_devices(cases) == {_NEMOTRON: "cpu"}


def test_a_device_a_models_runtime_cannot_reach_is_not_a_measurement():
    """ORT GenAI has no WebGPU provider. A row claiming one can only come from
    a hand-edited history, and storing it would be inert -- worse, it would
    count as the second device and turn a one-device run into a decision."""
    cases = [_case(_NEMOTRON, "webgpu", 0.05), _case(_NEMOTRON, "cpu", 0.10)]

    assert measured_fastest_devices(cases) == {}


@pytest.mark.parametrize("model", ["small", "parakeet-tdt-0.6b-v3", "canary-1b-v2"])
def test_a_model_whose_runtime_takes_no_device_is_skipped(model):
    """faster-whisper picks its own device and onnx-asr is CPU-only, so there
    is no `auto` chain for a preference to reorder."""
    cases = [_case(model, "cpu", 0.30), _case(model, "auto", 0.10)]

    assert measured_fastest_devices(cases) == {}


def test_every_measured_model_of_one_run_gets_its_own_answer():
    """One run measures several models, and they disagree: the Granite encoder
    runs on the GPU where Cohere's does not."""
    cases = [
        _case(_COHERE, "webgpu", 0.30),
        _case(_COHERE, "cpu", 0.10),
        _case(_GRANITE, "webgpu", 0.10),
        _case(_GRANITE, "cpu", 0.90),
        _case("small", "auto", 0.15),
    ]

    assert measured_fastest_devices(cases) == {_COHERE: "cpu", _GRANITE: "webgpu"}


@pytest.mark.parametrize(
    ("label", "cases"),
    [
        ("an empty model name", [_case("", "cpu", 0.1), _case("", "webgpu", 0.2)]),
        (
            "an empty device name",
            [_case(_COHERE, "", 0.1), _case(_COHERE, "webgpu", 0.2)],
        ),
        (
            "a device nothing resolves to",
            [_case(_COHERE, "cuda", 0.1), _case(_COHERE, "webgpu", 0.2)],
        ),
    ],
)
def test_a_name_nothing_recognises_costs_that_row_and_nothing_else(label, cases):
    """The cases also come back from a history file a user can edit, and this
    result is written into `settings.json` -- so an unrecognised name must be
    dropped rather than stored or raised."""
    assert measured_fastest_devices(cases) == {}, label


def test_a_device_name_is_read_the_way_every_other_device_value_is():
    """Stripped and lower-cased, like the settings field and the runner's own
    argument: a history file holding "CPU" describes the same run."""
    cases = [_case(_COHERE, " CPU ", 0.10), _case(_COHERE, "webgpu", 0.30)]

    assert measured_fastest_devices(cases) == {_COHERE: "cpu"}
