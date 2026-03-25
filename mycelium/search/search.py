"""Hybrid search: vector + BM25 + BFS → RRF → decay rerank."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from mycelium.config import Settings
from mycelium.core.models import Neuron, Signal, SignalStatus, SignalType, Synapse
from mycelium.core.telemetry import Telemetry
from mycelium.driver.driver import GraphDriver
from mycelium.embedder.client import EmbedderClient
from mycelium.search.config import (
    ScoredNeuron,
    ScoredSynapse,
    SearchMethod,
    SearchResults,
)
from mycelium.search.rerankers import (
    RerankerContext,
    make_pipeline,
    rrf_fuse,
)

if TYPE_CHECKING:
    from mycelium.llm.base import LLMBackend

log = structlog.get_logger()


# ── Lucene escape ─────────────────────────────────────────

_LUCENE_ESC = frozenset('+-&|!(){}[]^"~*?:\\/')


def _escape(q: str) -> str:
    """Escape Lucene special chars for fulltext search."""
    return "".join(f"\\{c}" if c in _LUCENE_ESC else c for c in q)



# ── Query routing (CF#8) ──────────────────────────────────

_QUERY_PREFIX_RE = re.compile(
    r"^((?:lex|vec|hyde)(?:\+(?:lex|vec|hyde))*):(.*)",
    re.DOTALL,
)


def _parse_query(raw: str) -> tuple[set[str], str]:
    """Parse query prefix → (modes, clean_query).

    Examples:
      "lex:Python"       → ({"lex"}, "Python")
      "vec+lex:health"   → ({"vec", "lex"}, "health")
      "hyde:my interests" → ({"hyde"}, "my interests")
      "plain query"       → (set(), "plain query")  # hybrid default
    """
    m = _QUERY_PREFIX_RE.match(raw.strip())
    if not m:
        return set(), raw.strip()
    modes = set(m.group(1).split("+"))
    return modes, m.group(2).strip()


# ── HyDE prompt (CF#9) ──────────────────────────────────

_HYDE_PROMPT = """\
Write a short paragraph (50-100 words) that would be a good answer \
to the following question. Write it as if describing facts from a \
personal knowledge graph. Be specific and detailed.

Question: {query}

Respond with ONLY the paragraph, no explanation.\
"""


# ── Data conversion ───────────────────────────────────────


def _to_dt(v: Any) -> datetime:
    if v is None:
        return datetime.now(UTC)
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if hasattr(v, "to_native"):
        return v.to_native()
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _to_dt_opt(v: Any) -> datetime | None:
    return None if v is None else _to_dt(v)


def _parse_attrs(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _to_neuron(d: dict) -> Neuron:
    imp = d.get("importance") or d.get("confidence") or 1.0
    return Neuron(
        uuid          = d["uuid"],
        name          = d.get("name") or "",
        neuron_type   = d.get("neuron_type") or "",
        summary       = d.get("summary") or "",
        importance    = imp,
        confidence    = imp,
        decay_rate    = d.get("decay_rate") or 0.008,
        confirmations = d.get("confirmations") or 0,
        freshness     = _to_dt(d.get("freshness")),
        attributes    = _parse_attrs(d.get("attributes")),
        origin        = d.get("origin") or "raw",
        created_at    = _to_dt(d.get("created_at")),
        expires_at    = _to_dt_opt(d.get("expires_at")),
    )


def _to_synapse(d: dict) -> Synapse:
    return Synapse(
        uuid          = d["uuid"],
        source_uuid   = d.get("source_uuid") or "",
        target_uuid   = d.get("target_uuid") or "",
        relation      = d.get("relation") or "",
        fact          = d.get("fact") or "",
        episodes      = d.get("episodes") or [],
        confidence    = d.get("confidence") or 1.0,
        valid_at      = _to_dt_opt(d.get("valid_at")),
        created_at    = _to_dt(d.get("created_at")),
    )


def _to_signal(d: dict) -> Signal:
    return Signal(
        uuid          = d["uuid"],
        name          = d.get("name") or "",
        content       = d.get("content") or "",
        source_type   = SignalType(d.get("source_type", "text")),
        source_desc   = d.get("source_desc") or "",
        status        = SignalStatus(d.get("status", "saved")),
        valid_at      = _to_dt(d.get("valid_at")),
        created_at    = _to_dt(d.get("created_at")),
    )


# ── Cypher column templates ───────────────────────────────

_N_COLS = (
    "e.uuid AS uuid, e.name AS name, e.neuron_type AS neuron_type, "
    "e.summary AS summary, "
    "coalesce(e.importance, e.confidence) AS importance, "
    "e.confidence AS confidence, "
    "e.decay_rate AS decay_rate, e.confirmations AS confirmations, "
    "e.freshness AS freshness, e.attributes AS attributes, "
    "e.created_at AS created_at, "
    "e.propagated_confidence AS propagated_confidence, "
    "coalesce(e.origin, 'raw') AS origin"
)

_S_COLS = (
    "r.uuid AS uuid, r.fact AS fact, r.relation AS relation, "
    "r.confidence AS confidence, r.episodes AS episodes, "
    "r.valid_at AS valid_at, r.created_at AS created_at, "
    "s.uuid AS source_uuid, s.name AS source_name, "
    "t.uuid AS target_uuid, t.name AS target_name"
)


# ── HybridSearch ──────────────────────────────────────────


class HybridSearch:
    """Vector + BM25 + BFS → RRF → decay-weighted rerank."""

    def __init__(
        self,
        driver:   GraphDriver,
        embedder: EmbedderClient,
        settings: Settings,
        llm:      LLMBackend | None = None,
        owner_uuid: str = "",
    ) -> None:
        self._drv        = driver
        self._emb        = embedder
        self._llm        = llm
        self._cfg        = settings.search
        self._sem        = settings.semantic
        self._pipeline   = make_pipeline(self._cfg)
        self._owner_uuid = owner_uuid

    def set_owner_uuid(self, uuid: str) -> None:
        """Update owner uuid (called after lazy init_owner)."""
        self._owner_uuid = uuid

    async def search(
        self,
        query:       str,
        *,
        top_k:       int | None = None,
        center_uuid: str | None = None,
    ) -> SearchResults:
        """Full hybrid search pipeline with query routing."""
        t0 = time.monotonic()
        n  = top_k or self._cfg.top_k

        # [0] Parse query prefix (CF#8)
        modes, clean_q = _parse_query(query)
        use_lex  = "lex" in modes or not modes
        use_vec  = "vec" in modes or "hyde" in modes or not modes
        use_hyde = "hyde" in modes
        use_bfs  = not modes  # BFS only in hybrid mode

        # [1] Embed query (needed for vec/hyde)
        vec: list[float] = []
        if use_vec or use_hyde:
            vec = await self._emb.embed(clean_q)

        # [1b] HyDE: generate hypothetical answer → embed it (CF#9)
        hyde_vec: list[float] = []
        if use_hyde and self._llm:
            hyde_text = await self._llm.generate_text(
                _HYDE_PROMPT.format(query=clean_q),
            )
            if hyde_text:
                hyde_vec = await self._emb.embed(hyde_text)
                log.info("hyde_generated", length=len(hyde_text))

        # Accumulators
        n_data: dict[str, dict] = {}
        s_data: dict[str, dict] = {}

        # [2] Build retrieval tasks: (method, kind, coro)
        tasks: list[tuple[str, str, Any]] = []
        escaped = _escape(clean_q)

        if use_vec:
            if self._sem.embed_entity_name:
                tasks.append(("vector", "n",
                              self._vec_neurons("neuron_name_emb", vec, n, n_data)))
            if self._sem.embed_entity_summary:
                tasks.append(("vector", "n",
                              self._vec_neurons("neuron_summary_emb", vec, n, n_data)))
            if self._sem.embed_fact:
                tasks.append(("vector", "s",
                              self._vec_synapses(vec, n, s_data)))

        if use_hyde and hyde_vec:
            hv = hyde_vec
            if self._sem.embed_entity_name:
                tasks.append(("vector", "n",
                              self._vec_neurons("neuron_name_emb", hv, n, n_data)))
            if self._sem.embed_entity_summary:
                tasks.append(("vector", "n",
                              self._vec_neurons("neuron_summary_emb", hv, n, n_data)))
            if self._sem.embed_fact:
                tasks.append(("vector", "s",
                              self._vec_synapses(hv, n, s_data)))

        if use_lex:
            tasks.append(("bm25", "n",
                          self._bm25_neurons(escaped, n, n_data)))
            tasks.append(("bm25", "s",
                          self._bm25_synapses(escaped, n, s_data)))

        # Run parallel
        coros   = [t[2] for t in tasks]
        results = await asyncio.gather(*coros, return_exceptions=True)

        n_ranks: list[list[str]] = []
        s_ranks: list[list[str]] = []
        methods: set[SearchMethod] = set()
        if use_hyde and hyde_vec:
            methods.add(SearchMethod.hyde)

        for (method, kind, _), res in zip(tasks, results, strict=False):
            if isinstance(res, BaseException) or not res:
                continue
            methods.add(SearchMethod(method))
            (n_ranks if kind == "n" else s_ranks).append(res)

        # [2b] BFS (only in hybrid/default mode)
        if use_bfs and self._cfg.bfs_depth > 0:
            center = center_uuid
            if not center and n_ranks:
                center = n_ranks[0][0] if n_ranks[0] else None
            if center:
                bfs = await self._bfs(center, self._cfg.bfs_depth, n, n_data)
                if bfs:
                    n_ranks.append(bfs)
                    methods.add(SearchMethod.bfs)

        # [3] RRF merge
        n_scored = rrf_fuse(n_ranks, self._cfg.rrf_k)
        s_scored = rrf_fuse(s_ranks, self._cfg.rrf_k)

        # [4] Reranker pipeline (CF#14)
        ctx = RerankerContext(
            neuron_data  = n_data,
            config       = self._cfg,
            load_vectors = self._load_embeddings,
            query        = clean_q,
            embedder     = self._emb,
            driver       = self._drv,
            owner_uuid   = self._owner_uuid,
        )
        n_scored = await self._pipeline.run(n_scored, ctx)

        # [5] Build results
        min_sc = self._cfg.min_score
        neurons = [
            ScoredNeuron(neuron=_to_neuron(n_data[uid]), score=sc)
            for uid, sc in n_scored[:n]
            if uid in n_data and sc >= min_sc
        ]
        synapses = [
            ScoredSynapse(
                synapse=_to_synapse(s_data[uid]), score=sc,
                source_name=s_data[uid].get("source_name", ""),
                target_name=s_data[uid].get("target_name", ""),
            )
            for uid, sc in s_scored[:n]
            if uid in s_data and sc >= min_sc
        ]

        # Provenance signals
        sig_uuids = {u for ss in synapses for u in ss.synapse.episodes}
        signals = await self._load_signals(sig_uuids) if sig_uuids else []

        ms = int((time.monotonic() - t0) * 1000)
        log.info("search_executed",
                 query_len=len(query), neurons=len(neurons),
                 synapses=len(synapses), duration_ms=ms)
        Telemetry().record_search(ms, len(neurons))

        return SearchResults(
            neurons=neurons, synapses=synapses, signals=signals,
            methods=sorted(methods), duration_ms=ms,
        )

    # ── Retrieval: vector neurons ─────────────────────────

    async def _vec_neurons(
        self, index: str, vec: list[float], n: int,
        data: dict[str, dict],
    ) -> list[str]:
        try:
            rows = await self._drv.execute_query(
                f"CALL db.index.vector.queryNodes('{index}', $n, $vec) "
                f"YIELD node AS e, score "
                f"WHERE e.expired_at IS NULL "
                f"  AND (e.expires_at IS NULL OR e.expires_at > datetime()) "
                f"RETURN {_N_COLS}, score",
                {"n": n, "vec": vec},
            )
        except Exception:
            return []
        uuids = []
        for r in rows:
            uid = r["uuid"]
            data.setdefault(uid, r)
            uuids.append(uid)
        return uuids

    # ── Retrieval: BM25 neurons ───────────────────────────

    async def _bm25_neurons(
        self, query: str, n: int,
        data: dict[str, dict],
    ) -> list[str]:
        if not query.strip():
            return []
        try:
            rows = await self._drv.execute_query(
                f"CALL db.index.fulltext.queryNodes('neuron_ft', $q) "
                f"YIELD node AS e, score "
                f"RETURN {_N_COLS}, score "
                f"LIMIT $n",
                {"q": query, "n": n},
            )
        except Exception:
            return []
        uuids = []
        for r in rows:
            uid = r["uuid"]
            data.setdefault(uid, r)
            uuids.append(uid)
        return uuids

    # ── Retrieval: vector synapses ────────────────────────

    async def _vec_synapses(
        self, vec: list[float], n: int,
        data: dict[str, dict],
    ) -> list[str]:
        try:
            rows = await self._drv.execute_query(
                f"CALL db.index.vector.queryRelationships('synapse_emb', $n, $vec) "
                f"YIELD relationship AS r, score "
                f"MATCH (s)-[r]->(t) "
                f"WHERE r.expired_at IS NULL "
                f"RETURN {_S_COLS}, score",
                {"n": n, "vec": vec},
            )
        except Exception:
            return []
        uuids = []
        for r in rows:
            uid = r["uuid"]
            data.setdefault(uid, r)
            uuids.append(uid)
        return uuids

    # ── Retrieval: BM25 synapses ──────────────────────────

    async def _bm25_synapses(
        self, query: str, n: int,
        data: dict[str, dict],
    ) -> list[str]:
        if not query.strip():
            return []
        try:
            rows = await self._drv.execute_query(
                f"CALL db.index.fulltext.queryRelationships('synapse_ft', $q) "
                f"YIELD relationship AS r, score "
                f"MATCH (s)-[r]->(t) "
                f"WHERE r.expired_at IS NULL "
                f"RETURN {_S_COLS}, score "
                f"LIMIT $n",
                {"q": query, "n": n},
            )
        except Exception:
            return []
        uuids = []
        for r in rows:
            uid = r["uuid"]
            data.setdefault(uid, r)
            uuids.append(uid)
        return uuids

    # ── Retrieval: BFS graph traversal ────────────────────

    async def _bfs(
        self, center: str, depth: int, n: int,
        data: dict[str, dict],
    ) -> list[str]:
        try:
            rows = await self._drv.execute_query(
                f"MATCH path = (c:Neuron {{uuid: $center}})"
                f"-[:SYNAPSE*1..{depth}]-(e:Neuron) "
                f"WHERE ALL(r IN relationships(path) "
                f"  WHERE r.expired_at IS NULL) "
                f"  AND e.uuid <> $center "
                f"  AND e.expired_at IS NULL "
                f"  AND (e.expires_at IS NULL OR e.expires_at > datetime()) "
                f"WITH DISTINCT e, min(length(path)) AS dist "
                f"RETURN {_N_COLS}, 1.0 / toFloat(dist) AS score "
                f"ORDER BY dist ASC LIMIT $n",
                {"center": center, "n": n},
            )
        except Exception:
            return []
        uuids = []
        for r in rows:
            uid = r["uuid"]
            data.setdefault(uid, r)
            uuids.append(uid)
        return uuids

    # ── Embedding loading (MMR) ────────────────────────────

    async def _load_embeddings(
        self, uuids: list[str],
    ) -> dict[str, list[float]]:
        """Load name_embedding vectors for MMR diversity calc."""
        if not uuids:
            return {}
        try:
            rows = await self._drv.execute_query(
                "MATCH (e:Neuron) WHERE e.uuid IN $uuids "
                "AND e.name_embedding IS NOT NULL "
                "RETURN e.uuid AS uuid, e.name_embedding AS vec",
                {"uuids": uuids},
            )
            return {r["uuid"]: r["vec"] for r in rows}
        except Exception:
            return {}

    # ── Signal loading ────────────────────────────────────

    async def _load_signals(self, uuids: set[str]) -> list[Signal]:
        if not uuids:
            return []
        try:
            rows = await self._drv.execute_query(
                "MATCH (e:Signal) WHERE e.uuid IN $uuids "
                "RETURN e.uuid AS uuid, e.name AS name, "
                "       e.content AS content, "
                "       e.source_type AS source_type, "
                "       e.source_desc AS source_desc, "
                "       e.status AS status, "
                "       e.valid_at AS valid_at, "
                "       e.created_at AS created_at",
                {"uuids": list(uuids)},
            )
            return [_to_signal(r) for r in rows]
        except Exception:
            return []
