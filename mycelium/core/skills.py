"""Skill learning: reusable extraction patterns (R1.4).

Skills live in two locations:
  • Bundled — `mycelium/skills/extraction/` (in source/image, read-only)
  • User   — `~/.mycelium/skills/extraction/` (volume-mounted, writable)

User-saved skills override bundled by name. `save_skill` always writes
to the user dir, so user data survives container rebuilds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

_BUNDLED_SKILLS_DIR = Path(__file__).parent.parent / "skills" / "extraction"
_USER_SKILLS_DIR    = Path.home() / ".mycelium" / "skills" / "extraction"


@dataclass
class Skill:
    name:     str
    path:     Path
    match:    dict[str, str]       = field(default_factory=dict)
    content:  str                  = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "path": str(self.path),
            "match": self.match,
        }


def _skill_dirs() -> list[Path]:
    """Existing skill dirs in load order (bundled first, user last → user wins)."""
    return [d for d in (_BUNDLED_SKILLS_DIR, _USER_SKILLS_DIR) if d.exists()]


def load_skills() -> list[Skill]:
    """Load all extraction skills from bundled + user dirs (hot-reload)."""
    by_name: dict[str, Skill] = {}
    for d in _skill_dirs():
        for p in sorted(d.glob("*.md")):
            try:
                s = _parse_skill(p)
                by_name[s.name] = s
            except Exception as e:
                log.warning("skill_load_failed", path=str(p), error=str(e))
    return list(by_name.values())


def match_skill(
    skills:      list[Skill],
    source_type: str = "",
    source_desc: str = "",
    name:        str = "",
) -> Skill | None:
    """Find best matching skill for signal metadata."""
    for skill in skills:
        if _matches(skill.match, source_type, source_desc, name):
            log.info("skill_matched", skill=skill.name)
            return skill
    return None


def save_skill(
    name:    str,
    match:   dict[str, str],
    content: str,
) -> Path:
    """Save a new extraction skill to the user dir (persistent across rebuilds)."""
    _USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    path = _USER_SKILLS_DIR / f"{slug}.md"

    # Build YAML header
    header_lines = ["---"]
    for k, v in match.items():
        header_lines.append(f"{k}: {v}")
    header_lines.append("---")

    text = "\n".join(header_lines) + "\n\n" + content
    path.write_text(text)

    log.info("skill_saved", name=name, path=str(path))
    return path


def list_skills() -> list[dict[str, Any]]:
    """List all available extraction skills."""
    return [s.to_dict() for s in load_skills()]


# ── Internal ──────────────────────────────────────────────


def _parse_skill(path: Path) -> Skill:
    """Parse a skill .md file with YAML header."""
    raw  = path.read_text()
    name = path.stem.replace("_", " ").title()

    match_rules: dict[str, str] = {}
    content = raw

    # Parse simple YAML header (--- delimited)
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            header  = parts[1].strip()
            content = parts[2].strip()
            for line in header.split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    match_rules[k.strip()] = v.strip()

    if "name" in match_rules:
        name = match_rules.pop("name")

    return Skill(name=name, path=path, match=match_rules, content=content)


def _matches(
    rules:       dict[str, str],
    source_type: str,
    source_desc: str,
    name:        str,
) -> bool:
    """Check if signal metadata matches skill rules."""
    if not rules:
        return False

    text = f"{source_type} {source_desc} {name}".lower()

    for key, pattern in rules.items():
        pattern_lower = pattern.lower()
        if key == "source":
            if pattern_lower not in source_type.lower():
                return False
        elif key == "format" or key == "keyword":
            if pattern_lower not in text:
                return False
        # Unknown keys: check against full text
        elif pattern_lower not in text:
            return False

    return True
