"""
"Hey JARVIS" — the only part of the system that has to work with the wifi off.
openWakeWord ships a pretrained `hey_jarvis` model and runs it in about 2.5ms
per 80ms frame, so this costs roughly 3% of one core to listen forever.

Two jobs, and they want different thresholds. Waiting from idle is permissive:
missing a wake word is the worst failure this project has, and the cost of a
false positive is a ring that lights up for nothing. Listening *while our own
speakers are running* is the opposite — a false positive there cuts JARVIS off
mid-sentence for no reason, so the bar goes up to 0.9.

The state event is published before anything else happens. Perceived
responsiveness is the ring going cyan, not the recording starting; the budget
for it is 300ms and almost all of that is the model, not us.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import TYPE_CHECKING

import numpy as np

from jarvis.config import CONFIG, Config

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from jarvis.audio.vad import FrameSource

log = logging.getLogger("jarvis.wake")


def strip_wake_phrase(text: str, config: Config = CONFIG) -> str:
    """Remove a leading "Hey JARVIS" from a transcript.

    Only at the front, and only once: "tell Sarah hey jarvis is working" should
    survive intact, and so should a question that happens to say the name.
    """
    return re.sub(config.wake.transcript_prefix, "", text, count=1, flags=re.IGNORECASE).strip()


class WakeWord:
    """The `hey_jarvis` detector, shared between the idle wait and barge-in."""

    def __init__(self, config: Config = CONFIG) -> None:
        self._config = config
        self._wake = config.wake
        self._model = None
        self._lock = threading.Lock()
        self._last_trigger = 0.0

    @property
    def name(self) -> str:
        return self._wake.model

    def load(self) -> None:
        """Build the ONNX session. ~150ms, so pay it at boot, not on first word."""
        if self._model is not None:
            return
        from openwakeword.model import Model

        started = time.perf_counter()
        self._model = Model(
            wakeword_models=[self._wake.model],
            inference_framework=self._wake.inference_framework,
        )
        log.info(
            "wake word %s loaded in %.0fms",
            self._wake.model,
            (time.perf_counter() - started) * 1000,
        )

    def score(self, frame: NDArray[np.int16]) -> float:
        """Score one 80ms frame. Frames must be multiples of 80ms at 16kHz."""
        if self._model is None:
            self.load()
        assert self._model is not None
        with self._lock:
            return float(self._model.predict(frame)[self._wake.model])

    def reset(self) -> None:
        with self._lock:
            if self._model is not None:
                self._model.reset()

    def _accept(self, score: float, threshold: float) -> bool:
        """Debounce: one trigger a second, and clear the model's history."""
        if score < threshold:
            return False
        now = time.monotonic()
        if now - self._last_trigger < self._wake.debounce_s:
            return False
        self._last_trigger = now
        self.reset()
        return True

    def check_frame(self, frame: NDArray[np.int16], threshold: float | None = None) -> float | None:
        """Score one frame and apply the debounce. The score if it crossed the
        (debounced) threshold, otherwise None.

        The building block `wait()` is made of, and what `main.py` uses to fold
        a push-to-talk press into the same single-reader poll loop rather than
        running a second, uncoordinated consumer against the wake queue.
        """
        bar = self._wake.threshold if threshold is None else threshold
        score = self.score(frame)
        if self._accept(score, bar):
            log.debug("wake word at %.3f", score)
            return score
        return None

    def wait(
        self,
        frames: FrameSource,
        *,
        timeout: float | None = None,
        threshold: float | None = None,
        stop: threading.Event | None = None,
    ) -> float | None:
        """Block until the wake word is heard. Returns its score, or None.

        Blocking on purpose: `main.py` calls this in a thread. Making it async
        would mean either awaiting inside the audio path or hopping threads per
        80ms frame, and both cost more than they buy.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if stop is not None and stop.is_set():
                return None
            if deadline is not None and time.monotonic() > deadline:
                return None
            frame = frames.get(timeout=0.1)
            if frame is None:
                continue
            score = self.check_frame(frame, threshold)
            if score is not None:
                return score

    def watch_for_barge_in(
        self, frames: FrameSource, stop: threading.Event, on_trigger: object = None
    ) -> threading.Thread:
        """Listen at the higher threshold while JARVIS speaks. Non-blocking.

        Returns the thread so the caller can join it. `stop` ends the watch; the
        callback fires on the watcher thread and must do nothing slow — in
        practice it aborts the output stream, which is one call.
        """

        def watch() -> None:
            score = self.wait(frames, threshold=self._wake.barge_in_threshold, stop=stop)
            if score is not None and not stop.is_set():
                log.info("barge-in at %.3f", score)
                if callable(on_trigger):
                    on_trigger()

        thread = threading.Thread(target=watch, daemon=True, name="jarvis-bargein")
        thread.start()
        return thread
