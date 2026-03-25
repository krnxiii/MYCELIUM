"""Direct API LLM backend via httpx (OpenAI-compatible).

Replaces litellm — zero supply-chain risk, ~80 lines vs 15MB dependency.
Works with any OpenAI-compatible endpoint: Anthropic, OpenAI, Ollama,
vLLM, LM Studio, DeepInfra, Together, etc.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog

from mycelium.config import LLMSettings
from mycelium.exceptions import ExtractionError
from mycelium.llm.base import LLMBackend, LLMProgressFn, parse_json

log = structlog.get_logger()


class LiteLLMClient(LLMBackend):
    """OpenAI-compatible LLM backend via httpx.

    Name kept as LiteLLMClient for config backwards compat
    (provider="litellm" still works).
    """

    def __init__(self, settings: LLMSettings | None = None) -> None:
        self._s = settings or LLMSettings()

    @property
    def model(self) -> str:
        return self._s.model

    async def generate(
        self, prompt: str, *,
        session:     Any = None,
        on_progress: LLMProgressFn = None,
    ) -> dict[str, Any]:
        """API → parsed JSON."""
        raw = await self._call(prompt, session=session, on_progress=on_progress)
        return parse_json(raw)

    async def generate_text(
        self, prompt: str, *,
        session:     Any = None,
        on_progress: LLMProgressFn = None,
    ) -> str:
        """API → raw text."""
        return await self._call(prompt, session=session, on_progress=on_progress)

    async def _call(
        self, prompt: str, *,
        session:     Any = None,
        on_progress: LLMProgressFn = None,
    ) -> str:
        t0       = time.monotonic()
        messages = self._build_messages(prompt, session)
        base     = self._s.api_base.rstrip("/") if self._s.api_base else ""
        url      = f"{base}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._s.api_key:
            headers["Authorization"] = f"Bearer {self._s.api_key}"

        log.info("api_llm_started",
                 prompt_len=len(prompt), model=self._s.model,
                 session="yes" if session else "no")

        last_err: Exception | None = None

        for attempt in range(self._s.max_retries + 1):
            try:
                if on_progress:
                    on_progress("calling LLM\u2026")

                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self._s.timeout),
                ) as client:
                    resp = await client.post(
                        url, json={"model": self._s.model, "messages": messages},
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                text = data["choices"][0]["message"]["content"] or ""

                if session is not None and hasattr(session, "append"):
                    session.append({"role": "assistant", "content": text})

                usage = data.get("usage", {})
                ms    = int((time.monotonic() - t0) * 1000)
                log.info("api_llm_done",
                         response_len=len(text), duration_ms=ms,
                         in_tok=usage.get("prompt_tokens"),
                         out_tok=usage.get("completion_tokens"))

                if on_progress:
                    parts: list[str] = []
                    if usage.get("completion_tokens"):
                        parts.append(f"{usage['completion_tokens']}tok")
                    parts.append(f"{ms / 1000:.1f}s")
                    on_progress(f"done ({', '.join(parts)})")

                return text

            except Exception as e:
                last_err = ExtractionError(f"API LLM error: {e}")
                log.warning("api_llm_error", attempt=attempt + 1, error=str(e))
                if attempt < self._s.max_retries:
                    await asyncio.sleep(2 ** attempt)

        raise last_err or ExtractionError("API LLM failed")

    def _build_messages(
        self, prompt: str, session: Any,
    ) -> list[dict[str, str]]:
        """Build messages array from prompt and optional session history."""
        if session is not None and isinstance(session, list):
            session.append({"role": "user", "content": prompt})
            return list(session)

        if session is not None and hasattr(session, "system_prompt"):
            msgs: list[dict[str, str]] = [
                {"role": "system", "content": session.system_prompt},
            ]
            msgs.append({"role": "user", "content": prompt})
            return msgs

        return [{"role": "user", "content": prompt}]
