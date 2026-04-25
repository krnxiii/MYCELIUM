"""Maintenance toolkit — keep the graph in shape.

Diagnose ↔ fix pair:
  - lint        — read-only structural health check (R9.4, lands in PR3)
  - tend        — orchestrator: runs maintenance stages

Stages (Tier 0, no LLM, idempotent):
  - decay_sweep        — materialize effective_weight on Neuron
  - prune_dead         — drop expired nodes / orphan signals / zombies / past-TTL
  - vault_compact      — reconcile vault disk ↔ index ↔ graph
  - centrality_refresh — degree (no GDS dependency)

Heartbeat (cron / launchd / systemd) drives freshness, not in-process daemons.
"""

from __future__ import annotations

from mycelium.tend.orchestrator import DEFAULT_STAGES, TendReport, tend
from mycelium.tend.stages.centrality import centrality_refresh
from mycelium.tend.stages.decay import StageResult, decay_sweep
from mycelium.tend.stages.prune import prune_dead
from mycelium.tend.stages.vault import vault_compact

__all__ = [
    "DEFAULT_STAGES",
    "StageResult",
    "TendReport",
    "centrality_refresh",
    "decay_sweep",
    "prune_dead",
    "tend",
    "vault_compact",
]
