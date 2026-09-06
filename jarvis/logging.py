"""
Two audiences, one logging setup. The terminal gets one line per voice turn,
because anything more and the thing you actually needed to see scrolls past
while JARVIS is talking. The rotating file gets everything, because when a wake
word misfires at midnight the useful record is the one nobody was watching.

The turn line exists to make BUILD.md's latency budget falsifiable: it prints
the real per-stage milliseconds, so "it feels slow" becomes a number with a
stage attached to it.
"""

from __future__ import annotations

import logging
import logging.handlers
import time
from collections.abc import Iterator
from contextlib import contextmanager

from jarvis.config import CONFIG, Config

# The order the stages happen in, which is the order they are worth reading in.
TURN_STAGES = ("wake", "vad", "stt", "llm_ttft", "chunk", "tts_ttfb", "total")

_CONSOLE_FMT = "%(message)s"
_FILE_FMT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"

_configured = False


def setup_logging(config: Config = CONFIG, *, force: bool = False) -> logging.Logger:
    """Install the console + rotating file handlers. Idempotent."""
    global _configured
    root = logging.getLogger("jarvis")
    if _configured and not force:
        return root
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    root.propagate = False

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if config.verbose else logging.INFO)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        config.log_path,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backups,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FMT))
    root.addHandler(file_handler)

    _configured = True
    return root


class TurnTimer:
    """Stopwatch for one voice turn, printed as a single line when it ends.

    ``turn  wake=142ms  vad=310ms  stt=138ms  llm_ttft=402ms  tts_ttfb=131ms
    total=1123ms  "what's my battery at"``
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.started = time.perf_counter()
        self.stages: dict[str, float] = {}
        self._log = logger or logging.getLogger("jarvis.turn")

    def mark(self, stage: str, ms: float) -> None:
        self.stages[stage] = float(ms)

    def restart_at(self, when: float) -> None:
        """Move the origin. The turn is timed from the end of the user's speech,
        not from the moment we started listening to it."""
        self.started = when

    def mark_since(self, stage: str, start: float) -> float:
        ms = (time.perf_counter() - start) * 1000.0
        self.mark(stage, ms)
        return ms

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.mark_since(name, start)

    @property
    def total_ms(self) -> float:
        return self.stages.get("total", (time.perf_counter() - self.started) * 1000.0)

    def format(self, text: str = "") -> str:
        self.stages.setdefault("total", (time.perf_counter() - self.started) * 1000.0)
        ordered = [s for s in TURN_STAGES if s in self.stages]
        ordered += [s for s in self.stages if s not in TURN_STAGES]
        parts = "  ".join(f"{name}={self.stages[name]:.0f}ms" for name in ordered)
        line = f"turn  {parts}"
        return f'{line}  "{text}"' if text else line

    def log(self, text: str = "") -> str:
        line = self.format(text)
        self._log.info(line)
        return line


def over_budget(timer: TurnTimer, config: Config = CONFIG) -> list[str]:
    """Stages that blew their BUILD.md budget. Empty list is the happy path."""
    budgets = {
        "stt": config.stt.budget_ms,
        "llm_ttft": config.brain.ttft_budget_ms,
        "chunk": config.tts.chunk_budget_ms,
        "tts_ttfb": config.tts.budget_ms,
    }
    return [
        f"{stage}={timer.stages[stage]:.0f}ms>{budget}ms"
        for stage, budget in budgets.items()
        if stage in timer.stages and timer.stages[stage] > budget
    ]
