---
name: mycelium-distill
description: Distill MYCELIUM knowledge graph — merge duplicates, strengthen weak neurons, clean noise. Use when user says "clean up graph", "distill", "merge duplicates", "remove noise". Do NOT use for discovering patterns — use mycelium-discover instead.
---

Distill MYCELIUM knowledge graph: merge duplicates, strengthen weak neurons, clean noise.

Like distillation — removing impurities, concentrating what matters,
letting go of what's fading, leaving only the essence.

## Critical

- **Never auto-delete without user confirmation.** Always list candidates and ask.
- Budget: process max 10 actions per run to keep LLM costs reasonable.
- Use the language the user communicates in.

## Strategy

1. Call `sleep_report()` to get consolidation candidates.
2. Analyze the report and act on each category (Phases 1-3).
3. Verify: call `sleep_report()` again to confirm improvement.
4. Report results.

## Phase 1: Near-Duplicates

For each pair in `duplicate_pairs`:
- Call `get_neuron(uuid)` on both to see full context (synapses, attributes).
- Decide: are they truly the same concept?
  - **Yes** -> `merge_neurons(primary_uuid, secondary_uuid)`. Pick the richer one as primary.
  - **No** -> skip, they are distinct despite name similarity.
- Log each decision with reasoning.

## Phase 2: Weak Neurons

For each neuron in `weak_neurons`:
- If `syn_count >= 2` and the neuron has meaningful synapses:
  -> `rethink_neuron(uuid)` to consolidate knowledge into a better summary.
- If `syn_count == 0` and `confirmations == 0`:
  -> candidate for deletion. Ask user before deleting: "Neuron X has no connections and is fading. Delete?"
- If the neuron seems important but weak:
  -> note it as "needs reinforcement" in the report. Don't delete.

## Phase 3: Isolated Neurons

For each neuron in `isolated_neurons` (0-1 synapses):
- If the neuron has a meaningful name/type but no synapses:
  -> `get_neuron(uuid)` to check context.
  -> If it's an orphan from a failed extraction: candidate for deletion.
  -> If it's a valid concept: note "needs enrichment" — user should mention it again.
- Skip neurons that are fresh (high weight) — they may just be new.

## Phase 4: Verify

- Call `sleep_report()` again after Phases 1-3.
- Compare: did duplicate_pairs / weak_neurons / isolated_neurons shrink?
- If new issues surfaced from topology changes — note them for next run.

## Safety

- **Merges are safe** — synapses are rewired, no data lost.
- **Rethink is safe** — only updates summary/type/attributes.

## Error Handling

- `sleep_report()` returns empty → "Graph is clean, no candidates for distillation."
- `merge_neurons` fails → log error, skip pair, continue with next.
- `rethink_neuron` fails → log error, skip neuron, continue.
- Neo4j connection lost → stop, report what was completed so far.

## Output

After all phases, produce a summary:

### Distillation Report
- Graph: N neurons, M synapses
- **Merged**: list of merged pairs with reasoning
- **Rethought**: list of neurons that got rethink
- **Deletion candidates**: list for user to approve
- **Needs enrichment**: isolated neurons worth keeping
- **Verification**: before/after comparison from sleep_report
- **Skipped**: count of candidates that needed no action
