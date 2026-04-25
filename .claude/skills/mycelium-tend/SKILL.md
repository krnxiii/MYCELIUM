---
name: mycelium-tend
description: Run MYCELIUM maintenance toolkit — recompute weights, prune dead data, reconcile vault, refresh degree. Use when user says "tend", "maintain graph", "clean up base", "optimize graph", "run maintenance", "graph upkeep". Diagnose first via lint, then act.
---

Run MYCELIUM maintenance toolkit: keep the graph in shape with deterministic Tier 0 operations (no LLM, idempotent, safe to repeat).

Like tending a garden — pull weeds, water roots, refresh the soil. Not the same as `/mycelium-distill` (which uses LLM to merge concepts) or `/mycelium-discover` (which finds new patterns).

## When to use

- After a large ingestion batch (lots of new data → effective_weight stale)
- Periodic upkeep (daily / weekly via cron — see docs/MAINTENANCE.md)
- When `/mycelium-reflect` shows "stale_swept" issues
- When user explicitly asks to clean / optimize / maintain / tend

## Strategy

1. **Diagnose** — call `lint()` first. Read the structured findings + score.
2. **Show the user** — present findings in plain language, grouped by severity.
3. **Propose action** — recommend `tend()` with appropriate scope:
   - Score > 0.9: probably skip, suggest dry-run
   - Score 0.7–0.9: run all default stages
   - Score < 0.7: run all + recommend reviewing high-severity findings manually
4. **Execute** — call `tend(stages=[...], dry_run=False)`. Report stage-by-stage results.
5. **Verify** — call `lint()` again. Confirm score improved or explain what didn't.

## Stage reference

Default stages run in order:

| Stage | What it does | Writes |
|-------|---|---|
| `decay_sweep` | Materialize `effective_weight` on every active Neuron | yes |
| `prune_dead` | Delete soft-expired data, mark zombie signals as failed | yes |
| `vault_compact` | Drop orphan `.index.json` entries (file already gone) | yes (only safe entries) |
| `centrality_refresh` | Materialize `Neuron.degree` (active synapse count) | yes |

Use `stages=["decay_sweep"]` to run a single stage.

## Useful flag combinations

- **Dry-run preview**: `tend(stages=[], dry_run=True)` — show counts without writes
- **Just clean dead data**: `tend(stages=["prune_dead"])`
- **Just refresh decay**: `tend(stages=["decay_sweep"])`
- **Full sweep, no log file**: `tend(write_report=False)`

## Edge cases

- **Empty graph** → tell user there's nothing to tend yet, suggest ingesting something
- **lint score = 1.0** → graph is clean, suggest skipping or running dry-run for sanity
- **All zombies, no good signals** → likely server crashed mid-batch; ask before pruning
- **vault_compact reports many `orphan_files`** → never auto-delete files; surface the list and ask the user

## Output Rules

- Use the language the user communicates in
- Lead with the score and 1-line verdict ("Graph is in good shape, score 0.95")
- Quote actual findings (counts, sample uuids), not generic advice
- After `tend()`: report what changed (deleted X neurons, marked Y zombies as failed)
- Mention the report path if `write_report=True` saved a markdown log
