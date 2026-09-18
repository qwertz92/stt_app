from __future__ import annotations

import pytest

from stt_app.config import (
    DEVICE_AWARE_LOCAL_MODELS,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_WEBGPU_AUTO_DEVICE_ORDER,
    LOCAL_WEBGPU_MODEL_SIZES,
    ONNX_MEASURABLE_DEVICES,
    effective_preferred_device,
    nemotron_provider_order,
    order_with_preferred_device,
)


def test_the_device_aware_models_are_the_two_runtimes_that_read_a_device():
    """The Settings picker, the stored map and the benchmark must agree on the
    set, and each defined its own before this."""
    assert DEVICE_AWARE_LOCAL_MODELS == (
        LOCAL_WEBGPU_MODEL_SIZES + LOCAL_NEMOTRON_MODEL_SIZES
    )


@pytest.mark.parametrize(
    ("preferred", "expected"),
    [
        ("cpu", ("cpu", "webgpu", "dml")),
        ("dml", ("dml", "webgpu", "cpu")),
        # Already first: the order is returned unchanged rather than rebuilt.
        ("webgpu", ("webgpu", "dml", "cpu")),
        # Not a member, empty, and garbage all leave the chain alone.
        ("cuda", ("webgpu", "dml", "cpu")),
        ("", ("webgpu", "dml", "cpu")),
        (None, ("webgpu", "dml", "cpu")),
    ],
)
def test_order_with_preferred_device_moves_only_a_member_to_the_front(
    preferred, expected
):
    assert (
        order_with_preferred_device(LOCAL_WEBGPU_AUTO_DEVICE_ORDER, preferred)
        == expected
    )


def test_order_with_preferred_device_keeps_every_device_exactly_once():
    """A reorder that dropped or duplicated a fallback would silently change
    which devices `auto` can still reach."""
    reordered = order_with_preferred_device(LOCAL_WEBGPU_AUTO_DEVICE_ORDER, "cpu")

    assert sorted(reordered) == sorted(LOCAL_WEBGPU_AUTO_DEVICE_ORDER)


@pytest.mark.parametrize(
    ("policy", "preferred", "expected"),
    [
        ("auto", "cpu", "cpu"),
        ("auto", "dml", "dml"),
        # Equal to the default first device: nothing to say, so no note and no
        # `--prefer` on the command line.
        ("auto", "webgpu", ""),
        ("auto", "cuda", ""),
        ("auto", "", ""),
        # A pinned device always wins; the measurement must not reorder it.
        ("cpu", "dml", ""),
        ("gpu", "cpu", ""),
        ("dml", "cpu", ""),
        ("webgpu", "cpu", ""),
    ],
)
def test_effective_preferred_device_speaks_only_for_the_auto_policy(
    policy, preferred, expected
):
    assert (
        effective_preferred_device(
            policy, preferred, LOCAL_WEBGPU_AUTO_DEVICE_ORDER
        )
        == expected
    )


def test_effective_preferred_device_normalizes_case_and_padding():
    """The value reaches here from a settings file and from a Node argument."""
    assert (
        effective_preferred_device("auto", " CPU ", LOCAL_WEBGPU_AUTO_DEVICE_ORDER)
        == "cpu"
    )


def test_nemotron_auto_starts_with_the_measured_device():
    """ORT GenAI has DirectML and CPU only, so the only thing a measurement can
    say here is "start with CPU"."""
    assert nemotron_provider_order("auto") == ("dml", "cpu")
    assert nemotron_provider_order("auto", "cpu") == ("cpu", "dml")
    # DirectML already leads the chain, and WebGPU is not a provider this
    # runtime has at all.
    assert nemotron_provider_order("auto", "dml") == ("dml", "cpu")
    assert nemotron_provider_order("auto", "webgpu") == ("dml", "cpu")


@pytest.mark.parametrize("policy", ["gpu", "dml", "webgpu", "cpu", "nonsense"])
def test_a_pinned_nemotron_policy_ignores_the_measurement(policy):
    assert nemotron_provider_order(policy, "cpu") == nemotron_provider_order(policy)


def test_the_measurable_devices_are_the_ones_a_case_can_resolve_to():
    """A stored preference may only hold a device a benchmark case can report,
    which is also what every `auto` chain is built from."""
    assert set(LOCAL_WEBGPU_AUTO_DEVICE_ORDER) <= set(ONNX_MEASURABLE_DEVICES)
    assert set(nemotron_provider_order("auto")) <= set(ONNX_MEASURABLE_DEVICES)
