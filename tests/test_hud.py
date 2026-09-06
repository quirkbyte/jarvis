"""
The HUD server, tested for the property the architecture exists to provide: the
core does not care whether any of this is here.

So the interesting cases are all failures — a client that never reads, a client
that vanishes mid-frame, a version nobody recognises — and what must survive
them is the voice loop, which in these tests means the bus and the publisher.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from jarvis.config import Config
from jarvis.events import EventBus
from jarvis.hud.server import Client, HudServer

PORT = 8791


def config(port: int, **over: str) -> Config:
    return Config.from_env({"JARVIS_HUD_PORT": str(port), **over})


class FakeControls:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.interrupts = 0
        self.ptt: list[bool] = []

    async def submit_text(self, text: str) -> None:
        self.said.append(text)

    def request_interrupt(self) -> None:
        self.interrupts += 1

    def push_to_talk(self, down: bool) -> None:
        self.ptt.append(down)


class FakeSocket:
    """A client that accepts frames and never sends them anywhere."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


async def serve(port: int, bus: EventBus, controls=None):
    server = HudServer(bus, config(port), controls)
    await server.start()
    await asyncio.sleep(0.6)
    return server


async def connected(port: int):
    return await websockets.connect(f"ws://127.0.0.1:{port}/ws", open_timeout=5)


async def hello(ws, version: int = 1) -> None:
    await ws.send(json.dumps({"type": "hello", "client": "test", "version": version}))


async def next_of(ws, kind: str, tries: int = 8) -> dict:
    """Read until the wanted frame arrives; ordering is not the contract."""
    for _ in range(tries):
        event = json.loads(await asyncio.wait_for(ws.recv(), 3))
        if event["type"] == kind:
            return event
    raise AssertionError(f"no {kind} frame arrived")


# -- the queue, without a socket ---------------------------------------------


def test_a_full_queue_drops_levels_and_keeps_state():
    client = Client(FakeSocket(), maxsize=4)
    for _ in range(50):
        client.offer({"type": "level", "rms": 0.5})
    assert client.queue.qsize() == 4
    assert client.dropped == 46

    client.offer({"type": "state", "state": "speaking"})
    kinds = [client.queue.get_nowait()["type"] for _ in range(client.queue.qsize())]
    assert "state" in kinds, "a state change was dropped to make room for audio levels"


def test_telemetry_is_droppable_too():
    client = Client(FakeSocket(), maxsize=2)
    for _ in range(10):
        client.offer({"type": "telemetry", "cpu": 0.1})
    assert client.queue.qsize() == 2 and client.dropped == 8


def test_transcripts_and_tools_are_never_the_first_thing_dropped():
    client = Client(FakeSocket(), maxsize=3)
    client.offer({"type": "transcript", "role": "jarvis", "text": "hello"})
    client.offer({"type": "level", "rms": 0.1})
    client.offer({"type": "tool", "phase": "start", "label": "SHELL · BASH"})
    client.offer({"type": "state", "state": "idle"})
    kinds = [client.queue.get_nowait()["type"] for _ in range(client.queue.qsize())]
    assert kinds == ["transcript", "tool", "state"]


def test_shedding_a_frame_does_not_reorder_the_rest():
    """Transcript fragments append to a line; a reordered queue garbles it."""
    client = Client(FakeSocket(), maxsize=4)
    for i in range(4):
        client.offer({"type": "transcript", "role": "jarvis", "text": f"part {i}"})
    client.offer({"type": "level", "rms": 0.9})  # forces an eviction
    texts = [
        e["text"]
        for e in (client.queue.get_nowait() for _ in range(client.queue.qsize()))
        if e["type"] == "transcript"
    ]
    assert texts == sorted(texts), f"fragments came out shuffled: {texts}"


def test_a_client_stuck_on_nothing_but_critical_frames_is_still_bounded():
    client = Client(FakeSocket(), maxsize=4)
    for i in range(100):
        client.offer({"type": "transcript", "role": "jarvis", "text": f"line {i}"})
    assert client.queue.qsize() <= 4


# -- the socket ---------------------------------------------------------------


def test_a_hud_joining_mid_session_is_told_the_current_state():
    async def go():
        bus = EventBus()
        server = await serve(PORT, bus)
        try:
            bus.publish_state("thinking")
            bus.publish_telemetry(cpu=0.2, mem=0.3, disk=0.4)
            await asyncio.sleep(0.2)
            async with await connected(PORT) as ws:
                await hello(ws)
                assert (await next_of(ws, "state"))["state"] == "thinking"
                assert (await next_of(ws, "telemetry"))["cpu"] == 0.2
        finally:
            await server.stop()

    asyncio.run(go())


def test_events_reach_a_connected_hud():
    async def go():
        bus = EventBus()
        server = await serve(PORT + 1, bus)
        try:
            async with await connected(PORT + 1) as ws:
                await hello(ws)
                bus.publish_tool_start("t_1", "Bash", "SHELL · BASH")
                event = await next_of(ws, "tool")
                assert event["label"] == "SHELL · BASH"
        finally:
            await server.stop()

    asyncio.run(go())


def test_an_unknown_protocol_version_is_a_notice_not_a_refusal():
    async def go():
        bus = EventBus()
        server = await serve(PORT + 2, bus)
        try:
            async with await connected(PORT + 2) as ws:
                await hello(ws, version=99)
                assert "MISMATCH" in (await next_of(ws, "notice"))["text"]
                bus.publish_state("idle")
                assert (await next_of(ws, "state"))["state"] == "idle", "it stopped serving"
        finally:
            await server.stop()

    asyncio.run(go())


@pytest.mark.parametrize(
    ("message", "check"),
    [
        (
            {"type": "text", "text": "what's my battery at"},
            lambda c: c.said == ["what's my battery at"],
        ),
        ({"type": "interrupt"}, lambda c: c.interrupts == 1),
        ({"type": "ptt", "down": True}, lambda c: c.ptt == [True]),
    ],
)
def test_the_hud_can_drive_the_same_paths_the_voice_loop_uses(message, check):
    async def go():
        controls = FakeControls()
        server = await serve(PORT + 3, EventBus(), controls)
        try:
            async with await connected(PORT + 3) as ws:
                await hello(ws)
                await ws.send(json.dumps(message))
                await asyncio.sleep(0.4)
            return controls
        finally:
            await server.stop()

    assert check(asyncio.run(go()))


def test_a_hud_that_vanishes_mid_frame_does_not_disturb_the_bus():
    """The resilience property, in the small: killing the browser changes nothing."""

    async def go():
        bus = EventBus()
        server = await serve(PORT + 4, bus)
        watcher = bus.subscribe(maxsize=64, name="core")
        try:
            ws = await connected(PORT + 4)
            await hello(ws)
            await asyncio.sleep(0.2)
            await ws.close()
            for i in range(200):
                bus.publish_level(rms=i / 200)
            bus.publish_state("speaking")
            await asyncio.sleep(0.3)
            assert server.clients == 0
            assert bus.errors == 0
            kinds = {watcher.get_nowait()["type"] for _ in range(watcher.qsize)}
            assert "state" in kinds, "the core lost an event because a browser died"
        finally:
            await server.stop()

    asyncio.run(go())


def test_it_binds_to_loopback_and_nothing_else():
    """This socket carries the conversation. There is no 'just for testing'."""
    assert Config.from_env({}).hud.host == "127.0.0.1"
    assert HudServer(EventBus(), config(PORT))._config.hud.host == "127.0.0.1"


def test_a_port_already_in_use_is_a_clean_error_not_a_silent_stall():
    """The failure mode this repo has actually hit: something else — often a
    leftover `hud_demo.py` — squatting on the port. `start()` must say so
    promptly rather than leaving the caller believing the HUD is up."""

    async def go():
        holder = await serve(PORT + 5, EventBus())
        try:
            squatter = HudServer(EventBus(), config(PORT + 5))
            with pytest.raises(RuntimeError, match=r"could not bind|uvicorn exited"):
                await squatter.start()
        finally:
            await holder.stop()

    asyncio.run(go())
