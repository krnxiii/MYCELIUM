"""API embedder via httpx (OpenAI-compatible endpoints).

Replaces litellm — direct HTTP calls, zero supply-chain risk.
Works with any OpenAI-compatible embedding provider: DeepInfra,
OpenAI, Cohere, Voyage, Ollama, Azure, vLLM, etc.
"""

from __future__ import annotations

import httpx
import structlog

from mycelium.config import SemanticSettings
from mycelium.exceptions import EmbeddingError

log = structlog.get_logger()


class APIEmbedder:
    """Embedding via OpenAI-compatible API (direct httpx).

    Config mapping:
      api_base_url → base URL (e.g. "https://api.deepinfra.com/v1/openai")
      api_key      → Bearer token
      model_name   → model (e.g. "BAAI/bge-m3")
    """

    def __init__(self, settings: SemanticSettings) -> None:
        self._s    = settings
        self._base = settings.api_base_url.rstrip("/")

    @property
    def dimensions(self) -> int:
        return self._s.dimensions

    @property
    def model_name(self) -> str:
        return self._s.model_name

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._s.api_key:
            h["Authorization"] = f"Bearer {self._s.api_key}"
        return h

    async def _request(self, texts: list[str]) -> list[list[float]]:
        url  = f"{self._base}/embeddings"
        body = {
            "model":           self._s.model_name,
            "input":           texts,
            "encoding_format": "float",
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0),
            ) as client:
                resp = await client.post(url, json=body, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            raise EmbeddingError(f"Embedding API failed: {e}") from e

        items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]

    async def embed(self, text: str) -> list[float]:
        return (await self._request([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await self._request(texts) if texts else []

    async def rerank(
        self, query: str, documents: list[str], top_n: int = 10,
    ) -> list[tuple[int, float]]:
        """Rerank via OpenAI-compatible rerank endpoint."""
        if not documents:
            return []

        url  = f"{self._base}/rerank"
        body = {
            "model":     self._s.reranker_model,
            "query":     query,
            "documents": documents,
            "top_n":     top_n,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
            ) as client:
                resp = await client.post(url, json=body, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
            return [
                (item["index"], item["relevance_score"])
                for item in sorted(
                    data["results"],
                    key=lambda x: x["relevance_score"],
                    reverse=True,
                )
            ]
        except Exception as e:
            log.warning("rerank_failed", error=str(e))
            return []
