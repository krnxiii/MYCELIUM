"""Obsidian sync: traverse vault → compute relations → write frontmatter."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from mycelium.config import ObsidianSettings
from mycelium.driver.driver import GraphDriver
from mycelium.obsidian import frontmatter as fm
from mycelium.obsidian.relations import get_neurons, get_related, get_similar
from mycelium.vault.storage import VaultStorage

log = structlog.get_logger()

_SKIP_PREFIXES = (".", "_")
_BINARY_EXTS   = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                  ".mp3", ".wav", ".ogg", ".mp4", ".mov", ".zip"}


@dataclass
class SyncResult:
    updated:      int           = 0
    companions:   int           = 0
    skipped:      int           = 0
    projected:    int           = 0
    pruned:       int           = 0
    moved:        int           = 0
    unindexed:    list[str]     = field(default_factory=list)
    hash_changed: list[str]     = field(default_factory=list)


def _vault_md_files(vault: VaultStorage) -> list[Path]:
    """Yield .md files in vault, skipping dot-dirs and special files."""
    files: list[Path] = []
    for p in vault.root.rglob("*.md"):
        parts = p.relative_to(vault.root).parts
        if any(part.startswith(_SKIP_PREFIXES) for part in parts):
            continue
        files.append(p)
    return sorted(files)


def _relative_path_for(vault: VaultStorage, path: Path) -> str:
    """Convert absolute path to vault-relative string."""
    return str(path.relative_to(vault.root))


def _source_desc_for(relative_path: str) -> str:
    """Build source_desc from vault-relative path."""
    return f"file:{relative_path}"


async def sync(
    driver:   GraphDriver,
    vault:    VaultStorage,
    settings: ObsidianSettings,
) -> SyncResult:
    """Full sync: recompute relations and update frontmatter for all .md files."""
    result = SyncResult()
    index  = vault._load_index()

    # Detect file moves (same content_hash, different path)
    result.moved = await _detect_moves(driver, vault, index)
    if result.moved:
        index = vault._load_index()  # reload after moves

    # Sync .md files — direct frontmatter
    for path in _vault_md_files(vault):
        rel = _relative_path_for(vault, path)
        meta = index.get(rel)

        if not meta or not meta.get("signal_uuid"):
            # Skip managed files (companions, projected neurons)
            if not _is_companion(path) and not _is_projected(vault, path):
                result.unindexed.append(rel)
            result.skipped += 1
            continue

        signal_uuid = meta["signal_uuid"]

        # Check content hash (detect user edits)
        current_hash = fm.content_hash(path)
        stored_hash  = meta.get("content_hash", "")
        if current_hash != stored_hash:
            result.hash_changed.append(rel)

        await _write_frontmatter(
            driver, vault, path, rel, signal_uuid, settings,
        )
        result.updated += 1

    # Sync binary files — companion .md
    for rel, meta in index.items():
        signal_uuid = meta.get("signal_uuid")
        if not signal_uuid:
            continue
        abs_path = vault.root / rel
        if not abs_path.exists():
            continue
        if abs_path.suffix.lower() in _BINARY_EXTS:
            companion = _companion_path(abs_path)
            await _write_companion(
                driver, vault, companion, abs_path, rel, signal_uuid, settings,
            )
            result.companions += 1

    # Update agent workspace context
    from mycelium.agent.workspace import update_context, ensure_workspace
    ensure_workspace(vault)
    await update_context(driver, vault)

    # Project neurons as .md files (experimental, opt-in)
    if settings.project_neurons:
        projected, pruned = await _project_neurons(driver, vault, settings)
        result.projected = projected
        result.pruned    = pruned

    log.info("obsidian_sync", **{
        "updated": result.updated, "companions": result.companions,
        "skipped": result.skipped, "projected": result.projected,
        "pruned": result.pruned, "moved": result.moved,
        "unindexed": len(result.unindexed),
        "hash_changed": len(result.hash_changed),
    })
    return result


async def inject_after_ingest(
    driver:       GraphDriver,
    vault:        VaultStorage,
    entry_path:   str,
    signal_uuid:  str,
    settings:     ObsidianSettings,
    *,
    original_ext: str = "",
) -> None:
    """Inject frontmatter into a freshly ingested file (called from add_file)."""
    abs_path = vault.root / entry_path

    if abs_path.suffix == ".md" and abs_path.exists():
        await _write_frontmatter(
            driver, vault, abs_path, entry_path, signal_uuid, settings,
            original_ext=original_ext,
        )
    elif abs_path.suffix.lower() in _BINARY_EXTS and abs_path.exists():
        companion = _companion_path(abs_path)
        await _write_companion(
            driver, vault, companion, abs_path, entry_path, signal_uuid, settings,
        )


async def status(
    vault:    VaultStorage,
    settings: ObsidianSettings,
) -> dict:
    """Compute vault status for CLI display."""
    index    = vault._load_index()
    md_files = _vault_md_files(vault)

    indexed_paths = {_relative_path_for(vault, p) for p in md_files} & set(index)
    with_signal   = sum(1 for p in indexed_paths if index[p].get("signal_uuid"))

    # Check which indexed .md files have mycelium frontmatter
    with_fm = 0
    for p in md_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            fmd, _ = fm.parse(text)
            if any(k.startswith("mycelium_") for k in fmd):
                with_fm += 1
        except OSError:
            pass

    # Count companion .md files for binaries
    companions = sum(1 for p in md_files if _is_companion(p))

    unindexed = []
    for p in md_files:
        rel = _relative_path_for(vault, p)
        if _is_companion(p):
            continue
        if rel not in index or not index[rel].get("signal_uuid"):
            unindexed.append(rel)

    return {
        "enabled":     settings.enabled,
        "md_files":    len(md_files),
        "with_signal": with_signal,
        "with_fm":     with_fm,
        "companions":  companions,
        "unindexed":   unindexed,
    }


# ── Internal ──────────────────────────────────────────────


def _companion_path(binary_path: Path) -> Path:
    """Companion .md path for a binary file: report.pdf → report.pdf.md"""
    return binary_path.parent / (binary_path.name + ".md")


def _is_companion(md_path: Path) -> bool:
    """Check if an .md file is a companion (e.g. report.pdf.md)."""
    stem_path = md_path.with_suffix("")
    return stem_path.suffix.lower() in _BINARY_EXTS


def _is_projected(vault: VaultStorage, md_path: Path) -> bool:
    """Check if an .md file is a projected neuron (vault/neurons/*.md)."""
    try:
        rel = md_path.relative_to(vault.root)
        return rel.parts[0] == _NEURONS_DIR if rel.parts else False
    except ValueError:
        return False


async def _write_companion(
    driver:        GraphDriver,
    vault:         VaultStorage,
    companion:     Path,
    binary_path:   Path,
    relative_path: str,
    signal_uuid:   str,
    settings:      ObsidianSettings,
) -> None:
    """Create/update companion .md for a binary file."""
    source_desc  = _source_desc_for(relative_path)
    neurons_info = await get_neurons(driver, source_desc)

    related_files, similar_files = await asyncio.gather(
        get_related(
            driver, source_desc,
            min_shared      = settings.min_shared_neurons,
            max_related     = settings.max_related,
            include_expired = settings.include_expired,
        ),
        get_similar(
            driver, source_desc,
            threshold   = settings.similarity_threshold,
            max_similar = settings.max_similar,
        ),
    )

    related_links = _build_related_links(related_files)
    similar_links = _build_similar_links(similar_files)
    neuron_types  = sorted({ni.type for ni in neurons_info if ni.type})
    importance    = _compute_importance(neurons_info)

    mycelium_fields: dict = {
        "mycelium_signal":       signal_uuid,
        "mycelium_neurons":      [ni.name for ni in neurons_info],
        "mycelium_neuron_types": neuron_types,
        "mycelium_importance":   importance,
        "mycelium_related":      related_links,
        "mycelium_similar":      similar_links,
        "mycelium_synced":       datetime.now(UTC).isoformat(timespec="seconds"),
    }

    # Companion body: embed the binary
    binary_rel = str(binary_path.relative_to(vault.root))
    body = f"![[{binary_rel}]]\n"

    if companion.exists():
        text = companion.read_text(encoding="utf-8", errors="replace")
        existing_fm, existing_body = fm.parse(text)
        if existing_body.strip() != f"![[{binary_rel}]]":
            body = existing_body
        merged = fm.merge_mycelium(existing_fm, mycelium_fields)
    else:
        merged = mycelium_fields

    companion.write_text(fm.render(merged, body), encoding="utf-8")
    log.debug("obsidian_companion_written", path=str(companion.relative_to(vault.root)))


async def _write_frontmatter(
    driver:        GraphDriver,
    vault:         VaultStorage,
    path:          Path,
    relative_path: str,
    signal_uuid:   str,
    settings:      ObsidianSettings,
    *,
    original_ext:  str = "",
) -> None:
    """Compute and write mycelium frontmatter to a .md file."""
    source_desc  = _source_desc_for(relative_path)
    neurons_info = await get_neurons(driver, source_desc)

    related_files, similar_files = await asyncio.gather(
        get_related(
            driver, source_desc,
            min_shared      = settings.min_shared_neurons,
            max_related     = settings.max_related,
            include_expired = settings.include_expired,
        ),
        get_similar(
            driver, source_desc,
            threshold   = settings.similarity_threshold,
            max_similar = settings.max_similar,
        ),
    )

    related_links = _build_related_links(related_files)
    similar_links = _build_similar_links(similar_files)
    neuron_types  = sorted({ni.type for ni in neurons_info if ni.type})
    importance    = _compute_importance(neurons_info)

    mycelium_fields: dict = {
        "mycelium_signal":       signal_uuid,
        "mycelium_neurons":      [ni.name for ni in neurons_info],
        "mycelium_neuron_types": neuron_types,
        "mycelium_importance":   importance,
        "mycelium_related":      related_links,
        "mycelium_similar":      similar_links,
        "mycelium_synced":       datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if original_ext:
        mycelium_fields["mycelium_original_ext"] = original_ext

    text = path.read_text(encoding="utf-8", errors="replace")
    existing_fm, body = fm.parse(text)

    # Preserve existing mycelium_original_ext if not overriding
    if not original_ext and "mycelium_original_ext" in existing_fm:
        mycelium_fields["mycelium_original_ext"] = existing_fm["mycelium_original_ext"]

    merged = fm.merge_mycelium(existing_fm, mycelium_fields)
    path.write_text(fm.render(merged, body), encoding="utf-8")

    log.debug("obsidian_fm_written", path=str(path.relative_to(vault.root)))


def _build_related_links(related_files: list) -> list[str]:
    """Build wikilinks from related files' source_desc."""
    links: list[str] = []
    for rf in related_files:
        rel_path = _source_desc_to_path(rf.source_desc)
        if rel_path and rel_path.endswith(".md"):
            links.append(fm.wikilink(rel_path))
        elif rel_path:
            links.append(fm.wikilink(rel_path + ".md"))
    return links


async def _detect_moves(
    driver: GraphDriver,
    vault:  VaultStorage,
    index:  dict[str, dict],
) -> int:
    """Detect file moves via content_hash and update index + graph.

    Algorithm:
      1. Build hash→(rel_path, signal_uuid) map from index
      2. Scan vault files not in index
      3. If file hash matches a missing indexed path → it's a move
    """
    # hash → (rel_path, signal_uuid) for indexed files with signals
    hash_map: dict[str, tuple[str, str]] = {}
    for rel, meta in index.items():
        h = meta.get("content_hash", "")
        uuid = meta.get("signal_uuid", "")
        if h and uuid:
            hash_map[h] = (rel, uuid)

    # Find indexed paths that no longer exist on disk
    missing = {
        rel for rel in index
        if not (vault.root / rel).exists()
    }
    if not missing:
        return 0

    moved = 0
    for path in _vault_all_files(vault):
        rel = str(path.relative_to(vault.root))
        if rel in index:
            continue  # already indexed

        h = fm.content_hash(path) if path.suffix == ".md" else _raw_hash(path)
        if h not in hash_map:
            continue

        old_rel, signal_uuid = hash_map[h]
        if old_rel not in missing:
            continue  # old file still exists → this is a copy, not move

        # Move detected: old_rel → rel
        old_desc = _source_desc_for(old_rel)
        new_desc = _source_desc_for(rel)

        # Update vault index
        old_meta = index.pop(old_rel, {})
        old_meta["content_hash"] = h
        index[rel] = old_meta
        vault._save_index(index)

        # Update Signal.source_desc in Neo4j
        await driver.execute_query(
            "MATCH (s:Signal) "
            "WHERE s.source_desc = $old_desc "
            "SET s.source_desc = $new_desc",
            {"old_desc": old_desc, "new_desc": new_desc},
        )

        missing.discard(old_rel)
        moved += 1
        log.info("file_move_detected",
                 old=old_rel, new=rel, signal=signal_uuid)

    return moved


def _vault_all_files(vault: VaultStorage) -> list[Path]:
    """All files in vault, skipping dot-dirs and _-dirs."""
    files: list[Path] = []
    for p in vault.root.rglob("*"):
        if not p.is_file():
            continue
        parts = p.relative_to(vault.root).parts
        if any(part.startswith(_SKIP_PREFIXES) for part in parts):
            continue
        files.append(p)
    return sorted(files)


def _raw_hash(path: Path) -> str:
    """SHA-256 of raw file bytes (for non-.md files)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_similar_links(similar_files: list) -> list[str]:
    """Build wikilinks from similar files' source_desc."""
    links: list[str] = []
    for sf in similar_files:
        rel_path = _source_desc_to_path(sf.source_desc)
        if rel_path and rel_path.endswith(".md"):
            links.append(fm.wikilink(rel_path))
        elif rel_path:
            links.append(fm.wikilink(rel_path + ".md"))
    return links


def _compute_importance(neurons_info: list) -> float:
    """Average confidence of associated neurons (0.0–1.0)."""
    if not neurons_info:
        return 0.0
    return round(
        sum(ni.confidence for ni in neurons_info) / len(neurons_info),
        3,
    )


def _source_desc_to_path(source_desc: str) -> str:
    """Extract relative vault path from signal source_desc.

    'file:documents/report.md' → 'documents/report.md'
    """
    if source_desc.startswith("file:"):
        return source_desc[5:]
    return ""


# ── Neuron projection (experimental) ─────────────────────

_NEURONS_DIR  = "NEURONS"
_PROJECTED_FM = "mycelium_projected"


def _neuron_filename(name: str) -> str:
    """Safe filename from neuron name: 'Alice Bob' → 'Alice Bob.md'."""
    safe = name.replace("/", "_").replace("\\", "_").replace(":", "_")
    return f"{safe}.md"


async def _project_neurons(
    driver:   GraphDriver,
    vault:    VaultStorage,
    settings: ObsidianSettings,
) -> tuple[int, int]:
    """Create/update .md files in vault/neurons/ for each active neuron.

    Returns (projected_count, pruned_count).
    """
    neurons_dir = vault.root / _NEURONS_DIR
    neurons_dir.mkdir(parents=True, exist_ok=True)

    # 1. Query all active neurons + their synapses
    rows = await driver.execute_query(
        "MATCH (n:Neuron) WHERE n.expired_at IS NULL "
        "  AND n.neuron_type <> 'community' "
        "  AND (n.expires_at IS NULL OR n.expires_at > datetime()) "
        "WITH n "
        "OPTIONAL MATCH (n)-[r:SYNAPSE]-(other:Neuron) "
        "  WHERE r.expired_at IS NULL AND other.expired_at IS NULL "
        "RETURN n.uuid AS uuid, n.name AS name, "
        "  n.neuron_type AS type, n.summary AS summary, "
        "  coalesce(n.importance, n.confidence) AS confidence, "
        "  n.confirmations AS confirmations, "
        "  coalesce(n.importance, n.confidence) * exp(-n.decay_rate * "
        "    duration.between(n.freshness, datetime()).days) AS weight, "
        "  collect(DISTINCT {name: other.name, type: other.neuron_type, "
        "    fact: r.fact, relation: r.relation, "
        "    direction: CASE WHEN startNode(r) = n "
        "      THEN 'out' ELSE 'in' END}) AS synapses"
    )

    # 2. Generate .md for each neuron
    active_files: set[str] = set()
    projected = 0

    for row in rows:
        filename = _neuron_filename(row["name"])
        active_files.add(filename)
        path = neurons_dir / filename

        synapses = [s for s in row["synapses"] if s.get("name")]
        wikilinks = sorted({
            fm.wikilink(f"{_NEURONS_DIR}/{_neuron_filename(s['name'])}")
            for s in synapses
        })

        # Build synapse lines for body
        synapse_lines = []
        for s in synapses:
            arrow = "→" if s["direction"] == "out" else "←"
            link  = fm.wikilink(f"{_NEURONS_DIR}/{_neuron_filename(s['name'])}")
            raw   = s.get("fact", "")
            fact  = (raw[:300] + "…") if len(raw) > 300 else raw
            synapse_lines.append(f"- {arrow} {link} *{s.get('relation', '')}*: {fact}")

        fields = {
            _PROJECTED_FM:           True,
            "mycelium_uuid":         row["uuid"],
            "mycelium_type":         row["type"],
            "mycelium_confidence":   round(row["confidence"] or 0, 3),
            "mycelium_weight":       round(row["weight"] or 0, 3),
            "mycelium_confirmations": row["confirmations"] or 0,
            "mycelium_connections":  wikilinks,
            "mycelium_synced":       datetime.now(UTC).isoformat(timespec="seconds"),
        }

        # Body: summary + synapses
        body_parts = []
        if row.get("summary"):
            body_parts.append(row["summary"])
        if synapse_lines:
            body_parts.append("\n## Connections\n")
            body_parts.append("\n".join(synapse_lines))

        body = "\n".join(body_parts) + "\n" if body_parts else ""

        # Preserve user additions below a marker
        if path.exists():
            existing_fm, existing_body = fm.parse(
                path.read_text(encoding="utf-8", errors="replace"),
            )
            # Keep user content after <!-- user --> marker
            marker = "<!-- user -->"
            if marker in existing_body:
                user_part = existing_body[existing_body.index(marker):]
                body = body.rstrip("\n") + "\n\n" + user_part

        path.write_text(fm.render(fields, body), encoding="utf-8")
        projected += 1

    # 3. Prune: remove .md files for neurons that no longer exist
    pruned = 0
    for existing in neurons_dir.iterdir():
        if existing.suffix != ".md":
            continue
        if existing.name not in active_files:
            # Only remove if it's a projected file (has our marker)
            try:
                text = existing.read_text(encoding="utf-8", errors="replace")
                existing_fm, _ = fm.parse(text)
                if existing_fm.get(_PROJECTED_FM):
                    existing.unlink()
                    pruned += 1
            except OSError:
                pass

    log.info("obsidian_neurons_projected",
             projected=projected, pruned=pruned)
    return projected, pruned
