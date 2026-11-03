"""Антифлуд бота: один пользователь не может завалить бота апдейтами.

Раньше у главного бота не было никакого ограничения частоты: каждое сообщение
и каждое нажатие кнопки шло в обработчики, которые ходят в базу, в Telegram и
к ИИ. Один скрипт с личного аккаунта (или подделанные апдейты) занимал пул
соединений и цикл событий для всех владельцев сразу, а секретную фразу входа в
админку можно было перебирать без счёта.

Правило: не больше BOT_FLOOD_MAX апдейтов за BOT_FLOOD_WINDOW секунд от
одного пользователя (по умолчанию 40 за 10 с — с запасом на пересылку пачки
сообщений и альбомов). Сверх лимита апдейт отбрасывается, а пользователь один
раз за окно получает понятное «слишком часто».

Только ЛИЧНЫЕ сообщения и нажатия кнопок. Сообщения в группах не трогаем:
модератор чатов обязан видеть именно того, кто флудит в группе.
Постоянные админы (ADMIN_IDS) не ограничиваются.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

log = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


class FloodGuardMiddleware(BaseMiddleware):
    """Скользящее окно на пользователя. Состояние — в экземпляре, не в модуле."""

    def __init__(self, max_events: int | None = None, window: float | None = None,
                 max_users: int = 50_000) -> None:
        self.max_events = max_events or _env_int("BOT_FLOOD_MAX", 40, 5)
        self.window = float(window or _env_int("BOT_FLOOD_WINDOW", 10, 1))
        self.max_users = max_users
        self._hits: dict[int, deque] = {}
        self._warned_at: dict[int, float] = {}

    def _sweep(self, now: float) -> None:
        cutoff = now - self.window
        for uid in [u for u, q in self._hits.items() if not q or q[-1] < cutoff]:
            self._hits.pop(uid, None)
            self._warned_at.pop(uid, None)

    @staticmethod
    def _is_admin(uid: int) -> bool:
        """Проверка ПРАВ (не оповещение): постоянные админы не ограничиваются."""
        return uid in _admin_ids()

    def allow(self, uid: int, now: float | None = None) -> bool:
        if self._is_admin(uid):
            return True
        now = time.monotonic() if now is None else now
        q = self._hits.get(uid)
        if q is None:
            if len(self._hits) >= self.max_users:
                self._sweep(now)
            q = self._hits[uid] = deque()
        cutoff = now - self.window
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        return True

    def _should_warn(self, uid: int, now: float) -> bool:
        last = self._warned_at.get(uid, 0.0)
        if now - last < self.window:
            return False
        self._warned_at[uid] = now
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
        elif not isinstance(event, CallbackQuery):
            return await handler(event, data)

        uid = int(user.id)
        if self.allow(uid):
            return await handler(event, data)

        now = time.monotonic()
        if self._should_warn(uid, now):
            log.warning("flood_guard: пользователь %s превысил %d апдейтов за %.0f с",
                        uid, self.max_events, self.window)
            text = "⏳ Слишком часто — подождите несколько секунд и повторите."
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=False)
                else:
                    await event.answer(text)
            except Exception:
                log.debug("flood_guard: не удалось предупредить %s", uid, exc_info=True)
        elif isinstance(event, CallbackQuery):
            # Кнопку всё равно гасим, иначе у пользователя крутятся часики.
            try:
                await event.answer()
            except Exception:
                pass
        return None
