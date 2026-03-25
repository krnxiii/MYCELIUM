"""Pydantic models: Signal, Neuron, Synapse, Mention."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


# ── Enums ────────────────────────────────────────────────────────────


class SignalType(StrEnum):
    message = "message"
    text    = "text"
    json    = "json"
    file    = "file"


class SignalStatus(StrEnum):
    pending    = "pending"
    extracting = "extracting"
    saved      = "saved"
    failed     = "failed"


# ── Signal ─────────────────────────────────────────────────────────


class Signal(BaseModel):
    """Raw input unit. Everything enters as a signal."""

    uuid:              str          = Field(default_factory=_uuid)
    name:              str          = ""
    content:           str          = ""
    content_embedding: list[float]  = Field(default_factory=list)
    source_type:       SignalType   = SignalType.text
    source_desc:       str          = ""
    status:            SignalStatus = SignalStatus.pending
    valid_at:          datetime     = Field(default_factory=_now)
    created_at:        datetime     = Field(default_factory=_now)


# ── Neuron ─────────────────────────────────────────────────────────


class Neuron(BaseModel):
    """Extracted knowledge node with decay + consolidation.

    R5.1 Three-Axis Scoring:
      importance — stable significance (birthday=1.0 forever)
      recency    — computed: exp(-decay_rate * days)
      relevance  — per-query: cosine(query, embedding)
    """

    uuid:              str             = Field(default_factory=_uuid)
    name:              str             = ""
    neuron_type:       str             = ""
    summary:           str             = ""
    name_embedding:    list[float]     = Field(default_factory=list)
    summary_embedding: list[float]     = Field(default_factory=list)
    importance:        float           = Field(default=1.0, ge=0.0, le=1.0)
    confidence:        float           = Field(default=1.0, ge=0.0, le=1.0)  # legacy alias
    decay_rate:        float           = Field(default=0.008, ge=0.0)
    confirmations:     int             = Field(default=0, ge=0)
    freshness:         datetime        = Field(default_factory=_now)
    attributes:        dict[str, Any]  = Field(default_factory=dict)
    origin:            str             = "raw"      # raw | derived
    created_at:        datetime        = Field(default_factory=_now)
    expires_at:        datetime | None = None


# ── Synapse ────────────────────────────────────────────────────────


class Synapse(BaseModel):
    """Semantic edge between neurons. Bi-temporal, searchable."""

    uuid:           str             = Field(default_factory=_uuid)
    source_uuid:    str             = ""
    target_uuid:    str             = ""
    relation:       str             = ""
    fact:           str             = ""
    fact_embedding: list[float]     = Field(default_factory=list)
    episodes:       list[str]       = Field(default_factory=list)
    confidence:     float           = Field(default=1.0, ge=0.0, le=1.0)
    valid_at:       datetime | None = None
    invalid_at:     datetime | None = None
    created_at:     datetime        = Field(default_factory=_now)
    expired_at:     datetime | None = None
    origin:         str             = "raw"      # raw | derived
    attributes:     dict[str, Any]  = Field(default_factory=dict)


# ── Mention ──────────────────────────────────────────────────────────


class Mention(BaseModel):
    """Signal → Neuron link."""

    uuid:        str      = Field(default_factory=_uuid)
    source_uuid: str      = ""
    target_uuid: str      = ""
    created_at:  datetime = Field(default_factory=_now)
