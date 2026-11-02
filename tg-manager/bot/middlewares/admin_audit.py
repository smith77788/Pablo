"""Журнал действий админов в боте: каждая кнопка и команда админ-панели.

Зачем. Админ платформы видит и меняет данные ВСЕХ владельцев: подписки, баны,
рассылки, ключи AI, токены. До этого в журнал admin_audit_log попадала только
рассылка из мини-аппа, а админ-панель бота (два роутера, десятки кнопок) не
оставляла следа вовсе: если доступ утёк, понять, что успели сделать, было не
из чего.

Что пишется. Нажатие кнопки — её callback_data; команда — только сама команда
с аргументами (текст, начинающийся с «/»). Свободный текст НЕ пишется никогда:
в FSM админ вводит ключи AI, токены и секретную фразу, и журнал не должен
становиться ещё одним местом, где лежат секреты.

Middleware внутренний (router.*.middleware), значит срабатывает только когда
событие дошло до хендлера админ-роутера, и только для тех, кто админ: случайные
нажатия чужих людей журнал не засоряют.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

log = logging.getLogger(__name__)

_MAX_LEN = 200


def describe_admin_event(event: TelegramObject) -> tuple[int, str] | None:
    """(user_id, действие) для журнала или None, если писать нечего."""
    if isinstance(event, CallbackQuery):
        if not event.from_user:
            return None
        return event.from_user.id, f"bot:cb:{(event.data or '?')[:_MAX_LEN]}"
    if isinstance(event, Message):
        if not event.from_user:
            return None
        text = event.text or ""
        if not text.startswith("/"):
            return None   # свободный текст: ключи, токены, фраза — не пишем
        return event.from_user.id, f"bot:cmd:{text[:_MAX_LEN]}"
    return None


class AdminAuditMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        desc = describe_admin_event(event)
        if desc is not None:
            uid, action = desc
            try:
                from services.mini_app_api import _is_admin
                is_admin = _is_admin(uid)
            except Exception:
                is_admin = False
            pool = data.get("pool")
            if is_admin and pool is not None:
                from database.db import log_admin_action
                await log_admin_action(pool, uid, action, None, {"source": "bot"},
                                       source="bot")
        return await handler(event, data)


def install(*routers) -> None:
    """Повесить журнал на сообщения и кнопки переданных роутеров."""
    mw = AdminAuditMiddleware()
    for r in routers:
        r.message.middleware(mw)
        r.callback_query.middleware(mw)
