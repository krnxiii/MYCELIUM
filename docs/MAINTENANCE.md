# Maintenance Toolkit

`mycelium tend` and `mycelium lint` keep the graph in shape with deterministic Tier 0 operations. No LLM, idempotent, safe to run at any cadence — daily via cron, weekly by hand, or on-demand from `/mycelium-tend`.

## Concept

Two tools, complementary:

| Tool | Reads / Writes | Purpose |
|------|---|---|
| `lint` | read-only | Structural health check + score 0..1 |
| `tend` | writes | Run one or more maintenance stages |

`/mycelium-distill` and `/mycelium-discover` remain the LLM-driven counterparts — they *interpret*. `tend` and `lint` *enforce*.

## Stages

All stages are independently runnable; the orchestrator runs them in order with isolated failure semantics (one stage crashing does not abort the rest).

| Stage | What it does |
|-------|---|
| `decay_sweep` | Materialize `Neuron.effective_weight` and `Neuron.last_swept_at` |
| `prune_dead` | Delete soft-expired data, soft-expire past-TTL synapses, mark zombie `extracting` Signals as failed |
| `vault_compact` | Drop orphan entries from `<vault>/.index.json`; report (never delete) orphan files / dangling-signal references |
| `centrality_refresh` | Materialize `Neuron.degree` (active synapse count) |

Run all: `mycelium tend`. Run a subset: `mycelium tend --stage decay_sweep --stage centrality_refresh`.

## Reports

Both commands append a markdown summary to `<vault>/_AGENT/log/YYYY-MM-DD-{tend|lint}.md` when `--report` is set (default for `tend`, opt-in for `lint`). Same-day runs concatenate with `---` separators so the file becomes a chronological trail.

`--json` prints the full structured payload to stdout (suitable for piping into your own dashboards).

## CLI quick reference

```sh
# Diagnose first
mycelium lint                      # human-readable
mycelium lint --json               # structured

# Maintain
mycelium tend                      # default stages, write report
mycelium tend --dry-run            # compute without writing
mycelium tend --stage decay_sweep  # one stage only
mycelium tend --no-report          # skip _AGENT/log/ append
```

`mycelium lint` exits 1 if any findings are present — useful for cron / CI gates.

## MCP tools

```
tend(stages=[], dry_run=False, write_report=True)  → write-gated
lint(write_report=False)                           → read-gated
```

## Scheduling cookbook

MYCELIUM does **not** ship a daemon. Scheduling is your deployment's responsibility — the design treats `tend` as an external job, not an in-process timer.

### macOS — launchd

`~/Library/LaunchAgents/com.mycelium.tend.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>           <string>com.mycelium.tend</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/mycelium</string>
    <string>tend</string>
    <string>--report</string>
  </array>
  <key>StartInterval</key>   <integer>3600</integer>  <!-- hourly -->
  <key>StandardOutPath</key> <string>/tmp/mycelium-tend.out</string>
  <key>StandardErrorPath</key><string>/tmp/mycelium-tend.err</string>
</dict>
</plist>
```

```sh
launchctl load ~/Library/LaunchAgents/com.mycelium.tend.plist
```

### Linux — systemd

`~/.config/systemd/user/mycelium-tend.service`:

```ini
[Unit]
Description=MYCELIUM maintenance toolkit

[Service]
Type=oneshot
ExecStart=/usr/local/bin/mycelium tend --report
```

`~/.config/systemd/user/mycelium-tend.timer`:

```ini
[Unit]
Description=Run mycelium tend hourly

[Timer]
OnBootSec=15min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
```

```sh
systemctl --user enable --now mycelium-tend.timer
```

### Linux/macOS — cron

```cron
# Run every hour, log to file
0 * * * * /usr/local/bin/mycelium tend --report >> ~/.mycelium/cron.log 2>&1

# Lint nightly; fire a notification when issues found
0 3 * * * /usr/local/bin/mycelium lint || say "Mycelium needs attention"
```

## Cadence guidance

| How busy is your graph? | Suggested cadence |
|---|---|
| Hobbyist (few signals/week) | weekly `tend`, monthly `lint` |
| Active (daily ingestion) | daily `tend`, weekly `lint` |
| Heavy (batch ingestion, many tools) | hourly `tend`, daily `lint` |

`stale_swept_neurons` in `lint` will tell you if your cadence is too lazy.

## Settings

In `~/.mycelium/.env` or `MYCELIUM_TEND__*` env vars:

```
MYCELIUM_TEND__STALENESS_HOURS=24      # search falls back when older
MYCELIUM_TEND__WEAK_THRESHOLD=0.05     # decay_sweep "weak" cutoff
MYCELIUM_TEND__SWEEP_BATCH_SIZE=1000   # nodes per UNWIND batch
MYCELIUM_TEND__ZOMBIE_AGE_HOURS=24     # extracting → failed cutoff
MYCELIUM_TEND__VAULT_CHECK_GRAPH=true  # cross-check vault ↔ graph
```

## Backup, sync, and persistence

User data lives under `~/.mycelium/` (overridable via `MYCELIUM_DATA_DIR`). Container deployments mount this directory wholesale into `mycelium-app` and `mycelium-telegram` at `/root/.mycelium/`, so anything you put there survives `docker compose build`.

### What to back up

| Path | Back up? | Notes |
|------|----------|-------|
| `vault/` | yes | knowledge files, owns provenance |
| `domains/` | yes | domain blueprints (your taxonomy) |
| `skills/extraction/` | yes | user-saved extraction skills |
| `.env` | yes | secrets and per-host config |
| `neo4j/data/` | yes | the graph itself |
| `models/` | optional | re-downloadable from HF |
| `logs/` | no | derived |
| `syncthing/` | no | regenerated on first run |

### Cross-device sync (Mac ↔ VPS)

The VPS `syncthing` service has bind mounts for three personal-config classes (granular allow-list — explicit declaration of what is syncable):

| Folder ID | Mac path (host) | VPS path (container) |
|-----------|------------------|----------------------|
| `mycelium-vault` | `~/.mycelium/vault` | `/var/syncthing/vault` |
| `mycelium-domains` | `~/.mycelium/domains` | `/var/syncthing/domains` |
| `mycelium-skills` | `~/.mycelium/skills/extraction` | `/var/syncthing/skills` |

`bash scripts/connect-vps.sh` registers all three folders automatically on both sides via the Syncthing REST API — re-run any time to re-pair. To set up by hand, open `http://localhost:8384` (Mac) and `http://<tailscale-ip>:8384` (VPS) and add each folder with the paths above.

After pairing, editing a blueprint or skill on the laptop propagates to the VPS within seconds, and `load_skills()` / `load_domains()` hot-reload on next call.

`.env` is intentionally **not** synced — it contains secrets and per-host config. Migrate manually if needed (`make migrate-user-data` for one-shot lift from a legacy container).

### Migrating from older versions

If you upgraded from a version that stored user data inside the container (skills baked into the image, domain blueprints in `/root/.mycelium/domains/`, `.env` written by the telegram setup wizard), run the migration once before rebuilding:

```sh
make migrate-user-data
```

It copies three classes of at-risk data out of the running container and onto the host volume:

1. **User extraction skills** — anything in `/app/mycelium/skills/extraction/` not tracked in git (bundled defaults are skipped).
2. **Domain blueprints** — every YAML in `/root/.mycelium/domains/`.
3. **`.env`** — only if the container has one and the host doesn't.

Idempotent: existing host-side files are never overwritten. `make update` runs this automatically before rebuilding, so a vanilla `git pull && make update` upgrade is safe.

## Why no daemon?

By design. Daemons are a deployment concern, not a product feature:

- **Predictable cost** — no surprise tokens, no surprise CPU
- **One trigger source** — cron / launchd / systemd, never racing with itself
- **Trivial debugging** — `tend` is a normal CLI process; logs are `_AGENT/log/`
- **Composable** — drop into any orchestration (Airflow, GitHub Actions, k8s CronJob)
