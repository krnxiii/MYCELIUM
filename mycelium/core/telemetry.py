"""R6.3: In-memory telemetry — per-tool counters, LLM token tracking, latency."""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator


@dataclass
class _ToolStats:
    calls:      int   = 0
    errors:     int   = 0
    total_ms:   float = 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0


@dataclass
class _LLMStats:
    calls:         int = 0
    input_tokens:  int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class _IngestStats:
    signals:        int = 0
    neurons_created: int = 0
    synapses_created: int = 0
    duplicates:     int = 0
    contradictions: int = 0


@dataclass
class _SearchStats:
    queries:    int   = 0
    total_ms:   float = 0.0
    total_results: int = 0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.queries if self.queries else 0.0

    @property
    def avg_results(self) -> float:
        return self.total_results / self.queries if self.queries else 0.0


class Telemetry:
    """Singleton in-memory telemetry. Resets on server restart."""

    _instance: Telemetry | None = None

    def __new__(cls) -> Telemetry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        self._started = time.monotonic()
        self._tools: dict[str, _ToolStats] = defaultdict(_ToolStats)
        self._llm    = _LLMStats()
        self._ingest = _IngestStats()
        self._search = _SearchStats()

    def reset(self) -> None:
        self._init()

    # ── Tool tracking ─────────────────────────────────────

    @contextmanager
    def track_tool(self, name: str) -> Generator[None, None, None]:
        t0 = time.monotonic()
        try:
            yield
            self._tools[name].calls += 1
        except Exception:
            self._tools[name].calls += 1
            self._tools[name].errors += 1
            raise
        finally:
            self._tools[name].total_ms += (time.monotonic() - t0) * 1000

    def record_tool(self, name: str, ms: float, error: bool = False) -> None:
        self._tools[name].calls += 1
        self._tools[name].total_ms += ms
        if error:
            self._tools[name].errors += 1

    # ── LLM tracking ──────────────────────────────────────

    def record_llm(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self._llm.calls += 1
        self._llm.input_tokens  += input_tokens
        self._llm.output_tokens += output_tokens

    # ── Ingestion tracking ────────────────────────────────

    def record_ingest(
        self, neurons: int = 0, synapses: int = 0,
        duplicates: int = 0, contradictions: int = 0,
    ) -> None:
        self._ingest.signals += 1
        self._ingest.neurons_created  += neurons
        self._ingest.synapses_created += synapses
        self._ingest.duplicates       += duplicates
        self._ingest.contradictions   += contradictions

    # ── Search tracking ───────────────────────────────────

    def record_search(self, ms: float, results: int) -> None:
        self._search.queries += 1
        self._search.total_ms += ms
        self._search.total_results += results

    # ── Snapshot ──────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        uptime_s = time.monotonic() - self._started
        tools = {
            name: {
                "calls":  s.calls,
                "errors": s.errors,
                "avg_ms": round(s.avg_ms, 1),
            }
            for name, s in sorted(self._tools.items())
            if s.calls > 0
        }
        return {
            "uptime_seconds": round(uptime_s),
            "tools": tools,
            "llm": {
                "calls":         self._llm.calls,
                "input_tokens":  self._llm.input_tokens,
                "output_tokens": self._llm.output_tokens,
                "total_tokens":  self._llm.total_tokens,
            },
            "ingestion": {
                "signals":          self._ingest.signals,
                "neurons_created":  self._ingest.neurons_created,
                "synapses_created": self._ingest.synapses_created,
                "duplicates":       self._ingest.duplicates,
                "contradictions":   self._ingest.contradictions,
            },
            "search": {
                "queries":     self._search.queries,
                "avg_ms":      round(self._search.avg_ms, 1),
                "avg_results": round(self._search.avg_results, 1),
            },
        }
