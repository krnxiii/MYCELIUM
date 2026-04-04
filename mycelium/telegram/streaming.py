"""Streaming delivery: progressive editMessageText for Telegram."""

from __future__ import annotations

import asyncio
import time

import structlog
from aiogram import Bot

log = structlog.get_logger()

# Telegram limits
MAX_MESSAGE_LEN = 4096
MIN_INITIAL_CHARS = 30
EDIT_THROTTLE_SEC = 1.0


class ProgressIndicator:
    """Show elapsed time message until first content arrives."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot     = bot
        self._chat_id = chat_id
        self._msg_id: int | None = None
        self._task: asyncio.Task | None = None
        self._start   = 0.0

    async def start(self) -> None:
        msg = await self._bot.send_message(self._chat_id, "\u23f3 ...")
        self._msg_id = msg.message_id
        self._start  = time.monotonic()
        self._task   = asyncio.create_task(self._tick())

    async def _tick(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                elapsed = int(time.monotonic() - self._start)
                try:
                    await self._bot.edit_message_text(
                        f"\u23f3 Thinking... ({elapsed}s)",
                        chat_id=self._chat_id,
                        message_id=self._msg_id,
                    )
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def stop(self) -> int | None:
        """Stop timer, delete progress message. Returns deleted msg_id."""
        if self._task:
            self._task.cancel()
        if self._msg_id:
            try:
                await self._bot.delete_message(self._chat_id, self._msg_id)
            except Exception:
                pass
            return self._msg_id
        return None


class StreamingDelivery:
    """Buffer agent chunks, deliver via sendMessage + editMessageText."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot     = bot
        self._chat_id = chat_id
        self._msg_id: int | None = None
        self._last_edit_at = 0.0
        self._last_text    = ""
        self._overflow: list[int] = []  # message_ids for overflow messages

    async def update(self, text: str) -> None:
        """Push new accumulated text. Sends or edits as needed."""
        if not text or text == self._last_text:
            return

        # First message: wait for minimum chars
        if self._msg_id is None:
            if len(text) < MIN_INITIAL_CHARS:
                return
            await self._send_initial(text)
            return

        # Throttle edits
        now = time.monotonic()
        if now - self._last_edit_at < EDIT_THROTTLE_SEC:
            return

        # Handle overflow (>4096 chars)
        if len(text) > MAX_MESSAGE_LEN:
            await self._handle_overflow(text)
            return

        await self._edit(text)

    async def finalize(self, text: str) -> None:
        """Send final version of the text."""
        if not text:
            return

        if self._msg_id is None:
            # Never sent anything — send as single message
            for chunk in _split_text(text, MAX_MESSAGE_LEN):
                await self._send_plain(chunk)
            return

        # Final edit with full text
        if len(text) > MAX_MESSAGE_LEN:
            await self._handle_overflow(text)
        elif text != self._last_text:
            await self._edit(text)

    async def _send_initial(self, text: str) -> None:
        """Send first message."""
        try:
            msg = await self._bot.send_message(
                self._chat_id,
                text[:MAX_MESSAGE_LEN],
            )
            self._msg_id = msg.message_id
            self._last_text = text[:MAX_MESSAGE_LEN]
            self._last_edit_at = time.monotonic()
        except Exception as e:
            log.warning("streaming.send_failed", error=str(e))

    async def _edit(self, text: str) -> None:
        """Edit current message with new text."""
        try:
            await self._bot.edit_message_text(
                text=text,
                chat_id=self._chat_id,
                message_id=self._msg_id,
            )
            self._last_text = text
            self._last_edit_at = time.monotonic()
        except Exception as e:
            err = str(e)
            # "message is not modified" is expected (duplicate edit)
            if "not modified" not in err.lower():
                log.warning("streaming.edit_failed", error=err)

    async def _send_plain(self, text: str) -> None:
        """Send a new plain message (for overflow/fallback)."""
        try:
            msg = await self._bot.send_message(self._chat_id, text)
            self._overflow.append(msg.message_id)
        except Exception as e:
            log.warning("streaming.overflow_send_failed", error=str(e))

    async def _handle_overflow(self, text: str) -> None:
        """Text exceeds 4096 — edit current msg with first part, send rest."""
        # Edit current message with first chunk
        first = text[:MAX_MESSAGE_LEN]
        if first != self._last_text:
            await self._edit(first)

        # Send remaining as new messages
        remaining = text[MAX_MESSAGE_LEN:]
        for chunk in _split_text(remaining, MAX_MESSAGE_LEN):
            await self._send_plain(chunk)
            await asyncio.sleep(0.3)  # avoid rate limit


def _split_text(text: str, limit: int) -> list[str]:
    """Split text at paragraph boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks
