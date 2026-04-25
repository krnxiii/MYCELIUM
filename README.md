# MYCELIUM

<div align="right">

[![EN](https://img.shields.io/badge/lang-EN-blue?style=flat-square)](README.md)
[![RU](https://img.shields.io/badge/lang-RU-lightgrey?style=flat-square)](README.ru.md)

</div>

<p align="center">
  <img src="assets/logo.JPG" alt="MYCELIUM" width="280" />
</p>

<p align="center">
  <a href="https://t.me/+93N9Kpw2PNcwYTEy"><img src="https://img.shields.io/badge/Telegram-Channel-blue?logo=telegram&style=flat-square" alt="Telegram" /></a>
</p>

Imagine every conversation with your AI agent starting not from scratch — but with full understanding of who you are, what you know, what you're working on, and what matters to you. You load documents, notes, articles, conversations. The system extracts structured knowledge, links facts to each other, stores everything locally on your machine. Your agent reads this graph as a native language: finding context, surfacing connections, personalizing every response.

Every neuron and synapse is embedded into a shared semantic space — vectors where meaning, not just words, determines proximity. Combined with graph architecture, this lets MYCELIUM find not only what you explicitly stored, but what relates to it, what it implies, and what hidden patterns emerge across your entire knowledge network.

MYCELIUM doesn't just store — it reasons. The system discovers non-obvious connections between distant concepts, auto-clusters your knowledge into emergent themes, and detects gaps and contradictions in your thinking. The more you use it, the sharper its model of you becomes.

Like fungal mycelium threading trees together through soil — sharing nutrients, propagating signals, enabling collective response — MYCELIUM creates a **Mind Wide Web**: knowledge that accumulates over time, decays if unvisited, strengthens with repetition, and surfaces what's fresh and relevant.

**What you get:**
- **Hybrid search** — vector similarity + keyword + graph traversal, fused
- **Temporal intelligence** — decay-weighted results: fresh and confirmed rises, stale fades
- **Emergent insights** — cross-neuron inference finds connections you haven't drawn explicitly
- **File similarity** — embedding-based links between vault files, visible in Obsidian graph
- **Adaptive domains** — teach the system how to process finances, reading notes, or any knowledge area with custom blueprints
- **Agent workspace** — persistent `_AGENT/` directory with auto-generated graph context, curated memory, and daily logs
- **Telegram bot** — full graph interface from your phone: text, voice, photos, documents, forwards. Fast commands + AI agent mode
- **VPS deployment** — always-on graph on a remote server with Tailscale VPN, Syncthing vault sync, and Telegram bot
- **Local-first sovereignty** — your machine, your data, your Neo4j. No cloud lock-in.

---

## Contents

- [How it works](#how-it-works)
- [Quickstart](#quickstart)
- [First 5 minutes](#first-5-minutes)
- [Skills](#skills-claude-code)
- [MCP Tools](#mcp-tools)
- [Domain blueprints](#domain-blueprints)
- [Telegram Bot](#telegram-bot)
- [Visualization](#visualization)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Makefile](#makefile)

---

## How it works

```
Text / Files / URLs                     AI Agent (Claude Code)
       ↓                                        ↑
   Signal (raw input)                    MCP tools
       ↓                                        ↑
   LLM extraction ──→ Neurons + Synapses ──→ Hybrid Search
                       (entities)  (facts)   (vector + BM25 + graph)
```

**Three layers of knowledge:**

| Layer | What it stores | Example |
|-------|---------------|---------|
| **Signal** | Raw input, preserved as-is | Text, PDF, conversation |
| **Neuron** | Extracted entity: person, concept, skill, event | `"Rust"` (skill), `"Alice"` (person) |
| **Synapse** | Semantic connection between neurons | `"Alice knows Rust since 2024"` |

**Memory that breathes.** Knowledge isn't static — it ages. Neurons you revisit grow stronger; those you ignore gradually fade. The math:

```
weight = confidence × e^(−decay_rate × days_since_last_mention)
```

Repeated confirmations lower the decay rate. Fresh + confident = surfaces first. Nothing gets hard-deleted — knowledge simply fades until re-confirmed.

---

## Quickstart

**Prerequisites:** Docker + Docker Compose, Python >= 3.12 + [uv](https://docs.astral.sh/uv/), [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI.

```bash
git clone <repo-url> && cd mycelium
bash scripts/install.sh   # interactive: picks scenario, configures .env, installs MCP
```

To remove everything later: `bash scripts/uninstall.sh`

The installer guides you through 4 scenarios:

| Scenario | Embeddings | Best for |
|----------|------------|----------|
| **1. Local dev** (`make quickstart`) | DeepInfra API | Development, hacking (MCP via stdio) |
| **2. Docker + API** (`make quickstart-app`) | DeepInfra API | Deploy without local Python (MCP via HTTP) |
| **3. Full Docker** (`make quickstart-docker`) | Local BGE-M3 (~2 GB) | Air-gapped, no external APIs (MCP via HTTP) |
| **4. VPS** (`make quickstart-vps`) | API or local | Always-on graph, Telegram bot, vault sync |

Or manually (scenario 1): `cp .env.example .env && make quickstart`

After install, MYCELIUM tools are available in Claude Code from any directory.

> **Docker note:** LLM extraction tools (`add_signal`, `re_extract`, `rethink_neuron`) require `claude` CLI unavailable inside Docker. Use `ingest_direct` / `ingest_batch` for Docker deployments.

---

## First 5 minutes

After install, from Claude Code:

```
# Enable write access
/mycelium-on

# Ingest a file — the graph bootstraps with ~30–50 neurons
/mycelium-ingest ~/Documents/my_notes.md

# Ask questions across everything you've ingested
/mycelium-recall "what are my main interests?"

# See graph health: strongest knowledge, what's fading, gaps
/mycelium-reflect
```

---

## Skills (Claude Code)

Slash commands that wrap common workflows. Available from any directory in Claude Code.

**Access control**

| Skill | What it does |
|-------|-------------|
| `/mycelium-on` | Enable read + write access |
| `/mycelium-off` | Disable all access |

**Working with your graph**

`/mycelium-ingest <path>` — Full ingestion workflow: stores file in vault → reads graph context (avoids duplicates) → auto-detects domain → extracts neurons and synapses with LLM → links cross-section connections → writes Obsidian frontmatter. Returns signal UUIDs, neurons created/merged, synapse count.

`/mycelium-recall <query>` — Hybrid search + synthesis: runs vector + BM25 + graph traversal → gets full neuron context → synthesizes an answer → cites sources with provenance.

**Maintenance**

`/mycelium-reflect` — Graph health snapshot: strongest knowledge by weight, fading knowledge, type distribution, gaps, and concrete recommendations on what to enrich or clean.

`/mycelium-distill` — Cleanup run (max 10 actions): merges near-duplicate neurons, rethinks weak neurons with LLM, flags orphans for deletion. Runs `sleep_report` before and after to verify improvement.

`/mycelium-tend` — Maintenance toolkit (no LLM, deterministic): `lint` first to diagnose (structured findings + 0..1 health score), then `tend` to act — recompute decay weights, prune soft-deleted data, reconcile vault, refresh degree. See [docs/MAINTENANCE.md](docs/MAINTENANCE.md) for cron / launchd / systemd recipes.

`/mycelium-discover` — Pattern discovery (non-destructive, max 10 inferences): clusters neurons into themes via Louvain algorithm, infers hidden cross-cluster connections, surfaces gaps and contradictions. Only adds, never deletes.

**Setup**

`/mycelium-domain [name]` — Interactive domain constructor: asks clarifying questions → generates blueprint YAML (vault prefix, triggers, extraction focus, tracking fields) → creates it in `~/.mycelium/domains/`. On next ingest, matching files use the domain's extraction rules automatically.

---

## MCP Tools

Full capabilities exposed via [MCP](https://modelcontextprotocol.io). Call directly from Claude Code or any MCP-compatible client.

<details>
<summary>Ingestion</summary>

| Tool | Description |
|------|-------------|
| `add_signal` | Ingest text/file/URL, extract knowledge (supports `async_mode`) |
| `ingest_direct` | Ingest pre-extracted neurons and synapses |
| `ingest_batch` | Bulk ingest multiple items with shared dedup |

</details>

<details>
<summary>Knowledge graph</summary>

| Tool | Description |
|------|-------------|
| `add_neuron` | Create a neuron manually |
| `get_neuron` | Get neuron by UUID with synapses and timeline |
| `list_neurons` | List neurons with filters (type, name, sort, limit) |
| `update_neuron` | Update neuron fields |
| `delete_neuron` | Soft-delete neuron and its synapses |
| `merge_neurons` | Merge two neurons into one, rewiring all synapses |
| `rethink_neuron` | LLM re-analyzes neuron with full context, rewrites summary |
| `add_synapse` | Create a synapse between neurons |
| `delete_synapse` | Soft-delete a synapse |
| `add_mention` | Record a mention of a neuron in a signal |

</details>

<details>
<summary>Search & retrieval</summary>

| Tool | Description |
|------|-------------|
| `search` | Hybrid search: vector + BM25 + graph traversal. Prefixes: `lex:` `vec:` `hyde:` `vec+lex:` |
| `get_signal` | Get signal by UUID |
| `get_signals` | List signals with status filter |
| `get_timeline` | Get temporal history of a neuron |
| `re_extract` | Re-run extraction on a signal |

</details>

<details>
<summary>Intelligence</summary>

| Tool | Description |
|------|-------------|
| `detect_communities` | Auto-cluster neurons into thematic groups via Louvain |
| `sleep_report` | Analyze graph health: weak neurons, near-duplicates, stale data, gaps |
| `health` | Quick system stats: neuron/synapse counts, Neo4j status |

</details>

<details>
<summary>Maintenance (v0.5)</summary>

| Tool | Description |
|------|-------------|
| `lint` | Read-only structural health check + 0..1 score. Findings by severity (zombies, expired data, stale sweep, duplicates) |
| `tend` | Run maintenance stages (decay_sweep, prune_dead, vault_compact, centrality_refresh). Idempotent, no LLM. Optionally appends report to `_AGENT/log/` |

CLI mirrors: `mycelium tend [--stage S]... [--dry-run]` and `mycelium lint [--json]`. See [docs/MAINTENANCE.md](docs/MAINTENANCE.md) for cron / launchd / systemd recipes.

</details>

<details>
<summary>Vault & Obsidian</summary>

| Tool | Description |
|------|-------------|
| `vault_store` | Store a file in the vault (SHA-256 addressed, `cortex/` by default) |
| `vault_link` | Link a vault file to its signal + inject Obsidian frontmatter |
| `obsidian_sync` | Sync all vault files: relations, similarity links, move detection, agent context. Use `ingest=true` to auto-ingest unindexed files |

</details>

<details>
<summary>Portability</summary>

| Tool | Description |
|------|-------------|
| `export_subgraph` | Export neurons + synapses + signals as JSON |
| `import_subgraph` | Import subgraph JSON (re-embeds if model differs) |

</details>

<details>
<summary>System</summary>

| Tool | Description |
|------|-------------|
| `set_owner` / `get_owner` | Manage owner identity |
| `save_extraction_skill` / `list_extraction_skills` | Reusable extraction pattern templates |
| `list_domains` / `get_domain` | Browse domain blueprints |
| `create_domain` / `update_domain` / `delete_domain` | Manage knowledge domains |

</details>

**Access control** — tools are gated by file-flags in `~/.mycelium/`:
- `.read_enabled` — read tools work (default: ON)
- `.write_enabled` — write tools work (default: OFF)

Toggle via: `/mycelium-on`, `/mycelium-off`.

---

## Domain blueprints

Domains teach MYCELIUM how to read a specialized knowledge area. Each blueprint defines:
- **Triggers** — keywords for auto-detection during ingest
- **Vault prefix** — where files of this type are stored
- **Anchor neuron** — hub node linking all domain knowledge
- **Extraction focus** — domain-specific prompting for LLM
- **Tracking fields** — attributes to measure over time (enables trend detection)

Example — research papers domain:

```yaml
name: research
vault_prefix: reading/research/
triggers: ["abstract", "doi", "arxiv", "references", "methodology"]
anchor_neuron: "Research"
extraction:
  focus: "core arguments, concepts, authors, methodologies, open questions"
  neuron_types: ["concept", "person", "belief", "project"]
tracking:
  fields: ["title", "author", "year", "field"]
  analysis: "surface recurring themes, find conceptual bridges between papers"
```

Create interactively: `/mycelium-domain research`

On next `/mycelium-ingest` for any matching file: domain is auto-detected by triggers, extraction rules are applied, file goes to `reading/research/`, and extracted neurons are linked to the anchor. Concepts from different papers connect automatically — an idea from a 2019 paper may resurface as a link to your current project.

---

## Visualization

Two complementary ways to explore your knowledge graph:

**Obsidian** (primary) — open `~/.mycelium/vault/` as an Obsidian vault. Files get YAML frontmatter with `mycelium_related` (shared neuron links) and `mycelium_similar` (embedding similarity links). Use Obsidian Graph View for visual exploration. Enable neuron projection: `MYCELIUM_OBSIDIAN__PROJECT_NEURONS=true`, then run `obsidian_sync`. File moves are auto-detected via content hash — reorganize freely without losing graph connections.

**Sigma.js** (optional) — interactive browser-based graph viewer with ForceAtlas2 physics, type-colored nodes (Catppuccin Mocha palette), search, hover tooltips, and edge/node/label size controls. Enable: `MYCELIUM_RENDER__ENABLED=true`, then `mycelium render` or `make render`. Opens at `http://localhost:8500`.

---

## Architecture

```
┌─ Local Node (Self-Sovereign) ──────────────────────────┐
│  Neo4j >= 5.18     — graph + vector indices (unified)  │
│  BGE-M3 / API      — embeddings (1024-dim, pluggable)  │
│  Claude Code CLI   — extraction + analysis (LLM)       │
│  MCP Server        — interface for AI agents           │
│  Vault + Obsidian  — file storage (cortex/) + graph viz │
│  Agent Workspace   — _AGENT/ context, memory, logs     │
│  Sigma.js (opt)    — interactive graph viewer :8500    │
└────────────────────────────────────────────────────────┘
```

### Search pipeline

```
Query → [vector name] [vector summary] [BM25] [vector synapse] [BM25 synapse]
                              ↓
                    RRF fusion (reciprocal rank)
                              ↓
                    Decay-weighted reranking
                              ↓
                    Optional: MMR diversity / cross-encoder / node distance
                              ↓
                    Position-aware blending → Results
```

Supports query prefixes: `lex:` (keyword), `vec:` (semantic), `hyde:` (hypothetical document).

---

## Configuration

All config via environment variables or `.env` file. See `.env.example` for the full list with comments.

Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MYCELIUM_SEMANTIC__PROVIDER` | `api` | Embeddings: `api` (DeepInfra) or `local` (BGE-M3) |
| `MYCELIUM_SEMANTIC__API_KEY` | — | DeepInfra API key |
| `MYCELIUM_LLM__MODEL` | `sonnet` | LLM for extraction: `sonnet` / `haiku` / `opus` |
| `MYCELIUM_OWNER__NAME` | — | Your name (for first-person linking) |
| `MYCELIUM_RENDER__ENABLED` | `false` | Enable Sigma.js graph viewer (`:8500`) |
| `MYCELIUM_MCP__TRANSPORT` | `stdio` | `stdio` or `streamable-http` |

> **HTTP transport:** Scenarios 2 and 3 run MCP over HTTP (port 8000). Manual registration: `claude mcp add -t http -s user mycelium http://localhost:8000/mcp`

---

## Telegram Bot

Full graph interface via Telegram — text, voice messages, photos, documents, forwards.

**Two modes:**
- **Fast mode** — instant commands without AI: `/capture`, `/search`, `/status`, `/today`, `/neurons`, `/domains`, `/abort`, `/level`
- **Full mode** — free text goes to the same AI agent as Claude Code, with access to all 35 MCP tools

**Input types:** text, voice (Whisper local or Deepgram), photos, documents (album batching), forwarded messages with source attribution.

**Setup:** create a bot via `@BotFather`, add the token to `.env` (`MYCELIUM_TELEGRAM__BOT_TOKEN`). VPS scenario configures this automatically.

---

## Makefile

| Target | Description |
|--------|-------------|
| `make quickstart` | Full local setup (Neo4j + deps + indices + MCP) |
| `make quickstart-app` | Docker setup with API embeddings |
| `make quickstart-docker` | Full Docker setup with local embeddings |
| `make quickstart-vps` | VPS deployment (Neo4j + MCP + Telegram + Syncthing + Tailscale) |
| `make update` | Pull latest code, rebuild, restart services |
| `make up` / `make down` | Start / stop Neo4j |
| `make reset` | Wipe Neo4j data and restart |
| `make uninstall` | Remove everything |

---

## License

[MIT](LICENSE)
