---
name: mycelium-reflect
description: Analyze MYCELIUM knowledge graph health and provide actionable insights. Use when user says "how is my graph", "reflect", "graph health", "analyze my knowledge", or wants to understand what's strong, fading, or missing.
---

Analyze MYCELIUM knowledge graph health and provide actionable insights.

## Strategy

1. `health()` — graph stats, stale neurons, Neo4j status.
2. `list_neurons(sort_by="weight", limit=15)` — strongest knowledge (high weight).
3. `list_neurons(sort_by="freshness", limit=10)` — most recent activity.
4. `get_owner()` — personalize the report.
5. **Maintenance pulse** — peek at `<vault>/_AGENT/log/` for the most recent
   `*-tend.md` and `*-lint.md`. If absent or older than ~7 days, suggest
   running `/mycelium-tend`. If recent, summarize what was cleaned.

Note: weakest/fading neurons come from `health()` response (stale list). No separate call needed.

## Analysis

From the collected data, produce:

### Graph Overview
- Total neurons, signals, active/expired synapses
- Neo4j health status

### Strongest Knowledge (top 5 by weight)
- What the user knows deeply and recently reinforced
- Patterns: clusters of related neurons

### Fading Knowledge (stale neurons from health)
- Knowledge that hasn't been mentioned recently
- For each: is it still relevant? Should the user reinforce or let it decay?

### Gaps & Recommendations
- Neuron types distribution — what's missing? (e.g. many interests but no goals)
- Orphan-like neurons (few synapses) — worth enriching?
- Suggest 2-3 specific actions: "tell me more about X", "is Y still relevant?"

## Edge Cases

- Empty graph (0 neurons) → "Your graph is empty. Start by ingesting a file or telling me something about yourself."
- Very small graph (<5 neurons) → skip distribution analysis, focus on "what to add next".
- All neurons healthy → celebrate, suggest running `/mycelium-discover` for insights.
- Health check fails → report Neo4j connection issue, suggest `make up`.

## Output Rules

- Use the language the user communicates in.
- Be specific — name actual neurons, not generic advice.
- Weight values: explain what they mean (1.0 = fresh + confident, <0.1 = almost forgotten).
- Keep the tone of a thoughtful advisor, not a dashboard.
