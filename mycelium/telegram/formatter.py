"""MCP response → Telegram HTML formatter with fallback chain."""

from __future__ import annotations

from html import escape
from typing import Any


def format_signal_created(result: dict[str, Any]) -> tuple[str, str]:
    """Format add_signal result."""
    status = result.get("status", "unknown")
    uuid   = result.get("signal_uuid", "")[:8]

    neurons  = result.get("neurons", [])
    synapses = result.get("synapses", [])

    lines = [f"Signal captured [{status}] ({uuid})"]
    if neurons:
        n_names = ", ".join(n.get("name", "?") for n in neurons[:5])
        lines.append(f"Neurons: {n_names}")
    if synapses:
        lines.append(f"Synapses: {len(synapses)}")

    plain = "\n".join(lines)
    html  = f"<b>Signal captured</b> [{escape(status)}]\n"
    if neurons:
        html += "<b>Neurons:</b> " + ", ".join(
            f"<code>{escape(n.get('name', '?'))}</code>" for n in neurons[:5]
        ) + "\n"
    if synapses:
        html += f"<b>Synapses:</b> {len(synapses)}\n"

    return plain, html


def format_search(result: dict[str, Any]) -> tuple[str, str]:
    """Format search results."""
    synapses = result.get("synapses", [])
    neurons  = result.get("neurons", [])
    ms       = result.get("duration_ms", 0)

    if not synapses and not neurons:
        msg = "No results found."
        return msg, msg

    lines_plain: list[str] = []
    lines_html:  list[str] = []

    for s in synapses[:7]:
        fact   = s.get("fact", "")
        src    = s.get("source", "")
        tgt    = s.get("target", "")
        score  = s.get("score", 0)
        lines_plain.append(f"  [{score:.2f}] {src} → {tgt}: {fact}")
        lines_html.append(
            f"  <b>[{score:.2f}]</b> {escape(src)} → {escape(tgt)}: "
            f"<i>{escape(fact)}</i>"
        )

    for n in neurons[:5]:
        name  = n.get("name", "?")
        ntype = n.get("type", "")
        score = n.get("score", 0)
        lines_plain.append(f"  [{score:.2f}] {name} ({ntype})")
        lines_html.append(
            f"  <b>[{score:.2f}]</b> <code>{escape(name)}</code> ({escape(ntype)})"
        )

    footer = f"({len(synapses)} synapses, {len(neurons)} neurons, {ms}ms)"
    plain = "Search results:\n" + "\n".join(lines_plain) + f"\n{footer}"
    html = (
        "<b>Search results:</b>\n"
        + "\n".join(lines_html)
        + f"\n<i>{escape(footer)}</i>"
    )
    return plain, html


def format_health(
    health: dict[str, Any],
    metrics: dict[str, Any],
) -> tuple[str, str]:
    """Format health + metrics status."""
    neo4j    = health.get("neo4j", "?")
    neurons  = health.get("neurons", 0)
    signals  = health.get("signals", 0)
    active   = health.get("active_synapses", 0)
    expired  = health.get("expired_synapses", 0)
    stale    = health.get("stale", [])

    lines = [
        f"Neo4j: {neo4j}",
        f"Neurons: {neurons}",
        f"Signals: {signals}",
        f"Synapses: {active} active / {expired} expired",
    ]
    if stale:
        fading = ", ".join(s.get("name", "?") for s in stale[:5])
        lines.append(f"Fading: {fading}")

    # Metrics summary
    stats = metrics.get("stats", {})
    if stats:
        lines.append("")
        for field, st in list(stats.items())[:3]:
            trend = st.get("trend", "")
            avg   = st.get("avg", 0)
            lines.append(f"{field}: avg={avg:.1f} {trend}")

    plain = "Status:\n" + "\n".join(f"  {line}" for line in lines)

    html_lines = [f"<b>Neo4j:</b> {escape(str(neo4j))}"]
    html_lines.append(f"<b>Neurons:</b> {neurons}  |  <b>Signals:</b> {signals}")
    html_lines.append(f"<b>Synapses:</b> {active} active / {expired} expired")
    if stale:
        html_lines.append(
            "<b>Fading:</b> " + ", ".join(
                f"<code>{escape(s.get('name', '?'))}</code>" for s in stale[:5]
            )
        )

    html = "\n".join(html_lines)
    return plain, html


def format_timeline(result: dict[str, Any] | list[dict[str, Any]]) -> tuple[str, str]:
    """Format recent signals as today's activity."""
    # result is list from get_signals
    items = result if isinstance(result, list) else result.get("signals", [])

    if not items:
        msg = "No recent activity."
        return msg, msg

    lines_plain: list[str] = []
    lines_html:  list[str] = []

    for sig in items[:10]:
        name    = sig.get("name", sig.get("source_desc", "?"))
        status  = sig.get("status", "")
        created = sig.get("created_at", "")[:16]  # trim to minute
        lines_plain.append(f"  {created}  {name} [{status}]")
        lines_html.append(
            f"  <code>{escape(created)}</code>  {escape(name)} [{escape(status)}]"
        )

    plain = "Recent signals:\n" + "\n".join(lines_plain)
    html  = "<b>Recent signals:</b>\n" + "\n".join(lines_html)
    return plain, html


def format_neurons(result: dict[str, Any] | list[dict[str, Any]]) -> tuple[str, str]:
    """Format neuron list."""
    items = result if isinstance(result, list) else result.get("neurons", [])

    if not items:
        msg = "No neurons found."
        return msg, msg

    lines_plain: list[str] = []
    lines_html:  list[str] = []

    for n in items[:15]:
        name   = n.get("name", "?")
        ntype  = n.get("type", "")
        weight = n.get("weight", 0)
        confs  = n.get("confirmations", 0)
        lines_plain.append(f"  {name} ({ntype}) w={weight:.2f} x{confs}")
        lines_html.append(
            f"  <code>{escape(name)}</code> ({escape(ntype)}) "
            f"w=<b>{weight:.2f}</b> x{confs}"
        )

    plain = f"Neurons ({len(items)}):\n" + "\n".join(lines_plain)
    html  = f"<b>Neurons ({len(items)}):</b>\n" + "\n".join(lines_html)
    return plain, html


def format_domains(result: dict[str, Any]) -> tuple[str, str]:
    """Format domain list."""
    domains = result.get("domains", [])
    count   = result.get("count", len(domains))

    if not domains:
        msg = "No domains configured."
        return msg, msg

    lines_plain: list[str] = []
    lines_html:  list[str] = []

    for d in domains:
        name = d.get("name", "?")
        desc = d.get("description", "")
        triggers = ", ".join(d.get("triggers", [])[:3])
        lines_plain.append(f"  {name}: {desc}")
        if triggers:
            lines_plain.append(f"    triggers: {triggers}")
        lines_html.append(f"  <b>{escape(name)}</b>: {escape(desc)}")
        if triggers:
            lines_html.append(f"    triggers: <i>{escape(triggers)}</i>")

    plain = f"Domains ({count}):\n" + "\n".join(lines_plain)
    html  = f"<b>Domains ({count}):</b>\n" + "\n".join(lines_html)
    return plain, html
