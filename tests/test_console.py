"""
The console reporter: what gets printed for each bus event. Small, but the one
thing worth pinning is that text mode does not tell the user to speak, since
that used to happen and it is bad advice on a machine with no microphone open.
"""

from __future__ import annotations

import asyncio

from jarvis.console import console_reporter
from jarvis.events import EventBus


def printed(bus: EventBus, wake_word_hint: bool, publish) -> list[str]:
    lines: list[str] = []

    async def go():
        import builtins

        original = builtins.print
        builtins.print = lambda *a, **k: lines.append(" ".join(str(x) for x in a))
        try:
            task = console_reporter(bus, wake_word_hint=wake_word_hint)
            # Let the reporter actually subscribe before anything is published,
            # or the event has no listener yet and is simply dropped.
            await asyncio.sleep(0.05)
            publish()
            await asyncio.sleep(0.1)
        finally:
            builtins.print = original
            task.cancel()

    asyncio.run(go())
    return lines


def test_the_wake_word_hint_appears_by_default_when_idle():
    bus = EventBus()
    lines = printed(bus, True, lambda: bus.publish_state("idle"))
    assert any("Hey JARVIS" in line for line in lines)


def test_text_mode_never_tells_the_user_to_speak():
    bus = EventBus()
    lines = printed(bus, False, lambda: bus.publish_state("idle"))
    assert lines == ["[idle]"]
    assert not any("Hey JARVIS" in line for line in lines)


def test_a_non_idle_state_never_carries_the_hint_either_way():
    bus = EventBus()
    lines = printed(bus, True, lambda: bus.publish_state("thinking"))
    assert lines == ["[thinking]"]
