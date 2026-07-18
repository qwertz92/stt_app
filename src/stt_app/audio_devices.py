"""Audio input device inventory and PortAudio lifecycle guards.

PortAudio snapshots its device list at initialization, so a microphone that is
connected while the app runs is invisible until PortAudio is re-initialized.
Re-initialization invalidates every open stream, so it must never run while a
stream exists. This module owns both concerns:

- ``list_input_devices``/``resolve_input_device`` translate the persisted
  microphone name (empty string = system default) into a PortAudio device
  index at stream-open time, preferring the WASAPI host API because it lists
  one entry per active endpoint with untruncated names (MME truncates device
  names to 31 characters).
- ``portaudio_guard``/``register_live_stream``/``unregister_live_stream``
  serialize stream opens against ``try_refresh_input_devices`` and track which
  streams are alive, so a re-enumeration is refused instead of tearing down a
  running capture.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import sounddevice as sd

# Persisted value meaning "follow the Windows default input device".
SYSTEM_DEFAULT_INPUT_DEVICE = ""

_WASAPI_NAME_FRAGMENT = "wasapi"

_portaudio_lock = threading.RLock()
_live_streams_lock = threading.Lock()
_live_stream_ids: set[int] = set()


class InputDeviceNotFoundError(RuntimeError):
    """The persisted microphone selection matches no connected device."""

    def __init__(self, device_name: str) -> None:
        super().__init__(
            f"Selected microphone '{device_name}' is not connected. "
            "Reconnect it or choose a different microphone in Settings."
        )
        self.device_name = device_name


@dataclass(frozen=True)
class InputDeviceInfo:
    name: str
    index: int


def portaudio_guard() -> threading.RLock:
    """Lock held while opening a stream so re-enumeration cannot interleave."""
    return _portaudio_lock


def register_live_stream(stream: object) -> None:
    with _live_streams_lock:
        _live_stream_ids.add(id(stream))


def unregister_live_stream(stream: object) -> None:
    with _live_streams_lock:
        _live_stream_ids.discard(id(stream))


def live_stream_count() -> int:
    with _live_streams_lock:
        return len(_live_stream_ids)


def _input_host_api_index() -> int | None:
    """Prefer WASAPI (one entry per endpoint, full names); else the default."""
    try:
        host_apis = sd.query_hostapis()
    except Exception:
        return None
    for index, host_api in enumerate(host_apis):
        name = str(host_api.get("name", "")).lower()
        if _WASAPI_NAME_FRAGMENT in name:
            return index
    try:
        default_index = int(sd.default.hostapi)
    except Exception:
        default_index = -1
    if 0 <= default_index < len(host_apis):
        return default_index
    return 0 if host_apis else None


def list_input_devices() -> list[InputDeviceInfo]:
    """Connected input devices of the preferred host API, first-name-wins.

    Reads PortAudio's current (possibly stale) device list; pair with
    ``try_refresh_input_devices`` to pick up hot-plugged hardware.
    """
    host_api_index = _input_host_api_index()
    if host_api_index is None:
        return []
    try:
        devices = sd.query_devices()
    except Exception:
        return []
    seen: set[str] = set()
    result: list[InputDeviceInfo] = []
    for index, device in enumerate(devices):
        try:
            if int(device.get("hostapi", -1)) != host_api_index:
                continue
            if int(device.get("max_input_channels", 0)) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        name = str(device.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(InputDeviceInfo(name=name, index=index))
    return result


def resolve_input_device(device_name: str) -> int | None:
    """Persisted microphone name -> PortAudio device index for this open.

    ``None`` (for the empty system-default selection) makes sounddevice use
    the PortAudio default input, which on Windows is the MME sound mapper and
    therefore follows the Windows default device at every stream open.
    Indices are only valid until the next re-enumeration, so resolution must
    happen freshly at each stream open, never be cached.
    """
    name = str(device_name or "").strip()
    if not name:
        return None
    for info in list_input_devices():
        if info.name == name:
            return info.index
    raise InputDeviceNotFoundError(name)


def try_refresh_input_devices(logger: logging.Logger | None = None) -> bool:
    """Re-initialize PortAudio so hot-plugged devices become visible.

    Refuses (returns False) while any registered stream is alive because
    ``Pa_Terminate`` would invalidate it. The caller is expected to close the
    warm stream first and retry later when a recording was active.
    """
    with _portaudio_lock:
        live = live_stream_count()
        if live > 0:
            if logger is not None:
                logger.info(
                    "audio_device_refresh_skipped live_streams=%d", live
                )
            return False
        try:
            sd._terminate()
        except Exception:
            if logger is not None:
                logger.exception("PortAudio terminate failed during refresh")
        try:
            sd._initialize()
        except Exception:
            if logger is not None:
                logger.exception("PortAudio initialize failed during refresh")
            return False
        if logger is not None:
            logger.info(
                "audio_device_refresh_done input_devices=%d",
                len(list_input_devices()),
            )
        return True
