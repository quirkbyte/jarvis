"""
Starting up, as its own concern separate from the turn loop that runs once
started. Boot is a fixed sequence with a real cost at each step — a model
loads, a device opens, a connection is made — and every step publishes real
progress rather than a faked animation, because `hud/static/boot.js` renders
exactly what arrives here.

Split out of `main.py` because "how JARVIS starts" and "how JARVIS has one
conversation" are different questions with different failure modes: a boot
step that hangs blocks everything before a word is spoken, a turn that hangs
blocks one exchange.
"""

from __future__ import annotations

import asyncio

from jarvis.audio import stt as stt_module
from jarvis.brain import Brain


class BootMixin:
    """Mixed into `Jarvis`; expects its config, mic, wake, stt, speaker, bus,
    memory, persona, guard, timers and resume attributes to already exist."""

    async def boot(self) -> None:
        steps = [
            ("OPENING CAPTURE DEVICE", self.mic.start),
            ("LOADING WAKE WORD", self.wake.load),
            ("LOADING ACOUSTIC MODEL", self._load_stt),
            ("WARMING SPEECH ENGINE", self._warm_stt),
            ("OPENING OUTPUT STREAM", self.speaker.start),
            ("WARMING VOICE", self.speaker.warmup),
            ("WAKING THE AGENT", None),
        ]
        for i, (label, action) in enumerate(steps):
            self.bus.publish_boot(label, i / len(steps))
            if action is None:
                await self.wake_agent()
            else:
                await asyncio.to_thread(action)
        self.bus.publish_boot("READY", 1.0)
        self._wake_frames = self.mic.subscribe(seconds=1.0, name="wake")
        # Sized for the follow-up window, where there is no wake word to leak
        # and the lead-in is what stops the first syllable being clipped.
        self._rec_frames = self.mic.subscribe(
            seconds=self.config.vad.pre_roll_ms / 1000, name="record"
        )
        assert self.stt is not None
        facts = len(self.memory.all())
        print(
            f"[boot] wake word: {self.wake.name} | stt: {self.stt.name} "
            f"| tts: {self.speaker.backend} | brain: {self.config.brain.model}"
            f" | memory: {facts} fact{'' if facts == 1 else 's'}"
        )

    def _load_stt(self) -> None:
        self.stt = stt_module.TimedTranscriber(stt_module.build(self.config), self.config)

    def _warm_stt(self) -> None:
        if self.stt is not None and self.config.stt.warmup:
            self.stt.warmup()

    async def wake_agent(self) -> None:
        """Connect the agent and hand it what it already knows about the user."""
        self.persona.set_memory(self.memory.all())
        self.brain = Brain(
            self.config,
            self.bus,
            memory=self.memory,
            timers=self.timers,
            guard=self.guard,
            system_prompt=self.persona.prompt(),
            resume=self.resume,
        )
        await self.brain.connect()
