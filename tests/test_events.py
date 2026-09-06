"""
The bus is the one component every other phase leans on, and its contract is
mostly about what it refuses to do: block the audio thread, raise, or let a
sleeping HUD cost unbounded memory. Those are the properties tested here.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from jarvis.events import CRITICAL_TYPES, DROPPABLE_TYPES, EventBus, SubscriptionClosed


def drain(sub, n):
    async def go():
        return [await sub.get() for _ in range(n)]

    return asyncio.run(go())


def test_publish_from_another_thread_reaches_the_subscriber():
    """The acceptance test from the spec: publish off-loop, receive on-loop."""

    async def go():
        bus = EventBus()
        sub = bus.subscribe()
        threading.Thread(target=lambda: [bus.publish_level(rms=i / 10) for i in range(10)]).start()
        return [await sub.get() for _ in range(10)]

    got = asyncio.run(go())
    assert len(got) == 10
    assert [e["rms"] for e in got] == [i / 10 for i in range(10)]
    assert all(e["type"] == "level" and isinstance(e["ts"], float) for e in got)


def test_publish_is_non_blocking_and_bounded():
    """10,000 events at a subscriber that never reads: fast, and memory-bounded."""
    bus = EventBus()
    sub = bus.subscribe(maxsize=256)
    start = time.perf_counter()
    for i in range(10_000):
        bus.publish_level(rms=i / 10_000)
    elapsed = time.perf_counter() - start

    assert sub.qsize == 256, "queue must stay at its bound"
    assert sub.dropped == 10_000 - 256
    assert sub.dropped_critical == 0
    assert elapsed < 1.0, f"publish took {elapsed:.3f}s for 10k events"


def test_droppable_events_are_evicted_before_critical_ones():
    bus = EventBus()
    sub = bus.subscribe(maxsize=4)
    bus.publish_state("listening")
    bus.publish_transcript("user", "battery")
    for i in range(20):
        bus.publish_level(rms=i / 20)

    events = drain(sub, sub.qsize)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "state" and kinds[1] == "transcript"
    assert sub.dropped_critical == 0
    assert all(e["type"] in DROPPABLE_TYPES for e in events[2:])


def test_critical_events_survive_a_flood_of_levels():
    bus = EventBus()
    sub = bus.subscribe(maxsize=8)
    for i in range(500):
        bus.publish_level(rms=0.5)
        if i % 100 == 0:
            bus.publish_transcript("jarvis", f"fragment {i}", final=False)
    events = drain(sub, sub.qsize)
    texts = [e["text"] for e in events if e["type"] == "transcript"]
    assert texts == [f"fragment {i}" for i in (0, 100, 200, 300, 400)]
    assert sub.dropped_critical == 0


def test_critical_overflow_bounds_memory_and_is_counted():
    """All-critical overflow still cannot grow without bound — and it says so."""
    bus = EventBus()
    sub = bus.subscribe(maxsize=4)
    for i in range(10):
        bus.publish_transcript("user", f"line {i}")
    assert sub.qsize == 4
    assert sub.dropped_critical == 6
    assert [e["text"] for e in drain(sub, 4)] == [f"line {i}" for i in range(6, 10)]


def test_publish_never_raises_on_a_bad_event():
    bus = EventBus()
    bus.subscribe()

    class Hostile(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("boom")

    bus.publish(Hostile())  # must not propagate: this runs on the audio thread
    assert bus.errors == 1


def test_each_subscriber_gets_its_own_queue_and_bound():
    bus = EventBus()
    fast = bus.subscribe(maxsize=16)
    slow = bus.subscribe(maxsize=2)
    for i in range(8):
        bus.publish_level(rms=i)
    assert fast.qsize == 8
    assert slow.qsize == 2
    assert bus.subscribers == 2


def test_subscription_closes_itself_on_context_exit():
    async def go():
        bus = EventBus()
        async with bus.subscribe() as sub:
            bus.publish_state("idle")
            assert (await sub.get())["state"] == "idle"
        assert sub.closed
        assert bus.subscribers == 0
        with pytest.raises(SubscriptionClosed):
            await sub.get()

    asyncio.run(go())


def test_async_iteration_stops_when_closed():
    async def go():
        bus = EventBus()
        sub = bus.subscribe()
        for i in range(3):
            bus.publish_level(rms=i)
        received = []

        async def consume():
            async for event in sub:
                received.append(event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        sub.close()
        await asyncio.wait_for(task, timeout=1.0)
        return received

    assert len(asyncio.run(go())) == 3


def test_a_waiting_consumer_is_woken_by_a_publish_from_a_thread():
    async def go():
        bus = EventBus()
        sub = bus.subscribe()
        threading.Timer(0.05, lambda: bus.publish_state("speaking")).start()
        event = await asyncio.wait_for(sub.get(), timeout=2.0)
        return event

    assert asyncio.run(go())["state"] == "speaking"


def test_helpers_match_the_protocol_shapes():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish_state("thinking")
    bus.publish_level(rms=0.42, bands=[0.1] * 8)
    bus.publish_transcript("jarvis", "Eighty-two percent", final=False)
    bus.publish_tool_start("t_01", "media.play", "SPOTIFY · PLAY")
    bus.publish_tool_end("t_01", "SPOTIFY · PLAY", ok=True, detail="Brian Eno")
    bus.publish_telemetry(cpu=0.23, mem=0.61, disk=0.44, battery={"pct": 0.82, "charging": False})
    bus.publish_notice("warn", "ELEVENLABS QUOTA LOW")
    bus.publish_boot("LOADING ACOUSTIC MODEL", 0.4)

    events = {e["type"]: e for e in drain(sub, 8)}
    assert set(events) == CRITICAL_TYPES | DROPPABLE_TYPES
    assert events["state"]["state"] == "thinking" and "since" in events["state"]
    assert events["level"]["rms"] == 0.42 and len(events["level"]["bands"]) == 8
    assert events["transcript"] == {
        "type": "transcript",
        "role": "jarvis",
        "text": "Eighty-two percent",
        "final": False,
        "ts": events["transcript"]["ts"],
    }
    assert events["tool"]["phase"] == "end" and events["tool"]["ok"] is True
    assert events["telemetry"]["battery"] == {"pct": 0.82, "charging": False}
    assert events["notice"]["level"] == "warn"
    assert events["boot"]["progress"] == 0.4


def test_unknown_state_warns_but_still_publishes(caplog):
    bus = EventBus()
    sub = bus.subscribe()
    with caplog.at_level("WARNING", logger="jarvis.events"):
        bus.publish_state("dancing")
    assert "dancing" in caplog.text
    assert drain(sub, 1)[0]["state"] == "dancing"


def test_stats_reports_drops():
    bus = EventBus()
    sub = bus.subscribe(maxsize=2)
    for _ in range(5):
        bus.publish_level(rms=1.0)
    stats = bus.stats()
    assert stats["published"] == 5 and stats["dropped"] == 3 and stats["subscribers"] == 1
    sub.close()
