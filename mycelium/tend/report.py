"""Markdown report writer for tend / lint runs.

Formats are stable so that downstream tooling (skills, dashboards) can
parse without LLM. Reports land in `_AGENT/log/YYYY-MM-DD-{kind}.md` by
convention; auto-mkdir, append if file exists with the same date.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mycelium.tend.lint import LintReport
from mycelium.tend.orchestrator import TendReport


# ── Tend ────────────────────────────────────────────────────────────


def format_tend(report: TendReport, *, when: datetime | None = None) -> str:
    """Render TendReport as markdown."""
    when    = when or datetime.now(UTC)
    summary = report.summary()
    out = [
        f"# tend run — {when.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        f"- elapsed: **{report.elapsed_ms} ms**",
        f"- mode: **{'dry-run' if report.dry_run else 'apply'}**",
        f"- stages: {summary['stages_run']} "
        f"({summary['stages_ok']} ok, {summary['stages_fail']} failed)",
        f"- total processed: {summary['total_processed']}",
        "",
        "## Stages",
        "",
    ]
    for s in report.stages:
        status = "FAIL" if s.errors else "ok"
        out.append(f"### {s.name} — {status} · {s.elapsed_ms} ms")
        out.append("")
        out.append(f"- processed: {s.processed}")
        if s.weak_count:
            out.append(f"- weak: {s.weak_count}")
        if s.mean_weight:
            out.append(f"- mean weight: {s.mean_weight}")
        for k, v in (s.extra or {}).items():
            if k == "samples":
                continue
            out.append(f"- {k}: {_fmt_value(v)}")
        if s.extra and s.extra.get("samples"):
            out.append("- samples:")
            for cat, items in s.extra["samples"].items():
                if items:
                    out.append(f"  - {cat}: {', '.join(str(x) for x in items)}")
        for err in s.errors:
            out.append(f"- error: `{err}`")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ── Lint ────────────────────────────────────────────────────────────


def format_lint(report: LintReport, *, when: datetime | None = None) -> str:
    when = when or datetime.now(UTC)
    out  = [
        f"# lint report — {when.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        f"- score: **{report.score}** (1.0 = pristine, 0.0 = at-cap pain)",
        f"- elapsed: {report.elapsed_ms} ms",
        f"- neurons: {report.stats.get('neurons', 0)} · "
        f"synapses: {report.stats.get('synapses', 0)} · "
        f"signals: {report.stats.get('signals', 0)}",
        "",
    ]
    if not report.findings:
        out.extend(["No findings — graph is clean.", ""])
        return "\n".join(out)

    out.append("## Findings")
    out.append("")
    by_sev = {"high": [], "medium": [], "low": []}
    for f in report.findings:
        by_sev[f.severity].append(f)
    for sev in ("high", "medium", "low"):
        if not by_sev[sev]:
            continue
        out.append(f"### {sev}")
        out.append("")
        for f in by_sev[sev]:
            out.append(f"- **{f.category}** ({f.count}) — {f.message}")
            if f.samples:
                samples = ", ".join(_fmt_sample(x) for x in f.samples[:5])
                out.append(f"  - samples: {samples}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ── Writer ──────────────────────────────────────────────────────────


def write_log(
    text:      str,
    *,
    vault_root: Path,
    kind:      str,                       # "tend" or "lint"
    when:      datetime | None = None,
) -> Path:
    """Append `text` to _AGENT/log/YYYY-MM-DD-{kind}.md, mkdir as needed.

    Returns the path written to. Multiple runs the same day append with
    a separator so the log retains a chronological trail.
    """
    when = when or datetime.now(UTC)
    log_dir = vault_root / "_AGENT" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{when.strftime('%Y-%m-%d')}-{kind}.md"

    if path.exists():
        existing = path.read_text(encoding="utf-8").rstrip()
        payload  = f"{existing}\n\n---\n\n{text}"
    else:
        payload = text
    path.write_text(payload, encoding="utf-8")
    return path


# ── Helpers ─────────────────────────────────────────────────────────


def _fmt_value(v: Any) -> str:
    if isinstance(v, dict):
        return ", ".join(f"{k}={_fmt_value(val)}" for k, val in v.items())
    if isinstance(v, list):
        return f"[{len(v)} items]" if len(v) > 5 else str(v)
    return str(v)


def _fmt_sample(x: Any) -> str:
    if isinstance(x, dict):
        return "{" + ", ".join(f"{k}={v}" for k, v in x.items()) + "}"
    return str(x)
