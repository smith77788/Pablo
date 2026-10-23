"""Фоновый сметатель капчи «Модератора чатов».

Новичок, не нажавший «Я не бот» до дедлайна, должен быть удалён из чата. Кнопку
он может проигнорировать (или это реально бот), поэтому наказание применяем НЕ по
клику, а по времени — этим фоновым циклом. Записи персистентны, так что кара
догоняет нарушителя даже после рестарта бота.
"""
from __future__ import annotations

import asyncio
import logging
import time

import asyncpg

from services import chat_guard as cg
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 15.0


async def _punish_expired(pool: asyncpg.Pool, bot, row: dict) -> None:
    """Наказать не прошедшего капчу: удалить сообщение-капчу + кик/бан."""
    chat_id = int(row["chat_id"])
    uid = int(row["user_id"])
    action = row.get("action") or "kick"
    # Убрать сообщение с кнопкой (оно больше не нужно).
    try:
        if row.get("captcha_msg_id"):
            await bot.delete_message(chat_id, int(row["captcha_msg_id"]))
    except Exception:
        log_exc_swallow(log, "guard_runner: delete captcha msg")
    # Наказание.
    try:
        if action == "ban":
            await bot.ban_chat_member(chat_id, uid)
        else:  # kick — бан+разбан, чтобы мог вернуться и пройти капчу заново
            await bot.ban_chat_member(chat_id, uid)
            await asyncio.sleep(0.4)
            await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        log.info("chat_guard_runner: капча не пройдена chat=%s user=%s → %s",
                 chat_id, uid, action)
    except Exception:
        log_exc_swallow(log, "guard_runner: punish")


async def sweep_once(pool: asyncpg.Pool, bot, limit: int = 50) -> int:
    """Один проход: наказать всех просроченных. Возвращает число обработанных."""
    try:
        expired = await cg.captcha_pop_expired(pool, limit=limit)
    except Exception:
        log.exception("chat_guard_runner: не смог выбрать просроченные капчи")
        return 0
    for row in expired:
        await _punish_expired(pool, bot, row)
    return len(expired)


async def run(pool: asyncpg.Pool, bot) -> None:
    """Фоновый цикл: каждые ~15 сек кикает не прошедших капчу к дедлайну."""
    log.info("chat_guard_runner: старт цикла капчи")
    while True:
        try:
            await sweep_once(pool, bot)
        except Exception:
            log.exception("chat_guard_runner: цикл упал, продолжаем")
        await asyncio.sleep(_POLL_INTERVAL_S)
