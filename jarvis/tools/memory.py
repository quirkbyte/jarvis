"""
Durable facts about the user, and nothing else.

The scoping is the whole design. Left to its own judgement a model will write
conversation state, task progress and its own reasoning into a memory store, and
since this store is injected into the system prompt at startup, that bloats the
prefix until the character degrades and the latency with it. So the tool
description is narrow, deliberately, and it is the description the model reads —
not this docstring.

Writes are atomic because the alternative is a half-written JSON file that
silently loses everything the user ever told it, discovered weeks later.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from jarvis.config import CONFIG, Config

log = logging.getLogger("jarvis.memory")


class MemoryStore:
    """A small JSON dictionary of things worth remembering between sessions."""

    def __init__(self, config: Config = CONFIG, path: Path | None = None) -> None:
        self._path = path or config.memory_path
        self._limit = config.brain.memory_max_facts
        self._lock = threading.Lock()
        self._facts: dict[str, str] = {}
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, str]:
        with self._lock:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._facts = {str(k): str(v) for k, v in raw.items()}
            except FileNotFoundError:
                self._facts = {}
            except (json.JSONDecodeError, AttributeError, TypeError) as exc:
                # A corrupt file must not stop JARVIS booting. Keep it for
                # forensics rather than overwriting it on the next save.
                log.warning("memory file unreadable (%s); starting empty", exc)
                self._salvage()
                self._facts = {}
            return dict(self._facts)

    def _salvage(self) -> None:
        broken = self._path.with_suffix(".corrupt.json")
        try:
            self._path.replace(broken)
            log.warning("moved the unreadable memory file to %s", broken.name)
        except OSError:
            pass

    def _save_locked(self) -> None:
        """Write via a temp file in the same directory, then rename over."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(self._facts, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, self._path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._facts)

    def remember(self, key: str, value: str) -> str:
        key, value = key.strip().lower(), value.strip()
        if not key or not value:
            return "Nothing to store."
        with self._lock:
            previous = self._facts.get(key)
            if len(self._facts) >= self._limit and key not in self._facts:
                return f"Memory is full at {self._limit} facts; forget something first."
            self._facts[key] = value
            self._save_locked()
        return f"Updated: {key} was {previous}." if previous else f"Noted: {key}."

    def forget(self, key: str) -> str:
        key = key.strip().lower()
        with self._lock:
            if key not in self._facts:
                return f"Nothing stored under {key}."
            del self._facts[key]
            self._save_locked()
        return f"Forgotten: {key}."


def memory_tools(store: MemoryStore) -> list[Any]:
    """The tools as the model sees them. The descriptions are the interface."""

    @tool(
        "remember",
        "Store a durable fact about the user for future conversations: their name, "
        "their preferences, how they like things done, people and projects that come "
        "up repeatedly. Use a short lower-case key like 'units' or 'wake time'. Do "
        "NOT use this for what is happening right now, task progress, or anything "
        "that stops being true when the conversation ends.",
        {"key": str, "value": str},
    )
    async def remember(args: dict[str, Any]) -> dict[str, Any]:
        return _text(store.remember(str(args.get("key", "")), str(args.get("value", ""))))

    @tool(
        "recall",
        "List everything currently remembered about the user. The same facts are "
        "already in your system prompt, so only call this if you need to check "
        "whether something was stored, or the user asks what you know about them.",
        {},
    )
    async def recall(_: dict[str, Any]) -> dict[str, Any]:
        facts = store.all()
        if not facts:
            return _text("Nothing stored yet.")
        return _text("\n".join(f"{key}: {value}" for key, value in sorted(facts.items())))

    @tool(
        "forget",
        "Delete one remembered fact by its key. Use when the user says to forget "
        "something or corrects a preference that no longer applies.",
        {"key": str},
    )
    async def forget(args: dict[str, Any]) -> dict[str, Any]:
        return _text(store.forget(str(args.get("key", ""))))

    return [remember, recall, forget]


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}
