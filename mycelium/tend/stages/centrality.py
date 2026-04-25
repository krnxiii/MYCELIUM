"""centrality_refresh — materialize per-Neuron degree.

Pure Cypher, no GDS. Sets `Neuron.degree` = number of active SYNAPSE edges
(in or out). Inactive (expired) edges excluded.

PageRank/Louvain are NOT included: GDS plugin is not part of MYCELIUM's
default Neo4j image (only APOC). When/if GDS becomes a default dependency
this stage will gain optional pagerank materialization.
"""

from __future__ import annotations

import time

import structlog

from mycelium.driver.neo4j_driver import Neo4jDriver
from mycelium.tend.stages.decay import StageResult

log = structlog.get_logger()


async def centrality_refresh(
    drv:     Neo4jDriver,
    *,
    dry_run: bool = False,
) -> StageResult:
    """Recompute and materialize Neuron.degree (active synapse count)."""
    res = StageResult(name="centrality_refresh", dry_run=dry_run)
    t0  = time.monotonic()

    try:
        # Count first (cheap, drives both report + dry_run output)
        c = (await drv.execute_query(
            "MATCH (n:Neuron) WHERE n.expired_at IS NULL RETURN count(n) AS c"
        ))[0]
        res.processed = c["c"]

        if dry_run or res.processed == 0:
            res.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return res

        await drv.execute_query(
            "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
            "OPTIONAL MATCH (n)-[r:SYNAPSE]-() WHERE r.expired_at IS NULL "
            "WITH n, count(r) AS deg "
            "SET n.degree = deg"
        )

        stats = (await drv.execute_query(
            "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
            "RETURN max(n.degree) AS max_deg, "
            "  round(avg(n.degree) * 100) / 100 AS avg_deg, "
            "  sum(CASE WHEN n.degree = 0 THEN 1 ELSE 0 END) AS isolated"
        ))[0]
        res.extra.update({
            "max_degree":      stats.get("max_deg")    or 0,
            "avg_degree":      stats.get("avg_deg")    or 0.0,
            "isolated_count":  stats.get("isolated")   or 0,
        })

    except Exception as exc:
        res.errors.append(f"{type(exc).__name__}: {exc}")
        log.error("centrality_refresh_failed", error=str(exc))

    res.elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info("centrality_refresh_done",
             processed=res.processed, elapsed_ms=res.elapsed_ms, **res.extra)
    return res
