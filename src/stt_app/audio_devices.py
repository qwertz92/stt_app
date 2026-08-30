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


class NoInputDeviceError(RuntimeError):
    """Windows itself reports no usable recording device.

    Distinct from a missing *selection*: no microphone can be chosen at all,
    so pointing at the Settings picker would be a dead end.
    """

    def __init__(self) -> None:
        super().__init__(
            "Windows reports no microphone at all. Open Windows Sound "
            "settings -> Input: if no device is listed there either, the "
            "microphone is disabled or its driver is missing, which the app "
            "cannot work around."
        )


class AudioSystemUnavailableError(RuntimeError):
    """PortAudio did not answer, so nothing can be said about the devices.

    Distinct from ``NoInputDeviceError``, which claims Windows itself has no
    recording device and sends the user to the Sound settings. The most likely
    cause is this app's own re-enumeration: ``try_refresh_input_devices``
    terminates PortAudio and returns False when the following initialize
    fails, and from then on every query raises "PortAudio not initialized".
    Measured on a machine with five working microphones, the old code reported
    that as a missing driver the app could not work around -- the opposite of
    the truth, and a dead end for the user. A fresh re-enumeration repairs it,
    which is what the message offers and what the controller triggers.
    """

    def __init__(self) -> None:
        super().__init__(
            "The Windows audio system did not respond, so the microphone "
            "list is unavailable. This usually clears by itself: try again, "
            "or press Refresh next to the microphone in Settings -> Audio & "
            "Recording."
        )


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


def query_input_devices() -> tuple[list[InputDeviceInfo], bool]:
    """``(devices, answered)`` -- whether PortAudio answered at all.

    The two are genuinely different states and must not be merged: an empty
    list because Windows has no microphone needs the Sound settings, while an
    empty list because PortAudio raised needs a re-enumeration. Callers that
    only render a list can keep using ``list_input_devices``.
    """
    host_api_index = _input_host_api_index()
    if host_api_index is None:
        return [], False
    try:
        devices = sd.query_devices()
    except Exception:
        return [], False
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
    return result, True


def list_input_devices() -> list[InputDeviceInfo]:
    """Connected input devices of the preferred host API, first-name-wins.

    Reads PortAudio's current (possibly stale) device list; pair with
    ``try_refresh_input_devices`` to pick up hot-plugged hardware.
    """
    return query_input_devices()[0]


def input_stream_extra_settings(device_index: int | None):
    """Host-API-specific stream settings for opening *device_index*.

    Explicitly selected microphones resolve to WASAPI device indices (one
    entry per endpoint, untruncated names), but WASAPI shared-mode streams
    reject sample rates that differ from the endpoint's shared mix format —
    typically 48 kHz versus the app's 16 kHz capture rate — with
    paInvalidSampleRate (-9997). The MME sound mapper behind the
    system-default path resamples transparently, which is why only explicit
    selections failed. ``WasapiSettings(auto_convert=True)`` turns on
    PortAudio's own sample-rate conversion so an explicit WASAPI selection
    opens like the default path. Returns ``None`` for the default selection
    and for non-WASAPI devices.
    """
    if device_index is None:
        return None
    try:
        device = sd.query_devices(device_index)
        host_api = sd.query_hostapis(int(device.get("hostapi", -1)))
        host_api_name = str(host_api.get("name", ""))
    except Exception:
        return None
    if _WASAPI_NAME_FRAGMENT not in host_api_name.lower():
        return None
    try:
        return sd.WasapiSettings(auto_convert=True)
    except Exception:
        return None


def resolve_input_device(device_name: str) -> int | None:
    """Persisted microphone name -> PortAudio device index for this open.

    ``None`` (for the empty system-default selection) makes sounddevice use
    the PortAudio default input, which on Windows is the MME sound mapper and
    therefore follows the Windows default device at every stream open.
    Indices are only valid until the next re-enumeration, so resolution must
    happen freshly at each stream open, never be cached.
    """
    name = str(device_name or "").strip()
    available, answered = query_input_devices()
    if not answered:
        raise AudioSystemUnavailableError()
    if not available:
        # Without this the default path fails deep inside PortAudio with
        # "Error querying device -1", which says nothing about the real cause.
        raise NoInputDeviceError()
    if not name:
        return None
    for info in available:
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
            # Do not initialize on top of it. `Pa_Initialize`/`Pa_Terminate`
            # are reference-counted, and sounddevice's `_terminate` raises
            # *before* decrementing its own counter -- so continuing here left
            # PortAudio at 2. Every later refresh then terminated 2 -> 1
            # (no real shutdown, no device rescan), initialized 1 -> 2, logged
            # `audio_device_refresh_done` and returned True: hot-plug
            # detection silently dead for the rest of the session while the
            # log and the Settings "Refresh" button both said it worked.
            # PortAudio is still initialized, so leaving the count alone is
            # the consistent state; the caller is told the refresh did not
            # happen.
            return False
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
