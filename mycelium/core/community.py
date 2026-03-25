"""Community detection: networkx Louvain with optional LLM naming.

Pipeline: cleanup -> load graph -> louvain -> assign -> create neurons.
No GDS dependency — pure Python via networkx.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog

from mycelium.config import CommunitySettings
from mycelium.driver.neo4j_driver import Neo4jDriver
from mycelium.embedder.client import EmbedderClient
from mycelium.llm.base import LLMBackend

log = structlog.get_logger()


@dataclass
class CommunityReport:
    communities: list[dict[str, Any]] = field(default_factory=list)
    stats:       dict[str, Any]       = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"communities": self.communities, "stats": self.stats}


async def detect_communities(
    drv:      Neo4jDriver,
    llm:      LLMBackend,
    emb:      EmbedderClient,
    settings: CommunitySettings,
) -> CommunityReport:
    """Run full community detection pipeline."""
    t0 = time.monotonic()

    # Early exit: not enough neurons
    cnt = await drv.execute_query(
        "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
        "AND n.neuron_type <> 'community' RETURN count(n) AS c"
    )
    neuron_count = cnt[0]["c"] if cnt else 0
    if neuron_count < 6:
        return CommunityReport(stats={
            "skipped": True, "reason": "too_few_neurons",
            "neurons_processed": neuron_count,
        })

    # 1. Cleanup old communities
    await _cleanup_old(drv)

    # 2. Load graph into networkx + run Louvain
    raw = await _run_louvain(drv, settings.resolution)

    # 3. Group by community_id
    groups = _group_communities(raw, settings.min_community_size)
    if not groups:
        return CommunityReport(stats={
            "algorithm": "louvain",
            "neurons_processed": neuron_count,
            "communities_found": 0,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        })

    # Cap communities
    groups = dict(list(groups.items())[:settings.max_communities])

    # 4. Assign community_id to neurons
    await _assign_ids(drv, groups)

    # 5. LLM naming
    summaries = await _generate_summaries(llm, groups)

    # 6. Create community neurons
    communities = await _create_community_neurons(drv, emb, summaries, groups)

    ms = int((time.monotonic() - t0) * 1000)
    log.info("communities_detected",
             count=len(communities), duration_ms=ms, algorithm="louvain")

    return CommunityReport(
        communities=communities,
        stats={
            "algorithm": "louvain",
            "neurons_processed": neuron_count,
            "communities_found": len(communities),
            "duration_ms": ms,
        },
    )


async def _cleanup_old(drv: Neo4jDriver) -> None:
    """Expire old community neurons, delete MEMBER_OF."""
    await drv.execute_query(
        "MATCH (c:Neuron {neuron_type: 'community'}) WHERE c.expired_at IS NULL "
        "OPTIONAL MATCH (c)<-[m:MEMBER_OF]-()"
        "SET c.expired_at = datetime() "
        "DELETE m"
    )


async def _run_louvain(drv: Neo4jDriver, resolution: float) -> list[dict]:
    """Load graph from Neo4j, run networkx Louvain, return results."""
    import networkx as nx
    from networkx.algorithms.community import louvain_communities

    # Load nodes
    nodes = await drv.execute_query(
        "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
        "AND n.neuron_type <> 'community' "
        "RETURN n.uuid AS uuid, n.name AS name, "
        "  n.neuron_type AS neuron_type, "
        "  coalesce(n.importance, n.confidence) * exp(-n.decay_rate * "
        "    duration.between(n.freshness, datetime()).days) AS weight"
    )
    if not nodes:
        return []

    uuid_to_info = {n["uuid"]: n for n in nodes}

    # Load edges
    edges = await drv.execute_query(
        "MATCH (a:Neuron)-[r:SYNAPSE]-(b:Neuron) "
        "WHERE r.expired_at IS NULL "
        "  AND a.expired_at IS NULL AND b.expired_at IS NULL "
        "  AND a.neuron_type <> 'community' AND b.neuron_type <> 'community' "
        "RETURN DISTINCT a.uuid AS source, b.uuid AS target"
    )

    G = nx.Graph()
    G.add_nodes_from(uuid_to_info.keys())
    for e in edges:
        if e["source"] in uuid_to_info and e["target"] in uuid_to_info:
            G.add_edge(e["source"], e["target"])

    # Run Louvain
    communities = louvain_communities(G, resolution=resolution, seed=42)

    # Convert to flat list
    result = []
    for cid, members in enumerate(communities):
        for uuid in members:
            info = uuid_to_info.get(uuid)
            if info:
                result.append({**info, "community_id": cid})

    return result


def _group_communities(
    raw: list[dict], min_size: int,
) -> dict[int, list[dict]]:
    """Group raw results by community_id, filter by min_size."""
    groups: dict[int, list[dict]] = defaultdict(list)
    for row in raw:
        groups[row["community_id"]].append(row)
    return {
        cid: sorted(members, key=lambda m: m.get("weight", 0), reverse=True)
        for cid, members in groups.items()
        if len(members) >= min_size
    }


async def _assign_ids(
    drv: Neo4jDriver, groups: dict[int, list[dict]],
) -> None:
    """Write community_id into neuron attributes."""
    for cid, members in groups.items():
        uuids = [m["uuid"] for m in members]
        await drv.execute_query(
            "UNWIND $uuids AS uid "
            "MATCH (n:Neuron {uuid: uid}) "
            "SET n.community_id = $cid",
            {"uuids": uuids, "cid": cid},
        )


async def _generate_summaries(
    llm: LLMBackend, groups: dict[int, list[dict]],
) -> dict[int, dict[str, str]]:
    """LLM names and summarizes each community."""
    result: dict[int, dict[str, str]] = {}
    for cid, members in groups.items():
        top = members[:15]
        member_lines = "\n".join(
            f"- {m['name']} ({m.get('neuron_type', '?')})" for m in top
        )
        prompt = (
            "Here are neurons in one knowledge cluster:\n"
            f"{member_lines}\n\n"
            "Give a short name (2-4 words) and 1-2 sentence summary "
            "describing the theme.\n"
            'JSON: {"name": "...", "summary": "..."}'
        )
        try:
            resp = await llm.generate(prompt)
            result[cid] = {
                "name":    resp.get("name", f"Cluster {cid}"),
                "summary": resp.get("summary", ""),
            }
        except Exception as e:
            log.warning("community_naming_failed", cid=cid, error=str(e))
            result[cid] = {
                "name":    f"Cluster {cid}",
                "summary": f"Auto-cluster of {len(members)} neurons",
            }
    return result


async def _create_community_neurons(
    drv: Neo4jDriver,
    emb: EmbedderClient,
    summaries: dict[int, dict[str, str]],
    groups:    dict[int, list[dict]],
) -> list[dict[str, Any]]:
    """Create community Neuron nodes + MEMBER_OF edges."""
    now = datetime.now(UTC).isoformat()
    communities: list[dict[str, Any]] = []

    for cid, info in summaries.items():
        members = groups[cid]
        cuuid   = str(uuid4())

        name_emb    = await emb.embed(info["name"])
        summary_emb = await emb.embed(info["summary"]) if info["summary"] else name_emb

        await drv.execute_query(
            "CREATE (c:Neuron {"
            "  uuid: $uuid, name: $name, neuron_type: 'community',"
            "  summary: $summary, name_embedding: $name_emb,"
            "  summary_embedding: $sum_emb,"
            "  confidence: 1.0, decay_rate: 0.001, confirmations: 0,"
            "  freshness: datetime($now), created_at: datetime($now),"
            "  attributes: $attrs"
            "})",
            {
                "uuid": cuuid, "name": info["name"],
                "summary": info["summary"],
                "name_emb": name_emb, "sum_emb": summary_emb,
                "now": now,
                "attrs": json.dumps({"member_count": len(members)}),
            },
        )

        member_uuids = [m["uuid"] for m in members]
        await drv.execute_query(
            "UNWIND $member_uuids AS mid "
            "MATCH (c:Neuron {uuid: $community_uuid}), (m:Neuron {uuid: mid}) "
            "CREATE (m)-[:MEMBER_OF]->(c)",
            {"member_uuids": member_uuids, "community_uuid": cuuid},
        )

        top_members = [
            {"name": m["name"], "type": m.get("neuron_type", "")}
            for m in members[:5]
        ]
        communities.append({
            "community_id": cid, "uuid": cuuid,
            "name": info["name"], "summary": info["summary"],
            "member_count": len(members), "top_members": top_members,
        })

    return communities
