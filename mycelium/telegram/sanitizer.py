"""Telegram HTML sanitizer: strip → balance → truncate."""

from __future__ import annotations

import re

# Telegram-safe HTML tags
ALLOWED_TAGS = frozenset({
    "b", "i", "code", "pre", "a", "s", "u", "blockquote", "tg-spoiler",
})
_TAG_RE = re.compile(r"<(/?)(\w[\w-]*)([^>]*)>")


def sanitize_html(text: str, max_len: int = 0) -> str:
    """Strip non-allowed tags → balance → truncate for Telegram."""
    text = _strip_disallowed(text)
    text = _balance_tags(text)
    if max_len and len(text) > max_len:
        text = _truncate_safe(text, max_len)
    return text


def strip_tags(text: str) -> str:
    """Remove ALL HTML tags (plain text fallback)."""
    return _TAG_RE.sub("", text)


def _strip_disallowed(text: str) -> str:
    """Keep only Telegram-safe tags."""
    def _repl(m: re.Match) -> str:
        return m.group(0) if m.group(2).lower() in ALLOWED_TAGS else ""
    return _TAG_RE.sub(_repl, text)


def _balance_tags(text: str) -> str:
    """Auto-close unclosed tags in reverse order."""
    stack: list[str] = []
    for m in _TAG_RE.finditer(text):
        tag = m.group(2).lower()
        if tag not in ALLOWED_TAGS:
            continue
        if m.group(1):  # closing tag
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            stack.append(tag)
    for tag in reversed(stack):
        text += f"</{tag}>"
    return text


def _truncate_safe(text: str, max_len: int) -> str:
    """Truncate without breaking inside a tag, then rebalance."""
    cut = max_len
    last_lt = text.rfind("<", 0, cut)
    last_gt = text.rfind(">", 0, cut)
    if last_lt > last_gt:
        cut = last_lt
    return _balance_tags(text[:cut])
