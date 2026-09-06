"""
The output device, behind a ring buffer.

This exists because of a deadlock. The obvious way to play streamed audio is a
blocking `OutputStream.write()` in a loop, and the obvious way to interrupt it
is `abort()` from the watcher thread — but PortAudio does not promise anything
about `Pa_WriteStream` and `Pa_AbortStream` running concurrently on one stream,
and on this Mac the pair hangs indefinitely.

So the producer never touches PortAudio. It appends to a bounded buffer that a
callback drains, which means writing cannot block on the device, interrupting is
a lock and a `clear()`, and barge-in cuts in about a millisecond because the
audio it discards had not been handed to the driver yet.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd

if TYPE_CHECKING:
    from numpy.typing import NDArray


class Playback:
    """A warm output stream fed by a bounded queue of int16 blocks."""

    def __init__(
        self,
        samplerate: int,
        *,
        device: str | None = None,
        blocksize: int = 480,
        buffer_s: float = 0.5,
    ) -> None:
        self.samplerate = samplerate
        self._device = device
        self._blocksize = blocksize
        self._max_samples = int(buffer_s * samplerate)
        self._queue: deque[NDArray[np.int16]] = deque()
        self._queued = 0
        self._lock = threading.Lock()
        self._stream: sd.OutputStream | None = None
        self.underruns = 0

    @property
    def queued_samples(self) -> int:
        return self._queued

    @property
    def active(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        self._stream = sd.OutputStream(
            samplerate=self.samplerate,
            channels=1,
            dtype="int16",
            device=self._device,
            blocksize=self._blocksize,
            callback=self._callback,
        )
        self._stream.start()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        self.clear()
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 - it is on its way out regardless
                pass

    def _callback(self, outdata: NDArray[np.int16], frames: int, time_info, status) -> None:
        # Real-time thread: copy out of the buffer, pad with silence, return.
        filled = 0
        with self._lock:
            while filled < frames and self._queue:
                block = self._queue[0]
                take = min(frames - filled, len(block))
                outdata[filled : filled + take, 0] = block[:take]
                filled += take
                self._queued -= take
                if take == len(block):
                    self._queue.popleft()
                else:
                    self._queue[0] = block[take:]
        if filled < frames:
            outdata[filled:, 0] = 0
            if filled:
                self.underruns += 1

    def write(self, pcm: NDArray[np.int16], abort: threading.Event) -> bool:
        """Queue audio, waiting only for room. False if we were interrupted.

        The wait is against our own buffer, never against the device, so an
        abort during it is noticed within a few milliseconds.
        """
        while True:
            if abort.is_set():
                return False
            with self._lock:
                if self._queued < self._max_samples:
                    self._queue.append(pcm)
                    self._queued += len(pcm)
                    return True
            time.sleep(0.002)

    def drain(self, abort: threading.Event, timeout: float = 30.0) -> bool:
        """Wait for queued audio to finish playing. False if interrupted."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if abort.is_set():
                return False
            with self._lock:
                if not self._queued:
                    break
            time.sleep(0.005)
        # The callback has taken the last block but the device still holds it.
        latency = self._stream.latency if self._stream is not None else 0.0
        time.sleep(min(float(latency or 0.0), 0.3))
        return not abort.is_set()

    def clear(self) -> int:
        """Discard everything not yet handed to the driver. This is barge-in."""
        with self._lock:
            dropped = self._queued
            self._queue.clear()
            self._queued = 0
        return dropped
