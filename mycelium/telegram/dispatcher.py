"""Channel-agnostic dispatcher: routes messages to MCP tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from mycelium.telegram.agent import AgentProcess
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
    files:    list[Path]     = field(default_factory=list)  # reserved: file attachments
    reply_to: str | None     = None


@dataclass
class ChannelReply:
    text: str                          # plain text (always present)
    html: str | None         = None    # formatted (channel renders if supported)
    files: list[Path]        = field(default_factory=list)  # reserved: file attachments
    is_stream: bool          = False   # True = streaming chunk (use editMessageText)


class Dispatcher:
    """Dispatch messages: /commands → MCP fast mode, free text → agent."""

    def __init__(self, mcp: MCPClient, agent: AgentProcess) -> None:
        self._mcp   = mcp
        self._agent = agent

    # Commands that involve LLM processing (show progress)
    _SLOW_COMMANDS = frozenset({"/capture", "/search"})

    async def dispatch(self, msg: ChannelMessage) -> AsyncIterator[ChannelReply]:
        """Route message to handler, yield replies."""
        text = msg.text.strip()
        if text.startswith("/"):
            log.info("dispatch.fast", cmd=text.split()[0], chat_id=msg.chat_id)
            async for reply in self._fast(msg):
                yield reply
        else:
            log.info("dispatch.agent", chat_id=msg.chat_id, text_len=len(text))
            async for reply in self._agent_stream(msg):
                yield reply

    async def _agent_stream(self, msg: ChannelMessage) -> AsyncIterator[ChannelReply]:
        """Route free text to claude -p agent, yield streaming chunks."""
        # Build context on first message per chat (no session yet)
        if not self._agent.has_session(msg.chat_id):
            ctx = await _build_graph_context(self._mcp)
            if ctx:
                self._agent.set_context(ctx)
        async for chunk in self._agent.run(msg.text, msg.chat_id):
            yield ChannelReply(
                text=chunk.text,
                is_stream=not chunk.is_final,
            )

    def is_busy(self) -> bool:
        """Check if agent subprocess is currently running."""
        p = self._agent._process
        return p is not None and p.returncode is None

    def abort(self) -> bool:
        """Abort current agent process."""
        return self._agent.abort()

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
            log.info("dispatch.fast_done", cmd=cmd, chat_id=msg.chat_id)
        except RuntimeError as exc:
            err = str(exc)
            if "MCP unavailable" in err or "MCP unreachable" in err:
                log.error("dispatcher.mcp_down", cmd=cmd, error=err)
                yield ChannelReply(text="MCP server unavailable. Try again in a minute.")
            else:
                log.error("dispatcher.error", cmd=cmd, error=err)
                yield ChannelReply(text=f"Error: {err}")
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
        health = await self._mcp.call_tool("health")
        plain, html = format_health(health, {})
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


async def _build_graph_context(mcp: MCPClient) -> str:
    """Fetch graph snapshot for agent system prompt."""
    try:
        health  = await mcp.call_tool("health")
        neurons = await mcp.call_tool("list_neurons", {"limit": 10})
        domains = await mcp.call_tool("list_domains")

        parts = ["GRAPH CONTEXT:"]

        # Stats from health
        if isinstance(health, dict):
            stats = health.get("stats", health)
            parts.append(f"  Neurons: {stats.get('neuron_count', '?')} | "
                         f"Signals: {stats.get('signal_count', '?')} | "
                         f"Synapses: {stats.get('synapse_count', '?')}")

        # Top neurons
        if isinstance(neurons, list) and neurons:
            names = [n.get("name", "?") for n in neurons[:10]]
            parts.append(f"  Top neurons: {', '.join(names)}")

        # Domains
        if isinstance(domains, dict):
            domain_names = list(domains.keys())[:5]
            if domain_names:
                parts.append(f"  Domains: {', '.join(domain_names)}")

        return "\n".join(parts)
    except Exception as exc:
        log.warning("dispatcher.context_failed", error=str(exc))
        return ""
