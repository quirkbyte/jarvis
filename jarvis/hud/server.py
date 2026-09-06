"""
The WebSocket the HUD talks to, and the one rule that governs all of it: the
core must not care whether any of this exists.

So every client gets its own bounded queue and its own sender task, the bus
subscription never blocks, and a browser that is slow, wedged, or killed
mid-sentence is that browser's problem. When a client falls behind, the level
and telemetry frames are dropped for it — a stuttering waveform is fine, a HUD
stuck in the wrong state is not, so those are never dropped.

Bound to loopback, deliberately and permanently. This socket carries a live feed
of what is on the user's screen and in their conversation, and there is no
version of "just for testing" that makes 0.0.0.0 acceptable here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from jarvis.config import CONFIG, Config
from jarvis.events import DROPPABLE_TYPES, EventBus, get_bus

log = logging.getLogger("jarvis.hud")

STATIC = Path(__file__).parent / "static"


class Controls(Protocol):
    """What the HUD is allowed to ask the core to do."""

    async def submit_text(self, text: str) -> None: ...
    def request_interrupt(self) -> None: ...
    def push_to_talk(self, down: bool) -> None: ...


class Client:
    """One browser. Owns a bounded queue and drops rather than blocking."""

    def __init__(self, socket: WebSocket, maxsize: int) -> None:
        self.socket = socket
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.maxsize = maxsize
        self.dropped = 0

    def offer(self, event: dict[str, Any]) -> None:
        if self.queue.qsize() >= self.maxsize:
            if event.get("type") in DROPPABLE_TYPES:
                self.dropped += 1
                return
            # Critical and the queue is full: make room by shedding the oldest
            # droppable frame rather than the state change.
            self._evict()
        self.queue.put_nowait(event)

    def _evict(self) -> None:
        """Shed the oldest droppable frame, keeping everything else in order.

        Order is part of the contract: transcript fragments append to a line,
        and a tool that ends before it starts is nonsense. So the queue is
        rebuilt rather than rotated.
        """
        items: list[dict[str, Any]] = []
        while not self.queue.empty():
            items.append(self.queue.get_nowait())
        for i, item in enumerate(items):
            if item.get("type") in DROPPABLE_TYPES:
                del items[i]
                self.dropped += 1
                break
        else:
            # Nothing droppable: the client is badly stuck. Bound the memory by
            # shedding the oldest frame, whatever it is.
            if items:
                del items[0]
                self.dropped += 1
        for item in items:
            self.queue.put_nowait(item)

    async def pump(self) -> None:
        while True:
            event = await self.queue.get()
            await self.socket.send_text(json.dumps(event))


class HudServer:
    """FastAPI + uvicorn, sharing the core's event loop."""

    def __init__(
        self, bus: EventBus | None = None, config: Config = CONFIG, controls: Controls | None = None
    ) -> None:
        self._bus = bus or get_bus()
        self._config = config
        self._controls = controls
        self._clients: set[Client] = set()
        self._server: uvicorn.Server | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._last_state: dict[str, Any] | None = None
        self._last_telemetry: dict[str, Any] | None = None
        self.app = self._build_app()

    @property
    def url(self) -> str:
        return f"http://{self._config.hud.host}:{self._config.hud.port}"

    @property
    def clients(self) -> int:
        return len(self._clients)

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="JARVIS HUD", docs_url=None, redoc_url=None, openapi_url=None)

        @app.middleware("http")
        async def no_cache(request, call_next):  # type: ignore[no-untyped-def]
            # This HUD is under active development and the browser tab is
            # typically left open for days — a stale cached hud.js against a
            # changed index.html has already produced a real, hard-to-diagnose
            # black screen once. There is no production deployment of this
            # server where caching the UI is worth that risk, so: never cache.
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC / "index.html")

        @app.websocket(self._config.hud.ws_path)
        async def socket(websocket: WebSocket) -> None:
            await self._serve(websocket)

        if STATIC.is_dir():
            app.mount("/static", StaticFiles(directory=STATIC), name="static")
        return app

    async def _serve(self, websocket: WebSocket) -> None:
        await websocket.accept()
        client = Client(websocket, self._config.hud.client_queue_max)
        self._clients.add(client)
        pump = asyncio.create_task(client.pump(), name="jarvis-hud-send")
        log.info("hud connected (%d client%s)", self.clients, "" if self.clients == 1 else "s")
        try:
            while True:
                message = json.loads(await websocket.receive_text())
                await self._handle(client, message)
        except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
            pass
        finally:
            pump.cancel()
            self._clients.discard(client)
            log.info("hud disconnected (%d left, %d frames dropped)", self.clients, client.dropped)

    async def _handle(self, client: Client, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "hello":
            version = int(message.get("version", 0))
            if version != self._config.hud.protocol_version:
                client.offer({"type": "notice", "level": "warn", "text": "HUD PROTOCOL MISMATCH"})
            # A HUD that joins mid-session must render correctly, not sit blank.
            if self._last_state:
                client.offer(self._last_state)
            if self._last_telemetry:
                client.offer(self._last_telemetry)
        elif kind == "text" and self._controls is not None:
            await self._controls.submit_text(str(message.get("text", "")))
        elif kind == "interrupt" and self._controls is not None:
            self._controls.request_interrupt()
        elif kind == "ptt" and self._controls is not None:
            self._controls.push_to_talk(bool(message.get("down")))

    async def _forward(self, sub) -> None:
        async with sub:
            async for event in sub:
                if event["type"] == "state":
                    self._last_state = event
                elif event["type"] == "telemetry":
                    self._last_telemetry = event
                for client in list(self._clients):
                    client.offer(event)

    async def _serve_guarded(self) -> None:
        """Run uvicorn, without letting it take the process down.

        On a bind failure — an `OSError`, most often something else already on
        this port — uvicorn's own `startup()` calls `sys.exit()`. That raises
        `SystemExit` inside the task, and asyncio treats `SystemExit` specially:
        it re-raises out of the task machinery rather than storing it as an
        ordinary result, which can propagate out of the whole event loop. A
        HUD that cannot bind its port must not be able to take the voice loop
        down with it, so this converts that `SystemExit` into a plain
        exception that `start()` can retrieve normally.
        """
        assert self._server is not None
        try:
            await self._server.serve()
        except SystemExit as exc:
            raise RuntimeError(f"uvicorn exited during startup ({exc})") from exc

    async def start(self) -> None:
        """Start serving. Raises if the socket cannot be bound."""
        hud = self._config.hud
        config = uvicorn.Config(
            self.app, host=hud.host, port=hud.port, log_level="warning", access_log=False
        )
        self._server = uvicorn.Server(config)
        # Subscribe here rather than inside the task: creating the task does not
        # run it, and anything published in between would be missed — including
        # the state a late-joining HUD needs to render correctly.
        sub = self._bus.subscribe(maxsize=512, name="hud")
        serve_task = asyncio.create_task(self._serve_guarded(), name="jarvis-hud-serve")
        self._tasks = [
            serve_task,
            asyncio.create_task(self._forward(sub), name="jarvis-hud-forward"),
        ]

        # Wait briefly for uvicorn to say it actually started, so the caller
        # can decide what "the core must not care whether the HUD exists"
        # means for a HUD that never came up, rather than running on unaware.
        for _ in range(50):  # 50 * 20ms = 1s
            if self._server.started:
                break
            if serve_task.done():
                raise serve_task.exception() or RuntimeError(f"could not bind {self.url}")
            await asyncio.sleep(0.02)
        log.info("hud on %s", self.url)

    async def stop(self) -> None:
        """Ask uvicorn to wind down before cancelling, or it logs its own death."""
        if self._server is not None:
            self._server.should_exit = True
        serve, rest = self._tasks[:1], self._tasks[1:]
        for task in serve:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=2.0)
        for task in [*serve, *rest]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
