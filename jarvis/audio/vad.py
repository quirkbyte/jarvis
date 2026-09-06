"""
Deciding when the user stopped talking, which is the single most felt number in
the whole system. Too short and it cuts people off mid-thought; too long and the
pause reads as the thing having hung. `BUILD.md` budgets 300ms for it and that
is the default, but it is the first knob anyone will want to move —
`JARVIS_VAD_SILENCE_MS`.

WebRTC's detector accepts only 10, 20 or 30ms frames, and the microphone hands
out 80ms blocks, so everything here is a matter of re-slicing 80 into four 20s
and counting. The pre-roll matters as much as the endpoint: the wake word fires
*after* "JARVIS" has been said, and people do not wait — the first syllable of
the real question is already in the past by the time we start recording.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import numpy as np
import webrtcvad

from jarvis.config import CONFIG, Config

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = logging.getLogger("jarvis.vad")


def _dbfs(samples: NDArray[np.int16]) -> float:
    """Level of one sub-frame, in decibels below full scale."""
    rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768.0) ** 2)))
    return 20.0 * np.log10(max(rms, 1e-7))


class FrameSource(Protocol):
    """Anything that hands out int16 blocks — the mic, or a test fixture."""

    def get(self, timeout: float | None = None) -> NDArray[np.int16] | None: ...


@dataclass(frozen=True)
class Utterance:
    audio: NDArray[np.int16]
    speech_ms: int
    duration_ms: int
    reason: str  # why recording stopped: silence | max_length | stalled
    # perf_counter at the last voiced frame. The latency budget in BUILD.md is
    # measured from here — "the user finished their sentence" — and not from the
    # start of the recording, which also contains however long they talked for.
    ended_at: float = 0.0
    endpoint_ms: int = 0

    @property
    def seconds(self) -> float:
        return self.duration_ms / 1000.0


class Endpointer:
    """Records one utterance, ending it when the speaker stops."""

    def __init__(self, config: Config = CONFIG) -> None:
        self._config = config
        self._vad = config.vad
        self._rate = config.audio.sample_rate
        self._sub_samples = self._rate * self._vad.frame_ms // 1000
        self._detector = webrtcvad.Vad(self._vad.aggressiveness)

    def record(
        self,
        frames: FrameSource,
        *,
        start_timeout: float | None = None,
        silence_ms: int | None = None,
        stop: threading.Event | None = None,
        force_end: threading.Event | None = None,
    ) -> Utterance | None:
        """Collect audio until the speaker stops. None if they never started.

        Whatever is already queued when this is called becomes the pre-roll —
        the subscription is sized to hold exactly that much history, so the word
        that was spoken over the wake word survives.

        `force_end` is push-to-talk releasing: protocol.md calls for ending the
        utterance immediately, skipping the silence wait. It does not skip the
        "was there actually any speech" gate below — releasing an empty press
        is not an utterance any more than silence timing out is one.
        """
        wait_for_start = self._vad.start_timeout_s if start_timeout is None else start_timeout
        end_after = self._vad.silence_ms if silence_ms is None else silence_ms
        collected: list[NDArray[np.int16]] = []
        speech_ms = trailing_silence_ms = 0
        peak_db = -120.0
        last_voice_at = time.perf_counter()
        started = False
        deadline = time.monotonic() + wait_for_start
        max_ms = int(self._vad.max_utterance_s * 1000)
        reason = "silence"

        stall_after = max(2.0, end_after / 1000 * 2)
        last_frame_at = time.monotonic()

        while True:
            if stop is not None and stop.is_set():
                return None
            if force_end is not None and force_end.is_set():
                reason = "forced"
                break
            frame = frames.get(timeout=0.25)
            if frame is None:
                now = time.monotonic()
                if not started:
                    if now > deadline:
                        return None
                    continue
                # Speech had started and the frames stopped: the device died, or
                # a fixture ran out. Either way this is the end of the
                # utterance, not a reason to wait forever.
                if now - last_frame_at > stall_after:
                    reason = "stalled"
                    break
                continue
            last_frame_at = time.monotonic()
            collected.append(frame)

            for sub in self._sub_frames(frame):
                level_db = _dbfs(sub)
                peak_db = max(peak_db, level_db)
                floor = max(self._vad.silence_floor_db, peak_db - self._vad.silence_range_db)
                voiced = level_db > floor and self._detector.is_speech(sub.tobytes(), self._rate)
                if voiced:
                    speech_ms += self._vad.frame_ms
                    trailing_silence_ms = 0
                    last_voice_at = time.perf_counter()
                    started = started or speech_ms >= self._vad.min_speech_ms
                elif started:
                    trailing_silence_ms += self._vad.frame_ms

            if started and trailing_silence_ms >= end_after:
                break
            if len(collected) * self._config.audio.block_ms >= max_ms:
                reason = "max_length"
                break
            if not started and time.monotonic() > deadline:
                return None

        if not collected:
            # force_end can fire before a single frame arrived — an
            # instantaneous press-release. Nothing was said.
            return None
        audio = np.concatenate(collected)
        duration_ms = int(len(audio) * 1000 / self._rate)
        if speech_ms < self._vad.min_speech_ms:
            log.debug("discarded %dms with only %dms of speech", duration_ms, speech_ms)
            return None
        endpoint_ms = int((time.perf_counter() - last_voice_at) * 1000)
        return Utterance(audio, speech_ms, duration_ms, reason, last_voice_at, endpoint_ms)

    def _sub_frames(self, frame: NDArray[np.int16]) -> list[NDArray[np.int16]]:
        """80ms in, four 20ms out. webrtcvad accepts nothing else."""
        n = self._sub_samples
        return [frame[i : i + n] for i in range(0, len(frame) - n + 1, n)]

    def speech_ratio(self, audio: NDArray[np.int16]) -> float:
        """Fraction of an existing buffer that is speech. Used by the tests."""
        subs = self._sub_frames(audio)
        if not subs:
            return 0.0
        floor = self._vad.silence_floor_db
        voiced = sum(
            _dbfs(s) > floor and self._detector.is_speech(s.tobytes(), self._rate) for s in subs
        )
        return voiced / len(subs)
