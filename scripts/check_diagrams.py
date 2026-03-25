#!/usr/bin/env python3
"""Validate alignment of Unicode box-drawing diagrams in text files.

Checks:
  1. Box corners:  ┌/└ same column, ┐/┘ same column
  2. Side borders: │ on every middle line at left/right columns
  3. Connectors:   ┬ → │/▼ below must be in the same column
  4. Box widths:   top ─ and bottom ─ same length

Usage:
  python scripts/check_diagrams.py <file>          # CLI
  echo '{"tool_input":{"file_path":"f"}}' | python  # hook (stdin JSON)
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field

# ── Box-drawing constants ────────────────────────────────────────────
TL, TR, BL, BR = "┌", "┐", "└", "┘"
H, V           = "─", "│"
TD, TU         = "┬", "┴"
TRt, TLt       = "├", "┤"
CROSS          = "┼"
ARROW          = "▼"

VERT_CHARS     = frozenset({V, TRt, TLt, TL, BL})
CONN_EXPECT    = frozenset({V, ARROW, TD, TU, TL, BL, TRt, TLt, CROSS, BR})


# ── Data ─────────────────────────────────────────────────────────────
@dataclass
class Box:
    top:    int
    bottom: int
    left:   int
    right:  int


@dataclass
class Issue:
    line:    int          # 1-based
    col:     int          # 1-based
    message: str

    def __str__(self) -> str:
        return f"  L{self.line}:C{self.col}  {self.message}"


# ── Find boxes ───────────────────────────────────────────────────────
def find_boxes(lines: list[str]) -> list[Box]:
    """Detect rectangular boxes by matching ┌...┐ with └...┘."""
    boxes: list[Box] = []

    for i, line in enumerate(lines):
        for m in re.finditer(re.escape(TL), line):
            cl = m.start()
            # find ┐ on same line
            tr = line.find(TR, cl + 1)
            if tr < 0:
                continue
            cr = tr

            # scan down for └ in same column
            for j in range(i + 1, len(lines)):
                if len(lines[j]) <= cl:
                    break
                ch = lines[j][cl]
                if ch == BL:
                    # verify ┘ at right column
                    if len(lines[j]) > cr and lines[j][cr] == BR:
                        boxes.append(Box(top=i, bottom=j, left=cl, right=cr))
                    break
                if ch not in (V, TRt):
                    break  # not a box continuation

    return boxes


# ── Check individual box ─────────────────────────────────────────────
def check_box(lines: list[str], b: Box) -> list[Issue]:
    issues: list[Issue] = []
    w = b.right - b.left

    # top and bottom horizontal widths
    top_h = lines[b.top][b.left + 1 : b.right]
    bot_h = lines[b.bottom][b.left + 1 : b.right]

    if len(top_h) != len(bot_h):
        issues.append(Issue(
            b.bottom + 1, b.left + 1,
            f"bottom width ({len(bot_h)}) != top width ({len(top_h)})",
        ))

    # side borders on middle lines
    for r in range(b.top + 1, b.bottom):
        line = lines[r]
        # left
        if len(line) > b.left:
            if line[b.left] not in (V, TRt):
                issues.append(Issue(
                    r + 1, b.left + 1,
                    f"expected │/├, got '{line[b.left]}'",
                ))
        else:
            issues.append(Issue(r + 1, b.left + 1, "line too short for left border"))

        # right
        if len(line) > b.right:
            if line[b.right] not in (V, TLt):
                issues.append(Issue(
                    r + 1, b.right + 1,
                    f"expected │/┤, got '{line[b.right]}'",
                ))
        else:
            issues.append(Issue(r + 1, b.right + 1, "line too short for right border"))

    return issues


# ── Check vertical connectors ────────────────────────────────────────
def check_connectors(lines: list[str]) -> list[Issue]:
    """Verify ┬ and ▼ align vertically with connectors below."""
    issues: list[Issue] = []

    for i, line in enumerate(lines):
        for m in re.finditer(re.escape(TD), line):
            col = m.start()
            # look 1-3 lines below for next non-space char in same column
            for j in range(i + 1, min(i + 4, len(lines))):
                if len(lines[j]) > col and lines[j][col].strip():
                    ch = lines[j][col]
                    if ch not in CONN_EXPECT:
                        issues.append(Issue(
                            j + 1, col + 1,
                            f"┬ at L{i+1} expects │/▼ below, got '{ch}'",
                        ))
                    break

        for m in re.finditer(re.escape(ARROW), line):
            col = m.start()
            # ▼ should have box or content within 2 lines below
            found = False
            for j in range(i + 1, min(i + 3, len(lines))):
                if len(lines[j]) > col and lines[j][col].strip():
                    found = True
                    break
            if not found:
                issues.append(Issue(
                    i + 1, col + 1,
                    "▼ has no content below",
                ))

    return issues


# ── Main validation ──────────────────────────────────────────────────
def validate(filepath: str) -> list[Issue]:
    with open(filepath) as f:
        content = f.read()

    # skip files without box-drawing chars
    if not any(c in content for c in (TL, TR, BL, BR)):
        return []

    lines = content.split("\n")
    issues: list[Issue] = []

    for box in find_boxes(lines):
        issues.extend(check_box(lines, box))

    issues.extend(check_connectors(lines))

    # sort by line, col
    issues.sort(key=lambda x: (x.line, x.col))
    return issues


# ── Entrypoint ───────────────────────────────────────────────────────
def main() -> None:
    # mode 1: CLI argument (check first — stdin may be piped but empty)
    if len(sys.argv) > 1:
        filepath = sys.argv[1]
    # mode 2: stdin JSON (hook sends JSON via pipe)
    elif not sys.stdin.isatty():
        try:
            data     = json.load(sys.stdin)
            filepath = data.get("tool_input", {}).get("file_path", "")
        except (json.JSONDecodeError, KeyError):
            sys.exit(0)
    else:
        print("Usage: check_diagrams.py <file>", file=sys.stderr)
        sys.exit(1)

    if not filepath or not filepath.endswith(".txt"):
        sys.exit(0)

    issues = validate(filepath)
    if issues:
        print(f"DIAGRAM ALIGNMENT ({len(issues)} issues) — {filepath}", file=sys.stderr)
        for iss in issues:
            print(str(iss), file=sys.stderr)
        # exit 0: warn but don't block Edit/Write
    sys.exit(0)


if __name__ == "__main__":
    main()
