"""Pydantic models for Domain Blueprints."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExtractionConfig(BaseModel):
    """How to extract knowledge in this domain."""

    model_config = ConfigDict(extra="ignore")

    skill:        str       = ""
    focus:        str       = ""
    neuron_types: list[str] = Field(default_factory=list)


class TrackingConfig(BaseModel):
    """What to track over time in this domain."""

    model_config = ConfigDict(extra="ignore")

    fields:   list[str] = Field(default_factory=list)
    analysis: str       = ""


class DomainBlueprint(BaseModel):
    """User-defined knowledge domain configuration.

    Stored as YAML in ~/.mycelium/domains/.
    Adapts ingestion, vault routing, and graph structure
    for a specific knowledge area.
    """

    model_config = ConfigDict(extra="ignore")

    name:          str              = ""
    description:   str              = ""
    vault_prefix:  str              = ""
    anchor_neuron: str              = ""
    anchor_type:   str              = ""
    anchor_uuid:   str              = ""
    triggers:      list[str]        = Field(default_factory=list)
    extraction:    ExtractionConfig = Field(default_factory=ExtractionConfig)
    tracking:      TrackingConfig   = Field(default_factory=TrackingConfig)
    created_at:    datetime         = Field(default_factory=_now)
    updated_at:    datetime         = Field(default_factory=_now)
