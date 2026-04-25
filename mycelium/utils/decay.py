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


def effective_weight_from_data(
    d:               dict,
    *,
    staleness_hours: int  = 24,
    now:             datetime | None = None,
) -> float:
    """Return effective_weight for a Neuron-as-dict.

    Prefers materialized `effective_weight` (set by `tend decay_sweep`) when
    present and within `staleness_hours`; otherwise falls back to on-the-fly
    `importance × exp(-decay_rate × days)`. Mirrors `cypher_effective_weight()`
    for callers that work on already-fetched rows instead of inside Cypher.
    """
    now = now or datetime.now(UTC)

    cached = d.get("effective_weight")
    swept  = d.get("last_swept_at")
    if cached is not None and swept is not None:
        s = swept.to_native() if hasattr(swept, "to_native") else swept
        if isinstance(s, datetime) and (now - s).total_seconds() < staleness_hours * 3600:
            return float(cached)

    imp   = (d.get("propagated_confidence")
             or d.get("importance")
             or d.get("confidence") or 1.0)
    rate  = d.get("decay_rate") or 0.008
    fresh = d.get("freshness")
    if hasattr(fresh, "to_native"):
        fresh = fresh.to_native()
    if not isinstance(fresh, datetime):
        fresh = now
    return effective_weight(imp, rate, fresh, now)


def cypher_effective_weight(alias: str = "e", *, staleness_hours: int | None = None) -> str:
    """Cypher snippet that returns effective_weight for a Neuron.

    Reads materialized `<alias>.effective_weight` if present and fresh
    (within staleness_hours), else falls back to on-read calc:
        importance * exp(-decay_rate * days_since_freshness).

    Use everywhere decay weight is needed — single source of truth that
    stays consistent as `tend decay_sweep` materializes the value.
    """
    # NOTE: use duration.inDays(...).days / duration.inSeconds(...).hours.
    # duration.between(...).days returns only the day-component of a calendar
    # (months + days) decomposition — it gives 0 for any delta ≥ 1 month, and
    # .between(...).hours similarly drops the day-part for deltas ≥ 1 day.
    fallback = (
        f"coalesce({alias}.importance, {alias}.confidence) * "
        f"exp(-{alias}.decay_rate * "
        f"duration.inDays({alias}.freshness, datetime()).days)"
    )
    if staleness_hours is None:
        return f"coalesce({alias}.effective_weight, {fallback})"
    return (
        f"CASE WHEN {alias}.effective_weight IS NOT NULL "
        f"  AND {alias}.last_swept_at IS NOT NULL "
        f"  AND duration.inSeconds({alias}.last_swept_at, datetime()).hours "
        f"      < {staleness_hours} "
        f"THEN {alias}.effective_weight ELSE {fallback} END"
    )
