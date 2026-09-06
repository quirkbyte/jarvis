"""
Speech out, with three properties that are worth more than the code that
implements them.

The output stream is opened once and kept warm, because opening one costs about
50ms and the moment you need it is the moment the budget has already been spent
on thinking. Interruption discards buffered audio rather than draining it:
barge-in has to cut mid-word, and a polite finish of the sentence reads as the
thing ignoring you. The buffer that gets discarded lives in `output.py`, for
reasons the deadlock in its docstring explains. And the `level` events published
while speaking come from the *outgoing* audio rather than the microphone, which
is what makes the HUD's reactor pulse in time with the voice instead of the room.

The fallback is macOS `say -v Daniel` — British, offline, free, and noticeably
worse. It exists so JARVIS still talks when the network is gone or the quota is
spent. It plays through the system directly rather than through our stream, so
it interrupts by dying rather than by aborting, and the HUD gets no waveform for
it. That is an acceptable price on a path we hope never to take.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from jarvis.audio.output import Playback
from jarvis.config import CONFIG, Config
from jarvis.events import EventBus

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = logging.getLogger("jarvis.tts")


@dataclass
class SpeechResult:
    ttfb_ms: float = 0.0
    samples: int = 0
    aborted: bool = False
    backend: str = ""


class ElevenLabsVoice:
    """Flash v2.5 streaming raw PCM — roughly 100ms to the first bytes."""

    name = "ElevenLabs"

    def __init__(self, config: Config = CONFIG) -> None:
        self._config = config.tts
        self._client = None

    @property
    def available(self) -> bool:
        if self._config.backend != "elevenlabs":
            return False
        return bool(self._config.api_key and self._config.voice_id)

    def connect(self) -> None:
        if self._client is not None:
            return
        from elevenlabs.client import ElevenLabs

        self._client = ElevenLabs(api_key=self._config.api_key)

    def stream(self, text: str) -> Iterator[bytes]:
        self.connect()
        assert self._client is not None
        return self._client.text_to_speech.stream(
            voice_id=self._config.voice_id,
            text=text,
            model_id=self._config.model,
            output_format=self._config.output_format,
            request_options={"timeout_in_seconds": self._config.request_timeout_s},
        )


class SystemVoice:
    """`say -v Daniel`. Plays itself; interrupts by being killed."""

    name = "say"

    def __init__(self, config: Config = CONFIG) -> None:
        self._config = config.tts
        self._process: subprocess.Popen[bytes] | None = None

    def play(self, text: str, abort: threading.Event) -> bool:
        """Speak, unless we were already interrupted. True if it was cut short.

        The check happens on both sides of the spawn: an interrupt that lands
        while the process is starting would otherwise be lost, and the user
        would hear a reply they had already talked over.
        """
        if abort.is_set():
            return True
        self._process = subprocess.Popen(
            [
                "say",
                "-v",
                self._config.fallback_voice,
                "-r",
                str(self._config.fallback_rate_wpm),
                text,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if abort.is_set():
            self._process.kill()
        self._process.wait()
        self._process = None
        return abort.is_set()

    def abort(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()


class Speaker:
    """Owns the output device. One warm stream, interruptible mid-word."""

    def __init__(self, config: Config = CONFIG, bus: EventBus | None = None) -> None:
        self._config = config
        self._tts = config.tts
        self._bus = bus
        self._playback: Playback | None = None
        self._voice = ElevenLabsVoice(config)
        self._fallback = SystemVoice(config)
        self._abort = threading.Event()
        self._lock = threading.Lock()
        self._smoothed = 0.0
        self._window = max(1, int(self._tts.sample_rate / config.audio.level_hz))
        self._residue = np.zeros(0, dtype=np.int16)

    @property
    def muted(self) -> bool:
        return self._tts.backend == "mute"

    @property
    def backend(self) -> str:
        if self.muted:
            return "mute"
        if not self._voice.available:
            return f"{self._fallback.name}({self._tts.fallback_voice})"
        named = self._tts.voice_name
        return f"{self._voice.name}({named})" if named else self._voice.name

    def warmup(self) -> None:
        """Pay for the TLS handshake at boot instead of mid-conversation.

        Measured on this machine: the first request to ElevenLabs takes about
        2.5 seconds to first byte and every one after it takes 200ms. All of
        that is connection setup, and the first request of a session would
        otherwise be the user's first question.
        """
        if self.muted or not self._voice.available:
            return
        self.start()
        started = time.perf_counter()
        try:
            # Drain it rather than breaking out. An abandoned response body
            # leaves the connection unusable for reuse, which costs the *next*
            # request the handshake this call was supposed to have paid for.
            for _ in self._voice.stream("Ready."):
                pass
            log.info("tts connection warm in %.0fms", (time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 - a cold connection is not fatal
            log.warning("tts warmup failed: %s", exc)

    def start(self) -> None:
        """Open the output device now, so the first reply does not pay for it."""
        if self._playback is not None:
            return
        audio = self._config.audio
        self._playback = Playback(
            self._tts.sample_rate,
            device=audio.output_device,
            blocksize=audio.output_block_ms * self._tts.sample_rate // 1000,
            buffer_s=audio.output_buffer_s,
        )
        self._playback.start()

    def _discard_stream(self) -> None:
        """Throw the output device away so the next utterance rebuilds it.

        Bluetooth devices renegotiate their profile and briefly refuse to open
        (PortAudio -9986). Keeping a dead stream turns one bad utterance into a
        session that never speaks again.
        """
        playback, self._playback = self._playback, None
        if playback is not None:
            playback.close()

    def stop(self) -> None:
        self._fallback.abort()
        self._discard_stream()

    def interrupt(self) -> None:
        """Cut immediately. Safe to call from any thread, including a watcher."""
        self._abort.set()
        self._fallback.abort()
        if self._playback is not None:
            # Everything not yet handed to the driver is thrown away. What the
            # user still hears after this is the device's own buffer, which on
            # Bluetooth is a headphone problem and not one we can reach.
            self._playback.clear()

    def say(self, text: str) -> SpeechResult:
        """Speak one chunk. Blocking — `main.py` calls this in a thread."""
        text = text.strip()
        if not text:
            return SpeechResult(backend=self.backend)
        self._abort.clear()
        if self.muted:
            return self._say_muted(text)
        if self._voice.available:
            for attempt in (1, 2):
                result = SpeechResult(backend=self._voice.name)
                try:
                    return self._say_streamed(text, result)
                except Exception as exc:  # noqa: BLE001 - degrading beats silence
                    self._discard_stream()  # a half-open device is worse than none
                    # Retrying a chunk the user has already partly heard speaks
                    # it twice, which sounds like an echo. A clipped sentence is
                    # the better failure.
                    if result.samples:
                        log.warning(
                            "speech failed %ss in (%s) — not repeating it",
                            result.samples / self._tts.sample_rate,
                            exc,
                        )
                        return result
                    if attempt == 1 and not self._abort.is_set():
                        log.warning("speech failed before any audio (%s) — rebuilding", exc)
                        continue
                    log.warning("elevenlabs failed (%s) — falling back to `say`", exc)
                    if self._bus is not None:
                        self._bus.publish_notice("warn", "VOICE FALLBACK · SAY")
        return self._say_system(text)

    def _say_streamed(self, text: str, result: SpeechResult) -> SpeechResult:
        self.start()
        assert self._playback is not None
        with self._lock:
            playback = self._playback
            started = time.perf_counter()
            self._residue = np.zeros(0, dtype=np.int16)
            for chunk in self._voice.stream(text):
                if self._abort.is_set():
                    result.aborted = True
                    break
                if not chunk:
                    continue
                if not result.ttfb_ms:
                    result.ttfb_ms = (time.perf_counter() - started) * 1000
                pcm = np.frombuffer(chunk, dtype=np.int16)
                self._publish_level(pcm)
                if not playback.write(pcm, self._abort):
                    result.aborted = True
                    break
                result.samples += len(pcm)
            if not result.aborted:
                self._publish_level(self._residue, flush=True)
                result.aborted = not playback.drain(self._abort)
            return result

    def _say_muted(self, text: str) -> SpeechResult:
        """Everything except the sound, at roughly the pace of speech.

        The pacing matters: without it a muted run finishes instantly and never
        exercises interruption, which is the behaviour most worth testing.
        """
        seconds = len(text.split()) / max(self._tts.fallback_rate_wpm, 1) * 60
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            if self._abort.is_set():
                return SpeechResult(ttfb_ms=1.0, aborted=True, backend="mute")
            time.sleep(0.01)
        return SpeechResult(
            ttfb_ms=1.0, samples=int(seconds * self._tts.sample_rate), backend="mute"
        )

    def _say_system(self, text: str) -> SpeechResult:
        started = time.perf_counter()
        aborted = self._fallback.play(text, self._abort)
        return SpeechResult(
            ttfb_ms=(time.perf_counter() - started) * 1000,
            aborted=aborted,
            backend=self._fallback.name,
        )

    def _publish_level(self, pcm: NDArray[np.int16], flush: bool = False) -> None:
        """Levels from what we are about to play, not from the microphone."""
        if self._bus is None:
            return
        buffer = np.concatenate([self._residue, pcm]) if len(self._residue) else pcm
        count = len(buffer) // self._window
        for i in range(count):
            self._emit(buffer[i * self._window : (i + 1) * self._window])
        remainder = buffer[count * self._window :]
        if flush and len(remainder):
            self._emit(remainder)
            remainder = remainder[:0]
        self._residue = np.array(remainder, dtype=np.int16)

    def _emit(self, window: NDArray[np.int16]) -> None:
        assert self._bus is not None
        samples = window.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples**2))) if len(samples) else 0.0
        db = 20.0 * np.log10(max(rms, 1e-7))
        floor, ceil = self._config.audio.level_floor_db, self._config.audio.level_ceil_db
        unit = float(np.clip((db - floor) / (ceil - floor), 0.0, 1.0))
        self._smoothed += self._config.audio.level_smoothing * (unit - self._smoothed)
        self._bus.publish_level(self._smoothed)

    def __enter__(self) -> Speaker:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
