"""
The spine. Everything JARVIS does that the outside world might want to watch —
state transitions, audio levels, transcript fragments, tool activity — is
published here, and anything that wants to watch subscribes. The HUD is just one
subscriber; a console logger is another; a lamp that pulses with the reactor
would be a third and would require no change to the core.

The hard constraint is that ``publish`` is called from the sounddevice callback,
which runs on a real-time audio thread. So it is synchronous, takes only an
uncontended lock, never awaits, never raises, and never lets a slow consumer
apply backpressure to the microphone. Consumers that fall behind lose their
oldest droppable message instead — a stuttering waveform is fine, a HUD stuck in
the wrong state is not.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import suppress
from typing import Any

from jarvis.config import VALID_STATES

log = logging.getLogger("jarvis.events")

Event = dict[str, Any]

# protocol.md: state, transcript and tool carry meaning that cannot be
# reconstructed from a later message, so they are never dropped first.
DROPPABLE_TYPES = frozenset({"level", "telemetry"})
CRITICAL_TYPES = frozenset({"state", "transcript", "tool", "notice", "boot"})


def is_droppable(event: Event) -> bool:
    return event.get("type") in DROPPABLE_TYPES


class SubscriptionClosed(RuntimeError):
    """Raised by ``get()`` when the subscription is closed and drained."""


def _resolve(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


class Subscription:
    """One consumer's bounded mailbox. Async iterator; closes itself on exit."""

    def __init__(self, bus: EventBus, maxsize: int, name: str) -> None:
        self._bus = bus
        self._maxsize = max(1, maxsize)
        self._name = name
        self._queue: deque[Event] = deque()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiter: asyncio.Future[None] | None = None
        self._closed = False
        self.dropped = 0
        self.dropped_critical = 0

    # -- producer side, any thread -------------------------------------------

    def _offer(self, event: Event) -> None:
        waiter: asyncio.Future[None] | None = None
        loop = None
        with self._lock:
            if self._closed:
                return
            if len(self._queue) >= self._maxsize and not self._evict_locked(event):
                return
            self._queue.append(event)
            waiter, self._waiter = self._waiter, None
            loop = self._loop
        if waiter is not None and loop is not None:
            # Hot path, called from the audio thread: a bare try beats building a
            # context manager. RuntimeError means the loop is closed, i.e. the
            # consumer has gone away and there is nothing to wake.
            try:  # noqa: SIM105
                loop.call_soon_threadsafe(_resolve, waiter)
            except RuntimeError:
                pass

    def _evict_locked(self, incoming: Event) -> bool:
        """Make room. Returns False if ``incoming`` itself should be dropped."""
        for i, queued in enumerate(self._queue):
            if is_droppable(queued):
                del self._queue[i]
                self.dropped += 1
                return True
        if is_droppable(incoming):
            self.dropped += 1
            return False
        # Every queued message is critical. Memory is bounded first: shed the
        # oldest and count it loudly, because this should not happen.
        self._queue.popleft()
        self.dropped += 1
        self.dropped_critical += 1
        return True

    # -- consumer side, the event loop ---------------------------------------

    async def get(self) -> Event:
        while True:
            with self._lock:
                if self._queue:
                    return self._queue.popleft()
                if self._closed:
                    raise SubscriptionClosed(self._name)
                loop = self._loop = asyncio.get_running_loop()
                waiter = self._waiter = loop.create_future()
            try:
                await waiter
            except asyncio.CancelledError:
                with self._lock:
                    if self._waiter is waiter:
                        self._waiter = None
                raise

    def get_nowait(self) -> Event | None:
        with self._lock:
            return self._queue.popleft() if self._queue else None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            waiter, self._waiter = self._waiter, None
            loop = self._loop
        self._bus._remove(self)
        if waiter is not None and loop is not None:
            with suppress(RuntimeError):  # the loop is already gone
                loop.call_soon_threadsafe(_resolve, waiter)

    @property
    def qsize(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def closed(self) -> bool:
        return self._closed

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        try:
            return await self.get()
        except SubscriptionClosed:
            raise StopAsyncIteration from None

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Subscription {self._name} q={self.qsize}/{self._maxsize} dropped={self.dropped}>"


class EventBus:
    """In-process pub/sub. One publisher thread-safe fan-out to bounded mailboxes."""

    def __init__(self) -> None:
        self._subs: tuple[Subscription, ...] = ()
        self._lock = threading.Lock()
        self._sub_seq = 0
        self.published = 0
        self.errors = 0

    def subscribe(self, *, maxsize: int = 256, name: str = "") -> Subscription:
        with self._lock:
            self._sub_seq += 1
            sub = Subscription(self, maxsize, name or f"sub{self._sub_seq}")
            self._subs = (*self._subs, sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            self._subs = tuple(s for s in self._subs if s is not sub)

    def close(self) -> None:
        for sub in self._subs:
            sub.close()

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, event: Event) -> None:
        """Fire and forget, from any thread. Never blocks, never raises."""
        try:
            if "ts" not in event:
                event["ts"] = time.time()
            # Read the tuple once: rebinding is atomic, so no lock is needed
            # here and a subscriber cannot stall the audio thread.
            for sub in self._subs:
                sub._offer(event)
            self.published += 1
        except Exception:  # the audio thread must survive anything a consumer does
            self.errors += 1
            with suppress(Exception):  # even the logging handler may be at fault
                log.exception("event publish failed: %r", event)

    # -- typed helpers: no other module builds one of these dicts by hand -----

    def publish_state(self, state: str, *, since: float | None = None) -> None:
        if state not in VALID_STATES:
            log.warning("unknown state %r (protocol.md allows %s)", state, VALID_STATES)
        now = time.time()
        self.publish({"type": "state", "ts": now, "state": state, "since": since or now})

    def publish_level(self, rms: float, bands: list[float] | None = None) -> None:
        event: Event = {"type": "level", "rms": round(float(rms), 4)}
        if bands is not None:
            event["bands"] = [round(float(b), 4) for b in bands]
        self.publish(event)

    def publish_transcript(self, role: str, text: str, *, final: bool = True) -> None:
        self.publish({"type": "transcript", "role": role, "text": text, "final": final})

    def publish_tool_start(self, tool_id: str, name: str, label: str) -> None:
        self.publish(
            {"type": "tool", "id": tool_id, "name": name, "phase": "start", "label": label}
        )

    def publish_tool_end(
        self, tool_id: str, label: str, *, ok: bool, detail: str | None = None
    ) -> None:
        event: Event = {"type": "tool", "id": tool_id, "phase": "end", "ok": ok, "label": label}
        if detail:
            event["detail"] = detail
        self.publish(event)

    def publish_telemetry(
        self,
        *,
        cpu: float,
        mem: float,
        disk: float,
        battery: dict[str, Any] | None = None,
        net: dict[str, Any] | None = None,
        uptime_s: float | None = None,
    ) -> None:
        event: Event = {"type": "telemetry", "cpu": cpu, "mem": mem, "disk": disk}
        if battery is not None:
            event["battery"] = battery
        if net is not None:
            event["net"] = net
        if uptime_s is not None:
            event["uptime_s"] = uptime_s
        self.publish(event)

    def publish_notice(self, level: str, text: str) -> None:
        self.publish({"type": "notice", "level": level, "text": text})

    def publish_boot(self, step: str, progress: float) -> None:
        self.publish({"type": "boot", "step": step, "progress": round(float(progress), 3)})

    def stats(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "errors": self.errors,
            "subscribers": self.subscribers,
            "dropped": sum(s.dropped for s in self._subs),
            "dropped_critical": sum(s.dropped_critical for s in self._subs),
        }

    def __iter__(self) -> Iterator[Subscription]:
        return iter(self._subs)


_BUS: EventBus | None = None


def get_bus() -> EventBus:
    """The process-wide bus. One per JARVIS; tests build their own."""
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS
