"""tend orchestrator — run maintenance stages in order, collect results.

Stages run sequentially; failure of one is isolated (logged into its own
StageResult.errors), the rest still run. Aggregate result is a TendReport.

Stage order is intentional:
  1. decay_sweep        — materialize current weights first (informs lint, search)
  2. prune_dead         — drop soft-deleted; smaller graph for next stages
  3. vault_compact      — independent; reconciles disk
  4. centrality_refresh — runs after prune so degrees reflect live edges
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from mycelium.config import Settings, TendSettings, VaultSettings
from mycelium.driver.neo4j_driver import Neo4jDriver
from mycelium.tend.stages.centrality import centrality_refresh
from mycelium.tend.stages.decay import StageResult, decay_sweep
from mycelium.tend.stages.prune import prune_dead
from mycelium.tend.stages.vault import vault_compact

log = structlog.get_logger()

DEFAULT_STAGES = ("decay_sweep", "prune_dead", "vault_compact", "centrality_refresh")


@dataclass
class TendReport:
    stages:     list[StageResult] = field(default_factory=list)
    elapsed_ms: int               = 0
    dry_run:    bool              = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stages":     [s.to_dict() for s in self.stages],
            "elapsed_ms": self.elapsed_ms,
            "dry_run":    self.dry_run,
            "summary":    self.summary(),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "stages_run":   len(self.stages),
            "stages_ok":    sum(1 for s in self.stages if not s.errors),
            "stages_fail":  sum(1 for s in self.stages if s.errors),
            "total_processed": sum(s.processed for s in self.stages),
        }


async def tend(
    drv:       Neo4jDriver,
    *,
    settings:  Settings | None = None,
    stages:    tuple[str, ...] | None = None,
    dry_run:   bool             = False,
) -> TendReport:
    """Run requested stages in order. ``stages=None`` runs DEFAULT_STAGES."""
    s          = settings or Settings()
    chosen     = stages or DEFAULT_STAGES
    report     = TendReport(dry_run=dry_run)
    t0         = time.monotonic()

    for name in chosen:
        runner = _STAGE_FNS.get(name)
        if runner is None:
            log.warning("tend_unknown_stage", stage=name)
            continue
        try:
            result = await runner(drv, s, dry_run)
        except Exception as exc:  # never let one stage kill the run
            result = StageResult(name=name, dry_run=dry_run,
                                 errors=[f"{type(exc).__name__}: {exc}"])
            log.error("tend_stage_crashed", stage=name, error=str(exc))
        report.stages.append(result)

    report.elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info("tend_done", **report.summary(), elapsed_ms=report.elapsed_ms)
    return report


# ── Stage adapters: uniform (drv, settings, dry_run) signature ──────


async def _decay(drv: Neo4jDriver, s: Settings, dry: bool) -> StageResult:
    return await decay_sweep(drv, settings=s.tend, dry_run=dry)


async def _prune(drv: Neo4jDriver, s: Settings, dry: bool) -> StageResult:
    return await prune_dead(drv, settings=s.tend, dry_run=dry)


async def _vault(drv: Neo4jDriver, s: Settings, dry: bool) -> StageResult:
    return await vault_compact(drv, vault=s.vault, settings=s.tend, dry_run=dry)


async def _centrality(drv: Neo4jDriver, s: Settings, dry: bool) -> StageResult:
    return await centrality_refresh(drv, dry_run=dry)


_STAGE_FNS = {
    "decay_sweep":        _decay,
    "prune_dead":         _prune,
    "vault_compact":      _vault,
    "centrality_refresh": _centrality,
}
