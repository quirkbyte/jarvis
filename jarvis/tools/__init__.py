"""
Assembles the MCP server the agent sees.

One server, built from every tool module, so adding a capability in Phase 4 is a
new module and one line here rather than a change to the brain. The names that
reach the model are `mcp__jarvis__<tool>`; `guard.py` matches on those, so the
prefix is part of the contract and not an implementation detail.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server

from jarvis.config import CONFIG, Config
from jarvis.tools.memory import MemoryStore, memory_tools
from jarvis.tools.timers import TimerService, timer_tools

SERVER_NAME = "jarvis"


def qualified(name: str) -> str:
    """The name as the model sees it."""
    return f"mcp__{SERVER_NAME}__{name}"


def build_server(
    memory: MemoryStore, timers: TimerService, config: Config = CONFIG
) -> tuple[Any, list[str]]:
    """Returns the server and the fully qualified names to allow."""
    tools = [*memory_tools(memory), *timer_tools(timers)]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)
    return server, [qualified(t.name) for t in tools]


__all__ = ["MemoryStore", "TimerService", "build_server", "qualified"]
