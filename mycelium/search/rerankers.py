"""Pluggable reranker pipeline: Protocol + implementations + factory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from mycelium.config import SearchSettings
from mycelium.utils.decay import effective_weight

if TYPE_CHECKING:
    from mycelium.driver.driver import GraphDriver
    from mycelium.embedder.client import EmbedderClient

log = structlog.get_logger()


# ── Context ──────────────────────────────────────────────


@dataclass
class RerankerContext:
    """Shared context passed through the reranker pipeline."""

    neuron_data:  dict[str, dict]
    config:       SearchSettings
    load_vectors: Callable[[list[str]], Awaitable[dict[str, list[float]]]] | None = None
    query:        str                    = ""      # R2.1
    embedder:     EmbedderClient | None  = None    # R2.1
    driver:       GraphDriver | None     = None    # R2.2
    owner_uuid:   str                    = ""      # R2.2


# ── Protocol ─────────────────────────────────────────────


@runtime_checkable
class Reranker(Protocol):
    """Reranker stage interface."""

    name: str

    async def rerank(
        self,
        items:   list[tuple[str, float]],
        context: RerankerContext,
    ) -> list[tuple[str, float]]: ...


# ── RRF (standalone fusion, not a reranker) ──────────────


def rrf_fuse(
    ranked_lists: list[list[str]],
    k:            int = 60,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion → sorted (uuid, score) desc."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, uid in enumerate(ranked, 1):
            scores[uid] = scores.get(uid, 0.0) + 1.0 / (rank + k)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ── Helpers ──────────────────────────────────────────────


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


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# ── Implementations ─────────────────────────────────────


class DecayReranker:
    """Multiply RRF score by neuron effective weight (R5.1: importance * recency)."""

    name = "decay"

    async def rerank(
        self,
        items:   list[tuple[str, float]],
        context: RerankerContext,
    ) -> list[tuple[str, float]]:
        out = []
        for uid, score in items:
            d   = context.neuron_data.get(uid, {})
            imp = (d.get("propagated_confidence")
                   or d.get("importance")
                   or d.get("confidence") or 1.0)
            ew  = effective_weight(
                imp,
                d.get("decay_rate") or 0.008,
                _to_dt(d.get("freshness")),
            )
            out.append((uid, score * ew))
        return sorted(out, key=lambda x: x[1], reverse=True)


# (max_rank, retrieval_weight, reranker_weight)
_BLEND_TIERS: list[tuple[int, float, float]] = [
    (3,  0.75, 0.25),
    (10, 0.60, 0.40),
]
_BLEND_DEFAULT = (0.40, 0.60)


class PositionBlendReranker:
    """Blend retrieval and reranker scores with position-aware weights."""

    name = "blend"

    def __init__(self) -> None:
        self._pre_decay: list[tuple[str, float]] = []

    async def rerank(
        self,
        items:   list[tuple[str, float]],
        context: RerankerContext,
    ) -> list[tuple[str, float]]:
        if not context.config.blend_enabled:
            return items

        # items = post-decay; _pre_decay = pre-decay (set by pipeline)
        retrieval = self._pre_decay or items
        rerank_map = {uid: sc for uid, sc in items}
        blended = []
        for rank, (uid, ret_sc) in enumerate(retrieval, 1):
            rnk_sc = rerank_map.get(uid, ret_sc)
            w_ret, w_rnk = _BLEND_DEFAULT
            for max_rank, wr, wk in _BLEND_TIERS:
                if rank <= max_rank:
                    w_ret, w_rnk = wr, wk
                    break
            blended.append((uid, w_ret * ret_sc + w_rnk * rnk_sc))
        return sorted(blended, key=lambda x: x[1], reverse=True)


class MMRReranker:
    """Maximal Marginal Relevance: diversity-aware reranking."""

    name = "mmr"

    async def rerank(
        self,
        items:   list[tuple[str, float]],
        context: RerankerContext,
    ) -> list[tuple[str, float]]:
        if not context.config.mmr_enabled:
            return items
        if not items or not context.load_vectors:
            return items

        top_k = context.config.top_k
        lam   = context.config.mmr_lambda
        uids  = [uid for uid, _ in items[:top_k * 2]]
        vectors = await context.load_vectors(uids)

        remaining = dict(items)
        selected: list[tuple[str, float]] = []

        while remaining and len(selected) < top_k:
            best_uid   = ""
            best_score = -1.0

            for uid, rel_score in remaining.items():
                vec = vectors.get(uid)
                if not vec:
                    max_sim = 0.0
                else:
                    max_sim = max(
                        (_cosine_sim(vec, vectors[s_uid])
                         for s_uid, _ in selected
                         if s_uid in vectors),
                        default=0.0,
                    )
                mmr = lam * rel_score - (1 - lam) * max_sim
                if mmr > best_score:
                    best_score = mmr
                    best_uid   = uid

            if best_uid:
                selected.append((best_uid, remaining.pop(best_uid)))
            else:
                break

        return selected


class CrossEncoderReranker:
    """R2.1: Neural reranker via DeepInfra bge-reranker-v2-m3."""

    name = "cross_encoder"

    async def rerank(
        self,
        items:   list[tuple[str, float]],
        context: RerankerContext,
    ) -> list[tuple[str, float]]:
        if not context.config.cross_encoder_enabled:
            return items
        if not context.embedder or not context.query:
            return items

        top_n = context.config.cross_encoder_top_n
        head  = items[:top_n]
        tail  = items[top_n:]

        # Build documents from neuron summaries/names
        docs: list[str] = []
        for uid, _ in head:
            d = context.neuron_data.get(uid, {})
            text = d.get("summary") or d.get("name") or uid
            docs.append(text)

        if not docs:
            return items

        ranked = await context.embedder.rerank(context.query, docs, top_n=top_n)
        if not ranked:
            return items  # API failed → graceful skip

        # Map back: ranked = [(doc_index, score)]
        reranked = [(head[idx][0], score) for idx, score in ranked if idx < len(head)]

        # Append tail with decaying scores below minimum reranked score
        min_score = reranked[-1][1] if reranked else 0.0
        for i, (uid, _) in enumerate(tail):
            reranked.append((uid, min_score * 0.9 ** (i + 1)))

        log.info("cross_encoder_reranked", top_n=len(head), returned=len(ranked))
        return reranked


class NodeDistanceReranker:
    """R2.2: Rank by graph proximity to owner neuron (BFS distance)."""

    name = "node_distance"

    async def rerank(
        self,
        items:   list[tuple[str, float]],
        context: RerankerContext,
    ) -> list[tuple[str, float]]:
        if not context.config.node_distance_enabled:
            return items
        if not context.driver or not context.owner_uuid:
            return items
        if not items:
            return items

        uuids     = [uid for uid, _ in items]
        max_depth = context.config.node_distance_max_depth
        weight    = context.config.node_distance_weight

        # BFS: shortest path from owner to each result neuron
        try:
            rows = await context.driver.execute_query(
                "UNWIND $uuids AS target_uuid "
                "MATCH (owner:Neuron {uuid: $owner}), (t:Neuron {uuid: target_uuid}) "
                "OPTIONAL MATCH p = shortestPath((owner)-[:SYNAPSE*..%d]-(t)) "
                "WHERE ALL(r IN relationships(p) WHERE r.expired_at IS NULL) "
                "RETURN target_uuid AS uuid, "
                "       CASE WHEN p IS NULL THEN %d + 1 "
                "            ELSE length(p) END AS dist"
                % (max_depth, max_depth),
                {"uuids": uuids, "owner": context.owner_uuid},
            )
        except Exception:
            return items  # graceful skip

        dist_map = {r["uuid"]: r["dist"] for r in rows}

        out = []
        for uid, score in items:
            d     = dist_map.get(uid, max_depth + 1)
            # boost = 1.0 (distance 1) down to 0.0 (beyond max_depth)
            boost = max(0.0, 1.0 - d / (max_depth + 1))
            out.append((uid, score * (1.0 + weight * boost)))

        result = sorted(out, key=lambda x: x[1], reverse=True)
        log.info("node_distance_reranked", items=len(result))
        return result


# ── Pipeline ─────────────────────────────────────────────


class RerankerPipeline:
    """Sequential reranker chain."""

    def __init__(self, stages: list[Reranker]) -> None:
        self._stages = stages

    async def run(
        self,
        items:   list[tuple[str, float]],
        context: RerankerContext,
    ) -> list[tuple[str, float]]:
        result = items
        for stage in self._stages:
            # PositionBlendReranker needs pre-decay scores
            if isinstance(stage, PositionBlendReranker):
                stage._pre_decay = items  # original RRF scores
            result = await stage.rerank(result, context)
        return result


# ── Factory ──────────────────────────────────────────────

_REGISTRY: dict[str, type] = {
    "decay":         DecayReranker,
    "blend":         PositionBlendReranker,
    "mmr":           MMRReranker,
    "cross_encoder": CrossEncoderReranker,
    "node_distance": NodeDistanceReranker,
}


def make_pipeline(cfg: SearchSettings) -> RerankerPipeline:
    """Build reranker pipeline from config chain."""
    stages: list[Reranker] = []
    for name in cfg.reranker_chain:
        cls = _REGISTRY.get(name)
        if cls:
            stages.append(cls())
        else:
            log.warning("unknown_reranker", name=name)
    return RerankerPipeline(stages)
