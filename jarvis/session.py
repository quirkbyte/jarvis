"""
Process lifecycle: take the run lock, start logging, build a JARVIS, and make
sure everything is put back however the process ends.

Kept apart from `main.py` so that file is only the state machine. This one is
about the things that happen once — and about ctrl-c, which has to reach a
microphone thread, an agent subprocess and a speaker mid-sentence.
"""

from __future__ import annotations

import asyncio
import sys
import webbrowser

from jarvis.audio.mic import NoInputDevice
from jarvis.config import CONFIG, Config
from jarvis.console import console_reporter, read_line
from jarvis.events import EventBus, get_bus
from jarvis.hud.server import HudServer
from jarvis.hud.telemetry import Telemetry
from jarvis.instance import AlreadyRunning, SingleInstance
from jarvis.logging import setup_logging
from jarvis.main import Jarvis


async def _start_hud(
    jarvis: Jarvis, config: Config, bus: EventBus
) -> tuple[HudServer, Telemetry] | tuple[None, None]:
    """The HUD, in-process, sharing the core's event loop — `jarvis` doubles as
    the `Controls` the browser drives (text, interrupt, push-to-talk).

    A HUD that cannot start must not take the voice loop with it: BUILD.md's
    rule is that the core does not care whether the HUD exists, and that has
    to hold even when starting it goes wrong. A port already held — the one way
    this has actually failed in this build, from a leftover test script — is
    reported and the voice loop proceeds without a HUD rather than dying.
    """
    if not config.hud.enabled:
        return None, None
    server = HudServer(bus, config, controls=jarvis)
    try:
        await server.start()
    except Exception as exc:  # noqa: BLE001 - a dead HUD must not be a dead voice loop
        print(f"hud unavailable ({exc}) — continuing without it", file=sys.stderr)
        await server.stop()
        return None, None
    telemetry = Telemetry(bus, config)
    telemetry.start()
    if config.hud.open_browser:
        # Best-effort: no browser configured (or one that fails to launch) is
        # not a reason to take the voice loop down, same rule as the HUD
        # itself failing to start above.
        try:
            webbrowser.open(server.url)
        except Exception as exc:  # noqa: BLE001
            print(f"could not open a browser for the hud ({exc})", file=sys.stderr)
    return server, telemetry


async def _stop_hud(server: HudServer | None, telemetry: Telemetry | None) -> None:
    if telemetry is not None:
        telemetry.stop()
    if server is not None:
        await server.stop()


async def run_text(config: Config = CONFIG, *, resume: str | None = None) -> int:
    """No microphone: type at it and it speaks back.

    This is not a lesser mode kept alive out of politeness — it is the only way
    to exercise the brain and the speech-out path on a machine with no working
    input, and the fastest way to hear a voice change without saying anything.
    """
    setup_logging(config)
    bus = get_bus()
    reporter = console_reporter(bus, wake_word_hint=False)
    jarvis = Jarvis(config, bus, resume=resume)
    jarvis.set_state("boot")
    await asyncio.to_thread(jarvis.speaker.start)
    await jarvis.wake_agent()
    hud, telemetry = await _start_hud(jarvis, config, bus)
    print(
        f"[text] brain: {config.brain.model} | tts: {jarvis.speaker.backend}"
        f" | memory: {len(jarvis.memory.all())} facts"
        + (f" | hud: {hud.url}" if hud is not None else "")
        + " — type, or ctrl-d to quit"
    )
    try:
        while True:
            # "idle" between turns, exactly as the mic-driven loop does — a
            # HUD attached here can only submit_text while idle or listening,
            # and text mode never says either unless something says it here.
            jarvis.set_state("idle")
            line = (await read_line("you: ")).strip()
            if not line:
                continue
            await jarvis.answer(line)
    except (EOFError, KeyboardInterrupt):
        print("\nstopping.")
    finally:
        await _stop_hud(hud, telemetry)
        jarvis.shutdown()
        if jarvis.brain is not None:
            jarvis.brain.save_session(config.session_path)
        reporter.cancel()
    return 0


async def run(config: Config = CONFIG, *, resume: str | None = None) -> int:
    setup_logging(config)
    try:
        lock = SingleInstance(config).acquire()
    except AlreadyRunning as exc:
        print(
            f"{exc}.\nTwo copies both hold the microphone and both answer, which sounds "
            f"like an echo.\nStop the other one first:  kill {exc.pid or ''}".rstrip(),
            file=sys.stderr,
        )
        return 1
    bus = get_bus()
    reporter = console_reporter(bus)
    jarvis = Jarvis(config, bus, resume=resume)
    # Started before jarvis.run(), so the HUD is already subscribed when the
    # boot sequence's own state and progress events go out — a HUD that opens
    # a moment later renders whatever is current either way, but the boot
    # sequence is worth actually seeing rather than joining after the fact.
    hud, telemetry = await _start_hud(jarvis, config, bus)
    if hud is not None:
        print(f"hud: {hud.url}", flush=True)
    try:
        await jarvis.run()
    except NoInputDevice as exc:
        print(
            f"\njarvis cannot hear anything: {exc}.\n"
            "  · bluetooth headphones drop off this list when they go to sleep or "
            "go back in the case\n"
            "  · otherwise pick an input under System Settings → Sound → Input\n"
            "  · `make doctor` will confirm it once something is connected\n"
            "  · `make text` needs no microphone at all",
            file=sys.stderr,
        )
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nstopping.", flush=True)
    finally:
        await _stop_hud(hud, telemetry)
        jarvis.shutdown()
        if jarvis.brain is not None:
            jarvis.brain.save_session(config.session_path)
        reporter.cancel()
        lock.release()
    return 0
