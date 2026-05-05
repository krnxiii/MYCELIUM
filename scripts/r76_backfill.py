#!/usr/bin/env python3
"""R7.6 fundamental — one-shot backfill for existing graphs.

Run **once** after deploying R7.6. Idempotent: re-running is a no-op.

What it does:
  1. For every domain in ``~/.mycelium/domains/``:
     - ensure ``slug`` is set in the YAML (legacy files written before
       R7.6 may not have it),
     - eagerly create ``CORTEX/{slug}/_blueprint.md`` (R7.6 (a) backfill).
  2. Schema migrations are applied automatically via ``build_indices``
     when the MCP server starts; they backfill ``VaultFile`` nodes and
     ``STORED_AT`` edges for every existing file-Signal (R7.6 (b)).
     This script can also force-run them by calling ``build_indices``
     against the configured Neo4j instance.

Safe to run on a live system: every operation is an idempotent MERGE
or ``exist_ok=True`` mkdir. Existing data is never deleted or moved.

Usage:
    python scripts/r76_backfill.py            # full backfill
    python scripts/r76_backfill.py --dry-run  # report only, no writes
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as a script from the repo root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mycelium.config import load_settings
from mycelium.domain import (
    DomainBlueprint, ensure_marker, load_all, save, slugify,
)
from mycelium.driver.neo4j_driver import Neo4jDriver


def backfill_domains(*, dry_run: bool) -> dict:
    """Ensure slug + CORTEX marker for every existing domain."""
    sett = load_settings()
    domains = load_all()
    fixed_slug   = []
    created_mark = []

    for bp in domains:
        if not bp.slug:
            # Legacy YAML — derive slug from name and rewrite.
            new_slug = slugify(bp.name)
            print(f"  [domain] adding slug='{new_slug}' to '{bp.name}'")
            if not dry_run:
                # frozen=True, so build a fresh blueprint with the slug set
                fresh = DomainBlueprint(**{**bp.model_dump(), "slug": new_slug})
                save(fresh)
                bp = fresh
            fixed_slug.append(bp.name)

        marker = sett.vault.path / "CORTEX" / (bp.slug or slugify(bp.name)) / "_blueprint.md"
        if marker.exists():
            continue
        print(f"  [domain] creating marker: {marker.relative_to(sett.vault.path)}")
        if not dry_run:
            ensure_marker(sett.vault.path, bp)
        created_mark.append(bp.name)

    return {
        "domains_total":    len(domains),
        "slug_fixed":       fixed_slug,
        "markers_created":  created_mark,
    }


async def backfill_graph(*, dry_run: bool) -> dict:
    """Apply schema constraints + migrations (idempotent)."""
    sett = load_settings()
    drv  = Neo4jDriver(sett.neo4j)

    try:
        async with drv:
            if dry_run:
                rows = await drv.execute_query(
                    "MATCH (s:Signal) WHERE s.source_type = 'file' "
                    "AND NOT (s)-[:STORED_AT]->(:VaultFile) "
                    "RETURN count(s) AS n",
                )
                missing = int(rows[0]["n"]) if rows else 0
                print(f"  [graph]  {missing} file-Signals lack STORED_AT edge "
                      f"(would be backfilled)")
                return {"signals_missing_edge": missing}

            # build_indices applies CONSTRAINTS + MIGRATIONS, including the
            # R7.6 fundamental migration that creates VaultFile + STORED_AT.
            await drv.build_indices()
            rows = await drv.execute_query(
                "MATCH (vf:VaultFile) RETURN count(vf) AS n",
            )
            total = int(rows[0]["n"]) if rows else 0
            print(f"  [graph]  VaultFile nodes total: {total}")
            return {"vault_files_total": total}
    except Exception as e:
        print(f"  [graph]  Neo4j unreachable: {e}", file=sys.stderr)
        print("  [graph]  Skip — restart MCP server to apply schema migrations.",
              file=sys.stderr)
        return {"graph_error": str(e)}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change, do not write.")
    args = parser.parse_args()

    print("R7.6 backfill" + (" (DRY RUN)" if args.dry_run else ""))
    print("=" * 50)

    print("Domains:")
    d = backfill_domains(dry_run=args.dry_run)
    print(f"  total: {d['domains_total']}, "
          f"slug fixed: {len(d['slug_fixed'])}, "
          f"markers created: {len(d['markers_created'])}")

    print("Graph:")
    g = await backfill_graph(dry_run=args.dry_run)
    for k, v in g.items():
        print(f"  {k}: {v}")

    print("=" * 50)
    print("Done." if not args.dry_run else "Dry-run complete — no changes written.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
