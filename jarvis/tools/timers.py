"""
Timers that announce themselves.

The requirement that shapes this: a fired timer speaks through the *normal*
speech path, not through a beep or a notification sound. It gets the same voice,
the same chunking, and — importantly — the same interruptibility, so "Hey
JARVIS" over the top of "Tea is up" behaves like every other interruption.

They deliberately do not survive a restart. A timer is a thing about the next
few minutes; something you want to outlive a reboot is a reminder, which is
Phase 4 and belongs in the user's actual Reminders database rather than in a
JSON file that only JARVIS knows about.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import tool

from jarvis import spoken
from jarvis.config import CONFIG, Config

log = logging.getLogger("jarvis.timers")

Announcer = Callable[[str], Awaitable[None]]


@dataclass
class Timer:
    label: str
    seconds: float
    fires_at: float
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def remaining(self) -> float:
        return max(0.0, self.fires_at - time.monotonic())


class TimerService:
    """Live timers, keyed by label. Announces through the caller's speech path."""

    def __init__(self, announce: Announcer, config: Config = CONFIG) -> None:
        self._announce = announce
        self._config = config
        self._timers: dict[str, Timer] = {}

    @property
    def timers(self) -> dict[str, Timer]:
        return dict(self._timers)

    def set(self, seconds: float, label: str) -> str:
        label = (label or "timer").strip().lower()
        seconds = float(seconds)
        if seconds <= 0:
            return "That is not a length of time."
        if seconds > self._config.brain.timer_max_s:
            return "That is too long for a timer; ask me for a reminder instead."
        if label in self._timers:
            self.cancel(label)
        timer = Timer(label, seconds, time.monotonic() + seconds)
        timer.task = asyncio.create_task(self._run(timer), name=f"jarvis-timer-{label}")
        self._timers[label] = timer
        return f"{spoken.duration(seconds)}, for {label}."

    async def _run(self, timer: Timer) -> None:
        try:
            await asyncio.sleep(timer.seconds)
        except asyncio.CancelledError:
            return
        self._timers.pop(timer.label, None)
        phrase = self._config.brain.timer_announcement.format(label=timer.label)
        log.info("timer fired: %s", timer.label)
        try:
            await self._announce(phrase)
        except Exception:  # a failed announcement must not kill the timer loop
            log.exception("could not announce the %s timer", timer.label)

    def cancel(self, label: str) -> str:
        timer = self._timers.pop((label or "").strip().lower(), None)
        if timer is None:
            return f"No timer called {label}."
        if timer.task is not None:
            timer.task.cancel()
        return f"Cancelled the {timer.label} timer."

    def cancel_all(self) -> None:
        for label in list(self._timers):
            self.cancel(label)

    def describe(self) -> str:
        if not self._timers:
            return "No timers running."
        return "; ".join(
            f"{timer.label}, {spoken.duration(timer.remaining)} left"
            for timer in sorted(self._timers.values(), key=lambda t: t.fires_at)
        )


def timer_tools(service: TimerService) -> list[Any]:
    @tool(
        "set_timer",
        "Start a countdown that announces itself out loud when it finishes. Give it "
        "a short label so it can be identified later — 'tea', 'pasta', 'laundry'. "
        "Setting one with an existing label replaces it. For anything that needs to "
        "survive a restart, or that is hours away, use a reminder instead.",
        {"seconds": int, "label": str},
    )
    async def set_timer(args: dict[str, Any]) -> dict[str, Any]:
        return _text(service.set(float(args.get("seconds", 0)), str(args.get("label", "timer"))))

    @tool(
        "list_timers",
        "What timers are running and how long each has left.",
        {},
    )
    async def list_timers(_: dict[str, Any]) -> dict[str, Any]:
        return _text(service.describe())

    @tool(
        "cancel_timer",
        "Stop a running timer by its label.",
        {"label": str},
    )
    async def cancel_timer(args: dict[str, Any]) -> dict[str, Any]:
        return _text(service.cancel(str(args.get("label", ""))))

    return [set_timer, list_timers, cancel_timer]


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}
