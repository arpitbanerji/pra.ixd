"""How the bot talks back to Telegram.

Defined as a protocol so the conversation runner can be exercised without a
live bot token, and implemented once here for the real Telegram client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter

from ohbot.render import split_for_telegram, strip_html, to_telegram_html

log = logging.getLogger(__name__)

ButtonRows = Sequence[Sequence[tuple[str, str]]]


class ChatSurface(Protocol):
    async def send_markdown(
        self, chat_id: int, markdown: str, *, buttons: ButtonRows | None = None
    ) -> list[int]: ...

    async def edit_markdown(
        self, chat_id: int, message_id: int, markdown: str, *, buttons: ButtonRows | None = None
    ) -> None: ...

    async def replace_with_markdown(
        self, chat_id: int, message_id: int, markdown: str, *, buttons: ButtonRows | None = None
    ) -> list[int]: ...

    async def send_typing(self, chat_id: int) -> None: ...


def _keyboard(buttons: ButtonRows | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in buttons]
    )


class TelegramSurface:
    """Thin wrapper over :class:`telegram.Bot` with rendering and retry handling."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def _send_one(
        self, chat_id: int, markdown: str, buttons: ButtonRows | None
    ) -> int:
        html = to_telegram_html(markdown)
        markup = _keyboard(buttons)
        try:
            message = await self._bot.send_message(
                chat_id=chat_id, text=html, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        except BadRequest as exc:
            # Agent output can contain markup our renderer cannot make safe;
            # plain text is always deliverable.
            log.warning("telegram_html_rejected", extra={"error": str(exc)})
            message = await self._bot.send_message(chat_id=chat_id, text=strip_html(html))
        return message.message_id

    async def send_markdown(
        self, chat_id: int, markdown: str, *, buttons: ButtonRows | None = None
    ) -> list[int]:
        chunks = split_for_telegram(markdown) or [""]
        ids: list[int] = []
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            ids.append(await self._send_one(chat_id, chunk, buttons if last else None))
        return ids

    async def edit_markdown(
        self, chat_id: int, message_id: int, markdown: str, *, buttons: ButtonRows | None = None
    ) -> None:
        chunks = split_for_telegram(markdown) or [""]
        html = to_telegram_html(chunks[0])
        markup = _keyboard(buttons)
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=html,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except BadRequest as exc:
            text = str(exc).lower()
            if "not modified" in text:
                return
            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=strip_html(html)
                )
            except BadRequest as inner:
                if "not modified" not in str(inner).lower():
                    raise

    async def replace_with_markdown(
        self, chat_id: int, message_id: int, markdown: str, *, buttons: ButtonRows | None = None
    ) -> list[int]:
        """Turn a placeholder message into the answer, then post any overflow."""
        chunks = split_for_telegram(markdown) or [""]
        await self.edit_markdown(chat_id, message_id, chunks[0], buttons=buttons)
        ids = [message_id]
        for chunk in chunks[1:]:
            ids.append(await self._send_one(chat_id, chunk, None))
        return ids

    async def send_typing(self, chat_id: int) -> None:
        try:
            await self._bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
        except BadRequest:
            pass
