"""Channel-agnostic dispatcher: routes messages to MCP tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from mycelium.telegram.formatter import (
    format_domains,
    format_health,
    format_neurons,
    format_search,
    format_signal_created,
    format_timeline,
)
from mycelium.telegram.mcp_client import MCPClient

log = structlog.get_logger()


@dataclass
class ChannelMessage:
    text:     str
    chat_id:  str            = ""
    files:    list[Path]     = field(default_factory=list)
    reply_to: str | None     = None


@dataclass
class ChannelReply:
    text: str                          # plain text (always present)
    html: str | None         = None    # formatted (channel renders if supported)
    files: list[Path]        = field(default_factory=list)


class Dispatcher:
    """Fast mode dispatcher: /commands → MCP HTTP tool calls."""

    def __init__(self, mcp: MCPClient) -> None:
        self._mcp = mcp

    # Commands that involve LLM processing (show progress)
    _SLOW_COMMANDS = frozenset({"/capture", "/search"})

    async def dispatch(self, msg: ChannelMessage) -> AsyncIterator[ChannelReply]:
        """Route message to handler, yield replies."""
        text = msg.text.strip()
        if text.startswith("/"):
            async for reply in self._fast(msg):
                yield reply
        else:
            # Phase 3: free text → claude -p
            yield ChannelReply(
                text="Free text mode coming in Phase 3. Use /capture or /search.",
            )

    async def _fast(self, msg: ChannelMessage) -> AsyncIterator[ChannelReply]:
        parts = msg.text.strip().split(maxsplit=1)
        cmd   = parts[0].lower().split("@")[0]  # strip @botname suffix
        arg   = parts[1] if len(parts) > 1 else ""

        # Progress feedback for slow commands
        if cmd in self._SLOW_COMMANDS and arg:
            yield ChannelReply(text="Processing...")

        try:
            match cmd:
                case "/capture":
                    yield await self._capture(arg)
                case "/search":
                    yield await self._search(arg)
                case "/status":
                    yield await self._status()
                case "/today":
                    yield await self._today()
                case "/neurons":
                    yield await self._neurons(arg)
                case "/domains":
                    yield await self._domains()
                case _:
                    yield ChannelReply(text=f"Unknown command: {cmd}")
        except Exception as exc:
            log.error("dispatcher.error", cmd=cmd, error=str(exc))
            yield ChannelReply(text=f"Error: {exc}")

    async def _capture(self, text: str) -> ChannelReply:
        if not text:
            return ChannelReply(text="Usage: /capture <text>")
        result = await self._mcp.call_tool("add_signal", {"content": text})
        plain, html = format_signal_created(result)
        return ChannelReply(text=plain, html=html)

    async def _search(self, query: str) -> ChannelReply:
        if not query:
            return ChannelReply(text="Usage: /search <query>")
        result = await self._mcp.call_tool("search", {"query": query, "top_k": 5})
        plain, html = format_search(result)
        return ChannelReply(text=plain, html=html)

    async def _status(self) -> ChannelReply:
        health  = await self._mcp.call_tool("health")
        metrics = await self._mcp.call_tool("get_metrics")
        plain, html = format_health(health, metrics)
        return ChannelReply(text=plain, html=html)

    async def _today(self) -> ChannelReply:
        result = await self._mcp.call_tool("get_signals", {"limit": 15})
        plain, html = format_timeline(result)
        return ChannelReply(text=plain, html=html)

    async def _neurons(self, type_filter: str) -> ChannelReply:
        args: dict[str, object] = {"limit": 15}
        if type_filter:
            args["neuron_type"] = type_filter
        result = await self._mcp.call_tool("list_neurons", args)
        plain, html = format_neurons(result)
        return ChannelReply(text=plain, html=html)

    async def _domains(self) -> ChannelReply:
        result = await self._mcp.call_tool("list_domains")
        plain, html = format_domains(result)
        return ChannelReply(text=plain, html=html)
