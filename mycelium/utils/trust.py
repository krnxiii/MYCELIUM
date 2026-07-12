"""Trust boundary: neutralize harness-impersonating markup in untrusted text.

Graph content is ingested third-party text. When it re-enters an LLM
context — MCP tool responses, extraction prompt context — text that mimics
harness structures (fake system-reminder blocks, tool-result wrappers,
role tags) arrives with tool-output authority: a standard indirect prompt
injection vector (OWASP LLM01). A zero-width space after ``<`` breaks the
tag structurally while staying invisible to humans.

Scope limit: this raises the bar against *structural impersonation* only.
Persuasive prose needs no markup and cannot be neutralized here — the
reader harness's permission layer is the final boundary.
"""

from __future__ import annotations

import re
from typing import Any

ZWSP = chr(0x200B)  # zero-width space — breaks a tag, invisible to humans

# Tag stems that shape agent-harness structures (system blocks, tool
# call/result wrappers, role turns). Extend as new shapes appear.
_TAGS = (
    "system", "assistant", "human", "user", "instructions",
    "antml", "function", "invoke", "parameter", "tool",
)
_SUSPICIOUS = re.compile(
    r"<(?=\s*/?\s*(?:" + "|".join(_TAGS) + r")[\w:-]*[\s>/])",
    re.IGNORECASE,
)


def neutralize(text: str) -> str:
    """Break harness-shaped tags in untrusted text with an invisible ZWSP."""
    return _SUSPICIOUS.sub(f"<{ZWSP}", text)


def neutralize_tree(obj: Any) -> Any:
    """Recursively neutralize every string in a JSON-like structure."""
    if isinstance(obj, str):
        return neutralize(obj)
    if isinstance(obj, dict):
        return {neutralize_tree(k): neutralize_tree(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [neutralize_tree(v) for v in obj]
    return obj
