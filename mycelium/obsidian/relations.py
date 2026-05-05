"""Compute file-to-file relations via shared neurons in Neo4j."""

from __future__ import annotations

from dataclasses import dataclass

from mycelium.driver.driver import GraphDriver


@dataclass
class RelatedFile:
    relative_path: str
    shared:        list[str]   # neuron names
    strength:      int         # count of shared neurons


# R7.6 fundamental: queries traverse the explicit
# (Signal)-[:STORED_AT]->(VaultFile) edge instead of joining on
# the implicit source_desc string. This makes multi-pass ingestion
# (N signals → 1 file) work uniformly: any signal whose VaultFile
# matches the path contributes its mentions.
_RELATED_QUERY = """\
MATCH (vf:VaultFile {relative_path: $relative_path})
      <-[:STORED_AT]-(sig:Signal)-[:MENTIONS]->(n:Neuron)
      <-[:MENTIONS]-(other:Signal)-[:STORED_AT]->(other_vf:VaultFile)
WHERE other_vf.relative_path <> $relative_path
  __EXPIRED_FILTER__
WITH other_vf.relative_path AS other_path,
     collect(DISTINCT n.name) AS shared,
     count(DISTINCT n) AS strength
RETURN other_path AS relative_path, shared, strength
ORDER BY strength DESC
LIMIT $max_related
"""

_NEURONS_QUERY = """\
MATCH (:VaultFile {relative_path: $relative_path})
      <-[:STORED_AT]-(:Signal)-[:MENTIONS]->(n:Neuron)
WHERE n.expired_at IS NULL
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
    driver:        GraphDriver,
    relative_path: str,
    *,
    min_shared:      int  = 1,
    max_related:     int  = 20,
    include_expired: bool = False,
) -> list[RelatedFile]:
    """Find files sharing neurons with the given file (graph-edge join)."""
    expired_filter = "" if include_expired else "AND n.expired_at IS NULL"
    query = _RELATED_QUERY.replace("__EXPIRED_FILTER__", expired_filter)

    rows = await driver.execute_query(query, {
        "relative_path": relative_path,
        "max_related":   max_related,
    })

    return [
        RelatedFile(
            relative_path = r["relative_path"] or "",
            shared        = r["shared"],
            strength      = r["strength"],
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

    relative_path: str
    name:          str
    valid_at:      str = ""


_SOURCE_SIGNALS_QUERY = """\
MATCH (sig:Signal)-[:MENTIONS]->(:Neuron {uuid: $neuron_uuid})
MATCH (sig)-[:STORED_AT]->(vf:VaultFile)
RETURN DISTINCT vf.relative_path AS relative_path,
                sig.name         AS name,
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
            relative_path = r["relative_path"] or "",
            name          = r["name"] or "",
            valid_at      = r["valid_at"] or "",
        )
        for r in rows
    ]


@dataclass
class SimilarFile:
    relative_path: str
    score:         float


_SIMILAR_QUERY = """\
MATCH (vf:VaultFile {relative_path: $relative_path})<-[:STORED_AT]-(sig:Signal)
WHERE sig.file_embedding IS NOT NULL
WITH sig.file_embedding AS vec LIMIT 1
CALL db.index.vector.queryNodes('signal_file_emb', $top_n, vec)
YIELD node AS other, score
MATCH (other)-[:STORED_AT]->(other_vf:VaultFile)
WHERE other_vf.relative_path <> $relative_path
  AND score >= $threshold
RETURN DISTINCT other_vf.relative_path AS relative_path, max(score) AS score
ORDER BY score DESC
LIMIT $max_similar
"""


async def get_similar(
    driver:        GraphDriver,
    relative_path: str,
    *,
    threshold:   float = 0.75,
    max_similar: int   = 10,
) -> list[SimilarFile]:
    """Find files with similar content via cosine on file_embedding."""
    rows = await driver.execute_query(_SIMILAR_QUERY, {
        "relative_path": relative_path,
        "top_n":         max_similar * 3,
        "threshold":     threshold,
        "max_similar":   max_similar,
    })
    return [
        SimilarFile(
            relative_path = r["relative_path"] or "",
            score         = round(r["score"], 3),
        )
        for r in rows
    ]


async def get_neurons(
    driver:        GraphDriver,
    relative_path: str,
) -> list[NeuronInfo]:
    """Get all neurons from all signals of a file (via STORED_AT edge)."""
    rows = await driver.execute_query(_NEURONS_QUERY, {
        "relative_path": relative_path,
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
