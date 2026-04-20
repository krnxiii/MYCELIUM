"""Trigger-based domain matching for auto-detection."""

from __future__ import annotations

import structlog

from mycelium.domain.models import DomainBlueprint

log = structlog.get_logger()


def match_domain(
    domains:     list[DomainBlueprint],
    content:     str = "",
    name:        str = "",
    source_desc: str = "",
) -> DomainBlueprint | None:
    """Find matching domain by trigger keywords.

    Checks if ANY trigger appears in content (first 2000 chars),
    filename, or source description. Case-insensitive.
    Returns first match (newest domain wins — list assumed sorted).

    Args:
        domains:     available blueprints
        content:     document text (truncated to 2000 chars)
        name:        filename or signal name
        source_desc: source description
    """
    if not domains:
        return None

    raw = f"{name} {source_desc} {content[:2000]}".lower()
    # Normalize separators so "blood_test" matches trigger "blood test".
    # Apply same normalization to triggers — otherwise an underscore/dash
    # in a trigger would never match any content.
    search_text = _normalize(raw)

    for domain in reversed(domains):  # newest first
        for trigger in domain.triggers:
            if _normalize(trigger.lower()) in search_text:
                log.info(
                    "domain_matched",
                    domain=domain.name,
                    trigger=trigger,
                )
                return domain

    return None


def _normalize(s: str) -> str:
    return s.replace("_", " ").replace("-", " ")
