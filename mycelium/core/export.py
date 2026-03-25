"""Export/Import subgraph: neurons + synapses + signals + mentions (R6.1)."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from mycelium import __version__
from mycelium.config import Settings
from mycelium.driver.driver import GraphDriver
from mycelium.embedder.client import EmbedderClient

log = structlog.get_logger()


# ── Export ────────────────────────────────────────────────────


_EMBEDDING_KEYS = frozenset({
    "name_embedding", "summary_embedding", "fact_embedding", "content_embedding",
})


async def export_subgraph(
    drv:               GraphDriver,
    settings:          Settings,
    neuron_uuids:      list[str] | None = None,
    include_expired:   bool             = False,
    include_embeddings: bool            = False,
) -> dict[str, Any]:
    """Export neurons + synapses + signals + mentions as JSON-ready dict."""
    t0 = time.monotonic()

    expire_filter = "" if include_expired else "AND n.expired_at IS NULL "

    # ── Neurons ───────────────────────────────────────
    if neuron_uuids:
        n_rows = await drv.execute_query(
            "MATCH (n:Neuron) WHERE n.uuid IN $uuids "
            + expire_filter
            + "RETURN n",
            {"uuids": neuron_uuids},
        )
    else:
        n_rows = await drv.execute_query(
            "MATCH (n:Neuron) WHERE true " + expire_filter + "RETURN n",
        )

    neurons = [_ser_node(r["n"], include_embeddings) for r in n_rows]
    n_set   = {n["uuid"] for n in neurons}

    # ── Synapses (between selected neurons) ───────────
    syn_filter = "" if include_expired else "AND r.expired_at IS NULL "
    s_rows = await drv.execute_query(
        "MATCH (s:Neuron)-[r:SYNAPSE]->(t:Neuron) "
        "WHERE s.uuid IN $uuids AND t.uuid IN $uuids "
        + syn_filter
        + "RETURN properties(r) AS r, s.uuid AS src, t.uuid AS tgt",
        {"uuids": list(n_set)},
    )
    synapses = [_ser_rel(r["r"], r["src"], r["tgt"], include_embeddings) for r in s_rows]

    # ── Signals (provenance: mentioned by selected neurons) ──
    sig_rows = await drv.execute_query(
        "MATCH (sig:Signal)-[:MENTIONS]->(n:Neuron) "
        "WHERE n.uuid IN $uuids "
        "RETURN DISTINCT sig",
        {"uuids": list(n_set)},
    )
    signals = [_ser_node(r["sig"], include_embeddings) for r in sig_rows]

    # ── Mentions ──────────────────────────────────────
    m_rows = await drv.execute_query(
        "MATCH (sig:Signal)-[m:MENTIONS]->(n:Neuron) "
        "WHERE n.uuid IN $uuids "
        "RETURN properties(m) AS m, sig.uuid AS sig_uuid, n.uuid AS n_uuid",
        {"uuids": list(n_set)},
    )
    mentions = [_ser_mention(r["m"], r["sig_uuid"], r["n_uuid"]) for r in m_rows]

    ms = int((time.monotonic() - t0) * 1000)
    log.info("export_done", neurons=len(neurons), synapses=len(synapses),
             signals=len(signals), mentions=len(mentions), ms=ms)

    return {
        "metadata": {
            "mycelium_version":  __version__,
            "embedding_model":   settings.semantic.model_name,
            "embedding_dims":    settings.semantic.dimensions,
            "export_date":       datetime.now(UTC).isoformat(),
            "include_expired":   include_expired,
        },
        "neurons":  neurons,
        "synapses": synapses,
        "signals":  signals,
        "mentions": mentions,
        "stats": {
            "neurons":  len(neurons),
            "synapses": len(synapses),
            "signals":  len(signals),
            "mentions": len(mentions),
            "duration_ms": ms,
        },
    }


# ── Import ────────────────────────────────────────────────────


async def import_subgraph(
    drv:      GraphDriver,
    emb:      EmbedderClient,
    settings: Settings,
    data:     dict[str, Any],
) -> dict[str, Any]:
    """Import previously exported subgraph. Returns counts."""
    t0       = time.monotonic()
    meta     = data.get("metadata", {})
    re_embed = meta.get("embedding_model", "") != settings.semantic.model_name

    neurons  = data.get("neurons", [])
    synapses = data.get("synapses", [])
    signals  = data.get("signals", [])
    mentions = data.get("mentions", [])

    # ── Signals (create if not exists) ────────────────
    if signals:
        await drv.execute_query(
            "UNWIND $batch AS s "
            "MERGE (sig:Signal {uuid: s.uuid}) "
            "ON CREATE SET sig += s.props",
            {"batch": [
                {"uuid": s["uuid"], "props": _import_props(s)}
                for s in signals
            ]},
        )

    # ── Neurons (merge by uuid, skip existing) ────────
    if re_embed and neurons:
        neurons = await _re_embed_neurons(emb, neurons)

    n_created = 0
    if neurons:
        rows = await drv.execute_query(
            "UNWIND $batch AS e "
            "MERGE (n:Neuron {uuid: e.uuid}) "
            "ON CREATE SET n += e.props, n._imported = true "
            "WITH n, n._imported IS NOT NULL AS is_new "
            "REMOVE n._imported "
            "RETURN n.uuid AS uuid, "
            "  CASE WHEN is_new THEN 'created' ELSE 'existed' END AS status",
            {"batch": [
                {"uuid": n["uuid"], "props": _import_props(n)}
                for n in neurons
            ]},
        )
        n_created = sum(1 for r in rows if r["status"] == "created")

    # ── Synapses (create if uuid doesn't exist) ───────
    s_created = 0
    if synapses:
        if re_embed:
            synapses = await _re_embed_synapses(emb, synapses)

        rows = await drv.execute_query(
            "UNWIND $batch AS f "
            "MATCH (s:Neuron {uuid: f.src}), (t:Neuron {uuid: f.tgt}) "
            "WHERE NOT EXISTS { MATCH ()-[r:SYNAPSE {uuid: f.uuid}]->() } "
            "CREATE (s)-[r:SYNAPSE]->(t) SET r += f.props "
            "RETURN f.uuid AS uuid",
            {"batch": [
                {
                    "uuid":  s["uuid"],
                    "src":   s["source_uuid"],
                    "tgt":   s["target_uuid"],
                    "props": _import_props(s),
                }
                for s in synapses
            ]},
        )
        s_created = len(rows)

    # ── Mentions (create if not exists) ───────────────
    m_created = 0
    if mentions:
        rows = await drv.execute_query(
            "UNWIND $batch AS m "
            "MATCH (sig:Signal {uuid: m.sig}), (n:Neuron {uuid: m.nrn}) "
            "WHERE NOT EXISTS { MATCH (sig)-[:MENTIONS {uuid: m.uuid}]->(n) } "
            "CREATE (sig)-[r:MENTIONS {uuid: m.uuid, created_at: m.created_at}]->(n) "
            "RETURN m.uuid AS uuid",
            {"batch": [
                {
                    "uuid":       m["uuid"],
                    "sig":        m["source_uuid"],
                    "nrn":        m["target_uuid"],
                    "created_at": _to_datetime(m.get("created_at")),
                }
                for m in mentions
            ]},
        )
        m_created = len(rows)

    ms = int((time.monotonic() - t0) * 1000)
    log.info("import_done", neurons=n_created, synapses=s_created,
             signals=len(signals), re_embed=re_embed, ms=ms)

    return {
        "neurons_created":  n_created,
        "neurons_skipped":  len(neurons) - n_created,
        "synapses_created": s_created,
        "synapses_skipped": len(synapses) - s_created,
        "signals_imported": len(signals),
        "mentions_created": m_created,
        "re_embedded":      re_embed,
        "duration_ms":      ms,
    }


# ── Helpers ───────────────────────────────────────────────────


def _ser_node(n: Any, include_embeddings: bool = False) -> dict[str, Any]:
    """Serialize Neo4j node to plain dict with str dates."""
    d = dict(n)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    if not include_embeddings:
        for key in _EMBEDDING_KEYS:
            d.pop(key, None)
    return d


def _ser_rel(r: Any, src: str, tgt: str, include_embeddings: bool = False) -> dict[str, Any]:
    """Serialize Neo4j relationship to dict with source/target."""
    d = dict(r)
    d["source_uuid"] = src
    d["target_uuid"] = tgt
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    if not include_embeddings:
        for key in _EMBEDDING_KEYS:
            d.pop(key, None)
    return d


def _ser_mention(m: Any, sig_uuid: str, n_uuid: str) -> dict[str, Any]:
    d = dict(m) if isinstance(m, dict) else {}
    d.setdefault("uuid", f"{sig_uuid}:{n_uuid}")
    d["source_uuid"] = sig_uuid
    d["target_uuid"] = n_uuid
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_DATETIME_KEYS = {"created_at", "freshness", "expired_at", "valid_at", "invalid_at"}


def _import_props(d: dict[str, Any]) -> dict[str, Any]:
    """Strip meta keys, convert ISO datetime strings back to native datetime.

    Neo4j properties must be primitives or arrays — dicts are serialised
    to JSON strings so they survive the round-trip.
    """
    skip = {"source_uuid", "target_uuid"}
    out = {}
    for k, v in d.items():
        if k in skip or v is None:
            continue
        if k in _DATETIME_KEYS and isinstance(v, str) and _ISO_RE.match(v):
            out[k] = datetime.fromisoformat(v)
        elif isinstance(v, dict):
            out[k] = json.dumps(v, ensure_ascii=False) if v else "{}"
        else:
            out[k] = v
    return out


def _to_datetime(v: str | None) -> datetime:
    """Convert ISO string or None to datetime."""
    if v and isinstance(v, str):
        return datetime.fromisoformat(v)
    return v if v else datetime.now(UTC)


async def _re_embed_neurons(
    emb: EmbedderClient, neurons: list[dict],
) -> list[dict]:
    """Re-generate embeddings for imported neurons."""
    for n in neurons:
        name = n.get("name", "")
        if name:
            n["name_embedding"] = await emb.embed(name)
        summary = n.get("summary", "")
        if summary:
            n["summary_embedding"] = await emb.embed(summary)
    return neurons


async def _re_embed_synapses(
    emb: EmbedderClient, synapses: list[dict],
) -> list[dict]:
    """Re-generate embeddings for imported synapses."""
    for s in synapses:
        fact = s.get("fact", "")
        if fact:
            s["fact_embedding"] = await emb.embed(fact)
    return synapses
