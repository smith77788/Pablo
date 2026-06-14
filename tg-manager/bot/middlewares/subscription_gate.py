"""Subscription gate middleware — blocks bot access until user subscribes to required channels."""

from __future__ import annotations
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

log = logging.getLogger(__name__)

_gate_enabled: bool = False
_gate_channels: list[dict] = []


def set_gate_enabled(value: bool) -> None:
    global _gate_enabled
    _gate_enabled = value


def get_gate_enabled() -> bool:
    return _gate_enabled


def set_gate_channels(channels) -> None:
    global _gate_channels
    _gate_channels = [dict(r) for r in channels]


def get_gate_channels() -> list[dict]:
    return list(_gate_channels)


def _is_admin(user_id: int) -> bool:
    raw = os.getenv("ADMIN_IDS", "")
    return user_id in {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def _gate_markup(channels: list[dict]):
    kb = InlineKeyboardBuilder()
    for ch in channels:
        title = ch.get("channel_title") or ch["channel_username"]
        url = "https://t.me/" + ch["channel_username"].lstrip("@")
        kb.button(text=f"📢 {title}", url=url)
    kb.button(text="✅ Я подписался — проверить", callback_data="gate:check")
    kb.adjust(1)
    return kb.as_markup()


def _gate_text(channels: list[dict]) -> str:
    lines = []
    for ch in channels:
        title = ch.get("channel_title") or ch["channel_username"]
        url = "https://t.me/" + ch["channel_username"].lstrip("@")
        lines.append(f'• <a href="{url}">{title}</a>')
    body = "\n".join(lines)
    return (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Для использования бота необходимо подписаться на наши каналы:\n\n"
        f"{body}\n\n"
        "После подписки нажмите кнопку ниже ↓"
    )


async def _check_membership(bot, user_id: int, channels: list[dict]) -> list[dict]:
    """Returns list of channels user is NOT subscribed to."""
    not_subscribed = []
    for ch in channels:
        username = ch["channel_username"]
        try:
            member = await bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status in ("left", "kicked", "banned"):
                not_subscribed.append(ch)
        except Exception:
            # Fail-closed: if check fails (bot not in channel / API error), block user
            log.warning("subscription_gate: cannot check %s — blocking user %s", username, user_id)
            not_subscribed.append(ch)
    return not_subscribed


class SubscriptionGateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not _gate_enabled or not _gate_channels:
            return await handler(event, data)

        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        user = event.from_user
        if not user:
            return await handler(event, data)

        if _is_admin(user.id):
            return await handler(event, data)

        bot = data.get("bot")
        if not bot:
            return await handler(event, data)

        # Handle "check" button — re-verify and unblock if subscribed
        if isinstance(event, CallbackQuery) and event.data == "gate:check":
            missing = await _check_membership(bot, user.id, _gate_channels)
            if not missing:
                await event.answer("✅ Подписка подтверждена!")
                try:
                    await event.message.edit_text(
                        "✅ <b>Спасибо за подписку!</b>\n\nНажмите /start чтобы продолжить.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            else:
                names = ", ".join(
                    ch.get("channel_title") or ch["channel_username"] for ch in missing
                )
                await event.answer(
                    f"Вы ещё не подписаны: {names}", show_alert=True
                )
            return None

        missing = await _check_membership(bot, user.id, _gate_channels)
        if not missing:
            return await handler(event, data)

        # Block — show gate screen
        text = _gate_text(_gate_channels)
        markup = _gate_markup(_gate_channels)

        if isinstance(event, Message):
            await event.answer(text, reply_markup=markup, parse_mode="HTML")
        elif isinstance(event, CallbackQuery):
            try:
                await event.answer("Необходима подписка на каналы!", show_alert=True)
            except Exception:
                pass
            try:
                await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                try:
                    await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
                except Exception:
                    pass

        return None
