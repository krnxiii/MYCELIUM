"""LLM session: CC CLI session reuse via --session-id / --resume."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()


class SessionExpiredError(Exception):
    """CC CLI session expired or corrupted — retry as fresh."""


@dataclass
class LLMSession:
    """CC CLI session state for --session-id / --resume reuse.

    First call:  --session-id UUID, system prompt prepended to stdin
    Subsequent:  --resume UUID, user prompt only (session remembers context)
    """

    system_prompt: str
    _session_id:   str | None = field(default=None, init=False)
    _initialized:  bool       = field(default=False, init=False)
    _call_count:   int        = field(default=0,     init=False)
    _pending_id:   str        = field(default="",    init=False)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def call_count(self) -> int:
        return self._call_count

    def build_cmd_args(self, base_cmd: list[str]) -> list[str]:
        """Augment CLI command with session flags.

        First call:  add --session-id UUID (system prompt via prepare_prompt)
        Subsequent:  replace with --resume UUID, remove --model (session remembers)
        """
        cmd = list(base_cmd)

        if not self._initialized:
            # Fresh session — system prompt goes into stdin via prepare_prompt()
            self._pending_id = str(uuid.uuid4())
            cmd.extend(["--session-id", self._pending_id])
        else:
            # Resume existing session — remove --model (session remembers)
            cmd.extend(["--resume", self._session_id])
            try:
                idx = cmd.index("--model")
                cmd.pop(idx)  # --model
                cmd.pop(idx)  # value
            except ValueError:
                pass
        return cmd

    def prepare_prompt(self, user_prompt: str) -> str:
        """Build actual stdin payload.

        First call:  system_prompt + user_prompt (model sees full context)
        Resume:      user_prompt only (session remembers system context)
        """
        if not self._initialized:
            return f"{self.system_prompt}\n\n{user_prompt}"
        return user_prompt

    def mark_initialized(self, session_id: str) -> None:
        """Called after first successful response."""
        self._session_id  = session_id or self._pending_id
        self._initialized = True
        self._call_count  = 1
        log.info("llm_session_initialized",
                 session_id=self._session_id[:8],
                 system_prompt_len=len(self.system_prompt))

    def record_call(self) -> None:
        """Record a successful subsequent call."""
        self._call_count += 1

    def invalidate(self) -> None:
        """Reset session (e.g. after resume failure)."""
        old_id = self._session_id
        self._session_id  = None
        self._initialized = False
        self._pending_id  = ""
        log.warning("llm_session_invalidated",
                    old_session_id=(old_id or "")[:8],
                    calls_completed=self._call_count)
        self._call_count = 0
