"""decay_sweep — materialize effective_weight on every active Neuron.

Idempotent. Batched via uuid pagination to keep transactions small.
Reads on hot path go through `cypher_effective_weight()` which falls back
to on-read calc when materialized value is missing or stale — so this stage
is safe to skip / delay; freshness degrades gracefully.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

import structlog

from mycelium.config import TendSettings
from mycelium.driver.neo4j_driver import Neo4jDriver

log = structlog.get_logger()


@dataclass
class StageResult:
    """Outcome of a single maintenance stage."""
    name:        str
    processed:   int            = 0
    weak_count:  int            = 0
    mean_weight: float          = 0.0
    elapsed_ms:  int            = 0
    dry_run:     bool           = False
    errors:      list[str]      = None  # type: ignore[assignment]
    extra:       dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.extra is None:
            self.extra = {}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def decay_sweep(
    drv:      Neo4jDriver,
    *,
    settings: TendSettings | None = None,
    dry_run:  bool                 = False,
) -> StageResult:
    """Recompute and materialize effective_weight for active Neuron nodes.

    Active = expired_at IS NULL AND (expires_at IS NULL OR expires_at > now).
    Writes effective_weight and last_swept_at on each node (skipped if dry_run).
    """
    s     = settings or TendSettings()
    res   = StageResult(name="decay_sweep", dry_run=dry_run)
    t0    = time.monotonic()

    try:
        last_uuid       = ""
        sum_weight      = 0.0
        weak_threshold  = s.weak_threshold

        while True:
            page = await drv.execute_query(
                "MATCH (n:Neuron) "
                "WHERE n.expired_at IS NULL "
                "  AND (n.expires_at IS NULL OR n.expires_at > datetime()) "
                "  AND n.uuid > $last_uuid "
                "WITH n ORDER BY n.uuid ASC LIMIT $batch_size "
                "WITH n, "
                "  coalesce(n.importance, n.confidence) * "
                "  exp(-n.decay_rate * "
                "    duration.between(n.freshness, datetime()).days) AS ew "
                "RETURN n.uuid AS uuid, ew",
                {"last_uuid": last_uuid, "batch_size": s.sweep_batch_size},
            )
            if not page:
                break

            res.processed   += len(page)
            sum_weight      += sum(r["ew"] for r in page)
            res.weak_count  += sum(1 for r in page if r["ew"] < weak_threshold)
            last_uuid        = page[-1]["uuid"]

            if not dry_run:
                await drv.execute_query(
                    "UNWIND $rows AS row "
                    "MATCH (n:Neuron {uuid: row.uuid}) "
                    "SET n.effective_weight = row.ew, "
                    "    n.last_swept_at    = datetime()",
                    {"rows": page},
                )

        if res.processed > 0:
            res.mean_weight = round(sum_weight / res.processed, 6)

    except Exception as exc:
        res.errors.append(f"{type(exc).__name__}: {exc}")
        log.error("decay_sweep_failed", error=str(exc))

    res.elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "decay_sweep_done",
        processed   = res.processed,
        weak        = res.weak_count,
        mean_weight = res.mean_weight,
        elapsed_ms  = res.elapsed_ms,
        dry_run     = dry_run,
    )
    return res
