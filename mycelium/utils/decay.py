"""Decay mechanism: consolidation through repetition."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from mycelium.config import DecaySettings


def calc_decay_rate(
    confirmations: int,
    settings:      DecaySettings | None = None,
) -> float:
    """decay_rate = base / (1 + confirmations * factor), clamped to min."""
    s    = settings or DecaySettings()
    rate = s.base_rate / (1 + confirmations * s.consolidation_factor)
    return max(rate, s.min_rate)


def effective_weight(
    importance: float,
    decay_rate: float,
    freshness:  datetime,
    now:        datetime | None = None,
) -> float:
    """R5.1: importance * recency. recency = exp(-decay_rate * days)."""
    now  = now or datetime.now(UTC)
    days = max(0.0, (now - freshness).total_seconds() / 86400)
    return importance * math.exp(-decay_rate * days)


def consolidate(
    importance:    float,
    confirmations: int,
    settings:      DecaySettings | None = None,
) -> tuple[float, float, int]:
    """Re-mention update: (new_importance, new_decay_rate, new_confirmations)."""
    s         = settings or DecaySettings()
    new_imp   = min(1.0, importance + s.evidence_boost)
    new_count = confirmations + 1
    new_rate  = calc_decay_rate(new_count, s)
    return new_imp, new_rate, new_count


def cypher_effective_weight(alias: str = "e", *, staleness_hours: int | None = None) -> str:
    """Cypher snippet that returns effective_weight for a Neuron.

    Reads materialized `<alias>.effective_weight` if present and fresh
    (within staleness_hours), else falls back to on-read calc:
        importance * exp(-decay_rate * days_since_freshness).

    Use everywhere decay weight is needed — single source of truth that
    stays consistent as `tend decay_sweep` materializes the value.
    """
    fallback = (
        f"coalesce({alias}.importance, {alias}.confidence) * "
        f"exp(-{alias}.decay_rate * "
        f"duration.between({alias}.freshness, datetime()).days)"
    )
    if staleness_hours is None:
        return f"coalesce({alias}.effective_weight, {fallback})"
    return (
        f"CASE WHEN {alias}.effective_weight IS NOT NULL "
        f"  AND {alias}.last_swept_at IS NOT NULL "
        f"  AND duration.between({alias}.last_swept_at, datetime()).hours "
        f"      < {staleness_hours} "
        f"THEN {alias}.effective_weight ELSE {fallback} END"
    )
