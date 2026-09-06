"""
The agent, behind an interface the voice layer can use: text in, speakable
fragments out. Everything the model does that is not speech — reaching for a
tool, thinking, reporting a result — goes to the event bus instead, because
nobody wants to hear "calling Bash".

Two decisions are load-bearing. It uses `ClaudeSDKClient` rather than `query()`,
because `query()` starts a fresh session per call and cannot be interrupted, and
a voice assistant lives and dies on interruption. And it runs with
`include_partial_messages`, because without it the first token and the last
arrive together: measured on this machine, 5926ms to first text without it
against 2064ms with it. The chunker downstream can only start speaking early if
something is arriving early.

The connection is made once at boot and held. The cost of that is one process
that must be kept alive; the benefit is that a turn does not pay for a
handshake, and the system prompt stays cached between turns.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    UserMessage,
)

from jarvis.config import CONFIG, Config
from jarvis.events import EventBus
from jarvis.guard import Guard
from jarvis.tools import MemoryStore, TimerService, build_server

log = logging.getLogger("jarvis.brain")

# Built-in tools JARVIS is given. Deliberately a short list: the full Claude
# Code set costs about 20,000 tokens of prefix and JARVIS has no use for most
# of it. Phase 4 adds its own through the MCP server, not through this.
BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]

DOMAINS = {
    "Bash": "SHELL",
    "Read": "FILES",
    "Write": "FILES",
    "Edit": "FILES",
    "Glob": "FILES",
    "Grep": "FILES",
    "WebSearch": "WEB",
    "WebFetch": "WEB",
}


def label_for(tool_name: str) -> str:
    """ "JARVIS · SET TIMER" — short, uppercase, for the HUD's activity column."""
    if tool_name.startswith("mcp__"):
        _, server, action = tool_name.split("__", 2)
        return f"{server.upper()} · {action.replace('_', ' ').upper()}"
    domain = DOMAINS.get(tool_name, "TOOL")
    return f"{domain} · {tool_name.upper()}"


def detail_for(tool_name: str, tool_input: dict[str, Any]) -> str:
    """One safe line. Never command output, file contents or message bodies."""
    if tool_name == "Bash":
        command = str(tool_input.get("command", "")).strip()
        return command.split()[0] if command else ""
    for key in ("key", "label", "query", "pattern"):
        if key in tool_input:
            return str(tool_input[key])[:60]
    for key in ("file_path", "path"):
        if key in tool_input:
            return str(tool_input[key]).rsplit("/", 1)[-1]
    return ""


class Brain:
    """One long conversation with the model, interruptible at any point."""

    def __init__(
        self,
        config: Config = CONFIG,
        bus: EventBus | None = None,
        *,
        memory: MemoryStore,
        timers: TimerService,
        guard: Guard,
        system_prompt: str,
        resume: str | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._guard = guard
        self._client: ClaudeSDKClient | None = None
        self._server, self._tool_names = build_server(memory, timers, config)
        self._system_prompt = system_prompt
        self._resume = resume
        self._pending_tools: dict[str, tuple[str, str]] = {}
        self.session_id: str | None = None
        self.last_ttft_ms: float = 0.0
        self.last_cost_usd: float = 0.0
        self.last_cache_read: int = 0
        self.last_cache_write: int = 0
        self.last_input_tokens: int = 0

    def options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self._config.brain.model,
            system_prompt=self._system_prompt,
            tools=BUILTIN_TOOLS,
            # Deliberately empty. An entry here auto-approves that tool *before*
            # can_use_tool is consulted — the SDK warns about it — which would
            # leave guard.py inert and `rm -rf` running unasked. Availability
            # comes from `tools` and `mcp_servers`; permission comes from the
            # guard, and only from the guard.
            allowed_tools=[],
            mcp_servers={"jarvis": self._server},
            can_use_tool=self._guard.can_use_tool,
            permission_mode=self._config.brain.permission_mode,
            include_partial_messages=True,
            max_turns=self._config.brain.max_turns,
            max_budget_usd=self._config.brain.max_budget_usd,
            resume=self._resume,
            # A neutral working directory with no settings loaded. Pointed at the
            # repo it reads CLAUDE.md and starts answering questions about its own
            # build; it also costs ~250ms of extra prefill per turn.
            cwd=tempfile.mkdtemp(prefix="jarvis-agent-"),
            setting_sources=[],
        )

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = ClaudeSDKClient(options=self.options())
        await self._client.connect()
        log.info("agent connected: %s", self._config.brain.model)

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()

    async def interrupt(self) -> None:
        """Cancel the in-flight turn — the tool calls, not just the audio."""
        if self._client is None:
            return
        try:
            await self._client.interrupt()
            log.info("agent turn interrupted")
        except Exception as exc:  # noqa: BLE001 - interrupting must never raise
            log.warning("could not interrupt the agent: %s", exc)

    async def ask(self, text: str) -> AsyncIterator[str]:
        """Yield speakable fragments. Tool activity goes to the bus, not the voice."""
        await self.connect()
        assert self._client is not None
        started = time.perf_counter()
        self.last_ttft_ms = 0.0
        await self._client.query(text)
        async for message in self._client.receive_response():
            if isinstance(message, StreamEvent):
                async for fragment in self._from_stream(message, started):
                    yield fragment
            elif isinstance(message, UserMessage):
                self._tool_results(message)
            elif isinstance(message, AssistantMessage):
                self._note_tool_blocks(message)
            elif isinstance(message, ResultMessage):
                self._finish(message)
                break

    async def _from_stream(self, message: StreamEvent, started: float) -> AsyncIterator[str]:
        event = message.event
        kind = event.get("type")
        if kind == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                piece = delta.get("text", "")
                if piece:
                    if not self.last_ttft_ms:
                        self.last_ttft_ms = (time.perf_counter() - started) * 1000
                    yield piece
        elif kind == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                self._tool_started(str(block.get("id", "")), str(block.get("name", "")), {})

    def _note_tool_blocks(self, message: AssistantMessage) -> None:
        """A fallback for tools we did not see start, and the resolved input."""
        for block in message.content:
            if type(block).__name__ != "ToolUseBlock":
                continue
            tool_id = str(getattr(block, "id", ""))
            name = str(getattr(block, "name", ""))
            args = getattr(block, "input", {}) or {}
            if tool_id not in self._pending_tools:
                self._tool_started(tool_id, name, args)
            elif self._bus is not None:
                detail = detail_for(name, args)
                if detail:
                    self._pending_tools[tool_id] = (self._pending_tools[tool_id][0], detail)

    def _tool_started(self, tool_id: str, name: str, args: dict[str, Any]) -> None:
        if not tool_id or tool_id in self._pending_tools:
            return
        label = label_for(name)
        self._pending_tools[tool_id] = (label, detail_for(name, args))
        log.info("tool: %s", label)
        if self._bus is not None:
            self._bus.publish_tool_start(tool_id, name, label)

    def _tool_results(self, message: UserMessage) -> None:
        content = message.content if isinstance(message.content, list) else []
        for block in content:
            if type(block).__name__ != "ToolResultBlock":
                continue
            tool_id = str(getattr(block, "tool_use_id", ""))
            label, detail = self._pending_tools.pop(tool_id, ("TOOL", ""))
            ok = not bool(getattr(block, "is_error", False))
            if self._bus is not None:
                self._bus.publish_tool_end(tool_id, label, ok=ok, detail=detail or None)

    def _finish(self, message: ResultMessage) -> None:
        self.session_id = message.session_id
        self.last_cost_usd = message.total_cost_usd or 0.0
        usage = message.usage or {}
        self.last_cache_read = int(usage.get("cache_read_input_tokens", 0))
        self.last_cache_write = int(usage.get("cache_creation_input_tokens", 0))
        self.last_input_tokens = int(usage.get("input_tokens", 0))
        for tool_id, (label, detail) in list(self._pending_tools.items()):
            # The turn ended with a tool still open — interrupted, most likely.
            if self._bus is not None:
                self._bus.publish_tool_end(tool_id, label, ok=False, detail=detail or None)
        self._pending_tools.clear()
        if message.is_error:
            log.warning("agent turn ended in error: %s", message.errors or message.subtype)

    def save_session(self, path: Any) -> None:
        if self.session_id:
            path.write_text(json.dumps({"session_id": self.session_id}), encoding="utf-8")


def last_session(path: Any) -> str | None:
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["session_id"])
    except Exception:  # noqa: BLE001 - no session to resume is the normal case
        return None
