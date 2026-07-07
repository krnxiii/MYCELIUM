"""MCP HTTP client: calls Data Node tools via streamable-http."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Any

import httpx
import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

log = structlog.get_logger()


class MCPClient:
    """Thin wrapper around MCP SDK client for tool calls."""

    def __init__(self, url: str, auth_token: str = "") -> None:
        self._url       = url
        self._token     = auth_token
        self._session:  ClientSession | None = None
        self._stack:    AsyncExitStack | None = None
        # Single-flight: two handler tasks hitting call_tool while
        # disconnected would each build a stack + httpx client and leak all
        # but one (audit P8).
        self._lock      = asyncio.Lock()

    async def connect(self, retries: int = 5, backoff: float = 2.0) -> None:
        """Connect with retry + exponential backoff (single-flight)."""
        async with self._lock:
            if self._session is not None:
                return  # another task already reconnected
            for attempt in range(1, retries + 1):
                try:
                    await self._connect_once()
                    return
                except Exception as exc:
                    if attempt == retries:
                        raise
                    delay = backoff * attempt
                    log.warning("mcp_client.connect_retry",
                                attempt=attempt, delay=delay, error=str(exc))
                    await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            headers: dict[str, str] = {}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            # Own the httpx client through the stack so a failed handshake
            # can't leak it (it was created but never registered before).
            http = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers, timeout=30.0),
            )
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(self._url, http_client=http),
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            await stack.aclose()          # roll back partial setup
            raise
        self._stack   = stack
        self._session = session
        log.info("mcp_client.connected", url=self._url)

    async def close(self) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is None:
            return
        try:
            await stack.aclose()
            log.info("mcp_client.closed")
        except Exception as exc:
            # anyio can raise if the stack is closed from a different task than
            # opened it — state is already reset, so just note it.
            log.warning("mcp_client.close_error", error=str(exc))

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call MCP tool with auto-reconnect on failure."""
        for attempt in range(1, 3):
            if not self._session:
                try:
                    await self.connect(retries=3, backoff=1.0)
                except Exception as exc:
                    raise RuntimeError(f"MCP unavailable: {exc}") from exc
            try:
                result: CallToolResult = await self._session.call_tool(  # type: ignore[union-attr]
                    name, arguments,
                )
                return _parse_result(result)
            except Exception as exc:
                log.warning("mcp_client.call_failed", tool=name,
                            attempt=attempt, error=str(exc))
                # Connection broken — reset and retry once
                await self.close()
                if attempt == 2:
                    raise RuntimeError(
                        f"MCP tool '{name}' failed after reconnect: {exc}"
                    ) from exc
        raise RuntimeError("MCP unreachable")  # unreachable, satisfies type checker

    @property
    def connected(self) -> bool:
        return self._session is not None


def _parse_result(result: CallToolResult) -> dict[str, Any]:
    """Extract first text content and parse as JSON."""
    for item in result.content:
        if isinstance(item, TextContent):
            try:
                return json.loads(item.text)  # type: ignore[no-any-return]
            except (json.JSONDecodeError, TypeError):
                return {"text": item.text}
    return {"text": str(result.content)}
