"""Skill learning: reusable extraction patterns (R1.4).

Skills are .md templates stored in mycelium/skills/extraction/.
Each skill has a YAML header with match rules and a body with
extraction guidance injected into the LLM prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

_SKILLS_DIR = Path(__file__).parent.parent / "skills" / "extraction"


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


def load_skills() -> list[Skill]:
    """Load all extraction skills from disk (hot-reload)."""
    if not _SKILLS_DIR.exists():
        return []

    skills = []
    for p in sorted(_SKILLS_DIR.glob("*.md")):
        try:
            skills.append(_parse_skill(p))
        except Exception as e:
            log.warning("skill_load_failed", path=str(p), error=str(e))

    return skills


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
    """Save a new extraction skill to disk."""
    _SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    path = _SKILLS_DIR / f"{slug}.md"

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
        elif key == "format":
            if pattern_lower not in text:
                return False
        elif key == "keyword":
            if pattern_lower not in text:
                return False
        # Unknown keys: check against full text
        elif pattern_lower not in text:
            return False

    return True
