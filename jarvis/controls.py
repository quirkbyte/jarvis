"""
The surface the HUD drives — `protocol.md`'s `text`, `interrupt` and `ptt`
messages — split out from the state machine in `main.py` because it is a
different concern: that file is how JARVIS listens and thinks, this is how
something *other than a microphone* can ask it to.

Every method here calls straight into a path the voice loop already uses.
`answer()` is the same call `run_text()` makes for a typed line; `request_interrupt`
is the same barge-in a spoken "Hey JARVIS" triggers. There is no second
implementation for the HUD to fall out of sync with — mix this into `Jarvis` and
it is done.
"""

from __future__ import annotations

from jarvis.logging import TurnTimer


class ControlsMixin:
    """`hud.server.Controls`, implemented against `Jarvis`'s own attributes.

    Not usable on its own — it expects `self.brain`, `self.bus`, `self.guard`,
    `self.persona`, `self.memory`, `self.speak()` and `self._barge_in()` from
    the class it is mixed into.
    """

    async def answer(self, text: str) -> None:
        """Answer text as though it had just been transcribed."""
        text = text.strip()
        if not text or self.brain is None:
            return
        self.bus.publish_transcript("user", text)
        self.guard.note_reply(text)
        timer = TurnTimer()
        await self.speak(self.brain.ask(text), timer)
        timer.log(text)
        self.persona.set_memory(self.memory.all())

    async def submit_text(self, text: str) -> None:
        """Typed input from the HUD. Only while nothing else is already being
        answered — a HUD is a second surface onto the one conversation, not a
        second conversation."""
        if self._state not in ("idle", "listening"):
            return
        await self.answer(text)

    def request_interrupt(self) -> None:
        """Escape, from the HUD. The same cut a spoken "Hey JARVIS" makes."""
        self._barge_in()

    def push_to_talk(self, down: bool) -> None:
        """Space held, from the HUD — the fastest path to a response, per
        protocol.md, because it skips both the wake word and the silence wait."""
        if down:
            if self._state != "idle" or self._wake_frames is None:
                return  # busy with something else; a press here is ambiguous
            self._ptt_release.clear()
            self._ptt_pressed.set()
        else:
            self._ptt_pressed.clear()
            self._ptt_release.set()
