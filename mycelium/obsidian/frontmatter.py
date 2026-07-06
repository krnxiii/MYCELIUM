"""YAML frontmatter: parse, merge mycelium_ fields, strip for hashing."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

# Closing '---' must sit at line start: the old r"\A---\n(.*?)---\n?" matched
# a '---' INSIDE a value ("date: 2024-01-01 --- draft"), silently truncating
# the block and leaking the remainder into the body on rewrite (audit M31).
# CRLF tolerated; BOM stripped in parse().
_FM_RE = re.compile(r"\A---\r?\n(.*?)^---[ \t]*\r?(?:\n|\Z)",
                    re.DOTALL | re.MULTILINE)
_MYCELIUM_PREFIX = "mycelium_"
_BOM = "\ufeff"


def parse(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter → (fm_dict, body).

    Returns ({}, text) when no frontmatter found.
    """
    if text.startswith(_BOM):
        text = text[len(_BOM):]
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    body = text[m.end():]
    return fm, body


def has_unparseable_frontmatter(text: str) -> bool:
    """True when the file LOOKS like it has a frontmatter block that we
    cannot parse — writers must skip such files instead of rewriting them
    (a rewrite would prepend a second block above the user's real one)."""
    if text.startswith(_BOM):
        text = text[len(_BOM):]
    if not text.startswith("---"):
        return False
    fm, body = parse(text)
    return not fm and body == text


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
