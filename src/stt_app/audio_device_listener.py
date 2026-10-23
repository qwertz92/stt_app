"""Event-driven Windows audio endpoint change notifications.

Registers an ``IMMNotificationClient`` with the MMDevice API so the app reacts
immediately when the Windows default capture device changes or a capture
device is connected/removed — no polling; output (render) endpoints are
filtered out here. Callbacks arrive on MMDevice API worker
threads, so the supplied ``on_change`` callable must be thread-safe (the
controller forwards it into a Qt signal). When comtypes or the COM stack is
unavailable (non-Windows dev environments), the listener stays inert and
``start()`` returns False; device changes then surface only through the
watchdog self-heal and manual refresh paths.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from collections.abc import Callable
from typing import ClassVar

# ``on_change`` kinds. "default": the Windows default capture device changed.
# "topology": an endpoint that is not known to be an output was added/removed
# or changed state -- a capture endpoint, or one whose data flow could not be
# read. An output's event is dropped: the reaction is not cheap, it closes
# the idle warm microphone, re-initializes PortAudio and reopens it, and a
# recording started meanwhile opens cold.
CHANGE_DEFAULT_DEVICE = "default"
CHANGE_TOPOLOGY = "topology"

# EDataFlow (mmdeviceapi.h).
_E_RENDER = 0
_E_CAPTURE = 1

# How long `stop()` (Qt thread, at shutdown) waits for the endpoint lookup
# thread; it only takes longer if a lookup hangs in the audio service.
_LOOKUP_CLOSE_TIMEOUT_S = 1.0


def _deliver(
    on_change: Callable[[str], None], kind: str, logger: logging.Logger | None
) -> None:
    # Never raises: not across the COM boundary of a callback, and not out
    # of the lookup thread, which would then miss every later event.
    try:
        on_change(kind)
    except Exception:
        if logger is not None:
            logger.exception("Audio device change callback failed")


_COM_AVAILABLE = False
if sys.platform == "win32":
    try:  # pragma: no cover - exercised on Windows only
        import ctypes
        from ctypes import HRESULT, POINTER, c_int
        from ctypes.wintypes import DWORD, LPCWSTR

        import comtypes
        from comtypes import COMMETHOD, GUID, COMObject, IUnknown

        class _PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", DWORD)]

        class IMMNotificationClient(IUnknown):
            _iid_ = GUID("{7991EEC9-7E89-4D85-8390-6C703CEC60C0}")
            # comtypes reads this plain class-level list when it builds
            # the COM vtable.
            _methods_: ClassVar = [
                COMMETHOD(
                    [],
                    HRESULT,
                    "OnDeviceStateChanged",
                    (["in"], LPCWSTR, "pwstrDeviceId"),
                    (["in"], DWORD, "dwNewState"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "OnDeviceAdded",
                    (["in"], LPCWSTR, "pwstrDeviceId"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "OnDeviceRemoved",
                    (["in"], LPCWSTR, "pwstrDeviceId"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "OnDefaultDeviceChanged",
                    (["in"], c_int, "flow"),
                    (["in"], c_int, "role"),
                    (["in"], LPCWSTR, "pwstrDefaultDeviceId"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "OnPropertyValueChanged",
                    (["in"], LPCWSTR, "pwstrDeviceId"),
                    (["in"], _PROPERTYKEY, "key"),
                ),
            ]

        class IMMDeviceEnumerator(IUnknown):
            # Only Register/Unregister are called; the preceding methods exist
            # to keep the vtable layout correct.
            _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            # comtypes reads this plain class-level list when it builds
            # the COM vtable.
            _methods_: ClassVar = [
                COMMETHOD(
                    [],
                    HRESULT,
                    "EnumAudioEndpoints",
                    (["in"], c_int, "dataFlow"),
                    (["in"], DWORD, "dwStateMask"),
                    (["out"], POINTER(POINTER(IUnknown)), "ppDevices"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "GetDefaultAudioEndpoint",
                    (["in"], c_int, "dataFlow"),
                    (["in"], c_int, "role"),
                    (["out"], POINTER(POINTER(IUnknown)), "ppEndpoint"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "GetDevice",
                    (["in"], LPCWSTR, "pwstrId"),
                    (["out"], POINTER(POINTER(IUnknown)), "ppDevice"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "RegisterEndpointNotificationCallback",
                    (["in"], POINTER(IMMNotificationClient), "pClient"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "UnregisterEndpointNotificationCallback",
                    (["in"], POINTER(IMMNotificationClient), "pClient"),
                ),
            ]

        class IMMEndpoint(IUnknown):
            _iid_ = GUID("{1BE09788-6894-4089-8586-9A2A6C265AC5}")
            # comtypes reads this plain class-level list when it builds
            # the COM vtable.
            _methods_: ClassVar = [
                COMMETHOD(
                    [],
                    HRESULT,
                    "GetDataFlow",
                    (["out"], POINTER(c_int), "pDataFlow"),
                ),
            ]

        _CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

        def _create_enumerator():
            return comtypes.CoCreateInstance(
                _CLSID_MMDeviceEnumerator,
                interface=IMMDeviceEnumerator,
                clsctx=comtypes.CLSCTX_ALL,
            )

        def _data_flow(enumerator, device_id: str | None) -> int | None:
            """``EDataFlow`` of an endpoint, or None when it cannot be read."""
            try:
                device = enumerator.GetDevice(device_id)
                return int(device.QueryInterface(IMMEndpoint).GetDataFlow())
            except Exception:
                return None

        class _EndpointLookup:
            """Reads a changed endpoint's data flow off the callback thread.

            Microsoft's rules for the callbacks: never block, never release
            the last reference to an MMDevice object; and their threads'
            COM state is not ours to change. So a callback only queues the
            endpoint ID, and this thread -- in an MTA it enters itself --
            keeps one enumerator for its whole life and releases it here.
            Only a proven output is dropped: a missed refresh leaves a
            hot-plugged microphone invisible, an extra one costs a
            warm-stream reopen.
            """

            def __init__(
                self,
                on_change: Callable[[str], None],
                logger: logging.Logger | None,
            ) -> None:
                self._on_change = on_change
                self._logger = logger
                self._queue: queue.SimpleQueue = queue.SimpleQueue()
                self._thread = threading.Thread(
                    target=self._run,
                    name="stt_app_audio_endpoint_lookup",
                    daemon=True,
                )
                self._thread.start()

            def submit(self, event: str, device_id: str | None) -> None:
                self._queue.put((event, device_id))

            def close(self) -> bool:
                """Handle what is queued, then end; False while still busy."""
                self._queue.put(None)
                self._thread.join(_LOOKUP_CLOSE_TIMEOUT_S)
                return not self._thread.is_alive()

            def _run(self) -> None:
                try:
                    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
                except OSError:
                    # No COM here: every lookup fails, so every event
                    # refreshes, as before this filter existed.
                    entered = False
                else:
                    entered = True
                try:
                    self._serve()
                finally:
                    if entered:
                        comtypes.CoUninitialize()

            def _serve(self) -> None:
                # Its own frame: the enumerator is released when it ends,
                # before `_run` leaves the apartment. Nothing here raises.
                enumerator = None
                while (item := self._queue.get()) is not None:
                    event, device_id = item
                    if enumerator is None:
                        try:
                            enumerator = _create_enumerator()
                        except Exception:
                            enumerator = None
                    flow = (
                        None
                        if enumerator is None
                        else _data_flow(enumerator, device_id)
                    )
                    if flow is None:
                        # A fresh enumerator next time: one from before an
                        # audio service restart fails every lookup.
                        enumerator = None
                    if flow == _E_RENDER:
                        if self._logger is not None:
                            self._logger.info(
                                "audio_device_change_ignored event=%s flow=render",
                                event,
                            )
                        continue
                    _deliver(self._on_change, CHANGE_TOPOLOGY, self._logger)

        class _NotificationClient(COMObject):
            # list from the class dict when it builds the COM vtable.
            _com_interfaces_ = [IMMNotificationClient]  # noqa: RUF012

            def __init__(
                self,
                on_change: Callable[[str], None],
                logger: logging.Logger | None,
            ) -> None:
                super().__init__()
                self._on_change = on_change
                self._logger = logger
                self._lookup = _EndpointLookup(on_change, logger)

            def close(self) -> None:
                """Deliver what is queued and end the lookup thread.

                Called after unregistering, so no callback queues more.
                """
                if not self._lookup.close() and self._logger is not None:
                    self._logger.warning(
                        "audio_endpoint_lookup_busy wait_s=%.1f",
                        _LOOKUP_CLOSE_TIMEOUT_S,
                    )

            def OnDeviceStateChanged(self, pwstrDeviceId, dwNewState):
                self._lookup.submit("state_changed", pwstrDeviceId)

            def OnDeviceAdded(self, pwstrDeviceId):
                self._lookup.submit("added", pwstrDeviceId)

            def OnDeviceRemoved(self, pwstrDeviceId):
                self._lookup.submit("removed", pwstrDeviceId)

            def OnDefaultDeviceChanged(self, flow, role, pwstrDefaultDeviceId):
                if flow == _E_CAPTURE:
                    _deliver(self._on_change, CHANGE_DEFAULT_DEVICE, self._logger)

            def OnPropertyValueChanged(self, pwstrDeviceId, key):
                # Fires frequently (volume, names); irrelevant for routing.
                return None

        _COM_AVAILABLE = True
    except Exception:  # pragma: no cover - depends on the host COM stack
        _COM_AVAILABLE = False


class AudioDeviceChangeListener:
    """Start/stop wrapper around the MMDevice endpoint notification callback."""

    def __init__(
        self,
        on_change: Callable[[str], None],
        logger: logging.Logger | None = None,
    ) -> None:
        self._on_change = on_change
        self._logger = logger
        self._enumerator = None
        self._client = None

    @property
    def is_active(self) -> bool:
        return self._client is not None

    def start(self) -> bool:
        if self._client is not None:
            return True
        if not _COM_AVAILABLE:
            if self._logger is not None:
                self._logger.info(
                    "audio_device_listener_unavailable platform=%s",
                    sys.platform,
                )
            return False
        client = None
        try:  # pragma: no cover - exercised on Windows only
            enumerator = _create_enumerator()
            client = _NotificationClient(self._on_change, self._logger)
            enumerator.RegisterEndpointNotificationCallback(client)
        except Exception:
            if self._logger is not None:
                self._logger.exception("Failed to start audio device listener")
            if client is not None:
                client.close()
            return False
        self._enumerator = enumerator
        self._client = client
        if self._logger is not None:
            self._logger.info("audio_device_listener_started")
        return True

    def stop(self) -> None:
        enumerator = self._enumerator
        client = self._client
        self._enumerator = None
        self._client = None
        if enumerator is None or client is None:
            return
        try:  # pragma: no cover - exercised on Windows only
            enumerator.UnregisterEndpointNotificationCallback(client)
        except Exception:
            if self._logger is not None:
                self._logger.exception("Failed to stop audio device listener")
        client.close()
