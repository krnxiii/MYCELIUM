"""Declarative Telegram renderer: idempotent state→chat reconciliation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

import structlog
from aiogram import Bot

log = structlog.get_logger()

# Telegram limits
MAX_MESSAGE_LEN   = 4096
MIN_INITIAL_CHARS = 30
EDIT_THROTTLE_SEC = 1.0
EMOJI_THINKING    = "\U0001f914"  # 🤔
_BRAILLE          = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_PROGRESS_TICK    = 5.0  # seconds between spinner updates


@dataclass
class _Chunk:
    msg_id: int
    text:   str   # invariant: this is the text currently in Telegram for msg_id


class TelegramRenderer:
    """Declarative renderer: `render(text)` reconciles chat to match `text`.

    State: list of rendered Telegram messages (one per ≤4096-char chunk).
    Invariant: after render(T), chat contains _split_text(T) verbatim.
    Idempotency: render(T); render(T) makes 0 extra API calls.

    Throttling: edits to existing chunks are throttled to one per
    EDIT_THROTTLE_SEC. New chunk sends are never throttled (new content,
    not spam). Pass force=True for the final flush to bypass throttle.
    """

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot           = bot
        self._chat_id       = chat_id
        self._chunks:        list[_Chunk]               = []
        self._last_edit_at:  float                      = 0.0
        self._progress_id:   int | None                 = None
        self._progress_task: asyncio.Task[None] | None  = None
        self._progress_start: float                     = 0.0

    # ── Progress indicator (folded in) ──────────────────────────────

    async def show_progress(self) -> None:
        """Show 'thinking' indicator. Idempotent: no-op if already shown
        or if any content has been rendered."""
        if self._chunks or self._progress_id is not None:
            return
        try:
            msg = await self._bot.send_message(
                self._chat_id, f"{EMOJI_THINKING} thinking {_BRAILLE[0]}",
            )
        except Exception as e:
            log.warning("renderer.progress_send_failed", error=str(e))
            return
        self._progress_id    = msg.message_id
        self._progress_start = time.monotonic()
        self._progress_task  = asyncio.create_task(self._tick())

    async def _tick(self) -> None:
        i = 0
        try:
            while True:
                await asyncio.sleep(_PROGRESS_TICK)
                i += 1
                elapsed = int(time.monotonic() - self._progress_start)
                spin    = _BRAILLE[i % len(_BRAILLE)]
                with contextlib.suppress(Exception):
                    await self._bot.edit_message_text(
                        f"{EMOJI_THINKING} thinking {spin} [{elapsed}s]",
                        chat_id=self._chat_id,
                        message_id=self._progress_id,
                    )
        except asyncio.CancelledError:
            pass

    async def _stop_progress(self) -> None:
        """Cancel tick task and delete progress message. Idempotent."""
        if self._progress_task:
            self._progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._progress_task
            self._progress_task = None
        if self._progress_id is not None:
            with contextlib.suppress(Exception):
                await self._bot.delete_message(self._chat_id, self._progress_id)
            self._progress_id = None

    # ── Render ──────────────────────────────────────────────────────

    async def render(self, text: str, *, force: bool = False) -> None:
        """Reconcile chat to match `text`.

        force=True bypasses throttle and the MIN_INITIAL_CHARS gate
        (use for the final flush after streaming ends).
        """
        if not text:
            return
        desired = _split_text(text, MAX_MESSAGE_LEN)
        if not desired:
            return

        # Avoid posting a near-empty first message that gets immediately edited.
        if not self._chunks and not force and len(text) < MIN_INITIAL_CHARS:
            return

        # First content arriving — replace progress spinner.
        if not self._chunks:
            await self._stop_progress()

        now      = time.monotonic()
        can_edit = force or (now - self._last_edit_at) >= EDIT_THROTTLE_SEC

        for i, want in enumerate(desired):
            if i < len(self._chunks):
                if self._chunks[i].text == want:
                    continue                        # already in sync
                if not can_edit:
                    continue                        # throttled; state stays stale
                if await self._edit(self._chunks[i].msg_id, want):
                    self._chunks[i].text = want
                    self._last_edit_at   = now
            else:
                msg_id = await self._send(want)
                if msg_id is not None:
                    self._chunks.append(_Chunk(msg_id=msg_id, text=want))
                    self._last_edit_at = now      # arm throttle from first send

    async def close(self) -> None:
        """Cleanup if no content was ever rendered (kill spinner)."""
        if not self._chunks:
            await self._stop_progress()

    # ── Telegram primitives ─────────────────────────────────────────

    async def _send(self, text: str) -> int | None:
        try:
            msg = await self._bot.send_message(self._chat_id, text)
            return msg.message_id
        except Exception as e:
            log.warning("renderer.send_failed", error=str(e))
            return None

    async def _edit(self, msg_id: int, text: str) -> bool:
        try:
            await self._bot.edit_message_text(
                text=text,
                chat_id=self._chat_id,
                message_id=msg_id,
            )
            return True
        except Exception as e:
            err = str(e)
            if "not modified" in err.lower():
                # State drift: chat already has this text. Treat as success
                # so the caller can sync local state.
                return True
            log.warning("renderer.edit_failed", error=err, msg_id=msg_id)
            return False


def _split_text(text: str, limit: int) -> list[str]:
    """Split text at paragraph boundaries (≤ limit chars per chunk)."""
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
