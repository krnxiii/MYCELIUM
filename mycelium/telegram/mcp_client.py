"""MCP HTTP client: calls Data Node tools via streamable-http."""

from __future__ import annotations

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

    async def connect(self) -> None:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        http = httpx.AsyncClient(headers=headers, timeout=30.0)

        read, write, _ = await self._stack.enter_async_context(
            streamable_http_client(self._url, http_client=http),
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(read, write),
        )
        await self._session.initialize()
        log.info("mcp_client.connected", url=self._url)

    async def close(self) -> None:
        if self._stack:
            await self._stack.aclose()
            self._stack = None
            self._session = None
            log.info("mcp_client.closed")

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call MCP tool, return parsed JSON result (or raw text)."""
        if not self._session:
            raise RuntimeError("MCPClient not connected")
        result: CallToolResult = await self._session.call_tool(name, arguments)
        return _parse_result(result)

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
