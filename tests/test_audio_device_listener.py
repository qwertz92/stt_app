import sys

import pytest

from stt_app import audio_device_listener
from stt_app.audio_device_listener import (
    CHANGE_DEFAULT_DEVICE,
    CHANGE_TOPOLOGY,
    AudioDeviceChangeListener,
)


def test_listener_is_inert_without_com(monkeypatch):
    monkeypatch.setattr(audio_device_listener, "_COM_AVAILABLE", False)
    listener = AudioDeviceChangeListener(on_change=lambda kind: None)

    assert listener.start() is False
    assert listener.is_active is False
    listener.stop()


@pytest.mark.skipif(
    not audio_device_listener._COM_AVAILABLE,
    reason="requires Windows COM and comtypes",
)
def test_notification_client_filters_capture_default_changes():
    seen: list[str] = []
    client = audio_device_listener._NotificationClient(seen.append, None)

    client.OnDefaultDeviceChanged(0, 0, "render-endpoint")
    client.OnDefaultDeviceChanged(
        audio_device_listener._E_CAPTURE, 0, "capture-endpoint"
    )
    client.OnDeviceAdded("endpoint")
    client.OnDeviceRemoved("endpoint")
    client.OnDeviceStateChanged("endpoint", 1)
    client.OnPropertyValueChanged("endpoint", None)

    assert seen == [
        CHANGE_DEFAULT_DEVICE,
        CHANGE_TOPOLOGY,
        CHANGE_TOPOLOGY,
        CHANGE_TOPOLOGY,
    ]


@pytest.mark.skipif(
    not audio_device_listener._COM_AVAILABLE,
    reason="requires Windows COM and comtypes",
)
def test_callback_exceptions_never_cross_the_com_boundary():
    def _raise(_kind: str) -> None:
        raise RuntimeError("boom")

    client = audio_device_listener._NotificationClient(_raise, None)

    client.OnDeviceAdded("endpoint")


@pytest.mark.skipif(
    sys.platform != "win32" or not audio_device_listener._COM_AVAILABLE,
    reason="requires Windows COM and comtypes",
)
def test_listener_registers_and_unregisters_on_windows():
    listener = AudioDeviceChangeListener(on_change=lambda kind: None)
    if not listener.start():
        pytest.skip("MMDevice registration unavailable in this environment")

    assert listener.is_active is True
    assert listener.start() is True  # idempotent

    listener.stop()
    assert listener.is_active is False
