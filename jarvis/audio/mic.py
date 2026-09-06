"""
One capture device, opened once at boot and never closed, fanned out to every
consumer that wants frames.

Two reasons it has to work this way. Opening a capture device costs roughly
200ms, and the moment you need it is the moment you cannot afford to spend it —
the wake word has just fired and the user is already talking. And the wake word
detector must keep listening *while* an utterance is being recorded and *while*
JARVIS is speaking, which is impossible if some other component owns the device.

The sounddevice callback runs on a real-time audio thread. It copies the frame,
pushes it to each subscriber, and returns. Level metering deliberately does not
happen there — the FFT runs on a worker draining its own subscription, so a slow
consumer or a busy CPU can never turn into a glitch in the capture.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from itertools import pairwise
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd

from jarvis.config import CONFIG, Config
from jarvis.events import EventBus

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = logging.getLogger("jarvis.mic")


class NoInputDevice(RuntimeError):
    """There is no microphone to open, or the one there is will not open.

    Its own exception type because this is the single most likely reason a
    perfectly working JARVIS refuses to start — bluetooth headphones go to
    sleep and take the only input device on the machine with them — and it
    deserves an answer rather than a PortAudio traceback.
    """


Frame = "NDArray[np.int16]"


class FrameQueue:
    """A subscriber's bounded mailbox of audio blocks.

    Bounded and lossy on purpose: audio that is 2 seconds stale is worthless to
    every consumer here, and blocking the capture thread to deliver it would be
    worse than dropping it.
    """

    def __init__(self, maxsize: int, name: str) -> None:
        self._frames: deque[NDArray[np.int16]] = deque(maxlen=maxsize)
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self.dropped = 0
        self.closed = False

    def _offer(self, frame: NDArray[np.int16]) -> None:
        with self._arrived:
            if self.closed:
                return
            if len(self._frames) == self._frames.maxlen:
                self.dropped += 1  # deque discards the oldest for us
            self._frames.append(frame)
            self._arrived.notify()

    def get(self, timeout: float | None = None) -> NDArray[np.int16] | None:
        """Block for the next frame. None means timed out, or closed."""
        with self._arrived:
            if not self._frames and not self.closed:
                self._arrived.wait(timeout)
            if self._frames:
                return self._frames.popleft()
            return None

    def clear(self) -> None:
        with self._arrived:
            self._frames.clear()

    def trim(self, keep: int) -> int:
        """Drop all but the newest `keep` frames. Returns how many went.

        Used the moment the wake word fires: the buffered audio behind it is
        the word itself, and recording it means answering "Hey JARVIS" as
        though it were the question.
        """
        with self._arrived:
            dropped = max(0, len(self._frames) - keep)
            for _ in range(dropped):
                self._frames.popleft()
            return dropped

    def close(self) -> None:
        with self._arrived:
            self.closed = True
            self._arrived.notify_all()

    def __len__(self) -> int:
        return len(self._frames)


class MicStream:
    """The always-open capture. Subscribe for frames; it never stops for you."""

    def __init__(self, config: Config = CONFIG, bus: EventBus | None = None) -> None:
        self._config = config
        self._audio = config.audio
        self._bus = bus
        self._stream: sd.InputStream | None = None
        self._subs: tuple[FrameQueue, ...] = ()
        self._lock = threading.Lock()
        self._meter: _LevelMeter | None = None
        self._meter_thread: threading.Thread | None = None
        self.overflows = 0

    @property
    def blocksize(self) -> int:
        return self._audio.block_samples

    def subscribe(self, *, seconds: float = 2.0, name: str = "") -> FrameQueue:
        maxsize = max(2, math.ceil(seconds * 1000 / self._audio.block_ms))
        queue = FrameQueue(maxsize, name or f"mic{len(self._subs)}")
        with self._lock:
            self._subs = (*self._subs, queue)
        return queue

    def unsubscribe(self, queue: FrameQueue) -> None:
        queue.close()
        with self._lock:
            self._subs = tuple(q for q in self._subs if q is not queue)

    def _callback(self, indata: NDArray[np.int16], frames: int, time_info, status) -> None:
        # Real-time thread. Copy, hand off, return. Nothing else belongs here.
        if status:
            self.overflows += 1
        frame = indata[:, 0].copy()
        for queue in self._subs:
            queue._offer(frame)

    def start(self) -> None:
        if self._stream is not None:
            return
        self._require_a_microphone()
        try:
            self._open()
        except sd.PortAudioError as exc:
            raise NoInputDevice(f"the input device would not open: {exc}") from exc

    def _require_a_microphone(self) -> None:
        try:
            inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        except sd.PortAudioError as exc:
            raise NoInputDevice(f"could not list audio devices: {exc}") from exc
        if not inputs:
            raise NoInputDevice("no input device is connected")
        if self._audio.input_device is None and sd.default.device[0] < 0:
            raise NoInputDevice("no input device is selected as the default")

    def _open(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self._audio.sample_rate,
            channels=self._audio.channels,
            dtype="int16",
            blocksize=self.blocksize,
            device=self._audio.input_device,
            callback=self._callback,
        )
        self._stream.start()
        device = sd.query_devices(kind="input")
        log.info(
            "mic open: %s @ %dHz, %dms blocks",
            device["name"],
            self._audio.sample_rate,
            self._audio.block_ms,
        )
        if self._bus is not None:
            self._meter = _LevelMeter(self, self._bus, self._config)
            self._meter_thread = threading.Thread(
                target=self._meter.run, daemon=True, name="jarvis-level"
            )
            self._meter_thread.start()

    def stop(self) -> None:
        if self._meter is not None:
            self._meter.stop()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        for queue in self._subs:
            queue.close()
        self._subs = ()

    def __enter__(self) -> MicStream:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class _LevelMeter:
    """Turns frames into `level` events, off the audio thread.

    The HUD should never do signal processing, so the normalisation happens
    here: amplitude in decibels mapped onto 0..1, because speech sits around
    -30 dBFS and a raw amplitude renders as a flat line.
    """

    def __init__(self, mic: MicStream, bus: EventBus, config: Config) -> None:
        self._mic = mic
        self._bus = bus
        self._audio = config.audio
        self._queue = mic.subscribe(seconds=0.5, name="level")
        self._smoothed = 0.0
        self._running = True
        self.enabled = False  # only while listening or speaking; idle stays quiet
        windows = round(self._audio.block_ms * self._audio.level_hz / 1000)
        self._windows = max(1, windows)

    def stop(self) -> None:
        self._running = False
        self._queue.close()

    def run(self) -> None:
        while self._running:
            frame = self._queue.get(timeout=0.25)
            if frame is None or not self.enabled:
                continue
            for window in np.array_split(frame, self._windows):
                rms, bands = self.analyse(window)
                self._bus.publish_level(rms, bands)

    def analyse(self, window: NDArray[np.int16]) -> tuple[float, list[float]]:
        samples = window.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples**2)))
        self._smoothed += self._audio.level_smoothing * (self._to_unit(rms) - self._smoothed)
        return self._smoothed, self._bands(samples)

    def _to_unit(self, rms: float) -> float:
        floor, ceil = self._audio.level_floor_db, self._audio.level_ceil_db
        db = 20.0 * np.log10(max(rms, 1e-7))
        return float(np.clip((db - floor) / (ceil - floor), 0.0, 1.0))

    def _bands(self, samples: NDArray[np.float32]) -> list[float]:
        """Log-spaced magnitude bins — log because hearing is.

        Scaled back to amplitude (2|X|/sum(w) undoes both the transform length
        and the window's coherent gain) so the bins share the decibel range the
        rms uses. Without that they saturate at 1.0 on any real speech.
        """
        window = np.hanning(len(samples))
        spectrum = 2.0 * np.abs(np.fft.rfft(samples * window)) / max(window.sum(), 1e-9)
        edges = np.geomspace(1, len(spectrum), self._audio.level_bands + 1).astype(int)
        return [
            self._to_unit(float(spectrum[a:b].mean()) if b > a else 0.0) for a, b in pairwise(edges)
        ]
