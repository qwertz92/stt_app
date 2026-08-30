import types

import pytest

from stt_app import audio_devices
from stt_app.audio_devices import (
    AudioSystemUnavailableError,
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


def test_resolve_without_any_input_device_names_the_real_cause(monkeypatch):
    """No device at all is a Windows problem, not a wrong selection.

    The default path used to fail deep inside PortAudio with "Error querying
    device -1", which told the user nothing.
    """
    monkeypatch.setattr(
        audio_devices, "query_input_devices", lambda: ([], True)
    )

    with pytest.raises(audio_devices.NoInputDeviceError) as excinfo:
        resolve_input_device("")

    assert "no microphone at all" in str(excinfo.value)
    assert "Sound" in str(excinfo.value)


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


def test_a_failed_terminate_is_not_reported_as_a_successful_refresh(monkeypatch):
    """Continuing past it left PortAudio initialized twice, for ever.

    `Pa_Initialize`/`Pa_Terminate` are reference-counted and sounddevice's
    `_terminate` raises before decrementing its own counter, so initializing
    on top of a failed terminate put the count at 2. Every later refresh then
    terminated 2 -> 1 (no real shutdown, so the device list is never rescanned)
    and initialized 1 -> 2, logged `audio_device_refresh_done` and returned
    True -- hot-plug detection dead for the session, with the log and the
    Settings "Refresh" button both reporting success.
    """
    fake = _fake_sd_with_wasapi()

    def _boom():
        fake.terminate_calls += 1
        raise RuntimeError("Error terminating PortAudio")

    monkeypatch.setattr(fake, "_terminate", _boom, raising=False)
    monkeypatch.setattr(audio_devices, "sd", fake)

    assert try_refresh_input_devices() is False
    assert fake.initialize_calls == 0, (
        "PortAudio was initialized on top of a failed terminate"
    )


def test_unregister_unknown_stream_is_a_noop():
    unregister_live_stream(object())

    assert live_stream_count() == 0


@pytest.mark.parametrize("silent_call", ["query_hostapis", "query_devices"])
def test_a_silent_portaudio_is_not_reported_as_a_missing_driver(
    monkeypatch, silent_call
):
    """An empty list has two causes and they need opposite advice.

    `try_refresh_input_devices` terminates PortAudio and returns False when
    the following initialize fails, and from then on every query raises
    "PortAudio not initialized". Measured on this machine, with five working
    microphones connected, the old code answered that with "Windows reports no
    microphone at all ... the microphone is disabled or its driver is missing,
    which the app cannot work around" -- the opposite of the truth, and it
    sends the user to a Sound settings page that looks perfectly healthy.

    Both queries are driven separately because they are separate arms: a fake
    whose `query_hostapis` already raises never reaches `query_devices`, so it
    cannot tell whether that second arm reports the failure at all.
    """
    fake = _FakeSd(
        hostapis=({"name": "Windows WASAPI"},),
        devices=[{"name": "Headset", "hostapi": 0, "max_input_channels": 1}],
    )

    def _silent(*_args, **_kwargs):
        raise RuntimeError("PortAudio not initialized [PaErrorCode -10000]")

    monkeypatch.setattr(fake, silent_call, _silent)
    monkeypatch.setattr(audio_devices, "sd", fake)

    assert audio_devices.query_input_devices() == ([], False), (
        f"a raising {silent_call} was reported as a device list"
    )

    with pytest.raises(AudioSystemUnavailableError) as excinfo:
        resolve_input_device("")

    message = str(excinfo.value)
    assert "did not respond" in message
    assert "Refresh" in message
    for blaming in ("no microphone at all", "driver is missing", "cannot work around"):
        assert blaming not in message, (
            f"the unavailable-audio message still blames the hardware: {blaming!r}"
        )


def test_a_query_that_answers_with_nothing_still_blames_windows(monkeypatch):
    """The other half: PortAudio answered, and the answer was 'none'.

    Without this the new arm could swallow the real no-device case and send
    everyone to the Refresh button instead of to Windows Sound settings.
    """
    fake = _FakeSd(
        hostapis=({"name": "Windows WASAPI"},),
        devices=[{"name": "Speakers", "hostapi": 0, "max_input_channels": 0}],
    )
    monkeypatch.setattr(audio_devices, "sd", fake)

    assert audio_devices.query_input_devices() == ([], True)

    with pytest.raises(audio_devices.NoInputDeviceError):
        resolve_input_device("")
