"""Neo4j GraphDriver implementation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import structlog
from neo4j import AsyncGraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable

from mycelium.config import Neo4jSettings
from mycelium.driver.driver import GraphDriver, TxExecute
from mycelium.exceptions import ConnectionError, SchemaError

T = TypeVar("T")

log = structlog.get_logger()

VECTOR_DIMS = 1024

# ── V2 Schema (idempotent) ──────────────────────────────────────────

CONSTRAINTS = [
    "CREATE CONSTRAINT neuron_uuid IF NOT EXISTS "
    "FOR (n:Neuron) REQUIRE n.uuid IS UNIQUE",

    "CREATE CONSTRAINT signal_uuid IF NOT EXISTS "
    "FOR (n:Signal) REQUIRE n.uuid IS UNIQUE",
]

_HNSW = (
    f"`vector.dimensions`: {VECTOR_DIMS}, "
    f"`vector.similarity_function`: 'cosine', "
    f"`vector.hnsw.m`: 16, "
    f"`vector.hnsw.ef_construction`: 400"
)

VECTOR_INDEXES = [
    f"CREATE VECTOR INDEX neuron_name_emb IF NOT EXISTS "
    f"FOR (n:Neuron) ON (n.name_embedding) "
    f"OPTIONS {{indexConfig: {{{_HNSW}}}}}",

    f"CREATE VECTOR INDEX neuron_summary_emb IF NOT EXISTS "
    f"FOR (n:Neuron) ON (n.summary_embedding) "
    f"OPTIONS {{indexConfig: {{{_HNSW}}}}}",

    f"CREATE VECTOR INDEX signal_content_emb IF NOT EXISTS "
    f"FOR (n:Signal) ON (n.content_embedding) "
    f"OPTIONS {{indexConfig: {{{_HNSW}}}}}",

    f"CREATE VECTOR INDEX signal_file_emb IF NOT EXISTS "
    f"FOR (n:Signal) ON (n.file_embedding) "
    f"OPTIONS {{indexConfig: {{{_HNSW}}}}}",

    f"CREATE VECTOR INDEX synapse_emb IF NOT EXISTS "
    f"FOR ()-[r:SYNAPSE]-() ON (r.fact_embedding) "
    f"OPTIONS {{indexConfig: {{{_HNSW}}}}}",
]

TEXT_INDEXES = [
    "CREATE FULLTEXT INDEX neuron_ft IF NOT EXISTS "
    "FOR (n:Neuron) ON EACH [n.name, n.summary]",

    "CREATE FULLTEXT INDEX signal_ft IF NOT EXISTS "
    "FOR (n:Signal) ON EACH [n.content]",

    "CREATE FULLTEXT INDEX synapse_ft IF NOT EXISTS "
    "FOR ()-[r:SYNAPSE]-() ON EACH [r.fact]",
]

PROPERTY_INDEXES = [
    "CREATE INDEX neuron_type_idx IF NOT EXISTS "
    "FOR (n:Neuron) ON (n.neuron_type)",

    "CREATE INDEX neuron_freshness_idx IF NOT EXISTS "
    "FOR (n:Neuron) ON (n.freshness)",

    "CREATE INDEX signal_status_idx IF NOT EXISTS "
    "FOR (n:Signal) ON (n.status)",

    "CREATE INDEX signal_created_idx IF NOT EXISTS "
    "FOR (n:Signal) ON (n.created_at)",

    "CREATE INDEX synapse_expired_idx IF NOT EXISTS "
    "FOR ()-[r:SYNAPSE]-() ON (r.expired_at)",
]

ALL_SCHEMA = CONSTRAINTS + VECTOR_INDEXES + TEXT_INDEXES + PROPERTY_INDEXES

# ── One-time migrations (idempotent) ─────────────────────────────────
MIGRATIONS = [
    # v2.1: entity_type → neuron_type rename (code changed, DB not)
    "MATCH (n:Neuron) WHERE n.entity_type IS NOT NULL "
    "SET n.neuron_type = n.entity_type REMOVE n.entity_type",

    # v2.2: backfill file_embedding from content_embedding for file signals
    "MATCH (s:Signal) "
    "WHERE s.source_type = 'file' "
    "  AND s.content_embedding IS NOT NULL "
    "  AND s.file_embedding IS NULL "
    "SET s.file_embedding = s.content_embedding",
]

EXPECTED_CONSTRAINTS = {"neuron_uuid", "signal_uuid"}
EXPECTED_INDEXES     = {
    "neuron_name_emb", "neuron_summary_emb", "signal_content_emb",
    "signal_file_emb", "synapse_emb",
    "neuron_ft", "signal_ft", "synapse_ft",
    "neuron_type_idx", "neuron_freshness_idx",
    "signal_status_idx", "signal_created_idx",
    "synapse_expired_idx",
}


# ── Driver ───────────────────────────────────────────────────────────


class Neo4jDriver(GraphDriver):
    """Neo4j >= 5.18 driver with async sessions."""

    def __init__(self, settings: Neo4jSettings) -> None:
        self._driver = AsyncGraphDatabase.driver(
            settings.uri,
            auth=(settings.user, settings.password.get_secret_value()),
            max_connection_pool_size=settings.pool_size,
            connection_acquisition_timeout=settings.pool_timeout,
        )
        self._db = settings.database

    async def execute_query(
        self,
        query:  str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        async with self._driver.session(database=self._db) as session:
            result = await session.run(query, params or {})
            return await result.data()

    async def run_in_transaction(
        self,
        work: Callable[[TxExecute], Awaitable[T]],
    ) -> T:
        async with self._driver.session(database=self._db) as session:
            async def _tx_work(tx: Any) -> T:
                async def _execute(
                    query:  str,
                    params: dict[str, Any] | None = None,
                ) -> list[dict[str, Any]]:
                    result = await tx.run(query, params or {})
                    return await result.data()
                return await work(_execute)
            return await session.execute_write(_tx_work)

    async def build_indices(self) -> None:
        """Create all constraints + indexes (idempotent)."""
        for stmt in ALL_SCHEMA:
            try:
                await self.execute_query(stmt)
            except Exception as e:
                raise SchemaError(f"Failed: {stmt[:80]}: {e}") from e
        for stmt in MIGRATIONS:
            try:
                await self.execute_query(stmt)
            except Exception:
                pass  # migration already applied or no matching nodes
        log.info("schema_initialized", count=len(ALL_SCHEMA))

    async def verify_schema(self) -> tuple[bool, list[str]]:
        """Check expected vs actual constraints/indexes."""
        rows = await self.execute_query(
            "SHOW CONSTRAINTS YIELD name RETURN name"
        )
        actual_c = {r["name"] for r in rows}

        rows = await self.execute_query(
            "SHOW INDEXES YIELD name RETURN name"
        )
        actual_i = {r["name"] for r in rows}

        missing = sorted(
            (EXPECTED_CONSTRAINTS - actual_c) | (EXPECTED_INDEXES - actual_i)
        )
        ok = not missing
        log.info("schema_verified", ok=ok, missing=missing)
        return ok, missing

    async def health_check(self) -> bool:
        """Verify connectivity."""
        try:
            rows = await self.execute_query("RETURN 1 AS ok")
            return bool(rows and rows[0].get("ok") == 1)
        except (ServiceUnavailable, AuthError, OSError):
            return False

    async def close(self) -> None:
        await self._driver.close()
        log.info("neo4j_closed")

    async def __aenter__(self) -> Neo4jDriver:
        if not await self.health_check():
            raise ConnectionError("Neo4j health check failed")
        return self
