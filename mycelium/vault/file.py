"""VaultFile node + STORED_AT edge — graph-level Signal↔file binding (R7.6 fundamental).

Replaces the implicit ``Signal.source_desc`` string-join with an explicit
graph edge:

    (Signal)-[:STORED_AT]->(VaultFile {relative_path: ..., content_hash: ...})

Why a node, not a property: a single file can hold N signals (multi-pass
ingestion), and the file itself has identity beyond any one signal — it has
its own lifecycle (rename, content edit, domain transfer). Promoting it to
a first-class node makes those operations one-edge-flips instead of N
property updates.

Invariants:
- ``relative_path`` is the primary key. Unique per vault root.
- ``content_hash`` mirrors the vault index entry; updated by ``bind_signal_to_file``
  whenever a signal is linked to a file with a known hash.
- ``Signal.source_desc`` is kept in sync (``file:{relative_path}``) for backward
  compatibility with denormalized queries — but the canonical join is the edge.
"""

from __future__ import annotations

import structlog

from mycelium.driver.driver import GraphDriver

log = structlog.get_logger()


def canonical_source_desc(relative_path: str) -> str:
    """Return the canonical ``file:`` form for a vault-relative path."""
    return f"file:{relative_path}"


async def bind_signal_to_file(
    driver:        GraphDriver,
    *,
    signal_uuid:   str,
    relative_path: str,
    content_hash:  str | None = None,
) -> bool:
    """Bind a Signal to a VaultFile — primary signal↔file operation.

    Idempotent. Effects:
      1. MERGE VaultFile by ``relative_path`` (creates if missing).
      2. Update ``content_hash`` and ``updated_at`` on the VaultFile.
      3. MERGE ``(Signal)-[:STORED_AT]->(VaultFile)`` edge.
      4. Mirror ``source_desc = file:{relative_path}`` onto the Signal
         (denormalized, kept until the v3 cleanup release retires
         source_desc-based queries).

    Returns True iff the Signal exists and was bound.
    """
    rows = await driver.execute_query(
        """
        MATCH (sig:Signal {uuid: $uuid})
        MERGE (vf:VaultFile {relative_path: $path})
          ON CREATE SET vf.created_at = datetime()
        SET vf.content_hash = COALESCE($hash, vf.content_hash),
            vf.updated_at   = datetime(),
            sig.source_desc = $canonical,
            sig.content_hash = COALESCE($hash, sig.content_hash)
        MERGE (sig)-[r:STORED_AT]->(vf)
          ON CREATE SET r.created_at = datetime()
        RETURN sig.uuid AS uuid, vf.relative_path AS path
        """,
        {
            "uuid":      signal_uuid,
            "path":      relative_path,
            "hash":      content_hash,
            "canonical": canonical_source_desc(relative_path),
        },
    )
    found = bool(rows)
    if not found:
        log.warning(
            "vault_bind_signal_missing",
            uuid = signal_uuid,
            path = relative_path,
        )
    return found


async def normalize_file_signals(
    driver:        GraphDriver,
    *,
    relative_path: str,
    content_hash:  str | None = None,
) -> int:
    """Normalize *all* signals already linked to a file (multi-pass safety net).

    Walks ``(:VaultFile {relative_path})<-[:STORED_AT]-(sig:Signal)`` and
    pins ``source_desc`` to the canonical form on every signal. Useful when
    additional signals were attached without going through ``bind_signal_to_file``.

    Returns the number of signals normalized.
    """
    rows = await driver.execute_query(
        """
        MATCH (vf:VaultFile {relative_path: $path})<-[:STORED_AT]-(sig:Signal)
        SET sig.source_desc  = $canonical,
            sig.content_hash = COALESCE($hash, sig.content_hash)
        RETURN count(sig) AS n
        """,
        {
            "path":      relative_path,
            "hash":      content_hash,
            "canonical": canonical_source_desc(relative_path),
        },
    )
    return int(rows[0]["n"]) if rows else 0


async def get_file_signals(
    driver:        GraphDriver,
    relative_path: str,
) -> list[str]:
    """Return UUIDs of all Signals linked to ``relative_path``."""
    rows = await driver.execute_query(
        """
        MATCH (:VaultFile {relative_path: $path})<-[:STORED_AT]-(sig:Signal)
        RETURN sig.uuid AS uuid
        ORDER BY sig.created_at
        """,
        {"path": relative_path},
    )
    return [r["uuid"] for r in rows]


async def get_signal_file(
    driver:      GraphDriver,
    signal_uuid: str,
) -> str | None:
    """Return ``relative_path`` of the file linked to a Signal, or None."""
    rows = await driver.execute_query(
        """
        MATCH (:Signal {uuid: $uuid})-[:STORED_AT]->(vf:VaultFile)
        RETURN vf.relative_path AS path
        LIMIT 1
        """,
        {"uuid": signal_uuid},
    )
    return rows[0]["path"] if rows else None
