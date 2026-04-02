"""Persistent reply keyboard for Telegram bot."""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# Button text → command mapping (for keyboard button handler)
BUTTON_COMMANDS: dict[str, str] = {
    "/status":  "/status",
    "/today":   "/today",
    "/neurons": "/neurons",
    "/domains": "/domains",
}


def main_keyboard() -> ReplyKeyboardMarkup:
    """Build persistent reply keyboard with common commands."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/status"),  KeyboardButton(text="/today")],
            [KeyboardButton(text="/neurons"), KeyboardButton(text="/domains")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
