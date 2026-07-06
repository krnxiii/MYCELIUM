"""vault_compact — reconcile vault disk ↔ .index.json ↔ graph Signals.

Detects three kinds of drift:
  1. orphan_index   — index entry, file missing on disk     → DELETE entry (safe)
  2. orphan_files   — file on disk, no index entry          → REPORT only
  3. dangling       — index has signal_uuid, no Signal node → REPORT only

Only orphan_index is auto-fixed. The other two require user judgment
(file may be intentionally added; signal may have been deleted intentionally).

Skip rules match obsidian sync: directory parts starting with '.' or '_'
are excluded (covers _AGENT/, _WIKI/, .index.json, .git, etc.).
"""

from __future__ import annotations

import time

import structlog

from mycelium.config import TendSettings, VaultSettings
from mycelium.driver.neo4j_driver import Neo4jDriver
from mycelium.tend.stages.decay import StageResult
from mycelium.vault.storage import VaultIndexCorruptError, VaultStorage

log = structlog.get_logger()

_SKIP_PREFIXES = (".", "_")


async def vault_compact(
    drv:      Neo4jDriver,
    *,
    vault:    VaultSettings | None = None,
    settings: TendSettings  | None = None,
    dry_run:  bool                  = False,
) -> StageResult:
    """Reconcile vault disk, index, and graph. Auto-fix only orphan_index entries."""
    s    = settings or TendSettings()
    v    = vault    or VaultSettings()
    res  = StageResult(name="vault_compact", dry_run=dry_run)
    t0   = time.monotonic()

    try:
        root = v.path

        if not root.exists():
            res.extra["skipped"] = "vault_root_missing"
            res.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return res

        # 1. Load index through the single owner (VaultStorage) — raises
        # on a corrupt index instead of treating it as empty (audit M34).
        storage = VaultStorage(v)
        try:
            index: dict[str, dict] = storage._load_index()
        except VaultIndexCorruptError as exc:
            res.errors.append(f"index_unreadable: {exc}")
            res.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return res

        # 2. Walk disk for active files (skip hidden / underscore dirs)
        disk_files: set[str] = set()
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel_parts = p.relative_to(root).parts
            except ValueError:
                continue
            if any(part.startswith(_SKIP_PREFIXES) for part in rel_parts):
                continue
            disk_files.add("/".join(rel_parts))

        index_paths = set(index.keys())

        # Orphan-index detection checks the filesystem directly, not the
        # skip-filtered walk: storage buckets like CORTEX/_other/ are
        # legitimately indexed but excluded by the walk's '_' rule — deriving
        # orphans from the walk alone would drop live bindings. Never delete
        # an entry whose file exists.
        orphan_index = sorted(
            path for path in index_paths - disk_files
            if not (root / path).is_file()
        )
        orphan_files = sorted(disk_files - index_paths)   # on disk, not in index

        # 3. Optional graph cross-check: index entries pointing at missing Signals
        dangling: list[str] = []
        if s.vault_check_graph and index:
            uuids = [
                e["signal_uuid"] for e in index.values()
                if isinstance(e, dict) and e.get("signal_uuid")
            ]
            if uuids:
                rows = await drv.execute_query(
                    "UNWIND $uuids AS u "
                    "OPTIONAL MATCH (s:Signal {uuid: u}) "
                    "WITH u, s WHERE s IS NULL "
                    "RETURN collect(u) AS missing",
                    {"uuids": uuids},
                )
                missing_set = set(rows[0]["missing"] if rows else [])
                dangling    = sorted(
                    path for path, e in index.items()
                    if isinstance(e, dict)
                    and e.get("signal_uuid") in missing_set
                )

        res.extra.update({
            "disk_files":      len(disk_files),
            "index_entries":   len(index_paths),
            "orphan_index":    len(orphan_index),
            "orphan_files":    len(orphan_files),
            "dangling":        len(dangling),
            "samples": {
                "orphan_index": orphan_index[:5],
                "orphan_files": orphan_files[:5],
                "dangling":     dangling[:5],
            },
        })
        res.processed = len(orphan_index)  # only auto-fixable category

        # 4. Auto-fix: drop orphan index entries (file already gone) —
        # through the single writer, never a direct file rewrite.
        if orphan_index and not dry_run:
            storage.drop_entries(orphan_index)

    except Exception as exc:
        res.errors.append(f"{type(exc).__name__}: {exc}")
        log.error("vault_compact_failed", error=str(exc))

    res.elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "vault_compact_done",
        orphan_index = res.extra.get("orphan_index", 0),
        orphan_files = res.extra.get("orphan_files", 0),
        dangling     = res.extra.get("dangling", 0),
        elapsed_ms   = res.elapsed_ms,
        dry_run      = dry_run,
    )
    return res

