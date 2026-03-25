"""Embedder protocol, mock provider, factory."""

from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable

import structlog

from mycelium.config import SemanticSettings

log = structlog.get_logger()


# ── Protocol ─────────────────────────────────────────────


@runtime_checkable
class EmbedderClient(Protocol):
    """Abstract embedder interface."""

    @property
    def dimensions(self) -> int: ...

    @property
    def model_name(self) -> str: ...

    async def embed(self, text: str) -> list[float]: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    async def rerank(
        self, query: str, documents: list[str], top_n: int = 10,
    ) -> list[tuple[int, float]]:
        """Rerank documents by relevance. Returns [(index, score), ...]."""
        ...


# ── Mock (deterministic, for tests/dev) ──────────────────


class MockEmbedder:
    """SHA-256 hash → L2-normalized vector. Deterministic."""

    def __init__(self, dimensions: int = 1024) -> None:
        self._dims = dimensions

    @property
    def dimensions(self) -> int:
        return self._dims

    @property
    def model_name(self) -> str:
        return "mock"

    def _hash_vec(self, text: str) -> list[float]:
        d   = hashlib.sha256(text.encode()).digest()
        raw = [(d[i % len(d)] / 127.5) - 1.0 for i in range(self._dims)]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw] if norm else raw

    async def embed(self, text: str) -> list[float]:
        return self._hash_vec(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._hash_vec(t) for t in texts]

    async def rerank(
        self, query: str, documents: list[str], top_n: int = 10,
    ) -> list[tuple[int, float]]:
        """Mock rerank: return original order with fake scores."""
        return [(i, 1.0 / (i + 1)) for i in range(min(top_n, len(documents)))]


# ── Factory ──────────────────────────────────────────────


def make_embedder(settings: SemanticSettings) -> EmbedderClient:
    """Create embedder based on settings.provider."""
    if settings.provider == "api":
        from mycelium.embedder.api import APIEmbedder
        return APIEmbedder(settings)

    if settings.provider == "mock":
        return MockEmbedder(settings.dimensions)

    # "local" — try BGE-M3, fallback to mock
    try:
        from mycelium.embedder.local import LocalEmbedder
        return LocalEmbedder(settings)
    except Exception:
        log.warning("local_embedder_unavailable", fallback="mock")
        return MockEmbedder(settings.dimensions)
