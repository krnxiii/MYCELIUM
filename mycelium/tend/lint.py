"""lint — read-only structural health check (R9.4).

Independent from sleep_report (which finds *distill* candidates: weak,
duplicates, gaps, bridges — mostly LLM-curated material). lint finds
*maintenance* issues a deterministic sweep can address: expired data
awaiting prune, zombie signals, broken edges, stale sweep state.

Output is structured (LintFinding list + score 0..1 + stats). Phantom-
neuron detection (R9.4 stretch) requires LLM and is intentionally absent
here — lint stays Tier 0.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from mycelium.config import TendSettings
from mycelium.driver.neo4j_driver import Neo4jDriver

log = structlog.get_logger()

Severity = Literal["low", "medium", "high"]

_SEVERITY_WEIGHT = {"low": 1, "medium": 3, "high": 9}
# Threshold at which a category contributes 1.0 to its severity bucket.
_SEVERITY_THRESHOLD = {"low": 50, "medium": 20, "high": 5}


@dataclass
class LintFinding:
    category: str
    severity: Severity
    count:    int
    message:  str
    samples:  list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "count":    self.count,
            "message":  self.message,
            "samples":  self.samples,
        }


@dataclass
class LintReport:
    findings: list[LintFinding] = field(default_factory=list)
    score:    float             = 1.0   # 1.0 = pristine, 0.0 = drowning
    stats:    dict[str, Any]    = field(default_factory=dict)
    elapsed_ms: int             = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings":   [f.to_dict() for f in self.findings],
            "score":      self.score,
            "stats":      self.stats,
            "elapsed_ms": self.elapsed_ms,
            "by_severity": {
                sev: sum(1 for f in self.findings if f.severity == sev)
                for sev in ("high", "medium", "low")
            },
        }


async def lint(
    drv:      Neo4jDriver,
    *,
    settings: TendSettings | None = None,
) -> LintReport:
    """Run all maintenance checks; never writes to the graph."""
    s   = settings or TendSettings()
    rep = LintReport()
    t0  = time.monotonic()

    rep.findings.extend(await _expired_data(drv))
    rep.findings.extend(await _signal_health(drv, s))
    rep.findings.extend(await _structural_integrity(drv))
    rep.findings.extend(await _stale_sweep(drv, s))
    rep.findings.extend(await _duplicate_names(drv))
    rep.stats        = await _stats(drv)
    rep.score        = _score(rep.findings)
    rep.elapsed_ms   = int((time.monotonic() - t0) * 1000)

    log.info("lint_done",
             findings=len(rep.findings),
             score=rep.score,
             elapsed_ms=rep.elapsed_ms)
    return rep


# ── Checks ──────────────────────────────────────────────────────────


async def _expired_data(drv: Neo4jDriver) -> list[LintFinding]:
    """Soft-deleted entities awaiting prune_dead."""
    rows = (await drv.execute_query(
        "OPTIONAL MATCH (n:Neuron) WHERE n.expired_at IS NOT NULL "
        "WITH count(n) AS expired_neurons "
        "OPTIONAL MATCH ()-[r:SYNAPSE]->() WHERE r.expired_at IS NOT NULL "
        "WITH expired_neurons, count(r) AS expired_synapses "
        "OPTIONAL MATCH ()-[r2:SYNAPSE]->() "
        "  WHERE r2.expired_at IS NULL "
        "    AND r2.expires_at IS NOT NULL "
        "    AND r2.expires_at < datetime() "
        "RETURN expired_neurons, expired_synapses, count(r2) AS past_ttl"
    ))[0]

    out: list[LintFinding] = []
    if rows["expired_neurons"]:
        out.append(LintFinding(
            "expired_neurons", "low", rows["expired_neurons"],
            "Soft-deleted neurons awaiting prune_dead",
        ))
    if rows["expired_synapses"]:
        out.append(LintFinding(
            "expired_synapses", "low", rows["expired_synapses"],
            "Soft-deleted synapses awaiting prune_dead",
        ))
    if rows["past_ttl"]:
        out.append(LintFinding(
            "past_ttl_synapses", "low", rows["past_ttl"],
            "Synapses past expires_at, not yet soft-expired",
        ))
    return out


async def _signal_health(drv: Neo4jDriver, s: TendSettings) -> list[LintFinding]:
    """Failed orphans + zombie extracting signals."""
    rows = (await drv.execute_query(
        "OPTIONAL MATCH (sig:Signal) "
        "  WHERE NOT EXISTS { MATCH (sig)-[:MENTIONS]->() } "
        "    AND sig.status = 'failed' "
        "WITH count(sig) AS orphan_failed "
        "OPTIONAL MATCH (z:Signal) "
        "  WHERE z.status = 'extracting' "
        "    AND z.created_at IS NOT NULL "
        "    AND duration.inSeconds(z.created_at, datetime()).hours > $hrs "
        "RETURN orphan_failed, count(z) AS zombies",
        {"hrs": s.zombie_age_hours},
    ))[0]

    out: list[LintFinding] = []
    if rows["orphan_failed"]:
        out.append(LintFinding(
            "orphan_failed_signals", "medium", rows["orphan_failed"],
            "Failed signals with no extracted mentions — safe to prune",
        ))
    if rows["zombies"]:
        sample_uuids = await drv.execute_query(
            "MATCH (z:Signal) "
            "WHERE z.status = 'extracting' "
            "  AND z.created_at IS NOT NULL "
            "  AND duration.inSeconds(z.created_at, datetime()).hours > $hrs "
            "RETURN z.uuid AS uuid LIMIT 5",
            {"hrs": s.zombie_age_hours},
        )
        out.append(LintFinding(
            "zombie_signals", "high", rows["zombies"],
            f"Signals stuck in 'extracting' > {s.zombie_age_hours}h "
            f"— probable crash mid-extraction",
            samples=[r["uuid"] for r in sample_uuids],
        ))
    return out


async def _structural_integrity(drv: Neo4jDriver) -> list[LintFinding]:
    """Synapses pointing to vanished neurons (corruption)."""
    rows = (await drv.execute_query(
        "MATCH ()-[r:SYNAPSE]->() "
        "WHERE r.expired_at IS NULL "
        "WITH r, startNode(r) AS s, endNode(r) AS t "
        "WHERE s IS NULL OR t IS NULL "
        "  OR (s.expired_at IS NOT NULL) "
        "  OR (t.expired_at IS NOT NULL) "
        "RETURN count(r) AS broken"
    ))[0]
    if not rows["broken"]:
        return []
    return [LintFinding(
        "broken_synapses", "high", rows["broken"],
        "Active synapses where source or target is missing/expired",
    )]


async def _stale_sweep(drv: Neo4jDriver, s: TendSettings) -> list[LintFinding]:
    """Neurons whose materialized effective_weight is stale (heartbeat lagging)."""
    rows = (await drv.execute_query(
        "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
        "WITH count(n) AS total, "
        "  sum(CASE WHEN n.last_swept_at IS NULL "
        "       OR duration.inSeconds(n.last_swept_at, datetime()).hours > $hrs "
        "       THEN 1 ELSE 0 END) AS stale "
        "RETURN total, stale",
        {"hrs": s.staleness_hours},
    ))[0]
    if not rows["stale"] or not rows["total"]:
        return []
    pct = rows["stale"] / rows["total"]
    sev: Severity = "high" if pct > 0.5 else "medium" if pct > 0.2 else "low"
    return [LintFinding(
        "stale_swept_neurons", sev, rows["stale"],
        f"{int(pct*100)}% of neurons not swept in > {s.staleness_hours}h "
        f"— run `mycelium tend` or schedule heartbeat",
    )]


async def _duplicate_names(drv: Neo4jDriver) -> list[LintFinding]:
    """Same name + same neuron_type → likely dedup miss."""
    rows = await drv.execute_query(
        "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
        "WITH toLower(n.name) AS nm, n.neuron_type AS t, "
        "     count(*) AS c, collect(n.name)[0..3] AS samples "
        "WHERE c > 1 "
        "RETURN nm, t, c, samples ORDER BY c DESC LIMIT 20"
    )
    if not rows:
        return []
    return [LintFinding(
        "duplicate_names", "medium", sum(r["c"] - 1 for r in rows),
        "Active neurons share a name (case-insensitive) within type",
        samples=[
            {"name": r["samples"][0], "type": r["t"], "count": r["c"]}
            for r in rows[:5]
        ],
    )]


# ── Stats + score ───────────────────────────────────────────────────


async def _stats(drv: Neo4jDriver) -> dict[str, Any]:
    rows = (await drv.execute_query(
        "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
        "WITH count(n) AS neurons "
        "OPTIONAL MATCH ()-[r:SYNAPSE]->() WHERE r.expired_at IS NULL "
        "WITH neurons, count(r) AS synapses "
        "OPTIONAL MATCH (s:Signal) "
        "RETURN neurons, synapses, count(s) AS signals"
    ))[0]
    return {k: rows.get(k, 0) for k in ("neurons", "synapses", "signals")}


def _score(findings: list[LintFinding]) -> float:
    """Weighted health score in [0, 1]. 1.0 = pristine, 0.0 = at-cap pain."""
    if not findings:
        return 1.0
    penalty = 0.0
    for f in findings:
        threshold = _SEVERITY_THRESHOLD[f.severity]
        weight    = _SEVERITY_WEIGHT[f.severity]
        penalty  += weight * min(1.0, f.count / threshold)
    # Max possible if every category in every severity is at-cap.
    max_penalty = float(sum(_SEVERITY_WEIGHT.values()) * 3)  # ~3 categories per bucket
    return round(max(0.0, 1.0 - penalty / max_penalty), 3)
