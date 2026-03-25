"""Abstract GraphDriver interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class GraphDriver(ABC):
    """Database-agnostic graph operations."""

    @abstractmethod
    async def execute_query(
        self,
        query:  str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def build_indices(self) -> None:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...

    async def __aenter__(self) -> GraphDriver:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
