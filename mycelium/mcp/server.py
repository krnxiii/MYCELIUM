"""MYCELIUM v2 MCP server via FastMCP (stdio + HTTP)."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from fastmcp import FastMCP

from mycelium.config import Settings, load_settings
from mycelium.core.models import SignalType
from mycelium.core.mycelium import Mycelium
from mycelium.core.types import MyceliumClients
from mycelium.driver.neo4j_driver import Neo4jDriver
from mycelium.embedder.client import make_embedder
from mycelium.llm import make_llm_client
from mycelium.core.community import detect_communities as run_community_detection
from mycelium.core.export import export_subgraph as run_export, import_subgraph as run_import
from mycelium.core.skills import list_skills, save_skill
from mycelium.domain import (
    load_all as load_domains, load_by_name as load_domain,
    save as save_domain_bp, delete as delete_domain_bp,
    DomainBlueprint, ExtractionConfig, FieldConfig, TrackingConfig,
    match_domain,
)
from mycelium.domain.registry import to_compact_list
from mycelium.domain.tracking import (
    template_parse, write_metric_file, read_metric_files,
    generate_dashboard, compute_stats,
)
from mycelium.core.sleep import build_sleep_report
from mycelium.core.telemetry import Telemetry
from mycelium.utils.decay import calc_decay_rate, consolidate, effective_weight

log = structlog.get_logger()
mcp = FastMCP("mycelium")

# ── File-flag gate ────────────────────────────────────────────────

_GATE_DIR      = pathlib.Path.home() / ".mycelium"
_KNOWLEDGE_DIR = pathlib.Path(__file__).resolve().parent.parent / "knowledge"

_GATE_DIR.mkdir(parents=True, exist_ok=True)
(_GATE_DIR / ".read_enabled").touch(exist_ok=True)
# VPS/Docker: auto-enable write (trusted single-user environment)
if os.environ.get("MYCELIUM_MCP__TRANSPORT") == "streamable-http":
    (_GATE_DIR / ".write_enabled").touch(exist_ok=True)


def _gate(mode: str) -> dict | None:
    """Return error dict if mode flag missing, None if ok."""
    if (_GATE_DIR / f".{mode}_enabled").exists():
        return None
    return {"error": f"{mode.title()} access is disabled. Run /mycelium-on to enable."}


# ── Lazy singleton ────────────────────────────────────────────────

_my:       Mycelium | None = None
_settings: Settings | None = None

# ── R6.2: Async task registry ────────────────────────────────────

_bg_tasks: dict[str, asyncio.Task] = {}   # signal_uuid → Task
_bg_sem = asyncio.Semaphore(2)             # max concurrent extractions


async def _get() -> tuple[Mycelium, Settings]:
    """Lazy-init Mycelium orchestrator."""
    global _my, _settings

    if _my is not None and _settings is not None:
        return _my, _settings

    _settings = load_settings()
    driver    = Neo4jDriver(_settings.neo4j)
    await driver.__aenter__()

    clients = MyceliumClients(
        driver   = driver,
        embedder = make_embedder(_settings.semantic),
        llm      = make_llm_client(_settings.llm),
    )
    _my = Mycelium(clients, _settings)
    # R6.2: mark zombie "extracting" signals as failed on restart
    await driver.execute_query(
        "MATCH (s:Signal) WHERE s.status = 'extracting' "
        "SET s.status = 'failed'",
    )
    log.info("mcp_initialized")
    return _my, _settings


# ── Tool logic (plain async, testable) ────────────────────────────


async def impl_add_signal(
    content: str, name: str = "",
    source_type: str = "text", source_desc: str = "",
    extraction_focus: str = "",
    async_mode: bool = False,
) -> dict[str, Any]:
    my, _ = await _get()

    # ── R6.2: async mode — return immediately, extract in background
    if async_mode:
        return await _start_bg_extraction(
            my, content, name, source_type, source_desc, extraction_focus,
        )

    # ── Sync mode (default) — block until done
    t0 = time.monotonic()
    sig, neurons, synapses, questions = await my.add_episode(
        content, name=name,
        source_type=SignalType(source_type),
        source_desc=source_desc,
        extraction_focus=extraction_focus,
    )
    ms = int((time.monotonic() - t0) * 1000)
    log.info("mcp_tool_called", tool="add_signal", duration_ms=ms)
    resp: dict[str, Any] = {
        "signal_uuid": sig.uuid, "status": sig.status.value,
        "neurons": [
            {"uuid": n.uuid, "name": n.name, "type": n.neuron_type}
            for n in neurons
        ],
        "synapses": [
            {"uuid": s.uuid, "fact": s.fact, "relation": s.relation}
            for s in synapses
        ],
        "duration_ms": ms,
    }
    if questions:
        resp["questions"] = [
            {"text": q.text, "category": q.category, "context": q.context}
            for q in questions
        ]
    return resp


async def _start_bg_extraction(
    my: Mycelium, content: str, name: str,
    source_type: str, source_desc: str, extraction_focus: str,
) -> dict[str, Any]:
    """Save signal as 'extracting', spawn background task, return uuid."""
    from mycelium.core.models import Signal, SignalStatus
    sig = Signal(
        name        = name or content[:60],
        content     = content,
        source_type = SignalType(source_type),
        source_desc = source_desc,
        status      = SignalStatus.extracting,
    )
    await my._c.driver.execute_query(
        "CREATE (e:Signal {"
        "  uuid: $uuid, name: $name, content: $content,"
        "  source_type: $stype, source_desc: $sdesc,"
        "  status: $status, created_at: datetime($created)"
        "})",
        {
            "uuid":    sig.uuid,
            "name":    sig.name,
            "content": sig.content,
            "stype":   sig.source_type.value,
            "sdesc":   sig.source_desc,
            "status":  sig.status.value,
            "created": sig.created_at.isoformat(),
        },
    )

    async def _run() -> None:
        async with _bg_sem:
            try:
                await my.add_episode(
                    content, name=name,
                    source_type=SignalType(source_type),
                    source_desc=source_desc,
                    extraction_focus=extraction_focus,
                )
            except Exception as e:
                log.warning("bg_extraction_failed", signal=sig.uuid, error=str(e))
                await my._c.driver.execute_query(
                    "MATCH (s:Signal {uuid: $uuid}) SET s.status = 'failed'",
                    {"uuid": sig.uuid},
                )
            finally:
                _bg_tasks.pop(sig.uuid, None)

    _bg_tasks[sig.uuid] = asyncio.create_task(_run())
    return {"signal_uuid": sig.uuid, "status": "extracting"}


async def impl_ingest_direct(
    content: str, neurons: str, synapses: str,
    name: str = "", source_type: str = "text", source_desc: str = "",
) -> dict[str, Any]:
    t0     = time.monotonic()
    my, _  = await _get()

    nrn_list = json.loads(neurons)
    syn_list = json.loads(synapses)
    if not isinstance(nrn_list, list):
        raise ValueError(f"neurons must be a JSON array, got {type(nrn_list).__name__}")
    if not isinstance(syn_list, list):
        raise ValueError(f"synapses must be a JSON array, got {type(syn_list).__name__}")

    sig, saved_neurons, saved_synapses = await my.ingest_direct(
        content,
        nrn_list,
        syn_list,
        name=name,
        source_type=SignalType(source_type),
        source_desc=source_desc,
    )
    ms = int((time.monotonic() - t0) * 1000)
    log.info("mcp_tool_called", tool="ingest_direct", duration_ms=ms)
    return {
        "signal_uuid": sig.uuid, "status": sig.status.value,
        "neurons": [
            {"uuid": n.uuid, "name": n.name, "type": n.neuron_type}
            for n in saved_neurons
        ],
        "synapses": [
            {"uuid": s.uuid, "fact": s.fact, "relation": s.relation}
            for s in saved_synapses
        ],
        "duration_ms": ms,
    }


async def impl_ingest_batch(items_json: str) -> dict[str, Any]:
    t0     = time.monotonic()
    my, _  = await _get()
    items  = json.loads(items_json)
    if not isinstance(items, list):
        raise ValueError(f"items must be a JSON array, got {type(items).__name__}")

    results = await my.ingest_batch(items)
    ms = int((time.monotonic() - t0) * 1000)
    log.info("mcp_tool_called", tool="ingest_batch", duration_ms=ms)
    return {
        "items": [
            {
                "signal_uuid": sig.uuid,
                "neurons": [
                    {"uuid": n.uuid, "name": n.name, "type": n.neuron_type}
                    for n in neurons
                ],
                "synapses": [
                    {"uuid": s.uuid, "fact": s.fact, "relation": s.relation}
                    for s in synapses
                ],
            }
            for sig, neurons, synapses in results
        ],
        "total_items": len(results),
        "duration_ms": ms,
    }


async def impl_add_neuron(
    name: str, neuron_type: str,
    confidence: float = 1.0, summary: str = "",
    attributes: str = "{}",
) -> dict[str, Any]:
    t0         = time.monotonic()
    my, sett   = await _get()
    drv, emb   = my._c.driver, my._c.embedder
    confidence = min(1.0, max(0.0, confidence))
    now        = datetime.now(UTC)

    # Embed name
    vec = await emb.embed(name)

    # 1. Exact dedup
    norm = name.strip().lower()
    rows = await drv.execute_query(
        "MATCH (e:Neuron) "
        "WHERE toLower(trim(e.name)) = $norm "
        "  AND e.expired_at IS NULL "
        "RETURN e.uuid AS uuid, e.name AS name, "
        "  e.confidence AS confidence, e.decay_rate AS decay_rate, "
        "  e.confirmations AS confirmations",
        {"norm": norm},
    )
    match = rows[0] if rows else None

    # 2. Vector fallback
    if not match and vec:
        try:
            vrows = await drv.execute_query(
                "CALL db.index.vector.queryNodes('neuron_name_emb', 1, $vec) "
                "YIELD node AS e, score "
                "WHERE score >= $thr AND e.expired_at IS NULL "
                "RETURN e.uuid AS uuid, e.name AS name, "
                "  e.confidence AS confidence, e.decay_rate AS decay_rate, "
                "  e.confirmations AS confirmations",
                {"vec": vec, "thr": sett.dedup.cosine_threshold},
            )
            match = vrows[0] if vrows else None
        except Exception as exc:
            log.warning("vector_dedup_skip", error=str(exc))

    # 3. Consolidate existing
    if match:
        new_conf, new_rate, new_count = consolidate(
            match.get("confidence", 1.0),
            match.get("confirmations", 0),
            sett.decay,
        )
        await drv.execute_query(
            "MATCH (e:Neuron {uuid: $uuid}) "
            "SET e.importance    = $conf, "
            "    e.confidence    = $conf, "
            "    e.decay_rate    = $rate, "
            "    e.confirmations = $count, "
            "    e.freshness     = datetime($now)",
            {"uuid": match["uuid"], "conf": new_conf,
             "rate": new_rate, "count": new_count,
             "now": now.isoformat()},
        )
        ms = int((time.monotonic() - t0) * 1000)
        log.info("mcp_tool_called", tool="add_neuron",
                 status="merged", duration_ms=ms)
        return {
            "status": "merged", "uuid": match["uuid"],
            "name": match["name"],
            "confidence": new_conf, "confirmations": new_count,
            "duration_ms": ms,
        }

    # 4. Create new neuron
    nuuid = str(uuid4())
    rate  = calc_decay_rate(0, sett.decay)
    attrs = attributes if attributes else "{}"
    if summary:
        try:
            a = json.loads(attrs)
            a["summary"] = summary
            attrs = json.dumps(a)
        except json.JSONDecodeError:
            attrs = json.dumps({"summary": summary})

    # Parse expires_at from attributes if provided
    exp_at = None
    try:
        a = json.loads(attrs)
        if "expires_at" in a:
            exp_at = a.pop("expires_at")
            attrs = json.dumps(a)
    except (json.JSONDecodeError, TypeError):
        pass

    exp_clause = ", n.expires_at = datetime($exp)" if exp_at else ""
    params: dict[str, Any] = {
        "uuid": nuuid, "name": name, "type": neuron_type,
        "emb": vec, "summary": summary, "conf": confidence,
        "rate": rate, "now": now.isoformat(), "attrs": attrs,
    }
    if exp_at:
        params["exp"] = exp_at

    await drv.execute_query(
        "MERGE (n:Neuron {uuid: $uuid}) "
        "SET n.name           = $name, "
        "    n.neuron_type    = $type, "
        "    n.name_embedding = $emb, "
        "    n.summary        = $summary, "
        "    n.importance     = $conf, "
        "    n.confidence     = $conf, "
        "    n.decay_rate     = $rate, "
        "    n.confirmations  = 0, "
        "    n.freshness      = datetime($now), "
        "    n.attributes     = $attrs, "
        "    n.created_at     = datetime($now)"
        + exp_clause,
        params,
    )
    ms = int((time.monotonic() - t0) * 1000)
    log.info("mcp_tool_called", tool="add_neuron",
             status="created", duration_ms=ms)
    return {
        "status": "created", "uuid": nuuid, "name": name,
        "neuron_type": neuron_type, "confidence": confidence,
        "duration_ms": ms,
    }


async def impl_search(
    query: str, top_k: int = 10, center_uuid: str = "",
) -> dict[str, Any]:
    t0    = time.monotonic()
    my, _ = await _get()
    res   = await my.search(query, top_k=top_k, center_uuid=center_uuid or None)
    ms    = int((time.monotonic() - t0) * 1000)
    log.info("mcp_tool_called", tool="search", duration_ms=ms)
    return {
        "neurons": [
            {"uuid": sn.neuron.uuid, "name": sn.neuron.name,
             "type": sn.neuron.neuron_type, "score": round(sn.score, 4)}
            for sn in res.neurons],
        "synapses": [
            {"uuid": ss.synapse.uuid, "fact": ss.synapse.fact,
             "source": ss.source_name, "target": ss.target_name,
             "score": round(ss.score, 4)}
            for ss in res.synapses],
        "signals":     [{"uuid": s.uuid, "name": s.name} for s in res.signals],
        "methods":     [m.value for m in res.methods],
        "duration_ms": ms,
    }


async def impl_get_neuron(uuid: str) -> dict[str, Any]:
    my, _ = await _get()
    rows  = await my._c.driver.execute_query(
        "MATCH (e:Neuron {uuid: $uuid}) "
        "OPTIONAL MATCH (e)-[f:SYNAPSE]->(t:Neuron) WHERE f.expired_at IS NULL "
        "OPTIONAL MATCH (e)<-[fi:SYNAPSE]-(s:Neuron) WHERE fi.expired_at IS NULL "
        "OPTIONAL MATCH (sig:Signal)-[:MENTIONS]->(e) "
        "RETURN e, "
        "  collect(DISTINCT {uuid: f.uuid, fact: f.fact, relation: f.relation, "
        "    target: t.name, confidence: f.confidence}) AS out_synapses, "
        "  collect(DISTINCT {uuid: fi.uuid, fact: fi.fact, relation: fi.relation, "
        "    source: s.name, confidence: fi.confidence}) AS in_synapses, "
        "  collect(DISTINCT {uuid: sig.uuid, name: sig.name, "
        "    source_desc: sig.source_desc}) AS signals",
        {"uuid": uuid},
    )
    if not rows:
        return {"error": f"Neuron {uuid} not found"}

    r         = rows[0]
    e         = dict(r["e"])
    freshness = e.get("freshness")
    if hasattr(freshness, "to_native"):
        freshness = freshness.to_native()
    ew = effective_weight(
        e.get("confidence", 1.0), e.get("decay_rate", 0.008),
        freshness or datetime.now(UTC),
    )
    return {
        "uuid": e.get("uuid"), "name": e.get("name"),
        "neuron_type": e.get("neuron_type"), "summary": e.get("summary", ""),
        "confidence": e.get("confidence"), "decay_rate": e.get("decay_rate"),
        "confirmations": e.get("confirmations"),
        "freshness":  str(e.get("freshness", "")),
        "expires_at":  str(e.get("expires_at",  "")) or None,
        "expired_at":  str(e.get("expired_at",  "")) or None,
        "weight": round(ew, 4), "attributes": e.get("attributes", "{}"),
        "out_synapses": [f for f in r["out_synapses"] if f.get("uuid")],
        "in_synapses":  [f for f in r["in_synapses"] if f.get("uuid")],
        "signals":      [s for s in r["signals"] if s.get("uuid")],
    }


async def impl_list_neurons(
    neuron_type: str = "", sort_by: str = "freshness", limit: int = 20,
) -> list[dict[str, Any]]:
    my, _ = await _get()
    base  = ("WHERE e.expired_at IS NULL "
             "AND (e.expires_at IS NULL OR e.expires_at > datetime())")
    where = f"{base} AND e.neuron_type = $type" if neuron_type else base
    order = {
        "freshness": "e.freshness DESC", "confidence": "e.importance DESC",
        "name": "e.name ASC", "weight": "ew DESC",
    }.get(sort_by, "e.freshness DESC")

    return await my._c.driver.execute_query(
        f"MATCH (e:Neuron) {where} "
        "WITH e, coalesce(e.importance, e.confidence) AS imp, e.decay_rate AS dr, "
        "  duration.between(e.freshness, datetime()).days AS days "
        "WITH e, imp * exp(-dr * days) AS ew, imp "
        f"RETURN e.uuid AS uuid, e.name AS name, e.neuron_type AS type, "
        "  imp AS importance, e.confirmations AS confirmations, "
        "  coalesce(e.origin, 'raw') AS origin, "
        f"  round(ew * 10000) / 10000 AS weight ORDER BY {order} LIMIT $limit",
        {"type": neuron_type, "limit": limit},
    )


async def impl_add_synapse(
    source_uuid: str, target_uuid: str, fact: str,
    relation: str = "RELATES_TO", confidence: float = 1.0,
) -> dict[str, Any]:
    my, _ = await _get()
    suuid = str(uuid4())
    vec   = await my._c.embedder.embed(fact)
    now   = datetime.now(UTC).isoformat()

    await my._c.driver.execute_query(
        "MATCH (s:Neuron {uuid: $src}), (t:Neuron {uuid: $tgt}) "
        "CREATE (s)-[:SYNAPSE {"
        "  uuid: $uuid, fact: $fact, fact_embedding: $emb,"
        "  relation: $rel, episodes: [], confidence: $conf,"
        "  created_at: datetime($now)}]->(t)",
        {"src": source_uuid, "tgt": target_uuid,
         "uuid": suuid, "fact": fact, "emb": vec,
         "rel": relation, "conf": confidence, "now": now},
    )
    return {"status": "created", "synapse_uuid": suuid}


async def impl_delete_synapse(uuid: str) -> dict[str, Any]:
    my, _ = await _get()
    rows  = await my._c.driver.execute_query(
        "MATCH ()-[f:SYNAPSE {uuid: $uuid}]->() "
        "SET f.expired_at = datetime() "
        "RETURN f.uuid AS uuid, f.fact AS fact",
        {"uuid": uuid},
    )
    if not rows:
        return {"error": f"Synapse {uuid} not found"}
    return {"status": "expired", "uuid": rows[0]["uuid"], "fact": rows[0]["fact"]}


async def impl_update_neuron(
    uuid: str, name: str = "", neuron_type: str = "",
    confidence: float = -1, importance: float = -1,
) -> dict[str, Any]:
    my, _ = await _get()
    sets: list[str]        = []
    params: dict[str, Any] = {"uuid": uuid}

    if name:
        sets.append("e.name = $name")
        params["name"] = name
    if neuron_type:
        sets.append("e.neuron_type = $type")
        params["type"] = neuron_type
    imp = importance if importance >= 0 else confidence
    if imp >= 0:
        clamped = min(1.0, max(0.0, imp))
        sets.append("e.importance = $imp")
        sets.append("e.confidence = $imp")
        params["imp"] = clamped

    if not sets:
        return {"error": "No fields to update. Provide at least one of: name, neuron_type, importance."}

    rows = await my._c.driver.execute_query(
        f"MATCH (e:Neuron {{uuid: $uuid}}) SET {', '.join(sets)} "
        "RETURN e.uuid AS uuid, e.name AS name", params,
    )
    if not rows:
        return {"error": f"Neuron {uuid} not found"}
    return {"status": "updated", **rows[0]}


async def impl_get_signals(status: str = "", limit: int = 20) -> list[dict[str, Any]]:
    my, _ = await _get()
    where = "WHERE e.status = $status" if status else ""
    return await my._c.driver.execute_query(
        f"MATCH (e:Signal) {where} "
        "RETURN e.uuid AS uuid, e.name AS name, e.status AS status, "
        "  e.source_type AS source_type, e.source_desc AS source_desc, "
        "  toString(e.created_at) AS created_at "
        "ORDER BY e.created_at DESC LIMIT $limit",
        {"status": status, "limit": limit},
    )


async def impl_re_extract(signal_uuid: str) -> dict[str, Any]:
    t0    = time.monotonic()
    my, _ = await _get()
    sig, neurons, synapses, questions = await my.re_extract(signal_uuid)
    ms = int((time.monotonic() - t0) * 1000)
    resp: dict[str, Any] = {
        "signal_uuid": sig.uuid, "status": sig.status.value,
        "neurons": len(neurons), "synapses": len(synapses), "duration_ms": ms,
    }
    if questions:
        resp["questions"] = [
            {"text": q.text, "category": q.category, "context": q.context}
            for q in questions
        ]
    return resp


async def impl_get_timeline(neuron_uuid: str, limit: int = 20) -> dict[str, Any]:
    my, _ = await _get()
    rows  = await my._c.driver.execute_query(
        "MATCH (e:Neuron {uuid: $uuid}) "
        "OPTIONAL MATCH (e)-[f:SYNAPSE]-(other:Neuron) "
        "RETURN e.name AS neuron_name, "
        "  collect({uuid: f.uuid, fact: f.fact, relation: f.relation, "
        "    other: other.name, valid_at: toString(f.valid_at), "
        "    created_at: toString(f.created_at), "
        "    expired_at: toString(f.expired_at)}) AS timeline "
        "LIMIT 1",
        {"uuid": neuron_uuid},
    )
    if not rows:
        return {"error": f"Neuron {neuron_uuid} not found"}

    r        = rows[0]
    synapses = sorted(
        [f for f in r["timeline"] if f.get("uuid")],
        key=lambda f: f.get("created_at") or "",
    )
    return {"neuron": r["neuron_name"], "synapses": synapses[:limit],
            "total": len(synapses)}


async def impl_health(verbose: bool = False) -> dict[str, Any]:
    my, _ = await _get()
    drv   = my._c.driver

    try:
        counts = await drv.execute_query(
            "OPTIONAL MATCH (n:Neuron) WHERE n.expired_at IS NULL "
            "  WITH count(n) AS neurons "
            "OPTIONAL MATCH (s:Signal) WITH neurons, count(s) AS signals "
            "OPTIONAL MATCH ()-[f:SYNAPSE]->() "
            "WITH neurons, signals, count(f) AS synapses "
            "OPTIONAL MATCH ()-[f2:SYNAPSE]->() WHERE f2.expired_at IS NOT NULL "
            "  WITH neurons, signals, synapses, count(f2) AS expired "
            "RETURN neurons, signals, synapses, expired"
        )
        c = counts[0] if counts else {}

        stale = await drv.execute_query(
            "MATCH (e:Neuron) WHERE e.expired_at IS NULL "
            "  AND (e.expires_at IS NULL OR e.expires_at > datetime()) "
            "WITH e, coalesce(e.importance, e.confidence) * exp(-e.decay_rate * "
            "  duration.between(e.freshness, datetime()).days) AS ew "
            "WHERE ew < 0.1 "
            "RETURN e.name AS name, round(ew * 10000) / 10000 AS weight "
            "ORDER BY ew ASC LIMIT 10"
        )
        neo_ok = await drv.health_check()
    except Exception as exc:
        log.warning("health_neo4j_unavailable", error=str(exc))
        result: dict[str, Any] = {
            "neo4j": "unavailable",
            "error": str(exc),
            "neurons": 0, "signals": 0,
            "active_synapses": 0, "expired_synapses": 0,
            "stale": [],
        }
        if verbose:
            result["telemetry"] = Telemetry().snapshot()
        return result

    result: dict[str, Any] = {
        "neo4j":            "ok" if neo_ok else "unreachable",
        "neurons":          c.get("neurons", 0),
        "signals":          c.get("signals", 0),
        "active_synapses":  c.get("synapses", 0) - c.get("expired", 0),
        "expired_synapses": c.get("expired", 0),
        "stale":            stale,
    }
    if verbose:
        result["telemetry"] = Telemetry().snapshot()
    return result


async def impl_set_owner(name: str) -> dict[str, Any]:
    my, _ = await _get()
    return await my.set_owner(name)


async def impl_get_owner() -> dict[str, Any]:
    my, _ = await _get()
    await my.init_owner()
    return {"owner": my.owner_name or None}


async def impl_rethink_neuron(uuid: str) -> dict[str, Any]:
    """Holistic neuron rewrite via LLM with full context."""
    t0    = time.monotonic()
    my, _ = await _get()
    drv   = my._c.driver

    # 1. Get neuron + all synapses
    rows = await drv.execute_query(
        "MATCH (e:Neuron {uuid: $uuid}) "
        "WHERE e.expired_at IS NULL "
        "OPTIONAL MATCH (e)-[f:SYNAPSE]-(other:Neuron) "
        "  WHERE f.expired_at IS NULL "
        "RETURN e.uuid AS uuid, e.name AS name, "
        "  e.neuron_type AS neuron_type, e.summary AS summary, "
        "  e.confidence AS confidence, e.attributes AS attributes, "
        "  e.confirmations AS confirmations, "
        "  collect(DISTINCT {fact: f.fact, relation: f.relation, "
        "    other: other.name, direction: CASE WHEN startNode(f) = e "
        "    THEN 'out' ELSE 'in' END, "
        "    valid_at: toString(f.valid_at)}) AS synapses",
        {"uuid": uuid},
    )
    if not rows:
        return {"error": f"Neuron {uuid} not found"}

    r        = rows[0]
    synapses = [s for s in r["synapses"] if s.get("fact")]

    # 2. Build context for LLM
    syn_text = "\n".join(
        f"  {'→' if s['direction'] == 'out' else '←'} "
        f"{s['other']} [{s['relation']}]: {s['fact']}"
        for s in synapses
    )
    prompt = (
        "You are MYCELIUM — a knowledge graph refinement engine.\n\n"
        f"## Neuron to rethink\n"
        f"Name: {r['name']}\n"
        f"Type: {r['neuron_type']}\n"
        f"Summary: {r['summary'] or '(none)'}\n"
        f"Attributes: {r['attributes'] or '{}'}\n"
        f"Confirmations: {r['confirmations']}\n\n"
        f"## All synapses ({len(synapses)} total)\n{syn_text}\n\n"
        "## Task\n"
        "Based on ALL accumulated knowledge above, rewrite this neuron.\n"
        "Produce a holistic, up-to-date representation.\n\n"
        "Respond with ONLY a valid JSON object:\n"
        '{"summary": "2-4 sentence comprehensive summary",'
        ' "neuron_type": "best fitting type",'
        ' "attributes": {"key": "value"}}\n\n'
        "Rules:\n"
        "- summary must synthesize ALL synapses, not just repeat the name\n"
        "- neuron_type: pick the most accurate from ontology\n"
        "- attributes: structured details (dates, metrics, lists)\n"
        "- Preserve existing valuable attributes, add new ones\n"
        "- If name seems wrong, add suggested_name in attributes"
    )

    # 3. LLM call
    try:
        data = await my._c.llm.generate(prompt)
    except Exception as e:
        return {"error": f"LLM failed: {e}"}

    new_summary = data.get("summary", "")
    new_type    = data.get("neuron_type", r["neuron_type"])
    new_attrs   = data.get("attributes", {})

    # 4. Merge attributes (preserve existing, add new)
    old_attrs = {}
    if r["attributes"]:
        try:
            old_attrs = (json.loads(r["attributes"])
                         if isinstance(r["attributes"], str)
                         else r["attributes"])
        except (ValueError, TypeError):
            pass
    merged_attrs = {**old_attrs, **new_attrs}

    # 5. Save
    await drv.execute_query(
        "MATCH (e:Neuron {uuid: $uuid}) "
        "SET e.summary     = $summary, "
        "    e.neuron_type = $type, "
        "    e.attributes  = $attrs",
        {
            "uuid":    uuid,
            "summary": new_summary,
            "type":    new_type,
            "attrs":   json.dumps(merged_attrs),
        },
    )

    # 6. Re-embed summary
    if new_summary:
        vec = await my._c.embedder.embed(new_summary)
        await drv.execute_query(
            "MATCH (e:Neuron {uuid: $uuid}) "
            "SET e.summary_embedding = $vec",
            {"uuid": uuid, "vec": vec},
        )

    ms = int((time.monotonic() - t0) * 1000)
    log.info("mcp_tool_called", tool="rethink_neuron", duration_ms=ms)
    return {
        "status": "rethought", "uuid": uuid, "name": r["name"],
        "old_type": r["neuron_type"], "new_type": new_type,
        "summary": new_summary, "attributes": merged_attrs,
        "synapses_analyzed": len(synapses), "duration_ms": ms,
    }


async def impl_delete_neuron(uuid: str) -> dict[str, Any]:
    my, _ = await _get()
    drv   = my._c.driver

    # Verify neuron exists
    rows = await drv.execute_query(
        "MATCH (e:Neuron {uuid: $uuid}) "
        "WHERE e.expired_at IS NULL "
        "RETURN e.uuid AS uuid, e.name AS name",
        {"uuid": uuid},
    )
    if not rows:
        return {"error": f"Neuron {uuid} not found"}

    # Expire neuron
    await drv.execute_query(
        "MATCH (e:Neuron {uuid: $uuid}) "
        "SET e.expired_at = datetime()",
        {"uuid": uuid},
    )

    # Expire all SYNAPSE (both directions)
    res = await drv.execute_query(
        "MATCH (e:Neuron {uuid: $uuid})-[f:SYNAPSE]-() "
        "WHERE f.expired_at IS NULL "
        "SET f.expired_at = datetime() "
        "RETURN count(f) AS cnt",
        {"uuid": uuid},
    )
    cnt = res[0]["cnt"] if res else 0

    log.info("neuron_deleted", uuid=uuid, name=rows[0]["name"],
             synapses_expired=cnt)
    return {"status": "expired", "uuid": uuid, "name": rows[0]["name"],
            "synapses_expired": cnt}


async def impl_merge_neurons(
    primary_uuid: str, secondary_uuid: str,
) -> dict[str, Any]:
    my, sett = await _get()
    drv      = my._c.driver

    # 1. Verify both exist
    rows = await drv.execute_query(
        "MATCH (p:Neuron {uuid: $p}), (s:Neuron {uuid: $s}) "
        "RETURN p.uuid AS p_uuid, p.name AS p_name, "
        "  p.confidence AS p_conf, p.confirmations AS p_cnt, "
        "  s.uuid AS s_uuid, s.name AS s_name, "
        "  s.confidence AS s_conf, s.confirmations AS s_cnt, "
        "  s.attributes AS s_attrs",
        {"p": primary_uuid, "s": secondary_uuid},
    )
    if not rows:
        return {"error": "One or both neurons not found"}
    r = rows[0]

    # 2. Rewire outgoing SYNAPSE: secondary→X → primary→X
    out = await drv.execute_query(
        "MATCH (s:Neuron {uuid: $s})-[f:SYNAPSE]->(t:Neuron) "
        "WHERE NOT EXISTS { "
        "  MATCH (p:Neuron {uuid: $p})-[ef:SYNAPSE]->(t) "
        "  WHERE ef.fact = f.fact AND ef.expired_at IS NULL "
        "} "
        "WITH f, t "
        "MATCH (p:Neuron {uuid: $p}) "
        "CREATE (p)-[nf:SYNAPSE {"
        "  uuid: f.uuid, fact: f.fact, fact_embedding: f.fact_embedding, "
        "  relation: f.relation, episodes: f.episodes, "
        "  confidence: f.confidence, valid_at: f.valid_at, "
        "  invalid_at: f.invalid_at, created_at: f.created_at"
        "}]->(t) "
        "DELETE f "
        "RETURN count(nf) AS cnt",
        {"s": secondary_uuid, "p": primary_uuid},
    )
    rewired = (out[0]["cnt"] if out else 0)

    # 3. Rewire incoming SYNAPSE: X→secondary → X→primary
    inc = await drv.execute_query(
        "MATCH (src:Neuron)-[f:SYNAPSE]->(s:Neuron {uuid: $s}) "
        "WHERE NOT EXISTS { "
        "  MATCH (src)-[ef:SYNAPSE]->(p:Neuron {uuid: $p}) "
        "  WHERE ef.fact = f.fact AND ef.expired_at IS NULL "
        "} "
        "WITH f, src "
        "MATCH (p:Neuron {uuid: $p}) "
        "CREATE (src)-[nf:SYNAPSE {"
        "  uuid: f.uuid, fact: f.fact, fact_embedding: f.fact_embedding, "
        "  relation: f.relation, episodes: f.episodes, "
        "  confidence: f.confidence, valid_at: f.valid_at, "
        "  invalid_at: f.invalid_at, created_at: f.created_at"
        "}]->(p) "
        "DELETE f "
        "RETURN count(nf) AS cnt",
        {"s": secondary_uuid, "p": primary_uuid},
    )
    rewired += (inc[0]["cnt"] if inc else 0)

    # 4. Redirect MENTIONS: Signal→secondary → Signal→primary
    mentions = await drv.execute_query(
        "MATCH (sig:Signal)-[m:MENTIONS]->(s:Neuron {uuid: $s}) "
        "WHERE NOT EXISTS { MATCH (sig)-[:MENTIONS]->(p:Neuron {uuid: $p}) } "
        "WITH m, sig "
        "MATCH (p:Neuron {uuid: $p}) "
        "CREATE (sig)-[:MENTIONS {uuid: m.uuid, created_at: m.created_at}]->(p) "
        "DELETE m "
        "RETURN count(*) AS cnt",
        {"s": secondary_uuid, "p": primary_uuid},
    )
    redirected = (mentions[0]["cnt"] if mentions else 0)

    # 5. Consolidate primary
    new_conf, new_rate, new_count = consolidate(
        r["p_conf"] or 1.0, (r["p_cnt"] or 0) + (r["s_cnt"] or 0), sett.decay,
    )
    await drv.execute_query(
        "MATCH (p:Neuron {uuid: $uuid}) "
        "SET p.confidence    = $conf, "
        "    p.decay_rate    = $rate, "
        "    p.confirmations = $count, "
        "    p.freshness     = datetime()",
        {"uuid": primary_uuid, "conf": new_conf,
         "rate": new_rate, "count": new_count},
    )

    # 6. DETACH DELETE secondary (remaining edges are duplicates)
    await drv.execute_query(
        "MATCH (s:Neuron {uuid: $uuid}) DETACH DELETE s",
        {"uuid": secondary_uuid},
    )

    log.info("neurons_merged",
             primary=r["p_name"], secondary=r["s_name"],
             rewired=rewired, redirected=redirected)
    return {
        "status": "merged", "primary_uuid": primary_uuid,
        "primary_name": r["p_name"], "secondary_name": r["s_name"],
        "rewired_synapses": rewired, "redirected_mentions": redirected,
    }


async def impl_add_mention(
    signal_uuid: str, neuron_uuid: str,
) -> dict[str, Any]:
    my, _ = await _get()
    drv   = my._c.driver

    # Verify both exist
    rows = await drv.execute_query(
        "MATCH (sig:Signal {uuid: $sig}), (nrn:Neuron {uuid: $nrn}) "
        "RETURN sig.uuid AS sig_uuid, nrn.uuid AS nrn_uuid",
        {"sig": signal_uuid, "nrn": neuron_uuid},
    )
    if not rows:
        return {"error": "Signal or Neuron not found"}

    # Check existing (idempotent)
    existing = await drv.execute_query(
        "MATCH (sig:Signal {uuid: $sig})-[m:MENTIONS]->(nrn:Neuron {uuid: $nrn}) "
        "RETURN m.uuid AS uuid",
        {"sig": signal_uuid, "nrn": neuron_uuid},
    )
    if existing:
        return {"status": "exists", "mention_uuid": existing[0]["uuid"]}

    # Create
    muuid = str(uuid4())
    now   = datetime.now(UTC).isoformat()
    await drv.execute_query(
        "MATCH (sig:Signal {uuid: $sig}), (nrn:Neuron {uuid: $nrn}) "
        "CREATE (sig)-[:MENTIONS {"
        "  uuid: $uuid, created_at: datetime($now)"
        "}]->(nrn)",
        {"sig": signal_uuid, "nrn": neuron_uuid,
         "uuid": muuid, "now": now},
    )

    log.info("mention_added", signal=signal_uuid, neuron=neuron_uuid)
    return {"status": "created", "mention_uuid": muuid}


async def impl_get_signal(uuid: str) -> dict[str, Any]:
    my, _ = await _get()
    rows  = await my._c.driver.execute_query(
        "MATCH (sig:Signal {uuid: $uuid}) "
        "OPTIONAL MATCH (sig)-[:MENTIONS]->(nrn:Neuron) "
        "  WHERE nrn.expired_at IS NULL "
        "RETURN sig, collect(DISTINCT {"
        "  uuid: nrn.uuid, name: nrn.name, type: nrn.neuron_type"
        "}) AS neurons",
        {"uuid": uuid},
    )
    if not rows:
        return {"error": f"Signal {uuid} not found"}

    r   = rows[0]
    sig = dict(r["sig"])
    return {
        "uuid":        sig.get("uuid"),
        "name":        sig.get("name", ""),
        "content":     sig.get("content", ""),
        "source_type": sig.get("source_type", ""),
        "source_desc": sig.get("source_desc", ""),
        "status":      sig.get("status", ""),
        "valid_at":    str(sig.get("valid_at", "")),
        "created_at":  str(sig.get("created_at", "")),
        "neurons":     [n for n in r["neurons"] if n.get("uuid")],
    }


def impl_schema() -> str:
    return (
        "# MYCELIUM v2 Schema\n\n"
        "## Nodes\n"
        "- **Signal**: uuid, name, content, source_type, "
        "status, valid_at, created_at\n"
        "- **Neuron**: uuid, name, neuron_type, summary, confidence, "
        "decay_rate, confirmations, freshness, attributes, expires_at\n\n"
        "## Edges\n"
        "- **SYNAPSE**: uuid, fact, relation, confidence, valid_at, "
        "invalid_at, created_at, expired_at, episodes[]\n"
        "- **MENTIONS**: uuid, created_at (Signal → Neuron)\n\n"
        "## Neuron Types\n"
        "person, relationship, trait, body, skill, practice, habit, project, "
        "belief, emotion, interest, goal, place, event, period, resource, "
        "recommendation, concept\n\n"
        "## Owner\n"
        "Owner = person neuron with attributes.is_owner=true. "
        "First-person text links to owner. Use set_owner/get_owner tools.\n\n"
        "## Communities\n"
        "detect_communities groups neurons into thematic clusters via Louvain.\n"
        "Creates community meta-neurons (neuron_type='community') with MEMBER_OF edges.\n"
        "Run periodically or when user asks about topics/themes.\n\n"
        "## Questions (S2)\n"
        "add_signal may return 'questions' — clarifying questions from LLM.\n"
        "Categories: conflict, incomplete, dedup, identity.\n"
        "Claude decides whether to ask the user."
    )


# ── R7: Domain Blueprints ────────────────────────────────────────


async def impl_list_domains() -> dict[str, Any]:
    domains = load_domains()
    return {
        "domains": [
            {
                "name":          d.name,
                "description":   d.description,
                "vault_prefix":  d.vault_prefix,
                "anchor_neuron": d.anchor_neuron,
                "triggers":      d.triggers,
            }
            for d in domains
        ],
        "count": len(domains),
    }


async def impl_get_domain(name: str) -> dict[str, Any]:
    d = load_domain(name)
    if not d:
        return {"error": f"Domain '{name}' not found."}
    return d.model_dump(mode="json")


async def impl_create_domain(
    name:          str,
    description:   str = "",
    vault_prefix:  str = "",
    anchor_neuron: str = "",
    anchor_type:   str = "",
    triggers:      str = "",
    skill:         str = "",
    focus:         str = "",
    neuron_types:  str = "",
    tracking_fields: str = "",
    tracking_fields_json: str = "",
    analysis:      str = "",
    chart_style_json: str = "",
) -> dict[str, Any]:
    if load_domain(name):
        return {"error": f"Domain '{name}' already exists. Use update_domain."}

    trigger_list = [t.strip() for t in triggers.split(",") if t.strip()] if triggers else []
    nt_list      = [t.strip() for t in neuron_types.split(",") if t.strip()] if neuron_types else []

    # tracking fields: structured JSON takes priority over comma-separated
    tf: dict[str, FieldConfig] | list[str] = []
    if tracking_fields_json:
        import json
        tf = json.loads(tracking_fields_json)
    elif tracking_fields:
        tf = [t.strip() for t in tracking_fields.split(",") if t.strip()]

    # chart style
    from mycelium.domain.models import ChartStyle
    cs = ChartStyle()
    if chart_style_json:
        import json
        cs = ChartStyle(**json.loads(chart_style_json))

    bp = DomainBlueprint(
        name          = name,
        description   = description,
        vault_prefix  = vault_prefix,
        anchor_neuron = anchor_neuron,
        anchor_type   = anchor_type or "domain",
        triggers      = trigger_list,
        extraction    = ExtractionConfig(skill=skill, focus=focus, neuron_types=nt_list),
        tracking      = TrackingConfig(fields=tf, analysis=analysis, chart_style=cs),
    )
    path = save_domain_bp(bp)
    return {"status": "created", "name": name, "path": str(path)}


async def impl_update_domain(
    name:          str,
    description:   str = "",
    vault_prefix:  str = "",
    anchor_neuron: str = "",
    anchor_type:   str = "",
    anchor_uuid:   str = "",
    triggers:      str = "",
    skill:         str = "",
    focus:         str = "",
    neuron_types:  str = "",
    tracking_fields: str = "",
    tracking_fields_json: str = "",
    analysis:      str = "",
    chart_style_json: str = "",
) -> dict[str, Any]:
    existing = load_domain(name)
    if not existing:
        return {"error": f"Domain '{name}' not found."}

    if description:   existing.description   = description
    if vault_prefix:  existing.vault_prefix  = vault_prefix
    if anchor_neuron: existing.anchor_neuron = anchor_neuron
    if anchor_type:   existing.anchor_type   = anchor_type
    if anchor_uuid:   existing.anchor_uuid   = anchor_uuid
    if triggers:
        existing.triggers = [t.strip() for t in triggers.split(",") if t.strip()]
    if skill:         existing.extraction.skill  = skill
    if focus:         existing.extraction.focus   = focus
    if neuron_types:
        existing.extraction.neuron_types = [t.strip() for t in neuron_types.split(",") if t.strip()]
    if tracking_fields_json:
        import json
        existing.tracking.fields = TrackingConfig._coerce_fields(json.loads(tracking_fields_json))
    elif tracking_fields:
        existing.tracking.fields = TrackingConfig._coerce_fields(
            [t.strip() for t in tracking_fields.split(",") if t.strip()],
        )
    if analysis:      existing.tracking.analysis = analysis
    if chart_style_json:
        import json
        from mycelium.domain.models import ChartStyle
        existing.tracking.chart_style = ChartStyle(**json.loads(chart_style_json))

    path = save_domain_bp(existing)
    return {"status": "updated", "name": name, "path": str(path)}


async def impl_delete_domain(name: str) -> dict[str, Any]:
    if delete_domain_bp(name):
        return {"status": "deleted", "name": name}
    return {"error": f"Domain '{name}' not found."}


async def impl_track(
    input_text: str,
    domain:     str = "",
    date:       str = "",
) -> dict[str, Any]:
    """Parse metrics from input and write to vault MD file."""
    from datetime import datetime as dt, timezone as tz
    sett = load_settings()

    # resolve date
    if not date:
        date = dt.now(tz.utc).strftime("%Y-%m-%d")

    # resolve domain
    bp = None
    if domain:
        bp = load_domain(domain)
        if not bp:
            return {"error": f"Domain '{domain}' not found."}
    else:
        # auto-detect by triggers
        domains = load_domains()
        bp = match_domain(domains, content=input_text)

    vault_root = sett.vault.path

    if bp and bp.tracking.fields:
        # domain tracking mode
        values, body = template_parse(input_text, bp.tracking.fields)
        if not values:
            return {"error": "No metrics parsed. Check field aliases in domain."}
        prefix = bp.vault_prefix or f"metrics/{bp.name}/"
        path   = write_metric_file(vault_root, prefix, date, values, body)
        rel    = str(path.relative_to(vault_root))
        return {
            "status":  "tracked",
            "file":    rel,
            "date":    date,
            "values":  values,
            "body":    body,
            "domain":  bp.name,
        }

    # quick mode: no domain — parse "<name> <number>"
    parts = input_text.strip().split(None, 1)
    if len(parts) >= 2:
        import re
        name_part = parts[0].lower().replace("-", "_")
        num_match = re.search(r"[-+]?\d+(?:[.,]\d+)?", parts[1])
        if num_match:
            val    = float(num_match.group().replace(",", "."))
            prefix = f"metrics/{name_part}/"
            path   = write_metric_file(vault_root, prefix, date, {name_part: val})
            rel    = str(path.relative_to(vault_root))
            return {
                "status":  "tracked",
                "file":    rel,
                "date":    date,
                "values":  {name_part: val},
                "body":    "",
                "domain":  None,
                "hint":    "Quick mode. Use /mycelium-domain to set up full tracking.",
            }

    return {"error": "Could not parse metrics. Use: /track <name> <value> or set up a domain."}


async def impl_get_metrics(
    domain: str,
    period: str = "30d",
    field:  str = "",
) -> dict[str, Any]:
    """Read metrics from vault and return table + stats."""
    from pathlib import Path as P
    sett       = load_settings()
    vault_root = sett.vault.path

    bp = load_domain(domain)
    if not bp:
        # check quick-mode folder
        quick_dir = vault_root / "metrics" / domain.lower().replace("-", "_") / "data"
        if not quick_dir.exists():
            return {"error": f"Domain '{domain}' not found and no quick metrics folder."}
        prefix = f"metrics/{domain.lower().replace('-', '_')}/"
        entries = read_metric_files(vault_root, prefix, period, field)
        return {"entries": entries, "count": len(entries), "domain": domain}

    prefix  = bp.vault_prefix or f"metrics/{bp.name}/"
    entries = read_metric_files(vault_root, prefix, period, field)

    # stats per field
    stats = {}
    for fname in bp.tracking.fields:
        if field and fname != field:
            continue
        stats[fname] = compute_stats(entries, fname)

    # update dashboard
    dash_path = None
    if bp.tracking.dashboard and bp.tracking.fields:
        dash_path = generate_dashboard(
            vault_root, prefix, bp.name, bp.tracking.fields, entries,
            chart_style=bp.tracking.chart_style,
        )

    return {
        "entries":   entries,
        "count":     len(entries),
        "stats":     stats,
        "domain":    bp.name,
        "dashboard": str(dash_path.relative_to(vault_root)) if dash_path else None,
    }


# ── MCP registration (thin wrappers) ─────────────────────────────


@mcp.tool
async def add_signal(
    content: str, name: str = "",
    source_type: str = "text", source_desc: str = "",
    extraction_focus: str = "",
    async_mode: bool = False,
) -> dict[str, Any]:
    """Ingest raw text through the FULL extraction pipeline (spawns LLM subprocess).

    Use for: raw text, messages, quick notes where you don't need to control extraction.
    Use INSTEAD: ingest_direct — when YOU extract neurons/synapses yourself (zero LLM calls, higher quality).

    Args:
        content: Raw text to ingest
        name: Signal label (auto-generated from content if empty)
        source_type: message | text | json | file
        source_desc: Origin description (e.g. "telegram chat")
        extraction_focus: Optional focus for LLM extraction (e.g. "technical decisions", "emotions only"). Empty = extract everything.
        async_mode: If true, return immediately with signal_uuid. Extraction runs in background. Poll get_signal(uuid) for status.
    """
    if g := _gate("write"): return g
    return await impl_add_signal(
        content, name, source_type, source_desc, extraction_focus, async_mode,
    )


@mcp.tool
async def ingest_direct(
    content: str, neurons: str, synapses: str,
    name: str = "", source_type: str = "text", source_desc: str = "",
) -> dict[str, Any]:
    """Ingest PRE-EXTRACTED neurons/synapses. Zero LLM calls — embed/dedup/save only.

    Use for: when YOU extracted neurons/synapses yourself via prompt (higher quality).
    Use INSTEAD: add_signal — when you want automatic LLM extraction.
    Workflow: vault_store → read file → extract → ingest_direct → vault_link.
    CAUTION: Synapses referencing neurons from a DIFFERENT ingest_direct call
    are silently dropped. Use add_synapse for cross-batch connections.

    Args:
        content: Raw text that was analyzed
        neurons: JSON string of neurons array.
            Each: {"name": str, "neuron_type": str, "confidence": 0.0-1.0,
                   "attributes": {}, "insights": []}
        synapses: JSON string of synapses array.
            Each: {"source": "neuron_name", "target": "neuron_name",
                   "relation": str, "fact": str, "confidence": 0.0-1.0,
                   "valid_at": "YYYY-MM-DD" or null}
        name: Signal label (auto-generated from content if empty)
        source_type: message | text | json | file
        source_desc: Origin description (e.g. "file:/path/to/file")
    """
    if g := _gate("write"): return g
    try:
        return await impl_ingest_direct(
            content, neurons, synapses, name, source_type, source_desc,
        )
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool
async def ingest_batch(items: str) -> dict[str, Any]:
    """Batch ingest multiple pre-extracted items with cross-item deduplication.

    More efficient than multiple ingest_direct calls: shared embedding,
    cross-item neuron dedup, single DB write pass.

    Args:
        items: JSON array of objects, each: {
            "content": str, "neurons": [...], "synapses": [...],
            "name": str?, "source_type": str?, "source_desc": str?
        }
        neurons format: [{"name": str, "neuron_type": str, "confidence": 0-1}]
        synapses format: [{"source": str, "target": str, "relation": str,
            "fact": str, "confidence": 0-1}]
    """
    if g := _gate("write"): return g
    try:
        return await impl_ingest_batch(items)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool
async def add_neuron(
    name: str, neuron_type: str,
    confidence: float = 1.0, summary: str = "",
    attributes: str = "{}",
) -> dict[str, Any]:
    """Create neuron directly (bypasses signal/LLM extraction).

    Auto-embeds name, deduplicates (exact + vector cosine >= 0.95),
    and consolidates on re-mention (confidence boost, decay reduction).

    Args:
        name: Neuron name (e.g. "Python", "Alice")
        neuron_type: Type (person, skill, interest, goal, concept, ...)
        confidence: Confidence score 0.0-1.0
        summary: Optional description
        attributes: JSON string with extra attributes (MCP limitation — no dict)
    """
    if g := _gate("write"): return g
    return await impl_add_neuron(name, neuron_type, confidence, summary, attributes)


@mcp.tool
async def search(query: str, top_k: int = 10, center_uuid: str = "") -> dict[str, Any]:
    """Hybrid search: vector + BM25 + graph BFS → RRF → decay-weighted reranking.

    Presets (combine top_k + query prefix for different modes):
      Quick:  top_k=5               — fast default
      Deep:   top_k=15              — thorough, more results
      Exact:  prefix query "lex:X"  — keyword match only (BM25)
      Vector: prefix query "vec:X"  — vector similarity only
      Smart:  prefix query "hyde:X" — LLM-augmented (best quality, slower)

    Args:
        query: Natural language search query. Supports mode prefixes: lex:, vec:, hyde:, vec+lex:
        top_k: Maximum results to return (default 10)
        center_uuid: Optional neuron UUID — starts BFS graph traversal from this neuron
    """
    if g := _gate("read"): return g
    return await impl_search(query, top_k, center_uuid)


@mcp.tool
async def get_neuron(uuid: str) -> dict[str, Any]:
    """Get neuron details + surrounding active synapses.

    Args:
        uuid: Neuron UUID
    """
    if g := _gate("read"): return g
    return await impl_get_neuron(uuid)


@mcp.tool
async def list_neurons(
    neuron_type: str = "", sort_by: str = "freshness",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Browse neurons with optional type filter and sorting.

    Args:
        neuron_type: Filter by type (person, interest, goal, skill, etc.)
        sort_by: freshness | confidence | name | weight
        limit: Max results (default 20)
    """
    if g := _gate("read"): return g
    return await impl_list_neurons(neuron_type, sort_by, limit)


@mcp.tool
async def add_synapse(
    source_uuid: str, target_uuid: str, fact: str,
    relation: str = "RELATES_TO", confidence: float = 1.0,
) -> dict[str, Any]:
    """Manually add a synapse between two neurons (bypasses LLM extraction).

    Args:
        source_uuid: Source neuron UUID
        target_uuid: Target neuron UUID
        fact: Natural language fact text
        relation: Relation subtype (e.g. INTERESTED_IN, WORKS_AT)
        confidence: Confidence score 0.0-1.0
    """
    if g := _gate("write"): return g
    return await impl_add_synapse(source_uuid, target_uuid, fact, relation, confidence)


@mcp.tool
async def delete_synapse(uuid: str) -> dict[str, Any]:
    """Soft-delete a synapse by setting expired_at (preserved for audit).

    Args:
        uuid: Synapse UUID to expire
    """
    if g := _gate("write"): return g
    return await impl_delete_synapse(uuid)


@mcp.tool
async def update_neuron(
    uuid: str, name: str = "",
    neuron_type: str = "", confidence: float = -1,
    importance: float = -1,
) -> dict[str, Any]:
    """Manual correction of neuron properties.

    Args:
        uuid: Neuron UUID
        name: New name (empty = no change)
        neuron_type: New type (empty = no change)
        confidence: Legacy alias for importance (negative = no change)
        importance: Stable significance 0-1 (negative = no change)
    """
    if g := _gate("write"): return g
    return await impl_update_neuron(uuid, name, neuron_type, confidence, importance)


@mcp.tool
async def get_signals(status: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Browse signal history.

    Args:
        status: Filter by status (pending | saved | failed). Empty = all.
        limit: Max results (default 20)
    """
    if g := _gate("read"): return g
    return await impl_get_signals(status, limit)


@mcp.tool
async def re_extract(signal_uuid: str) -> dict[str, Any]:
    """Re-run extraction on an existing signal (e.g. after prompt improvement).

    Args:
        signal_uuid: UUID of the signal to re-process
    """
    if g := _gate("write"): return g
    return await impl_re_extract(signal_uuid)


@mcp.tool
async def get_timeline(neuron_uuid: str, limit: int = 20) -> dict[str, Any]:
    """Temporal view: neuron evolution over time (synapses ordered by date).

    Args:
        neuron_uuid: Neuron UUID to trace
        limit: Max synapses to return
    """
    if g := _gate("read"): return g
    return await impl_get_timeline(neuron_uuid, limit)


@mcp.tool
async def health(verbose: bool = False) -> dict[str, Any]:
    """Quick graph stats: neuron/synapse counts, Neo4j status, stale neuron list.

    Use for: dashboard overview, checking if graph is alive.
    Use INSTEAD: sleep_report — for deep analysis with merge/rethink recommendations.

    Args:
        verbose: Include telemetry (tool calls, LLM tokens, latency)
    """
    if g := _gate("read"): return g
    return await impl_health(verbose=verbose)


@mcp.tool
async def set_owner(name: str) -> dict[str, Any]:
    """Set or update the knowledge graph owner name.

    Merges temporary 'я' neuron into the named owner if it exists.
    Creates owner neuron if not present.

    Args:
        name: Owner's real name
    """
    if g := _gate("write"): return g
    return await impl_set_owner(name)


@mcp.tool
async def get_owner() -> dict[str, Any]:
    """Get current owner name (null if not set yet)."""
    if g := _gate("read"): return g
    return await impl_get_owner()


@mcp.tool
async def rethink_neuron(uuid: str) -> dict[str, Any]:
    """Holistic neuron rewrite via LLM: analyzes ALL synapses and rewrites summary, type, attributes.

    Use when a neuron has accumulated many synapses but its summary/type are
    outdated or shallow. The LLM sees the full context and produces a richer
    representation. Knowledge improves through reflection, not just accumulation.

    Args:
        uuid: Neuron UUID to rethink
    """
    if g := _gate("write"): return g
    return await impl_rethink_neuron(uuid)


@mcp.tool
async def delete_neuron(uuid: str) -> dict[str, Any]:
    """Soft-delete neuron by setting expired_at + expire all related synapses.
    MENTIONS preserved for provenance.

    Args:
        uuid: Neuron UUID to expire
    """
    if g := _gate("write"): return g
    return await impl_delete_neuron(uuid)


@mcp.tool
async def merge_neurons(
    primary_uuid: str, secondary_uuid: str,
) -> dict[str, Any]:
    """Merge duplicate neurons: rewire synapses/mentions to primary, delete secondary.

    Args:
        primary_uuid: UUID of the neuron to keep
        secondary_uuid: UUID of the neuron to merge into primary and delete
    """
    if g := _gate("write"): return g
    return await impl_merge_neurons(primary_uuid, secondary_uuid)


@mcp.tool
async def add_mention(
    signal_uuid: str, neuron_uuid: str,
) -> dict[str, Any]:
    """Link Signal to Neuron (MENTIONS edge). Idempotent.

    Args:
        signal_uuid: UUID of the signal
        neuron_uuid: UUID of the neuron to link
    """
    if g := _gate("write"): return g
    return await impl_add_mention(signal_uuid, neuron_uuid)


@mcp.tool
async def get_signal(uuid: str) -> dict[str, Any]:
    """Get signal details with content and mentioned neurons.

    Args:
        uuid: Signal UUID
    """
    if g := _gate("read"): return g
    return await impl_get_signal(uuid)


async def impl_sleep_report(
    weak_threshold: float, dup_cosine_low: float,
    dup_cosine_high: float, limit: int,
) -> dict[str, Any]:
    my, _ = await _get()
    report = await build_sleep_report(
        my._c.driver,
        weak_threshold=weak_threshold,
        dup_cosine_low=dup_cosine_low,
        dup_cosine_high=dup_cosine_high,
        limit=limit,
    )
    return report.to_dict()


@mcp.tool
async def sleep_report(
    weak_threshold: float = 0.15,
    dup_cosine_low: float = 0.85,
    dup_cosine_high: float = 0.95,
    limit: int = 30,
) -> dict[str, Any]:
    """Deep graph health analysis: weak neurons, near-duplicates, isolated nodes,
    gaps, contradictions, and bridge candidates.

    Use for: planning distillation (merge/rethink/delete candidates).
    Use INSTEAD: health — for quick stats without analysis.

    Args:
        weak_threshold: Effective weight below this = weak (default 0.15)
        dup_cosine_low: Lower cosine bound for duplicate detection (default 0.85)
        dup_cosine_high: Upper cosine bound for duplicate detection (default 0.95)
        limit: Max candidates per category (default 30)
    """
    if g := _gate("read"): return g
    return await impl_sleep_report(
        weak_threshold, dup_cosine_low, dup_cosine_high, limit,
    )


async def impl_detect_communities(
    resolution: float, min_size: int,
) -> dict[str, Any]:
    my, sett = await _get()
    sett.community.resolution         = resolution
    sett.community.min_community_size = min_size
    report = await run_community_detection(
        my._c.driver, my._c.llm, my._c.embedder, sett.community,
    )
    return report.to_dict()


@mcp.tool
async def detect_communities(
    resolution: float = 1.0,
    min_size: int = 3,
) -> dict[str, Any]:
    """Detect thematic communities via Louvain algorithm.

    Creates community meta-neurons searchable by name/summary.
    Re-running replaces old communities (idempotent).

    Args:
        resolution: Higher = more clusters (default 1.0)
        min_size: Minimum neurons per community (default 3)
    """
    if g := _gate("write"): return g
    return await impl_detect_communities(resolution, min_size)




# ── R1.4: Skill Learning ────────────────────────────────────────


@mcp.tool
async def save_extraction_skill(
    name:    str,
    content: str,
    source:  str = "",
    format:  str = "",
    keyword: str = "",
) -> dict[str, Any]:
    """Save a reusable extraction pattern as a skill.

    Skills are .md templates with match rules. During ingestion,
    matching skills are injected into the extraction prompt.

    Args:
        name: Skill name (e.g. "Book extraction")
        content: Extraction guidance text (markdown)
        source: Match rule: signal source type (e.g. "file", "message")
        format: Match rule: format keyword (e.g. "pdf", "book")
        keyword: Match rule: keyword in signal name/desc
    """
    if g := _gate("write"): return g
    match_rules = {}
    if source:  match_rules["source"]  = source
    if format:  match_rules["format"]  = format
    if keyword: match_rules["keyword"] = keyword

    if not match_rules:
        return {"error": "At least one match rule (source, format, keyword) required"}

    path = save_skill(name, match_rules, content)
    return {"saved": str(path), "name": name, "match": match_rules}


@mcp.tool
async def list_extraction_skills() -> dict[str, Any]:
    """List all saved extraction skill patterns.

    Shows name, match rules, and file path for each skill.
    """
    if g := _gate("read"): return g
    skills = list_skills()
    return {"skills": skills, "count": len(skills)}


# ── R7: Domain Blueprint tools ───────────────────────────────────


@mcp.tool
async def list_domains() -> dict[str, Any]:
    """List all domain blueprints.

    Use to see available knowledge domains and their triggers.
    Use INSTEAD of get_domain when you need an overview.

    Returns: domains list with name, description, triggers, anchor.
    """
    if g := _gate("read"): return g
    return await impl_list_domains()


@mcp.tool
async def get_domain(name: str) -> dict[str, Any]:
    """Get full domain blueprint by name.

    Use to see complete config: extraction rules, tracking fields,
    vault prefix, anchor neuron. Use AFTER list_domains to inspect.

    Args:
        name: Domain name (case-insensitive).
    """
    if g := _gate("read"): return g
    return await impl_get_domain(name)


@mcp.tool
async def create_domain(
    name:            str,
    description:     str = "",
    vault_prefix:    str = "",
    anchor_neuron:   str = "",
    anchor_type:     str = "",
    triggers:        str = "",
    skill:           str = "",
    focus:           str = "",
    neuron_types:    str = "",
    tracking_fields: str = "",
    tracking_fields_json: str = "",
    analysis:        str = "",
    chart_style_json: str = "",
) -> dict[str, Any]:
    """Create a new domain blueprint.

    Use to define a knowledge domain (e.g., "Blood Analysis", "Finances").
    After creating: add_neuron to create the anchor, then update_domain
    with anchor_uuid.

    Args:
        name:            Domain name.
        description:     Human-readable purpose.
        vault_prefix:    Vault subdirectory (e.g., "health/blood_tests/").
        anchor_neuron:   Hub neuron name in graph.
        anchor_type:     Neuron type for anchor (default: "domain").
        triggers:        Comma-separated trigger keywords for auto-detection.
        skill:           Extraction skill name to apply.
        focus:           Extraction focus text (injected into prompt).
        neuron_types:    Comma-separated expected neuron types.
        tracking_fields: Comma-separated field names (simple mode).
        tracking_fields_json: JSON dict of structured fields with labels, aliases, references.
        analysis:        Analysis instruction for trend detection.
        chart_style_json: JSON chart style: {"type","color","show_point","point_size","height"}.
    """
    if g := _gate("write"): return g
    return await impl_create_domain(
        name, description, vault_prefix, anchor_neuron, anchor_type,
        triggers, skill, focus, neuron_types, tracking_fields,
        tracking_fields_json, analysis, chart_style_json,
    )


@mcp.tool
async def update_domain(
    name:            str,
    description:     str = "",
    vault_prefix:    str = "",
    anchor_neuron:   str = "",
    anchor_type:     str = "",
    anchor_uuid:     str = "",
    triggers:        str = "",
    skill:           str = "",
    focus:           str = "",
    neuron_types:    str = "",
    tracking_fields: str = "",
    tracking_fields_json: str = "",
    analysis:        str = "",
    chart_style_json: str = "",
) -> dict[str, Any]:
    """Update an existing domain blueprint.

    Only provided fields are updated; omitted fields stay unchanged.

    Args:
        name:            Domain name (used to find existing blueprint).
        description:     New description (if provided).
        vault_prefix:    New vault subdirectory (if provided).
        anchor_neuron:   New anchor neuron name (if provided).
        anchor_type:     New anchor type (if provided).
        anchor_uuid:     Anchor neuron UUID (set after add_neuron).
        triggers:        New comma-separated triggers (replaces all).
        skill:           New extraction skill name (if provided).
        focus:           New extraction focus (if provided).
        neuron_types:    New comma-separated neuron types (replaces all).
        tracking_fields: Comma-separated field names (simple mode, replaces all).
        tracking_fields_json: JSON dict of structured fields (replaces all).
        analysis:        New analysis instruction (if provided).
        chart_style_json: JSON chart style (replaces all).
    """
    if g := _gate("write"): return g
    return await impl_update_domain(
        name, description, vault_prefix, anchor_neuron, anchor_type,
        anchor_uuid, triggers, skill, focus, neuron_types,
        tracking_fields, tracking_fields_json, analysis, chart_style_json,
    )


@mcp.tool
async def track(
    input: str,
    domain: str = "",
    date:   str = "",
) -> dict[str, Any]:
    """Track metrics: parse numbers from text, save as structured MD file.

    Template-based parsing using domain field aliases (zero LLM cost).
    Quick mode: if no domain matches, auto-creates minimal metric tracking.

    Args:
        input:  Text with metrics (e.g., "bench 80 squat 100 45min").
        domain: Domain name (optional, auto-detected from triggers).
        date:   Date override YYYY-MM-DD (default: today).
    """
    if g := _gate("write"): return g
    return await impl_track(input, domain, date)


@mcp.tool
async def get_metrics(
    domain: str,
    period: str = "30d",
    field:  str = "",
) -> dict[str, Any]:
    """Read tracked metrics and compute stats.

    Returns entries table, stats (min/max/avg/trend), and updates dashboard.

    Args:
        domain: Domain name or quick-mode metric name.
        period: Time window: "7d", "30d", "90d", "all" (default: "30d").
        field:  Filter to specific field (optional, default: all).
    """
    if g := _gate("read"): return g
    return await impl_get_metrics(domain, period, field)


@mcp.tool
async def delete_domain(name: str) -> dict[str, Any]:
    """Delete a domain blueprint.

    Removes the YAML config file. Anchor neuron stays in graph
    (soft-delete pattern). Use when domain is no longer needed.

    Args:
        name: Domain name (case-insensitive).
    """
    if g := _gate("write"): return g
    return await impl_delete_domain(name)


@mcp.resource("mycelium://domains")
def domains_resource() -> str:
    """Available domain blueprints with triggers and anchors.

    Use before ingestion to check if a matching domain exists.
    """
    return to_compact_list(load_domains())


@mcp.resource("mycelium://schema")
def schema_resource() -> str:
    """MYCELIUM v2 knowledge graph schema."""
    return impl_schema()


@mcp.resource("mycelium://stats")
async def stats_resource() -> str:
    """Current graph statistics."""
    try:
        h = await impl_health()
        return (
            f"Neurons: {h['neurons']}, Signals: {h['signals']}\n"
            f"Active synapses: {h['active_synapses']}, "
            f"Expired: {h['expired_synapses']}\n"
            f"Neo4j: {h['neo4j']}\nStale neurons: {len(h['stale'])}"
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.resource("mycelium://context")
async def context_resource() -> str:
    """Compact graph context: owner, top neurons by weight, recent neurons, counts.

    Use before extraction to check what already exists and avoid duplicates.
    Cheaper than calling health() + list_neurons() separately.
    """
    try:
        my, _ = await _get()
        drv = my._c.driver

        owner = await drv.execute_query(
            "MATCH (n:Neuron {neuron_type: 'person'}) "
            "WHERE n.expired_at IS NULL AND (n.attributes IS NULL OR n.attributes CONTAINS '\"is_owner\"') "
            "RETURN n.name AS name LIMIT 1"
        )
        counts = await drv.execute_query(
            "OPTIONAL MATCH (n:Neuron) WHERE n.expired_at IS NULL "
            "  WITH count(n) AS neurons "
            "OPTIONAL MATCH ()-[f:SYNAPSE]->() WHERE f.expired_at IS NULL "
            "RETURN neurons, count(f) AS synapses"
        )
        top = await drv.execute_query(
            "MATCH (e:Neuron) WHERE e.expired_at IS NULL "
            "WITH e, coalesce(e.importance, e.confidence) * exp(-e.decay_rate * "
            "  duration.between(e.freshness, datetime()).days) AS ew "
            "WHERE ew > 0.1 "
            "RETURN e.name AS name, e.neuron_type AS type, round(ew * 100) / 100 AS weight "
            "ORDER BY ew DESC LIMIT 15"
        )
        recent = await drv.execute_query(
            "MATCH (e:Neuron) WHERE e.expired_at IS NULL "
            "WITH e, coalesce(e.importance, e.confidence) * exp(-e.decay_rate * "
            "  duration.between(e.freshness, datetime()).days) AS ew "
            "WHERE ew > 0.1 "
            "RETURN e.name AS name, e.neuron_type AS type "
            "ORDER BY e.freshness DESC LIMIT 10"
        )

        c = counts[0] if counts else {}
        lines = []
        if owner:
            lines.append(f"Owner: {owner[0]['name']}")
        lines.append(f"Neurons: {c.get('neurons', 0)}, Active synapses: {c.get('synapses', 0)}")
        if top:
            top_str = ", ".join(f"{n['name']} ({n['type']}, w={n['weight']})" for n in top)
            lines.append(f"Top by weight: {top_str}")
        if recent:
            rec_str = ", ".join(f"{n['name']} ({n['type']})" for n in recent)
            lines.append(f"Recent: {rec_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# ── R6.1: Export/Import Subgraph ──────────────────────────────────


async def impl_export_subgraph(
    neuron_uuids:       list[str] | None = None,
    include_expired:    bool             = False,
    include_embeddings: bool             = False,
) -> dict[str, Any]:
    my, settings = await _get()
    return await run_export(
        my._c.driver, settings,
        neuron_uuids=neuron_uuids, include_expired=include_expired,
        include_embeddings=include_embeddings,
    )


async def impl_import_subgraph(data: dict[str, Any]) -> dict[str, Any]:
    my, settings = await _get()
    return await run_import(my._c.driver, my._c.embedder, settings, data)


@mcp.tool
async def export_subgraph(
    neuron_uuids:       list[str] | None = None,
    include_expired:    bool             = False,
    include_embeddings: bool             = False,
) -> dict[str, Any]:
    """Export neurons + synapses + signals as JSON.

    Args:
        neuron_uuids:       specific neurons to export (None = all active)
        include_expired:    include soft-deleted neurons/synapses
        include_embeddings: include raw embedding vectors (large, for migration only)

    Returns dict with metadata, neurons, synapses, signals, mentions, stats.
    Save result to file for backup/migration/sharing."""
    if err := _gate("read"):
        return err
    return await impl_export_subgraph(neuron_uuids, include_expired, include_embeddings)


@mcp.tool
async def import_subgraph(data: dict[str, Any]) -> dict[str, Any]:
    """Import previously exported subgraph into graph.

    Args:
        data: dict from export_subgraph (with metadata, neurons, synapses, etc.)

    Merges by uuid — existing neurons skipped, new ones created.
    Re-embeds vectors if embedding model differs from export."""
    if err := _gate("write"):
        return err
    return await impl_import_subgraph(data)


# ── Vault ─────────────────────────────────────────────────────────


@mcp.tool
async def vault_store(file_path: str, category: str = "") -> dict[str, Any]:
    """Store file in vault. Step 1 of 3: vault_store → ingest_direct → vault_link.

    Call BEFORE extraction. Returns relative_path for source_desc and vault_link.

    Args:
        file_path: Absolute path to file on disk
        category: Override category (default: auto from MIME type)

    Returns:
        relative_path, content_hash, mime_type, size_bytes, is_duplicate, original_ext
    """
    if err := _gate("write"):
        return err
    from mycelium.vault.storage import VaultStorage
    _, settings = await _get()
    vault = VaultStorage(settings.vault)
    p = pathlib.Path(file_path).expanduser()
    if not p.exists():
        return {"error": f"File not found: {file_path}"}

    # .txt → .md for Obsidian
    store_name   = ""
    original_ext = ""
    if settings.obsidian.enabled and p.suffix == ".txt":
        store_name   = p.stem + ".md"
        original_ext = "txt"

    existing = vault.find_by_hash(
        __import__("hashlib").sha256(p.read_bytes()).hexdigest()
    )
    entry = vault.store(p, category=category, name=store_name)
    return {
        "relative_path": entry.relative_path,
        "content_hash":  entry.content_hash,
        "mime_type":     entry.mime_type,
        "size_bytes":    entry.size_bytes,
        "is_duplicate":  existing is not None,
        "original_ext":  original_ext,
    }


@mcp.tool
async def vault_link(
    relative_path: str, signal_uuid: str, original_ext: str = "",
) -> dict[str, Any]:
    """Link vault file to signal. Step 3 of 3: vault_store → ingest_direct → vault_link.

    Connects vault entry to knowledge graph. Injects Obsidian frontmatter if enabled.

    Args:
        relative_path: Vault relative path from vault_store response
        signal_uuid: Signal UUID from the FIRST ingest_direct response
        original_ext: Original extension from vault_store (e.g. "txt" if converted to .md)
    """
    if err := _gate("write"):
        return err
    from mycelium.vault.storage import VaultStorage
    my, settings = await _get()
    vault = VaultStorage(settings.vault)

    vault.update_signal_uuid(relative_path, signal_uuid)

    if settings.obsidian.enabled:
        from mycelium.obsidian.sync import inject_after_ingest
        await inject_after_ingest(
            my._c.driver, vault,
            relative_path, signal_uuid,
            settings.obsidian,
            original_ext=original_ext,
        )

    return {"status": "linked", "relative_path": relative_path}


# ── Obsidian ──────────────────────────────────────────────────────


@mcp.tool
async def obsidian_sync(
    ingest: bool = False,
) -> dict[str, Any]:
    """Sync Obsidian frontmatter: recompute relations and write YAML frontmatter
    for all vault .md files. Creates companion .md for binary files.

    When unindexed files are found (manually added to vault), their paths
    are returned in `unindexed`. Use `ingest=true` to auto-ingest them
    into the knowledge graph and re-sync all frontmatter.

    Requires obsidian.enabled = true in config."""
    if err := _gate("read"):
        return err
    my, settings = await _get()
    if not settings.obsidian.enabled:
        return {"error": "Obsidian layer is disabled. Set obsidian.enabled = true."}
    from mycelium.obsidian.sync import sync
    from mycelium.vault.storage import VaultStorage
    t0    = time.monotonic()
    vault = VaultStorage(settings.vault)
    result = await sync(my._c.driver, vault, settings.obsidian)

    ingested: list[str] = []
    ingest_errors: list[str] = []

    if ingest and result.unindexed:
        if err := _gate("write"):
            return err
        ingested, ingest_errors = await _ingest_unindexed(
            my, vault, result.unindexed,
        )
        if ingested:
            result = await sync(my._c.driver, vault, settings.obsidian)

    ms = int((time.monotonic() - t0) * 1000)
    resp: dict[str, Any] = {
        "updated":      result.updated,
        "companions":   result.companions,
        "skipped":      result.skipped,
        "unindexed":    result.unindexed,
        "hash_changed": result.hash_changed,
        "duration_ms":  ms,
    }
    if result.moved:
        resp["moved"] = result.moved
    if result.projected or result.pruned:
        resp["neurons_projected"] = result.projected
        resp["neurons_pruned"]    = result.pruned
    if ingested:
        resp["ingested"] = ingested
    if ingest_errors:
        resp["ingest_errors"] = ingest_errors
    if not ingest and result.unindexed:
        resp["hint"] = (
            f"{len(result.unindexed)} unindexed file(s) found. "
            "Run obsidian_sync(ingest=true) to ingest them into the graph."
        )
    return resp


async def _ingest_unindexed(
    my:        Mycelium,
    vault:     VaultStorage,
    unindexed: list[str],
) -> tuple[list[str], list[str]]:
    """Ingest unindexed vault files. Returns (ingested, errors)."""
    ingested: list[str] = []
    errors:   list[str] = []

    for rel_path in unindexed:
        abs_path = vault.root / rel_path
        if not abs_path.exists():
            continue
        try:
            # Register in vault index if missing (no copy — file is already in vault)
            entry = vault.get_by_path(rel_path)
            if not entry:
                entry = vault.register(rel_path)

            content = vault.extract_text(entry)
            if not content:
                errors.append(f"{rel_path}: no extractable text")
                continue

            signal, neurons, synapses, _ = await my.add_episode(
                content,
                name        = abs_path.name,
                source_type = SignalType.file,
                source_desc = f"file:{entry.relative_path}",
            )
            vault.update_signal_uuid(entry.relative_path, signal.uuid)

            # Inject frontmatter
            if my._s.obsidian.enabled:
                from mycelium.obsidian.sync import inject_after_ingest
                await inject_after_ingest(
                    my._c.driver, vault,
                    entry.relative_path, signal.uuid,
                    my._s.obsidian,
                )

            ingested.append(rel_path)
            log.info("unindexed_ingested", path=rel_path,
                     neurons=len(neurons), synapses=len(synapses))

        except Exception as e:
            errors.append(f"{rel_path}: {e}")
            log.warning("unindexed_ingest_failed", path=rel_path, error=str(e))

    return ingested, errors


# ── Knowledge resources ───────────────────────────────────────────


@mcp.resource("mycelium://ontology")
def ontology_resource() -> str:
    """Neuron and relation type definitions."""
    return (_KNOWLEDGE_DIR / "ontology.md").read_text()


@mcp.resource("mycelium://extraction-rules")
def extraction_resource() -> str:
    """Extraction rules, confidence levels, examples."""
    return (_KNOWLEDGE_DIR / "extraction.md").read_text()


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    from mycelium.config import load_settings as _load
    _mcp_cfg = _load().mcp
    if _mcp_cfg.transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=_mcp_cfg.transport, host=_mcp_cfg.host, port=_mcp_cfg.port)  # type: ignore[arg-type]
