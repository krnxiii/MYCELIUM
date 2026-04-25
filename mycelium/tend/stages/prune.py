"""prune_dead — physically remove soft-deleted data and orphans.

Categories:
  - expired Neuron     (expired_at IS NOT NULL)              → DETACH DELETE
  - expired SYNAPSE    (expired_at IS NOT NULL)              → DELETE
  - past-TTL SYNAPSE   (expires_at < now AND expired_at IS NULL)
                        → soft-expire (set expired_at = expires_at)
  - orphan Signal      (status='failed' AND no MENTIONS)     → DETACH DELETE
  - zombie Signal      (status='extracting' AND age > N h)   → mark 'failed'

Idempotent. dry_run reports counts without writing.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from mycelium.config import TendSettings
from mycelium.driver.neo4j_driver import Neo4jDriver
from mycelium.tend.stages.decay import StageResult

log = structlog.get_logger()


async def prune_dead(
    drv:      Neo4jDriver,
    *,
    settings: TendSettings | None = None,
    dry_run:  bool                 = False,
) -> StageResult:
    """Sweep dead/zombie data. Reports counts in extra: {neurons, synapses, ttl, signals, zombies}."""
    s   = settings or TendSettings()
    res = StageResult(name="prune_dead", dry_run=dry_run)
    t0  = time.monotonic()

    counts: dict[str, int] = {
        "expired_neurons":    0,
        "expired_synapses":   0,
        "ttl_synapses":       0,
        "orphan_signals":     0,
        "zombie_signals":     0,
    }

    try:
        # Count first (always cheap, gives dry_run its output)
        c = (await drv.execute_query(
            "OPTIONAL MATCH (n:Neuron) WHERE n.expired_at IS NOT NULL "
            "WITH count(n) AS expired_neurons "
            "OPTIONAL MATCH ()-[r:SYNAPSE]->() WHERE r.expired_at IS NOT NULL "
            "WITH expired_neurons, count(r) AS expired_synapses "
            "OPTIONAL MATCH ()-[r2:SYNAPSE]->() "
            "  WHERE r2.expired_at IS NULL "
            "    AND r2.expires_at IS NOT NULL "
            "    AND r2.expires_at < datetime() "
            "WITH expired_neurons, expired_synapses, count(r2) AS ttl_synapses "
            "OPTIONAL MATCH (sig:Signal) "
            "  WHERE NOT EXISTS { MATCH (sig)-[:MENTIONS]->() } "
            "    AND sig.status = 'failed' "
            "WITH expired_neurons, expired_synapses, ttl_synapses, "
            "     count(sig) AS orphan_signals "
            "OPTIONAL MATCH (z:Signal) "
            "  WHERE z.status = 'extracting' "
            "    AND z.created_at IS NOT NULL "
            "    AND duration.between(z.created_at, datetime()).hours > $hrs "
            "RETURN expired_neurons, expired_synapses, ttl_synapses, "
            "       orphan_signals, count(z) AS zombie_signals",
            {"hrs": s.zombie_age_hours},
        ))[0]

        counts.update({k: c.get(k, 0) for k in counts})
        res.extra["counts"] = dict(counts)
        res.processed = sum(counts.values())

        if dry_run or res.processed == 0:
            res.elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.info("prune_dead_dry" if dry_run else "prune_dead_noop", **counts)
            return res

        # Soft-expire past-TTL synapses (do BEFORE deleting expired)
        if counts["ttl_synapses"]:
            await drv.execute_query(
                "MATCH ()-[r:SYNAPSE]->() "
                "WHERE r.expired_at IS NULL "
                "  AND r.expires_at IS NOT NULL "
                "  AND r.expires_at < datetime() "
                "SET r.expired_at = r.expires_at"
            )
            counts["expired_synapses"] += counts["ttl_synapses"]

        # Delete expired synapses
        if counts["expired_synapses"]:
            await drv.execute_query(
                "MATCH ()-[r:SYNAPSE]->() WHERE r.expired_at IS NOT NULL DELETE r"
            )
        # Delete expired neurons (DETACH cleans remaining rels)
        if counts["expired_neurons"]:
            await drv.execute_query(
                "MATCH (n:Neuron) WHERE n.expired_at IS NOT NULL DETACH DELETE n"
            )
        # Delete orphan failed signals
        if counts["orphan_signals"]:
            await drv.execute_query(
                "MATCH (sig:Signal) "
                "WHERE NOT EXISTS { MATCH (sig)-[:MENTIONS]->() } "
                "  AND sig.status = 'failed' "
                "DETACH DELETE sig"
            )
        # Mark zombies as failed (don't delete — they may have partial MENTIONS)
        if counts["zombie_signals"]:
            await drv.execute_query(
                "MATCH (z:Signal) "
                "WHERE z.status = 'extracting' "
                "  AND z.created_at IS NOT NULL "
                "  AND duration.between(z.created_at, datetime()).hours > $hrs "
                "SET z.status = 'failed'",
                {"hrs": s.zombie_age_hours},
            )

    except Exception as exc:
        res.errors.append(f"{type(exc).__name__}: {exc}")
        log.error("prune_dead_failed", error=str(exc))

    res.elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info("prune_dead_done", **counts, elapsed_ms=res.elapsed_ms)
    return res


def _summary(counts: dict[str, int]) -> dict[str, Any]:
    """Compact one-line summary for the report writer."""
    return {k: v for k, v in counts.items() if v > 0}
