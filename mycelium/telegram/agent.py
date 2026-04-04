"""Agent subprocess: claude -p wrapper with NDJSON streaming for Telegram."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()

# MCP tools the agent is allowed to use
_ALLOWED_TOOLS = "mcp__mycelium__*"

_SYSTEM_PROMPT = (
    "You are MYCELIUM assistant — a personal knowledge graph interface. "
    "You have access to MCP tools (mcp__mycelium__*) that let you search, "
    "add, and manage the user's knowledge graph. "
    "ALWAYS use mcp__mycelium__search to answer questions about what the user knows. "
    "Use mcp__mycelium__add_signal to capture new information. "
    "Respond concisely in the user's language. "
    "Do not use markdown tables — use plain text lists."
)


@dataclass
class AgentChunk:
    """One piece of agent output for streaming delivery."""
    text:       str            # accumulated text so far (not delta)
    delta:      str  = ""      # new text since last chunk
    is_final:   bool = False
    session_id: str  = ""
    usage:      dict = field(default_factory=dict)


class AgentProcess:
    """Manages claude -p subprocesses with per-chat sessions."""

    def __init__(
        self,
        model:    str = "sonnet",
        max_turns: int = 3,
        timeout:  float = 120.0,
    ) -> None:
        self._model     = model
        self._max_turns = max_turns
        self._timeout   = timeout
        self._sessions: dict[str, str] = {}  # chat_id → session_id
        self._process:  asyncio.subprocess.Process | None = None
        self._context:  str = ""

    def set_context(self, context: str) -> None:
        """Inject graph context into next system prompt."""
        self._context = context

    async def run(
        self,
        text:    str,
        chat_id: str,
    ) -> AsyncIterator[AgentChunk]:
        """Spawn claude -p, yield AgentChunks as text streams in."""
        cmd = [
            "claude", "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model",         self._model,
            "--max-turns",     str(self._max_turns),
            "--allowedTools",  _ALLOWED_TOOLS,
        ]

        # Resume existing session or start new
        session_id = self._sessions.get(chat_id)
        if session_id:
            cmd.extend(["--resume", session_id])

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=32 * 1024 * 1024,
            )
        except FileNotFoundError:
            yield AgentChunk(
                text="Claude CLI not found in container.",
                is_final=True,
            )
            return

        # Send prompt (with system prompt on first call, bare on resume)
        assert self._process.stdin is not None
        ctx_block = f"\n\n{self._context}" if self._context else ""
        prompt    = text if session_id else f"{_SYSTEM_PROMPT}{ctx_block}\n\n{text}"
        self._process.stdin.write(prompt.encode())
        await self._process.stdin.drain()
        self._process.stdin.close()

        # Drain stderr in background
        stderr_buf = bytearray()

        async def _drain_stderr() -> None:
            assert self._process is not None
            assert self._process.stderr is not None
            while True:
                chunk = await self._process.stderr.read(4096)
                if not chunk:
                    break
                stderr_buf.extend(chunk)

        stderr_task = asyncio.create_task(_drain_stderr())

        # Stream NDJSON events
        prev_text = ""
        new_session_id = ""
        assert self._process.stdout is not None

        try:
            while True:
                try:
                    line = await asyncio.wait_for(
                        self._process.stdout.readline(),
                        timeout=self._timeout,
                    )
                except TimeoutError:
                    self._process.kill()
                    await self._process.wait()
                    yield AgentChunk(
                        text=prev_text or "Timeout: no response.",
                        is_final=True,
                    )
                    stderr_task.cancel()
                    return

                if not line:
                    break  # EOF

                raw = line.decode(errors="replace").strip()
                if not raw:
                    continue

                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                etype = ev.get("type", "")

                if etype == "system":
                    sid = ev.get("session_id", "")
                    if sid:
                        new_session_id = sid

                elif etype == "assistant":
                    for block in ev.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            cur_text = block["text"]
                            if cur_text != prev_text:
                                delta = cur_text[len(prev_text):]
                                prev_text = cur_text
                                yield AgentChunk(
                                    text=cur_text,
                                    delta=delta,
                                    session_id=new_session_id,
                                )

                elif etype == "result":
                    result_text = ev.get("result", prev_text)
                    usage = ev.get("usage", {})
                    if result_text and result_text != prev_text:
                        delta = result_text[len(prev_text):]
                        prev_text = result_text
                    else:
                        delta = ""
                    yield AgentChunk(
                        text=result_text or prev_text,
                        delta=delta,
                        is_final=True,
                        session_id=new_session_id,
                        usage=usage,
                    )

        except Exception:
            if self._process.returncode is None:
                self._process.kill()
            raise
        finally:
            await self._process.wait()
            stderr_task.cancel()

        # Store session for resume
        if new_session_id:
            self._sessions[chat_id] = new_session_id

        # If we got here without yielding a final chunk
        if prev_text:
            yield AgentChunk(text=prev_text, is_final=True)

        # Check for errors
        if self._process.returncode and self._process.returncode != 0:
            stderr_text = stderr_buf.decode(errors="replace").strip()
            # Session expired — clear and let next message start fresh
            if _is_session_error(stderr_text):
                self._sessions.pop(chat_id, None)
                if not prev_text:
                    yield AgentChunk(
                        text="Session expired. Send your message again.",
                        is_final=True,
                    )
            elif not prev_text:
                yield AgentChunk(
                    text=f"Agent error: {stderr_text[:200]}",
                    is_final=True,
                )

    def abort(self) -> bool:
        """Kill current subprocess. Returns True if killed."""
        if self._process and self._process.returncode is None:
            self._process.kill()
            log.info("agent.aborted")
            return True
        return False


def _is_session_error(stderr: str) -> bool:
    lower = stderr.lower()
    return any(kw in lower for kw in (
        "session not found", "session expired", "invalid session",
        "could not resume", "no such session",
    ))
