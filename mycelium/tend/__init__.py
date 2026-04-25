"""Maintenance toolkit — keep the graph in shape.

Diagnose ↔ fix pair:
  - lint        — read-only structural health check (R9.4)
  - tend        — orchestrator: runs maintenance stages

Stages (Tier 0, no LLM, idempotent):
  - decay_sweep        — materialize effective_weight on Neuron
  - prune_dead         — drop expired nodes / orphan signals / zombies
  - vault_compact      — reconcile vault ↔ graph ↔ index
  - centrality_refresh — degree (always) + PageRank (if GDS)
  - community_refresh  — Louvain via GDS (skip if absent)

Design principle: detected staleness, not pushed invalidation.
Heartbeat (cron / launchd / systemd) drives freshness, not in-process daemons.
"""

from __future__ import annotations

from mycelium.tend.stages.decay import StageResult, decay_sweep

__all__ = ["StageResult", "decay_sweep"]
