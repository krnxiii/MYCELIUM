"""Text chunker: scored break-points + code block protection. L1/CF#7."""

from __future__ import annotations

import re

# ── Table detection ────────────────────────────────────────

_TABLE_RE = re.compile(r"((?:^\|[^\n]+\|[ \t]*\n?)+)", re.MULTILINE)
_SEP_RE   = re.compile(r"^[|:+\s-]+$")


def linearize_tables(text: str) -> str:
    """Convert markdown tables to linearized key: value text."""
    return _TABLE_RE.sub(_linearize_md, text)


def _linearize_md(match: re.Match[str]) -> str:
    """Linearize one markdown table."""
    rows = match.group(0).strip().split("\n")
    data = [r for r in rows if not _SEP_RE.match(r.strip())]
    if len(data) < 2:
        return match.group(0)

    headers = [h.strip() for h in data[0].split("|") if h.strip()]
    lines: list[str] = []
    for row in data[1:]:
        cells = [c.strip() for c in row.split("|") if c.strip()]
        pairs = [f"{h}: {v}" for h, v in zip(headers, cells, strict=False) if v]
        if pairs:
            lines.append(", ".join(pairs))

    return "\n".join(lines) + "\n" if lines else match.group(0)


# ── Break-point scoring ───────────────────────────────────

_CODE_FENCE = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)

# (pattern, score) — higher score = better split point
_BREAK_PATTERNS: list[tuple[str, int]] = [
    ("\n# ",    100),    # H1
    ("\n## ",    90),    # H2
    ("\n### ",   80),    # H3
    ("\n#### ",  70),    # H4
    ("\n---\n",  60),    # thematic break
    ("\n***\n",  60),    # thematic break
    ("\n\n",     20),    # blank line (paragraph)
    ("\n- ",      5),    # list item
    ("\n* ",      5),    # list item
    ("\n",        1),    # any line break
]


def _find_code_fences(text: str) -> list[tuple[int, int]]:
    """Find all code block ranges (start, end) — protected zones."""
    fences: list[tuple[int, int]] = []
    positions = list(_CODE_FENCE.finditer(text))

    i = 0
    while i < len(positions) - 1:
        open_m  = positions[i]
        close_m = positions[i + 1]
        fences.append((open_m.start(), close_m.end()))
        i += 2

    return fences


def _in_code_block(pos: int, fences: list[tuple[int, int]]) -> bool:
    """Check if position falls inside a code block."""
    for start, end in fences:
        if start <= pos <= end:
            return True
    return False


def _find_best_break(
    text:   str,
    start:  int,
    end:    int,
    fences: list[tuple[int, int]],
) -> int:
    """Find highest-scored break point in [start+half, end)."""
    half   = start + (end - start) // 2
    window = text[half:end]

    best_pos   = end
    best_score = 0

    for pattern, score in _BREAK_PATTERNS:
        idx = window.rfind(pattern)
        if idx < 0:
            continue

        abs_pos = half + idx + 1  # split after the newline
        if pattern.startswith("\n") and len(pattern) > 1:
            abs_pos = half + idx + 1  # before the header/item

        if _in_code_block(abs_pos, fences):
            continue

        if score > best_score:
            best_score = score
            best_pos   = abs_pos

    return best_pos


# ── Chunking ──────────────────────────────────────────────


def chunk_text(
    text:      str,
    max_chars: int = 8000,
    overlap:   int = 500,
) -> list[str]:
    """Split text into chunks using scored break-points."""
    if not text:
        return [text]

    text = linearize_tables(text)

    if len(text) <= max_chars:
        return [text]

    fences = _find_code_fences(text)
    chunks: list[str] = []
    start  = 0

    while start < len(text):
        end = start + max_chars

        if end >= len(text):
            chunks.append(text[start:])
            break

        cut = _find_best_break(text, start, end, fences)
        chunks.append(text[start:cut])
        start = max(cut - overlap, start + 1)

    return chunks
