import types

import pytest

from stt_app import audio_devices
from stt_app.audio_devices import (
    InputDeviceInfo,
    InputDeviceNotFoundError,
    list_input_devices,
    live_stream_count,
    register_live_stream,
    resolve_input_device,
    try_refresh_input_devices,
    unregister_live_stream,
)


class _FakeSd:
    def __init__(self, hostapis, devices, default_hostapi=0):
        self._hostapis = hostapis
        self._devices = devices
        self.default = types.SimpleNamespace(hostapi=default_hostapi)
        self.terminate_calls = 0
        self.initialize_calls = 0
        self.wasapi_settings_calls = []

    def query_hostapis(self, index=None):
        if index is None:
            return self._hostapis
        return self._hostapis[index]

    def query_devices(self, index=None):
        if index is None:
            return self._devices
        return self._devices[index]

    def WasapiSettings(self, **kwargs):
        self.wasapi_settings_calls.append(kwargs)
        return ("wasapi-settings", tuple(sorted(kwargs.items())))

    def _terminate(self):
        self.terminate_calls += 1

    def _initialize(self):
        self.initialize_calls += 1


def _fake_sd_with_wasapi():
    return _FakeSd(
        hostapis=(
            {"name": "MME"},
            {"name": "Windows WASAPI"},
        ),
        devices=[
            {
                "name": "Microsoft Sound Mapper - Input",
                "hostapi": 0,
                "max_input_channels": 2,
            },
            {
                "name": "Headset Microphone (truncated na",
                "hostapi": 0,
                "max_input_channels": 2,
            },
            {
                "name": "Headset Microphone (Full WASAPI Name)",
                "hostapi": 1,
                "max_input_channels": 2,
            },
            {
                "name": "Speakers (Output Only)",
                "hostapi": 1,
                "max_input_channels": 0,
            },
            {"name": "USB Microphone", "hostapi": 1, "max_input_channels": 1},
            {"name": "USB Microphone", "hostapi": 1, "max_input_channels": 1},
        ],
    )


@pytest.fixture(autouse=True)
def _clean_live_stream_registry(monkeypatch):
    monkeypatch.setattr(audio_devices, "_live_stream_ids", set())


def test_list_prefers_wasapi_filters_inputs_and_dedupes(monkeypatch):
    monkeypatch.setattr(audio_devices, "sd", _fake_sd_with_wasapi())

    devices = list_input_devices()

    assert devices == [
        InputDeviceInfo(name="Headset Microphone (Full WASAPI Name)", index=2),
        InputDeviceInfo(name="USB Microphone", index=4),
    ]


def test_list_falls_back_to_default_host_api_without_wasapi(monkeypatch):
    fake = _FakeSd(
        hostapis=({"name": "ALSA"},),
        devices=[
            {"name": "default", "hostapi": 0, "max_input_channels": 2},
            {"name": "hdmi-out", "hostapi": 0, "max_input_channels": 0},
        ],
    )
    monkeypatch.setattr(audio_devices, "sd", fake)

    devices = list_input_devices()

    assert devices == [InputDeviceInfo(name="default", index=0)]


def test_resolve_empty_selection_means_system_default(monkeypatch):
    monkeypatch.setattr(audio_devices, "sd", _fake_sd_with_wasapi())

    assert resolve_input_device("") is None
    assert resolve_input_device("   ") is None


def test_resolve_matches_device_name(monkeypatch):
    monkeypatch.setattr(audio_devices, "sd", _fake_sd_with_wasapi())

    index = resolve_input_device("Headset Microphone (Full WASAPI Name)")

    assert index == 2


def test_resolve_missing_device_raises_actionable_error(monkeypatch):
    monkeypatch.setattr(audio_devices, "sd", _fake_sd_with_wasapi())

    with pytest.raises(InputDeviceNotFoundError) as excinfo:
        resolve_input_device("Unplugged Mic")

    assert "Unplugged Mic" in str(excinfo.value)
    assert "not connected" in str(excinfo.value)


def test_extra_settings_none_for_default_selection(monkeypatch):
    monkeypatch.setattr(audio_devices, "sd", _fake_sd_with_wasapi())

    assert audio_devices.input_stream_extra_settings(None) is None


def test_extra_settings_enable_wasapi_auto_convert(monkeypatch):
    fake = _fake_sd_with_wasapi()
    monkeypatch.setattr(audio_devices, "sd", fake)

    # Index 2 is the WASAPI headset microphone in the fake device table.
    result = audio_devices.input_stream_extra_settings(2)

    assert result == ("wasapi-settings", (("auto_convert", True),))
    assert fake.wasapi_settings_calls == [{"auto_convert": True}]


def test_extra_settings_none_for_non_wasapi_device(monkeypatch):
    monkeypatch.setattr(audio_devices, "sd", _fake_sd_with_wasapi())

    # Index 1 is the truncated MME entry.
    assert audio_devices.input_stream_extra_settings(1) is None


def test_extra_settings_none_when_device_query_fails(monkeypatch):
    monkeypatch.setattr(audio_devices, "sd", _fake_sd_with_wasapi())

    assert audio_devices.input_stream_extra_settings(99) is None


def test_refresh_refused_while_a_stream_is_live(monkeypatch):
    fake = _fake_sd_with_wasapi()
    monkeypatch.setattr(audio_devices, "sd", fake)
    stream = object()
    register_live_stream(stream)

    assert try_refresh_input_devices() is False
    assert fake.terminate_calls == 0
    assert fake.initialize_calls == 0

    unregister_live_stream(stream)
    assert live_stream_count() == 0
    assert try_refresh_input_devices() is True
    assert fake.terminate_calls == 1
    assert fake.initialize_calls == 1


def test_unregister_unknown_stream_is_a_noop():
    unregister_live_stream(object())

    assert live_stream_count() == 0
