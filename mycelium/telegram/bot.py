"""MYCELIUM Telegram bot: aiogram 3.x handlers + lifecycle."""

from __future__ import annotations

import structlog
from aiogram import Bot, Router
from aiogram import Dispatcher as AioDispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message

from mycelium.telegram.dispatcher import ChannelMessage, ChannelReply, Dispatcher
from mycelium.telegram.keyboard import main_keyboard
from mycelium.telegram.mcp_client import MCPClient
from mycelium.telegram.middleware import (
    ACKMiddleware,
    AuthMiddleware,
    RateLimitMiddleware,
    SequentialMiddleware,
    TypingKeepAlive,
)

log = structlog.get_logger()

router = Router()


# ── Handlers ────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "<b>MYCELIUM</b> — knowledge graph interface\n\n"
        "Commands:\n"
        "  /capture &lt;text&gt; — save a thought\n"
        "  /search &lt;query&gt; — search the graph\n"
        "  /status — graph health\n"
        "  /today — recent signals\n"
        "  /neurons — top neurons\n"
        "  /domains — active domains\n",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "/capture &lt;text&gt; — capture a thought\n"
        "/search &lt;query&gt; — search knowledge graph\n"
        "/status — graph health + metrics\n"
        "/today — recent signals\n"
        "/neurons [type] — list neurons\n"
        "/domains — list domains",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("capture"))
async def cmd_capture(message: Message, dispatcher: Dispatcher) -> None:
    async with TypingKeepAlive(message):
        channel_msg = ChannelMessage(
            text=message.text or "",
            chat_id=str(message.chat.id),
        )
        async for reply in dispatcher.dispatch(channel_msg):
            await _send_reply(message, reply)


@router.message(Command("search"))
async def cmd_search(message: Message, dispatcher: Dispatcher) -> None:
    async with TypingKeepAlive(message):
        channel_msg = ChannelMessage(
            text=message.text or "",
            chat_id=str(message.chat.id),
        )
        async for reply in dispatcher.dispatch(channel_msg):
            await _send_reply(message, reply)


@router.message(Command("status"))
async def cmd_status(message: Message, dispatcher: Dispatcher) -> None:
    async with TypingKeepAlive(message):
        channel_msg = ChannelMessage(
            text="/status",
            chat_id=str(message.chat.id),
        )
        async for reply in dispatcher.dispatch(channel_msg):
            await _send_reply(message, reply)


@router.message(Command("today"))
async def cmd_today(message: Message, dispatcher: Dispatcher) -> None:
    async with TypingKeepAlive(message):
        channel_msg = ChannelMessage(
            text="/today",
            chat_id=str(message.chat.id),
        )
        async for reply in dispatcher.dispatch(channel_msg):
            await _send_reply(message, reply)


@router.message(Command("neurons"))
async def cmd_neurons(message: Message, dispatcher: Dispatcher) -> None:
    async with TypingKeepAlive(message):
        channel_msg = ChannelMessage(
            text=message.text or "/neurons",
            chat_id=str(message.chat.id),
        )
        async for reply in dispatcher.dispatch(channel_msg):
            await _send_reply(message, reply)


@router.message(Command("domains"))
async def cmd_domains(message: Message, dispatcher: Dispatcher) -> None:
    async with TypingKeepAlive(message):
        channel_msg = ChannelMessage(
            text="/domains",
            chat_id=str(message.chat.id),
        )
        async for reply in dispatcher.dispatch(channel_msg):
            await _send_reply(message, reply)


@router.message()
async def free_text(message: Message, dispatcher: Dispatcher) -> None:
    """Catch-all: free text → dispatcher (Phase 3: claude -p)."""
    channel_msg = ChannelMessage(
        text=message.text or "",
        chat_id=str(message.chat.id),
    )
    async for reply in dispatcher.dispatch(channel_msg):
        await _send_reply(message, reply)


# ── Reply delivery with fallback chain ──────────────────────────────

async def _send_reply(message: Message, reply: ChannelReply) -> None:
    """Send reply: try HTML first, fallback to plain text. Split if >4096."""
    text = reply.html or reply.text
    mode = ParseMode.HTML if reply.html else None

    for chunk in _split_text(text, 4096):
        try:
            await message.answer(chunk, parse_mode=mode)
        except Exception:
            # Fallback: plain text (HTML parse error)
            if mode:
                await message.answer(
                    _strip_html(chunk) if reply.html else chunk,
                )


def _split_text(text: str, limit: int) -> list[str]:
    """Split text at paragraph boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find last newline before limit
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


def _strip_html(text: str) -> str:
    """Naive HTML tag removal for fallback."""
    import re
    return re.sub(r"<[^>]+>", "", text)


# ── Bot lifecycle ───────────────────────────────────────────────────

async def run_bot() -> None:
    """Main entry point: configure bot, connect MCP, start polling."""
    from mycelium.config import load_settings

    cfg = load_settings()
    tg  = cfg.telegram

    if not tg.bot_token:
        log.error("telegram.no_token", hint="Set MYCELIUM_TELEGRAM__BOT_TOKEN")
        return

    # MCP auth: use telegram-specific token, fallback to MCP server token
    mcp_token = tg.mcp_auth_token or cfg.mcp.auth_token

    bot = Bot(
        token=tg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Set bot commands menu
    await bot.set_my_commands([
        BotCommand(command="capture", description="Capture a thought"),
        BotCommand(command="search",  description="Search knowledge graph"),
        BotCommand(command="status",  description="Graph health"),
        BotCommand(command="today",   description="Recent signals"),
        BotCommand(command="neurons", description="Top neurons"),
        BotCommand(command="domains", description="Active domains"),
        BotCommand(command="help",    description="Show help"),
    ])

    # Connect to MCP Data Node
    mcp_client = MCPClient(tg.mcp_url, mcp_token)
    await mcp_client.connect()

    dispatcher = Dispatcher(mcp_client)

    # aiogram Dispatcher
    dp = AioDispatcher()
    dp["dispatcher"] = dispatcher

    # Middleware stack (order matters: first registered = outermost)
    router.message.middleware(RateLimitMiddleware(tg.rate_limit))
    router.message.middleware(AuthMiddleware(tg.owner_chat_id))
    router.message.middleware(SequentialMiddleware())
    router.message.middleware(ACKMiddleware())

    dp.include_router(router)

    log.info(
        "telegram.starting",
        mcp_url=tg.mcp_url,
        owner_chat_id=tg.owner_chat_id,
    )

    try:
        await dp.start_polling(bot)
    finally:
        await mcp_client.close()
        await bot.session.close()
        log.info("telegram.stopped")
