"""Agent subprocess: claude -p wrapper with NDJSON streaming for Telegram."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from time import monotonic

import structlog

log = structlog.get_logger()

# Tools the agent is allowed to use (broad: container limits blast radius)
_ALLOWED_TOOLS = "mcp__mycelium__*,Read,Glob,Grep,Bash,WebSearch,WebFetch,Write"

_SYSTEM_PROMPT = (
    "You are MYCELIUM assistant — a personal knowledge graph interface. "
    "You have access to MCP tools (mcp__mycelium__*) that let you search, "
    "add, and manage the user's knowledge graph. "
    "You also have Read, Glob, Grep, Bash, Write tools for file/system operations. "
    "You have WebSearch and WebFetch to search the internet and read web pages. "
    "ALWAYS use mcp__mycelium__search to answer questions about what the user knows. "
    "Use mcp__mycelium__add_signal to capture new information. "
    "For files/photos: use Read to view the content, vault_store to save in vault, "
    "add_signal with extracted info, vault_link to connect them. "
    "If a tool call fails, retry it once. "
    "Always report what happened: which tool, what error, whether retry helped. "
    "DIAGNOSTICS: if the user reports a bug or you encounter a persistent error, "
    "use Read/Grep to inspect source code at /app/mycelium/, diagnose the root cause, "
    "and report to the user: affected file/line, what's wrong, proposed fix. "
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
        model:       str = "sonnet",
        max_turns:   int = 10,
        session_ttl: int = 14400,
    ) -> None:
        self._model       = model
        self._max_turns   = max_turns
        self._session_ttl = session_ttl
        self._sessions:  dict[str, str]   = {}  # chat_id → session_id
        self._session_ts: dict[str, float] = {}  # chat_id → last activity
        self._process:   asyncio.subprocess.Process | None = None
        self._context:   str = ""

    def has_session(self, chat_id: str) -> bool:
        """Check if a session exists for chat_id."""
        return chat_id in self._sessions

    def is_running(self) -> bool:
        """Check if agent subprocess is currently active."""
        p = self._process
        return p is not None and p.returncode is None

    def set_context(self, context: str) -> None:
        """Inject graph context into next system prompt."""
        self._context = context

    def _evict_stale(self) -> None:
        """Remove sessions older than TTL."""
        now     = monotonic()
        expired = [k for k, ts in self._session_ts.items() if now - ts > self._session_ttl]
        for k in expired:
            self._sessions.pop(k, None)
            self._session_ts.pop(k, None)
        if expired:
            log.info("agent.sessions_evicted", count=len(expired))

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

        log.info("agent.started", chat_id=chat_id, model=self._model,
                 resume=bool(session_id), prompt_len=len(text),
                 has_context=bool(self._context))

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
                line = await self._process.stdout.readline()

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
                    result_text = ev.get("result", "")
                    subtype     = ev.get("subtype", "")
                    usage       = ev.get("usage", {})
                    # Handle max_turns error gracefully
                    if subtype == "error_max_turns" and not result_text:
                        result_text = prev_text or "Reached turn limit. Try a simpler question."
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
            self._sessions[chat_id]   = new_session_id
            self._session_ts[chat_id] = monotonic()
            self._evict_stale()
            log.info("agent.done", chat_id=chat_id, session_id=new_session_id[:8],
                     response_len=len(prev_text))

        # If we got here without yielding a final chunk
        if prev_text:
            yield AgentChunk(text=prev_text, is_final=True)

        # Check for errors
        if self._process.returncode and self._process.returncode != 0:
            stderr_text = stderr_buf.decode(errors="replace").strip()
            log.warning("agent.exit_error", rc=self._process.returncode,
                        stderr=stderr_text[:300])
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
