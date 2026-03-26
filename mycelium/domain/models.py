"""Pydantic models for Domain Blueprints."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExtractionConfig(BaseModel):
    """How to extract knowledge in this domain."""

    model_config = ConfigDict(extra="ignore")

    skill:        str       = ""
    focus:        str       = ""
    neuron_types: list[str] = Field(default_factory=list)


class FieldConfig(BaseModel):
    """Single tracking field definition."""

    model_config = ConfigDict(extra="ignore")

    label:     str              = ""
    aliases:   list[str]        = Field(default_factory=list)
    reference: list[float] | None = None  # [min, max] normal range


class ChartStyle(BaseModel):
    """Visual style for Tracker plugin charts."""

    model_config = ConfigDict(extra="ignore")

    type:       str  = "line"      # line | bar | scatter
    color:      str  = "#89b4fa"   # Catppuccin blue
    show_point: bool = True
    point_size: int  = 5
    height:     int  = 200


class TrackingConfig(BaseModel):
    """What to track over time in this domain."""

    model_config = ConfigDict(extra="ignore")

    fields:      dict[str, FieldConfig] = Field(default_factory=dict)
    analysis:    str                    = ""
    dashboard:   bool                   = True
    chart_style: ChartStyle             = Field(default_factory=ChartStyle)

    @field_validator("fields", mode="before")
    @classmethod
    def _coerce_fields(cls, v: list | dict) -> dict:
        """Backward compat: list[str] → dict[str, FieldConfig]."""
        if isinstance(v, list):
            return {name: FieldConfig() for name in v}
        if isinstance(v, dict):
            return {
                k: (v_ if isinstance(v_, FieldConfig) else FieldConfig(**v_)
                    if isinstance(v_, dict) else FieldConfig())
                for k, v_ in v.items()
            }
        return v


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
