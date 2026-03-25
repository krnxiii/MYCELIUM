---
name: mycelium-recall
description: Search MYCELIUM knowledge graph and synthesize an answer with provenance. Use when user asks "what do I know about X", "recall X", "search my graph for X", or needs information from their personal knowledge base.
argument-hint: <query>
---

Search MYCELIUM knowledge graph and synthesize an answer with provenance.

Arguments: <query>

## Strategy

1. `search(query="<query>", top_k=10)` — hybrid search (vector + BM25 + graph).
2. For each top neuron (up to 5): `get_neuron(uuid)` — get details + synapses.
3. If temporal context matters: `get_timeline(neuron_uuid)` — evolution over time.
4. `get_owner()` — to personalize the answer ("you" vs third-person).

## Synthesis Rules

- Answer the query using ONLY graph data. Do not hallucinate facts.
- Cite provenance: mention neuron names and signal sources where possible.
- If graph has no relevant data — say so honestly, suggest what to ingest.
- Preserve the richness: include dates, numbers, attributes from neurons.
- Use the language the user asked in.

## Output Format

Answer naturally, then append a compact source block:

```
Sources:
- neuron "X" (type, confidence, weight)
- synapse "X → Y": fact text
```

## Edge Cases

- Empty search results → "Nothing found in the graph for this query."
- Low-weight results (weight < 0.1) → note that the knowledge is fading.
- Contradictions in synapses → present both, note the conflict.
