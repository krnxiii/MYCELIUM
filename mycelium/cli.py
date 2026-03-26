"""MYCELIUM v2 CLI: thin wrapper over Mycelium orchestrator."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import typer

from mycelium import __version__
from mycelium.config import Settings, load_settings
from mycelium.core.models import SignalType
from mycelium.core.mycelium import Mycelium
from mycelium.core.types import MyceliumClients
from mycelium.driver.neo4j_driver import Neo4jDriver
from mycelium.embedder.client import make_embedder
from mycelium.llm import make_llm_client

app = typer.Typer(name="mycelium", help="MYCELIUM — Mind Wide Web")


# ── Helpers ───────────────────────────────────────────────────────

_t0: float = 0.0


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _init_logging() -> Settings:
    """Load settings + init logging (file-only, no console spam)."""
    from mycelium.logging import setup_logging

    s = load_settings()
    setup_logging(
        level=s.log.level, fmt=s.log.format, log_dir=s.log.dir,
        max_bytes=s.log.max_bytes, backup_count=s.log.backup_count,
        console=False,
    )
    return s


_transient = False          # last line was overwritable (no \n)


def _log(step: str, detail: str = "") -> None:
    """Unified CLI output: [elapsed] step  detail.

    Transient messages (waiting…, thinking:) overwrite in place
    so the terminal doesn't flood with timer ticks.
    """
    import sys

    global _transient
    elapsed = time.monotonic() - _t0
    pad     = step.ljust(12)
    line    = f"  [{elapsed:5.1f}s] {pad} {detail}"

    is_transient = ("waiting…" in detail or "thinking:" in detail)

    if _transient:
        sys.stdout.write("\r\033[K")        # erase previous transient

    if is_transient:
        sys.stdout.write(line)
        sys.stdout.flush()
        _transient = True
    else:
        typer.echo(line)
        _transient = False


async def _make(s: Settings | None = None) -> tuple[Mycelium, Neo4jDriver, Settings]:
    """Create Mycelium + driver (caller must close driver)."""
    s   = s or load_settings()
    drv = Neo4jDriver(s.neo4j)
    await drv.__aenter__()

    clients = MyceliumClients(
        driver   = drv,
        embedder = make_embedder(s.semantic),
        llm      = make_llm_client(s.llm),
    )
    return Mycelium(clients, s), drv, s


# ── Commands ──────────────────────────────────────────────────────


@app.command()
def version() -> None:
    """Print version."""
    typer.echo(f"mycelium {__version__}")


@app.command()
def ingest(
    text:   str  = typer.Argument(..., help="Text to ingest"),
    name:   str  = typer.Option("", help="Signal name"),
    source: str  = typer.Option("text", help="Source type"),
    focus:  str  = typer.Option("", help="Extraction focus"),
    quiet:  bool = typer.Option(False, "-q", "--quiet", help="Suppress progress"),
) -> None:
    """Ingest text through the extraction pipeline."""
    global _t0
    _t0 = time.monotonic()
    s   = _init_logging()

    progress = None if quiet else _log

    async def _go() -> None:
        my, drv, _ = await _make(s)
        try:
            sig, neurons, synapses, questions = await my.add_episode(
                text, name=name, source_type=SignalType(source),
                on_progress=progress, extraction_focus=focus,
            )
            typer.echo(
                f"\n  Signal {sig.uuid[:8]} ({sig.status.value})"
                f" — {len(neurons)} neurons, {len(synapses)} synapses"
            )
            for n in neurons:
                typer.echo(f"    + {n.neuron_type}: {n.name}")
            for syn in synapses:
                typer.echo(f"    ~ {syn.relation}: {syn.fact[:80]}")
            for q in questions:
                typer.echo(f"    ? [{q.category}] {q.text}")
        finally:
            await drv.close()
    _run(_go())


@app.command()
def ingest_file(
    path:     Path = typer.Argument(  # noqa: B008
        ..., exists=True, help="File to ingest",
    ),
    category: str  = typer.Option("", "--category", "-c", help="Vault category override"),
    quiet:    bool = typer.Option(False, "-q", "--quiet", help="Suppress progress"),
) -> None:
    """Ingest a file (stored in vault, text extracted)."""
    global _t0
    _t0 = time.monotonic()
    s   = _init_logging()

    progress = None if quiet else _log

    async def _go() -> None:
        my, drv, _ = await _make(s)
        try:
            if progress:
                progress("vault", f"storing {path.name}")
            sig, neurons, synapses, _questions = await my.add_file(
                path, category=category, on_progress=progress,
            )
            typer.echo(
                f"\n  File {path.name} → {sig.uuid[:8]} ({sig.status.value})"
                f" — {len(neurons)} neurons, {len(synapses)} synapses"
            )
            for n in neurons:
                typer.echo(f"    + {n.neuron_type}: {n.name}")
            for syn in synapses:
                typer.echo(f"    ~ {syn.relation}: {syn.fact[:80]}")
        finally:
            await drv.close()
    _run(_go())


@app.command(name="search")
def search_cmd(
    query: str = typer.Argument(..., help="Search query"),
    top_k: int = typer.Option(10, "--top-k", help="Max results"),
) -> None:
    """Hybrid search with decay-weighted reranking."""
    async def _go() -> None:
        my, drv, _ = await _make()
        try:
            res = await my.search(query, top_k=top_k)
            methods = [m.value for m in res.methods]
            typer.echo(f"Search ({res.duration_ms}ms, {methods})")
            if res.neurons:
                typer.echo("\nNeurons:")
                for sn in res.neurons:
                    typer.echo(
                        f"  {sn.neuron.name} [{sn.neuron.neuron_type}] "
                        f"score={sn.score:.4f}"
                    )
            if res.synapses:
                typer.echo("\nSynapses:")
                for ss in res.synapses:
                    typer.echo(
                        f"  {ss.source_name} → {ss.target_name}: "
                        f"{ss.synapse.fact[:80]} (score={ss.score:.4f})"
                    )
            if not res.neurons and not res.synapses:
                typer.echo("No results.")
        finally:
            await drv.close()
    _run(_go())


@app.command()
def status() -> None:
    """Show graph health and statistics."""
    async def _go() -> None:
        s   = load_settings()
        drv = Neo4jDriver(s.neo4j)
        await drv.__aenter__()
        try:
            neo_ok = await drv.health_check()
            counts = await drv.execute_query(
                "OPTIONAL MATCH (n:Neuron) WITH count(n) AS neurons "
                "OPTIONAL MATCH (s:Signal) WITH neurons, count(s) AS signals "
                "OPTIONAL MATCH ()-[f:SYNAPSE]->() "
                "  WITH neurons, signals, count(f) AS synapses "
                "RETURN neurons, signals, synapses"
            )
            c = counts[0] if counts else {}
            typer.echo(
                f"Neo4j: {'ok' if neo_ok else 'unreachable'}\n"
                f"Neurons:  {c.get('neurons', 0)}\n"
                f"Signals:  {c.get('signals', 0)}\n"
                f"Synapses: {c.get('synapses', 0)}"
            )
        finally:
            await drv.close()
    _run(_go())


@app.command()
def serve(
    transport: str = typer.Option("", help="Transport override: stdio | streamable-http"),
    host:      str = typer.Option("", help="Bind address override (HTTP only)"),
    port:      int = typer.Option(0, help="Port override (HTTP only)"),
) -> None:
    """Start MCP server."""
    from mycelium.config import load_settings
    cfg = load_settings().mcp
    transport = transport or cfg.transport
    host      = host or cfg.host
    port      = port or cfg.port
    try:
        from mycelium.mcp.server import mcp as mcp_server
    except ImportError as exc:
        typer.echo("fastmcp not installed. pip install mycelium[mcp]", err=True)
        raise typer.Exit(1) from exc
    if transport == "stdio":
        mcp_server.run()
    else:
        mcp_server.run(transport=transport, host=host, port=port)  # type: ignore[arg-type]


@app.command()
def signals(
    sig_status: str = typer.Option("", "--status", help="Filter: pending|saved|failed"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """List signals."""
    async def _go() -> None:
        s   = load_settings()
        drv = Neo4jDriver(s.neo4j)
        await drv.__aenter__()
        try:
            where = "WHERE e.status = $status" if sig_status else ""
            rows = await drv.execute_query(
                f"MATCH (e:Signal) {where} "
                "RETURN e.uuid AS uuid, e.name AS name, "
                "  e.status AS status, toString(e.created_at) AS created "
                "ORDER BY e.created_at DESC LIMIT $limit",
                {"status": sig_status, "limit": limit},
            )
            for r in rows:
                uid = r['uuid'][:8]
                typer.echo(
                    f"  {uid}  [{r['status']}]  "
                    f"{r['name'][:60]}  {r['created']}"
                )
            if not rows:
                typer.echo("No signals.")
        finally:
            await drv.close()
    _run(_go())


def _ensure_neo4j() -> None:
    """Start Neo4j docker container if not running, wait for ready."""
    import shutil
    import subprocess
    import time as _time

    if not shutil.which("docker"):
        return  # no docker — assume external Neo4j
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "mycelium-neo4j"],
            capture_output=True, text=True, timeout=5,
        )
        if out.stdout.strip() == "true":
            return  # already running
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return

    # ensure data dir exists
    data_dir = Path.home() / ".mycelium" / "neo4j"
    (data_dir / "data").mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)

    typer.echo("Starting Neo4j…")
    # try `docker start` first (existing container), fallback to compose
    started = subprocess.run(
        ["docker", "start", "mycelium-neo4j"],
        capture_output=True, timeout=10,
    ).returncode == 0
    if not started:
        compose_cmd = ["docker", "compose"] if subprocess.run(
            ["docker", "compose", "version"], capture_output=True,
        ).returncode == 0 else ["docker-compose"]
        subprocess.run(compose_cmd + ["up", "-d", "neo4j"], timeout=30)

    # wait for bolt port
    import socket
    for i in range(30):
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", 7687)) == 0:
                typer.echo("Neo4j ready.")
                return
        _time.sleep(1)
        if i % 5 == 4:
            typer.echo(f"  waiting for Neo4j… ({i + 1}s)")
    typer.echo("Warning: Neo4j may not be ready yet.", err=True)


@app.command()
def purge(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Delete ALL data from Neo4j (neurons, signals, synapses). Requires confirmation."""
    if not force:
        confirm = typer.confirm("This will delete ALL data from the graph. Continue?")
        if not confirm:
            raise typer.Abort()

    async def _go() -> None:
        s   = load_settings()
        drv = Neo4jDriver(s.neo4j)
        await drv.__aenter__()
        try:
            result = await drv.execute_query(
                "MATCH (n) DETACH DELETE n RETURN count(n) AS deleted"
            )
            deleted = result[0]["deleted"] if result else 0
            typer.echo(f"Deleted {deleted} nodes (all relationships included).")
        finally:
            await drv.close()
    _run(_go())


@app.command()
def neurons(
    neuron_type: str = typer.Option("", "--type", help="Filter by neuron type"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """List neurons."""
    async def _go() -> None:
        s   = load_settings()
        drv = Neo4jDriver(s.neo4j)
        await drv.__aenter__()
        try:
            where = "WHERE e.neuron_type = $type" if neuron_type else ""
            rows = await drv.execute_query(
                f"MATCH (e:Neuron) {where} "
                "RETURN e.uuid AS uuid, e.name AS name, "
                "  e.neuron_type AS type, e.confidence AS conf, "
                "  e.confirmations AS cnt "
                "ORDER BY e.freshness DESC LIMIT $limit",
                {"type": neuron_type, "limit": limit},
            )
            for r in rows:
                typer.echo(
                    f"  {r['uuid'][:8]}  [{r['type']}]  {r['name']}  "
                    f"conf={r['conf']:.2f}  x{r['cnt']}"
                )
            if not rows:
                typer.echo("No neurons.")
        finally:
            await drv.close()
    _run(_go())


# ── Database management ───────────────────────────────────────


@app.command()
def backup(
    output: Path = typer.Option(  # noqa: B008
        None, "-o", "--output", help="Output file (default: mycelium_backup_<ts>.json)",
    ),
    include_expired: bool = typer.Option(False, "--include-expired", help="Include soft-deleted data"),
) -> None:
    """Export full graph to a JSON backup file."""
    from mycelium.core.export import export_subgraph

    ts   = time.strftime("%Y%m%d_%H%M%S")
    dest = output or Path(f"mycelium_backup_{ts}.json")

    async def _go() -> None:
        s   = load_settings()
        drv = Neo4jDriver(s.neo4j)
        await drv.__aenter__()
        try:
            data = await export_subgraph(drv, s, include_expired=include_expired)
            dest.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            st = data["stats"]
            typer.echo(
                f"Backup → {dest}\n"
                f"  Neurons:  {st['neurons']}\n"
                f"  Synapses: {st['synapses']}\n"
                f"  Signals:  {st['signals']}\n"
                f"  Mentions: {st['mentions']}\n"
                f"  Duration: {st['duration_ms']}ms"
            )
        finally:
            await drv.close()
    _run(_go())


@app.command()
def restore(
    path: Path = typer.Argument(  # noqa: B008
        ..., exists=True, help="Backup JSON file to restore",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Restore graph from a JSON backup file (merge, not overwrite)."""
    from mycelium.core.export import import_subgraph

    data = json.loads(path.read_text())
    meta = data.get("metadata", {})
    st   = data.get("stats", {})
    typer.echo(
        f"Backup from {meta.get('export_date', '?')}\n"
        f"  Neurons:  {st.get('neurons', '?')}\n"
        f"  Synapses: {st.get('synapses', '?')}\n"
        f"  Signals:  {st.get('signals', '?')}"
    )
    if not force:
        if not typer.confirm("Merge this backup into the current graph?"):
            raise typer.Abort()

    async def _go() -> None:
        s   = load_settings()
        drv = Neo4jDriver(s.neo4j)
        await drv.__aenter__()
        emb = make_embedder(s.semantic)
        try:
            res = await import_subgraph(drv, emb, s, data)
            typer.echo(
                f"\nRestored from {path}\n"
                f"  Neurons created:  {res['neurons_created']} "
                f"(skipped {res['neurons_skipped']})\n"
                f"  Synapses created: {res['synapses_created']} "
                f"(skipped {res['synapses_skipped']})\n"
                f"  Signals imported: {res['signals_imported']}\n"
                f"  Mentions created: {res['mentions_created']}\n"
                f"  Re-embedded:      {res['re_embedded']}\n"
                f"  Duration:         {res['duration_ms']}ms"
            )
        finally:
            await drv.close()
    _run(_go())


@app.command()
def migrate() -> None:
    """Run schema migrations and verify database schema."""
    async def _go() -> None:
        s   = load_settings()
        drv = Neo4jDriver(s.neo4j)
        await drv.__aenter__()
        try:
            typer.echo("Running schema migrations…")
            await drv.build_indices()
            ok, missing = await drv.verify_schema()
            if ok:
                typer.echo("Schema OK — all constraints and indexes present.")
            else:
                typer.echo(f"Schema INCOMPLETE — missing: {', '.join(missing)}")
                raise typer.Exit(1)
        finally:
            await drv.close()
    _run(_go())


@app.command()
def compact(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be deleted"),
) -> None:
    """Remove expired (soft-deleted) neurons, synapses, and orphan signals."""
    async def _go() -> None:
        s   = load_settings()
        drv = Neo4jDriver(s.neo4j)
        await drv.__aenter__()
        try:
            # count expired data
            counts = await drv.execute_query(
                "OPTIONAL MATCH (n:Neuron) WHERE n.expired_at IS NOT NULL "
                "WITH count(n) AS neurons "
                "OPTIONAL MATCH ()-[r:SYNAPSE]->() WHERE r.expired_at IS NOT NULL "
                "WITH neurons, count(r) AS synapses "
                "OPTIONAL MATCH (sig:Signal) "
                "  WHERE NOT EXISTS { MATCH (sig)-[:MENTIONS]->() } "
                "    AND sig.status IN ['failed'] "
                "WITH neurons, synapses, count(sig) AS orphan_signals "
                "RETURN neurons, synapses, orphan_signals"
            )
            c = counts[0] if counts else {}
            n_exp = c.get("neurons", 0)
            s_exp = c.get("synapses", 0)
            o_sig = c.get("orphan_signals", 0)

            if n_exp == 0 and s_exp == 0 and o_sig == 0:
                typer.echo("Nothing to compact — no expired data found.")
                return

            typer.echo(
                f"Found:\n"
                f"  Expired neurons:  {n_exp}\n"
                f"  Expired synapses: {s_exp}\n"
                f"  Orphan signals:   {o_sig}"
            )

            if dry_run:
                typer.echo("\nDry run — no changes made.")
                return

            if not force:
                if not typer.confirm("Permanently delete this data?"):
                    raise typer.Abort()

            # delete expired synapses
            res_syn = await drv.execute_query(
                "MATCH ()-[r:SYNAPSE]->() WHERE r.expired_at IS NOT NULL "
                "DELETE r RETURN count(r) AS deleted"
            )
            # delete expired neurons (detach removes remaining rels)
            res_nrn = await drv.execute_query(
                "MATCH (n:Neuron) WHERE n.expired_at IS NOT NULL "
                "DETACH DELETE n RETURN count(n) AS deleted"
            )
            # delete orphan failed signals
            res_sig = await drv.execute_query(
                "MATCH (sig:Signal) "
                "WHERE NOT EXISTS { MATCH (sig)-[:MENTIONS]->() } "
                "  AND sig.status IN ['failed'] "
                "DETACH DELETE sig RETURN count(sig) AS deleted"
            )

            d_syn = res_syn[0]["deleted"] if res_syn else 0
            d_nrn = res_nrn[0]["deleted"] if res_nrn else 0
            d_sig = res_sig[0]["deleted"] if res_sig else 0
            typer.echo(
                f"\nCompacted:\n"
                f"  Neurons deleted:  {d_nrn}\n"
                f"  Synapses deleted: {d_syn}\n"
                f"  Signals deleted:  {d_sig}"
            )
        finally:
            await drv.close()
    _run(_go())


@app.command()
def stats() -> None:
    """Extended database statistics."""
    async def _go() -> None:
        s   = load_settings()
        drv = Neo4jDriver(s.neo4j)
        await drv.__aenter__()
        try:
            # ── Basic counts ──
            basic = await drv.execute_query(
                "OPTIONAL MATCH (n:Neuron) WHERE n.expired_at IS NULL "
                "WITH count(n) AS neurons "
                "OPTIONAL MATCH (ne:Neuron) WHERE ne.expired_at IS NOT NULL "
                "WITH neurons, count(ne) AS expired_neurons "
                "OPTIONAL MATCH ()-[r:SYNAPSE]->() WHERE r.expired_at IS NULL "
                "WITH neurons, expired_neurons, count(r) AS synapses "
                "OPTIONAL MATCH ()-[re:SYNAPSE]->() WHERE re.expired_at IS NOT NULL "
                "WITH neurons, expired_neurons, synapses, count(re) AS expired_synapses "
                "OPTIONAL MATCH (sig:Signal) "
                "WITH neurons, expired_neurons, synapses, expired_synapses, "
                "  count(sig) AS signals "
                "RETURN neurons, expired_neurons, synapses, expired_synapses, signals"
            )
            b = basic[0] if basic else {}

            typer.echo(
                f"═══ MYCELIUM Stats ═══\n\n"
                f"Nodes\n"
                f"  Neurons (active):   {b.get('neurons', 0)}\n"
                f"  Neurons (expired):  {b.get('expired_neurons', 0)}\n"
                f"  Signals:            {b.get('signals', 0)}\n\n"
                f"Edges\n"
                f"  Synapses (active):  {b.get('synapses', 0)}\n"
                f"  Synapses (expired): {b.get('expired_synapses', 0)}"
            )

            # ── Neuron types distribution ──
            types = await drv.execute_query(
                "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
                "RETURN n.neuron_type AS type, count(n) AS cnt "
                "ORDER BY cnt DESC"
            )
            if types:
                typer.echo("\nNeuron types")
                for r in types:
                    typer.echo(f"  {r['type'] or '(none)':20s} {r['cnt']}")

            # ── Signal status distribution ──
            statuses = await drv.execute_query(
                "MATCH (s:Signal) "
                "RETURN s.status AS status, count(s) AS cnt "
                "ORDER BY cnt DESC"
            )
            if statuses:
                typer.echo("\nSignal statuses")
                for r in statuses:
                    typer.echo(f"  {r['status'] or '(none)':20s} {r['cnt']}")

            # ── Confidence distribution ──
            conf = await drv.execute_query(
                "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
                "RETURN "
                "  avg(n.confidence) AS avg_conf, "
                "  min(n.confidence) AS min_conf, "
                "  max(n.confidence) AS max_conf, "
                "  avg(n.confirmations) AS avg_confirms"
            )
            if conf and conf[0].get("avg_conf") is not None:
                c = conf[0]
                typer.echo(
                    f"\nConfidence\n"
                    f"  avg={c['avg_conf']:.3f}  "
                    f"min={c['min_conf']:.3f}  "
                    f"max={c['max_conf']:.3f}\n"
                    f"  avg confirmations: {c['avg_confirms']:.1f}"
                )

            # ── Stale neurons (low effective weight) ──
            stale = await drv.execute_query(
                "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
                "WITH n, n.confidence * exp(-n.decay_rate * "
                "  duration.between(n.freshness, datetime()).days) AS ew "
                "WHERE ew < 0.1 "
                "RETURN count(n) AS stale"
            )
            if stale:
                typer.echo(f"\nStale neurons (ew < 0.1): {stale[0].get('stale', 0)}")

            # ── Schema ──
            ok, missing = await drv.verify_schema()
            if ok:
                typer.echo("\nSchema: OK")
            else:
                typer.echo(f"\nSchema: MISSING {', '.join(missing)}")

            # ── Disk usage ──
            data_dir = Path.home() / ".mycelium" / "neo4j" / "data"
            if data_dir.exists():
                import shutil
                total, used, free = shutil.disk_usage(data_dir)
                dir_size = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file())
                typer.echo(f"\nDisk\n  Neo4j data: {dir_size / 1024 / 1024:.1f} MB")

        finally:
            await drv.close()
    _run(_go())


@app.command()
def dump(
    output: Path = typer.Option(  # noqa: B008
        None, "-o", "--output", help="Output file (default: mycelium_dump_<ts>.dump)",
    ),
) -> None:
    """Neo4j-native binary dump (faster than JSON backup, requires Docker)."""
    import shutil
    import subprocess

    if not shutil.which("docker"):
        typer.echo("Docker not found — use 'mycelium backup' for JSON export.", err=True)
        raise typer.Exit(1)

    ts   = time.strftime("%Y%m%d_%H%M%S")
    dest = output or Path(f"mycelium_dump_{ts}.dump")

    typer.echo("Stopping Neo4j for consistent dump…")
    subprocess.run(["docker", "stop", "mycelium-neo4j"], capture_output=True, timeout=30)

    try:
        typer.echo("Creating dump…")
        result = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{Path.home()}/.mycelium/neo4j/data:/data",
             "neo4j:5.26-community",
             "neo4j-admin", "database", "dump", "neo4j",
             "--to-stdout"],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            typer.echo(f"Dump failed: {result.stderr.decode()}", err=True)
            raise typer.Exit(1)
        dest.write_bytes(result.stdout)
        size_mb = len(result.stdout) / 1024 / 1024
        typer.echo(f"Dump → {dest} ({size_mb:.1f} MB)")
    finally:
        typer.echo("Restarting Neo4j…")
        subprocess.run(["docker", "start", "mycelium-neo4j"], capture_output=True, timeout=30)
        _ensure_neo4j()


@app.command()
def load(
    path: Path = typer.Argument(  # noqa: B008
        ..., exists=True, help="Dump file to load",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Load Neo4j-native binary dump (replaces current database)."""
    import shutil
    import subprocess

    if not shutil.which("docker"):
        typer.echo("Docker not found — use 'mycelium restore' for JSON import.", err=True)
        raise typer.Exit(1)

    if not force:
        if not typer.confirm("This will REPLACE the current database. Continue?"):
            raise typer.Abort()

    typer.echo("Stopping Neo4j…")
    subprocess.run(["docker", "stop", "mycelium-neo4j"], capture_output=True, timeout=30)

    try:
        typer.echo("Loading dump…")
        result = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{Path.home()}/.mycelium/neo4j/data:/data",
             "-v", f"{path.resolve()}:/dump/{path.name}",
             "neo4j:5.26-community",
             "neo4j-admin", "database", "load", "neo4j",
             f"--from-path=/dump",
             "--overwrite-destination=true"],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            typer.echo(f"Load failed: {result.stderr.decode()}", err=True)
            raise typer.Exit(1)
        typer.echo(f"Database restored from {path}")
    finally:
        typer.echo("Restarting Neo4j…")
        subprocess.run(["docker", "start", "mycelium-neo4j"], capture_output=True, timeout=30)
        _ensure_neo4j()


# ── Render (Sigma.js) ─────────────────────────────────────────────

_RENDER_PID = Path.home() / ".mycelium" / "render.pid"


def _render_alive() -> int | None:
    """Return PID if render server is running, else None."""
    import signal

    if not _RENDER_PID.exists():
        return None
    try:
        pid = int(_RENDER_PID.read_text().strip())
        os.kill(pid, 0)  # check alive
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        _RENDER_PID.unlink(missing_ok=True)
        return None


def _render_stop() -> bool:
    """Stop render server if running. Returns True if stopped."""
    import signal

    pid = _render_alive()
    if not pid:
        return False
    os.kill(pid, signal.SIGTERM)
    _RENDER_PID.unlink(missing_ok=True)
    return True


@app.command()
def render(
    host:   str  = typer.Option("", help="Bind address (overrides config)"),
    port:   int  = typer.Option(0, help="Port (overrides config)"),
    stop:   bool = typer.Option(False, "--stop", help="Stop running server"),
    status: bool = typer.Option(False, "--status", help="Show server status"),
) -> None:
    """Sigma.js graph viewer. Starts server (restarts if running)."""
    if status:
        pid = _render_alive()
        if pid:
            s = load_settings()
            typer.echo(f"Render running (PID {pid}) → http://localhost:{s.render.port}")
        else:
            typer.echo("Render not running.")
        return

    if stop:
        if _render_stop():
            typer.echo("Render stopped.")
        else:
            typer.echo("Render not running.")
        return

    s = _init_logging()
    if not s.render.enabled:
        typer.echo(
            "Render is disabled. Set MYCELIUM_RENDER__ENABLED=true in .env"
        )
        raise typer.Exit(1)

    bind_host = host or s.render.host
    bind_port = port or s.render.port

    try:
        import uvicorn
        from mycelium.render.server import app as render_app  # noqa: F811
    except ImportError as exc:
        typer.echo("Render deps missing. Install: uv sync --extra render", err=True)
        raise typer.Exit(1) from exc

    # Restart if already running
    old_pid = _render_alive()
    if old_pid:
        _render_stop()
        typer.echo(f"Stopped previous (PID {old_pid}).")

    # Fork to background
    child = os.fork()
    if child:
        # Parent: write PID, exit
        _RENDER_PID.parent.mkdir(parents=True, exist_ok=True)
        _RENDER_PID.write_text(str(child))
        typer.echo(f"Graph viewer → http://localhost:{bind_port}  (PID {child})")
        return

    # Child: detach and run
    os.setsid()
    import sys
    sys.stdout = sys.stderr = open(os.devnull, "w")
    uvicorn.run(render_app, host=bind_host, port=bind_port, log_level="error")


# ── Obsidian commands ─────────────────────────────────────────────


@app.command(name="obsidian-sync")
def obsidian_sync(
    force: bool = typer.Option(False, "--force", "-f", help="Rewrite all frontmatter"),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="No interactive prompts"),
) -> None:
    """Sync Obsidian frontmatter: recompute relations for all vault .md files."""
    from mycelium.obsidian.sync import sync
    from mycelium.vault.storage import VaultStorage

    global _t0
    _t0 = time.monotonic()
    s   = _init_logging()

    if not s.obsidian.enabled:
        typer.echo("Obsidian layer is disabled. Set obsidian.enabled = true in config.")
        raise typer.Exit(1)

    async def _go() -> None:
        my, drv, _ = await _make(s)
        try:
            vault  = VaultStorage(s.vault)
            result = await sync(drv, vault, s.obsidian)
            parts = [f"{result.updated} updated"]
            if result.companions:
                parts.append(f"{result.companions} companions")
            parts.append(f"{result.skipped} skipped")
            _log("done", ", ".join(parts))

            # Offer to ingest unindexed files
            if result.unindexed:
                typer.echo(f"\n  Unindexed files ({len(result.unindexed)}):")
                for p in result.unindexed:
                    typer.echo(f"    - {p}")
                if not quiet and typer.confirm("\n  Ingest these files?", default=False):
                    for p in result.unindexed:
                        abs_p = vault.root / p
                        try:
                            _log("ingest", p)
                            await my.add_file(abs_p, on_progress=_log)
                        except Exception as exc:
                            typer.echo(f"    ! {p}: {exc}")

            # Offer to re-extract changed files
            if result.hash_changed:
                typer.echo(f"\n  Content changed ({len(result.hash_changed)}):")
                for p in result.hash_changed:
                    typer.echo(f"    - {p}")
                if not quiet and typer.confirm("\n  Re-extract these files?", default=False):
                    for p in result.hash_changed:
                        abs_p = vault.root / p
                        try:
                            _log("re-extract", p)
                            await my.add_file(abs_p, on_progress=_log)
                        except Exception as exc:
                            typer.echo(f"    ! {p}: {exc}")
        finally:
            await drv.close()
    _run(_go())


@app.command(name="obsidian-status")
def obsidian_status() -> None:
    """Show Obsidian layer status."""
    from mycelium.obsidian.sync import status as obs_status
    from mycelium.vault.storage import VaultStorage

    s     = _init_logging()
    vault = VaultStorage(s.vault)

    async def _go() -> None:
        st = await obs_status(vault, s.obsidian)
        typer.echo(f"Obsidian layer: {'enabled' if st['enabled'] else 'disabled'}")
        typer.echo(f"  Vault:            {vault.root}")
        typer.echo(f"  Markdown files:   {st['md_files']}")
        typer.echo(f"  With signal:      {st['with_signal']}")
        typer.echo(f"  With frontmatter: {st['with_fm']}")
        if st.get('companions'):
            typer.echo(f"  Companions:       {st['companions']}")
        if st['unindexed']:
            typer.echo(f"  Unindexed:        {len(st['unindexed'])}")
    _run(_go())


@app.command(name="vault-check")
def vault_check() -> None:
    """Show vault index status."""
    from mycelium.vault.storage import VaultStorage

    s     = _init_logging()
    vault = VaultStorage(s.vault)
    index = vault._load_index()

    total    = len(index)
    linked   = sum(1 for m in index.values() if m.get("signal_uuid"))
    orphaned = sum(1 for p in index if not (vault.root / p).exists())

    typer.echo(f"Vault: {vault.root}")
    typer.echo(f"  Files indexed: {total}")
    typer.echo(f"  With signal:   {linked}")
    if orphaned:
        typer.echo(f"  Orphaned:      {orphaned} (path missing)")


@app.command()
def update() -> None:
    """Pull latest code, sync deps, and verify system."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # 1. fetch + pull main
    typer.echo("Pulling latest code...")
    subprocess.run(["git", "fetch", "origin"], capture_output=True)
    current = subprocess.run(
        ["git", "branch", "--show-current"], capture_output=True, text=True,
    ).stdout.strip()
    if current != "main":
        typer.echo(f"Switching from {current} to main...")
        subprocess.run(["git", "checkout", "main"], capture_output=True)
    r = subprocess.run(["git", "pull", "origin", "main"], capture_output=True, text=True)
    typer.echo(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        typer.echo("git pull failed.", err=True)
        raise typer.Exit(1)

    if "Already up to date" in (r.stdout or ""):
        typer.echo("No updates.")
        raise typer.Exit(0)

    # 2. sync deps (only if lockfile changed)
    typer.echo("Syncing dependencies...")
    subprocess.run(
        [sys.executable, "-m", "uv", "sync", "--extra", "mcp", "--frozen"],
        check=False,
    )

    # 3. verify import
    typer.echo("Verifying...")
    try:
        from mycelium import __version__ as v
        typer.echo(f"MYCELIUM {v} — updated. Restart Claude Code session to apply.")
    except Exception as e:
        typer.echo(f"Import check failed: {e}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
