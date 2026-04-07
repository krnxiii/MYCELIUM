#!/usr/bin/env python3
"""Upload a local file to MYCELIUM vault on remote VPS.

Usage: python3 scripts/vault-upload.py <file_path> [category]

Reads VPS connection from ~/.mycelium/.vps (created by connect-vps.sh).
Base64-encodes the file and calls vault_store via fastmcp client.
Prints JSON result on success, exits 1 on error.
"""
import asyncio
import base64
import json
import sys
from pathlib import Path

CLAUDE_JSON = Path.home() / ".claude.json"


def load_mcp_config() -> tuple[str, str]:
    """Read mycelium MCP URL and token from ~/.claude.json."""
    if not CLAUDE_JSON.exists():
        print(json.dumps({"error": "~/.claude.json not found. Run connect-vps.sh first."}))
        sys.exit(1)
    cfg = json.loads(CLAUDE_JSON.read_text())
    mcp = cfg.get("mcpServers", {}).get("mycelium", {})
    url = mcp.get("url", "")
    auth = mcp.get("headers", {}).get("Authorization", "")
    tok  = auth.removeprefix("Bearer ").strip() if auth else ""
    if not url:
        print(json.dumps({"error": "mycelium MCP server not configured in ~/.claude.json"}))
        sys.exit(1)
    return url, tok


async def upload(file_path: str, category: str = "") -> None:
    from fastmcp import Client

    url, tok = load_mcp_config()

    p = Path(file_path)
    if not p.exists():
        print(json.dumps({"error": f"File not found: {file_path}"}))
        sys.exit(1)

    b64 = base64.b64encode(p.read_bytes()).decode()

    from fastmcp.client.transports import StreamableHttpTransport

    headers   = {"Authorization": f"Bearer {tok}"} if tok else {}
    transport = StreamableHttpTransport(url, headers=headers)
    client    = Client(transport)

    async with client:
        result = await client.call_tool(
            "vault_store",
            {"file_content": b64, "file_name": p.name, "category": category},
        )

    # Extract text content from MCP CallToolResult
    for item in result.content:
        if hasattr(item, "text"):
            try:
                print(json.dumps(json.loads(item.text)))
            except (json.JSONDecodeError, AttributeError):
                print(item.text)
            return
    print(json.dumps({"error": "No text content in response"}))
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file_path> [category]")
        sys.exit(1)
    asyncio.run(upload(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
