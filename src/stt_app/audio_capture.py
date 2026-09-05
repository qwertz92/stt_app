from __future__ import annotations

import io
import logging
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np
import sounddevice as sd

from .audio_devices import (
    SYSTEM_DEFAULT_INPUT_DEVICE,
    AudioSystemUnavailableError,
    InputDeviceNotFoundError,
    NoInputDeviceError,
    input_stream_extra_settings,
    portaudio_guard,
    register_live_stream,
    unregister_live_stream,
)
from .config import AUDIO_BLOCK_DURATION_MS, AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from .persistence import atomic_write_bytes
from .vad import EnergyVad


class AudioCaptureError(RuntimeError):
    """Recording could not be started.

    ``audio_system_unavailable`` marks the one cause the app can repair by
    itself -- PortAudio not answering -- so the controller can trigger a
    re-enumeration instead of leaving every following recording to fail the
    same way.
    """

    def __init__(
        self,
        message: str,
        *,
        audio_system_unavailable: bool = False,
    ) -> None:
        super().__init__(message)
        self.audio_system_unavailable = audio_system_unavailable


def _close_input_stream(
    stream,
    *,
    logger: logging.Logger | None,
    context: str,
    stop_first: bool = True,
) -> None:
    """Best-effort close that never skips close() when stop() fails."""
    if stop_first:
        try:
            stream.stop()
        except Exception:
            if logger is not None:
                logger.exception("Failed to stop %s", context)
    try:
        stream.close()
    except Exception:
        if logger is not None:
            logger.exception("Failed to close %s", context)
    # The stream object is abandoned either way; keeping a failed close
    # registered would block device re-enumeration forever.
    unregister_live_stream(stream)


class WarmMicrophoneStream:
    """Keeps one PortAudio input stream open so recording starts instantly.

    On locked-down machines (EDR/GPO-hooked audio stacks) opening and starting
    an ``InputStream`` can take seconds, and everything spoken before the
    stream runs is lost. With a warm stream the device is opened once; a
    recording merely attaches itself as the consumer of the already-running
    callback, which is effectively instant. The trade-off is that the
    microphone stays open (Windows shows the in-use indicator), which is why
    this is opt-in via the ``keep_microphone_warm`` setting.
    """

    def __init__(
        self,
        sample_rate: int = AUDIO_SAMPLE_RATE,
        channels: int = AUDIO_CHANNELS,
        block_duration_ms: int = AUDIO_BLOCK_DURATION_MS,
        logger: logging.Logger | None = None,
        device_provider: Callable[[], tuple[str, int | None]] | None = None,
        selected_key_provider: Callable[[], str] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = int(sample_rate * block_duration_ms / 1000)
        self._logger = logger
        # Called at every stream open; returns (persisted device key, PortAudio
        # index) so a restart after a device change resolves freshly instead of
        # reusing an index that re-enumeration may have invalidated.
        self._device_provider = device_provider
        # The persisted key alone, read before the resolution above starts.
        # `opening_device_key` is published from it, so a save that changes
        # the microphone while the open is still inside the device query --
        # milliseconds to seconds on the stacks this feature exists for --
        # can tell the open apart from one that resolves the new selection.
        # Published only from the resolved key, it stayed None for the whole
        # of that query and the save asked for no restart: the open finished
        # on the old microphone, `attach` refused it, and every recording
        # cold-opened until the next save.
        self._selected_key_provider = selected_key_provider
        self._lock = threading.Lock()
        self._stream = None
        self._consumer: Callable | None = None
        self._starting = False
        self._generation = 0
        self._opened_device_key: str | None = None
        # The key the open in flight resolved, so a settings save can tell
        # "still opening the microphone I selected" from "opening another".
        self._opening_device_key: str | None = None
        self._pending_restart = False
        self._pending_close = False
        # Set by `close` and never cleared: the controller drops its
        # reference to a stream it closes and builds a fresh one when the
        # feature returns, so an open that passed its gate after `close`
        # would be a microphone nothing references. The generation bump
        # refuses only a reopen that carries a generation; the settings
        # save's retry and the refresh worker's reopen carry none.
        self._closed = False
        # Streams handed off for closing but not yet taken by a closer, and
        # the number a closer is working on right now; `close_if_idle` waits
        # on `_idle` until both are zero and no open is in flight.
        self._retiring: list = []
        self._closes_in_flight = 0
        self._idle = threading.Condition(self._lock)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._stream is not None

    @property
    def opened_device_key(self) -> str | None:
        """Persisted device key the running stream was opened with; None if idle."""
        with self._lock:
            return self._opened_device_key if self._stream is not None else None

    _CLOSE_WAIT_S = 10.0

    @property
    def is_opening(self) -> bool:
        """True while an open is in flight; `opened_device_key` is None then too."""
        with self._lock:
            return self._starting

    @property
    def opening_device_key(self) -> str | None:
        """Device key the open in flight is opening; None when idle.

        `opened_device_key` is None for the whole of an open, so a save that
        compared it with the selected microphone restarted the open on every
        save -- an opacity or hotkey change discarded the in-flight open and
        paid the cold-open latency twice. This is the selected key as the
        open read it (see `selected_key_provider`), so a save can compare
        against what the open is actually going to produce. It is None only
        between the open passing its gate and that read, a couple of
        statements; an open that has not read the settings yet resolves the
        saved ones when it does.
        """
        with self._lock:
            return self._opening_device_key if self._starting else None

    def device_state(self) -> tuple[str | None, str | None, bool]:
        """`(opened_device_key, opening_device_key, is_opening)` in one read.

        Three property reads are three lock acquisitions with gaps between
        them, in which an open can finish or a restart begin, and the caller
        then reasons about a state no moment had.
        """
        with self._lock:
            opened = self._opened_device_key if self._stream is not None else None
            opening = self._opening_device_key if self._starting else None
            return opened, opening, self._starting

    def ensure_started(self, *, generation: int | None = None) -> bool:
        """Open and start the shared stream if needed. Safe off the UI thread.

        ``generation`` is passed by a restart helper and by the retry a
        pending restart schedules: the open is refused once the generation
        has moved since the restart was requested, because whoever bumped it
        -- a newer restart, `close_if_idle` ahead of a device re-enumeration,
        or `close` -- now decides whether a stream runs at all. Without this
        a restart helper reopened the stream *after* `close_if_idle` had
        closed everything for the re-enumeration, so the refresh found a live
        stream and refused; and after `close` had run for a disabled feature,
        which left a microphone open that nothing referenced. The check sits
        under the same lock that sets `_starting`, so a bump lands either
        before it (refused) or after it (the bumper sees `_starting`, waits,
        and closes the stream this open produces).

        The PortAudio guard is taken *before* the gate. The re-enumeration
        holds that guard (`try_refresh_input_devices`), so an open arriving
        during it -- a settings save's retry, a helper's reopen -- waits here
        and then runs its gate against the bumped generation and the fresh
        device list, instead of passing the gate first and opening on the
        list about to be replaced. What the guard is *not* held across is
        the warm stream's close: the device-refresh worker did that for one
        round, and a cold recording start takes this same guard on the Qt
        thread, so the hotkey press froze the UI for the length of a close
        that nothing bounds (see `_refresh_audio_devices_worker`). An open
        that is merely queued for the guard has claimed nothing and is
        invisible to `close_if_idle`; it is `_closed`, checked under the
        lock, that refuses it once `close` has run.
        """
        stream = None
        opened_key = SYSTEM_DEFAULT_INPUT_DEVICE
        # Resolved INSIDE the guard, together with the open. A PortAudio
        # index is only valid until the next re-enumeration -- which is
        # what `try_refresh_input_devices` does, under this same lock,
        # from the device-change worker thread. Resolving outside it left
        # two separate critical sections with a gap between them, so a
        # hot-plug arriving in that gap renumbered the devices and the
        # recording opened whatever now sat at the old index: a different
        # microphone, silently, which is precisely what "never silently
        # record from another device" forbids. The lock is an RLock, so
        # widening the section costs nothing but the query itself.
        with portaudio_guard():
            with self._lock:
                if generation is not None and generation != self._generation:
                    return False
                if self._closed:
                    return False
                if self._stream is not None:
                    return True
                if self._starting:
                    return False
                self._starting = True
                self._opening_device_key = None
                generation = self._generation
            try:
                if self._selected_key_provider is not None:
                    selected = self._selected_key_provider()
                    with self._lock:
                        self._opening_device_key = selected
                device_index: int | None = None
                if self._device_provider is not None:
                    opened_key, device_index = self._device_provider()
                with self._lock:
                    self._opening_device_key = opened_key
                stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="float32",
                    blocksize=self.block_size,
                    device=device_index,
                    extra_settings=input_stream_extra_settings(device_index),
                    callback=self._dispatch,
                )
                try:
                    stream.start()
                except Exception:
                    _close_input_stream(
                        stream,
                        logger=self._logger,
                        context="warm microphone stream after start failure",
                        stop_first=False,
                    )
                    stream = None
                    raise
                register_live_stream(stream)
            except (
                AudioSystemUnavailableError,
                InputDeviceNotFoundError,
                NoInputDeviceError,
            ) as exc:
                if self._logger is not None:
                    self._logger.warning(
                        "Warm microphone stream not started: %s", exc
                    )
            except Exception:
                if self._logger is not None:
                    self._logger.exception("Failed to start warm microphone stream")
            finally:
                with self._lock:
                    self._starting = False
                    self._opening_device_key = None
                    accepted = stream is not None and generation == self._generation
                    if accepted:
                        self._stream = stream
                        self._opened_device_key = opened_key
                    elif stream is not None:
                        # Superseded by a bump during the open. Retired under
                        # the lock so that a `close_if_idle` waiting on this
                        # open finds it and closes it itself. Closed from
                        # here outside the accounting, it was open and
                        # registered after the waiter had already answered
                        # True: the re-enumeration it went on to run refused,
                        # and `ensure_started` opened a second stream beside
                        # the one still closing.
                        self._retiring.append(stream)
                    # A restart requested mid-open bumped the generation, so
                    # the stream above was discarded; honor the request with
                    # a fresh open that re-resolves the device.
                    # `close_if_idle` and `close` clear the flag when they
                    # bump, because their bump means "no stream until the
                    # caller says so".
                    retry = self._pending_restart and self._consumer is None
                    if retry:
                        self._pending_restart = False
                    retry_generation = self._generation
                    self._idle.notify_all()

        if not accepted and stream is not None:
            self._close_retiring()
        if retry:
            return self.ensure_started(generation=retry_generation)
        if accepted and self._logger is not None:
            self._logger.info(
                "warm_microphone_stream_started sample_rate=%d block_size=%d "
                "device=%s",
                self.sample_rate,
                self.block_size,
                opened_key or "default",
            )
        return accepted

    def attach(self, consumer: Callable, expected_device_key: str) -> bool:
        """Route the running stream's audio to ``consumer``; False if not running.

        ``expected_device_key`` is required, and checked here rather than by
        the caller. A warm stream still open on a different device -- a
        previous selection, or a stale default -- must not serve this
        recording, because "never silently record from another device" is the
        rule the whole device-resolution path exists for. `AudioCapture.start`
        used to read `opened_device_key` and then call `attach`, two separate
        acquisitions of this lock with a gap between them; the gap is
        microseconds against a device open that takes milliseconds to seconds,
        so nothing was observed going through it, but the invariant was
        documented as belonging to a function that did not hold it, which is
        exactly the shape a later refactor drops. The system default is
        ``SYSTEM_DEFAULT_INPUT_DEVICE`` (the empty string), which is what both
        `AudioCapture`'s own `device_key` and the warm stream's `opened_key`
        default to -- not ``None``, which only `opened_device_key` reports and
        only while the stream is idle.
        """
        with self._lock:
            if (
                self._stream is None
                or self._consumer is not None
                or self._pending_close
                or self._pending_restart
                or self._opened_device_key != expected_device_key
            ):
                return False
            self._consumer = consumer
            return True

    def detach(self, consumer: Callable) -> None:
        action = None
        restart: Callable[[], None] | None = None
        with self._lock:
            # Bound methods compare equal but are not identical, so use ==.
            if self._consumer == consumer:
                self._consumer = None
                if self._pending_close:
                    self._pending_close = False
                    self._pending_restart = False
                    action = "close"
                elif self._pending_restart:
                    # Under this same hold, not through `request_restart`
                    # after it: released in between, a `close_if_idle` on
                    # the device-refresh thread bumped and closed
                    # everything, and the restart then bumped again on its
                    # own acquisition -- so its helper's reopen carried the
                    # current generation and opened a fresh stream behind
                    # the caller's re-enumeration (forced schedule; 0 hits
                    # in 400 natural trials).
                    self._pending_restart = False
                    restart = self._restart_locked()
        if action == "close":
            self._spawn_or_run(self.close, "stt_app_warm_mic_close", fallback=self.close)
        elif restart is not None:
            restart()

    def request_restart(self) -> None:
        """Close and reopen with a freshly resolved device.

        While a recording is attached the restart is deferred until ``detach``
        so an active capture never loses its audio source mid-recording.

        The stream being retired stays reachable in `_retiring` until a closer
        has actually closed it. It used to be handed to the helper's closure
        and nowhere else, so a helper thread that could not be started left a
        PortAudio stream open and registered with no reference able to close
        it: `close`, `close_if_idle` and `request_close` all found `_stream`
        None, the microphone stayed open for the process lifetime, and every
        device re-enumeration was refused. Reached through `detach` inside
        `AudioCapture.stop`, the same `RuntimeError` also escaped before the
        recording's chunks were drained, so the whole recording was lost.
        """
        with self._lock:
            restart = self._restart_locked()
        if restart is not None:
            restart()

    def _restart_locked(self) -> Callable[[], None] | None:
        """The restart's bookkeeping; the caller holds `_lock`.

        Returns the helper spawn to run *outside* the lock, or None when the
        restart was deferred (a consumer is attached, a close is pending) or
        handed to the open in flight through `_pending_restart`.
        """
        if self._pending_close:
            return None
        if self._consumer is not None:
            self._pending_restart = True
            return None
        self._generation += 1
        generation = self._generation
        stream = self._stream
        self._stream = None
        self._opened_device_key = None
        if self._starting:
            # The in-flight open observes the generation bump, discards
            # its stream, and retries via the pending flag.
            self._pending_restart = True
            return None
        if stream is not None:
            self._retiring.append(stream)
        return lambda: self._spawn_or_run(
            lambda: self._close_and_reopen(generation),
            "stt_app_warm_mic_restart",
            fallback=self._close_retiring,
        )

    def request_close(self) -> None:
        """Close, deferred until an attached recording finishes."""
        with self._lock:
            self._pending_restart = False
            if self._consumer is not None:
                self._pending_close = True
                return
        self._spawn_or_run(self.close, "stt_app_warm_mic_close", fallback=self.close)

    def close_if_idle(self) -> bool:
        """Synchronous close unless a consumer is attached.

        Used before PortAudio re-enumeration, which must not run while this
        stream is open and must not race a recording that is using it. Only
        an attached consumer answers False at once; *another thread's* open
        or close in flight is waited for, and the stream is then closed here,
        because the caller reads True as "the registry is clear" and acts on
        it immediately:

        - An open holds `portaudio_guard()` across construct/start/register,
          so a refresh begun on the strength of a True blocks on that lock,
          then finds the stream registered while it waited and refuses.
          Reproduced with a stubbed `sd.InputStream` whose `start()` blocks:
          `_starting` True, `_stream` None, and the old `close_if_idle()`
          answered True.
        - `request_restart`, `request_close` and a deferred `detach` hand the
          stream to a helper thread, which closes it *outside* the lock. With
          `_stream` already None this method answered True while that close
          was still running, `try_refresh_input_devices` found the stream
          still registered and refused, and a refused refresh is only retried
          on the next recording stop or abort -- so a hot-plugged or newly
          defaulted microphone stayed invisible until the user recorded once
          or pressed Refresh. Routine rather than exotic: a recording stop
          runs `detach` and then arms exactly this refresh.

        `_CLOSE_WAIT_S` bounds that waiting and nothing else: the closes this
        call makes itself run synchronously to completion outside it, so a
        PortAudio stack that takes minutes to close a stream keeps this call
        for that long and then answers True (measured with a 2.4 s close
        against a 0.6 s budget: True after 2.41 s, no busy log). Bounding
        them too would mean returning True with a stream still closing, which
        is the answer this method exists to avoid.

        The generation is bumped *before* the wait, not after it: a restart
        helper that finishes its close while this waits reaches its reopen on
        its own lock acquisition, and the notify does not hand this thread
        the lock first (measured: the helper reopened, this method then waited
        for that open and closed the new stream, and the test timed out
        inside the second close). Bumped first, the helper's reopen is
        refused whichever thread wins (see `ensure_started`). A wait that
        runs out answers False and logs; the caller then defers the refresh
        as it does for an attached consumer -- and because the bump already
        cancelled the helper's reopen, the stream stays closed until that
        deferred refresh runs and reopens it, which is the honest state for
        an audio stack that has not finished a close in ten seconds.

        Its own closes go through `_close_retiring`, i.e. are counted in
        `_closes_in_flight`, and it answers True only once nothing is in
        flight *after* they are done. Closed outside the accounting, a
        second `close_if_idle` -- two device notifications more than the
        settle interval apart, or Settings > Refresh during one -- found
        nothing to wait for and answered True while the first was still
        inside `stream.close()` (measured: `live_stream_count() -> 1`, the
        re-enumeration refused). The loop re-reads `_stream` after every
        close it performs, so an open that lands *during* the close -- the
        guard is free then -- is retired before this answers True. What it
        cannot cover is the gap between its answer and the caller's
        re-enumeration taking the guard; the caller closes a stream
        registered there and re-enumerates once more.
        """
        deadline = time.monotonic() + self._CLOSE_WAIT_S
        with self._lock:
            if self._consumer is not None:
                return False
            self._generation += 1
            # A restart requested during the open in flight would otherwise
            # be honoured by that open's retry, which reads the generation
            # *after* this bump and therefore passes the check -- a fresh
            # stream opened behind the caller's re-enumeration, which the
            # bump exists to make impossible. The deferred refresh reopens.
            self._pending_restart = False
        while True:
            with self._idle:
                if self._consumer is not None:
                    return False
                stream = self._stream
                self._stream = None
                self._opened_device_key = None
                if stream is not None:
                    self._retiring.append(stream)
                if not self._retiring:
                    if not self._starting and not self._closes_in_flight:
                        return True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if self._logger is not None:
                            self._logger.warning(
                                "warm_microphone_stream_busy opening=%s "
                                "closes_in_flight=%d; device re-enumeration "
                                "deferred",
                                self._starting,
                                self._closes_in_flight,
                            )
                        return False
                    self._idle.wait(remaining)
                    continue
            self._close_retiring()

    def close(self) -> None:
        """Close for good; no open passes the gate afterwards.

        Every caller has dropped its reference by the time this runs, so a
        later open -- one parked behind the PortAudio guard while this ran,
        in particular -- would leave a stream registered that nothing can
        close (measured: one live stream after `shutdown()`, and every
        re-enumeration refused for the rest of the session).
        """
        with self._lock:
            self._closed = True
            self._generation += 1
            stream = self._stream
            self._stream = None
            self._consumer = None
            self._opened_device_key = None
            self._pending_restart = False
            self._pending_close = False
            if stream is not None:
                self._retiring.append(stream)
        self._close_retiring()

    def _close_and_reopen(self, generation: int) -> None:
        self._close_retiring()
        self.ensure_started(generation=generation)

    def _close_retiring(self) -> None:
        """Close every retired stream, one at a time, outside the lock.

        Whoever gets to a retired stream first closes it -- a helper, `close`,
        or `close_if_idle` -- and `close_if_idle` waits for the ones a helper
        has already taken, which is what `_closes_in_flight` counts.
        """
        while True:
            with self._lock:
                if not self._retiring:
                    return
                stream = self._retiring.pop(0)
                self._closes_in_flight += 1
            try:
                _close_input_stream(
                    stream,
                    logger=self._logger,
                    context="retired warm microphone stream",
                )
            finally:
                with self._idle:
                    self._closes_in_flight -= 1
                    self._idle.notify_all()

    def _spawn_or_run(
        self,
        target: Callable[[], None],
        name: str,
        *,
        fallback: Callable[[], None],
    ) -> None:
        """Run ``target`` on a helper thread, or ``fallback`` right here.

        `Thread.start` raises when the interpreter cannot create another
        thread. The stream involved must be closed either way, and the
        fallback does that on the calling thread -- slower than the helper
        would have been, but bounded, logged, and never a leaked microphone.
        No reopen is attempted then: an open on the calling thread is what
        the helper exists to avoid, and the next recording cold-opens.
        """
        try:
            self._spawn(target, name)
        except Exception:
            if self._logger is not None:
                self._logger.exception(
                    "Could not start %s; running its close on the calling thread",
                    name,
                )
            fallback()

    @staticmethod
    def _spawn(target: Callable[[], None], name: str) -> None:
        threading.Thread(target=target, name=name, daemon=True).start()

    def _dispatch(self, indata, frames, time_info, status) -> None:
        consumer = self._consumer
        if consumer is None:
            return
        try:
            consumer(indata, frames, time_info, status)
        except Exception:
            if self._logger is not None:
                self._logger.exception("Warm microphone consumer failed")


class AudioCapture:
    def __init__(
        self,
        sample_rate: int = AUDIO_SAMPLE_RATE,
        channels: int = AUDIO_CHANNELS,
        block_duration_ms: int = AUDIO_BLOCK_DURATION_MS,
        vad: EnergyVad | None = None,
        auto_stop_callback=None,
        chunk_callback: Callable[[bytes], None] | None = None,
        logger: logging.Logger | None = None,
        warm_stream: WarmMicrophoneStream | None = None,
        device_key: str = SYSTEM_DEFAULT_INPUT_DEVICE,
        device_resolver: Callable[[], int | None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = int(sample_rate * block_duration_ms / 1000)
        self.vad = vad
        self.auto_stop_callback = auto_stop_callback
        self.chunk_callback = chunk_callback
        self._logger = logger
        self._warm_stream = warm_stream
        self._device_key = device_key
        self._device_resolver = device_resolver

        self._stream = None
        self._warm_attached = False
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._auto_stop_fired = False
        self._callback_failed = False
        self._capture_generation = 0
        self._accepting_audio = False
        self._active_callback: Callable | None = None
        self._callback_count = 0

    @property
    def is_recording(self) -> bool:
        return self._stream is not None or self._warm_attached

    @property
    def callback_count(self) -> int:
        with self._lock:
            return self._callback_count

    @property
    def has_received_audio(self) -> bool:
        return self.callback_count > 0

    @property
    def uses_warm_stream(self) -> bool:
        return self._warm_attached

    def start(self) -> None:
        if self._stream is not None or self._warm_attached:
            return

        with self._lock:
            self._capture_generation += 1
            generation = self._capture_generation
            self._chunks = []
            self._auto_stop_fired = False
            self._accepting_audio = True
            self._callback_count = 0
            # Once per capture, not once per object.
            self._callback_failed = False

        def session_callback(indata, frames, time_info, status) -> None:
            self._on_audio_for_generation(
                generation,
                indata,
                frames,
                time_info,
                status,
            )

        self._active_callback = session_callback
        if self.vad is not None:
            self.vad.reset()

        warm = self._warm_stream
        if (
            warm is not None
            and warm.sample_rate == self.sample_rate
            and warm.block_size == self.block_size
            # The device check lives inside `attach`, under the lock that
            # also publishes the stream, so it cannot be separated from the
            # attach it guards. A warm stream still open on a different
            # (previously selected, or stale-default) device falls through to
            # a cold open on the right one.
            and warm.attach(session_callback, self._device_key)
        ):
            # The shared stream is already running; attaching is instant and
            # audio flows from the very next callback block.
            self._warm_attached = True
            return

        try:
            # Resolved INSIDE the guard, together with the open. A PortAudio
            # index is only valid until the next re-enumeration -- which is
            # what `try_refresh_input_devices` does, under this same lock,
            # from the device-change worker thread. Resolving outside it left
            # two separate critical sections with a gap between them, so a
            # hot-plug arriving in that gap renumbered the devices and the
            # recording opened whatever now sat at the old index: a different
            # microphone, silently, which is precisely what "never silently
            # record from another device" forbids. The lock is an RLock, so
            # widening the section costs nothing but the query itself.
            with portaudio_guard():
                device_index: int | None = None
                if self._device_resolver is not None:
                    device_index = self._device_resolver()
                stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="float32",
                    blocksize=self.block_size,
                    device=device_index,
                    extra_settings=input_stream_extra_settings(device_index),
                    callback=session_callback,
                )
                try:
                    stream.start()
                except Exception:
                    # ``InputStream`` may have opened the device during
                    # construction; close it so PortAudio does not keep the
                    # device handle alive when ``start()`` fails.
                    _close_input_stream(
                        stream,
                        logger=self._logger,
                        context="audio stream after start failure",
                        stop_first=False,
                    )
                    raise
                register_live_stream(stream)
            self._stream = stream
        except (
            AudioSystemUnavailableError,
            InputDeviceNotFoundError,
            NoInputDeviceError,
        ) as exc:
            with self._lock:
                if generation == self._capture_generation:
                    self._accepting_audio = False
                    self._active_callback = None
            raise AudioCaptureError(
                str(exc),
                audio_system_unavailable=isinstance(exc, AudioSystemUnavailableError),
            ) from exc
        except Exception as exc:
            with self._lock:
                if generation == self._capture_generation:
                    self._accepting_audio = False
                    self._active_callback = None
            raise AudioCaptureError(f"Failed to start microphone capture: {exc}") from exc

    def stop(self) -> bytes:
        with self._lock:
            self._accepting_audio = False
            self._capture_generation += 1
            active_callback = self._active_callback
            self._active_callback = None
        stream = self._stream
        self._stream = None
        if self._warm_attached:
            self._warm_attached = False
            # Only detach; the shared warm stream keeps running for the
            # next recording.
            if self._warm_stream is not None and active_callback is not None:
                self._warm_stream.detach(active_callback)

        if stream is not None:
            _close_input_stream(
                stream,
                logger=self._logger,
                context="audio capture stream",
            )

        with self._lock:
            if not self._chunks:
                return b""
            audio = np.concatenate(self._chunks)
            self._chunks = []

        return self._to_wav_bytes(audio)

    def save_wav(self, output_path: Path, wav_bytes: bytes) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(output_path, wav_bytes)

    def _on_audio(self, indata, frames, _time, status) -> None:
        """Process an unscoped callback, retained for direct callers and tests."""
        self._process_audio(indata, frames, status, generation=None)

    def _on_audio_for_generation(
        self,
        generation: int,
        indata,
        frames,
        _time,
        status,
    ) -> None:
        self._process_audio(indata, frames, status, generation=generation)

    def _process_audio(self, indata, frames, status, *, generation: int | None) -> None:
        # sounddevice's callback wrapper catches only CallbackStop/CallbackAbort;
        # any other exception reaches the cffi callback built with
        # `error=paAbort`, which ends the stream at once -- a cold-stream
        # recording then goes silently deaf, with the traceback on a stderr
        # a windowed build does not have. Logged once, because this runs for
        # every block.
        try:
            self._process_audio_unguarded(indata, frames, status, generation=generation)
        except Exception:
            if self._callback_failed:
                return
            self._callback_failed = True
            if self._logger is not None:
                self._logger.exception(
                    "Audio callback failed; later failures are not logged"
                )

    def _process_audio_unguarded(
        self, indata, frames, status, *, generation: int | None
    ) -> None:
        if status and self._logger is not None:
            self._logger.warning("Audio stream status: %s", status)

        data = np.asarray(indata, dtype=np.float32)
        if data.ndim == 2 and data.shape[1] > 1:
            mono = np.mean(data, axis=1)
        else:
            mono = data.reshape(-1)

        with self._lock:
            if generation is not None and (
                not self._accepting_audio or generation != self._capture_generation
            ):
                return
            self._chunks.append(np.copy(mono))
            self._callback_count += 1
            if self.chunk_callback is not None:
                try:
                    self.chunk_callback(self._to_pcm16_bytes(mono))
                except Exception:
                    if self._logger is not None:
                        self._logger.exception("Streaming chunk callback failed")

            if self.vad is None:
                return

            decision = self.vad.process_chunk(mono)
            if (
                decision.should_stop
                and self.auto_stop_callback is not None
                and not self._auto_stop_fired
            ):
                self._auto_stop_fired = True
                try:
                    threading.Thread(
                        target=self.auto_stop_callback,
                        name="stt_app_vad_auto_stop",
                        daemon=True,
                    ).start()
                except RuntimeError:
                    # Latched *after* the thread exists: a start that fails
                    # used to leave the flag set, so auto-stop was silently
                    # off for the rest of the recording. The next block
                    # tries again; the guard above logs the failure once.
                    self._auto_stop_fired = False
                    raise

    def _to_wav_bytes(self, audio: np.ndarray) -> bytes:
        pcm = self._to_pcm16_array(audio)

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(pcm.tobytes())

        return buffer.getvalue()

    def _to_pcm16_array(self, audio: np.ndarray) -> np.ndarray:
        clipped = np.clip(audio, -1.0, 1.0)
        return (clipped * 32767.0).astype(np.int16)

    def _to_pcm16_bytes(self, audio: np.ndarray) -> bytes:
        return self._to_pcm16_array(audio).tobytes()
