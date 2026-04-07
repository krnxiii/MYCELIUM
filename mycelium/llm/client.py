"""LLMClient: CC CLI subprocess wrapper + streaming + retry + JSON parsing."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from mycelium.config import LLMSettings
from mycelium.exceptions import ExtractionError
from mycelium.llm.base import LLMBackend, LLMProgressFn, parse_json
from mycelium.llm.session import LLMSession, SessionExpiredError

if TYPE_CHECKING:
    pass

log = structlog.get_logger()


class LLMClient(LLMBackend):
    """Claude Code CLI subprocess wrapper.

    Calls `claude -p` with prompt via stdin, streams stdout incrementally.
    Features: session reuse, inactivity timeout, retry with exponential backoff.
    """

    def __init__(
        self,
        settings: LLMSettings | None = None,
    ) -> None:
        self._s     = settings or LLMSettings()
        self._last_session_id: str | None = None

    @property
    def model(self) -> str:
        return self._s.model

    async def generate(
        self, prompt: str, *,
        session:     LLMSession | None = None,
        on_progress: LLMProgressFn     = None,
    ) -> dict[str, Any]:
        """Run CC CLI → parsed JSON. Parse errors fail fast."""
        raw = await self._call_with_retry(
            prompt, session=session, on_progress=on_progress,
        )
        return parse_json(raw)

    async def generate_text(
        self, prompt: str, *,
        session:     LLMSession | None = None,
        on_progress: LLMProgressFn     = None,
    ) -> str:
        """Run CC CLI → raw text (no JSON parsing)."""
        return await self._call_with_retry(
            prompt, session=session, on_progress=on_progress,
        )

    async def _call_with_retry(
        self, prompt: str, *,
        session:     LLMSession | None = None,
        on_progress: LLMProgressFn     = None,
    ) -> str:
        """Retry loop with exponential backoff + session reuse."""
        t0  = time.monotonic()
        base_cmd = [
            "claude", "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model",         self._s.model,
            "--max-turns",     "1",
        ]
        sid_tag = (
            (session.session_id or "new")[:8] if session else "none"
        )
        log.info("cc_cli_started",
                 prompt_len=len(prompt), model=self._s.model,
                 session=sid_tag, inactivity_timeout=self._s.timeout)

        last_err: Exception | None = None

        for attempt in range(self._s.max_retries + 1):
            # Session augments command (--session-id or --resume)
            cmd = (
                session.build_cmd_args(base_cmd) if session
                else list(base_cmd)
            )
            # Session prepends system prompt on first call, user-only on resume
            actual_prompt = (
                session.prepare_prompt(prompt) if session else prompt
            )

            try:
                raw = await self._call_streaming(
                    cmd, actual_prompt, on_progress=on_progress,
                )
            except SessionExpiredError:
                if session and session.is_initialized:
                    log.warning("cc_cli_session_expired",
                                session_id=session.session_id[:8],
                                attempt=attempt + 1)
                    session.invalidate()
                    continue  # retry as fresh session
                raise ExtractionError("CC CLI session error (no session)")
            except TimeoutError:
                log.error("cc_cli_timeout",
                          attempt=attempt + 1, timeout_sec=self._s.timeout)
                raise ExtractionError(
                    f"CC CLI inactivity timeout ({self._s.timeout:.0f}s no data)",
                )
            except ExtractionError:
                raise                          # permanent (CLI not found)
            except Exception as e:
                last_err = ExtractionError(f"CC CLI error: {e}")
                log.warning("cc_cli_error", attempt=attempt + 1, error=str(e))
            else:
                # Empty response on session call → invalidate and retry
                if not raw and session:
                    log.warning("cc_cli_empty_session_response",
                                session=sid_tag, attempt=attempt + 1)
                    session.invalidate()
                    continue

                # Success — update session state
                if session:
                    if not session.is_initialized:
                        session.mark_initialized(
                            self._last_session_id or "",
                        )
                    else:
                        session.record_call()

                ms = int((time.monotonic() - t0) * 1000)
                log.info("cc_cli_done", response_len=len(raw), duration_ms=ms)
                return raw

            if attempt < self._s.max_retries:
                await asyncio.sleep(2 ** attempt)

        raise last_err or ExtractionError("CC CLI failed")

    async def _call_streaming(
        self,
        cmd:         list[str],
        prompt:      str,
        *,
        on_progress: LLMProgressFn = None,
    ) -> str:
        """Stream CC CLI subprocess via NDJSON events.

        Reads ``--output-format stream-json`` line by line, parsing events:
        system/init, assistant (thinking/text), rate_limit, result.
        Inactivity timeout kills the process if no event arrives for
        ``self._s.timeout`` seconds.

        Side effect: sets ``self._last_session_id`` from system event.
        """
        self._last_session_id = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin  = asyncio.subprocess.PIPE,
                stdout = asyncio.subprocess.PIPE,
                stderr = asyncio.subprocess.PIPE,
                limit  = 32 * 1024 * 1024,  # 32 MB — large extractions can hit 1 MB default
            )
        except FileNotFoundError:
            raise ExtractionError(
                "claude CLI not found. Install Claude Code.",
            ) from None

        # Send prompt, close stdin
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # Drain stderr in background (prevent pipe buffer deadlock)
        stderr_buf = bytearray()

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_buf.extend(chunk)

        stderr_task = asyncio.create_task(_drain_stderr())

        # Stream NDJSON events with inactivity timeout
        result_text   = ""
        inactivity_to = self._s.timeout
        tick_interval = 5.0
        assert proc.stdout is not None

        try:
            idle_since = time.monotonic()
            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=min(tick_interval, inactivity_to),
                    )
                except TimeoutError:
                    elapsed_idle = time.monotonic() - idle_since
                    if elapsed_idle >= inactivity_to:
                        proc.kill()
                        await proc.wait()
                        stderr_task.cancel()
                        raise
                    if on_progress:
                        on_progress(f"waiting… ({elapsed_idle:.0f}s)")
                    continue

                if not line:
                    break  # EOF

                idle_since = time.monotonic()
                text = self._handle_stream_event(
                    line, on_progress=on_progress,
                )
                if text is not None:
                    result_text = text
        except TimeoutError:
            raise
        except Exception:
            proc.kill()
            await proc.wait()
            stderr_task.cancel()
            raise

        # Wait for process exit + stderr drain
        await proc.wait()
        await stderr_task

        if proc.returncode != 0:
            stderr_text = stderr_buf.decode(errors="replace").strip()
            if _is_session_error(stderr_text):
                raise SessionExpiredError(stderr_text)
            raise RuntimeError(
                f"claude CLI rc={proc.returncode}: {stderr_text}",
            )

        return result_text

    # ── NDJSON event dispatcher ──────────────────────────

    def _handle_stream_event(
        self,
        line:        bytes,
        *,
        on_progress: LLMProgressFn = None,
    ) -> str | None:
        """Parse one NDJSON event, log it, fire progress.

        Returns result text if present, else None.
        """
        raw = line.decode(errors="replace").strip()
        if not raw:
            return None

        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("cc_stream_bad_json", line=raw[:200])
            return None

        etype = ev.get("type", "")

        # ── system/init ──────────────────────────────────
        if etype == "system":
            sid = ev.get("session_id") or ev.get("sessionId", "")
            if sid:
                self._last_session_id = sid
            log.debug("cc_stream_init",
                      subtype=ev.get("subtype"),
                      model=ev.get("model"),
                      session=sid[:8] if sid else "")
            if on_progress:
                on_progress("session started")
            return None

        # ── assistant (thinking / text) ──────────────────
        if etype == "assistant":
            for block in ev.get("message", {}).get("content", []):
                bt = block.get("type", "")
                if bt == "thinking":
                    t = block.get("thinking", "")
                    log.debug("cc_stream_thinking",
                              length=len(t), preview=t[:120])
                    if on_progress:
                        pre = t[:60].replace("\n", " ").strip()
                        on_progress(
                            f"thinking: {pre}…" if pre else "thinking…",
                        )
                elif bt == "text":
                    txt = block.get("text", "")
                    log.debug("cc_stream_text", length=len(txt))
                    if on_progress:
                        on_progress(f"response ({len(txt)} chars)")
                    return txt
            return None

        # ── rate_limit_event ─────────────────────────────
        if etype == "rate_limit_event":
            info = ev.get("rate_limit_info", {})
            log.info("cc_stream_rate_limit",
                     status=info.get("status"),
                     limit_type=info.get("rateLimitType"))
            return None

        # ── result ───────────────────────────────────────
        if etype == "result":
            if ev.get("is_error"):
                err = ev.get("result", "unknown CLI error")
                log.error("cc_stream_error", error=err[:300])
                raise ExtractionError(f"CC CLI: {err[:500]}")

            usage = ev.get("usage", {})
            log.info("cc_stream_result",
                     duration_ms=ev.get("duration_ms"),
                     cost_usd=ev.get("total_cost_usd"),
                     in_tok=usage.get("input_tokens"),
                     out_tok=usage.get("output_tokens"),
                     cache_read=usage.get("cache_read_input_tokens"),
                     cache_create=usage.get("cache_creation_input_tokens"))
            if on_progress:
                parts = []
                if usage.get("output_tokens"):
                    parts.append(f"{usage['output_tokens']}tok")
                cost = ev.get("total_cost_usd")
                if cost:
                    parts.append(f"${cost:.4f}")
                dur = ev.get("duration_ms")
                if dur:
                    parts.append(f"{dur / 1000:.1f}s")
                on_progress(
                    f"done ({', '.join(parts)})" if parts else "done",
                )
            return ev.get("result") or None

        log.debug("cc_stream_unknown", event_type=etype)
        return None


def _is_session_error(stderr: str) -> bool:
    """Detect session-related errors from CC CLI stderr."""
    lower = stderr.lower()
    return any(kw in lower for kw in (
        "session not found", "session expired", "invalid session",
        "could not resume", "no such session",
    ))
