"""
The state machine, and the only module that knows about asyncio. Everything in
`jarvis/audio` is blocking, because PortAudio, ONNX, MLX and HTTP streaming all
are; the bridge is `asyncio.to_thread` and it lives here, so no audio module
has to think about it.

    BOOT → IDLE → LISTENING → THINKING → SPEAKING → IDLE
                     ▲                      │
                     └──── follow-up ───────┘

The follow-up window is what makes it feel like a conversation rather than a
vending machine: after a reply it keeps listening for a few seconds without
needing the wake word again.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from collections.abc import AsyncIterator

from jarvis.audio import stt as stt_module
from jarvis.audio.chunker import SentenceChunker
from jarvis.audio.mic import MicStream
from jarvis.audio.tts import Speaker
from jarvis.audio.vad import Endpointer
from jarvis.audio.wake import WakeWord, strip_wake_phrase
from jarvis.boot import BootMixin
from jarvis.brain import Brain
from jarvis.config import CONFIG, Config
from jarvis.controls import ControlsMixin
from jarvis.events import EventBus, get_bus
from jarvis.guard import Guard
from jarvis.logging import TurnTimer, over_budget
from jarvis.persona import Persona
from jarvis.tools import MemoryStore, TimerService

log = logging.getLogger("jarvis.main")


class Jarvis(BootMixin, ControlsMixin):
    def __init__(
        self,
        config: Config = CONFIG,
        bus: EventBus | None = None,
        *,
        resume: str | None = None,
    ) -> None:
        self.config = config
        self.bus = bus or get_bus()
        self.mic = MicStream(config, self.bus)
        self.wake = WakeWord(config)
        self.endpointer = Endpointer(config)
        self.speaker = Speaker(config, self.bus)
        self.stt: stt_module.TimedTranscriber | None = None
        self.memory = MemoryStore(config)
        self.persona = Persona(config)
        self.guard = Guard(config, self.bus)
        self.timers = TimerService(self.announce, config)
        self.brain: Brain | None = None
        self.resume = resume
        self._state = "boot"
        self._stopping = False
        # Handed to every blocking wait, so ctrl-c does not leave a worker
        # thread listening to a microphone that nobody is going to answer.
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_frames = None
        self._rec_frames = None
        # Push-to-talk, driven by the HUD. Pressed asks the idle loop to start a
        # turn without a wake word; released is fed to the endpointer as an
        # immediate end-of-utterance, skipping the silence wait.
        self._ptt_pressed = threading.Event()
        self._ptt_release = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def set_state(self, state: str) -> None:
        if state == self._state:
            return  # protocol.md: on every transition, and only on transitions
        self._state = state
        self.bus.publish_state(state)
        if self.mic._meter is not None:
            # protocol.md: levels while listening or speaking, silence when idle.
            # Speaking publishes from the outgoing audio, so the mic stays quiet.
            self.mic._meter.enabled = state == "listening"

    async def announce(self, phrase: str) -> None:
        """A timer firing, spoken through the ordinary path so it can be cut off."""
        was = self._state
        self.bus.publish_transcript("jarvis", phrase, final=True)
        self.set_state("speaking")
        await asyncio.to_thread(self.speaker.say, phrase)
        self.set_state(was if was in ("idle", "listening") else "idle")

    def shutdown(self) -> None:
        self._stopping = True
        self._stop.set()
        self.timers.cancel_all()
        self.speaker.interrupt()  # cut mid-word rather than finishing politely
        self.speaker.stop()
        self.mic.stop()
        if self.stt is not None:
            self.stt.close()

    # -- the loop ------------------------------------------------------------

    async def run(self) -> None:
        # The console is a transcript, not a log file: flush every line so it
        # stays readable when piped, and so a state change is visible when it
        # happens rather than when the buffer fills.
        sys.stdout.reconfigure(line_buffering=True)
        self._loop = asyncio.get_running_loop()
        self.set_state("boot")
        await self.boot()
        while not self._stopping:
            self.set_state("idle")
            self._wake_frames.clear()
            trigger = await self._wait_for_trigger()
            if trigger is None or self._stopping:
                continue
            timer = TurnTimer()
            lit = time.perf_counter()
            force_end = None
            if trigger == "ptt":
                # No wake phrase was spoken, so nothing to trim off the front —
                # the natural pre-roll is what stops the first word clipping.
                timer.mark("wake", 0.0)
                force_end = self._ptt_release
            else:
                # How far behind live audio we were when the ring lit up: the
                # frames still queued, plus whatever we spend publishing the
                # state. This is the 300ms in BUILD.md, measured because it is
                # perceptual.
                backlog_ms = len(self._wake_frames) * self.config.audio.block_ms
                # The buffered audio behind the trigger is "Hey JARVIS" itself.
                keep = max(1, self.config.vad.wake_pre_roll_ms // self.config.audio.block_ms)
                self._rec_frames.trim(keep)
                timer.mark("wake", backlog_ms + (time.perf_counter() - lit) * 1000)
            self.set_state("listening")  # before anything else
            await self.conversation(timer, force_end=force_end)

    async def _wait_for_trigger(self) -> str | None:
        """The wake word, or a push-to-talk press — whichever comes first.

        Polls one frame at a time rather than handing the frame queue to
        `wake.wait()` on its own thread, so a push-to-talk press can be noticed
        between frames without a second, uncoordinated reader on that queue.
        """
        while not self._stopping:
            if self._ptt_pressed.is_set():
                return "ptt"
            if self._stop.is_set():
                return None
            frame = await asyncio.to_thread(self._wake_frames.get, 0.1)
            if frame is None:
                continue
            if await asyncio.to_thread(self.wake.check_frame, frame) is not None:
                return "voice"
        return None

    async def conversation(
        self, timer: TurnTimer | None = None, *, force_end: threading.Event | None = None
    ) -> None:
        """One wake word (or push-to-talk press), then as many follow-ups as
        the user offers. `force_end` applies only to that first recording."""
        start_timeout = self.config.vad.start_timeout_s
        while not self._stopping:
            turn = timer or TurnTimer()
            timer = None
            spoke = await self.turn(turn, start_timeout, force_end=force_end)
            force_end = None  # only the first recording is push-to-talk driven
            if not spoke:
                return
            self.set_state("listening")
            start_timeout = self.config.vad.follow_up_s

    async def turn(
        self,
        timer: TurnTimer,
        start_timeout: float,
        *,
        force_end: threading.Event | None = None,
    ) -> bool:
        """Record, transcribe, answer. False when the user said nothing."""
        if self._stopping:
            return False
        if force_end is not None:
            force_end.clear()
        utterance = await asyncio.to_thread(
            self.endpointer.record,
            self._rec_frames,
            start_timeout=start_timeout,
            stop=self._stop,
            force_end=force_end,
        )
        if utterance is None:
            return False
        # Everything after this point is what the user is waiting through.
        timer.restart_at(utterance.ended_at)
        timer.mark("vad", utterance.endpoint_ms)

        self.set_state("thinking")
        assert self.stt is not None
        text = await asyncio.to_thread(
            self.stt.transcribe, utterance.audio, self.config.audio.sample_rate
        )
        timer.mark("stt", self.stt.last_ms)
        if not text.strip():
            log.info("nothing transcribable in %dms of audio", utterance.duration_ms)
            return False
        asked = strip_wake_phrase(text, self.config)
        if asked:
            # The guard asked a question out loud; this may be the answer to it.
            self.guard.note_reply(asked)
        if not asked:
            # They said the name and nothing else. Keep listening rather than
            # reading their own wake word back to them.
            log.info("wake word only: %r", text)
            return True
        self.bus.publish_transcript("user", asked)

        assert self.brain is not None
        await self.speak(self.brain.ask(asked), timer)
        timer.log(asked)
        self.persona.set_memory(self.memory.all())
        blown = over_budget(timer, self.config)
        if blown:
            log.warning("over budget: %s", ", ".join(blown))
        return True

    async def speak(self, fragments: AsyncIterator[str], timer: TurnTimer) -> None:
        """Stream the reply into speech, watching for an interruption throughout.

        The state only becomes `speaking` once there is a chunk to speak. An
        empty reply — which the persona asks for when the wake word fires on
        noise — must produce no sound and no flicker at all.
        """
        chunker = SentenceChunker(self.config.tts)
        stream_started = time.perf_counter()
        stop = threading.Event()
        # Text mode has no microphone, so there is nothing to barge in with.
        watcher = None
        if self._wake_frames is not None:
            self._wake_frames.clear()
            watcher = self.wake.watch_for_barge_in(
                self._wake_frames, stop, on_trigger=self._barge_in
            )
        interrupted = False
        try:
            async for fragment in fragments:
                for chunk in chunker.feed(fragment):
                    interrupted = await self._say(chunk, timer, stream_started)
                    if interrupted:
                        return
            for chunk in chunker.flush():
                interrupted = await self._say(chunk, timer, stream_started)
                if interrupted:
                    return
        finally:
            stop.set()
            if watcher is not None:
                watcher.join(timeout=0.5)
            self.bus.publish_transcript("jarvis", "", final=True)
            if interrupted:
                self.set_state("listening")

    def _barge_in(self) -> None:
        """Cut the audio *and* the turn behind it — stopping only the speaker
        leaves the agent calling tools for an answer nobody is listening to."""
        self.speaker.interrupt()
        if self.brain is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.brain.interrupt(), self._loop)

    async def _say(self, chunk: str, timer: TurnTimer, stream_started: float) -> bool:
        """Speak one chunk. True if the user cut in. HUD first, then audio.

        `Speaker.say` clears its own abort flag on entry, so a shutdown has to
        be checked here — otherwise the next chunk starts talking over a ctrl-c.
        """
        if self._stopping:
            return True
        self.set_state("speaking")
        self.bus.publish_transcript("jarvis", chunk, final=False)
        started = time.perf_counter()
        if "chunk" not in timer.stages:
            # The model's first token is not a thing anyone can say out loud.
            # This is the wait for enough of a sentence to be worth speaking.
            ttft = self.brain.last_ttft_ms if self.brain is not None else 0.0
            timer.mark("llm_ttft", ttft)
            timer.mark("chunk", max(0.0, (started - stream_started) * 1000 - ttft))
        result = await asyncio.to_thread(self.speaker.say, chunk)
        if "tts_ttfb" not in timer.stages and result.ttfb_ms:
            timer.mark("tts_ttfb", result.ttfb_ms)
            # The turn ends at the first syllable, not when the reply finishes.
            first_audio = started + result.ttfb_ms / 1000
            timer.mark("total", (first_audio - timer.started) * 1000)
        return result.aborted
