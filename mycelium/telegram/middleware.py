"""Telegram bot middleware stack: auth, ACK, debouncer, sequentializer."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

log = structlog.get_logger()

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


# ── Auth: verify chat_id matches owner ──────────────────────────────

class AuthMiddleware(BaseMiddleware):
    """Reject messages from unauthorized users."""

    def __init__(self, owner_chat_id: int) -> None:
        self.owner_chat_id = owner_chat_id
        if owner_chat_id == 0:
            log.warning("auth.open_mode",
                        hint="MYCELIUM_TELEGRAM__OWNER_CHAT_ID not set — bot accepts all users")

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any],
    ) -> Any:
        if (
            isinstance(event, Message)
            and self.owner_chat_id != 0
            and event.chat.id != self.owner_chat_id
        ):
            log.warning("auth.rejected", chat_id=event.chat.id)
            await event.answer("Unauthorized.")
            return None
        return await handler(event, data)


# ── Rate limiter: drop messages above threshold ─────────────────────

class RateLimitMiddleware(BaseMiddleware):
    """Simple per-chat rate limiter (sliding window)."""

    def __init__(self, max_per_minute: int = 30) -> None:
        self.max_per_minute = max_per_minute
        self._timestamps: dict[int, list[float]] = defaultdict(list)

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            chat_id = event.chat.id
            now     = time.monotonic()
            # Atomic in asyncio — no await between read/write
            window  = [t for t in self._timestamps[chat_id] if now - t < 60]
            if len(window) >= self.max_per_minute:
                log.warning("rate_limit.exceeded", chat_id=chat_id)
                return None
            window.append(now)
            self._timestamps[chat_id] = window
        return await handler(event, data)


# ── ACK reaction: visual feedback on message receipt ────────────────

class ACKMiddleware(BaseMiddleware):
    """Set reaction on incoming message, remove after processing."""

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            await _set_reaction(event, "👀")
            try:
                result = await handler(event, data)
                await _remove_reaction(event)
                return result
            except Exception:
                await _remove_reaction(event)
                raise
        return await handler(event, data)


async def _set_reaction(msg: Message, emoji: str) -> None:
    """Set emoji reaction, ignore errors (not all chats support reactions)."""
    from aiogram.types import ReactionTypeEmoji
    with contextlib.suppress(Exception):
        await msg.react([ReactionTypeEmoji(emoji=emoji)])


async def _remove_reaction(msg: Message) -> None:
    with contextlib.suppress(Exception):
        await msg.react([])


# ── Sequentializer with message coalescing ─────────────────────────

_COALESCE_DELAY = 0.5  # seconds to wait for more messages after last buffered


class SequentialMiddleware(BaseMiddleware):
    """Process messages sequentially per chat. Coalesce queued messages.

    While a handler is running for a chat, incoming messages are buffered.
    When the handler finishes, buffered messages are merged into one
    and processed as a single request (collect mode).
    """

    def __init__(self) -> None:
        self._locks:   dict[int, asyncio.Lock]        = defaultdict(asyncio.Lock)
        self._buffers: dict[int, list[Message]]        = defaultdict(list)

    async def __call__(
        self, handler: Handler, event: TelegramObject, data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        chat_id = event.chat.id
        lock    = self._locks[chat_id]

        # Lock busy — buffer for coalescing, don't block
        if lock.locked():
            self._buffers[chat_id].append(event)
            log.info("sequential.buffered", chat_id=chat_id,
                      queued=len(self._buffers[chat_id]))
            return None

        # Lock free — process, then drain buffer
        async with lock:
            result = await handler(event, data)

        # After handler done: drain any messages that arrived meanwhile
        await self._drain(chat_id, handler, data)
        return result

    async def _drain(self, chat_id: int, handler: Handler, data: dict[str, Any]) -> None:
        """Drain and coalesce buffered messages."""
        while self._buffers.get(chat_id):
            # Brief pause to collect stragglers
            await asyncio.sleep(_COALESCE_DELAY)

            messages = self._buffers.pop(chat_id, [])
            if not messages:
                return

            merged = _merge_messages(messages)
            if not merged:
                return

            log.info("sequential.coalesced", chat_id=chat_id, count=len(messages))
            async with self._locks[chat_id]:
                await handler(merged, data)


def _merge_messages(messages: list[Message]) -> Message | None:
    """Merge buffered messages: combine text, keep last message as carrier."""
    texts: list[str] = []
    last_msg: Message | None = None
    for msg in messages:
        last_msg = msg
        t = msg.text or msg.caption or ""
        if t:
            texts.append(t)
    if not last_msg or not texts:
        return last_msg
    # Patch text on the carrier message (it has chat_id, bot, etc.)
    object.__setattr__(last_msg, "text", "\n".join(texts))
    return last_msg


# ── Typing keepalive ────────────────────────────────────────────────

class TypingKeepAlive:
    """Send typing action every 3s while processing. Auto-stop after TTL."""

    def __init__(self, msg: Message, ttl: float = 1800.0) -> None:
        self._msg  = msg
        self._ttl  = ttl
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> TypingKeepAlive:
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        start    = time.monotonic()
        failures = 0
        while time.monotonic() - start < self._ttl:
            try:
                await self._msg.bot.send_chat_action(  # type: ignore[union-attr]
                    self._msg.chat.id, "typing",
                )
                failures = 0
            except Exception:
                failures += 1
                if failures >= 10:  # circuit breaker
                    break
            await asyncio.sleep(3)
