import ctypes
import sys
from collections import Counter

import pytest
from conftest import make_controller

from stt_app import audio_device_listener
from stt_app.audio_device_listener import (
    CHANGE_DEFAULT_DEVICE,
    CHANGE_TOPOLOGY,
    AudioDeviceChangeListener,
)

# mmdeviceapi.h (Windows SDK 10.0.22621): EDataFlow, ERole, DEVICE_STATE_XXX.
_E_RENDER = 0
_E_CAPTURE = 1
_E_CONSOLE = 0
_DEVICE_STATE_ACTIVE = 0x1

_requires_com = pytest.mark.skipif(
    not audio_device_listener._COM_AVAILABLE,
    reason="requires Windows COM and comtypes",
)


def _default_endpoint_id(flow: int) -> str:
    """The endpoint ID string of this machine's default endpoint for ``flow``.

    Skips the test when the machine has none (a CI runner has no audio
    endpoints). Only the ID is read; no stream is opened.
    """
    import comtypes
    from comtypes import COMMETHOD, GUID, IUnknown

    class IMMDevice(IUnknown):
        # Vtable order from mmdeviceapi.h; only GetId is called.
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD(
                [],
                ctypes.HRESULT,
                "Activate",
                (["in"], ctypes.POINTER(GUID), "iid"),
                (["in"], ctypes.c_ulong, "dwClsCtx"),
                (["in"], ctypes.c_void_p, "pActivationParams"),
                (["out"], ctypes.POINTER(ctypes.c_void_p), "ppInterface"),
            ),
            COMMETHOD(
                [],
                ctypes.HRESULT,
                "OpenPropertyStore",
                (["in"], ctypes.c_ulong, "stgmAccess"),
                (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "ppProperties"),
            ),
            COMMETHOD(
                [],
                ctypes.HRESULT,
                "GetId",
                (["out"], ctypes.POINTER(ctypes.c_wchar_p), "ppstrId"),
            ),
        ]

    try:
        enumerator = comtypes.CoCreateInstance(
            audio_device_listener._CLSID_MMDeviceEnumerator,
            interface=audio_device_listener.IMMDeviceEnumerator,
            clsctx=comtypes.CLSCTX_ALL,
        )
        endpoint = enumerator.GetDefaultAudioEndpoint(flow, _E_CONSOLE)
        return endpoint.QueryInterface(IMMDevice).GetId()
    except Exception as exc:
        pytest.skip(f"no default endpoint for data flow {flow}: {exc}")


def test_listener_is_inert_without_com(monkeypatch):
    monkeypatch.setattr(audio_device_listener, "_COM_AVAILABLE", False)
    listener = AudioDeviceChangeListener(on_change=lambda kind: None)

    assert listener.start() is False
    assert listener.is_active is False
    listener.stop()


@_requires_com
def test_an_output_endpoint_event_leaves_the_warm_microphone_alone():
    """A monitor, HDMI audio or headphone change must not refresh the mic.

    Every added/removed/state-changed endpoint used to start the controller's
    refresh, whatever its data flow: the worker closes the idle warm stream,
    re-initializes PortAudio and reopens it, and a hotkey press in that
    window starts cold -- for an output device the microphone never uses.
    The endpoints here are this machine's real defaults, so the data flow
    comes from the real `IMMEndpoint::GetDataFlow`.
    """
    render_id = _default_endpoint_id(_E_RENDER)
    capture_id = _default_endpoint_id(_E_CAPTURE)
    controller, app = make_controller()
    try:
        client = audio_device_listener._NotificationClient(
            controller.audio_devices_changed.emit, None
        )
        client.OnDeviceStateChanged(render_id, _DEVICE_STATE_ACTIVE)
        client.OnDeviceAdded(render_id)
        client.OnDeviceRemoved(render_id)
        client.OnDefaultDeviceChanged(_E_RENDER, _E_CONSOLE, render_id)
        client.close()  # the lookup thread handles its queue, then ends
        app.processEvents()  # delivers what it emitted to the Qt thread

        assert not controller._audio_device_change_timer.isActive()

        client = audio_device_listener._NotificationClient(
            controller.audio_devices_changed.emit, None
        )
        client.OnDeviceStateChanged(capture_id, _DEVICE_STATE_ACTIVE)
        client.close()
        app.processEvents()

        assert controller._audio_device_change_timer.isActive()
    finally:
        controller.shutdown()


@_requires_com
def test_every_capture_endpoint_event_still_reaches_the_microphone():
    capture_id = _default_endpoint_id(_E_CAPTURE)
    seen: list[str] = []
    client = audio_device_listener._NotificationClient(seen.append, None)

    client.OnDeviceStateChanged(capture_id, _DEVICE_STATE_ACTIVE)
    client.OnDeviceAdded(capture_id)
    client.OnDeviceRemoved(capture_id)
    client.OnDefaultDeviceChanged(_E_CAPTURE, _E_CONSOLE, capture_id)
    client.OnPropertyValueChanged(capture_id, None)
    client.close()

    # The default change is delivered at once, the others by the lookup
    # thread, so only the counts are fixed.
    assert Counter(seen) == {CHANGE_TOPOLOGY: 3, CHANGE_DEFAULT_DEVICE: 1}


@_requires_com
def test_an_endpoint_whose_data_flow_cannot_be_read_still_refreshes():
    """Unknown is treated as a microphone: a missed refresh is the worse error.

    Ignoring it would leave a hot-plugged microphone invisible until the next
    event; refreshing for an output costs one warm-stream reopen.
    """
    seen: list[str] = []
    client = audio_device_listener._NotificationClient(seen.append, None)

    client.OnDeviceAdded("not-an-endpoint-id")
    client.close()

    assert seen == [CHANGE_TOPOLOGY]


@_requires_com
def test_a_failing_receiver_never_crosses_the_com_boundary_or_ends_the_lookup():
    calls: list[str] = []

    def _fail_first(kind: str) -> None:
        calls.append(kind)
        if len(calls) == 1:
            raise RuntimeError("boom")

    client = audio_device_listener._NotificationClient(_fail_first, None)

    client.OnDefaultDeviceChanged(_E_CAPTURE, _E_CONSOLE, "capture-endpoint")
    client.OnDeviceAdded("not-an-endpoint-id")
    client.OnDeviceAdded("not-an-endpoint-id")
    client.close()

    assert calls == [CHANGE_DEFAULT_DEVICE, CHANGE_TOPOLOGY, CHANGE_TOPOLOGY]
    assert not client._lookup._thread.is_alive()


@_requires_com
def test_a_failed_lookup_gets_a_fresh_enumerator_next_time(monkeypatch):
    """One enumerator from before an audio service restart fails every lookup;
    kept, it would silently switch the filter off for the rest of the session.
    """
    created: list[object] = []
    flows = iter([None, _E_RENDER])
    seen: list[str] = []

    def _create():
        created.append(object())
        return created[-1]

    monkeypatch.setattr(audio_device_listener, "_create_enumerator", _create)
    monkeypatch.setattr(
        audio_device_listener, "_data_flow", lambda enumerator, device_id: next(flows)
    )
    client = audio_device_listener._NotificationClient(seen.append, None)

    client.OnDeviceAdded("endpoint-a")
    client.OnDeviceAdded("endpoint-b")
    client.close()

    assert len(created) == 2
    assert seen == [CHANGE_TOPOLOGY]


@pytest.mark.skipif(
    sys.platform != "win32" or not audio_device_listener._COM_AVAILABLE,
    reason="requires Windows COM and comtypes",
)
def test_listener_registers_and_unregisters_on_windows():
    listener = AudioDeviceChangeListener(on_change=lambda kind: None)
    if not listener.start():
        pytest.skip("MMDevice registration unavailable in this environment")
    lookup_thread = listener._client._lookup._thread

    assert listener.is_active is True
    assert listener.start() is True  # idempotent

    listener.stop()
    assert listener.is_active is False
    assert not lookup_thread.is_alive()
