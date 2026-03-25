"""YAML frontmatter: parse, merge mycelium_ fields, strip for hashing."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

_FM_RE = re.compile(r"\A---\n(.*?)---\n?", re.DOTALL)
_MYCELIUM_PREFIX = "mycelium_"


def parse(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter → (fm_dict, body).

    Returns ({}, text) when no frontmatter found.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    body = text[m.end():]
    return fm, body


def render(fm: dict, body: str) -> str:
    """Render frontmatter dict + body back to file content."""
    if not fm:
        return body
    header = yaml.dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    return f"---\n{header}---\n{body}"


def merge_mycelium(fm: dict, fields: dict) -> dict:
    """Merge mycelium_ fields into frontmatter, preserving user keys."""
    result = {k: v for k, v in fm.items() if not k.startswith(_MYCELIUM_PREFIX)}
    result.update(fields)
    return result


def strip_mycelium(text: str) -> str:
    """Remove mycelium_ fields from frontmatter (for content-hash)."""
    fm, body = parse(text)
    if not fm:
        return text
    cleaned = {k: v for k, v in fm.items() if not k.startswith(_MYCELIUM_PREFIX)}
    return render(cleaned, body)


def content_hash(path: Path) -> str:
    """SHA-256 of file content with mycelium_ fields stripped."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    stripped = strip_mycelium(raw)
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()


def wikilink(relative_path: str) -> str:
    """Convert relative vault path to Obsidian wikilink.

    'documents/meeting_notes.md' → '[[documents/meeting_notes]]'
    """
    stem = re.sub(r"\.md$", "", relative_path)
    return f"[[{stem}]]"
