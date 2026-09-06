"""
The terminal side of JARVIS: what it prints, and how it reads a typed line.

The printed transcript is a bus subscriber like any other — the HUD in phase 5
is a second one, and neither knows about the other. That is the whole point of
routing state through the bus rather than calling a renderer directly.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress

from jarvis.events import EventBus


async def read_line(prompt: str) -> str:
    """`input()` on a thread we are willing to abandon.

    asyncio.to_thread uses the default executor, whose threads are joined when
    the loop shuts down — so a ctrl-c while waiting at the prompt would hang
    forever on a read that is never going to return. A daemon thread can simply
    be left behind.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def read() -> None:
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            _settle(loop, future, None)
        else:
            _settle(loop, future, line)

    threading.Thread(target=read, daemon=True, name="jarvis-input").start()
    line = await future
    if line is None:
        raise EOFError
    return line


def _settle(loop: asyncio.AbstractEventLoop, future: asyncio.Future, value: str | None) -> None:
    def resolve() -> None:
        if not future.done():
            future.set_result(value)

    with suppress(RuntimeError):  # the loop went away first; nobody is waiting
        loop.call_soon_threadsafe(resolve)


def console_reporter(bus: EventBus, *, wake_word_hint: bool = True) -> asyncio.Task[None]:
    """A bus subscriber that prints. The HUD in phase 5 is another one.

    `wake_word_hint` is off in text mode: there is no microphone listening
    for "Hey JARVIS" there, so telling the user to say it would be wrong
    advice rather than a harmless nicety.
    """

    async def report() -> None:
        async with bus.subscribe(maxsize=256, name="console") as sub:
            async for event in sub:
                kind = event["type"]
                if kind == "state":
                    idle_hint = ' say "Hey JARVIS"' if wake_word_hint else ""
                    hint = idle_hint if event["state"] == "idle" else ""
                    print(f"[{event['state']}]{hint}")
                elif kind == "transcript" and event["text"]:
                    who = "you" if event["role"] == "user" else "jarvis"
                    print(f"  {who}: {event['text']}")
                elif kind == "notice":
                    print(f"  ! {event['text']}")

    return asyncio.create_task(report())
