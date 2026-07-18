"""Event-driven Windows audio endpoint change notifications.

Registers an ``IMMNotificationClient`` with the MMDevice API so the app reacts
immediately when the Windows default capture device changes or a device is
connected/removed — no polling. Callbacks arrive on MMDevice API worker
threads, so the supplied ``on_change`` callable must be thread-safe (the
controller forwards it into a Qt signal). When comtypes or the COM stack is
unavailable (non-Windows dev environments), the listener stays inert and
``start()`` returns False; device changes then surface only through the
watchdog self-heal and manual refresh paths.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable

# ``on_change`` kinds. "default": the Windows default capture device changed.
# "topology": a device was added/removed or changed state (any data flow; the
# reaction is cheap and coalesced, so capture-only filtering is not worth a
# COM round-trip inside the callback).
CHANGE_DEFAULT_DEVICE = "default"
CHANGE_TOPOLOGY = "topology"

_E_CAPTURE = 1  # EDataFlow.eCapture

_COM_AVAILABLE = False
if sys.platform == "win32":
    try:  # pragma: no cover - exercised on Windows only
        import ctypes
        from ctypes import HRESULT, POINTER, c_int
        from ctypes.wintypes import DWORD, LPCWSTR

        import comtypes
        from comtypes import COMMETHOD, COMObject, GUID, IUnknown

        class _PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", DWORD)]

        class IMMNotificationClient(IUnknown):
            _iid_ = GUID("{7991EEC9-7E89-4D85-8390-6C703CEC60C0}")
            _methods_ = [
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
            _methods_ = [
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

        _CLSID_MMDeviceEnumerator = GUID(
            "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
        )

        class _NotificationClient(COMObject):
            _com_interfaces_ = [IMMNotificationClient]

            def __init__(
                self,
                on_change: Callable[[str], None],
                logger: logging.Logger | None,
            ) -> None:
                super().__init__()
                self._on_change = on_change
                self._logger = logger

            def _emit(self, kind: str) -> None:
                # COM callbacks must never raise across the boundary.
                try:
                    self._on_change(kind)
                except Exception:
                    if self._logger is not None:
                        self._logger.exception(
                            "Audio device change callback failed"
                        )

            def OnDeviceStateChanged(self, pwstrDeviceId, dwNewState):
                self._emit(CHANGE_TOPOLOGY)

            def OnDeviceAdded(self, pwstrDeviceId):
                self._emit(CHANGE_TOPOLOGY)

            def OnDeviceRemoved(self, pwstrDeviceId):
                self._emit(CHANGE_TOPOLOGY)

            def OnDefaultDeviceChanged(self, flow, role, pwstrDefaultDeviceId):
                if flow == _E_CAPTURE:
                    self._emit(CHANGE_DEFAULT_DEVICE)

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
        try:  # pragma: no cover - exercised on Windows only
            enumerator = comtypes.CoCreateInstance(
                _CLSID_MMDeviceEnumerator,
                interface=IMMDeviceEnumerator,
                clsctx=comtypes.CLSCTX_ALL,
            )
            client = _NotificationClient(self._on_change, self._logger)
            enumerator.RegisterEndpointNotificationCallback(client)
        except Exception:
            if self._logger is not None:
                self._logger.exception("Failed to start audio device listener")
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
