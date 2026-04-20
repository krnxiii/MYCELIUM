"""Compute file-to-file relations via shared neurons in Neo4j."""

from __future__ import annotations

from dataclasses import dataclass

from mycelium.driver.driver import GraphDriver


@dataclass
class RelatedFile:
    source_desc:  str
    shared:       list[str]   # neuron names
    strength:     int         # count of shared neurons


# Query by source_desc (not signal_uuid) — handles multi-section files:
# all signals from the same file share the canonical source_desc
# "file:{relative_path}" (normalized by Signal validator in core/models.py).
_RELATED_QUERY = """\
MATCH (sig:Signal)-[:MENTIONS]->(n:Neuron)<-[:MENTIONS]-(other:Signal)
WHERE sig.source_desc = $source_desc
  AND other.source_desc <> sig.source_desc
  AND other.source_type = 'file'
  {expired_filter}
WITH other.source_desc AS other_desc,
     collect(DISTINCT n.name) AS shared,
     count(DISTINCT n) AS strength
RETURN other_desc AS source_desc, shared, strength
ORDER BY strength DESC
LIMIT $max_related
"""

_NEURONS_QUERY = """\
MATCH (sig:Signal)-[:MENTIONS]->(n:Neuron)
WHERE sig.source_desc = $source_desc
  AND n.expired_at IS NULL
RETURN DISTINCT n.uuid AS uuid, n.name AS name,
                n.neuron_type AS type, n.confidence AS confidence
ORDER BY name
"""

_SIGNAL_META_QUERY = """\
MATCH (s:Signal {uuid: $signal_uuid})
RETURN s.source_type  AS source_type,
       s.source_desc  AS source_desc,
       s.domain       AS domain,
       s.status       AS status,
       s.content_hash AS content_hash,
       s.chunk_count  AS chunk_count,
       toString(s.valid_at)   AS valid_at,
       toString(s.created_at) AS created_at
"""


async def get_related(
    driver:       GraphDriver,
    source_desc:  str,
    *,
    min_shared:      int  = 1,
    max_related:     int  = 20,
    include_expired: bool = False,
) -> list[RelatedFile]:
    """Find files sharing neurons with the given file (by source_desc)."""
    expired_filter = "" if include_expired else "AND n.expired_at IS NULL"
    query = _RELATED_QUERY.format(expired_filter=expired_filter)

    rows = await driver.execute_query(query, {
        "source_desc": source_desc,
        "max_related": max_related,
    })

    return [
        RelatedFile(
            source_desc = r["source_desc"] or "",
            shared      = r["shared"],
            strength    = r["strength"],
        )
        for r in rows
        if r["strength"] >= min_shared
    ]


@dataclass
class NeuronInfo:
    uuid:       str
    name:       str
    type:       str
    confidence: float


@dataclass
class SignalMeta:
    """Signal properties for document frontmatter enrichment."""

    source_type:  str = ""
    source_desc:  str = ""
    domain:       str = ""
    status:       str = ""
    content_hash: str = ""
    chunk_count:  int = 0
    valid_at:     str = ""
    created_at:   str = ""


async def get_signal_meta(
    driver:      GraphDriver,
    signal_uuid: str,
) -> SignalMeta | None:
    """Fetch Signal properties for frontmatter projection."""
    rows = await driver.execute_query(_SIGNAL_META_QUERY, {
        "signal_uuid": signal_uuid,
    })
    if not rows:
        return None
    r = rows[0]
    return SignalMeta(
        source_type  = r.get("source_type")  or "",
        source_desc  = r.get("source_desc")  or "",
        domain       = r.get("domain")       or "",
        status       = r.get("status")       or "",
        content_hash = r.get("content_hash") or "",
        chunk_count  = int(r.get("chunk_count") or 0),
        valid_at     = r.get("valid_at")     or "",
        created_at   = r.get("created_at")   or "",
    )


@dataclass
class SourceSignal:
    """Signal that mentions a neuron — for neuron→source backlinks."""

    source_desc: str
    name:        str
    valid_at:    str = ""


_SOURCE_SIGNALS_QUERY = """\
MATCH (sig:Signal)-[:MENTIONS]->(n:Neuron {uuid: $neuron_uuid})
WHERE sig.source_type = 'file'
RETURN DISTINCT sig.source_desc AS source_desc,
                sig.name        AS name,
                toString(sig.valid_at) AS valid_at
ORDER BY valid_at DESC
LIMIT $max_sources
"""


async def get_source_signals(
    driver:      GraphDriver,
    neuron_uuid: str,
    *,
    max_sources: int = 20,
) -> list[SourceSignal]:
    """Get file-signals that mention this neuron (for neuron→doc backlinks)."""
    rows = await driver.execute_query(_SOURCE_SIGNALS_QUERY, {
        "neuron_uuid": neuron_uuid,
        "max_sources": max_sources,
    })
    return [
        SourceSignal(
            source_desc = r["source_desc"] or "",
            name        = r["name"] or "",
            valid_at    = r["valid_at"] or "",
        )
        for r in rows
    ]


@dataclass
class SimilarFile:
    source_desc: str
    score:       float


_SIMILAR_QUERY = """\
MATCH (sig:Signal)
WHERE sig.source_desc = $source_desc
  AND sig.source_type = 'file'
  AND sig.file_embedding IS NOT NULL
WITH sig.file_embedding AS vec LIMIT 1
CALL db.index.vector.queryNodes('signal_file_emb', $top_n, vec)
YIELD node AS other, score
WHERE other.source_desc <> $source_desc
  AND other.source_type = 'file'
  AND score >= $threshold
RETURN DISTINCT other.source_desc AS source_desc, max(score) AS score
ORDER BY score DESC
LIMIT $max_similar
"""


async def get_similar(
    driver:      GraphDriver,
    source_desc: str,
    *,
    threshold:   float = 0.75,
    max_similar: int   = 10,
) -> list[SimilarFile]:
    """Find files with similar content via cosine on file_embedding."""
    rows = await driver.execute_query(_SIMILAR_QUERY, {
        "source_desc": source_desc,
        "top_n":       max_similar * 3,
        "threshold":   threshold,
        "max_similar":  max_similar,
    })
    return [
        SimilarFile(
            source_desc = r["source_desc"] or "",
            score       = round(r["score"], 3),
        )
        for r in rows
    ]


async def get_neurons(
    driver:      GraphDriver,
    source_desc: str,
) -> list[NeuronInfo]:
    """Get all neurons from all signals of a file (by source_desc)."""
    rows = await driver.execute_query(_NEURONS_QUERY, {
        "source_desc": source_desc,
    })
    return [
        NeuronInfo(
            uuid       = r.get("uuid") or "",
            name       = r["name"],
            type       = r["type"] or "",
            confidence = r.get("confidence", 0.0) or 0.0,
        )
        for r in rows
    ]
