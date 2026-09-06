"""
`_start_hud`'s one bit of side-effecting behaviour outside the server itself:
opening a browser tab, best-effort, and only when asked.
"""

from __future__ import annotations

import asyncio

from jarvis.config import Config
from jarvis.events import EventBus
from jarvis.session import _start_hud, _stop_hud

PORT = 8797


def config(port: int, **over: str) -> Config:
    return Config.from_env({"JARVIS_HUD_PORT": str(port), **over})


class FakeControls:
    async def submit_text(self, text: str) -> None: ...
    def request_interrupt(self) -> None: ...
    def push_to_talk(self, down: bool) -> None: ...


def test_open_browser_is_off_by_default(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    async def go():
        hud, telemetry = await _start_hud(FakeControls(), config(PORT), EventBus())
        await _stop_hud(hud, telemetry)

    asyncio.run(go())
    assert opened == []


def test_open_browser_launches_the_huds_own_url(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    async def go():
        cfg = config(PORT + 1, JARVIS_HUD_OPEN_BROWSER="1")
        hud, telemetry = await _start_hud(FakeControls(), cfg, EventBus())
        await _stop_hud(hud, telemetry)
        return hud

    hud = asyncio.run(go())
    assert opened == [hud.url]


def test_a_browser_that_fails_to_launch_does_not_take_the_hud_down(monkeypatch):
    def boom(url):
        raise OSError("no browser found")

    monkeypatch.setattr("webbrowser.open", boom)

    async def go():
        cfg = config(PORT + 2, JARVIS_HUD_OPEN_BROWSER="1")
        hud, telemetry = await _start_hud(FakeControls(), cfg, EventBus())
        assert hud is not None, "a browser failing to open must not fail the hud"
        await _stop_hud(hud, telemetry)

    asyncio.run(go())
