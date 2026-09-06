"""
Drives the HUD through every state with synthetic events. No microphone, no API
key, no agent.

This exists because iterating on visuals through the real voice loop means
saying "Hey JARVIS" a hundred times, and because a renderer that can only be
exercised by speaking cannot be tested at all. Every event type in
`protocol.md` is emitted here at least once.

    python scripts/hud_demo.py              the full cycle, looping
    python scripts/hud_demo.py --hold idle  sit in one state, for screenshots
    python scripts/hud_demo.py --once       one pass, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from jarvis.config import CONFIG  # noqa: E402
from jarvis.events import get_bus  # noqa: E402
from jarvis.hud.server import HudServer  # noqa: E402
from jarvis.hud.telemetry import Telemetry  # noqa: E402

BOOT_STEPS = [
    ("OPENING CAPTURE DEVICE", 0.15),
    ("LOADING WAKE WORD", 0.35),
    ("LOADING ACOUSTIC MODEL", 0.6),
    ("WARMING SPEECH ENGINE", 0.8),
    ("WAKING THE AGENT", 0.95),
    ("READY", 1.0),
]

USER_LINE = "what's my battery at"
REPLY = "Eighty-two percent, and not charging. It should see you through the afternoon."

TOOLS = [
    ("t_01", "mcp__jarvis__recall", "JARVIS · RECALL", "units"),
    ("t_02", "Bash", "SHELL · BASH", "pmset"),
    ("t_03", "WebSearch", "WEB · WEBSEARCH", "weather in London"),
]


class Driver:
    """Emits the same events the real core emits, at the same sort of pace."""

    def __init__(self, bus, rate_hz: float) -> None:
        self.bus = bus
        self.period = 1.0 / rate_hz

    async def levels(self, seconds: float, *, loud: float, speaking: bool) -> None:
        """A plausible envelope: syllables, not a sine wave."""
        end = time.monotonic() + seconds
        phase = 0.0
        while time.monotonic() < end:
            phase += self.period
            syllable = abs(math.sin(phase * 6.5)) ** 1.5
            jitter = random.uniform(0.75, 1.0)
            rms = min(1.0, loud * syllable * jitter)
            bands = [
                min(1.0, rms * (1.15 - abs(i - 2.4) / 7) * random.uniform(0.7, 1.15))
                for i in range(CONFIG.audio.level_bands)
            ]
            self.bus.publish_level(rms, bands)
            await asyncio.sleep(self.period)
        if speaking:
            self.bus.publish_level(0.0, [0.0] * CONFIG.audio.level_bands)

    async def boot(self) -> None:
        self.bus.publish_state("boot")
        for step, progress in BOOT_STEPS:
            self.bus.publish_boot(step, progress)
            await asyncio.sleep(0.32)

    async def turn(self) -> None:
        self.bus.publish_state("listening")
        listening = asyncio.create_task(self.levels(2.2, loud=0.8, speaking=False))
        await asyncio.sleep(1.4)
        self.bus.publish_transcript("user", USER_LINE, final=True)
        await listening

        self.bus.publish_state("thinking")
        for tool_id, name, label, detail in TOOLS[:2]:
            self.bus.publish_tool_start(tool_id, name, label)
            await asyncio.sleep(0.7)
            self.bus.publish_tool_end(tool_id, label, ok=True, detail=detail)
        await asyncio.sleep(0.4)

        self.bus.publish_state("speaking")
        speaking = asyncio.create_task(self.levels(5.0, loud=0.95, speaking=True))
        for fragment in (REPLY[:36], REPLY[36:]):
            self.bus.publish_transcript("jarvis", fragment, final=False)
            await asyncio.sleep(2.4)
        self.bus.publish_transcript("jarvis", "", final=True)
        await speaking

    async def fault(self) -> None:
        self.bus.publish_state("error")
        self.bus.publish_notice("error", "ELEVENLABS UNREACHABLE")
        tool_id, name, label, _ = TOOLS[2]
        self.bus.publish_tool_start(tool_id, name, label)
        await asyncio.sleep(0.8)
        self.bus.publish_tool_end(tool_id, label, ok=False, detail="no route to host")
        await asyncio.sleep(1.6)

    async def cycle(self) -> None:
        await self.boot()
        self.bus.publish_state("idle")
        self.bus.publish_notice("info", "DEMO MODE · SYNTHETIC EVENTS")
        await asyncio.sleep(2.0)
        await self.turn()
        self.bus.publish_state("idle")
        await asyncio.sleep(2.0)
        await self.fault()
        self.bus.publish_state("idle")
        await asyncio.sleep(2.0)

    def _context(self, state: str) -> None:
        """Everything a screenshot of this state should contain.

        Re-stated on a cycle, because only `state` and `telemetry` are replayed
        to a browser that connects late — a transcript published before the HUD
        opened is simply gone, and a screenshot of it would show an empty ribbon.
        """
        self.bus.publish_transcript("user", USER_LINE, final=True)
        if state in ("thinking", "speaking", "error"):
            for tool_id, name, label, detail in TOOLS:
                self.bus.publish_tool_start(tool_id, name, label)
                self.bus.publish_tool_end(
                    tool_id, label, ok=state != "error" or tool_id != TOOLS[2][0], detail=detail
                )
        if state in ("speaking", "error"):
            self.bus.publish_transcript("jarvis", REPLY, final=False)
        if state == "error":
            self.bus.publish_notice("error", "ELEVENLABS UNREACHABLE")

    async def hold(self, state: str) -> None:
        """Sit in one state indefinitely, for looking at and photographing."""
        if state == "boot":
            while True:
                self.bus.publish_state("boot")
                for step, progress in BOOT_STEPS[:4]:
                    self.bus.publish_boot(step, progress)
                    await asyncio.sleep(0.45)
                await asyncio.sleep(2.0)

        self.bus.publish_state("idle")
        self.bus.publish_state(state)
        loud = {"listening": 0.8, "speaking": 0.95}.get(state, 0.0)
        restated = 0.0
        while True:
            if time.monotonic() - restated > 0.5:
                self._context(state)
                restated = time.monotonic()
            if loud:
                await self.levels(1.0, loud=loud, speaking=state == "speaking")
            else:
                await asyncio.sleep(0.4)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="drive the HUD with synthetic events")
    parser.add_argument(
        "--hold", help="sit in one state: boot idle listening thinking speaking error"
    )
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--no-telemetry", action="store_true", help="skip the left panel feed")
    args = parser.parse_args(argv)

    bus = get_bus()
    server = HudServer(bus, CONFIG)
    await server.start()
    telemetry = Telemetry(bus, CONFIG)
    if not args.no_telemetry:
        telemetry.start()

    print(f"hud demo on {server.url}   (ctrl-c to stop)")
    driver = Driver(bus, CONFIG.audio.level_hz)
    try:
        if args.hold:
            await driver.hold(args.hold)
        elif args.once:
            await driver.cycle()
        else:
            while True:
                await driver.cycle()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        telemetry.stop()
        await server.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0) from None
