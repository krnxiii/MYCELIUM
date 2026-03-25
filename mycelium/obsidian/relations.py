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
# all signals from the same file share source_desc = "file:{relative_path}"
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
RETURN DISTINCT n.name AS name, n.neuron_type AS type, n.confidence AS confidence
ORDER BY name
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
    name:       str
    type:       str
    confidence: float


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
            name       = r["name"],
            type       = r["type"] or "",
            confidence = r.get("confidence", 0.0) or 0.0,
        )
        for r in rows
    ]
