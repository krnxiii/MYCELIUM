"""Domain blueprint registry: load/save/list/delete from ~/.mycelium/domains/."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml

from mycelium.domain.models import DomainBlueprint, slugify

log = structlog.get_logger()

DOMAINS_DIR = Path.home() / ".mycelium" / "domains"


def _slugify(name: str) -> str:
    """Backward-compat alias — prefer mycelium.domain.models.slugify."""
    return slugify(name)


def load_all() -> list[DomainBlueprint]:
    """Load all domain blueprints from disk (hot-reload)."""
    if not DOMAINS_DIR.exists():
        return []

    domains = []
    for p in sorted(DOMAINS_DIR.glob("*.yaml")):
        try:
            domains.append(_parse(p))
        except Exception as e:
            log.warning("domain_load_failed", path=str(p), error=str(e))

    return domains


def load_by_slug(slug: str) -> DomainBlueprint | None:
    """Load a domain blueprint by its immutable slug — primary lookup."""
    path = DOMAINS_DIR / f"{slug}.yaml"
    if path.exists():
        try:
            return _parse(path)
        except Exception:
            return None
    return None


def load_by_name(name: str) -> DomainBlueprint | None:
    """Load a domain blueprint by display name (case-insensitive).

    Prefer ``load_by_slug`` for identity lookups — name is mutable and
    can change while slug stays fixed.
    """
    bp = load_by_slug(slugify(name))
    if bp is not None:
        return bp

    # Slug derived from current name didn't match — search by name field
    # (covers domains where the user renamed via mutable `name`).
    for d in load_all():
        if d.name.lower() == name.lower():
            return d
    return None


def save(blueprint: DomainBlueprint) -> Path:
    """Save domain blueprint to disk. Returns the file path."""
    DOMAINS_DIR.mkdir(parents=True, exist_ok=True)

    blueprint.updated_at = datetime.now(timezone.utc)

    slug = blueprint.slug or slugify(blueprint.name)
    path = DOMAINS_DIR / f"{slug}.yaml"

    data = blueprint.model_dump(mode="json")
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    log.info("domain_saved", slug=slug, name=blueprint.name, path=str(path))
    return path


def delete(name: str) -> bool:
    """Delete domain blueprint by name or slug. Returns True if deleted."""
    slug = slugify(name)
    path = DOMAINS_DIR / f"{slug}.yaml"

    if path.exists():
        path.unlink()
        log.info("domain_deleted", name=name, path=str(path))
        return True

    # Fallback: search by name field in all files
    for p in DOMAINS_DIR.glob("*.yaml"):
        try:
            d = _parse(p)
            if d.name.lower() == name.lower() or d.slug == slug:
                p.unlink()
                log.info("domain_deleted", name=name, slug=d.slug, path=str(p))
                return True
        except Exception:
            continue

    return False


def to_compact_list(domains: list[DomainBlueprint]) -> str:
    """Compact text representation for MCP resource."""
    if not domains:
        return "No domain blueprints configured."

    lines = [f"Domains: {len(domains)}"]
    for d in domains:
        triggers = ", ".join(d.triggers[:5]) if d.triggers else "none"
        lines.append(
            f"- {d.name}: {d.description or 'no description'}\n"
            f"  triggers: [{triggers}] | vault: {d.vault_prefix or 'default'} | "
            f"anchor: {d.anchor_neuron or 'none'}"
        )
    return "\n".join(lines)


# ── Internal ──────────────────────────────────────────────


def _parse(path: Path) -> DomainBlueprint:
    """Parse a domain blueprint YAML file."""
    data = yaml.safe_load(path.read_text()) or {}
    return DomainBlueprint.model_validate(data)
