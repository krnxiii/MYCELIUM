"""Abstract GraphDriver interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

TxExecute = Callable[
    [str, "dict[str, Any] | None"],
    Awaitable[list[dict[str, Any]]],
]


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
    async def run_in_transaction(
        self,
        work: Callable[[TxExecute], Awaitable[T]],
    ) -> T:
        """Run callback inside a single write transaction.

        The callback receives an `execute` fn with the same signature as
        `execute_query`, but all calls share one tx — commit on success,
        rollback on exception.
        """
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
