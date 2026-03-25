"""Local embedder via fastembed (ONNX runtime, no torch dependency)."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from mycelium.config import SemanticSettings
from mycelium.exceptions import EmbeddingError

log = structlog.get_logger()


class LocalEmbedder:
    """BGE-M3 embedding via fastembed (ONNX, ~200MB vs torch ~2GB)."""

    def __init__(self, settings: SemanticSettings) -> None:
        self._s     = settings
        self._model: Any = None

    @property
    def dimensions(self) -> int:
        return self._s.dimensions

    @property
    def model_name(self) -> str:
        return self._s.model_name

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise EmbeddingError(
                "fastembed not installed. pip install fastembed",
            ) from e
        self._model = TextEmbedding(model_name=self._s.model_name)
        log.info("fastembed_loaded", model=self._s.model_name)
        return self._model

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        model   = self._load()
        prepped = [t.strip()[:self._s.max_tokens * 4] for t in texts]
        results = list(model.embed(prepped))
        return [v.tolist() for v in results]

    async def embed(self, text: str) -> list[float]:
        return (await asyncio.to_thread(self._embed_sync, [text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed_sync, texts) if texts else []
