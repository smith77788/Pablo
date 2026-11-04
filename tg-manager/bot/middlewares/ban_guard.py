"""Забаненный владелец не пользуется ботом (см. services/platform_ban.py).

Личные сообщения, нажатия кнопок и инлайн-запросы забаненного пользователя
дальше обработчиков не идут. Сообщения в группах не трогаем: модерация чатов
должна видеть и забаненного. Напоминание о бане — не чаще раза в 10 минут,
кнопку гасим всегда, чтобы у пользователя не крутились часики.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject

from services import platform_ban

log = logging.getLogger(__name__)

_NOTIFY_EVERY = 600.0


class BanGuardMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._told: dict[int, float] = {}

    def _should_tell(self, uid: int) -> bool:
        now = time.monotonic()
        if now - self._told.get(uid, -_NOTIFY_EVERY) < _NOTIFY_EVERY:
            return False
        if len(self._told) > 10_000:
            self._told.clear()
        self._told[uid] = now
        return True

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        if isinstance(event, Message):
            chat = getattr(event, "chat", None)
            if chat is None or chat.type != "private":
                return await handler(event, data)
        elif not isinstance(event, (CallbackQuery, InlineQuery)):
            return await handler(event, data)

        if not await platform_ban.is_banned(data.get("pool"), int(user.id)):
            return await handler(event, data)

        try:
            if isinstance(event, CallbackQuery):
                await event.answer(platform_ban.BANNED_TEXT, show_alert=True)
            elif isinstance(event, InlineQuery):
                await event.answer([], cache_time=60, is_personal=True)
            elif self._should_tell(int(user.id)):
                await event.answer(platform_ban.BANNED_TEXT)
        except Exception:
            log.debug("ban_guard: не удалось ответить %s", user.id, exc_info=True)
        return None
