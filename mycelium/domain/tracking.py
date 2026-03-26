"""Metric tracking: template parser, MD file ops, dashboard generation."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mycelium.domain.models import ChartStyle, FieldConfig
from mycelium.obsidian import frontmatter as fm

_NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


# ---------------------------------------------------------------------------
# Alias index
# ---------------------------------------------------------------------------

def build_alias_index(
    fields: dict[str, FieldConfig],
) -> list[tuple[str, str]]:
    """Flatten field aliases → [(alias_lower, field_name)] sorted by length desc."""
    pairs: list[tuple[str, str]] = []
    for name, cfg in fields.items():
        # field name itself is an implicit alias
        pairs.append((name.lower(), name))
        for alias in cfg.aliases:
            pairs.append((alias.lower(), name))
    # longest first so "жим лёжа" matches before "жим"
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


# ---------------------------------------------------------------------------
# Template parser
# ---------------------------------------------------------------------------

def template_parse(
    text: str,
    fields: dict[str, FieldConfig],
) -> tuple[dict[str, float], str]:
    """Parse metrics from text using field aliases.

    Returns (parsed_values, leftover_text).
    Zero LLM cost — pure string matching.
    """
    if not fields or not text.strip():
        return {}, text.strip()

    index   = build_alias_index(fields)
    norm    = text.lower().replace("_", " ").replace("-", " ")
    values: dict[str, float] = {}
    used:   set[int]         = set()  # char positions consumed

    for alias, field_name in index:
        if field_name in values:
            continue  # already matched this field
        pos = norm.find(alias)
        if pos == -1:
            continue
        # skip if this region already consumed
        alias_range = set(range(pos, pos + len(alias)))
        if alias_range & used:
            continue

        # look right for number
        right = text[pos + len(alias):pos + len(alias) + 30]
        m = _NUM_RE.search(right)
        if m:
            num_start = pos + len(alias) + m.start()
            num_end   = pos + len(alias) + m.end()
        else:
            # look left for number
            left_start = max(0, pos - 20)
            left = text[left_start:pos]
            matches = list(_NUM_RE.finditer(left))
            if matches:
                m = matches[-1]  # rightmost number before alias
                num_start = left_start + m.start()
                num_end   = left_start + m.end()
            else:
                continue  # no number found near alias

        raw_num = text[num_start:num_end].replace(",", ".")
        try:
            values[field_name] = float(raw_num)
        except ValueError:
            continue

        # mark positions as consumed
        used.update(alias_range)
        used.update(range(num_start, num_end))

    # build leftover: chars not consumed, cleaned up
    leftover_chars = [c for i, c in enumerate(text) if i not in used]
    leftover = re.sub(r"[,\s]+", " ", "".join(leftover_chars)).strip()

    return values, leftover


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def _data_dir(vault_root: Path, vault_prefix: str) -> Path:
    """Ensure data/ directory exists under vault prefix."""
    d = vault_root / vault_prefix / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_metric_file(
    vault_root:   Path,
    vault_prefix: str,
    date:         str,
    values:       dict[str, float],
    body:         str = "",
) -> Path:
    """Create or merge vault/{prefix}/data/{date}.md with frontmatter."""
    data_dir = _data_dir(vault_root, vault_prefix)
    path     = data_dir / f"{date}.md"

    if path.exists():
        existing_fm, existing_body = fm.parse(
            path.read_text(encoding="utf-8", errors="replace"),
        )
        # merge: new values override existing
        existing_fm.update(values)
        existing_fm["date"] = date
        if body:
            existing_body = (existing_body.rstrip() + "\n\n" + body) if existing_body.strip() else body
        path.write_text(fm.render(existing_fm, existing_body), encoding="utf-8")
    else:
        frontmatter = {"date": date, **values}
        path.write_text(fm.render(frontmatter, body + "\n" if body else ""), encoding="utf-8")

    return path


def read_metric_files(
    vault_root:   Path,
    vault_prefix: str,
    period:       str = "",
    field:        str = "",
) -> list[dict]:
    """Read metric MD files from vault/{prefix}/data/.

    Returns list of dicts with 'date' + field values, sorted newest first.
    """
    data_dir = vault_root / vault_prefix / "data"
    if not data_dir.exists():
        return []

    # period filter
    cutoff = None
    if period:
        match = re.match(r"(\d+)d", period)
        if match:
            days   = int(match.group(1))
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    entries: list[dict] = []
    for p in sorted(data_dir.glob("*.md"), reverse=True):
        text     = p.read_text(encoding="utf-8", errors="replace")
        fmdata, _ = fm.parse(text)
        if not fmdata:
            continue
        date_val = str(fmdata.get("date", p.stem))
        if cutoff and date_val < cutoff:
            continue
        entry = {"date": date_val}
        for k, v in fmdata.items():
            if k == "date":
                continue
            if field and k != field:
                continue
            entry[k] = v
        entries.append(entry)

    return entries


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def compute_stats(entries: list[dict], field: str) -> dict:
    """Compute min, max, avg, trend for a field across entries."""
    vals = [e[field] for e in entries if field in e and isinstance(e[field], (int, float))]
    if not vals:
        return {"field": field, "count": 0}

    avg = sum(vals) / len(vals)
    trend = "stable"
    if len(vals) >= 4:
        mid    = len(vals) // 2
        recent = sum(vals[:mid]) / mid
        older  = sum(vals[mid:]) / (len(vals) - mid)
        diff   = (recent - older) / older if older else 0
        if diff > 0.03:
            trend = "up"
        elif diff < -0.03:
            trend = "down"

    return {
        "field": field,
        "count": len(vals),
        "min":   min(vals),
        "max":   max(vals),
        "avg":   round(avg, 2),
        "trend": trend,
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def generate_dashboard(
    vault_root:   Path,
    vault_prefix: str,
    domain_name:  str,
    fields:       dict[str, FieldConfig],
    entries:       list[dict],
    max_rows:      int = 30,
    chart_style:   ChartStyle | None = None,
) -> Path:
    """Generate/update dashboard.md with plain table + Tracker blocks."""
    data_dir = _data_dir(vault_root, vault_prefix)
    path     = data_dir.parent / "dashboard.md"

    field_names  = list(fields.keys())
    field_labels = {n: (fields[n].label or n) for n in field_names}

    lines: list[str] = [f"# {domain_name}\n"]

    # plain table (works in any MD viewer)
    if entries:
        header = "| Date | " + " | ".join(field_labels[n] for n in field_names) + " |"
        sep    = "|------|" + "|".join("---" for _ in field_names) + "|"
        lines.append(header)
        lines.append(sep)
        for entry in entries[:max_rows]:
            row = f"| {entry.get('date', '')} |"
            for n in field_names:
                v = entry.get(n, "")
                row += f" {v} |"
            lines.append(row)
        lines.append("")

    # stats
    for n in field_names:
        stats = compute_stats(entries, n)
        if stats.get("count", 0) > 0:
            lines.append(
                f"**{field_labels[n]}**: "
                f"min {stats['min']}, max {stats['max']}, "
                f"avg {stats['avg']}, trend {stats['trend']}"
            )
    lines.append("")

    # tracker plugin blocks (bonus for users with plugin)
    lines.append("---\n")
    lines.append("*Charts below require [Tracker plugin](https://github.com/pyrochlore/obsidian-tracker)*\n")
    prefix_data = vault_prefix.rstrip("/") + "/data"
    cs = chart_style or ChartStyle()
    for n in field_names:
        label = field_labels[n]
        lines.append(
            f"```tracker\n"
            f"searchType: frontmatter\n"
            f"searchTarget: {n}\n"
            f"folder: {prefix_data}\n"
            f"{cs.type}:\n"
            f"  title: \"{label}\"\n"
            f"  lineColor: \"{cs.color}\"\n"
            f"  showPoint: {'true' if cs.show_point else 'false'}\n"
            f"  pointSize: {cs.point_size}\n"
            f"  fillGap: true\n"
            f"```\n"
        )

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
