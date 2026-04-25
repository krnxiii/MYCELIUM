"""Cypher queries for graph visualization."""

from mycelium.utils.decay import cypher_effective_weight

# ── Graph (full) ─────────────────────────────────────────

GRAPH_NODES = f"""
MATCH (e:Neuron)
WITH e, {cypher_effective_weight("e")} AS ew
WHERE ew > 0.05
RETURN e.uuid          AS id,
       e.name          AS label,
       e.neuron_type   AS type,
       e.confidence    AS conf,
       e.confirmations AS cnt,
       toString(e.freshness) AS freshness,
       ew
"""

GRAPH_EDGES = """
MATCH (a:Neuron)-[f:SYNAPSE]->(b:Neuron)
WHERE f.expired_at IS NULL
RETURN f.uuid       AS id,
       a.uuid       AS source,
       b.uuid       AS target,
       f.fact        AS fact,
       f.relation    AS relation,
       f.confidence  AS conf
"""

GRAPH_STATS = """
OPTIONAL MATCH (e:Neuron) WITH count(e) AS neurons
OPTIONAL MATCH ()-[f:SYNAPSE]->() WHERE f.expired_at IS NULL
RETURN neurons, count(f) AS synapses
"""

# ── Neuron detail ───────────────────────────────────────

NEURON_SYNAPSES = """
MATCH (e:Neuron {uuid: $uuid})-[f:SYNAPSE]-(other:Neuron)
WHERE f.expired_at IS NULL
RETURN f.fact        AS fact,
       f.relation    AS relation,
       f.confidence  AS conf,
       other.name    AS other,
       other.uuid    AS other_uuid,
       startNode(f).uuid = $uuid AS outgoing,
       coalesce(f.episodes, []) AS episodes
ORDER BY f.confidence DESC
"""

SIGNALS_BY_IDS = """
MATCH (sig:Signal)
WHERE sig.uuid IN $uuids
RETURN sig.uuid        AS id,
       sig.name        AS name,
       sig.source_type AS source,
       toString(sig.created_at) AS created
ORDER BY sig.created_at DESC
"""

# ── Neighbors (expand on double-click) ───────────────────

NEIGHBOR_NODES = f"""
MATCH (c:Neuron {{uuid: $uuid}})-[:SYNAPSE*1..2]-(n:Neuron)
WHERE n.uuid <> $uuid
WITH DISTINCT n, {cypher_effective_weight("n")} AS ew
WHERE ew > 0.05
RETURN n.uuid          AS id,
       n.name          AS label,
       n.neuron_type   AS type,
       n.confidence    AS conf,
       n.confirmations AS cnt,
       toString(n.freshness) AS freshness,
       ew
"""

NEIGHBOR_EDGES = """
MATCH (c:Neuron {uuid: $uuid})-[:SYNAPSE*1..2]-(n:Neuron)
WITH collect(DISTINCT n.uuid) + [$uuid] AS scope
MATCH (a:Neuron)-[f:SYNAPSE]->(b:Neuron)
WHERE a.uuid IN scope AND b.uuid IN scope
  AND f.expired_at IS NULL
RETURN f.uuid       AS id,
       a.uuid       AS source,
       b.uuid       AS target,
       f.fact        AS fact,
       f.relation    AS relation,
       f.confidence  AS conf
"""
