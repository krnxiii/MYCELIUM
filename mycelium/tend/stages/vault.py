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

import json
import time
from pathlib import Path

import structlog

from mycelium.config import TendSettings, VaultSettings
from mycelium.driver.neo4j_driver import Neo4jDriver
from mycelium.tend.stages.decay import StageResult

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
        root       = v.path
        index_path = root / ".index.json"

        if not root.exists():
            res.extra["skipped"] = "vault_root_missing"
            res.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return res

        # 1. Load index
        index: dict[str, dict] = {}
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
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

        orphan_index = sorted(index_paths - disk_files)   # in index, not on disk
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

        # 4. Auto-fix: drop orphan index entries (file already gone)
        if orphan_index and not dry_run:
            for path in orphan_index:
                index.pop(path, None)
            payload = json.dumps(index, indent=2, ensure_ascii=False).encode("utf-8")
            _atomic_write(index_path, payload)

    except Exception as exc:
        res.errors.append(f"{type(exc).__name__}: {exc}")
        log.error("vault_compact_failed", error=str(exc))

    res.elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "vault_compact_done",
        orphan_index = len(res.extra.get("samples", {}).get("orphan_index", [])),
        orphan_files = res.extra.get("orphan_files", 0),
        dangling     = res.extra.get("dangling", 0),
        elapsed_ms   = res.elapsed_ms,
        dry_run      = dry_run,
    )
    return res


def _atomic_write(path: Path, data: bytes) -> None:
    """Write to a temp file then rename — avoids partial-write corruption."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
