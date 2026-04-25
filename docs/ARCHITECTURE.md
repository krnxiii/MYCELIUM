# MYCELIUM — Architecture

Current state of the system as of v0.5.0. Focused on what runs in production today, not on the path that got us here. For historical context (v1 design, v2 transition plan) see local-only `docs/v2_done/` and `docs/v1/` archives.

## Three-layer model

Knowledge flows through three immutable concepts:

```
Signal ──extract──► Neuron ──connect──► Synapse
(raw input)        (entity)              (semantic edge)
```

| Layer | Purpose | Pydantic model |
|-------|---------|---|
| **Signal** | Raw input preserved verbatim — source of truth, addressable, hashable | `mycelium.core.models.Signal` |
| **Neuron** | Extracted entity (concept, person, skill, event…) with decay state | `mycelium.core.models.Neuron` |
| **Synapse** | Bi-temporal semantic edge: contains a `fact`, an embedding, and provenance | `mycelium.core.models.Synapse` |

Neuron `name`, `summary`, and Synapse `fact` are all embedded — the same vector space hosts entities and facts, which lets hybrid search blend them without tier-juggling.

## Bi-temporal facts

Every Synapse carries two time pairs:

```
valid_at   ─ when the fact was true in the world
invalid_at ─ when it stopped being true (NULL = still true)
created_at ─ when it entered the graph
expired_at ─ when it was soft-deleted (NULL = active)
```

This separates **what reality says** (valid/invalid) from **what we know** (created/expired). Old facts can be invalidated without being deleted — useful for contradiction handling and audit.

Neurons are simpler: only `created_at`, `expires_at`, and a `freshness` timestamp that drives decay (see below). No bi-temporal model on entities — only on facts.

## Decay + consolidation

Memory ages. Knowledge that's revisited grows stronger; knowledge that's ignored fades. The math:

```
effective_weight = importance × exp(-decay_rate × days_since_freshness)
```

| Field | Behavior |
|---|---|
| `importance` | Stable significance (birthday=1.0 forever). Set on creation, rarely touched. |
| `decay_rate` | Lowered on every confirmation: `base / (1 + confirmations × factor)`, floored at `min_rate`. |
| `confirmations` | Increments on each re-mention via `consolidate()`. |
| `freshness` | Bumped on each confirmation; days-since-freshness drives the exponential. |

Defaults (see `DecaySettings`): `base_rate=0.008` (half-life ~90 days), `consolidation_factor=0.3`, `min_rate=0.001`, `evidence_boost=0.1`.

The single source of truth for decay in Cypher is `mycelium.utils.decay.cypher_effective_weight()` — it returns a snippet that reads the materialized `effective_weight` (set by `tend decay_sweep`) with on-read fallback when the value is missing or stale.

## Hybrid search

Pipeline (`mycelium/search/search.py`):

```
query
  ├── [1] vector  — cosine over entity + fact embeddings
  ├── [2] BM25    — keyword fulltext over name/summary/fact
  └── [3] BFS     — graph traversal from anchor neurons
                ↓
          RRF fusion (Reciprocal Rank Fusion)
                ↓
          reranker chain (config-driven)
                ↓
              top-k results
```

Reranker chain is pluggable (`SearchSettings.reranker_chain`, defaults to `["decay", "blend", "mmr"]`):

- **decay** — multiplies score by `effective_weight` (recency × importance)
- **blend** — position-aware blending across signal types
- **mmr** — diversity (max marginal relevance), opt-in
- **cross_encoder** — bge-reranker-v2-m3 over top-N, opt-in (requires API)
- **node_distance** — boost results closer to owner neuron, opt-in

Query prefixes accepted: `lex:` (BM25 only), `vec:` (vector only), `hyde:` (hypothetical doc), `vec+lex:` (no BFS).

## Vault — local-first storage

Files live under `MYCELIUM_VAULT__PATH` (default `~/.mycelium/vault`). Layout:

```
vault/
├── CORTEX/                    raw source files (SHA-256 addressed)
│   ├── documents/             text + PDF
│   ├── images/
│   ├── audio/                 with optional transcripts
│   ├── {domain}/              optionally scoped by domain blueprint
│   └── {domain}/{subdomain}/  nested via R8 (subdomain routing)
├── NEURONS/                   one .md per Neuron (obsidian sync)
├── _AGENT/                    agent workspace (skipped by sync)
│   ├── context.md             auto-generated graph snapshot
│   ├── memory.md              curated agent notes
│   └── log/YYYY-MM-DD-{tend|lint}.md   maintenance reports
└── .index.json                {relative_path: {content_hash, signal_uuid}}
```

`_SKIP_PREFIXES = (".", "_")` — directories starting with `.` or `_` are skipped by obsidian sync, never re-ingested into the graph. This protects `_AGENT/` (and the planned `_WIKI/` for R9) from causing recursion.

## Maintenance toolkit (v0.5.0)

Diagnose ↔ fix pair:

| Tool | Reads / Writes | Purpose |
|------|---|---|
| `lint` | read-only | 8-category structural health check + score 0..1 |
| `tend` | writes | Run one or more maintenance stages |

Tier 0 stages (no LLM, idempotent):

| Stage | Effect |
|-------|--------|
| `decay_sweep` | Materialize `Neuron.effective_weight` + `Neuron.last_swept_at` |
| `prune_dead` | Delete soft-expired data, soft-expire past-TTL synapses, mark zombie `extracting` Signals as failed |
| `vault_compact` | Drop orphan entries from `.index.json`; report orphan files / dangling-signal references |
| `centrality_refresh` | Materialize `Neuron.degree` (active synapse count) |

`/mycelium-distill` and `/mycelium-discover` remain the LLM-driven counterparts. They *interpret* (merge near-duplicates, infer cross-cluster connections). `tend` and `lint` *enforce* (deterministic housekeeping).

No daemon ships — heartbeat is a deployment concern (cron / launchd / systemd recipes in [MAINTENANCE.md](MAINTENANCE.md)).

## MCP-native interface

The MCP server (`mycelium/mcp/server.py`) is the primary user interface; the CLI is a thin mirror. AI agents are first-class users — every operation that's exposed to a human is also a tool call.

Access is gated by file flags in `~/.mycelium/`:

- `.read_enabled` (default ON) — read tools work
- `.write_enabled` (default OFF) — write tools work

Toggle via `/mycelium-on`, `/mycelium-off` skills. Never edit the flag files directly — the skills carry additional safety logic.

Tool surface (35 tools at v0.5.0):

```
ingestion        add_signal · ingest_direct · ingest_batch
graph            add_neuron · get_neuron · list_neurons · update_neuron
                 delete_neuron · merge_neurons · rethink_neuron
                 add_synapse · delete_synapse · add_mention
search           search · get_signal · get_signals · get_timeline · re_extract
intelligence    detect_communities · sleep_report · health
maintenance      tend · lint
vault/obsidian   vault_store · vault_link · obsidian_sync
portability      export_subgraph · import_subgraph
system           set_owner / get_owner · save_extraction_skill / list_extraction_skills
                 list_domains · get_domain · create_domain · update_domain · delete_domain
```

## Domain blueprints

Adaptive knowledge domains let the user teach the system how to process specific types of input (medical records, finance, reading notes…). A blueprint is a YAML file in `~/.mycelium/domains/`:

```yaml
name:        cardio
triggers:    [blood pressure, heart rate, BP, ekg]
vault_prefix: cortex/cardio
extraction:
  focus:     vital signs, medications, symptoms, dates
tracking:
  fields:    [systolic, diastolic, hr, weight]
```

On ingestion, content is matched against blueprint triggers; the matching domain's `vault_prefix` and `extraction.focus` are applied. Unmatched content falls back to `cortex/`. CRUD via 5 MCP tools and a `/mycelium-domain` interactive constructor.

## Telegram + VPS layer (v0.4.0)

A full graph interface from a phone: text, voice (Deepgram or local Whisper), photos, documents, forwards. Two modes:

- **Command mode** — fast, scripted; mirrors a subset of CLI/MCP for quick capture
- **Agent mode** — free text routed to the same Claude agent as Claude Code, with full access to the 35 MCP tools

Deployment patterns:

- Local dev — MCP via stdio, Neo4j on localhost
- Docker + API — MCP via HTTP, embeddings via DeepInfra
- Full Docker — MCP via HTTP, local BGE-M3 (~2 GB, air-gapped)
- VPS — always-on graph, Tailscale VPN, Syncthing for vault sync, Telegram bot

## Configuration

Single source of truth: `mycelium/config.py` (Pydantic Settings). Priority: env vars > `.env` > defaults. Search chain:

```
1. ~/.mycelium/.env       global user config
2. <project>/.env         project root
3. ./.env                 cwd override
```

Env prefix: `MYCELIUM_`. Nested delimiter: `__`. Examples:

```
MYCELIUM_NEO4J__URI=bolt://localhost:7687
MYCELIUM_SEMANTIC__PROVIDER=api
MYCELIUM_TEND__STALENESS_HOURS=24
```

Settings groups: `neo4j`, `decay`, `semantic`, `llm`, `search`, `dedup`, `contradiction`, `ingestion`, `summary`, `vault`, `log`, `community`, `render`, `mcp`, `owner`, `interaction`, `obsidian`, `telegram`, `tend`.

## Key Cypher patterns

**Decay-weighted scoring** (single source of truth via helper):

```python
from mycelium.utils.decay import cypher_effective_weight

ew = cypher_effective_weight("n", staleness_hours=24)
query = f"MATCH (n:Neuron) WHERE n.expired_at IS NULL RETURN n.name, {ew} AS weight ORDER BY weight DESC"
```

**Synapse provenance** (which Signal a fact came from):

```cypher
MATCH (a:Neuron)-[f:SYNAPSE]->(b:Neuron)
WHERE $signal_uuid IN f.episodes
  AND f.expired_at IS NULL
RETURN a.name, f.fact, b.name
```

**Active edges only** (the standard filter for any read query):

```cypher
MATCH (a:Neuron)-[f:SYNAPSE]->(b:Neuron)
WHERE f.expired_at IS NULL
  AND (f.invalid_at IS NULL OR f.invalid_at > datetime())
RETURN a.name, f.fact, b.name
```

## Stack

```
Local node (self-sovereign)
  Neo4j  >= 5.18    Graph + native vector index (unified store, no Weaviate/Pinecone)
  BGE-M3 / API      Embeddings (1024-dim, pluggable via SemanticSettings)
  Claude Code CLI   Extraction + analysis (LLM runtime, swappable to API)
  MCP server        Primary interface for AI agents

Federated layer (post-MVP)
  Differential privacy + zero-knowledge proofs for pattern exchange
```

Pluggability boundaries:

- **LLM** — `mycelium.llm.base.LLMBackend` (Claude CLI, OpenAI API via litellm, Ollama)
- **Embedder** — `mycelium.embedder.client.EmbedderClient` (API, local, mock)
- **Reranker** — `mycelium.search.rerankers` (decay, blend, mmr, cross-encoder, node-distance)

## What's intentionally NOT here

- No daemons in the product. Scheduling is a deployment concern.
- No GDS dependency. Default Neo4j image carries APOC only; `centrality_refresh` is degree-only. PageRank/Louvain remain in `/mycelium-distill` (uses networkx, not GDS).
- No P2P / federation implementation yet — only the data model is set up to support it (privacy-by-design via group_id placeholder, planned for L6).
- No autonomous agents trigger themselves. Every operation is user-triggered (CLI, skill, or MCP call). Tools designed to be composable into external schedulers, not embedded ones.

## See also

- [README.md](README.md) — documentation index
- [MAINTENANCE.md](MAINTENANCE.md) — `tend`/`lint` cookbook + scheduling recipes
- [../README.md](../README.md) — project overview, quickstart, MCP tool reference
