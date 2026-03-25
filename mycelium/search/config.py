"""Search result models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from mycelium.core.models import Neuron, Signal, Synapse


class SearchMethod(StrEnum):
    vector = "vector"
    bm25   = "bm25"
    bfs    = "bfs"
    hyde   = "hyde"


class ScoredNeuron(BaseModel):
    """Neuron with search relevance score."""
    neuron: Neuron
    score:  float = 0.0


class ScoredSynapse(BaseModel):
    """Synapse with endpoints + score."""
    synapse:     Synapse
    source_name: str   = ""
    target_name: str   = ""
    score:       float = 0.0


class SearchResults(BaseModel):
    """Hybrid search output."""
    neurons:     list[ScoredNeuron]  = Field(default_factory=list)
    synapses:    list[ScoredSynapse] = Field(default_factory=list)
    signals:     list[Signal]        = Field(default_factory=list)
    methods:     list[SearchMethod]  = Field(default_factory=list)
    duration_ms: int                 = 0
