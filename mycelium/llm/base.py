"""LLM backend: abstract interface for language model providers."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import structlog

from mycelium.exceptions import ExtractionError

try:
    from json_repair import repair_json as _repair_json
    _REPAIR_AVAILABLE = True
except ImportError:
    _REPAIR_AVAILABLE = False

_log = structlog.get_logger()

LLMProgressFn = Callable[[str], None] | None


class LLMBackend(ABC):
    """Abstract LLM backend. Implementations: CCLIClient, APILLMClient."""

    @abstractmethod
    async def generate(
        self, prompt: str, *,
        session:     Any  = None,
        on_progress: LLMProgressFn = None,
    ) -> dict[str, Any]:
        """Run LLM → parsed JSON dict."""

    @abstractmethod
    async def generate_text(
        self, prompt: str, *,
        session:     Any  = None,
        on_progress: LLMProgressFn = None,
    ) -> str:
        """Run LLM → raw text string."""


def parse_json(text: str) -> dict[str, Any]:
    """Extract JSON dict from LLM text response."""
    if not text:
        raise ExtractionError("Empty LLM response")

    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Markdown code block: ```json ... ``` or ``` ... ```
    for marker in ("```json\n", "```json\r\n", "```\n"):
        start = text.find(marker)
        if start >= 0:
            start += len(marker)
            end = text.find("```", start)
            if end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass

    # First { ... } block
    start = text.find("{")
    if start >= 0:
        end = text.rfind("}")
        if end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

    # Truncated JSON recovery (large outputs cut mid-stream)
    if _REPAIR_AVAILABLE and start >= 0:
        try:
            fragment = text[start:]
            repaired = _repair_json(fragment, skip_json_loads=True, return_objects=True)
            if isinstance(repaired, dict) and repaired:
                _log.warning("json_repaired", original_len=len(text), keys=list(repaired))
                return repaired
        except Exception:
            pass

    raise ExtractionError(f"No JSON in response: {text[:200]}")
