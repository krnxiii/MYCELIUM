---
name: mycelium-discover
description: Discover hidden patterns, connections, and blind spots in MYCELIUM knowledge graph. Use when user says "find patterns", "discover connections", "what am I missing", "explore my graph". Do NOT use for cleanup — use mycelium-distill instead.
---

Discover hidden patterns, connections, and blind spots in your MYCELIUM knowledge graph.

Unlike distillation (which cleans), discovery adds — new clusters, inferred connections,
and questions that reveal what you haven't noticed yet.

## Strategy

1. Call `detect_communities()` to find thematic clusters.
2. Cross-neuron inference: find hidden connections between clusters.
3. Call `sleep_report()` to surface gaps, contradictions, and bridges.
4. Report findings.

## Phase 1: Community Detection

- Call `detect_communities()` with default parameters.
- Groups neurons into thematic clusters and creates community meta-neurons.
- If the graph has < 6 neurons, this step is skipped automatically.
- Report discovered communities: name, member count, top members.
- Communities are searchable — user can now ask "What do I know about X?" and get a cluster answer.

## Phase 2: Cross-Neuron Inference

Manual cross-cluster analysis (no dedicated tool — you ARE the inference engine):

1. From Phase 1 communities, pick 2-3 pairs of neurons from DIFFERENT clusters.
2. For each pair, call `get_neuron(uuid)` on both to see full context.
3. Analyze: is there a non-obvious connection? (causal, temporal, structural)
4. If yes → `add_synapse(source_uuid, target_uuid, relation, fact, confidence=0.7)`
   with `fact` explaining the discovered connection.
5. Budget: max 5 pairs per run.

If no communities exist (graph too small), pick neurons with highest weight
from `list_neurons(sort_by="weight", limit=10)` and look for cross-type connections
(e.g. a practice → trait, an interest → goal).

## Phase 3: Gaps & Contradictions

- Call `sleep_report()` to surface structural issues.
- From the report, extract and present to user (max 3 most impactful):
  - **Contradictions**: conflicting synapses about the same topic
  - **Bridges**: neurons that could connect isolated clusters
  - **Gaps**: neuron types with weak coverage (e.g. many interests, no goals)
  - **Decay**: important knowledge that is fading

## Rules

- Discovery is non-destructive — it only adds knowledge, never deletes.
- Inferred synapses are marked with `source: "inference"` and `origin: "derived"`.
- Community meta-neurons are created with `neuron_type: "community"`.
- Budget: max 10 inference pairs per run to keep LLM costs reasonable.
- Use the language the user communicates in.

## Error Handling

- Graph too small (<6 neurons) → skip communities, proceed to inference. If <3 neurons, skip inference too and report: "Graph needs more knowledge before discovery is useful."
- `detect_communities()` fails → skip and continue with Phase 2 using `list_neurons()` instead.
- No cross-cluster pairs found → "All neuron pairs are already well-connected."
- `sleep_report()` returns no gaps → "No gaps or contradictions detected — graph is consistent."

## Output

After all phases, produce a summary:

### Discovery Report
- Graph: N neurons, M synapses
- **Communities**: count detected, names + member counts
- **Inferred connections**: list of discovered links with reasoning
- **Questions**: emergent questions for reflection
- **Suggestion**: what to explore next based on findings
