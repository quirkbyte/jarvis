"""
Builds the system prompt by filling `reference/persona.md`, which is the prompt
verbatim — nothing here paraphrases it, and changing the character means editing
that file, not this one.

The rebuild policy is the interesting part. The system prompt is the largest
cacheable prefix in every request, so rebuilding it with a fresh timestamp each
turn throws the cache away and costs latency on the exact path that matters.
It is built once at startup and the clock is only refreshed when it has moved
far enough that the model would be wrong about the time — ten minutes, which is
inside the tolerance of "twenty past nine" anyway.

The values are all in spoken form for the same reason the replies are: the model
writes what it reads. Give it "Darwin arm64" and it will start saying things
like that back.
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
import time
from datetime import datetime
from pathlib import Path

from jarvis import spoken
from jarvis.config import CONFIG, Config

log = logging.getLogger("jarvis.persona")

NO_MEMORY = "You have no notes on them yet."


def _fenced_prompt(markdown: str) -> str:
    """The prompt is the first fenced block in persona.md; the rest is notes."""
    parts = markdown.split("```")
    if len(parts) < 3:
        raise ValueError("persona.md has no fenced prompt block")
    return parts[1].strip()


def host_description() -> str:
    """ "a Mac mini, Apple Silicon" — the marketing name, not the model id."""
    name = None
    try:
        out = subprocess.run(
            ["system_profiler", "SPHardwareDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if out.returncode == 0:
            hardware = json.loads(out.stdout)["SPHardwareDataType"][0]
            name = hardware.get("machine_name") or hardware.get("machine_model")
    except Exception as exc:  # noqa: BLE001 - a cosmetic string is not worth failing over
        log.debug("could not read hardware name: %s", exc)
    name = name or ("a Mac" if platform.system() == "Darwin" else platform.system())
    article = "an" if name[:1].upper() in "AEIOU" else "a"
    chip = "Apple Silicon" if platform.machine() == "arm64" else "Intel"
    return f"{article} {name}, {chip}"


def session_note(now: datetime | None = None) -> str:
    """ "since this morning" / "for about three hours", from the boot time."""
    now = now or datetime.now()
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=10
        )
        seconds = time.time() - float(out.stdout.split("sec = ")[1].split(",")[0])
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read boot time: %s", exc)
        return "for a while"
    booted = datetime.fromtimestamp(time.time() - seconds)
    if seconds < 3600:
        return f"for {spoken.duration(seconds)}"
    if booted.date() == now.date() and booted.hour < 12 and now.hour >= 12:
        return "since this morning"
    if seconds > 36 * 3600:
        return "for days"
    return f"for about {spoken.duration(seconds)}"


def memory_block(facts: dict[str, str] | None) -> str:
    if not facts:
        return NO_MEMORY
    lines = "\n".join(f"- {key}: {value}" for key, value in sorted(facts.items()))
    return f"What you know about them, from previous conversations:\n{lines}"


class Persona:
    """The system prompt, built once and refreshed only when the clock demands."""

    def __init__(self, config: Config = CONFIG, path: Path | None = None) -> None:
        self._config = config
        self._path = path or config.brain.persona_path
        self._template = _fenced_prompt(self._path.read_text(encoding="utf-8"))
        self._host = host_description()
        self._prompt: str | None = None
        self._built_at: datetime | None = None
        self._facts: dict[str, str] = {}

    @property
    def stale_after_s(self) -> float:
        return self._config.brain.persona_refresh_s

    def set_memory(self, facts: dict[str, str]) -> None:
        """Replace the remembered-facts block. Invalidates the cached prompt."""
        if facts != self._facts:
            self._facts = dict(facts)
            self._prompt = None

    def build(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        return self._template.format(
            user=self._config.brain.user_name,
            address=self._config.brain.address,
            now=spoken.moment(now),
            host=self._host,
            session_note=session_note(now),
            memory=memory_block(self._facts),
        )

    def prompt(self, now: datetime | None = None) -> str:
        """The cached prompt, rebuilt only when the time in it has gone stale."""
        now = now or datetime.now()
        if self._prompt is None or self._built_at is None:
            self._prompt, self._built_at = self.build(now), now
            return self._prompt
        if abs((now - self._built_at).total_seconds()) >= self.stale_after_s:
            log.debug("persona refreshed: the clock moved on")
            self._prompt, self._built_at = self.build(now), now
        return self._prompt

    @property
    def rebuilt_at(self) -> datetime | None:
        return self._built_at
