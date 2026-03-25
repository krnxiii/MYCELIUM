"""Agent workspace: auto-context, memory, daily logs in _AGENT/."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import structlog

from mycelium.driver.driver import GraphDriver
from mycelium.vault.storage import VaultStorage

log = structlog.get_logger()

_AGENT_DIR = "_AGENT"
_LOG_DIR   = "log"


async def update_context(driver: GraphDriver, vault: VaultStorage) -> Path:
    """Generate _agent/context.md — graph snapshot for agent bootstrap."""
    agent_dir = vault.root / _AGENT_DIR
    agent_dir.mkdir(parents=True, exist_ok=True)

    stats   = await _graph_stats(driver)
    top     = await _top_neurons(driver, limit=15)
    recent  = await _recent_signals(driver, days=7, limit=10)
    owner   = await _owner_info(driver)
    domains = await _active_domains(driver)

    lines = [
        "# Graph Context",
        f"*Auto-generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}*",
        "",
        "## Stats",
        f"- Neurons: {stats['neurons']}",
        f"- Synapses: {stats['synapses']}",
        f"- Signals: {stats['signals']}",
    ]

    if owner:
        lines += ["", "## Owner", f"- {owner}"]

    if domains:
        lines += ["", "## Active Domains"]
        for d in domains:
            lines.append(f"- {d}")

    if top:
        lines += ["", "## Top Neurons (by effective weight)"]
        for n in top:
            lines.append(
                f"- **{n['name']}** ({n['type']}) "
                f"w={n['weight']:.3f} c={n['confirmations']}"
            )

    if recent:
        lines += ["", "## Recent Signals (7 days)"]
        for s in recent:
            lines.append(f"- {s['date']} — {s['name']}")

    lines.append("")

    path = agent_dir / "context.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("agent_context_updated", path=str(path.relative_to(vault.root)))
    return path


def ensure_workspace(vault: VaultStorage) -> None:
    """Ensure _agent/ structure exists with template files."""
    agent_dir = vault.root / _AGENT_DIR
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / _LOG_DIR).mkdir(exist_ok=True)

    memory_path = agent_dir / "memory.md"
    if not memory_path.exists():
        memory_path.write_text(
            "# Agent Memory\n\n"
            "## Rules\n\n"
            "## Observations\n\n"
            "## Working Context\n",
            encoding="utf-8",
        )


# ── Queries ──────────────────────────────────────────────


async def _graph_stats(driver: GraphDriver) -> dict:
    rows = await driver.execute_query(
        "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
        "WITH count(n) AS neurons "
        "OPTIONAL MATCH ()-[r:SYNAPSE]->() WHERE r.expired_at IS NULL "
        "WITH neurons, count(r) AS synapses "
        "OPTIONAL MATCH (s:Signal) "
        "RETURN neurons, synapses, count(s) AS signals"
    )
    r = rows[0] if rows else {}
    return {
        "neurons":  r.get("neurons", 0),
        "synapses": r.get("synapses", 0),
        "signals":  r.get("signals", 0),
    }


async def _top_neurons(driver: GraphDriver, limit: int = 15) -> list[dict]:
    rows = await driver.execute_query(
        "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
        "WITH n, coalesce(n.importance, n.confidence, 0.5) "
        "  * exp(-n.decay_rate * "
        "    duration.between(n.freshness, datetime()).days) AS w "
        "RETURN n.name AS name, n.neuron_type AS type, "
        "  w AS weight, coalesce(n.confirmations, 0) AS confirmations "
        "ORDER BY w DESC LIMIT $limit",
        {"limit": limit},
    )
    return [
        {
            "name":          r["name"],
            "type":          r["type"] or "?",
            "weight":        r["weight"] or 0,
            "confirmations": r["confirmations"],
        }
        for r in rows
    ]


async def _recent_signals(
    driver: GraphDriver, days: int = 7, limit: int = 10,
) -> list[dict]:
    rows = await driver.execute_query(
        "MATCH (s:Signal) "
        "WHERE s.created_at > datetime() - duration({days: $days}) "
        "RETURN s.name AS name, "
        "  toString(date(s.created_at)) AS date "
        "ORDER BY s.created_at DESC LIMIT $limit",
        {"days": days, "limit": limit},
    )
    return [{"name": r["name"], "date": r["date"] or "?"} for r in rows]


async def _owner_info(driver: GraphDriver) -> str:
    rows = await driver.execute_query(
        "MATCH (n:Neuron {neuron_type: 'owner'}) "
        "WHERE n.expired_at IS NULL "
        "RETURN n.name AS name, n.summary AS summary "
        "LIMIT 1"
    )
    if not rows:
        return ""
    r = rows[0]
    summary = f" — {r['summary']}" if r.get("summary") else ""
    return f"{r['name']}{summary}"


async def _active_domains(driver: GraphDriver) -> list[str]:
    """List domain blueprints (from filesystem, not graph)."""
    domain_dir = Path.home() / ".mycelium" / "domains"
    if not domain_dir.exists():
        return []
    return sorted(
        p.stem for p in domain_dir.glob("*.yaml")
    )
