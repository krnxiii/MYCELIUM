"""MYCELIUM Telegram bot: aiogram 3.x handlers + lifecycle."""

from __future__ import annotations

import html
import re

import structlog
from aiogram import Bot, F, Router
from aiogram import Dispatcher as AioDispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    ReplyKeyboardRemove,
)

from mycelium.telegram.agent import AgentProcess
from mycelium.telegram.dispatcher import ChannelMessage, ChannelReply, Dispatcher
from mycelium.telegram.mcp_client import MCPClient
from mycelium.telegram.middleware import (
    ACKMiddleware,
    AuthMiddleware,
    RateLimitMiddleware,
    SequentialMiddleware,
    TypingKeepAlive,
)
from mycelium.telegram.streaming import ProgressIndicator, StreamingDelivery
from mycelium.telegram.stt import STTProvider, create_stt

log = structlog.get_logger()

router = Router()


# ── Handlers ────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "<b>MYCELIUM</b> — knowledge graph interface\n\n"
        "Just text me naturally — I have full access to your knowledge graph.\n\n"
        "<i>/commands — list all shortcuts</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("commands"))
async def cmd_commands(message: Message) -> None:
    await message.answer(
        "<b>Shortcuts</b> (fast mode, no AI):\n"
        "  /capture &lt;text&gt; — save a thought\n"
        "  /search &lt;query&gt; — search the graph\n"
        "  /status — graph health + metrics\n"
        "  /today — recent signals\n"
        "  /neurons [type] — list neurons\n"
        "  /domains — list domains\n\n"
        "<b>Control:</b>\n"
        "  /abort — cancel current operation\n\n"
        "<i>Or just write naturally — AI agent handles everything.</i>",
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


@router.message(F.forward_origin)
async def handle_forward(message: Message, dispatcher: Dispatcher) -> None:
    """Forwarded message -> extract source attribution -> route to agent."""
    source = _extract_forward_source(message)
    text   = message.text or message.caption or ""
    if not text:
        return

    assert message.bot is not None
    attributed = f"[Forwarded from {source}]\n{text}"

    async with TypingKeepAlive(message):
        progress = ProgressIndicator(message.bot, message.chat.id)
        await progress.start()
        stream      = StreamingDelivery(message.bot, message.chat.id)
        channel_msg = ChannelMessage(text=attributed, chat_id=str(message.chat.id))
        last_text     = ""
        first_content = True
        async for reply in dispatcher.dispatch(channel_msg):
            if first_content:
                await progress.stop()
                first_content = False
            if reply.is_stream:
                await stream.update(reply.text)
            else:
                await stream.finalize(reply.text)
            last_text = reply.text
        if first_content:
            await progress.stop()
        if last_text:
            await stream.finalize(last_text)


@router.message(F.voice)
async def handle_voice(
    message: Message, dispatcher: Dispatcher, stt: STTProvider | None = None,
) -> None:
    """Voice message -> transcribe -> route as text."""
    if not message.voice or not message.bot:
        return

    if stt is None:
        await message.reply(
            "Voice input not configured."
            " Set MYCELIUM_TELEGRAM__STT_PROVIDER.",
        )
        return

    async with TypingKeepAlive(message):
        # Download voice file
        file = await message.bot.get_file(message.voice.file_id)
        assert file.file_path is not None
        buf = await message.bot.download_file(file.file_path)
        assert buf is not None
        audio_bytes = buf.read()

        # Transcribe
        try:
            transcript = await stt.transcribe(audio_bytes)
        except Exception as e:
            log.error("voice.transcribe_failed", error=str(e))
            await message.reply("Failed to transcribe voice message.")
            return

        if not transcript:
            await message.reply("Could not recognize speech.")
            return

        # Show transcript
        await message.reply(
            f"\U0001f3a4 <i>{html.escape(transcript)}</i>",
            parse_mode=ParseMode.HTML,
        )

        # Route through dispatcher (same as free_text)
        progress = ProgressIndicator(message.bot, message.chat.id)
        await progress.start()
        stream      = StreamingDelivery(message.bot, message.chat.id)
        channel_msg = ChannelMessage(text=transcript, chat_id=str(message.chat.id))
        last_text     = ""
        first_content = True
        async for reply in dispatcher.dispatch(channel_msg):
            if first_content:
                await progress.stop()
                first_content = False
            if reply.is_stream:
                await stream.update(reply.text)
            else:
                await stream.finalize(reply.text)
            last_text = reply.text
        if first_content:
            await progress.stop()
        if last_text:
            await stream.finalize(last_text)


@router.message()
async def free_text(message: Message, dispatcher: Dispatcher) -> None:
    """Catch-all: free text -> agent with streaming delivery."""
    if not message.text:
        return

    assert message.bot is not None

    async with TypingKeepAlive(message):
        progress = ProgressIndicator(message.bot, message.chat.id)
        await progress.start()
        stream      = StreamingDelivery(message.bot, message.chat.id)
        channel_msg = ChannelMessage(text=message.text, chat_id=str(message.chat.id))
        last_text     = ""
        first_content = True
        async for reply in dispatcher.dispatch(channel_msg):
            if first_content:
                await progress.stop()
                first_content = False
            if reply.is_stream:
                await stream.update(reply.text)
            else:
                await stream.finalize(reply.text)
            last_text = reply.text
        if first_content:
            await progress.stop()
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


def _extract_forward_source(message: Message) -> str:
    """Extract human-readable source from forward_origin."""
    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        parts = [origin.sender_user.first_name, origin.sender_user.last_name or ""]
        return " ".join(p for p in parts if p)
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.title or "channel"
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.title or "chat"
    return "unknown"


# ── MCP registration for agent ──────────────────────────────────────

def _setup_agent_mcp(mcp_url: str, auth_token: str) -> None:
    """Register MCP server via claude CLI for agent subprocess."""
    import subprocess

    # Remove stale registration
    subprocess.run(
        ["claude", "mcp", "remove", "mycelium", "-s", "user"],
        capture_output=True, timeout=10,
    )
    # Register: name + url before --header (--header is variadic)
    cmd = ["claude", "mcp", "add", "-t", "http", "-s", "user", "mycelium", mcp_url]
    if auth_token:
        cmd.extend(["--header", f"Authorization: Bearer {auth_token}"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        log.info("agent.mcp_registered", url=mcp_url)
    else:
        log.error("agent.mcp_register_failed", stderr=result.stderr[:200])


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

    # Minimal visible menu (all other commands work but are hidden)
    await bot.set_my_commands([
        BotCommand(command="status",   description="Graph health"),
        BotCommand(command="abort",    description="Cancel current operation"),
        BotCommand(command="commands", description="All shortcuts"),
    ])

    # Connect to MCP Data Node (for fast mode)
    mcp_client = MCPClient(tg.mcp_url, mcp_token)
    await mcp_client.connect()

    # Agent for full mode (claude -p subprocess)
    agent = AgentProcess(model=cfg.llm.model)

    dispatcher = Dispatcher(mcp_client, agent)

    # STT provider (optional: voice transcription)
    stt = create_stt(
        provider=tg.stt_provider,
        api_key=tg.stt_api_key,
        url=tg.stt_whisper_url,
        model=tg.stt_model,
        language=tg.stt_language,
    )

    # aiogram Dispatcher
    dp = AioDispatcher()
    dp["dispatcher"] = dispatcher
    if stt:
        dp["stt"] = stt

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
        stt_provider=tg.stt_provider if stt else "none",
    )

    try:
        await dp.start_polling(bot)
    finally:
        await mcp_client.close()
        await bot.session.close()
        log.info("telegram.stopped")
