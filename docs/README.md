# MYCELIUM Documentation

Three categories: **shipped with the repo**, **historical reference**, **personal notes**. Only the first lives in git.

## Shipped (in repo)

User- and contributor-facing reference. Every fresh clone includes these.

| File | Description |
|------|----------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Current system architecture: data model, decay, hybrid search, vault structure, MCP surface, key Cypher patterns. |
| [MAINTENANCE.md](MAINTENANCE.md) | `tend` / `lint` toolkit: usage, MCP/CLI surface, scheduling cookbook (cron / launchd / systemd), settings reference. |
| [README.md](README.md) | This index. |

## Historical reference (local-only)

Snapshots of design intent and migration plans. Fulfilled their purpose; preserved for archeology, not active reference. Lives under `docs/v2_done/` and `docs/v1/` on your local checkout — `.gitignore` keeps them out of the repo.

| File | Why it's archived |
|------|----------|
| `v2_done/V2_TRANSITION.txt` | The plan that took us from v1 → v2 (Feb 2026). Migration is done; current state is in [ARCHITECTURE.md](ARCHITECTURE.md). |
| `v2_done/COMPETITIVE_FEATURES.txt` | Feature tracker from the v2 design phase. Live status now lives in `BACKLOG.txt`. |
| `v2_done/QUICKSTART.txt` | Pre-v0.4 quickstart. Replaced by the project README. |
| `v2_done/RELEASE_SPECS.txt` | Pre-v0.4 release spec format. |
| `v1/` | Original v1 docs (ARCHITECTURE, CONCEPT, DATABASE_SCHEMA, SYSTEM_INTELLIGENCE). |

## Personal / planning notes (local-only)

Roadmap, design drafts, reviews. Not user-facing — gitignored deliberately. If you fork the repo you write your own.

| File | Purpose |
|------|----------|
| `BACKLOG.txt` | Master roadmap with priorities and statuses. Updated when work ships. |
| `DESIGN_DISTRIBUTED_ARCHITECTURE.txt` | VPS, Telegram, Tailscale, Syncthing design notes. |
| `DESIGN_METRIC_TRACKING.txt` | Metric tracking (time-series alongside the graph). |
| `DESIGN_TELEGRAM_UX.txt` | Telegram UX: rich input, voice, photo, forwards, autonomous reports. |
| `DESIGN_WIKI_SYNTHESIS.txt` | Wiki synthesis layer (R9): graph → markdown pages, save_insight. |
| `DESIGN_YOUTUBE_IMPORT.txt` | YouTube transcript ingest pipeline. |
| `DESIGN_CHATGPT_BULK_INGEST.txt` | ChatGPT extracts bulk ingest. |
| `DOMAIN_BLUEPRINTS.txt` | Adaptive domain blueprints for extraction. |
| `OBSIDIAN_LAYER.txt` | Obsidian visualization layer notes. |

To track one of these later, whitelist it in the project's `.gitignore`:

```
docs/*
!docs/README.md
!docs/ARCHITECTURE.md
!docs/MAINTENANCE.md
!docs/<your-new-file>.md
```
