"""Tests for MYCELIUM Telegram bot: aiogram-dependent tests.

Skipped when aiogram is not installed (optional dependency).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("aiogram", reason="aiogram not installed")


# ── bot._split_text ──────────────────────────────────────────────────

class TestSplitText:
    @staticmethod
    def _split(text: str, limit: int = 4096) -> list[str]:
        from mycelium.telegram.bot import _split_text
        return _split_text(text, limit)

    def test_short_text_no_split(self) -> None:
        assert self._split("hello") == ["hello"]

    def test_exact_limit(self) -> None:
        text = "a" * 4096
        assert self._split(text) == [text]

    def test_split_at_newline(self) -> None:
        line = "x" * 50
        text = (line + "\n") * 100  # 5100 chars
        chunks = self._split(text, 200)
        for chunk in chunks:
            assert len(chunk) <= 200

    def test_split_no_newline_fallback(self) -> None:
        """When no newline exists, cut at hard limit."""
        text = "a" * 300
        chunks = self._split(text, 100)
        assert chunks[0] == "a" * 100
        assert "".join(chunks) == text

    def test_empty_text(self) -> None:
        assert self._split("") == [""]

    def test_split_preserves_all_content(self) -> None:
        """Joined chunks reproduce original (minus stripped newlines between chunks)."""
        text = "line1\nline2\nline3\nline4\nline5"
        chunks = self._split(text, 12)
        assert all(len(c) <= 12 for c in chunks)
        joined = "\n".join(chunks)
        for line in ["line1", "line2", "line3", "line4", "line5"]:
            assert line in joined

    def test_trailing_newline(self) -> None:
        text = "abc\ndef\n"
        chunks = self._split(text, 5)
        joined = "\n".join(chunks)
        assert "abc" in joined
        assert "def" in joined


# ── bot._extract_forward_source ──────────────────────────────────────

class TestExtractForwardSource:
    @staticmethod
    def _extract(message: object) -> str:
        from mycelium.telegram.bot import _extract_forward_source
        return _extract_forward_source(message)  # type: ignore[arg-type]

    def test_origin_none(self) -> None:
        msg = SimpleNamespace(forward_origin=None)
        assert self._extract(msg) == "unknown"

    def test_origin_user(self) -> None:
        from aiogram.types import MessageOriginUser, User
        user = User(id=1, is_bot=False, first_name="Alice", last_name="Smith")
        origin = MessageOriginUser(
            type="user",
            date=0,  # type: ignore[arg-type]
            sender_user=user,
        )
        msg = SimpleNamespace(forward_origin=origin)
        assert self._extract(msg) == "Alice Smith"

    def test_origin_user_no_last_name(self) -> None:
        from aiogram.types import MessageOriginUser, User
        user = User(id=1, is_bot=False, first_name="Bob")
        origin = MessageOriginUser(
            type="user",
            date=0,  # type: ignore[arg-type]
            sender_user=user,
        )
        msg = SimpleNamespace(forward_origin=origin)
        assert self._extract(msg) == "Bob"

    def test_origin_channel(self) -> None:
        from aiogram.types import Chat, MessageOriginChannel
        chat = Chat(id=-100, type="channel", title="News")
        origin = MessageOriginChannel(
            type="channel",
            date=0,  # type: ignore[arg-type]
            chat=chat,
            message_id=42,
        )
        msg = SimpleNamespace(forward_origin=origin)
        assert self._extract(msg) == "News"

    def test_origin_hidden_user(self) -> None:
        from aiogram.types import MessageOriginHiddenUser
        origin = MessageOriginHiddenUser(
            type="hidden_user",
            date=0,  # type: ignore[arg-type]
            sender_user_name="Ghost",
        )
        msg = SimpleNamespace(forward_origin=origin)
        assert self._extract(msg) == "Ghost"

    def test_origin_hidden_user_no_name(self) -> None:
        from aiogram.types import MessageOriginHiddenUser
        origin = MessageOriginHiddenUser(
            type="hidden_user",
            date=0,  # type: ignore[arg-type]
            sender_user_name="",
        )
        msg = SimpleNamespace(forward_origin=origin)
        assert self._extract(msg) == "hidden user"

    def test_origin_chat(self) -> None:
        from aiogram.types import Chat, MessageOriginChat
        chat = Chat(id=-200, type="group", title="Dev Team")
        origin = MessageOriginChat(
            type="chat",
            date=0,  # type: ignore[arg-type]
            sender_chat=chat,
        )
        msg = SimpleNamespace(forward_origin=origin)
        assert self._extract(msg) == "Dev Team"

    def test_unknown_origin_type(self) -> None:
        """Fallback for unexpected origin type."""
        msg = SimpleNamespace(forward_origin=SimpleNamespace())
        assert self._extract(msg) == "unknown"


# ── bot._INTERACTION_LEVELS ──────────────────────────────────────────

class TestInteractionLevels:
    def test_expected_values(self) -> None:
        from mycelium.telegram.bot import _INTERACTION_LEVELS
        assert _INTERACTION_LEVELS == ("silent", "minimal", "balanced", "curious")

    def test_is_tuple(self) -> None:
        from mycelium.telegram.bot import _INTERACTION_LEVELS
        assert isinstance(_INTERACTION_LEVELS, tuple)


# ── middleware._merge_messages ────────────────────────────────────────

class TestMergeMessages:
    @staticmethod
    def _make_msg(text: str | None = None, caption: str | None = None) -> MagicMock:
        """Minimal Message-like mock."""
        msg = MagicMock()
        msg.text = text
        msg.caption = caption
        return msg

    @staticmethod
    def _merge(messages: list) -> object:
        from mycelium.telegram.middleware import _merge_messages
        return _merge_messages(messages)

    def test_single_message(self) -> None:
        msg = self._make_msg("hello")
        result = self._merge([msg])
        assert result is msg

    def test_two_messages_merged(self) -> None:
        m1 = self._make_msg("first")
        m2 = self._make_msg("second")
        result = self._merge([m1, m2])
        assert result is m2
        assert result.text == "first\nsecond"

    def test_caption_fallback(self) -> None:
        m1 = self._make_msg(text=None, caption="photo caption")
        m2 = self._make_msg("text msg")
        result = self._merge([m1, m2])
        assert "photo caption" in result.text
        assert "text msg" in result.text

    def test_all_empty_text(self) -> None:
        m1 = self._make_msg(text=None, caption=None)
        result = self._merge([m1])
        assert result is m1

    def test_empty_list(self) -> None:
        result = self._merge([])
        assert result is None

    def test_mixed_text_and_none(self) -> None:
        m1 = self._make_msg(text=None)
        m2 = self._make_msg("only this")
        m3 = self._make_msg(text=None)
        result = self._merge([m1, m2, m3])
        assert result.text == "only this"
