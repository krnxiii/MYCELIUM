"""Embedder providers: protocol, API, local, mock."""

from mycelium.embedder.client import EmbedderClient, MockEmbedder, make_embedder

__all__ = ["EmbedderClient", "MockEmbedder", "make_embedder"]
