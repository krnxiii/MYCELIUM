"""MYCELIUM Telegram bot: aiogram 3.x handlers + lifecycle."""

from __future__ import annotations

import re

import structlog
from aiogram import Bot, Router
from aiogram import Dispatcher as AioDispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message

from mycelium.telegram.agent import AgentProcess
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
from mycelium.telegram.streaming import StreamingDelivery

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
        "  /domains — active domains\n"
        "  /abort — cancel current operation\n\n"
        "<i>Free text → AI agent with full graph access</i>",
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
        "/domains — list domains\n"
        "/abort — cancel current operation\n\n"
        "Free text → AI agent (Claude) with MCP tools",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("abort"))
async def cmd_abort(message: Message, dispatcher: Dispatcher) -> None:
    """Priority lane: kill current agent subprocess."""
    if dispatcher.abort():
        await message.answer("Aborted.")
    else:
        await message.answer("Nothing to abort.")


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
        channel_msg = ChannelMessage(text="/status", chat_id=str(message.chat.id))
        async for reply in dispatcher.dispatch(channel_msg):
            await _send_reply(message, reply)


@router.message(Command("today"))
async def cmd_today(message: Message, dispatcher: Dispatcher) -> None:
    async with TypingKeepAlive(message):
        channel_msg = ChannelMessage(text="/today", chat_id=str(message.chat.id))
        async for reply in dispatcher.dispatch(channel_msg):
            await _send_reply(message, reply)


@router.message(Command("neurons"))
async def cmd_neurons(message: Message, dispatcher: Dispatcher) -> None:
    async with TypingKeepAlive(message):
        channel_msg = ChannelMessage(
            text=message.text or "/neurons", chat_id=str(message.chat.id),
        )
        async for reply in dispatcher.dispatch(channel_msg):
            await _send_reply(message, reply)


@router.message(Command("domains"))
async def cmd_domains(message: Message, dispatcher: Dispatcher) -> None:
    async with TypingKeepAlive(message):
        channel_msg = ChannelMessage(text="/domains", chat_id=str(message.chat.id))
        async for reply in dispatcher.dispatch(channel_msg):
            await _send_reply(message, reply)


@router.message()
async def free_text(message: Message, dispatcher: Dispatcher) -> None:
    """Catch-all: free text → agent with streaming delivery."""
    if not message.text:
        return

    assert message.bot is not None

    async with TypingKeepAlive(message):
        stream = StreamingDelivery(message.bot, message.chat.id)
        channel_msg = ChannelMessage(
            text=message.text,
            chat_id=str(message.chat.id),
        )
        last_text = ""
        async for reply in dispatcher.dispatch(channel_msg):
            if reply.is_stream:
                await stream.update(reply.text)
            else:
                await stream.finalize(reply.text)
            last_text = reply.text

        # Ensure final text is delivered
        if last_text:
            await stream.finalize(last_text)


# ── Reply delivery with fallback chain ──────────────────────────────

async def _send_reply(message: Message, reply: ChannelReply) -> None:
    """Send reply: try HTML first, fallback to plain text. Split if >4096."""
    text = reply.html or reply.text
    mode = ParseMode.HTML if reply.html else None

    for chunk in _split_text(text, 4096):
        try:
            await message.answer(chunk, parse_mode=mode)
        except Exception:
            if mode:
                await message.answer(_strip_html(chunk) if reply.html else chunk)


def _split_text(text: str, limit: int) -> list[str]:
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


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# ── MCP registration for agent ──────────────────────────────────────

def _setup_agent_mcp(mcp_url: str, auth_token: str) -> None:
    """Register MCP server in project-level Claude settings for agent."""
    import json
    from pathlib import Path

    settings_dir = Path("/app/.claude")
    settings_dir.mkdir(exist_ok=True)
    server_cfg: dict = {"type": "http", "url": mcp_url}
    if auth_token:
        server_cfg["headers"] = {"Authorization": f"Bearer {auth_token}"}
    settings = {"mcpServers": {"mycelium": server_cfg}}
    (settings_dir / "settings.json").write_text(json.dumps(settings, indent=2))
    log.info("agent.mcp_registered", url=mcp_url)


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

    # Register MCP server for claude -p agent
    _setup_agent_mcp(tg.mcp_url, mcp_token)

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
        BotCommand(command="abort",   description="Cancel current operation"),
        BotCommand(command="help",    description="Show help"),
    ])

    # Connect to MCP Data Node (for fast mode)
    mcp_client = MCPClient(tg.mcp_url, mcp_token)
    await mcp_client.connect()

    # Agent for full mode (claude -p subprocess)
    agent = AgentProcess(model=cfg.llm.model)

    dispatcher = Dispatcher(mcp_client, agent)

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
        agent_model=cfg.llm.model,
    )

    try:
        await dp.start_polling(bot)
    finally:
        await mcp_client.close()
        await bot.session.close()
        log.info("telegram.stopped")
