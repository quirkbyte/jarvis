"""
The permission policy, in code, because a voice assistant cannot show a dialog.

The user is across the room with their hands full. There is no window to click,
so "are you sure?" has to be a spoken exchange — and a spoken yes is only worth
anything if the code can tell *which* action it was a yes to. That is what the
confirmation token is for: a denial records a fingerprint of the exact tool call
that was described, and only a matching call within the confirmation window is
allowed through. Without it, "yes" is just a word in a transcript and a model
that wants to retry can talk itself into treating any agreement as consent.

Three tiers. Reads go through silently. Ordinary changes go through. Anything
irreversible is denied with an instruction to describe it and ask out loud —
denials are fed back to the model, so they are written as instructions rather
than as errors.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from jarvis.config import CONFIG, Config
from jarvis.events import EventBus

log = logging.getLogger("jarvis.guard")

SILENT = "silent"  # reads: never mentioned, never logged as activity
ALLOW = "allow"  # changes we are happy to make unannounced
CONFIRM = "confirm"  # irreversible: describe it and ask out loud first

READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "WebSearch", "WebFetch", "NotebookRead"})
SAFE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "TodoWrite", "NotebookEdit", "Task"})

# Tools of ours that only ever read. Everything else of ours changes something
# small and reversible, which is the ALLOW tier.
READ_ONLY_JARVIS = frozenset({"recall", "list_timers"})

# Shell that cannot be undone, or that could lock the user out of their own Mac.
DANGEROUS_SHELL = [
    (re.compile(r"\brm\b.*\s-\w*[rf]", re.I), "delete files recursively"),
    (re.compile(r"\bsudo\b", re.I), "run something as root"),
    (re.compile(r"\bdd\b\s+if=", re.I), "write directly to a disk"),
    (re.compile(r"\b(mkfs|diskutil\s+(erase|reformat|partition))", re.I), "erase a disk"),
    (re.compile(r"\bgit\s+push\b", re.I), "push to a remote repository"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.I), "discard uncommitted work"),
    (re.compile(r"\bgit\s+clean\b\s+-\w*[fd]", re.I), "delete untracked files"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh", re.I), "run a script off the web"),
    (re.compile(r"\b(shutdown|reboot|halt)\b", re.I), "shut the machine down"),
    (re.compile(r"\bkillall\b", re.I), "force-quit applications"),
    (re.compile(r"\bchmod\b\s+-\w*R\w*\s+777", re.I), "make files world-writable"),
    (re.compile(r"\bnetworksetup\b.*\b(off|-setairportpower\s+\w+\s+off)", re.I), "turn wifi off"),
    (re.compile(r"\bpmset\b.*\bsleep\b", re.I), "change the sleep settings"),
    (re.compile(r"\blaunchctl\b\s+(unload|remove|bootout)", re.I), "remove a background service"),
    (re.compile(r"\bdefaults\s+delete\b", re.I), "delete application settings"),
    (re.compile(r">\s*/dev/(disk|rdisk)", re.I), "write directly to a disk"),
]

# Anything under here can be deleted freely; that is what it is for.
TRASH = "/.Trash"


@dataclass(frozen=True)
class Pending:
    """A tool call that was denied and described, waiting on a spoken yes."""

    tool: str
    fingerprint: str
    summary: str
    at: float


# Keys the model writes for a human to read, which have no effect on what runs.
# They are left out of the fingerprint because the model rewords them between
# attempts, and a retry of the same command must match the yes it was given.
COSMETIC_KEYS = frozenset({"description", "title", "explanation", "reason"})


def fingerprint(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Identify one exact call. A different argument is a different request."""
    significant = {k: v for k, v in tool_input.items() if k not in COSMETIC_KEYS}
    canonical = json.dumps(significant, sort_keys=True, default=str)
    return hashlib.sha256(f"{tool_name}\x00{canonical}".encode()).hexdigest()[:16]


def _segments(command: str) -> list[str]:
    """Split a shell line on the operators, so `ls && rm -rf ~` is not one string."""
    return [part for part in re.split(r"&&|\|\||;|\n", command) if part.strip()]


def _deletes_outside_trash(command: str) -> bool:
    if not re.search(r"\brm\b", command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return True  # unparseable quoting around a delete: assume the worst
    targets = [w for w in words[1:] if not w.startswith("-")]
    return not targets or any(TRASH not in str(Path(t).expanduser()) for t in targets)


class Guard:
    """Decides what runs without asking, and remembers what was agreed to."""

    def __init__(self, config: Config = CONFIG, bus: EventBus | None = None) -> None:
        self._config = config
        self._bus = bus
        self._pending: Pending | None = None
        self._approved: dict[str, float] = {}

    @property
    def pending(self) -> Pending | None:
        return self._pending

    # -- classification ------------------------------------------------------

    def classify(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
        """Returns (tier, what it would do) — the second half is spoken aloud."""
        bare = tool_name.rsplit("__", 1)[-1]
        if tool_name.startswith("mcp__jarvis__"):
            return (SILENT if bare in READ_ONLY_JARVIS else ALLOW), ""
        if tool_name in READ_ONLY_TOOLS:
            return SILENT, ""
        if tool_name == "Bash":
            return self._classify_shell(str(tool_input.get("command", "")))
        if tool_name in SAFE_TOOLS:
            return ALLOW, ""
        # Unknown tool. Later phases add them; until one is classified here it
        # gets the cautious tier rather than a free pass.
        return CONFIRM, f"use {tool_name}, which I have no policy for"

    def _classify_shell(self, command: str) -> tuple[str, str]:
        for segment in _segments(command):
            for pattern, description in DANGEROUS_SHELL:
                if pattern.search(segment):
                    return CONFIRM, description
            if _deletes_outside_trash(segment):
                return CONFIRM, "delete files that do not go to the Trash"
        return ALLOW, ""

    # -- the spoken confirmation loop ---------------------------------------

    def note_reply(self, text: str) -> str | None:
        """Feed the user's transcript in. Returns 'approved', 'refused' or None.

        Only ever acts on a request that is already pending and still fresh —
        an unprompted "yes" approves nothing.
        """
        pending = self._pending
        if pending is None:
            return None
        if time.monotonic() - pending.at > self._config.brain.confirm_window_s:
            self._pending = None
            return None
        spoken = text.strip().lower()
        if self._config.brain.refusal_pattern and re.search(
            self._config.brain.refusal_pattern, spoken
        ):
            self._pending = None
            log.info("refused: %s", pending.summary)
            return "refused"
        if re.search(self._config.brain.approval_pattern, spoken):
            self._approved[pending.fingerprint] = time.monotonic()
            self._pending = None
            log.info("approved by voice: %s", pending.summary)
            return "approved"
        return None

    def _approval_is_live(self, key: str) -> bool:
        granted = self._approved.get(key)
        if granted is None:
            return False
        if time.monotonic() - granted > self._config.brain.confirm_window_s:
            del self._approved[key]
            return False
        return True

    # -- the SDK callback ----------------------------------------------------

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: Any = None
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context  # the SDK passes suggestions we have no use for
        tier, description = self.classify(tool_name, tool_input)
        key = fingerprint(tool_name, tool_input)

        if tier in (SILENT, ALLOW):
            return PermissionResultAllow(updated_input=tool_input)

        if self._approval_is_live(key):
            del self._approved[key]  # one yes, one action
            log.info("running the confirmed action: %s", tool_name)
            if self._bus is not None:
                self._bus.publish_notice("info", "CONFIRMED")
            return PermissionResultAllow(updated_input=tool_input)

        self._pending = Pending(tool_name, key, description, time.monotonic())
        log.info("confirm first: %s (%s)", tool_name, description)
        if self._bus is not None:
            self._bus.publish_notice("warn", "AWAITING CONFIRMATION")
        return PermissionResultDeny(message=self._instruction(description))

    def _instruction(self, description: str) -> str:
        return (
            f"That is on the confirm-first list: it would {description}. "
            "Tell the user in one sentence exactly what it would do, and ask them to "
            "say yes. Do not call this tool again until they have. If they decline, "
            "drop it without arguing."
        )
