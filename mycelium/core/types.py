"""Dependency injection container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mycelium.driver.driver import GraphDriver
    from mycelium.embedder.client import EmbedderClient
    from mycelium.llm.base import LLMBackend


@dataclass
class MyceliumClients:
    """Service container — passed through pipeline stages."""

    driver:   GraphDriver
    embedder: EmbedderClient
    llm:      LLMBackend
