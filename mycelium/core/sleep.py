"""Sleep-time compute: unified graph health analysis.

Finds weak neurons, near-duplicate pairs, isolated neurons,
goal/interest gaps, contradictions, and potential bridges.
Returns a structured report for the agent to act on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from mycelium.driver.neo4j_driver import Neo4jDriver
from mycelium.utils.decay import cypher_effective_weight

log = structlog.get_logger()


@dataclass
class SleepReport:
    """Consolidation candidates produced by sleep-time analysis."""

    weak:          list[dict[str, Any]] = field(default_factory=list)
    duplicates:    list[dict[str, Any]] = field(default_factory=list)
    isolated:      list[dict[str, Any]] = field(default_factory=list)
    gaps:          list[dict[str, Any]] = field(default_factory=list)
    conflicts:     list[dict[str, Any]] = field(default_factory=list)
    bridges:       list[dict[str, Any]] = field(default_factory=list)
    stats:         dict[str, Any]       = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weak_neurons":      self.weak,
            "duplicate_pairs":   self.duplicates,
            "isolated_neurons":  self.isolated,
            "gaps":              self.gaps,
            "conflicts":         self.conflicts,
            "bridges":           self.bridges,
            "stats": {
                **self.stats,
                "weak_count":      len(self.weak),
                "duplicate_count": len(self.duplicates),
                "isolated_count":  len(self.isolated),
                "gap_count":       len(self.gaps),
                "conflict_count":  len(self.conflicts),
                "bridge_count":    len(self.bridges),
                "total_candidates": (
                    len(self.weak) + len(self.duplicates) + len(self.isolated)
                    + len(self.gaps) + len(self.conflicts) + len(self.bridges)
                ),
            },
        }


async def build_sleep_report(
    drv: Neo4jDriver,
    *,
    weak_threshold:    float = 0.15,
    dup_cosine_low:    float = 0.85,
    dup_cosine_high:   float = 0.95,
    isolation_max_syn: int   = 1,
    limit:             int   = 30,
) -> SleepReport:
    """Analyze graph and return consolidation candidates."""
    report = SleepReport()

    # Run independent queries in parallel
    weak_t, iso_t, gaps_t, conflicts_t, bridges_t, stats_t = await asyncio.gather(
        _find_weak_neurons(drv, weak_threshold, limit),
        _find_isolated_neurons(drv, isolation_max_syn, limit),
        _find_gaps(drv),
        _find_conflicts(drv),
        _find_bridges(drv),
        _graph_stats(drv),
    )
    report.weak      = weak_t
    report.isolated  = iso_t
    report.gaps      = gaps_t
    report.conflicts = conflicts_t
    report.bridges   = bridges_t
    report.stats     = stats_t

    # Near-duplicate detection via Neo4j vector index
    report.duplicates = await _find_duplicate_pairs(
        drv, dup_cosine_low, dup_cosine_high, limit,
    )

    log.info("sleep_report_built",
             weak=len(report.weak),
             dups=len(report.duplicates),
             isolated=len(report.isolated))
    return report


async def _find_weak_neurons(
    drv: Neo4jDriver, threshold: float, limit: int,
) -> list[dict[str, Any]]:
    """Neurons with effective weight below threshold."""
    ew = cypher_effective_weight("e")
    return await drv.execute_query(
        "MATCH (e:Neuron) WHERE e.expired_at IS NULL "
        "  AND (e.expires_at IS NULL OR e.expires_at > datetime()) "
        f"WITH e, {ew} AS ew "
        "WHERE ew < $threshold "
        "OPTIONAL MATCH (e)-[f:SYNAPSE]-() WHERE f.expired_at IS NULL "
        "WITH e, ew, count(f) AS syn_count "
        "RETURN e.uuid AS uuid, e.name AS name, "
        "  e.neuron_type AS type, e.summary AS summary, "
        "  round(ew * 10000) / 10000 AS weight, "
        "  e.confirmations AS confirmations, syn_count "
        "ORDER BY ew ASC LIMIT $limit",
        {"threshold": threshold, "limit": limit},
    )


async def _find_isolated_neurons(
    drv: Neo4jDriver, max_synapses: int, limit: int,
) -> list[dict[str, Any]]:
    """Neurons with few or no active synapses."""
    ew = cypher_effective_weight("e")
    return await drv.execute_query(
        "MATCH (e:Neuron) WHERE e.expired_at IS NULL "
        "  AND (e.expires_at IS NULL OR e.expires_at > datetime()) "
        "OPTIONAL MATCH (e)-[f:SYNAPSE]-() WHERE f.expired_at IS NULL "
        "WITH e, count(f) AS syn_count "
        "WHERE syn_count <= $max_syn "
        f"WITH e, syn_count, {ew} AS ew "
        "RETURN e.uuid AS uuid, e.name AS name, "
        "  e.neuron_type AS type, "
        "  round(ew * 10000) / 10000 AS weight, "
        "  syn_count "
        "ORDER BY syn_count ASC, ew ASC LIMIT $limit",
        {"max_syn": max_synapses, "limit": limit},
    )


async def _find_duplicate_pairs(
    drv:      Neo4jDriver,
    cos_low:  float,
    cos_high: float,
    limit:    int,
) -> list[dict[str, Any]]:
    """Neuron pairs with high name embedding cosine similarity.

    Uses Neo4j vector index to find candidates efficiently:
    for each neuron, query nearest neighbors and filter by cosine range.
    """
    # Get all active neuron names + embeddings via vector similarity
    # Strategy: for each neuron, find top-5 similar and keep pairs in range
    rows = await drv.execute_query(
        "MATCH (a:Neuron) WHERE a.expired_at IS NULL "
        "  AND a.name_embedding IS NOT NULL "
        "  AND (a.expires_at IS NULL OR a.expires_at > datetime()) "
        "WITH a ORDER BY a.name LIMIT 200 "
        "CALL db.index.vector.queryNodes("
        "  'neuron_name_emb', 6, a.name_embedding"
        ") YIELD node AS b, score "
        "WHERE b.uuid > a.uuid "
        "  AND b.expired_at IS NULL "
        "  AND score >= $cos_low AND score < $cos_high "
        "RETURN a.uuid AS a_uuid, a.name AS a_name, "
        "  a.neuron_type AS a_type, "
        "  b.uuid AS b_uuid, b.name AS b_name, "
        "  b.neuron_type AS b_type, "
        "  round(score * 10000) / 10000 AS cosine "
        "ORDER BY score DESC LIMIT $limit",
        {"cos_low": cos_low, "cos_high": cos_high, "limit": limit},
    )
    return rows


async def _graph_stats(drv: Neo4jDriver) -> dict[str, Any]:
    """Basic graph counts for the report."""
    rows = await drv.execute_query(
        "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
        "WITH count(n) AS neurons "
        "OPTIONAL MATCH ()-[f:SYNAPSE]->() WHERE f.expired_at IS NULL "
        "WITH neurons, count(f) AS synapses "
        "OPTIONAL MATCH ()-[fe:SYNAPSE]->() WHERE fe.expired_at IS NOT NULL "
        "RETURN neurons, synapses, count(fe) AS expired"
    )
    return rows[0] if rows else {"neurons": 0, "synapses": 0, "expired": 0}


# ── Graph weak spots (merged from questions.py) ──────────────────


async def _find_gaps(drv: Neo4jDriver) -> list[dict[str, Any]]:
    """Goals/interests without associated actions/practices."""
    rows = await drv.execute_query(
        "MATCH (g:Neuron) "
        "WHERE g.neuron_type IN ['goal', 'interest'] "
        "  AND g.expired_at IS NULL "
        "  AND (g.expires_at IS NULL OR g.expires_at > datetime()) "
        "WITH g "
        "OPTIONAL MATCH (g)-[:SYNAPSE]-(a:Neuron) "
        "  WHERE a.neuron_type IN ['action', 'practice', 'skill', 'habit'] "
        "  AND a.expired_at IS NULL "
        "WITH g, count(a) AS action_count "
        "WHERE action_count = 0 "
        "RETURN g.uuid AS uuid, g.name AS name, "
        "  g.neuron_type AS type "
        "LIMIT 5"
    )
    return [{"category": "GAP", **r} for r in rows]


async def _find_conflicts(drv: Neo4jDriver) -> list[dict[str, Any]]:
    """Synapses marked as contradictions."""
    rows = await drv.execute_query(
        "MATCH (s:Neuron)-[new:SYNAPSE]->(t:Neuron) "
        "WHERE new.contradiction_of IS NOT NULL "
        "  AND new.expired_at IS NULL "
        "MATCH ()-[old:SYNAPSE {uuid: new.contradiction_of}]->() "
        "WHERE old.expired_at IS NULL "
        "RETURN s.name AS source, t.name AS target, "
        "  new.fact AS new_fact, old.fact AS old_fact, "
        "  new.uuid AS new_uuid, old.uuid AS old_uuid "
        "LIMIT 5"
    )
    return [{"category": "CONFLICT", **r} for r in rows]


async def _find_bridges(drv: Neo4jDriver) -> list[dict[str, Any]]:
    """Neurons with high name similarity but no direct synapse."""
    rows = await drv.execute_query(
        "MATCH (a:Neuron) "
        "WHERE a.expired_at IS NULL AND a.neuron_type <> 'community' "
        "  AND a.name_embedding IS NOT NULL "
        "WITH a ORDER BY rand() LIMIT 50 "
        "CALL db.index.vector.queryNodes("
        "  'neuron_name_emb', 4, a.name_embedding"
        ") YIELD node AS b, score "
        "WHERE b.uuid <> a.uuid AND a.uuid < b.uuid "
        "  AND b.expired_at IS NULL "
        "  AND b.neuron_type <> 'community' "
        "  AND score >= 0.5 AND score < 0.85 "
        "  AND NOT EXISTS { MATCH (a)-[:SYNAPSE]-(b) } "
        "RETURN a.name AS a_name, a.neuron_type AS a_type, "
        "  b.name AS b_name, b.neuron_type AS b_type, "
        "  round(score * 10000) / 10000 AS similarity "
        "ORDER BY score DESC LIMIT 5"
    )
    return [{"category": "BRIDGE", **r} for r in rows]
